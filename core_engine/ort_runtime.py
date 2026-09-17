"""core_engine/ort_runtime — onnxruntime GPU 运行时检测。

平台 × GPU 厂商 → Execution Provider：
  Windows:  cpu | cuda(NVIDIA) | dml(DirectML，通吃 AMD/Intel/NVIDIA)
  Linux:    cpu | cuda(NVIDIA) | migraphx(AMD) | openvino(Intel)
  注：Linux 无 DirectML（DirectX 专属），改走各厂商原生 EP；
      老 ROCm EP 自 ORT 1.23 起已移除，统一用 MIGraphX EP。

检测：get_available_providers() 看所选 device 对应 EP 是否可用。
**不做自动安装**：打包版捆绑了对应运行时、开发版由开发者自行 pip 安装，
UI 只显示「就绪 / 未安装」状态（见 runtime_status）。

> 历史：本模块曾有 `install_runtime()` / `install_command()`（子进程跑 pip 安装）。
> 两者**全仓已无调用者**，且 `install_runtime` 会把 pip 的 stderr 原文返回给
> 调用方 —— 其中含本机用户名、venv 绝对路径、内网代理的 `user:pass@`（审查
> §3.8）。既然 UI 触发链早已消失、函数又带凭据外泄面，2026-09-17 直接删除
> （不是「留着但不可达」）：需要时从 git 历史取回。

各厂商最低支持配置（仅供选型参考；OCR 模型小，2GB 显存即够）：
  厂商     EP          最低 GPU                驱动/运行时              示例
  NVIDIA   CUDA        Maxwell+(CC≥5.0)       驱动≥525 / CUDA 12.x   GTX 1050
  AMD      MIGraphX    Radeon 7000/9000       ROCm 6.4+ + MIGraphX   RX 7600
  Intel    OpenVINO    核显 Haswell+/Arc/NPU  OpenVINO 2025 + NEO    UHD 620
  通用(Win) DirectML  任意 DX12 GPU           WDDM2.4+ 驱动           几乎全部
"""
from __future__ import annotations

import sys
import threading

IS_WINDOWS = sys.platform.startswith("win")

# 包体积（wheel 估算，用于安装前确认提示；带 + 为含厂商运行库的粗估）
PKG_SIZE = {
    "onnxruntime-directml": "约 25MB",
    "onnxruntime-gpu": "约 242MB",
    "onnxruntime-migraphx": "约 200MB+",
    "onnxruntime-openvino": "约 150MB+",
}

# device → (onnxruntime 包名, ExecutionProvider)
# Windows: cuda / dml；Linux: cuda / migraphx(AMD) / openvino(Intel)
DEVICE_PKG = {
    "cuda": ("onnxruntime-gpu", "CUDAExecutionProvider"),
    "dml": ("onnxruntime-directml", "DmlExecutionProvider"),
    "migraphx": ("onnxruntime-migraphx", "MIGraphXExecutionProvider"),
    "openvino": ("onnxruntime-openvino", "OpenVINOExecutionProvider"),
}

# 各 EP 设备前提提示（打包版已捆绑运行时，不可用 = 设备/驱动不支持）
_EP_HINT = {
    "CUDAExecutionProvider": "需 NVIDIA 显卡 + 驱动 + CUDA 环境",
    "DmlExecutionProvider": "需 DX12 显卡（AMD/Intel/NVIDIA 均可）",
    "MIGraphXExecutionProvider": "需 AMD Radeon 7000/9000 + ROCm",
    "OpenVINOExecutionProvider": "需 Intel 核显 / Arc / NPU",
}


def available_devices() -> list[str]:
    """当前平台可选的 device 列表（含 cpu）。

    Windows = cpu|cuda|dml；Linux = cpu|cuda|migraphx|openvino。
    不按已装 EP 过滤——保留未装项以便用户选中后触发检测/安装。
    """
    if IS_WINDOWS:
        return ["cpu", "cuda", "dml"]
    return ["cpu", "cuda", "migraphx", "openvino"]


_EPS_CACHE: set[str] | None = None
_EPS_CACHE_MOD = None


def detect_available_eps() -> set[str]:
    """当前环境 onnxruntime 可用 providers（CUDA/TensorRT/Dml/CPU…）。

    结果只与「进程里装了哪个 onnxruntime 包」有关，运行期不可能变化，故缓存
    一次（设置面板每次刷新都会问到它，原实现每次都重建一个 set）。
    缓存按 `sys.modules['onnxruntime']` 的**对象身份**失效：测试里会用假 ORT
    顶替真模块，若只按「首次结果」缓存，后面的断言会读到前一个假 ORT 的
    providers（顺序依赖、静默错判）。
    返回**副本**：调用方拿到 `set` 后可能就地增删（不污染缓存）。
    """
    global _EPS_CACHE, _EPS_CACHE_MOD
    mod = sys.modules.get("onnxruntime")
    if _EPS_CACHE is not None and mod is _EPS_CACHE_MOD:
        return set(_EPS_CACHE)
    try:
        import onnxruntime as ort
        _EPS_CACHE = set(ort.get_available_providers())
    except Exception:
        _EPS_CACHE = set()
    _EPS_CACHE_MOD = sys.modules.get("onnxruntime")
    return set(_EPS_CACHE)


def device_ready(device: str) -> bool:
    """所选 device（cpu|cuda|dml）的 EP 在**当前环境是否已注册**。

    注意语义：这只是「EP 在列表里」，不等于「推理时真的落位到该 EP」——
    列表里有 CUDA/DML 但驱动异常、显存不足时，建 session 会失败或回落 CPU。
    要判断「实际用上了没有」，看 `record_actual`/`actual_ok`（由引擎构建
    完成后回报，见 runtime_status）。
    """
    if device not in DEVICE_PKG:
        return True                      # cpu：天然就绪
    _, ep = DEVICE_PKG[device]
    return ep in detect_available_eps()


# ---- 实际落位回报（引擎构建 → 设置面板显示）----
# 只回答「EP 在不在列表里」会让 UI 显示「已就绪」而后台实际已回退 CPU
# （审查 H5）。引擎构建时是唯一知道真像的地方，故由它回报，这里只存结果。
_ACTUAL: dict[str, bool] = {}
_ACTUAL_LOCK = threading.Lock()


def record_actual(device: str, ok: bool) -> None:
    """引擎构建完成后回报该 device 的**实际落位**结果（供 UI 显示真状态）。"""
    with _ACTUAL_LOCK:
        _ACTUAL[device] = bool(ok)


def actual_ok(device: str) -> bool | None:
    """该 device 的实际落位结果：True/False；None = 本次进程还没跑过引擎。"""
    with _ACTUAL_LOCK:
        return _ACTUAL.get(device)


def runtime_status(device: str) -> str:
    """给设置面板显示的状态文案。"""
    if device not in DEVICE_PKG:
        return "无需安装（CPU 模式）"
    pkg, ep = DEVICE_PKG[device]
    actual = actual_ok(device)
    if actual is True:
        return f"已就绪（{ep} 实际生效）"
    if actual is False:
        return f"{ep} 可用但落位失败（已回退 CPU）"
    if device_ready(device):
        return f"已就绪（{ep} 可用）"
    if getattr(sys, "frozen", False):
        # 打包版已捆绑对应运行时（通用版捆绑 DirectML，CUDA 版捆绑 onnxruntime-gpu）；
        # 不可用 = 设备/驱动不支持，自动回退 CPU，无需手动安装
        return f"{ep} 不可用（{_EP_HINT.get(ep, '设备不支持')}），已回退 CPU"
    return f"未安装 {pkg}（{PKG_SIZE[pkg]}）"
