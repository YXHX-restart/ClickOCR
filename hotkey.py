"""全局热键：基于 Windows API RegisterHotKey + 窗口级 nativeEvent。

关键修正（相较上一版）：
- 上一版用 RegisterHotKey(None, ...) 注册，再靠 QApplication 级的
  QAbstractNativeEventFilter 拦截 WM_HOTKEY。但 WM_HOTKEY 是「线程消息」
  (hwnd = NULL)，Qt 的应用级 nativeEventFilter 并不保证会把这类消息泵入，
  因此在普通用户会话里常出现「注册成功但永远触发不了」。
- 本版改为：用一个隐藏 QWidget 宿主窗口的「真实 HWND」注册热键，WM_HOTKEY
  会变成发往该窗口的窗口消息 (hwnd != NULL)，由该窗口的 nativeEvent 稳定捕获。
  这是 Windows 全局热键最可靠的 Qt 实现方式。

所有状态都会 print 到控制台（python main.py 从命令行启动可见），便于排查。
非 Windows 平台下不注册、不拦截，热键不可用（托盘"截图"按钮仍可用）。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from platform_compat import IS_WINDOWS

WIN = IS_WINDOWS   # 本文件即 Win32 后端实现；非 Windows 由 install() 优雅降级

if WIN:
    user32 = ctypes.windll.user32
    user32.RegisterHotKey.argtypes = [wintypes.HWND, wintypes.INT, wintypes.UINT, wintypes.UINT]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, wintypes.INT]
    user32.UnregisterHotKey.restype = wintypes.BOOL

    MOD_CTRL, MOD_ALT, MOD_SHIFT, MOD_WIN = 0x0002, 0x0001, 0x0004, 0x0008
    WM_HOTKEY = 0x0312

    # 字母 / 数字 / 反引号 / F 键 / 方向键 / 空格 / 常用标点。
    # token → VK 映射收敛到 ui.base.hotkey_keys（与本地快捷键取框/解析共用一份表）
    from ui.base.hotkey_keys import token_to_vk as _token_to_vk
    # 字母/数字逐字符作为独立 token（不能连成一个字符串，否则会被当成一个键）
    _VK = {p: _token_to_vk(p) for p in
           (*"abcdefghijklmnopqrstuvwxyz", *[str(d) for d in range(10)],
            *"f1 f2 f3 f4 f5 f6 f7 f8 f9 f10 f11 f12 "
             "left right up down space ` ~ - = [ ] ; ' , . / \\".split())}

    # 自绘 MSG 结构（wintypes.MSG 在部分 Python 版本缺字段，且需 wParam 区分多热键）
    class _MSG(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                    ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                    ("time", wintypes.DWORD),
                    ("pt_x", ctypes.c_long), ("pt_y", ctypes.c_long)]

    def _parse_combo(combo: str):
        mods = 0
        vk = None
        for p in [x.strip().lower() for x in combo.replace("+", " ").split()]:
            if p == "ctrl":
                mods |= MOD_CTRL
            elif p == "alt":
                mods |= MOD_ALT
            elif p == "shift":
                mods |= MOD_SHIFT
            elif p == "win":
                mods |= MOD_WIN
            elif p in _VK:
                vk = _VK[p]
        return mods, vk


# 主组合注册失败时的备用组合（按顺序尝试，优先选不易被占用的）
_FALLBACKS = ["ctrl+shift+r", "ctrl+alt+s", "ctrl+shift+s"]


class HotkeyHost(QWidget):
    """隐藏宿主窗口，承载全局热键的窗口消息。

    - 用真实 HWND 注册，WM_HOTKEY 由 nativeEvent 捕获（主线程、随 Qt 事件循环）。
    - install() 在 QApplication 创建后、exec() 前调用。
    """

    def __init__(self):
        super().__init__()
        # 隐藏但不参与焦点/任务栏，避免干扰用户
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_DontShowOnScreen, True)
        self._cb = None
        self._id = 1
        self._ok = False
        self.combo = ""
        self.active_combo = ""
        # M2：附加全局热键（不同 id + 独立回调），如历史记录 Ctrl+`
        self._secondary = {}          # hotkey_id -> callback
        self._secondary_combo = {}    # hotkey_id -> combo 字符串
        self.on_registered = None   # callable(str_label)
        self.on_failed = None       # callable()

    def set_callback(self, cb):
        self._cb = cb

    def install(self, app, combo: str = "ctrl+shift+r"):
        """注册热键。app 仅用于挂 aboutToQuit 清理。"""
        if not WIN:
            print("[热键] 非 Windows 平台，全局热键不可用（用托盘\"截图\"按钮）。")
            if self.on_failed:
                self.on_failed()
            return
        mods, vk = _parse_combo(combo)
        if vk is None:
            print(f"[热键] 组合键解析失败：{combo!r}")
            if self.on_failed:
                self.on_failed()
            return

        # 强制创建原生窗口，使其 HWND 有效并能接收 WM_HOTKEY
        self.createWinId()
        hwnd = int(self.winId())

        ok = False
        tried = []
        for cand in [combo] + _FALLBACKS:
            m, v = _parse_combo(cand)
            tried.append(cand)
            if user32.RegisterHotKey(hwnd, self._id, m, v):
                self.active_combo = cand
                ok = True
                combo = cand
                break
        if not ok:
            print("[热键] 所有组合均注册失败（可能被其它程序占用）："
                  + " / ".join(tried) + "。请用托盘\"截图\"按钮。")
            if self.on_failed:
                self.on_failed()
            return

        self._ok = True
        app.aboutToQuit.connect(self.unregister)
        label = self.active_combo.upper()
        print(f"[热键] 已启用全局热键：{label}（托盘\"截图\"按钮同样可用）")
        if self.on_registered:
            self.on_registered(label)

    def unregister(self):
        """解注册全部热键（主热键 + 附加热键）。"""
        if not WIN:
            return
        try:
            hwnd = int(self.winId())
        except Exception:
            return
        ids = [self._id] + list(self._secondary.keys())
        for hid in ids:
            try:
                user32.UnregisterHotKey(hwnd, hid)
            except Exception:
                pass
        self._ok = False
        self._secondary.clear()
        self._secondary_combo.clear()

    def unregister_primary(self):
        """仅解注册主热键（保留附加热键）；用于设置中关闭截图热键。"""
        if not WIN:
            return
        try:
            hwnd = int(self.winId())
        except Exception:
            return
        try:
            user32.UnregisterHotKey(hwnd, self._id)
        except Exception:
            pass
        self._ok = False
        self.active_combo = ""

    # ---- M2：设置面板动态改键（主热键）----
    def check_conflict(self, combo: str) -> bool:
        """探测组合是否已被其它程序占用。返回 True=冲突。"""
        if not WIN:
            return False
        mods, vk = _parse_combo(combo)
        if vk is None:
            return False
        try:
            self.createWinId()
            hwnd = int(self.winId())
            tmp_id = self._id + 1000   # 临时 id，避免与主热键混淆
            if user32.RegisterHotKey(hwnd, tmp_id, mods, vk):
                user32.UnregisterHotKey(hwnd, tmp_id)
                return False           # 注册成功 → 无冲突
            return True                # 注册失败 → 被占用
        except Exception:
            return False

    def set_combo(self, combo: str) -> bool:
        """运行时更换主热键：先解注册旧键，再注册新键；失败回滚旧键。"""
        if not WIN:
            return False
        mods, vk = _parse_combo(combo)
        if vk is None:
            return False
        old_combo = self.active_combo or self.combo
        try:
            hwnd = int(self.winId())
        except Exception:
            self.createWinId()
            hwnd = int(self.winId())
        # 1) 解注册旧键（保留附加键）
        try:
            user32.UnregisterHotKey(hwnd, self._id)
        except Exception:
            pass
        self._ok = False
        # 2) 注册新键（用户指定组合，不走 fallback）
        if user32.RegisterHotKey(hwnd, self._id, mods, vk):
            self._ok = True
            self.combo = combo
            self.active_combo = combo
            print(f"[热键] 已切换热键：{combo.upper()}")
            if self.on_registered:
                self.on_registered(combo.upper())
            return True
        # 3) 失败 → 回滚旧键
        om, ov = _parse_combo(old_combo)
        if om is not None and ov is not None:
            if user32.RegisterHotKey(hwnd, self._id, om, ov):
                self._ok = True
                self.active_combo = old_combo
                self.combo = old_combo
                print(f"[热键] 新组合被占用，已回滚：{old_combo.upper()}")
        return False

    # ---- M2：附加全局热键（历史记录 Ctrl+` 等）----
    def register_secondary(self, combo: str, callback, hotkey_id: int = 2) -> bool:
        """注册附加热键（独立 id + 回调）。返回是否成功。"""
        if not WIN:
            return False
        mods, vk = _parse_combo(combo)
        if vk is None:
            print(f"[热键] 附加组合解析失败：{combo!r}")
            return False
        try:
            hwnd = int(self.winId())
        except Exception:
            self.createWinId()
            hwnd = int(self.winId())
        if user32.RegisterHotKey(hwnd, hotkey_id, mods, vk):
            self._secondary[hotkey_id] = callback
            self._secondary_combo[hotkey_id] = combo
            print(f"[热键] 已启用附加热键：{combo.upper()}")
            return True
        print(f"[热键] 附加热键注册失败（可能被占用）：{combo.upper()}")
        return False

    def unregister_secondary(self, hotkey_id: int) -> bool:
        try:
            user32.UnregisterHotKey(int(self.winId()), hotkey_id)
        except Exception:
            pass
        self._secondary.pop(hotkey_id, None)
        self._secondary_combo.pop(hotkey_id, None)
        return True

    def set_secondary_combo(self, hotkey_id: int, combo: str) -> bool:
        """运行时更换附加热键；失败回滚旧键。返回是否成功。"""
        if not WIN:
            return False
        mods, vk = _parse_combo(combo)
        if vk is None:
            return False
        cb = self._secondary.get(hotkey_id)        # 先保留回调
        old = self._secondary_combo.get(hotkey_id)
        self.unregister_secondary(hotkey_id)
        try:
            hwnd = int(self.winId())
        except Exception:
            self.createWinId()
            hwnd = int(self.winId())
        if user32.RegisterHotKey(hwnd, hotkey_id, mods, vk):
            self._secondary[hotkey_id] = cb
            self._secondary_combo[hotkey_id] = combo
            print(f"[热键] 已切换附加热键：{combo.upper()}")
            return True
        # 回滚旧键
        if old is not None:
            om, ov = _parse_combo(old)
            if om is not None and ov is not None:
                user32.RegisterHotKey(hwnd, hotkey_id, om, ov)
                self._secondary[hotkey_id] = cb
                self._secondary_combo[hotkey_id] = old
                print(f"[热键] 附加热键新组合被占用，已回滚：{old.upper()}")
        return False

    @staticmethod
    def _msg_from_event(message):
        """把 nativeEvent 的 message 参数转成 MSG（含 wParam，用于区分多热键）。

        PySide6 里 message 是 shiboken6.Shiboken.VoidPtr，支持 int() 取地址；
        部分版本可能是裸 int 或 ctypes 指针，逐一兼容。
        注意：不能用 ctypes.cast(message, c_void_p) —— VoidPtr 不是 ctypes
        类型，会抛 TypeError 被 except 吞掉，导致 WM_HOTKEY 永远识别不到。
        """
        try:
            addr = int(message)
        except Exception:
            addr = ctypes.cast(message, ctypes.c_void_p).value
        return _MSG.from_address(addr)

    def nativeEvent(self, eventType, message):
        if not WIN or not self._ok:
            return False, 0
        if "windows" not in str(eventType):
            return False, 0
        try:
            msg = self._msg_from_event(message)
        except Exception:
            return False, 0
        if msg.message == WM_HOTKEY:
            hid = int(msg.wParam)
            if hid == self._id:
                cb = self._cb
            else:
                cb = self._secondary.get(hid)
            if cb is not None:
                try:
                    cb()
                except Exception as e:  # noqa: BLE001
                    print(f"[热键] 回调执行异常：{e}")
            return True, 0   # 已处理
        return False, 0
