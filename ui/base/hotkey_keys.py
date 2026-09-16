"""ui.base.hotkey_keys — 快捷键「键名 token ⇄ Qt 键值 / Win32 虚拟键码」统一映射。

历史痛点：快捷键设置分散在 settings_panel(取框/读写)、result_overlay(解析)、
hotkey(Win32 全局) 三处，各自维护一套字母键表，加一个键类型要改三处。
本模块集中定义 token → Qt.KeyValue / Win32 VK，以及 token 的显示名，
三处都从这里取，避免重复与漂移。

token 规范：
- 字母/数字用单字符（c / 5）；
- F 键用 f1..f12；
- 方向键用 left/right/up/down；
- 空格用 space；反引号用 `（"~" 是它的别名）；
- 加号用 plus（"+" 是组合分隔符，不能自身作 token，见 split_combo）；
- 少量标点用原字符（- = [ ] ; ' , . / \\）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt

from core_engine.logging_setup import get_logger

log = get_logger("hotkey")

# ---- token → Qt.KeyValue（本地/贴图快捷键匹配用）----
# 同一 Qt 键允许多个 token（别名）：如 "`" 与 "~" 都落在 Key_QuoteLeft 上。
_QT_BY_TOK = {
    "left": Qt.Key_Left, "right": Qt.Key_Right,
    "up": Qt.Key_Up, "down": Qt.Key_Down,
    "space": Qt.Key_Space,
    "`": Qt.Key_QuoteLeft, "~": Qt.Key_QuoteLeft,
    "-": Qt.Key_Minus, "=": Qt.Key_Equal, "plus": Qt.Key_Plus,
    "[": Qt.Key_BracketLeft, "]": Qt.Key_BracketRight,
    ";": Qt.Key_Semicolon, "'": Qt.Key_Apostrophe,
    ",": Qt.Key_Comma, ".": Qt.Key_Period,
    "/": Qt.Key_Slash, "\\": Qt.Key_Backslash,
}
_QT_BY_TOK.update({f"f{i}": getattr(Qt, f"Key_F{i}") for i in range(1, 13)})
for _d in range(10):                       # 数字
    _QT_BY_TOK[str(_d)] = getattr(Qt, f"Key_{_d}")

# ---- Qt.KeyValue → token（反向：取框显示 / 按键捕获用）----
# 不能用 `{v: k for k, v in _QT_BY_TOK.items()}`：一个键对应多个 token 时，
# 字典推导是「后者覆盖前者」——"~" 排在 "`" 之后，Key_QuoteLeft 反查就成了 "~"，
# 于是 token 往返不一致（用户在取框里按 ` 被记成 ~，与配置/默认值里的 ctrl+`
# 对不上，热键静默失效）。
# 收敛规则（显式，不靠推导）：
# 1) `_QT_ALIAS_TOKENS` 里的 token 只服务**正向**解析（用户输入别名），不参与反向；
# 2) 其余 token 按声明顺序「首个即主 token」，用 setdefault 生成，
#    重复键不再静默覆盖（即便日后漏登记别名，也保住已声明的主 token）。
_QT_ALIAS_TOKENS = frozenset({"~"})


def _build_qt_reverse() -> dict:
    rev: dict = {}
    for tok, qt in _QT_BY_TOK.items():
        if tok not in _QT_ALIAS_TOKENS:
            rev.setdefault(qt, tok)
    return rev


_QT_TO_TOK = _build_qt_reverse()
_QT_TO_TOK.update({getattr(Qt, f"Key_{c}"): c.lower()
                   for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"})   # 字母
_QT_TO_TOK.update({getattr(Qt, f"Key_{d}"): str(d) for d in range(10)})


# ---- token → Win32 虚拟键码（全局热键注册用，US 布局）----
_VK_BY_TOK = {
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "space": 0x20, "`": 0xC0, "~": 0xC0,
    "-": 0xBD, "=": 0xBB, "plus": 0xBB, "[": 0xDB, "]": 0xDD,
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
    "plus": "+",                # "+" 的 token 别名，取框里按原字符显示
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


# ---- 组合键串的拆分（Qt 本地快捷键与 Win32 全局热键共用的唯一入口）----

# 修饰键 token（与框架无关；Qt 侧映射为 Qt.*Modifier，Win32 侧映射为 MOD_*）
MODIFIER_TOKENS = ("ctrl", "alt", "shift", "win")

# 组合串里表示「关闭」的 token：显式关闭不是「未知 token」，不告警
_OFF_TOKENS = frozenset({"off", "none", "false"})


def split_combo(combo: str) -> tuple[list[str], str | None]:
    """拆分组合键串 → (修饰键 token 列表, 主键 token 或 None)。

    收敛点：用户输入串的拆分原先在 hotkey_keys.parse_qt_combo（本地快捷键）
    与 hotkey._parse_combo（Win32 全局热键）各写一份
    `combo.replace("+", " ").split()` + 一份修饰键 if/elif 链，于是：
    - "+" 本身无法表达（"+" 是分隔符，"ctrl++" 拆完只剩 ["ctrl"]）→
      统一用别名 token **plus** 表达 "+" 键；
    - 未知 token 被静默丢弃（配置里写错键名毫无线索，热键无声失效）。
    本函数是唯一的拆分/校验入口，两侧解析器都从这里取 token。

    dpr/平台无关，返回的修饰键 token 由调用方映射到自己的常量。
    """
    mods: list[str] = []
    key: str | None = None
    for p in str(combo or "").strip().lower().replace("+", " ").split():
        if not p or p in _OFF_TOKENS:
            continue
        if p in MODIFIER_TOKENS:
            if p not in mods:
                mods.append(p)
        elif token_to_qt(p) is not None or token_to_vk(p) is not None:
            key = p
        else:
            log.warning("快捷键组合 %r 含无法识别的键名 %r，已忽略"
                        "（加号键请写 plus）", combo, p)
    return mods, key


def parse_qt_combo(combo: str):
    """'shift+c' -> (Qt.ShiftModifier, Qt.Key_C)；'off'/空 -> None（关闭）。

    本地（贴图/选区）快捷键匹配用：把组合字符串解析成 Qt 修饰键掩码 + Qt 键值。
    token 由本模块统一映射（含拆分与未知 token 告警，见 split_combo），
    支持 字母/数字/F1-F12/方向键/空格/标点/plus。
    原先住在 result_overlay.py（ui.windows.settings 三处反向 import 它），2026-09-14
    搬到本模块——这里本就是「三处统一映射」的家。
    """
    if not combo or str(combo).strip().lower() in _OFF_TOKENS:
        return None
    mod_toks, key_tok = split_combo(combo)
    if key_tok is None:
        return None
    mods = Qt.NoModifier
    if "ctrl" in mod_toks:
        mods |= Qt.ControlModifier
    if "alt" in mod_toks:
        mods |= Qt.AltModifier
    if "shift" in mod_toks:
        mods |= Qt.ShiftModifier
    if "win" in mod_toks:
        mods |= Qt.MetaModifier
    key = token_to_qt(key_tok)
    return (mods, key) if key is not None else None