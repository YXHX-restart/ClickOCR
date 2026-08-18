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
import sys
from ctypes import wintypes

from PySide6.QtCore import Qt, QRect, Signal, QPoint
from PySide6.QtGui import (QPainter, QColor, QPen, QBrush, QFont,
                           QGuiApplication, QImage)
from PySide6.QtWidgets import QApplication, QWidget

from config import ConfigManager

WIN = sys.platform.startswith("win")

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
    region_selected = Signal(QRect)     # 逻辑坐标
    cancelled = Signal()                # 取消截图（Esc / 空白单击）
    scroll_requested = Signal(QRect)    # 滚动截图模式：框选完成 → 滚动区域

    def __init__(self, config: ConfigManager | None = None):
        super().__init__()
        cfg = config or ConfigManager()
        self._border_color = QColor(*cfg.getcolor("selection", "border_color", (100, 190, 255)))
        self._border_width = cfg.getint("selection", "border_width", 2)
        self._fill_alpha = cfg.getint("selection", "fill_alpha", 50)
        self._mask_alpha = cfg.getint("selection", "mask_alpha", 120)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)   # 悬停检测窗口框
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)
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
        self._screen_img = None       # 屏幕底图（遮罩显示前抓取，供取色）
        self._picker_size = 7         # 采样边长（奇数，3~15）
        self._picker_zoom = 9         # 每像素放大倍数
        self._pick_hotkey = "c"       # 复制色值快捷键（config 可配）
        self._fmt_hotkey = "h"        # 切换色值格式快捷键（config 可配）
        self._color_fmt = "rgb"       # 色值格式：rgb | hsv | hsl（按 H 循环）
        self._slow = False            # Shift 按住：放慢鼠标（精细框选）
        self._ratio_lock = None       # Alt 按住：锁定宽高比（按下的比例）
        self._scroll_mode = False     # R 键：滚动截图模式（截长图）
        self._scroll_hotkey = "r"     # 滚动截图模式快捷键（config 可配）
        try:
            cfg0 = config or ConfigManager()
            self._pick_hotkey = cfg0.get("hotkey_ext", "color_pick", "c") or "c"
            self._pick_hotkey = self._pick_hotkey.lower()
            self._fmt_hotkey = cfg0.get("hotkey_ext", "color_fmt", "h") or "h"
            self._fmt_hotkey = self._fmt_hotkey.lower()
            # 全屏框选快捷键（[hotkey_ext] full_sel，默认 A，可改键/可关闭）
            self._full_sel_hotkey = (cfg0.get("hotkey_ext", "full_sel", "a")
                                     or "a").lower()
            # 滚动截图模式快捷键（[hotkey_ext] scroll_sel，默认 R）
            self._scroll_hotkey = (cfg0.get("hotkey_ext", "scroll_sel", "r")
                                   or "r").lower()
            self._picker_size = cfg0.getint("selection", "picker_size", 7)
            if self._picker_size % 2 == 0:
                self._picker_size = min(15, self._picker_size + 1)   # 奇数（中心格）
            self._picker_zoom = cfg0.getint("selection", "picker_zoom", 9)
            # 遮罩显示前抓屏：取色器读取真实屏幕像素（不被遮罩压暗）
            scr = QGuiApplication.primaryScreen()
            if scr is not None:
                self._screen_img = scr.grabWindow(0).toImage()
        except Exception:
            self._screen_img = None

        # Esc 兜底：Tool 窗口默认不抢键盘焦点（热键唤出后没点过鼠标时
        # keyPressEvent 收不到按键），用 ApplicationShortcut 在应用激活
        # 期间捕获 Esc 取消截图。
        from PySide6.QtGui import QShortcut, QKeySequence
        self._esc_sc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._esc_sc.setContext(Qt.ApplicationShortcut)
        self._esc_sc.activated.connect(self.close)
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
    def _window_at(self, logical_pt):
        """返回鼠标逻辑坐标下、遮罩下方的第一个可见窗口 (hwnd, 逻辑QRect)；无则 None。

        从本遮罩的 HWND 沿 Z 序向下（GW_HWNDPREV）遍历，跳过不可见/自身进程/
        桌面背景窗口；优先用 DWM 扩展外框（真实可见区域），失败回退 GetWindowRect。

        全屏窗口（覆盖整个屏幕）只在鼠标位于屏幕**边缘 5%** 区域时才算命中：
        否则把全屏当窗口会挡住自由框选（桌面无其他窗口时全屏会被误识别为窗口）。
        """
        if not WIN:
            return None
        dpr = self.devicePixelRatioF() or 1.0
        x = int(round(logical_pt.x() * dpr))
        y = int(round(logical_pt.y() * dpr))
        scr = QApplication.primaryScreen().geometry()
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
            qr = QRect(
                int(round(rect.left / dpr)), int(round(rect.top / dpr)),
                int(round((rect.right - rect.left) / dpr)),
                int(round((rect.bottom - rect.top) / dpr)),
            )
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
    def _pick_color(self, logical_pt) -> QColor | None:
        """取光标处屏幕像素颜色（物理坐标 × dpr 读底图）。"""
        if self._screen_img is None or logical_pt is None:
            return None
        dpr = self.devicePixelRatioF() or 1.0
        x = int(round(logical_pt.x() * dpr))
        y = int(round(logical_pt.y() * dpr))
        if not (0 <= x < self._screen_img.width()
                and 0 <= y < self._screen_img.height()):
            return None
        self._pixel_pt = (x, y)
        return self._screen_img.pixelColor(x, y)

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

    def _draw_picker(self, p: QPainter):
        """光标放大镜：采样 n×n 物理像素放大显示，中心格高亮 + 色值标签。"""
        pt = self._cursor_pt
        if pt is None or self._screen_img is None:
            return
        dpr = self.devicePixelRatioF() or 1.0
        n = self._picker_size
        zoom = self._picker_zoom
        # 采样区（物理像素，以光标为中心）
        sx = int(round(pt.x() * dpr)) - n // 2
        sy = int(round(pt.y() * dpr)) - n // 2
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
        # 底
        p.setPen(QPen(QColor(255, 255, 255), 1))
        p.setBrush(QColor(0, 0, 0, 190))
        p.drawRect(QRect(mx, my, mw + 1, mh + 1))
        # 逐格放大
        img = self._screen_img
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
            self.cancelled.emit()       # 右键 = 取消截图
            self.close()
            return
        if self._adjusted is not None:
            return                      # 微调锁定中：按下即确认，保留选区
        self._start = e.pos()
        self._end = e.pos()
        self._dragging = False
        self._ratio_lock = None
        self._press_win = self._window_at(e.pos())
        self.update()

    def mouseMoveEvent(self, e):
        self._cursor_pt = e.pos()
        if self._adjusted is not None:
            # 滚轮微调后锁定选区：移动鼠标不改变（避免悬停窗口覆盖微调框）
            self.update()
            return
        if self._start is not None:
            # 超过阈值才算拖拽（区分单击捕捉与自由框选）
            if not self._dragging and (e.pos() - self._start).manhattanLength() > 5:
                self._dragging = True
                self._press_win = None
            self._end = self._apply_modifiers(e)
        else:
            # 悬停：更新窗口高亮
            info = self._window_at(e.pos())
            if info is not None:
                self._hover_win = info[1]
                self._hover_title = _window_title(info[0])
                self.setCursor(Qt.PointingHandCursor)
            else:
                self._hover_win = None
                self._hover_title = ""
                self.setCursor(Qt.CrossCursor)
        self.update()

    def _apply_modifiers(self, e):
        """按修饰键调整框选终点：
        - Shift：放慢鼠标位移（仅取 1/4，精细框选）
        - Alt：锁定宽高比（以起点为锚，等比缩放）
        """
        pt = e.pos()
        sx, sy = self._start.x(), self._start.y()
        # Shift 慢速：位移缩小到 1/4
        if e.modifiers() & Qt.ShiftModifier:
            self._slow = True
            dx = int(round((pt.x() - sx) * 0.25))
            dy = int(round((pt.y() - sy) * 0.25))
            pt = QPoint(sx + dx, sy + dy)
        else:
            self._slow = False
        # Alt 等比：锁定按下时的宽高比
        if e.modifiers() & Qt.AltModifier:
            if self._ratio_lock is None:
                w = abs(pt.x() - sx)
                h = abs(pt.y() - sy)
                self._ratio_lock = (w / h) if h > 0 else 1.0
            w = abs(pt.x() - sx)
            h = abs(pt.y() - sy)
            if w < h * self._ratio_lock:
                w = int(round(h * self._ratio_lock))
            else:
                h = int(round(w / self._ratio_lock))
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
            self.region_selected.emit(r)
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
                    self.scroll_requested.emit(self._press_win[1])
                else:
                    self.cancelled.emit()
            elif self._press_win is not None:
                self.region_selected.emit(self._press_win[1])
            else:
                self.cancelled.emit()
            self.close()
            return
        r = QRect(self._start, self._end).normalized()
        self._start = self._end = None
        if r.width() < 5 or r.height() < 5:
            self.cancelled.emit()   # 拖拽过小视为误触/取消
            self.close()
            return
        if self._scroll_mode:
            self.scroll_requested.emit(r)   # 滚动截图：框选完成 → 滚屏拼接
        else:
            self.region_selected.emit(r)
        self.close()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.cancelled.emit()
            self.close()
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
        scr = QApplication.primaryScreen().geometry()
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
