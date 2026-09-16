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

from core_engine.logging_setup import get_logger
from platform_compat import IS_WINDOWS

log = get_logger("hotkey")

WIN = IS_WINDOWS   # 本文件即 Win32 后端实现；非 Windows 由 install() 优雅降级

if WIN:
    user32 = ctypes.windll.user32
    user32.RegisterHotKey.argtypes = [wintypes.HWND, wintypes.INT, wintypes.UINT, wintypes.UINT]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, wintypes.INT]
    user32.UnregisterHotKey.restype = wintypes.BOOL

    MOD_CTRL, MOD_ALT, MOD_SHIFT, MOD_WIN = 0x0002, 0x0001, 0x0004, 0x0008
    WM_HOTKEY = 0x0312

    # token → VK 映射、组合串拆分、未知 token 告警**全部收敛到
    # ui.base.hotkey_keys**（与本地快捷键取框/解析共用同一份表与同一套规则）：
    # 原先这里另抄了一份 token 清单（字母/数字/标点逐个列出）并自己
    # `replace("+", " ")`，于是加一个键要改两处、"+"/未知 token 在两边表现不一致。
    from ui.base.hotkey_keys import split_combo as _split_combo
    from ui.base.hotkey_keys import token_to_vk as _token_to_vk

    _MOD_VK = {"ctrl": MOD_CTRL, "alt": MOD_ALT,
               "shift": MOD_SHIFT, "win": MOD_WIN}

    # 自绘 MSG 结构（wintypes.MSG 在部分 Python 版本缺字段，且需 wParam 区分多热键）
    class _MSG(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                    ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                    ("time", wintypes.DWORD),
                    ("pt_x", ctypes.c_long), ("pt_y", ctypes.c_long)]

    def _parse_combo(combo: str):
        """组合串 → (mods, vk)。

        拆分与校验（"+" 的别名 plus、未知 token 告警）走
        hotkey_keys.split_combo——与本地快捷键解析同一入口，避免两套规则漂移。
        """
        mod_toks, key_tok = _split_combo(combo)
        mods = 0
        for t in mod_toks:
            mods |= _MOD_VK.get(t, 0)
        return mods, (_token_to_vk(key_tok) if key_tok else None)


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
        self.on_fallback = None     # callable(str_actual) —— 注册走了备用组合（换键了）

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
        if self.active_combo != combo and self.on_fallback:
            # 用户配置的组合被占用、静默换成了备用组合——GUI 用户看不到
            # print，必须回调通知实际生效的组合（否则以为还是自己设的键）
            self.on_fallback(label)
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
    def check_conflict(self, combo: str) -> bool | None:
        """探测组合是否已被其它程序占用。

        三态返回（原实现异常路径 return False，等于把「探测失败」当成「无冲突」，
        真冲突会被放过去）：
        - True  = 已被占用；
        - False = 确认无冲突（真的注册成功过）；
        - None  = 判定不了（组合非法/探测过程出错）。
        调用方（ui/windows/settings/hotkey_tab.py）对 None 按「未知」处理：
        继续交给 set_combo 做权威注册、失败自动回滚——所以 None 既不会误报
        「被占用」，也不会让真冲突静默通过。
        """
        if not WIN:
            return False
        mods, vk = _parse_combo(combo)
        if vk is None:
            return None                # 组合无法解析：判定不了
        try:
            self.createWinId()
            hwnd = int(self.winId())
            tmp_id = self._id + 1000   # 临时 id，避免与主热键混淆
            if user32.RegisterHotKey(hwnd, tmp_id, mods, vk):
                user32.UnregisterHotKey(hwnd, tmp_id)
                return False           # 注册成功 → 无冲突
            return True                # 注册失败 → 被占用
        except Exception:              # noqa: BLE001
            log.warning("热键冲突探测失败（无法判定，交由 set_combo 兜底）: %r",
                        combo, exc_info=True)
            return None

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
        rolled = False
        if om is not None and ov is not None:
            if user32.RegisterHotKey(hwnd, self._id, om, ov):
                self._ok = True
                self.active_combo = old_combo
                self.combo = old_combo
                rolled = True
                print(f"[热键] 新组合被占用，已回滚：{old_combo.upper()}")
        if not rolled:
            # 回滚也失败：主热键彻底不可用——清空状态并通知
            # （原先残留旧 active_combo 且无任何回调，UI 显示与实际脱节）
            self.active_combo = ""
            if self.on_failed:
                self.on_failed()
        return False

    # ---- M2：附加全局热键（历史记录 Ctrl+` 等）----
    def register_secondary(self, combo: str, callback, hotkey_id: int = 2) -> bool:
        """注册附加热键（独立 id + 回调）。返回是否成功。

        callback 为 None 且该 id 已有回调时保留原回调（供 App 预绑定后
        重复调用不覆盖）。
        """
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
            if callback is not None or hotkey_id not in self._secondary:
                self._secondary[hotkey_id] = callback
            self._secondary_combo[hotkey_id] = combo
            print(f"[热键] 已启用附加热键：{combo.upper()}")
            return True
        print(f"[热键] 附加热键注册失败（可能被占用）：{combo.upper()}")
        return False

    def set_secondary_callback(self, hotkey_id: int, callback) -> None:
        """预绑定附加热键回调（不做 OS 注册）。

        用于配置为 off 的附加热键：App 启动时先绑定回调，设置面板日后
        启用该热键时 set_secondary_combo 才能取到回调——否则注册成功但
        回调为 None，按键按下无响应。
        """
        self._secondary[hotkey_id] = callback

    def unregister_secondary(self, hotkey_id: int) -> bool:
        """仅解除 OS 注册（设置中关闭附加热键用）。

        回调与组合记录中**保留回调**：off → 再次启用的循环中
        set_secondary_combo 才能取回回调；未注册的热键本就收不到
        WM_HOTKEY，残留回调无害。
        """
        if WIN:
            try:
                user32.UnregisterHotKey(int(self.winId()), hotkey_id)
            except Exception:
                pass
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
        # 不能以主热键状态 self._ok 短路：_ok 只反映主截图热键的注册情况，
        # 用户在设置中关闭截图热键（unregister_primary）或主键注册失败时
        # _ok=False，但附加热键（历史/全屏/穿透）可能注册成功——原实现会把
        # 它们的 WM_HOTKEY 一起丢弃，三组热键随主键一起失效且无提示。
        # 改为按 hotkey id 查表分发：未注册的 id 本就不会收到 OS 消息。
        if not WIN:
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
