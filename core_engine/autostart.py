"""core_engine.autostart — Windows 开机自启动（注册表 HKCU Run）。

- 打包（frozen）后注册 exe 本身；开发时注册 python + main.py。
- 无 Qt 依赖。

失败策略：返回 False **且记日志**。原先静默返回 False，设置页开关会「看起来
切换成功了」（勾上、重启后没生效），排查时日志里什么都没有。
"""
from __future__ import annotations

import sys
from pathlib import Path

from core_engine.logging_setup import get_logger

log = get_logger("autostart")

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "ClickOCR"


def _command() -> str:
    """注册表里保存的启动命令。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    main = Path(sys.argv[0]).resolve()
    return f'"{sys.executable}" "{main}"'


def is_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except FileNotFoundError:
        return False            # 键/值不存在 = 未注册，属正常状态，不记日志
    except (OSError, ImportError) as e:
        log.warning("读取自启动项失败（按未开启处理）：%s", e)
        return False


def set_enabled(on: bool) -> bool:
    """写入/删除自启动项。返回是否成功（失败已记日志）。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass        # 本来就没注册 —— 目标状态已达成
        return True
    except (OSError, ImportError) as e:
        log.warning("写入自启动项失败（on=%s）：%s", on, e)
        return False
