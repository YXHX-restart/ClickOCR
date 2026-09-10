"""RapidOCR 引擎实现 —— M1-1。

支持三档 v6 离线模型集（按 config [software] ocr_model 选择）：
- v6-tiny（默认）：统一 `rapidocr` 包（>=3.9.2，OCRVersion.PPOCRV6）
  + PP-OCRv6 tiny ONNX（resources/models_v6/，ModelScope 官方导出，SHA256 校验）。
- v6-small：PP-OCRv6 small ONNX + 通用字典 ppocrv6_dict.txt。
- v6-medium：PP-OCRv6 medium ONNX + 通用字典 ppocrv6_dict.txt。
  v6 无独立 cls 模型，强制 use_cls=False；三档可在运行期 switch_model 热切换
  （仅重建内部 RapidOCR 引擎，包装对象不变，表格引擎绑定的 recognize 引用依然有效）。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

import numpy as np

from .types import OcrBox

# 统一包（PP-OCRv6 支持）：rapidocr 3.9.x（旧 PP-OCRv3 回退已移除）
try:
    from rapidocr import RapidOCR as _RapidOCR_NEW
    from rapidocr import EngineType, ModelType, OCRVersion
    _NEW = True
except Exception:  # pragma: no cover - 依赖缺失
    _RapidOCR_NEW = None
    _NEW = False

_RES = Path(__file__).resolve().parent.parent / "resources"
_DIR_V6 = _RES / "models_v6"

# ---- DirectML 适配器选择 ----
# rapidocr 的 ProviderConfig 用 omegaconf DictConfig 传 EP options，onnxruntime
# 不认（报 EP Error 回退 CPU）；且直接传 dml_ep_cfg 也会崩（onnxruntime-directml
# 1.24.4 的兼容问题）。方案：探测可用 D3D12 适配器，monkeypatch get_ep_list
# 用普通 dict 注入 device_id —— 老电脑（如 930MX 不支持 DML）自动跳过，
# 落到核显（HD620）或回退 CPU。
_DML_DEVICE: int | None = None      # None/负值 = 未探测/不可用；>=0 = 选中适配器
_DML_PATCHED = False

try:
    from rapidocr.inference_engine.onnxruntime import provider_config as _pc

    _orig_get_ep_list = _pc.ProviderConfig.get_ep_list

    def _dml_get_ep_list(self):
        lst = _orig_get_ep_list(self)
        if _DML_DEVICE is not None and _DML_DEVICE >= 0:
            return [
                ("DmlExecutionProvider", {"device_id": str(_DML_DEVICE)})
                if ep == "DmlExecutionProvider"
                else (ep, dict(cfg) if cfg else {})
                for ep, cfg in lst
            ]
        return lst

    _pc.ProviderConfig.get_ep_list = _dml_get_ep_list
    _DML_PATCHED = True
except Exception:  # pragma: no cover - rapidocr 未装/版本差异
    _DML_PATCHED = False


def _probe_infer_ms(model_path, providers) -> float | None:
    """建 session 预热后跑 5 次推理，返回平均毫秒；不可用返回 None。

    输入用 640x640（接近真实截图 det 负载）——320 太小会低估 GPU 收益
    （实测 tiny det 320x320 比值 <1.5 而完整管线 1.63×，导致门槛误拒）。
    """
    try:
        import onnxruntime as ort
        sess = ort.InferenceSession(str(model_path), providers=providers)
        io = sess.get_inputs()[0]
        feed = {io.name: np.random.rand(1, 3, 640, 640).astype("float32")}
        for _ in range(3):
            sess.run(None, feed)          # 预热 + 实际推理验证（930MX 等建成功跑失败会被排除）
        ts = []
        for _ in range(5):
            t0 = time.perf_counter()
            sess.run(None, feed)
            ts.append((time.perf_counter() - t0) * 1000)
        return sum(ts) / len(ts)
    except Exception:  # noqa: BLE001
        return None


def _ensure_nvidia_dlls() -> None:
    """预加载 nvidia cudnn/cublas DLL（绝对路径 LoadLibrary），CUDA EP 前置。

    onnxruntime-gpu 的 CUDA EP 运行时用「裸名 LoadLibrary」加载 cudnn/cublas
    （如 RequireCudnnHandle 时 LoadLibrary("cudnn64_9.dll")），而 Windows 运行时
    LoadLibrary 只搜 exe 目录 / System32 / PATH，不含 site-packages/onnxruntime/capi
    或 _internal/onnxruntime/capi → 必须先用绝对路径把相关 DLL 预加载进进程，
    之后 onnxruntime 裸名加载命中「已加载模块」。
    开发环境：site-packages/nvidia/{cudnn,cublas}/bin（cudnn 9 拆分 DLL 所在）；
    打包版（frozen）：DLL 已整合进 _internal/onnxruntime/capi/，预加载其中
    cudnn/cublas/nvrtc 等 nvidia 相关 DLL —— PyInstaller 的 onnxruntime hook 只把
    nvidia/cudnn/bin 的个别拆分 DLL 收进 _internal/nvidia/，主 dll（cudnn64_9.dll）
    在 capi 而未被预加载 → 打包版真机 CUDA EP 静默回退 CPU（实测
    "No registered plugin EP device found" + gpu_active=False）。
    """
    if sys.platform != "win32":
        return
    try:
        import onnxruntime as _ort
        sp = Path(_ort.__file__).resolve().parent.parent
        capi = sp / "onnxruntime" / "capi"
    except Exception:  # pragma: no cover
        return
    import logging
    _lg = logging.getLogger("clickocr.ocr")
    # 候选目录：开发环境 nvidia 包目录 + 打包版 capi（frozen 下 nvidia 目录不全）
    _dirs = [sp.joinpath("nvidia", "cudnn", "bin"),
             sp.joinpath("nvidia", "cublas", "bin"),
             capi]
    _loaded = set()
    _fail = 0
    for d in _dirs:
        if not d.is_dir():
            continue
        for dll in sorted(d.glob("*.dll")):
            _fn = dll.name.lower()
            # capi 只挑 nvidia 相关（cudnn/cublas/nvrtc/cufft/cudart），
            # 不碰 onnxruntime 自身 dll（onnxruntime.dll/pybind/providers）
            if d == capi and not any(_fn.startswith(p) for p in
                                     ("cudnn", "cublas", "nvrtc", "cufft", "cudart")):
                continue
            if dll in _loaded:
                continue
            _loaded.add(dll)
            try:
                ctypes.WinDLL(str(dll))
            except Exception as e:  # noqa: BLE001 个别 dll 依赖缺失不致命
                _fail += 1
                _lg.warning("预加载 %s 失败: %s", dll.name, e)
    _lg.info("nvidia DLL 预加载完成: %d 成功 %d 失败", len(_loaded) - _fail, _fail)


def probe_cuda_devices(det_path=None) -> list[dict]:
    """探测所有 CUDA 设备（NVIDIA 多卡）的可用性与相对 CPU 推理倍率。

    返回结构同 probe_dml_devices。CUDA 环境（onnxruntime-gpu / CUDA 版打包）
    可用；无 CUDA EP 时返回空列表。供设置面板「检测」按钮在 CUDA 版使用。
    """
    if not (_NEW and _DIR_V6.exists()):
        return []
    _ensure_nvidia_dlls()
    try:
        import onnxruntime as ort
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            return []
    except Exception:  # pragma: no cover
        return []
    det = Path(det_path) if det_path else _DIR_V6 / _V6_MODELS["v6-tiny"][1]
    cpu_ms = _probe_infer_ms(det, [("CPUExecutionProvider", {})])
    results = []
    import onnxruntime as ort
    for dev in range(8):
        try:
            sess = ort.InferenceSession(
                str(det),
                providers=[("CUDAExecutionProvider", {"device_id": str(dev)}),
                           "CPUExecutionProvider"])
            # 不存在的卡 onnxruntime 会静默回退 CPU —— 需确认真用 CUDA EP
            if sess.get_providers()[0] != "CUDAExecutionProvider":
                results.append({"id": dev, "ok": False})
                continue
            gpu_ms = _probe_infer_ms(
                det,
                [("CUDAExecutionProvider", {"device_id": str(dev)}),
                 "CPUExecutionProvider"])
        except Exception as e:  # noqa: BLE001 设备不可用 → 试下一个
            results.append({"id": dev, "ok": False, "err": str(e)[:100]})
            continue
        if gpu_ms is None or cpu_ms is None:
            results.append({"id": dev, "ok": False,
                            "err": "session 可用但推理失败"})
            continue
        results.append({
            "id": dev,
            "ok": True,
            "cpu_ms": round(cpu_ms, 1),
            "dml_ms": round(gpu_ms, 1),
            "speedup": round(cpu_ms / gpu_ms, 2),
        })
    return results


def probe_dml_devices(det_path=None) -> list[dict]:
    """探测所有 D3D12 适配器的 DML 可用性与相对 CPU 推理倍率（不做自动门槛）。

    返回 [{"id": 0, "ok": True, "cpu_ms": .., "dml_ms": .., "speedup": ..}, ...]；
    ok=False 表示该设备不可用（930MX 等建 session 成功但推理失败会被排除）。
    供设置面板「检测」按钮记录并展示；det_path 缺省用 v6-tiny det。
    """
    if not (_NEW and _DIR_V6.exists()):
        return []
    try:
        import onnxruntime as ort
        if "DmlExecutionProvider" not in ort.get_available_providers():
            return []
    except Exception:  # pragma: no cover
        return []
    det = Path(det_path) if det_path else _DIR_V6 / _V6_MODELS["v6-tiny"][1]
    cpu_ms = _probe_infer_ms(det, [("CPUExecutionProvider", {})])
    results = []
    import onnxruntime as ort
    for dev in range(8):
        try:
            sess = ort.InferenceSession(
                str(det),
                providers=[("DmlExecutionProvider", {"device_id": str(dev)}),
                           "CPUExecutionProvider"])
            if sess.get_providers()[0] != "DmlExecutionProvider":
                results.append({"id": dev, "ok": False})
                continue
            dml_ms = _probe_infer_ms(
                det,
                [("DmlExecutionProvider", {"device_id": str(dev)}),
                 "CPUExecutionProvider"])
        except Exception as e:  # noqa: BLE001 设备不可用 → 试下一个
            results.append({"id": dev, "ok": False, "err": str(e)[:100]})
            continue
        if dml_ms is None or cpu_ms is None:
            results.append({"id": dev, "ok": False,
                            "err": "session 可用但推理失败"})
            continue
        results.append({
            "id": dev,
            "ok": True,
            "cpu_ms": round(cpu_ms, 1),
            "dml_ms": round(dml_ms, 1),
            "speedup": round(cpu_ms / dml_ms, 2),
        })
    return results


def _detect_dml_device(det_path=None) -> int:
    """取第一个可用的 DML 适配器索引（仅可用性判断，不做性能门槛）。

    环境变量 CLICKOCR_DML_DEVICE 可强制指定；全部不可用返回 -1（回退 CPU）。
    """
    env = os.environ.get("CLICKOCR_DML_DEVICE", "").strip()
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    for r in probe_dml_devices(det_path):
        if r.get("ok"):
            return r["id"]
    return -1

# v6 三档 → (ModelType, det 文件, rec 文件, 字典文件)；仅统一包可用时定义
if _NEW:
    _V6_MODELS = {
        "v6-tiny":   (ModelType.TINY,   "PP-OCRv6_det_tiny.onnx",
                      "PP-OCRv6_rec_tiny.onnx", "ppocrv6_tiny_dict.txt"),
        "v6-small":  (ModelType.SMALL,  "PP-OCRv6_det_small.onnx",
                      "PP-OCRv6_rec_small.onnx", "ppocrv6_dict.txt"),
        "v6-medium": (ModelType.MEDIUM, "PP-OCRv6_det_medium.onnx",
                      "PP-OCRv6_rec_medium.onnx", "ppocrv6_dict.txt"),
    }
else:
    _V6_MODELS = {}


def _physical_cores() -> int:
    """物理核心数近似（逻辑核/2），避免 ONNX Runtime 默认全逻辑核导致的超订开销。"""
    try:
        n = os.cpu_count() or 4
        return max(1, n // 2)
    except Exception:
        return -1


# ============ NUMA 感知（跨平台；仅多 NUMA 节点时生效） ============
def _parse_cpulist(s: str) -> set:
    """解析 Linux /sys 的 cpulist 文本（如 '0-3,8-11'）为 CPU 集合。"""
    cpus = set()
    for part in s.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            a, b = part.split('-')
            cpus.update(range(int(a), int(b) + 1))
        else:
            cpus.add(int(part))
    return cpus


class _GROUP_AFFINITY(ctypes.Structure):
    _fields_ = [
        ("Mask", ctypes.c_ulonglong),
        ("Group", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 3),
    ]


class _PROCESSOR_RELATIONSHIP(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_ubyte),
        ("EfficiencyClass", ctypes.c_ubyte),
        ("Reserved", ctypes.c_ubyte * 20),
        ("GroupCount", ctypes.c_ushort),
        ("GroupMask", _GROUP_AFFINITY),
    ]


class _NUMA_NODE_RELATIONSHIP(ctypes.Structure):
    _fields_ = [
        ("NodeNumber", ctypes.c_ulong),
        ("Reserved", ctypes.c_ubyte * 20),
        ("GroupMask", _GROUP_AFFINITY),
    ]


class _SLPI_EX_U(ctypes.Union):
    _fields_ = [
        ("Processor", _PROCESSOR_RELATIONSHIP),
        ("NumaNode", _NUMA_NODE_RELATIONSHIP),
    ]


class _SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX(ctypes.Structure):
    _fields_ = [
        ("Relationship", ctypes.c_ulong),
        ("Size", ctypes.c_ulong),
        ("_u", _SLPI_EX_U),
    ]


def _windows_numa_info():
    """返回 (nodes, cores)。
    nodes: [(node_id, mask, group), ...]   —— mask 为该节点的亲和性位图
    cores: [(mask, group, flags), ...]     —— 每个逻辑处理器（含 SMT 线程）
    结构与 _linux_numa_info 对齐，便于上层统一处理。
    仅多 NUMA 节点时上层才会据此绑定；单节点返回内容亦可用于计物理核。
    """
    try:
        k32 = ctypes.windll.kernel32
        RelationProcessorCore = 0
        RelationNumaNode = 1
        RelationNumaNodeEx = 6
        buffer_size = ctypes.c_ulong(0)
        # 第一次调用：只探长度（传 NULL 缓冲）
        k32.GetLogicalProcessorInformationEx(
            ctypes.c_ulong(0xFFFF),  # 0xFFFF = RelationAll
            None, ctypes.byref(buffer_size))
        if buffer_size.value == 0:
            return [], []
        buf = ctypes.create_string_buffer(buffer_size.value)
        ret = k32.GetLogicalProcessorInformationEx(
            ctypes.c_ulong(0xFFFF), buf, ctypes.byref(buffer_size))
        if not ret:
            return [], []
        nodes = []
        cores = []
        offset = 0
        rec_size = ctypes.sizeof(_SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX)
        while offset + rec_size <= buffer_size.value:
            info = _SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX.from_buffer_copy(
                buf, offset)
            rel = info.Relationship
            if rel == RelationProcessorCore:
                ga = info._u.Processor.GroupMask
                cores.append((ga.Mask, ga.Group, info._u.Processor.Flags))
            elif rel in (RelationNumaNode, RelationNumaNodeEx):
                ga = info._u.NumaNode.GroupMask
                nodes.append((info._u.NumaNode.NodeNumber, ga.Mask, ga.Group))
            offset += info.Size
        return nodes, cores
    except Exception:
        return [], []


def _linux_numa_info():
    """返回 (nodes, cores)，结构与 _windows_numa_info 对齐。
    注意：亲和性位图按 64 位掩码表示（Single NUMA 节点内 CPU 数通常 < 64，
    满足目标多路服务器场景；超大规模节点需改列表表示，当前不覆盖）。
    """
    try:
        import glob
        base = "/sys/devices/system/node"
        node_dirs = sorted(
            glob.glob(os.path.join(base, "node[0-9]*")),
            key=lambda p: int(os.path.basename(p)[4:]))
        nodes = []
        for d in node_dirs:
            nid = int(os.path.basename(d)[4:])
            try:
                with open(os.path.join(d, "cpulist")) as f:
                    cpu_set = _parse_cpulist(f.read())
            except Exception:
                cpu_set = set()
            mask = 0
            for c in cpu_set:
                if c < 64:
                    mask |= (1 << c)
            nodes.append((nid, mask, 0))  # Linux 单节点无 processor group 概念
        # 物理核：按 core_id 去重（同 core_id 的多线程属同一物理核）
        cores = []
        cpu_topo = "/sys/devices/system/cpu"
        for c in range(256):
            core_path = os.path.join(
                cpu_topo, "cpu%d" % c, "topology", "core_id")
            try:
                with open(core_path) as f:
                    core_id = int(f.read().strip())
            except Exception:
                continue
            mask = (1 << c) if c < 64 else 0
            cores.append((mask, 0, 1))  # flags=1 标记为主线程/物理核
        return nodes, cores
    except Exception:
        return [], []


def _get_numa_info():
    if sys.platform == "win32":
        return _windows_numa_info()
    if sys.platform.startswith("linux"):
        return _linux_numa_info()
    return [], []


def _single_numa_physical_cores() -> int:
    """取「单 NUMA 节点物理核心数」。单节点（含未知）时回退到 _physical_cores()。"""
    nodes, cores = _get_numa_info()
    if len(nodes) <= 1:
        return _physical_cores()
    _nid, node_mask, _grp = nodes[0]
    n = sum(1 for (m, _g, _s) in cores if m & node_mask)
    return max(1, n) if n else _physical_cores()


_NUMA_APPLIED = False


def _apply_numa_affinity() -> None:
    """将进程（/主线程）绑定到 NUMA 节点 0，规避跨节点访存。
    仅多 NUMA 节点时执行；单节点直接返回，零开销。
    """
    nodes, _ = _get_numa_info()
    if len(nodes) <= 1:
        return
    _nid, node_mask, node_group = nodes[0]
    try:
        if sys.platform == "win32":
            k32 = ctypes.windll.kernel32
            if node_group == 0:
                # 单 processor group：进程级亲和掩码即可
                k32.GetCurrentProcess.restype = ctypes.c_void_p
                k32.SetProcessAffinityMask.argtypes = [
                    ctypes.c_void_p, ctypes.c_ulonglong]
                k32.SetProcessAffinityMask.restype = ctypes.c_int
                k32.SetProcessAffinityMask(
                    k32.GetCurrentProcess(), node_mask)
            else:
                # 跨 group：必须用 SetThreadGroupAffinity（带 GROUP_AFFINITY）
                ga = _GROUP_AFFINITY(Mask=node_mask, Group=node_group)
                k32.GetCurrentThread.restype = ctypes.c_void_p
                k32.SetThreadGroupAffinity.argtypes = [
                    ctypes.c_void_p, ctypes.POINTER(_GROUP_AFFINITY),
                    ctypes.c_void_p]
                k32.SetThreadGroupAffinity.restype = ctypes.c_int
                k32.SetThreadGroupAffinity(
                    k32.GetCurrentThread(), ctypes.byref(ga), None)
        else:
            # Linux：os.sched_setaffinity 吃 CPU id 列表，最直观
            cpus = [c for c in range(256) if node_mask & (1 << c)]
            os.sched_setaffinity(0, cpus)
    except Exception:
        # 绑定失败不致命：继续跑，只是可能跨节点
        pass


def _ocr_thread_count() -> int:
    """供 onnxruntime intra_op_num_threads 使用的线程数。"""
    return _single_numa_physical_cores()


class RapidOcrEngine:
    def __init__(self, config_path=None, use_cls: bool = True,
                 model_set: str = "v6-tiny", use_cuda: bool = False,
                 use_dml: bool = False, lazy: bool = False):
        self._use_cls = use_cls
        self._model_set = model_set
        self._use_cuda = use_cuda
        self._use_dml = use_dml
        self._gpu_active = False
        self._lazy = lazy
        # 懒加载：引擎对象先建（参数已存），模型等首次 recognize 时才真正构建，
        # 避免「OCR 引擎常驻」关闭时启动即占用 ~200-400MB 内存。
        self._engine = None
        if not lazy:
            self._ensure_engine(config_path)

    def _ensure_engine(self, config_path=None):
        """首次识别前构建底层引擎（懒加载用，幂等）。"""
        global _NUMA_APPLIED
        if not _NUMA_APPLIED:
            _apply_numa_affinity()
            _NUMA_APPLIED = True
        if self._engine is not None:
            return self._engine
        self._engine = self._build_v6()
        return self._engine

    # ---- 构建 ----
    def _build_v6(self):
        if _RapidOCR_NEW is None:
            raise RuntimeError("未找到可用 OCR 后端：rapidocr(3.9+) 未安装")
        mtype, det_name, rec_name, dict_name = _V6_MODELS.get(
            self._model_set, _V6_MODELS["v6-tiny"])
        params = {
            "Global.use_cls": False,                    # v6 无独立方向分类
            "EngineConfig.onnxruntime.intra_op_num_threads": _ocr_thread_count(),
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Det.ocr_version": OCRVersion.PPOCRV6,
            "Det.model_type": mtype,
            "Det.model_path": str(_DIR_V6 / det_name),
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Rec.ocr_version": OCRVersion.PPOCRV6,
            "Rec.model_type": mtype,
            "Rec.model_path": str(_DIR_V6 / rec_name),
            "Rec.rec_keys_path": str(_DIR_V6 / dict_name),
        }
        # GPU provider 选择：rapidocr 只认 use_cuda/use_dml flag
        # （直接传 providers 列表会被其 ProviderConfig 忽略，引擎静默跑 CPU）
        # DML 选卡经 _detect_dml_device 探测 + monkeypatch 注入 device_id
        if self._use_cuda:
            _ensure_nvidia_dlls()      # 引擎构建前预加载 cudnn/cublas（开发环境）
            params["EngineConfig.onnxruntime.use_cuda"] = True
            # CUDA EP 显存策略（修复"截图几次显存耗尽"）：rapidocr 默认 cuda_ep_cfg
            # 是 EXHAUSTIVE 卷积搜索 + kNextPowerOfTwo 竞技场扩展。前者每遇新输入
            # shape（rec 每张截图的文字块宽度/batch 都在变）就全量 benchmark 并申请
            # 大 workspace，缓存只增不释放；后者按 2 的幂翻倍预占显存、长跑只涨不缩。
            # 改为 HEURISTIC（启发式选卷积算法，快且近最优，省掉 benchmark 开销）+
            # kSameAsRequested（按需精确扩展，不翻倍预占）——不降低识别速度。
            params["EngineConfig.onnxruntime.cuda_ep_cfg"] = {
                "device_id": 0,
                "arena_extend_strategy": "kSameAsRequested",
                "cudnn_conv_algo_search": "HEURISTIC",
                "do_copy_in_default_stream": True,
            }
        elif self._use_dml:
            params["EngineConfig.onnxruntime.use_dml"] = True
            global _DML_DEVICE
            _DML_DEVICE = _detect_dml_device(_DIR_V6 / det_name)
        engine = _RapidOCR_NEW(params=params)
        # DML：gpu_active 由探测判定（探测时已建 session 并跑过推理验证）；
        # 不可再调 _probe_gpu —— onnxruntime-directml 多 DML session 共存会段错误（实测）
        if self._use_dml:
            self._gpu_active = _DML_DEVICE >= 0
        else:
            self._gpu_active = self._probe_gpu()
        return engine

    def _probe_gpu(self) -> bool:
        """用当前档位 v6 det 模型建 session 探测所选 GPU provider 是否真正可用。"""
        if not (self._use_cuda or self._use_dml):
            return False
        ep = "CUDAExecutionProvider" if self._use_cuda else "DmlExecutionProvider"
        try:
            import onnxruntime as ort
            # 只做可用性判断，不再额外新建临时 InferenceSession 探测——
            # 之前这里会为「探测」再加载一遍 det 模型到 GPU（与主引擎 det 重复占用
            # ~30-50MB 权重 + arena），且 onnxruntime 全局 CUDA 竞技场在 session 析构
            # 后仍保留峰值，属隐性显存占用。主引擎构建时已按 CUDA→CPU 顺序落位，
            # 真用不上 CUDA 时会自动回退 CPU，此处 get_device() 判断与 rapidocr
            # 自身 is_cuda_available 一致，足够判 gpu_active。
            if ep not in ort.get_available_providers():
                return False
            return ort.get_device() == "GPU"
        except Exception:
            return False

    # ---- 档位切换 ----
    def switch_model(self, model_set: str) -> None:
        """M2 — 运行期切换模型档位（v6-tiny / v6-small / v6-medium）。

        仅丢弃并重建内部 RapidOCR 引擎（self._engine = None 后 _ensure_engine()），
        保持本 RapidOcrEngine 包装对象不变——app 层 table_engine 等已绑定的
        self.recognize 引用依然有效，表格识别自动跟着新档位走。
        切换为同步加载（首次约 1–2s），调用方注意放后台线程。
        """
        self._model_set = model_set
        self._engine = None          # 丢弃旧引擎（释放模型内存）
        self._gpu_active = False
        self._ensure_engine()        # 同步重建（构建失败向上抛，由调用方兜底）

    # ---- 识别 ----
    def recognize(self, image: np.ndarray) -> list[OcrBox]:
        engine = self._ensure_engine()
        out = engine(image, use_det=True, use_cls=False, use_rec=True)
        result = out[0] if isinstance(out, tuple) else out

        # 统一包：boxes(N,4,2) / txts / scores
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            return []          # 无识别结果（空图/纯色图）
        return [
            OcrBox(
                text=str(t),
                polygon=np.asarray(pts, dtype=int).tolist(),
                score=float(s),
            )
            for pts, t, s in zip(boxes, result.txts or [], result.scores or [])
        ]

    def get_info(self) -> dict:
        tier = {"v6-tiny": "tiny", "v6-small": "small",
                "v6-medium": "medium"}.get(self._model_set, "tiny")
        return {
            "engine": "RapidOCR(ONNX)",
            "model_set": f"PP-OCRv6-{tier}",
            "device": ("dml" if (self._use_dml and self._gpu_active)
                       else ("cuda" if self._gpu_active else "cpu")),
            "gpu_available": self._gpu_active,
        }
