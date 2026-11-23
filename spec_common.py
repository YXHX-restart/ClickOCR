"""PyInstaller spec 公共逻辑 —— ClickOCR.spec / _cpu.spec / _dml.spec 共用。

**为什么不放在 `packaging/` 目录**：spec 执行时会把 SPECPATH（= 本目录）插进
`sys.path`；目录名若叫 `packaging` 会遮蔽 PyInstaller 自己依赖的同名 PyPI 包。

三个 spec 原有约 80 行逐字重复（rapidocr/zxingcpp 的收集与瘦身、pillow_heif 的
DLL 兜底、CUDA 与 ffmpeg 剔除、EXE/COLLECT 收尾），此处收敛成一份。

**必须保持原有的列表拼接顺序**：PyInstaller 对同名条目按「先到者生效」，
顺序变化即行为变化（已用 `scripts/_spec_probe.py` 做重构前后逐值比对）。

`slim=True`（cpu / dml）：剔除 rapidocr 未用的推理后端与重复的 v6 模型缓存。
`slim=False`（gpu）：原样全收。
"""
from __future__ import annotations

import glob
import os
import sysconfig

from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules


def _bn(path: str) -> str:
    return os.path.basename(path.replace("\\", "/"))


# ---- rapidocr：未用推理后端 / 重复模型缓存 ----
# ClickOCR 显式 Det/Rec.engine_type=ONNXRUNTIME，只加载 onnxruntime 后端
EXCLUDE_ENGINES = (
    "rapidocr.inference_engine.paddle",
    "rapidocr.inference_engine.openvino",
    "rapidocr.inference_engine.mnn",
    "rapidocr.inference_engine.pytorch",
    "rapidocr.inference_engine.tensorrt",
)
_EXCLUDE_ENGINE_DIRS = tuple("/inference_engine/%s/" % e.split(".")[-1]
                             for e in EXCLUDE_ENGINES)
# rapidocr/models/ 内仅保留 cls：v6 small det/rec 与 resources/models_v6 重复的缓存
# （保留 ch_ppocr_mobile_v2.0_cls_mobile.onnx —— rapidocr 构造期会加载 cls）
_KEEP_RAPIDOCR_MODELS = {"ch_ppocr_mobile_v2.0_cls_mobile.onnx", ".gitkeep"}

#: 现代图片格式解码（拖放导入 avif / heic / heif）—— AVIF 由 Pillow(>=11.3) 内置，
#: HEIC/HEIF 走 pillow_heif。插件由 Pillow 动态注册，显式声明防漏收。
PILLOW_HEIF_HIDDENIMPORTS = [
    "_pillow_heif", "pillow_heif.HeifImagePlugin",
    "PIL.ImageOps",          # EXIF 方向纠正（exif_transpose / transpose）
    "PIL.AvifImagePlugin",
]

#: 打包时应剔除的 CUDA / TensorRT 运行库（约 884MB）
_CUDA_BIN_PATTERNS = (
    "onnxruntime_providers_cuda", "onnxruntime_providers_tensorrt",
    "cublas", "cudnn", "cufft", "nvrtc",
    "nvonnxparser", "nvinfer", "nvparsers",
)


#: 包目录里的**非 .py 数据文件** —— PyInstaller 只按导入关系收集 .py 与原生库，
#: `ui/base/` 这类包目录里的 .toml/.json 一律不会自动带上，必须在 datas 里显式声明。
#: 漏收的代价是「打包成功、运行即崩」：2026-09-17 配色收敛为 themes.toml 单真源后，
#: `theme.py` 用 `Path(__file__)` 同目录读取它，而 spec 没跟上 —— PyInstaller 退出码
#: 仍是 0，缺陷要等到有人双击 exe 才暴露（2026-09-18 由 `scripts/build_package.py
#: --smoke` 抓出）。
#: 新增「__file__ 同目录读取」的数据文件时，**必须**在这里登记；
#: `scripts/build_package.py` 会拿本表逐项回查产物，登记了就不会再漏。
PACKAGE_DATA_FILES = (
    ("ui/base/themes.toml", "ui/base"),
)


def resource_datas(config_entry: str = "resources/config.ini",
                   whole_dir: bool = False) -> list:
    """resources 的 datas 项（含 `PACKAGE_DATA_FILES` 登记的包内数据文件）。

    `whole_dir=True`（gpu）：整目录打包。
    否则只打用到的子项（排除 resources/models 里已失效的 v3 回退模型）。

    PyInstaller datas 语义：目录项 dest 必须带目录名（否则内容被展开到 dest 平铺）；
    单文件项 dest 指向目录（否则被当目录创建，产出 config.ini/config.ini 嵌套）。
    `config_entry` 允许 dml spec 换成「已覆写 ocr_device 的临时副本」。
    """
    if whole_dir:
        return [("resources", "resources"), *PACKAGE_DATA_FILES]
    return [
        ("resources/icons", "resources/icons"),
        ("resources/models_v6", "resources/models_v6"),
        (config_entry, "resources"),
        ("resources/ui_text.json", "resources"),
        ("resources/PP-OCRv6_models_manifest.txt", "resources"),
        *PACKAGE_DATA_FILES,
    ]


