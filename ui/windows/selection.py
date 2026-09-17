"""M1-2 区域框选遮罩（自研 Qt 橡皮筋）+ 捕捉程序框。

交互（类似 Snipaste/QQ 截图）：
- 悬停：鼠标移过窗口时高亮该窗口边框，并提示「单击捕捉窗口」；
- 单击窗口：直接按该窗口的外框范围截图；
- 按住左键拖拽：自由框选区域（原有行为）；
- 空白处单击 / Esc：取消。

外观（边框颜色/线宽/透明度）从 config.ini [selection] 读取，不写死。
坐标说明：
- 本控件覆盖主屏（逻辑像素），鼠标事件 e.pos() 即逻辑屏幕坐标；
- 窗口框来自 Win32 API（物理像素），按 dpr 换算为逻辑坐标；
- 业务层拿到逻辑 QRect 后需 ×dpr 还原为 mss 原始像素，再交给 ScreenshotService.grab_region。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from PySide6.QtCore import Qt, QRect, Signal, QPoint
from PySide6.QtGui import (QPainter, QColor, QPen, QBrush, QImage,
                           QGuiApplication)
from PySide6.QtWidgets import QApplication, QWidget

from config import ConfigManager
from platform_compat import IS_WINDOWS

WIN = IS_WINDOWS   # 前台窗口探测走 user32 + dwmapi，仅 Windows 有实现


def _positive_ratio(num, den=1.0) -> float:
    """取一个恒为**有效正数**的宽高比（num / den），非法时回退 1.0。

    Alt 等比锁定的比值有「计算」（按下瞬间 w/h）与「使用」（h*ratio、w/ratio）
    两处，任一处为 0/非正都会在 mouseMoveEvent 里除零 —— 纯垂直拖动 w=0 →
    比值 0.0 → `w / 0.0` 直接 ZeroDivisionError 崩溃。
    收敛为一个函数，两处都经它取值（而不是只修计算侧），比值恒 > 0：
    修一处漏一处的复发路径被彻底掐断。
    """
    try:
        r = float(num) / float(den)
    except (TypeError, ValueError, ZeroDivisionError):
        return 1.0
    return r if r > 0 else 1.0

if WIN:
    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = \
        [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    dwmapi.DwmGetWindowAttribute.argtypes = \
        [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long  # HRESULT

    GW_HWNDPREV = 2      # GetWindow: 下一个（Z 序下方）
    GA_ROOT = 2          # GetAncestor: 根窗口
    DWMWA_EXTENDED_FRAME_BOUNDS = 9   # 可见外框（不含不可见缩放边）
    DWMWA_CLOAKED = 14                # UWP 是否被隐藏
    _MY_PID = os.getpid()
    _SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd"}   # 桌面/任务栏背景

    def _window_title(hwnd) -> str:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        return buf.value.strip()

else:  # pragma: no cover - 非 Windows 分支
    user32 = dwmapi = None


class SelectionOverlay(QWidget):
    # 两个坐标信号都发**虚拟桌面物理像素**（mss 的坐标系），不是控件局部逻辑坐标。
    # 换算只在 SelectionOverlay._to_raw() 一处做——它同时掌握「本屏 dpr」与
    # 「本屏物理原点」两个只有它才知道的信息；调用方（capture_mixin）不再乘 dpr。
    #   用法：raw_bbox = (r.left(), r.top(), r.left() + r.width(), r.top() + r.height())
    region_selected = Signal(QRect)     # 框选完成（raw 物理像素）
    cancelled = Signal()                # 取消截图（Esc / 空白单击）
    scroll_requested = Signal(QRect)    # 滚动截图模式：框选完成 → 滚动区域（raw 物理像素）

    def __init__(self, config: ConfigManager | None = None):
        super().__init__()
        cfg = config or ConfigManager()
        self._border_color = QColor(*cfg.getcolor("ui", "border_color", (100, 190, 255)))
        self._border_width = cfg.getint("ui", "border_width", 2)
        self._fill_alpha = cfg.getint("ui", "fill_alpha", 50)
        self._mask_alpha = cfg.getint("ui", "mask_alpha", 120)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)   # 悬停检测窗口框
        # 遮罩覆盖**整个虚拟桌面**（所有屏几何的并集）：跨屏框选时选区框必须能
        # 画到别的屏上去。三种历史写法都不行——
        #   固定主屏  → 副屏根本选不了；
        #   单块屏    → 能选任意一屏，但仍跨不了屏（选区框被窗口裁掉）。
        # 覆盖并集后，鼠标按下会隐式抓取，拖动越出本窗口也能继续收到事件，
        # 选区框在每块屏上都可见。
        from PySide6.QtGui import QCursor, QGuiApplication
        self._screens = [s for s in QGuiApplication.screens() if s is not None]
        # 用第一块屏初始化，**不能**用 QRect()：QRect() 是 (0,0,-1,-1)，
        # 它不是 isNull()（宽高非 0）而是 isEmpty()，于是 united() 会保留它的
        # 原点 (0,0) —— 一旦没有任何屏位于 x=0/y=0（例如副屏挂在左侧或上方），
        # 并集原点就会被错误地拉成 (0,0)，整条坐标换算随之偏移。
        _geo = (QRect(self._screens[0].geometry()) if self._screens else QRect())
        for _s in self._screens[1:]:
            _geo = _geo.united(_s.geometry())
        self._geo = _geo
        # 光标所在屏（仅用于「整屏窗口命中」判定与 dpr 兜底；坐标换算按点逐屏算）
        self._cursor_screen = (QGuiApplication.screenAt(QCursor.pos())
                               or QGuiApplication.primaryScreen()
                               or (self._screens[0] if self._screens else None))
        self._dpr = (float(self._cursor_screen.devicePixelRatio())
                     if self._cursor_screen is not None else 1.0)
        self._mixed_dpr_warned = False
        self._mon_origin = None           # 兼容旧引用，见 _monitor_origin
        self.setGeometry(_geo)
        self._start = None
        self._end = None
        self._dragging = False
        self._press_win = None        # 按下时命中的窗口 (hwnd, QRect)
        self._hover_win = None        # 悬停命中的窗口 QRect
        self._hover_title = ""
        self._adjusted = None         # 滚轮微调后的选区 QRect（锁定用，单击确认）
        self._fullscreen = False      # A 键：全屏框选模式（再按 A 恢复原选区）
        self._prev_sel = None         # 进入全屏前的选区状态 (start, end, adjusted)

        # ---- 取色器（类似微信/QQ：光标带放大镜，按 C 复制色值）----
        self._cursor_pt = None        # 当前光标逻辑坐标
        self._pixel_pt = None         # 当前取色像素坐标（物理）
        self._screen_img = None       # 兜底屏幕底图（主屏；逐屏底图见 _screen_imgs）
        self._picker_size = 7         # 采样边长（奇数，3~15）
        self._picker_zoom = 9         # 每像素放大倍数
        self._pick_hotkey = "c"       # 复制色值快捷键（config 可配）
        self._fmt_hotkey = "h"        # 切换色值格式快捷键（config 可配）
        self._color_fmt = "rgb"       # 色值格式：rgb | hsv | hsl（按 H 循环）
        self._ratio_lock = None       # Alt 按住：锁定宽高比（按下的比例）
        self._hover_probe = None      # 悬停窗口探测节流：上次 (x, y, 单调时刻)
        # 上一帧取色器**实际画过**的矩形（由 _draw_picker 累积）。局部重绘要靠它
        # 擦掉旧位置——记录「画了什么」而不是在别处复刻一遍布局算式，后者必然漂移。
        self._picker_painted = None
        self._scroll_mode = False     # R 键：滚动截图模式（截长图）
        self._closing = False         # 已取消/已定稿：防 Esc 双通路重复发信号
        self._scroll_hotkey = "r"     # 滚动截图模式快捷键（config 可配）
        # 快捷键/取色器参数：复用 __init__ 顶部已构造的 cfg（原先在这里又
        # `config or ConfigManager()` 造了第二个实例）。这些读操作各自带默认值，
        # **不再用 try/except 包住** —— 原先整段被包住并在 except 里把
        # `_screen_img` 置 None，于是「配置读错」会被伪装成「抓屏失败」，
        # 表现为快捷键失灵而日志一片空白。
        self._pick_hotkey = (cfg.get("hotkey_select", "color_pick", "c") or "c").lower()
        self._fmt_hotkey = (cfg.get("hotkey_select", "color_fmt", "h") or "h").lower()
        # 全屏框选快捷键（[hotkey_select] full_sel，默认 A，可改键/可关闭）
        self._full_sel_hotkey = (cfg.get("hotkey_select", "full_sel", "a")
                                 or "a").lower()
        # 滚动截图模式快捷键（[hotkey_select] scroll_sel，默认 R）
        self._scroll_hotkey = (cfg.get("hotkey_select", "scroll_sel", "r")
                               or "r").lower()
        self._picker_size = cfg.getint("ui", "picker_size", 7)
        if self._picker_size % 2 == 0:
            self._picker_size = min(15, self._picker_size + 1)   # 奇数（中心格）
        self._picker_zoom = cfg.getint("ui", "picker_zoom", 9)
        # 遮罩显示前抓屏：取色器读取真实屏幕像素（不被遮罩压暗）。
        # **逐屏抓取**：底图是物理像素，用主屏底图 + 主屏 dpr 去读副屏上的光标
        # 既会越界（取色返回 None）也会读错颜色 —— 多屏下取色器等于失效。
        # 单屏抓取失败只降级该屏的取色，不阻断框选主流程（见 _grab_screens）。
        self._screen_imgs = {}        # QScreen → QImage（该屏物理像素底图）
        self._grab_screens()
        self._screen_img = self._screen_imgs.get(
            QGuiApplication.primaryScreen())   # 兜底底图（找不到光标屏时用）

        # Esc 兜底：Tool 窗口默认不抢键盘焦点（热键唤出后没点过鼠标时
        # keyPressEvent 收不到按键），用 ApplicationShortcut 在应用激活
        # 期间捕获 Esc 取消截图。注意：必须走 _on_esc（先 cancelled.emit
        # 再 close）——直接连 close 会绕过信号，App 侧就无法恢复截图前
        # 被隐藏的浮窗（keyPressEvent 的 Esc 分支因此成为死代码）。
        from PySide6.QtGui import QShortcut, QKeySequence
        self._esc_sc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._esc_sc.setContext(Qt.ApplicationShortcut)
        self._esc_sc.activated.connect(self._on_esc)
        # 滚动截图模式键（默认 R）：单字母时同样挂 ApplicationShortcut
        # 兜底（仅本应用激活期间生效，**不使用全局热键**，不抢其它程序按键）
        hk = (self._scroll_hotkey or "").lower()
        self._scroll_sc = None
        if len(hk) == 1 and hk.isalpha():
            self._scroll_sc = QShortcut(QKeySequence(hk), self)
            self._scroll_sc.setContext(Qt.ApplicationShortcut)
            self._scroll_sc.activated.connect(self._toggle_scroll_mode)

    def showEvent(self, _e):
        # 抢键盘焦点：确保 keyPressEvent（如 Esc）能收到
        self.activateWindow()
        self.setFocus(Qt.OtherFocusReason)
        self.raise_()

    # ---- 窗口捕捉 ----
    # ---- 坐标换算（局部逻辑 ↔ 虚拟桌面物理）----
    # 全局逻辑坐标 = 遮罩自身逻辑原点（＝虚拟桌面并集原点）+ 控件局部逻辑坐标。
    # 物理坐标 = 该点所在屏的物理原点 + (全局逻辑 - 该屏逻辑原点) × 该屏 dpr。
    #
    # 为什么「该屏物理原点 = 该屏逻辑原点 × dpr」是精确的：
    # 所有屏 dpr 相同时，Qt 的逻辑虚拟桌面就是物理布局整体缩放 1/dpr，
    # 因而对每块屏都成立。**混合 dpr 时该式只是近似** —— 精确做法需要按显示器
    # 取真实物理原点（Win32 EnumDisplayMonitors 并做屏↔显示器配对），
    # 混合 dpr 下那种配对是启发式的、容易配错，故本项目暂不支持并会告警一次。

    @staticmethod
    def _screen_phys_origin(screen) -> tuple:
        """某块屏的物理原点（同 dpr 下精确，混合 dpr 下近似）。"""
        g = screen.geometry()
        d = float(screen.devicePixelRatio()) or 1.0
        return (int(round(g.x() * d)), int(round(g.y() * d)))

    def _screen_at(self, global_logical):
        """点所在屏；落在屏间缝隙里时取**最近的屏**（不用主屏兜底）。

        走 `screen_for_point`：`screenAt()` 对「不属于任何屏的逻辑缝隙」返回 None，
        而跨屏框选时鼠标终点正好会落进去 —— 若退回主屏，换算出的物理坐标就会
        偏到主屏上去（本机实测布局见 widgets.screen_for_point 的说明）。
        """
        try:
            from ui.base.widgets import screen_for_point
        except Exception:  # noqa: BLE001
            screen_for_point = None
        if screen_for_point is not None:
            try:
                return screen_for_point(global_logical.toPoint()) or self._cursor_screen
            except Exception:  # noqa: BLE001
                pass
        from PySide6.QtGui import QGuiApplication
        try:
            return QGuiApplication.screenAt(global_logical.toPoint()) \
                or self._cursor_screen
        except Exception:  # noqa: BLE001
            return self._cursor_screen

    def _to_phys(self, global_logical) -> tuple:
        """全局逻辑坐标 → 虚拟桌面物理坐标（按点所在屏换算）。"""
        s = self._screen_at(global_logical)
        if s is None:
            return (global_logical.x() * self._dpr, global_logical.y() * self._dpr)
        d = float(s.devicePixelRatio()) or 1.0
        ox, oy = self._screen_phys_origin(s)
        g = s.geometry()
        if not self._mixed_dpr_warned and abs(d - self._dpr) > 1e-6:
            # 已进入「混合 dpr」区间：本项目的映射只是近似，明确告警而不是静默错
            self._mixed_dpr_warned = True
            from core_engine.logging_setup import get_logger
            get_logger("selection").warning(
                "检测到多屏 dpr 不一致（%s vs %s）：跨屏框选的坐标换算为近似值，"
                "可能偏移；请把显示器缩放设为相同比例", self._dpr, d)
        return (ox + (global_logical.x() - g.x()) * d,
                oy + (global_logical.y() - g.y()) * d)

    def _phys_to_local(self, px: float, py: float) -> tuple:
        """虚拟桌面物理坐标 → 控件局部逻辑坐标（_to_phys 的逆）。"""
        from PySide6.QtCore import QPointF as _QPF
        # 先按光标屏猜一次得到全局逻辑，再用该全局逻辑所在屏精确复核一次
        gx, gy = None, None
        s = self._cursor_screen
        for _ in range(2):
            if s is None:
                break
            d = float(s.devicePixelRatio()) or 1.0
            ox, oy = self._screen_phys_origin(s)
            _g = s.geometry()
            gx = _g.x() + (px - ox) / d
            gy = _g.y() + (py - oy) / d
            s = self._screen_at(_QPF(gx, gy))
        if gx is None:
            gx, gy = px / self._dpr, py / self._dpr
        return (gx - self._geo.x(), gy - self._geo.y())

    def _monitor_origin(self) -> tuple:
        """兼容入口：光标屏的物理原点（旧代码引用）。"""
        if self._cursor_screen is None:
            return (0, 0)
        return self._screen_phys_origin(self._cursor_screen)

    def _to_raw(self, rect: QRect) -> QRect:
        """控件局部逻辑坐标 → 虚拟桌面物理像素（mss 坐标系）。

        **这是唯一的坐标换算点。** 遮罩覆盖整个虚拟桌面，故：
        局部 → 全局逻辑（＋遮罩逻辑原点）→ 物理（按点所在屏逐点换算）。
        两个角分别换算，因此跨屏（甚至跨不同 dpr 的屏）时结果是绝对物理矩形。
        width/height 语义 = 右/下边界的**排他**值，故
        raw_bbox = (r.left(), r.top(), r.left()+r.width(), r.top()+r.height())。
        """
        from PySide6.QtCore import QPointF as _QPF
        gx0 = self._geo.x() + rect.x()
        gy0 = self._geo.y() + rect.y()
        gx1 = self._geo.x() + rect.x() + rect.width()
        gy1 = self._geo.y() + rect.y() + rect.height()
        x0, y0 = self._to_phys(_QPF(gx0, gy0))
        x1, y1 = self._to_phys(_QPF(gx1, gy1))
        return QRect(int(round(x0)), int(round(y0)),
                     int(round(x1 - x0)), int(round(y1 - y0)))

    def _window_at(self, logical_pt):
        """返回鼠标逻辑坐标下、遮罩下方的第一个可见窗口 (hwnd, 逻辑QRect)；无则 None。

        从本遮罩的 HWND 沿 Z 序向下（GW_HWNDPREV）遍历，跳过不可见/自身进程/
        桌面背景窗口；优先用 DWM 扩展外框（真实可见区域），失败回退 GetWindowRect。

        全屏窗口（覆盖整个屏幕）只在鼠标位于屏幕**边缘 5%** 区域时才算命中：
        否则把全屏当窗口会挡住自由框选（桌面无其他窗口时全屏会被误识别为窗口）。
        """
        if not WIN:
            return None
        from PySide6.QtCore import QPointF as _QPF
        # 控件局部逻辑 → 全局逻辑 → 物理（与 _to_raw 同一套换算）
        _gl = _QPF(self._geo.x() + logical_pt.x(), self._geo.y() + logical_pt.y())
        _px, _py = self._to_phys(_gl)
        x = int(round(_px))
        y = int(round(_py))
        # 「整屏窗口」判定用**点所在屏**的逻辑几何换算到控件局部：
        # 不能再用遮罩自身几何——遮罩现在覆盖整个虚拟桌面，比任何单屏都大
        _s = self._screen_at(_gl)
        _sg = _s.geometry() if _s is not None else self._geo
        scr = QRect(_sg.x() - self._geo.x(), _sg.y() - self._geo.y(),
                    _sg.width(), _sg.height())
        hwnd = int(self.winId())
        while True:
            hwnd = user32.GetWindow(hwnd, GW_HWNDPREV)
            if not hwnd:
                break
            if not user32.IsWindowVisible(hwnd):
                continue
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == _MY_PID:
                continue
            rect = RECT()
            ok = dwmapi.DwmGetWindowAttribute(
                hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                ctypes.byref(rect), ctypes.sizeof(rect))
            if ok != 0:
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
            if not (rect.left <= x <= rect.right and rect.top <= y <= rect.bottom):
                continue
            root = user32.GetAncestor(hwnd, GA_ROOT)
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(root, cls, 64)
            if cls.value in _SKIP_CLASSES:
                continue
            cloaked = wintypes.DWORD(0)
            if dwmapi.DwmGetWindowAttribute(
                    root, DWMWA_CLOAKED,
                    ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
                continue
            # 物理 → 控件局部逻辑（_to_phys 的逆，按点所在屏逐点换算）
            lx, ly = self._phys_to_local(rect.left, rect.top)
            rx, ry = self._phys_to_local(rect.right, rect.bottom)
            qr = QRect(int(round(lx)), int(round(ly)),
                       int(round(rx - lx)), int(round(ry - ly)))
            # 全屏窗口：仅边缘 5% 区域命中（中间留给自由框选）
            full_screen = (qr.width() >= scr.width() - 4
                           and qr.height() >= scr.height() - 4
                           and qr.left() <= 2 and qr.top() <= 2)
            if full_screen:
                edge = 0.05
                ex = scr.width() * edge
                ey = scr.height() * edge
                if not (logical_pt.x() < ex or logical_pt.y() < ey
                        or logical_pt.x() > scr.width() - ex
                        or logical_pt.y() > scr.height() - ey):
                    continue      # 在屏幕中部：全屏不算窗口
            return root, qr
        return None

    # ---- 绘制 ----
    def paintEvent(self, _event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, self._mask_alpha))
        if self._adjusted is not None:
            # 滚轮微调后的选区（中心锚点等距扩/缩）
            r = self._adjusted
            p.setPen(QPen(self._border_color, self._border_width))
            c = self._border_color
            p.setBrush(QBrush(QColor(c.red(), c.green(), c.blue(), self._fill_alpha)))
            p.drawRect(r)
            self._draw_size_label(p, r)
        elif self._start is not None and self._end is not None:
            # 橡皮筋（自由框选）
            r = QRect(self._start, self._end).normalized()
            p.setPen(QPen(self._border_color, self._border_width))
            c = self._border_color
            p.setBrush(QBrush(QColor(c.red(), c.green(), c.blue(), self._fill_alpha)))
            p.drawRect(r)
            self._draw_size_label(p, r)   # 框选时显示尺寸 + 像素信息
        elif self._hover_win is not None:
            # 悬停高亮：窗口外框 + 单击捕捉提示
            r = self._hover_win
            p.setPen(QPen(self._border_color, 3))
            p.setBrush(Qt.NoBrush)
            p.drawRect(r)
            self._draw_hover_label(p, r)
        if self._scroll_mode:
            self._draw_scroll_badge(p)
        self._draw_picker(p)   # 光标取色器（始终显示）

    # ---- 取色器 ----
    def _grab_screens(self):
        """逐屏抓取底图（构造期调用，必须在遮罩显示前）。

        单屏抓取失败只让**那一块屏**的取色器降级，不阻断框选主流程；但必须
        留痕 —— 否则「取色器取不到颜色」无从排查（原先只抓主屏，副屏取色
        静默失效属于同一类问题）。
        """
        from core_engine.logging_setup import get_logger
        log = get_logger("selection")
        for scr in QGuiApplication.screens():
            try:
                img = scr.grabWindow(0).toImage()
                if not img.isNull():
                    self._screen_imgs[scr] = img
            except Exception as e:        # noqa: BLE001 抓屏失败不影响框选
                log.warning("抓取屏幕底图失败（该屏取色器降级）：%s", e)
        if not self._screen_imgs:
            log.warning("所有屏幕底图抓取失败：取色器与放大镜本次不可用")

    def _pick_src(self, logical_pt) -> tuple:
        """光标处像素的取色源：返回 `(该屏底图, 底图内物理像素 x, y)`。

        底图索引 = 全局逻辑坐标经 `_to_phys` 换算后减去**该屏物理原点**
        （与框选换算共用同一套函数，避免出现第二份 dpr 实现而漂移）。
        取不到光标屏时退回主屏底图（多屏 DPI 混合时只是近似，好过整块失效）。
        """
        if logical_pt is None or not self._screen_imgs:
            return (None, 0, 0)
        g = QPoint(self._geo.x() + int(logical_pt.x()),
                   self._geo.y() + int(logical_pt.y()))
        s = self._screen_at(g)
        img = self._screen_imgs.get(s) if s is not None else None
        if img is None:
            s = QGuiApplication.primaryScreen()
            img = self._screen_imgs.get(s) or self._screen_img
        if img is None:
            return (None, 0, 0)
        px, py = self._to_phys(g)
        if s is not None:
            ox, oy = self._screen_phys_origin(s)
        else:
            ox = oy = 0
        return (img, int(round(px - ox)), int(round(py - oy)))

    def _pick_color(self, logical_pt) -> QColor | None:
        """取光标处屏幕像素颜色（按该点所在屏换算，混合 dpr 下也正确）。

        `_pixel_pt` 记录的是**光标所在屏**底图内的物理像素坐标（多屏下比
        原先「主屏物理坐标」更贴近用户看到的「这是屏幕上哪一点」）。
        """
        img, x, y = self._pick_src(logical_pt)
        if img is None:
            return None
        if not (0 <= x < img.width() and 0 <= y < img.height()):
            return None
        self._pixel_pt = (x, y)
        return img.pixelColor(x, y)

    def _fmt_color(self, col: QColor) -> str:
        """按当前格式输出色值文本：RGB / HSV / HSL。"""
        if self._color_fmt == "hsv":
            h, s, v, _ = col.getHsv()
            return f"HSV({h}, {round(s * 100 / 255)}%, {round(v * 100 / 255)}%)"
        if self._color_fmt == "hsl":
            h, s, l, _ = col.getHsl()
            return f"HSL({h}, {round(s * 100 / 255)}%, {round(l * 100 / 255)}%)"
        return f"RGB({col.red()}, {col.green()}, {col.blue()})"

    def _cycle_color_fmt(self):
        """按 H：RGB → HSV → HSL → RGB 循环。"""
        order = ["rgb", "hsv", "hsl"]
        self._color_fmt = order[(order.index(self._color_fmt) + 1) % len(order)]

    # ---- 绘制辅助 ----
    def _apply_small_font(self, p: QPainter):
        """信息标签统一用较小字号（11px），避免默认字号偏大。"""
        f = p.font()
        f.setPixelSize(11)
        p.setFont(f)

    def _sample_picker_image(self, src: QImage, sx: int, sy: int,
                             n: int) -> QImage | None:
        """采样 n×n 物理像素为一张小图（越界处补黑，与逐格读取语义一致）。

        `src` 由调用方按光标所在屏取（多屏下非主屏），不再内部固定读主屏底图。
        numpy 一次性取块，替代原先每帧 n² 次 QImage.pixelColor（n=15 时 225 次
        逐像素取色，单次涉及格式换算）；返回 None 表示源图格式不适合直接取块，
        调用方回退逐格路径。
        """
        if src is None or src.depth() != 32 or src.isNull():
            return None
        try:
            import numpy as np
            sub = np.zeros((n, n, 4), dtype=np.uint8)
            sub[..., 3] = 255                    # 不透明（越界处即黑）
            x0, y0 = max(0, sx), max(0, sy)
            x1 = min(src.width(), sx + n)
            y1 = min(src.height(), sy + n)
            if x1 > x0 and y1 > y0:
                raw = np.frombuffer(src.constBits(), dtype=np.uint8).reshape(
                    src.height(), src.bytesPerLine())
                rows = raw[y0:y1, x0 * 4:x1 * 4].reshape(y1 - y0, x1 - x0, 4)
                sub[y0 - sy:y1 - sy, x0 - sx:x1 - sx] = rows
            # Format_RGB32 的内存布局即 BGRA 字节序，与源图 32 位格式一致
            qimg = QImage(sub.data, n, n, int(sub.strides[0]),
                          QImage.Format_RGB32)
            return qimg.copy()                   # copy：脱离临时 buffer
        except Exception:                        # noqa: BLE001 取块失败走回退
            return None

    # ---- 局部重绘的脏区裕量（逻辑 px）----
    # 遮罩覆盖整个虚拟桌面（2560×1440 级别），原先每次鼠标移动都 `update()`
    # 整屏重画：100Hz 鼠标 = 100 次/秒全屏 fillRect。改为只重画「这帧真的变了」
    # 的区域。下面这些裕量宁可给宽一点——少算会留下残影，多算只是多刷几行像素。
    _LABEL_BAND = 64      # 尺寸/标题标签贴在框上方（单行/两行），按两行留裕量
    _LABEL_MAX_W = 460    # 标签最宽（长标题 + 像素坐标），同样宁宽勿窄
    _PICK_ENV_W = 420     # 取色器（放大镜 + 三行色值框）相对光标的横向包络
    _PICK_ENV_H = 220     # 同上，纵向包络（含「放不下翻到光标左/上」的翻转）

    def _draw_picker(self, p: QPainter):
        """光标放大镜：采样 n×n 物理像素放大显示，中心格高亮 + 色值标签。"""
        pt = self._cursor_pt
        if pt is None or not self._screen_imgs:
            return
        # 底图按光标所在屏取：多屏下主屏底图配主屏 dpr 会把放大镜整块画成黑格
        src, ix, iy = self._pick_src(pt)
        if src is None:
            return
        n = self._picker_size
        zoom = self._picker_zoom
        # 采样区（物理像素，以光标为中心）
        sx = ix - n // 2
        sy = iy - n // 2
        cx = n // 2   # 中心格（光标处）
        cy = n // 2
        # 放大镜尺寸（逻辑 px）
        mw, mh = n * zoom, n * zoom
        # 定位：优先光标右下方，放不下翻左上
        mx, my = pt.x() + 16, pt.y() + 16
        if mx + mw > self.width():
            mx = pt.x() - mw - 16
        if my + mh > self.height():
            my = pt.y() - mh - 16
        mx = max(0, mx); my = max(0, my)
        # 本帧实际画过的区域（供局部重绘擦除旧位置，见 _content_dirty）
        dirty = QRect(mx, my, mw + 1, mh + 1)
        # 底
        p.setPen(QPen(QColor(255, 255, 255), 1))
        p.setBrush(QColor(0, 0, 0, 190))
        p.drawRect(QRect(mx, my, mw + 1, mh + 1))
        # 采样内容：整块一次性绘制（缩放为整数倍且关闭平滑 → 每格边界清晰，
        # 与原逐格 fillRect 的观感一致）
        small = self._sample_picker_image(src, sx, sy, n)
        if small is not None:
            _smooth = p.renderHints() & QPainter.SmoothPixmapTransform
            p.setRenderHint(QPainter.SmoothPixmapTransform, False)
            p.drawImage(QRect(mx, my, mw, mh), small)
            p.setRenderHint(QPainter.SmoothPixmapTransform, bool(_smooth))
        else:
            img = src
            for gy in range(n):
                for gx in range(n):
                    px, py = sx + gx, sy + gy
                    if 0 <= px < img.width() and 0 <= py < img.height():
                        col = img.pixelColor(px, py)
                    else:
                        col = QColor(0, 0, 0)
                    p.fillRect(QRect(mx + gx * zoom, my + gy * zoom,
                                     zoom, zoom), col)
        # 网格线
        p.setPen(QPen(QColor(255, 255, 255, 60), 1))
        for i in range(1, n):
            p.drawLine(mx + i * zoom, my, mx + i * zoom, my + mh)
            p.drawLine(mx, my + i * zoom, mx + mw, my + i * zoom)
        # 中心格高亮框
        p.setPen(QPen(QColor(255, 255, 255), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRect(mx + cx * zoom, my + cy * zoom, zoom, zoom))
        # 中心色值（光标处像素）
        col = self._pick_color(pt)
        if col is not None:
            self._apply_small_font(p)          # 信息文字用小字号
            hexv = "#%02X%02X%02X" % (col.red(), col.green(), col.blue())
            px, py = self._pixel_pt or (0, 0)
            fm = p.fontMetrics()
            th = fm.height() + 6
            # 三行：坐标 / HEX 色值 / RGB·HSV·HSL
            coord = f"@{px},{py}"
            fmt = self._fmt_color(col)
            cw = max(fm.horizontalAdvance(coord),
                     fm.horizontalAdvance(hexv),
                     fm.horizontalAdvance(fmt)) + 20
            lx = mx
            ly = my + mh + 4
            if ly + 3 * th + 4 > self.height():
                ly = my - 3 * th - 4
            dirty = dirty.united(QRect(lx, ly, cw, 3 * th + 2))
            # 底（三行）
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 190))
            p.drawRect(QRect(lx, ly, cw, 3 * th + 2))
            # 第 1 行：坐标（白字）
            p.setPen(QColor(255, 255, 255))
            p.drawText(QRect(lx + 7, ly + 3, cw - 14, th - 6), coord)
            # 第 2 行：色块 + HEX
            p.setPen(QPen(QColor(255, 255, 255), 1))
            p.setBrush(col)
            p.drawRect(QRect(lx + 4, ly + th + 3, th - 6, th - 6))
            p.setPen(QColor(255, 255, 255))
            p.drawText(QRect(lx + th + 2, ly + th + 3, cw - th, th - 6), hexv)
            # 第 3 行：RGB / HSV / HSL
            p.setPen(QColor(255, 255, 255))
            p.drawText(QRect(lx + 7, ly + 2 * th + 3, cw - 14, th - 6), fmt)
        self._picker_painted = dirty

    def _draw_size_label(self, p: QPainter, r: QRect):
        """框选/微调时在框上方显示尺寸 + 光标像素坐标（宽×高 @ x,y）。"""
        label = f"{r.width()} × {r.height()}"
        if self._pixel_pt is not None:
            label += f"  @{self._pixel_pt[0]},{self._pixel_pt[1]}"
        self._apply_small_font(p)          # 信息文字用小字号
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(label) + 14
        th = fm.height() + 6
        lx = r.left()
        ly = r.top() - th - 4 if r.top() - th > 0 else r.top() + 4
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 170))
        p.drawRect(QRect(lx, ly, tw, th))
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRect(lx + 7, ly + 3, tw - 14, th - 6), label)

    def _draw_scroll_badge(self, p: QPainter):
        """滚动截图模式徽标（顶部居中）：提示框选滚动区域。"""
        label = (f"⇣ 滚动截图：框选后 左键自动 / 右键退出 / 滚轮手动"
                 f"（{self._scroll_hotkey.upper()} 退出）")
        self._apply_small_font(p)
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(label) + 20
        th = fm.height() + 10
        lx = (self.width() - tw) // 2
        ly = 10
        p.setPen(QPen(QColor(138, 180, 255), 1))
        p.setBrush(QColor(28, 28, 30, 235))
        p.drawRoundedRect(QRect(lx, ly, tw, th), 8, 8)
        p.setPen(QColor(138, 180, 255))
        p.drawText(QRect(lx + 10, ly + 5, tw - 20, th - 10),
                   Qt.AlignCenter, label)

    def _draw_hover_label(self, p: QPainter, r: QRect):
        """悬停窗口框标签：标题 + 窗口尺寸/像素坐标（两行）。

        滚动截图模式：提示单击将滚动截取整个窗口。
        """
        title = self._hover_title
        if len(title) > 30:
            title = title[:30] + "…"
        if self._scroll_mode:
            label = (f"单击滚动截取此窗口：{title}" if title
                     else "单击滚动截取此窗口")
        else:
            label = f"单击捕捉窗口：{title}" if title else "单击捕捉窗口"
        # 窗口像素信息（第二行）
        px, py = self._pixel_pt or (0, 0)
        info = f"窗口 {r.width()}×{r.height()}  @{px},{py}"
        self._apply_small_font(p)          # 信息文字用小字号
        fm = p.fontMetrics()
        th = fm.height() + 6
        tw = max(fm.horizontalAdvance(label), fm.horizontalAdvance(info)) + 14
        lx = r.left()
        ly = r.top() - 2 * th - 4 if r.top() - 2 * th > 0 else r.top() + 4
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 170))
        p.drawRect(QRect(lx, ly, tw, 2 * th + 2))
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRect(lx + 7, ly + 3, tw - 14, th - 6), label)
        p.setPen(QColor(200, 220, 255))
        p.drawText(QRect(lx + 7, ly + th + 3, tw - 14, th - 6), info)

    # ---- 交互 ----
    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self._on_esc()              # 右键 = 取消截图（走幂等唯一出口）
            return
        if self._adjusted is not None:
            return                      # 微调锁定中：按下即确认，保留选区
        self._start = e.pos()
        self._end = e.pos()
        self._dragging = False
        self._ratio_lock = None
        self._press_win = self._window_at(e.pos())
        self.update()

    def _frame_dirty(self, r: QRect) -> QRect:
        """选区/悬停框的脏区：框本身（含描边）+ 它上方那条标签。

        标签画在 `r.top() - th - 4`（贴框上方，放不下才落回框内），且可能比框
        宽得多（长标题 + 像素坐标）——所以横向按 `_LABEL_MAX_W` 留量，
        而不是跟着框宽走。
        """
        pen = self._border_width + 4
        band = QRect(r.left() - 4, r.top() - self._LABEL_BAND,
                     self._LABEL_MAX_W, self._LABEL_BAND + 8)
        return r.adjusted(-pen, -pen, pen, pen).united(band)

    def _content_dirty(self) -> QRect:
        """本帧「内容可能变化」的区域——局部重绘用它替代整屏 update()。

        组成：当前选区帧（微调框 / 橡皮筋 / 悬停高亮） ∪ 取色器（**上一帧实际
        画过的矩形** ∪ 当前光标处的包络）。

        取色器为什么要「上一帧矩形 + 包络」两份：旧位置的像素必须被擦掉（只有
        记录过才知道它画在哪），而新位置的布局要等 `_draw_picker` 跑完才知道，
        所以按最大包络保守覆盖，不去复刻那份布局算式。
        """
        d = QRect()
        r = self._adjusted
        if r is None and self._start is not None and self._end is not None:
            r = QRect(self._start, self._end).normalized()
        if r is not None:
            d = d.united(self._frame_dirty(r))
        elif self._hover_win is not None:
            d = d.united(self._frame_dirty(self._hover_win))
        painted = self._picker_painted
        if painted is not None:
            d = d.united(painted)
        pt = self._cursor_pt
        if pt is not None:
            d = d.united(QRect(pt.x() - self._PICK_ENV_W, pt.y() - self._PICK_ENV_H,
                               self._PICK_ENV_W * 2, self._PICK_ENV_H * 2))
        # 先外扩再与整屏求交：外扩是为盖住描边/抗锯齿的 1px 溢出；求交保证不会
        # 产生窗口外的无效区域（也让「脏区必在窗口内」成为可断言的事实）。
        # 空脏区要单独返回空矩形：QRect() 外扩后会变成一个 (0,0,2,2) 的小区域，
        # 白白触发一次原点处的重绘。
        if d.isNull():
            return QRect()
        return d.adjusted(-2, -2, 2, 2).intersected(self.rect())

    def mouseMoveEvent(self, e):
        # 先按**旧状态**取脏区（旧选区/旧取色器位置），改完状态再取一次并合并：
        # 两帧之间的差异区域才是真正要重画的部分。原先这里是无条件
        # `self.update()`，等价于每次鼠标移动都重画整个虚拟桌面。
        dirty = self._content_dirty()
        self._cursor_pt = e.pos()
        if self._adjusted is not None:
            # 滚轮微调后锁定选区：移动鼠标不改变（避免悬停窗口覆盖微调框）
            self.update(dirty.united(self._content_dirty()))
            return
        if self._start is not None:
            # 超过阈值才算拖拽（区分单击捕捉与自由框选）
            if not self._dragging and (e.pos() - self._start).manhattanLength() > 5:
                self._dragging = True
                self._press_win = None
            self._end = self._apply_modifiers(e)
        else:
            # 悬停：更新窗口高亮。_window_at 会沿 Win32 Z 序逐个候选窗口调用
            # 多次 API（桌面窗口多时每秒数千次跨 FFI 调用），按位移阈值 +
            # 时间节流复用上次结果。
            if self._should_probe_hover(e.pos()):
                info = self._window_at(e.pos())
                if info is not None:
                    self._hover_win = info[1]
                    self._hover_title = _window_title(info[0])
                    self.setCursor(Qt.PointingHandCursor)
                else:
                    self._hover_win = None
                    self._hover_title = ""
                    self.setCursor(Qt.CrossCursor)
        self.update(dirty.united(self._content_dirty()))

    def _should_probe_hover(self, pt: QPoint) -> bool:
        """悬停窗口探测节流：位移 < 8px 且距上次探测 < 30ms 时复用结果。

        鼠标高频移动（100Hz+）时无需每次都遍历窗口 Z 序——高亮框的视觉
        粒度远小于 8px，节流后行为观感不变而 FFI 调用量显著下降。
        """
        import time as _t
        last = self._hover_probe
        now = _t.monotonic()
        if last is not None:
            lx, ly, lt = last
            if (abs(pt.x() - lx) < 8 and abs(pt.y() - ly) < 8
                    and (now - lt) < 0.03):
                return False
        self._hover_probe = (pt.x(), pt.y(), now)
        return True

    def _apply_modifiers(self, e):
        """按修饰键调整框选终点：
        - Shift：放慢鼠标位移（仅取 1/4，精细框选）
        - Alt：锁定宽高比（以起点为锚，等比缩放）
        """
        pt = e.pos()
        sx, sy = self._start.x(), self._start.y()
        # Shift 慢速：位移缩小到 1/4
        if e.modifiers() & Qt.ShiftModifier:
            dx = int(round((pt.x() - sx) * 0.25))
            dy = int(round((pt.y() - sy) * 0.25))
            pt = QPoint(sx + dx, sy + dy)
        # Alt 等比：锁定按下时的宽高比（比值恒为正，见 _positive_ratio）
        if e.modifiers() & Qt.AltModifier:
            if self._ratio_lock is None:
                w = abs(pt.x() - sx)
                h = abs(pt.y() - sy)
                self._ratio_lock = _positive_ratio(w, h)
            w = abs(pt.x() - sx)
            h = abs(pt.y() - sy)
            r = _positive_ratio(self._ratio_lock)   # 使用侧同样经它取值（防御）
            if w < h * r:
                w = int(round(h * r))
            else:
                h = int(round(w / r))
            # 按象限还原方向
            pt = QPoint(sx + (w if pt.x() >= sx else -w),
                        sy + (h if pt.y() >= sy else -h))
        else:
            self._ratio_lock = None
        return pt

    def wheelEvent(self, e):
        """滚轮微调：悬停识别到窗口时，以窗口中心为锚等距扩大/缩小选区。
        上滚扩大、下滚缩小（每格 4px）；微调后锁定，单击确认、Esc 取消。"""
        if self._start is not None or self._dragging:
            return                       # 拖拽中不微调
        base = self._hover_win
        if base is None:
            return
        dy = e.angleDelta().y()
        if dy == 0:
            return
        step = 4 if dy > 0 else -4       # 上滚 +4px / 下滚 -4px（每边）
        # 各维度独立 clamp 下限 8px；每边变化量 = (目标-当前)/2（最后一步自动收窄）
        w = max(8, base.width() + 2 * step)
        h = max(8, base.height() + 2 * step)
        dw = (w - base.width()) // 2
        dh = (h - base.height()) // 2
        r = base.adjusted(-dw, -dh, dw, dh)   # 四边等距，中心保持
        self._adjusted = r
        self._hover_win = r              # 高亮跟随微调框
        self.update()
        e.accept()

    def mouseReleaseEvent(self, e):
        if self._adjusted is not None:
            # 滚轮微调后单击确认：提交微调选区（press 时已锁定，无需 _start）
            r = self._adjusted
            self._start = self._end = self._adjusted = self._hover_win = None
            self._closing = True    # 已定稿：后续 Esc（双通路）不再发 cancelled
            self.region_selected.emit(self._to_raw(r))
            self.close()
            return
        if self._start is None:
            return
        self._end = e.pos()
        if not self._dragging:
            # 单击：滚动截图模式 → 命中窗口则滚动截取整个窗口；空白取消
            # （不误触普通捕捉）；普通模式命中窗口 → 捕捉该窗口；空白 → 取消
            if self._scroll_mode:
                if self._press_win is not None:
                    self._closing = True
                    self.scroll_requested.emit(self._to_raw(self._press_win[1]))
                else:
                    self._on_esc()      # 空白取消：走幂等唯一出口
                    return
            elif self._press_win is not None:
                self._closing = True
                self.region_selected.emit(self._to_raw(self._press_win[1]))
            else:
                self._on_esc()          # 空白取消：走幂等唯一出口
                return
            self.close()
            return
        r = QRect(self._start, self._end).normalized()
        self._start = self._end = None
        if r.width() < 5 or r.height() < 5:
            self._on_esc()          # 拖拽过小视为误触/取消（走幂等唯一出口）
            return
        self._closing = True        # 已定稿：后续 Esc（双通路）不再发 cancelled
        if self._scroll_mode:
            self.scroll_requested.emit(self._to_raw(r))   # 滚动截图：框选完成 → 滚屏拼接
        else:
            self.region_selected.emit(self._to_raw(r))
        self.close()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._on_esc()
            return
        # 按配置键（默认 R）：切换滚动截图模式（截长图，再按一次退出）
        if self._match_hotkey(e, self._scroll_hotkey):
            self._toggle_scroll_mode()
            e.accept()
            return
        # 按配置键（默认 A）：切换全屏框选（再按一次恢复之前的选区）
        if self._cursor_pt is not None and self._match_hotkey(
                e, self._full_sel_hotkey):
            self._toggle_fullscreen()
            e.accept()
            return
        # 按配置键（默认 H）：循环切换色值格式 RGB → HSV → HSL
        if self._cursor_pt is not None and self._match_fmt_key(e):
            self._cycle_color_fmt()
            self._set_status(f"色值格式：{self._color_fmt.upper()}")
            self.update()
            e.accept()
            return
        # 复制色值快捷键（默认 C，可在设置中改键）
        if self._cursor_pt is not None and self._match_pick_key(e):
            col = self._pick_color(self._cursor_pt)
            if col is not None:
                hexv = "#%02X%02X%02X" % (col.red(), col.green(), col.blue())
                QApplication.clipboard().setText(hexv)
                self._set_status(f"已复制色值 {hexv}")
            e.accept()

    def _on_esc(self):
        """Esc 取消截图的唯一出口（keyPressEvent 与 QShortcut 共用）：
        先发 cancelled 再 close——App 侧靠 cancelled 恢复截图前隐藏的浮窗。

        本函数必须**幂等**：Esc 有两条通路（keyPressEvent + ApplicationShortcut），
        一次按键可能落到两处，重复 emit 会让 App 侧把「恢复浮窗」做两次
        （若其实现是 toggle 就会二次翻转）。
        """
        if self._closing:
            return
        self._closing = True
        self.cancelled.emit()
        self.close()

    def _toggle_scroll_mode(self):
        """R 键：滚动截图模式开关（keyPressEvent 与 QShortcut 共用入口）。"""
        self._scroll_mode = not self._scroll_mode
        self._set_status(
            "滚动截图模式：框选滚动区域，松手后**手动滚动页面**实时拼接"
            f"（Enter 完成 / Esc 退出；再按 {self._scroll_hotkey.upper()} 退出模式）"
            if self._scroll_mode else "已退出滚动截图模式")
        self.update()

    def _toggle_fullscreen(self):
        """A 键：全屏框选 ⇄ 恢复原选区。进入时保存当前选区，退出时还原。"""
        # 与遮罩几何保持一致（遮罩覆盖哪块屏，全屏选区就是哪块屏）
        scr = self.geometry()
        if not self._fullscreen:
            self._prev_sel = (self._start, self._end, self._adjusted)
            self._fullscreen = True
            self._start = scr.topLeft()
            self._end = scr.bottomRight()
            self._adjusted = QRect(scr)
            self._hover_win = None
            self._set_status(f"已框选全屏 {scr.width()}×{scr.height()}（再按 A 恢复）")
        else:
            self._fullscreen = False
            self._start, self._end, self._adjusted = self._prev_sel
            self._prev_sel = None
            self._set_status("已恢复原选区")
        self.update()

    def _match_pick_key(self, e) -> bool:
        """匹配取色复制快捷键（如 "c" / "shift+c"）。"""
        return self._match_hotkey(e, self._pick_hotkey)

    def _match_fmt_key(self, e) -> bool:
        """匹配切换色值格式快捷键（默认 "h"）。"""
        return self._match_hotkey(e, self._fmt_hotkey)

    @staticmethod
    def _match_hotkey(e, hotkey: str) -> bool:
        """通用热键匹配：'c' / 'shift+c'（修饰键可选，需精确一致）。"""
        hk = hotkey.lower().replace("+", " ")
        parts = hk.split()
        key_name = parts[-1] if parts else ""
        mods = parts[:-1]
        if not (len(key_name) == 1 and key_name.isalpha()):
            return False
        if e.key() != getattr(Qt, f"Key_{key_name.upper()}", None):
            return False
        def has(mod):
            return bool(e.modifiers() & mod)
        want = {"ctrl": has(Qt.ControlModifier),
                "alt": has(Qt.AltModifier),
                "shift": has(Qt.ShiftModifier),
                "win": has(Qt.MetaModifier)}
        for m in ("ctrl", "alt", "shift", "win"):
            if (m in mods) != want[m]:
                return False
        return True

    def _set_status(self, text: str):
        """遮罩状态提示：无状态栏时打印（截图遮罩不显示状态栏）。"""
        print(f"[截图] {text}")
