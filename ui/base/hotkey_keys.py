"""ui.base.hotkey_keys — 快捷键「键名 token ⇄ Qt 键值 / Win32 虚拟键码」统一映射。

历史痛点：快捷键设置分散在 settings_panel(取框/读写)、result_overlay(解析)、
hotkey(Win32 全局) 三处，各自维护一套字母键表，加一个键类型要改三处。
本模块集中定义 token → Qt.KeyValue / Win32 VK，以及 token 的显示名，
三处都从这里取，避免重复与漂移。

token 规范：
- 字母/数字用单字符（c / 5）；
- F 键用 f1..f12；
- 方向键用 left/right/up/down；
- 空格用 space；反引号用 `；
- 少量标点用原字符（- = [ ] ; ' , . / \\）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt

# ---- token → Qt.KeyValue（本地/贴图快捷键匹配用）----
_QT_BY_TOK = {
    "left": Qt.Key_Left, "right": Qt.Key_Right,
    "up": Qt.Key_Up, "down": Qt.Key_Down,
    "space": Qt.Key_Space,
    "`": Qt.Key_QuoteLeft, "~": Qt.Key_QuoteLeft,
    "-": Qt.Key_Minus, "=": Qt.Key_Equal,
    "[": Qt.Key_BracketLeft, "]": Qt.Key_BracketRight,
    ";": Qt.Key_Semicolon, "'": Qt.Key_Apostrophe,
    ",": Qt.Key_Comma, ".": Qt.Key_Period,
    "/": Qt.Key_Slash, "\\": Qt.Key_Backslash,
}
_QT_BY_TOK.update({f"f{i}": getattr(Qt, f"Key_F{i}") for i in range(1, 13)})
for _d in range(10):                       # 数字
    _QT_BY_TOK[str(_d)] = getattr(Qt, f"Key_{_d}")

# Qt → token（反向，取框显示用）
_QT_TO_TOK = {v: k for k, v in _QT_BY_TOK.items()}
_QT_TO_TOK.update({getattr(Qt, f"Key_{c}"): c.lower()
                   for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"})   # 字母
_QT_TO_TOK.update({getattr(Qt, f"Key_{d}"): str(d) for d in range(10)})


# ---- token → Win32 虚拟键码（全局热键注册用，US 布局）----
_VK_BY_TOK = {
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "space": 0x20, "`": 0xC0, "~": 0xC0,
    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE,
    "/": 0xBF, "\\": 0xDC,
}
_VK_BY_TOK.update({f"f{i}": 0x70 + i - 1 for i in range(1, 13)})  # F1=0x70
_VK_BY_TOK.update({str(d): 0x30 + d for d in range(10)})
_VK_BY_TOK.update({c: 0x41 + i for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")})


def qt_to_token(key) -> str | None:
    """把 QKeyEvent.key() / Qt.KeyValue 映射为 token；不支持的返回 None。"""
    return _QT_TO_TOK.get(key)


def token_to_qt(tok: str):
    """token → Qt.KeyValue。单字母/单数字也支持。"""
    t = tok.strip().lower()
    if len(t) == 1 and t.isalpha():
        return getattr(Qt, f"Key_{t.upper()}", None)
    return _QT_BY_TOK.get(t)


def token_to_vk(tok: str) -> int | None:
    """token → Win32 VK。单字母/单数字也支持。"""
    t = tok.strip().lower()
    if len(t) == 1 and t.isalpha():
        return 0x41 + ord(t) - ord("a")
    return _VK_BY_TOK.get(t)


# ---- 显示名（取框里展示）----
_DISPLAY = {
    "left": "←", "right": "→", "up": "↑", "down": "↓",
    "space": "空格", "backspace": "退格",
}


def token_display(tok: str) -> str:
    t = (tok or "").strip().lower()
    if not t:
        return ""
    if t in _DISPLAY:
        return _DISPLAY[t]
    if len(t) == 1 and t.isalpha():
        return t.upper()
    if t.startswith("f") and t[1:].isdigit():
        return t.upper()
    return t