def collect_sqlite_vec() -> list:
    """sqlite-vec 扩展二进制（vec0 表用）。"""
    return collect_dynamic_libs("sqlite_vec")


def seed_config_entry(device: str, specpath: str) -> str:
    """生成「产物内 resources/config.ini」的临时副本，把默认加速档位覆写为 device。

    `resources/config.ini` 是**出厂模板**（只读，首次运行由 `ConfigManager` 复制到
    可写数据目录），三份产物共用同一个源文件，其 `ocr_device` 是通用兜底值 `cpu`。
    各产物只有自己那一种 ExecutionProvider，沿用 cpu 就等于「装了加速版却一直跑
    CPU」—— 故打包时按档位覆写，只作用于本 spec 的产物：

    · 源码 `resources/config.ini` 一个字不改 → 开发环境 / 其它产物照旧
    · 不引入任何运行期分支 → `.py` 业务代码零改动

    返回临时副本的路径（交给 `resource_datas(config_entry=...)`）。
    找不到唯一的 `ocr_device` 键时直接失败 —— 宁可不出包，也不出「加速档位不确定」的包。
    """
    src = os.path.join(specpath, "resources", "config.ini")
    lines = open(src, encoding="utf-8").read().splitlines(keepends=True)
    patched = 0
    for i, ln in enumerate(lines):
        s = ln.lstrip()
        if s.startswith("ocr_device") and "=" in s:
            head, _, tail = ln.partition("=")
            comment = ""
            for mark in ("#", ";"):              # 保留行尾注释
                pos = tail.find(mark)
                if pos != -1:
                    comment = tail[pos:].rstrip("\r\n")
                    tail = tail[:pos]
                    break
            eol = "\r\n" if ln.endswith("\r\n") else ("\n" if ln.endswith("\n") else "")
            lines[i] = head + "= " + device + ((" " + comment) if comment else "") + eol
            patched += 1
            break
    if patched != 1:
        raise SystemExit(
            "spec_common.seed_config_entry: resources/config.ini 里没找到唯一的 "
            "ocr_device 键，拒绝产出『加速档位不确定』的包")
    out_dir = os.path.join(specpath, "build", "_seed_cfg_" + device)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "config.ini")
    with open(out, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print("[spec] 产物内 config.ini 的 ocr_device 已覆写为 %s -> %s" % (device, out))
    return out


def collect_rapidocr(slim: bool = True) -> tuple[list, list, list, list]:
    """rapidocr 的 (datas, binaries, hiddenimports, excludes)。

    slim=True 时按 `EXCLUDE_ENGINES` 过滤三份清单并瘦身重复模型缓存，
    同时把过滤掉的引擎作为 `excludes` 返回（Analysis 用）。
    """
    datas, binaries, hiddenimports = collect_all("rapidocr")
    if not slim:
        return datas, binaries, hiddenimports, []

    d, b, h = [], [], []
    for item in datas:
        norm = item[0].replace("\\", "/")
        if any(seg in norm for seg in _EXCLUDE_ENGINE_DIRS):
            continue                    # 未用后端的数据文件
        if "/rapidocr/models/" in norm and _bn(item[0]) not in _KEEP_RAPIDOCR_MODELS:
            continue                    # v6 small det/rec 重复缓存
        d.append(item)
    for item in binaries:
        if any(item[0].startswith(p + ".") or item[0] == p for p in EXCLUDE_ENGINES):
            continue
        b.append(item)
    for item in hiddenimports:
        if any(item == p or item.startswith(p + ".") for p in EXCLUDE_ENGINES):
            continue
        h.append(item)
    return d, b, h, list(EXCLUDE_ENGINES)


def collect_zxingcpp() -> tuple[list, list, list]:
    """zxingcpp 的 (datas, binaries, hiddenimports)。"""
    return collect_all("zxingcpp")


def core_engine_submodules() -> list:
    """core_engine 已内置到 ClickOCR/core_engine，确保所有子模块被打包。"""
    return collect_submodules("core_engine")


def pillow_heif_dlls() -> list:
    """pillow_heif 的依赖 DLL（libheif / libde265 / libx265 / libstdc++ / libgcc_s /
    libwinpthread）。

    它们由 delvewheel 铺在 site-packages 根目录且为**延迟加载**，PyInstaller 的
    导入表分析易漏收 → 显式 glob 到 _internal 根目录，与 _pillow_heif.pyd 同目录
    （delvewheel 加载器按同目录查找）。
    """
    out = []
    try:
        sp = sysconfig.get_paths()["purelib"]
        for pat in ("libheif-*.dll", "libde265-*.dll", "libx265-*.dll",
                    "libstdc++-*.dll", "libgcc_s_*-*.dll", "libwinpthread-*.dll"):
            for dll in glob.glob(os.path.join(sp, pat)):
                out.append((dll, "."))
    except Exception:                   # pragma: no cover  收集失败不该阻断出包
        pass
    return out


def drop_ffmpeg(binaries: list) -> list:
    """剔除 cv2 注入的 opencv_videoio_ffmpeg*.dll（视频编解码，约 29MB）。

    ClickOCR 只有截图(mss) 与图像数组运算，无视频 I/O。Analysis 之前用（二元组）。
    """
    return [b for b in binaries if "opencv_videoio_ffmpeg" not in b[0].lower()]


def append_cudnn_split(binaries: list) -> None:
    """补收 cudnn 9 拆分 DLL（cudnn_adv64_9.dll 等 9 个，仅 GPU 版）。

    onnxruntime hook 只收主 dll（cudnn64_9.dll，位于 onnxruntime/capi/），拆分 DLL
    若不在同目录，打包版真机加载 cudnn64_9.dll 时找不到依赖 → CUDA provider 静默
    回退 CPU。故重定向到 onnxruntime/capi/ 与主 dll 同目录；主 dll 本身已由 hook
    收集，跳过。（binaries 为二元组 (src, dest)。）
    """
    for src, _dst in collect_dynamic_libs("nvidia.cudnn"):
        if _bn(src) == "cudnn64_9.dll":
            continue
        binaries.append((src, os.path.join("onnxruntime", "capi")))


def append_directml_fallback(binaries: list) -> None:
    """DirectML.dll 兜底（仅 DML 版）。

    DmlExecutionProvider 依赖 capi/DirectML.dll（17.6MB），若被 hook 漏收，
    产物只剩 CPU EP（表现为设置里 DirectML 检测不到设备）。仅在缺失时补入。
    """
    try:
        import onnxruntime as ort
        dll = os.path.join(os.path.dirname(ort.__file__), "capi", "DirectML.dll")
        if os.path.isfile(dll) and not any(_bn(b[0]) == "DirectML.dll" for b in binaries):
            # 二元组语义：(源文件, 目标目录)；与 onnxruntime.dll 同目录 ORT 才能找到
            binaries.append((dll, os.path.join("onnxruntime", "capi")))
    except Exception:                   # noqa: BLE001  兜底失败不该阻断出包
        pass


def prune_cuda_and_ffmpeg(analysis) -> None:
    """Analysis 之后剔除 CUDA / TensorRT 运行库与 ffmpeg（cpu / dml 版）。

    运行时 onnxruntime 无 CUDA 原生库，自动回退到所选 provider
    （cpu）；DML 版另有防呆作用（directml 与 gpu 包互斥，正常不会命中）。
    """
    analysis.binaries = [
        (name, path, typ) for (name, path, typ) in analysis.binaries
        if not any(pat in name.lower() for pat in _CUDA_BIN_PATTERNS)
        and "opencv_videoio_ffmpeg" not in name.lower()
    ]


def drop_nvidia_tree(analysis) -> None:
    """Analysis 之后剔除 nvidia/ 目录树（仅 GPU 版）。

    依赖分析会把 cudnn 拆分 DLL 按原始目录收到 nvidia/cudnn/bin/，与
    `append_cudnn_split` 重定向到 onnxruntime/capi/ 的同一批 DLL 重复（约 141MB）。
    运行时 cudnn64_9.dll 只从同目录加载拆分 DLL，nvidia/ 下的副本无人引用。
    """
    analysis.binaries = [
        (name, path, typ) for (name, path, typ) in analysis.binaries
        if not name.replace("\\", "/").startswith("nvidia/")
    ]


def build_exe(EXE, PYZ, analysis, name: str):
    """EXE 收尾（三个 spec 逐字相同，仅产物名不同）。

    upx=False：Qt / onnxruntime / DirectML 的 DLL 过 UPX 有损坏与杀软误报风险，
    体积换稳定。
    """
    pyz = PYZ(analysis.pure)
    return EXE(
        pyz,
        analysis.scripts,
        [],
        exclude_binaries=True,
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )


def build_collect(COLLECT, exe, analysis, name: str):
    """COLLECT 收尾（同上）。"""
    return COLLECT(
        exe,
        analysis.binaries,
        analysis.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=name,
    )
