"""视图变换：缩放、适配屏幕、边缘拖拽调整、导航图 —— 从 result_overlay.py 按职责搬移（纯搬移，行为零变化）。

归属断言见 scripts/_verify_review_fixes.py 的 5f 区块：方法一旦被别的 mixin 或
ResultOverlay 同名覆盖，MRO 会**静默遮蔽**（不报错、只是行为变了），故必须断言。
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, QPointF

from ui.base.widgets import screen_available_geometry



class ZoomNavMixin:
    """视图变换（缩放 / 适配 / 边缘调整 / 导航图）"""
    def _apply_transform(self):
        """统一视图变换：系数 = 几何缩放 × 图片缩放（窗口宽高比与图片解耦后，
        图片大小只由这两个系数决定，不随窗口边缘拉伸变化）。"""
        self.resetTransform()
        self.scale(self._zoom * self._img_zoom / self.dpr,
                   self._zoom * self._img_zoom / self.dpr)

    def _set_image_zoom(self, zoom: float, anchor: QPointF):
        """窗口内图片缩放：**只改 transform，绝不动窗口几何**（窗口尺寸不变，
        图片在窗口内缩放；超出部分靠滚动条/中键平移查看）。
        以鼠标位置为锚：缩放前后鼠标指向的场景点保持不动（滚动条补偿）。"""
        zoom = max(0.1, min(10.0, zoom))
        if abs(zoom - self._img_zoom) < 1e-3:
            return
        t0 = self.transform().m11()
        if t0 <= 0:
            return
        self._img_zoom = zoom
        self._apply_transform()
        t1 = self.transform().m11()
        # 滚动条补偿：锚点（视口坐标）下的场景点保持不动。
        # 正确公式 v1 = (a + v0) * t1/t0 - a（推导：锚点场景坐标 s=(a+v0)/t0，
        # 缩放后要求 a = s*t1 - v1）。原实现 v0 + a*(1/t0 - 1/t1) 仅在
        # v0=0（未平移）时近似成立——大图平移后再缩放，鼠标指向点会漂移
        hb, vb = self.horizontalScrollBar(), self.verticalScrollBar()
        hb.setValue(int((anchor.x() + hb.value()) * t1 / t0 - anchor.x()))
        vb.setValue(int((anchor.y() + vb.value()) * t1 / t0 - anchor.y()))
        self._layout_close_btn()
        self._fgroup.follow()
        self._update_navigator()

    def _set_zoom(self, zoom: float, anchor: QPointF):
        """以窗口内 anchor 点为锚缩放窗口（几何缩放）：窗口尺寸 = 当前基准 × zoom，
        锚点下场景坐标保持不动（鼠标指向处不漂移）。图片缩放系数 _img_zoom 保留。"""
        zoom = max(0.05, min(5.0, zoom))
        if abs(zoom - self._zoom) < 1e-3:
            return
        self._zoom = zoom
        nw = max(1, int(round(self._base_w * zoom)))
        nh = max(1, int(round(self._base_h * zoom)))
        # 锚点在窗口内的相对位置（缩放后保持不变）
        rx = anchor.x() / max(1, self.width())
        ry = anchor.y() / max(1, self.height())
        # 新窗口左上角 = 锚点全局位置 - 相对偏移 × 新尺寸
        g = self.mapToGlobal(anchor.toPoint())
        nx = g.x() - rx * nw
        ny = g.y() - ry * nh
        self.setGeometry(int(nx), int(ny), nw, nh)
        self._apply_transform()
        self._layout_close_btn()
        self._fgroup.follow()
        self._layout_navigator()
        self._update_navigator()

    def fit_to_screen(self):
        """滚动截图长图：窗口等比缩放到**整图**在屏幕内可见。

        普通贴图窗口尺寸=框选区域；长图比区域高得多，若不缩放只能看到
        顶部一屏（视觉上"图片被限制在屏幕高度"）。此处把缩放基准改成
        整图逻辑尺寸，等比放入可用屏幕（占 90%），窗口水平居中。
        """
        h, w = self.img.shape[:2]
        lw, lh = w / self.dpr, h / self.dpr
        # 用窗口所在屏（多显示器下按主屏算会把图缩到与所在屏不匹配的尺寸）
        scr = screen_available_geometry(self)
        zoom = min(1.0, scr.width() * 0.9 / lw, scr.height() * 0.9 / lh)
        zoom = max(0.05, zoom)
        nw = max(1, int(round(lw * zoom)))
        nh = max(1, int(round(lh * zoom)))
        x = scr.left() + max(4, (scr.width() - nw) // 2)
        y = scr.top() + max(4, (scr.height() - nh) // 4)
        self._base_w = int(math.ceil(lw))  # 基准 = 图片逻辑尺寸（几何缩放连续）
        self._base_h = int(math.ceil(lh))
        self._zoom = zoom
        self._img_zoom = 1.0              # 整图适配后重置窗口内图片缩放
        self.setGeometry(x, y, nw, nh)
        self._apply_transform()
        self._layout_close_btn()
        self._fgroup.follow()
        self._layout_navigator()
        self._update_navigator()

    def _toggle_navigator(self):
        """导航图开关（快捷键可配置，默认 M；标注模式跳过，避免误触）。"""
        if self._anno_mode:
            return
        strip = self._status_strip
        if strip is not None:
            strip.btn_nav.setChecked(not strip.btn_nav.isChecked())

    def _toggle_ratio_lock(self):
        """等比例调节窗口开关（快捷键 E / 状态栏按钮）：翻转按钮即翻转状态。"""
        strip = self._status_strip
        if strip is not None:
            strip.btn_ratio.setChecked(not strip.btn_ratio.isChecked())

    def _set_ratio_lock(self, on: bool):
        """状态栏等比例按钮状态 → 同步锁定状态；解锁/重新锁定时立即重新适配
        图片（锁定=填满窗口；解锁=图片等比 contain 居中）。"""
        self._ratio_lock = bool(on)
        self._recontain()
        self._set_status("等比例调节窗口：锁定（窗口与图片同比例）" if on
                         else "等比例调节窗口：解锁（窗口可自由比例）")

    def set_navigator(self, on: bool):
        """状态栏导航图开关：开 = 显示缩略小地图（点击/拖动跳转大图）。"""
        self._nav_on = bool(on)
        if on:
            self._ensure_navigator()
            nav = self._navigator
            nav.show()
            nav.raise_()
            self._layout_navigator()
            self._update_navigator()
        elif self._navigator is not None:
            self._navigator.hide()

    def _ensure_navigator(self):
        if self._navigator is not None:
            return
        from ui.components.navigator import Navigator
        nav = Navigator()
        nav.set_image(self._pix)
        nav.jumped.connect(self._on_nav_jump)
        # 松手后还焦主窗口（Esc 等快捷键继续生效）
        nav.interacted.connect(
            lambda: (self.activateWindow(),
                     self.setFocus(Qt.OtherFocusReason)))
        self._navigator = nav

    def _layout_navigator(self):
        nav = self._navigator
        if nav is None or not self._nav_on or self._pinned:
            return
        x = self.x() + self.width() - nav.width() - 8
        y = self.y() + self.height() - nav.height() - 8
        nav.move(max(0, x), max(0, y))

    def _update_navigator(self):
        nav = self._navigator
        if nav is None or not self._nav_on:
            return
        view = self.mapToScene(self.viewport().rect()).boundingRect()
        nav.set_view(view)

    def _on_nav_jump(self, sx: float, sy: float):
        """导航图点击/拖动：跳转主视图（场景点居中）。"""
        vp = self.viewport()
        t = self.transform()
        self.horizontalScrollBar().setValue(
            max(0, int(sx * t.m11() - vp.width() / 2)))
        self.verticalScrollBar().setValue(
            max(0, int(sy * t.m22() - vp.height() / 2)))

    def _edge_at(self, pos) -> str | None:
        """返回鼠标位置命中的拉伸边/角；顶部中间 = 拖动把手区（返回 None，
        保留原顶部把手移动窗口行为），仅顶边两角可对角拉伸。
        尺寸以 viewport 为基准（e.position() 是视口坐标，滚动条出现时视口
        小于窗口，右/下热区落在滚动条内侧）。"""
        vp = self.viewport()
        w, h = vp.width(), vp.height()
        x, y = pos.x(), pos.y()
        ed = self._EDGE
        left = x <= ed
        right = x >= w - ed
        top = y <= ed
        bottom = y >= h - ed
        if top:
            if x <= ed * 2:
                return "tl"
            if x >= w - ed * 2:
                return "tr"
            return "t"             # 顶边中间也可拉伸（移动窗口用 Alt+拖动）
        if bottom:
            if x <= ed * 2:
                return "bl"
            if x >= w - ed * 2:
                return "br"
            return "b"
        if left:
            return "l"
        if right:
            return "r"
        return None

    def _start_edge_resize(self, e, edge: str):
        """开始边缘拉伸：记录 (全局点, 当前窗口几何)；拉伸中隐藏滚动条
        （避免随窗口缩放出现/消失造成闪烁），松手恢复。

        松手保证 = 下面的 `viewport().grabMouse()`（不是 QApplication 级事件
        过滤器）：抓取后松手事件无论光标在哪都回到 viewport → 本视图
        mouseReleaseEvent → _end_edge_resize；_end_edge_resize 自己会 releaseMouse。
        （模块顶部 __init__ 里的说明记录了「为什么可以不要 app 级兜底」。）
        """
        self._resize_edge = edge
        self._resize_geom = (e.globalPosition().toPoint(), self.geometry())
        self._manual_resize = True
        self._sb_prev = (self.horizontalScrollBarPolicy(),
                         self.verticalScrollBarPolicy())
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 抓取 viewport（而非 view 本体）鼠标：保证松手事件一定回到本窗、
        # 即便在窗口外/桌面上释放，_end_edge_resize 也能被触发，避免状态卡死。
        self.viewport().grabMouse()

    def _do_edge_resize(self, g):
        """拖动中按边缘类型调整窗口几何。
        - 等比例锁定（默认）：宽高比锁定图片比例（任意边/角拉伸都是等比缩放，
          图片恒填满窗口，无透明区、不变形）。
        - 解锁：窗口自由调整比例（水平/垂直独立），图片等比 contain 适配。"""
        g0, geo = self._resize_geom
        dx = g.x() - g0.x()
        dy = g.y() - g0.y()
        e = self._resize_edge
        x, y = geo.x(), geo.y()
        w, h = geo.width(), geo.height()
        if self._ratio_lock:
            ratio = (self._img_lw / self._img_lh) if self._img_lh > 0 else 1.0
            if "l" in e:
                w = max(80, geo.width() - dx)
            elif "r" in e:
                w = max(80, geo.width() + dx)
            if "t" in e:
                h = max(60, geo.height() - dy)
            elif "b" in e:
                h = max(60, geo.height() + dy)
            if "l" in e or "r" in e:
                h = max(60, int(round(w / ratio)))   # 水平主导
            else:
                w = max(80, int(round(h * ratio)))   # 垂直主导
        else:
            # 解锁：自由比例（独立调整宽高）
            if "l" in e:
                x = geo.x() + dx
                w = geo.width() - dx
            if "r" in e:
                w = geo.width() + dx
            if "t" in e:
                y = geo.y() + dy
                h = geo.height() - dy
            if "b" in e:
                h = geo.height() + dy
            w = max(80, w)
            h = max(60, h)
        if "l" in e:
            x = geo.right() - w
        if "t" in e:
            y = geo.bottom() - h
        self.setGeometry(x, y, w, h)

    def _end_edge_resize(self):
        """边缘拉伸结束：恢复滚动条策略（拉伸中隐藏防闪烁）+ 附属浮层重排。
        由 mouseReleaseEvent、滚动条事件过滤器、以及全局 MouseButtonRelease
        捕获共用（松手在窗口外/状态栏上也一定结束，避免 _resize_edge 卡死）。"""
        # 释放拉伸中可能抓取的 viewport 鼠标（保证不卡死）
        vp = self.viewport()
        if vp.mouseGrabber() is vp:
            vp.releaseMouse()
        if self._resize_edge is None:
            return
        self._resize_edge = None
        self._resize_geom = None
        self._manual_resize = False
        if getattr(self, "_sb_prev", None):
            self.setHorizontalScrollBarPolicy(self._sb_prev[0])
            self.setVerticalScrollBarPolicy(self._sb_prev[1])
            self._sb_prev = None
        self._layout_close_btn()
        self._fgroup.follow()
        self._layout_navigator()
        self._update_navigator()

    def _sb_edge(self, sb, event) -> str | None:
        """滚动条最外侧 _EDGE px 命中 = 对应窗口边/角；否则 None（滚动条照常）。
        垂直滚动条在右 → 'r'（细分 'tr'/'br'）；水平滚动条在下 → 'b'（细分 'bl'/'br'）。"""
        p = event.position()
        x, y = p.x(), p.y()
        ed = self._EDGE
        if sb is self.verticalScrollBar():
            if x < sb.width() - ed:       # 不在右边缘热区
                return None
            if y <= ed * 2:
                return "tr"
            if y >= sb.height() - ed * 2:
                return "br"
            return "r"
        # 水平滚动条
        if y < sb.height() - ed:          # 不在下边缘热区
            return None
        if x <= ed * 2:
            return "bl"
        if x >= sb.width() - ed * 2:
            return "br"
        return "b"

    def _recontain(self):
        """窗口被手动拉伸后重新适配：
        - 等比例锁定：窗口 = 图片等比尺寸，图片填满窗口（zoom = 窗口宽/图片宽）；
        - 解锁：**窗口大小与图片大小解耦**——图片保持当前显示尺寸不变，
          窗口只是视口：图片大于窗口时滚动条自动出现（中键拖动/滚轮查看），
          图片小于窗口时居中（AlignCenter）。
        以图片逻辑尺寸为缩放基准，几何缩放连续。"""
        lw, lh = self._img_lw, self._img_lh
        if lw <= 0 or lh <= 0:
            return
        self._base_w = int(math.ceil(lw))
        self._base_h = int(math.ceil(lh))
        if self._ratio_lock:
            self._zoom = max(0.01, self.width() / lw)
        # 解锁：不动 zoom/img_zoom（图片尺寸固定，仅窗口为视口）
        self._apply_transform()
