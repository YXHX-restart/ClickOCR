"""core_engine.autostart — Windows 开机自启动（注册表 HKCU Run）。

- 打包（frozen）后注册 exe 本身；开发时注册 python + main.py。
- 无 Qt 依赖。
"""
from __future__ import annotations

import sys
from pathlib import Path

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
    except (OSError, ImportError):
        return False


def set_enabled(on: bool) -> bool:
    """写入/删除自启动项。返回是否成功。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except OSError:
                    pass
        return True
    except (OSError, ImportError):
        return False
