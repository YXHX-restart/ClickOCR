"""core_engine/ort_runtime — onnxruntime GPU 运行时检测与自动安装。

平台 × GPU 厂商 → Execution Provider：
  Windows:  cpu | cuda(NVIDIA) | dml(DirectML，通吃 AMD/Intel/NVIDIA)
  Linux:    cpu | cuda(NVIDIA) | migraphx(AMD) | openvino(Intel)
  注：Linux 无 DirectML（DirectX 专属），改走各厂商原生 EP；
      老 ROCm EP 自 ORT 1.23 起已移除，统一用 MIGraphX EP。

检测：get_available_providers() 看所选 device 对应 EP 是否可用。
安装：开发模式（venv 有 pip）可自动装；打包版无 pip，仅提示手动命令。
      AMD 的 onnxruntime-migraphx 不在 PyPI 主源，需 -f repo.radeon.com。

各厂商最低支持配置（仅供选型参考；OCR 模型小，2GB 显存即够）：
  厂商     EP          最低 GPU                驱动/运行时              示例
  NVIDIA   CUDA        Maxwell+(CC≥5.0)       驱动≥525 / CUDA 12.x   GTX 1050
  AMD      MIGraphX    Radeon 7000/9000       ROCm 6.4+ + MIGraphX   RX 7600
  Intel    OpenVINO    核显 Haswell+/Arc/NPU  OpenVINO 2025 + NEO    UHD 620
  通用(Win) DirectML  任意 DX12 GPU           WDDM2.4+ 驱动           几乎全部
"""
from __future__ import annotations

import subprocess
import sys

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

# 各包 pip 额外参数：AMD 的 migraphx 不在 PyPI 主源，需 -f AMD 仓库
PKG_INSTALL = {
    "onnxruntime-migraphx": ["-f", "https://repo.radeon.com/rocm/manylinux/rocm-rel-7.1.1/"],
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


def install_command(device: str) -> str:
    """所选 device 的完整手动安装命令（含必要 -f 源）；cpu 返回空串。"""
    if device not in DEVICE_PKG:
        return ""
    pkg, _ = DEVICE_PKG[device]
    extra = PKG_INSTALL.get(pkg, [])
    return " ".join(["pip", "install", pkg, *extra])


def detect_available_eps() -> set[str]:
    """当前环境 onnxruntime 可用 providers（CUDA/TensorRT/Dml/CPU…）。"""
    try:
        import onnxruntime as ort
        return set(ort.get_available_providers())
    except Exception:
        return set()


def device_ready(device: str) -> bool:
    """所选 device（cpu|cuda|dml）在当前环境是否已就绪。"""
    if device not in DEVICE_PKG:
        return True                      # cpu：天然就绪
    _, ep = DEVICE_PKG[device]
    return ep in detect_available_eps()


def runtime_status(device: str) -> str:
    """给设置面板显示的状态文案。"""
    if device not in DEVICE_PKG:
        return "无需安装（CPU 模式）"
    pkg, ep = DEVICE_PKG[device]
    if device_ready(device):
        return f"已就绪（{ep} 可用）"
    if getattr(sys, "frozen", False):
        # 打包版已捆绑对应运行时（通用版捆绑 DirectML，CUDA 版捆绑 onnxruntime-gpu）；
        # 不可用 = 设备/驱动不支持，自动回退 CPU，无需手动安装
        return f"{ep} 不可用（{_EP_HINT.get(ep, '设备不支持')}），已回退 CPU"
    return f"未安装 {pkg}（{PKG_SIZE[pkg]}）"


def install_runtime(device: str, timeout: int = 1800):
    """安装所选 device 的 onnxruntime 包。

    返回 (ok: bool, 说明: str)。开发模式用当前解释器 pip 安装；
    pip 会自动替换互斥包。打包版返回 False 提示手动安装。
    """
    if device not in DEVICE_PKG:
        return True, "CPU 模式无需安装"
    pkg, ep = DEVICE_PKG[device]
    if device_ready(device):
        return True, f"已就绪（{ep} 可用），无需安装"
    if getattr(sys, "frozen", False):
        return True, (f"{ep} 不可用（{_EP_HINT.get(ep, '设备不支持')}），"
                      "已回退 CPU；运行时已随包捆绑，无需手动安装")
    extra = PKG_INSTALL.get(pkg, [])
    cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir", pkg, *extra]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        if r.returncode == 0:
            return True, f"安装完成：{pkg}（请重启程序生效）"
        return False, (r.stderr or r.stdout or "").strip()[-400:]
    except subprocess.TimeoutExpired:
        return False, "安装超时，请检查网络后重试"
    except Exception as e:  # noqa: BLE001
        return False, str(e)
