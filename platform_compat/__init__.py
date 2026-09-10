"""platform_compat —— 桌面外壳的平台适配层（叶子模块，零第三方依赖）。

## 归属规则（重要，别再散回各处）

本包只管**桌面外壳**的平台差异：

  - 平台标志（`IS_WINDOWS` / `IS_LINUX` / `IS_MACOS`）
  - 能力探测（哪些功能在当前平台可用，见 `capabilities()`）
  - 字体族决策（见 `fonts.py`）

谁可以引用：`app.py`、`ui/`、`ocr/`、`screenshot/`、`hotkey.py`、`config.py`。

**`core_engine/` 不得引用本包** —— 它要保持自包含（可被 clickocr-server 等其它
产品单独 vendor），平台差异由它自己处理（`autostart.py` 用 try/except 优雅降级、
`ort_runtime.py` 自己有 `IS_WINDOWS`）。

引擎内部的**双实现**平台分支（`ocr/rapid_engine.py` 的 NUMA：Windows 走
`GetLogicalProcessorInformationEx`、Linux 读 `/sys/devices/system/node/*/cpulist`）
也留在原处 —— 那是双实现，不是能力开关，搬进来只会增加一层无谓跳转。

## 为什么要有这一层

原先 `sys.platform` 判断散落在 6 个文件里、字体族 `"Microsoft YaHei"` 硬编码 8 处，
跨平台移植时得逐个文件找。集中后能力矩阵在一处即可看全，字体决策只改一个文件。
"""
from __future__ import annotations

import sys

PLATFORM = (
    "windows" if sys.platform.startswith("win")
    else "macos" if sys.platform == "darwin"
    else "linux" if sys.platform.startswith("linux")
    else sys.platform
)

IS_WINDOWS = PLATFORM == "windows"
IS_LINUX = PLATFORM == "linux"
IS_MACOS = PLATFORM == "macos"

__all__ = [
    "PLATFORM", "IS_WINDOWS", "IS_LINUX", "IS_MACOS",
    "hotkey_supported", "autostart_supported", "scroll_capture_supported",
    "capabilities",
]


def hotkey_supported() -> bool:
    """全局热键是否有可用后端。

    目前只有 Windows 后端（`RegisterHotKey` + 隐藏宿主窗口的 `nativeEvent`）实现并
    验证过。Linux 需要 X11 的 `XGrabKey`（或 Wayland 的 GlobalShortcuts portal）、
    macOS 需要 Carbon/`CGEventTap`，均尚未实现 —— 在此之前热键能力一律返回 False，
    调用方（`hotkey.py`）据此提示用户改用托盘「截图」按钮。
    """
    return IS_WINDOWS


def autostart_supported() -> bool:
    """开机自启是否有实现（`core_engine.autostart` 写 HKCU Run，仅 Windows）。"""
    return IS_WINDOWS


def scroll_capture_supported() -> bool:
    """滚动截长图是否可用：需要 user32 的光标移动 / 滚轮模拟，仅 Windows。"""
    return IS_WINDOWS


def capabilities() -> dict:
    """当前平台的能力矩阵：一眼看清「哪些功能在这个平台上不可用」。

    供启动日志与诊断输出使用；新增平台相关功能时请在这里登记一项。
    """
    return {
        "platform": PLATFORM,
        "global_hotkey": hotkey_supported(),
        "autostart": autostart_supported(),
        "scroll_capture": scroll_capture_supported(),
        # 以下两项由跨平台库提供，无需分平台实现，登记在此便于对照
        "tray": True,        # QSystemTrayIcon（Qt 跨平台）
        "screenshot": True,  # mss（跨平台；Wayland 下受 portal 限制）
        "fonts": True,
    }
