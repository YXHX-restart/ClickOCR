"""ui.components.status_strip — 纯信息状态栏（与统一工具栏分离）。

- 只放信息：状态文字（操作提示）+ 选择预览，**无任何按钮**（操作全部在
  统一工具栏 ui/toolbar.py）。
- **独立可拉伸**：右下角手柄拖拽调整宽度（340~1400，记忆用户宽度）。
- **链锁跟随开关**：锁定（默认）= 随图片窗口移动；解锁 = 独立拖动、不联动。
- **关闭**：按 Esc 关闭（不设 ✕ 关闭按钮）；标注模式下 Esc 先退出标注。
- **全局开关**：软件设置里可整体关闭状态栏（[result] strip_enabled）。
- 样式由全局主题提供（#StatusStrip 已纳入 theme 面板选择器）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QEvent, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QLabel, QPushButton, QHBoxLayout, QWidget

from ui.base.card_window import CardWindow
from ui.base.texts import T


class _ResizeGrip(QWidget):
    """状态栏右下角拉伸手柄：拖拽调整宽度（340~1400，记忆用户宽度）。

    放在布局中（parent 为 card，window() 为 StatusStrip），自绘斜线标记；
    用全局坐标 + 起始宽度计算，避免布局 reparent 影响。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("resize_grip")
        self.setFixedSize(16, 16)
        self.setCursor(Qt.SizeHorCursor)
        self.setToolTip("拖拽调整宽度")
        self.setAttribute(Qt.WA_StyledBackground)
        self._start = None        # (global_x, start_width)

    def paintEvent(self, _e):
        # 竖排三个小圆点（约定俗成的拖动/缩放标识，右侧）
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(140, 140, 150, 170))
        cx = self.width() - 5
        for i in range(3):
            y = 3 + i * 5
            p.drawEllipse(cx - 1.5, y, 3, 3)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            w = self.window()
            self._start = (e.globalPosition().x(), w.width())
            e.accept()

    def mouseMoveEvent(self, e):
        if self._start is not None and (e.buttons() & Qt.LeftButton):
            sx, sw = self._start
            w = self.window()
            new_w = max(w.minimumWidth(),
                        min(sw + e.globalPosition().x() - sx, 1400))
            w.resize(new_w, w.height())
            # 拖动即记忆用户宽度（后续链锁跟随保持，不被图片宽度覆盖）
            if hasattr(w, "_user_width"):
                w._user_width = new_w
            e.accept()

    def mouseReleaseEvent(self, _e):
        self._start = None


def _elide(text: str, max_len: int = 60) -> str:
    """预览超长截断（保留尾部最新内容）。"""
    t = " ".join(str(text).replace("\n", " ").split())
    if not t:
        return "—"
    if len(t) <= max_len:
        return t
    return "…" + t[-(max_len - 1):]


class StatusStrip(CardWindow):
    """纯信息状态栏：状态文字 + 选择预览；可拉伸、链锁跟随、Esc 关闭。"""

    esc_pressed = Signal()                # 状态栏内按 Esc：标注模式先退标注，否则关闭状态栏
    nav_toggled = Signal(bool)            # 导航图开关：缩略小地图跳转
    ratio_toggled = Signal(bool)          # 等比例调节窗口开关（默认锁定）

    def __init__(self, parent=None):
        super().__init__(title=None, with_close=False)
        # 位置完全由 ResultOverlay._sync_status_strip_pos() 决定，关掉 CardWindow
        # 的「首次显示自动居中」。否则跨屏截图时鼠标终点会落在 Qt 逻辑空间的屏间
        # 缝隙里，自动居中因取不到屏而退回**主屏并居中**——表现为
        # 「跨屏框选后图片（连状态栏一起）弹到主屏中间、尺寸不变」。
        self._centered = True
        self.setObjectName("StatusStrip")
        self.setMinimumWidth(240)
        self.setMaximumWidth(1400)   # 状态栏最大宽度限制
        self._overlay = None
        self._drag_offset = None
        self.card_lay.setContentsMargins(10, 6, 10, 6)
        self.card_lay.setSpacing(3)

        # 第 1 行：操作提示 + 链锁开关 + 拉伸手柄
        row = QHBoxLayout()
        row.setSpacing(6)
        self.status = QLabel(T("strip.status_ocr", "识别中…"))
        self.status.setProperty("kind", "text")
        self.status.setWordWrap(False)
        _f = self.status.font()
        _f.setPixelSize(12)          # 信息文字缩小一号（默认 13px）
        self.status.setFont(_f)
        row.addWidget(self.status, 1)
        # 链锁跟随开关：锁定 = 随图片移动；解锁 = 独立拖动。
        # 图标用 SVG 锁链 link（锁定）/ 断链 link-off（解锁）区分两种状态。
        self.btn_lock = QPushButton()
        self.btn_lock.setProperty("kind", "lock")
        self.btn_lock.setCheckable(True)
        self.btn_lock.setChecked(True)     # 默认锁定
        self.btn_lock.setFixedSize(32, 32)
        self.btn_lock.toggled.connect(self._sync_lock_icon)
        self._sync_lock_icon(True)
        row.addWidget(self.btn_lock)
        # 导航图开关：缩略小地图 + 视口框，点击/拖动跳转
        self.btn_nav = QPushButton()
        self.btn_nav.setProperty("kind", "lock")
        self.btn_nav.setCheckable(True)
        self.btn_nav.setFixedSize(32, 32)
        self.btn_nav.setToolTip("导航图：缩略小地图查看/跳转大图位置")
        self.btn_nav.toggled.connect(self.nav_toggled.emit)
        self.btn_nav.toggled.connect(self._sync_nav_icon)
        self._sync_nav_icon(False)
        row.addWidget(self.btn_nav)
        # 等比例调节窗口：锁定（默认）= 窗口与图片同比例；解锁 = 自由比例
        self.btn_ratio = QPushButton()
        self.btn_ratio.setProperty("kind", "lock")
        self.btn_ratio.setCheckable(True)
        self.btn_ratio.setChecked(True)    # 默认锁定
        self.btn_ratio.setFixedSize(32, 32)
        self.btn_ratio.toggled.connect(self.ratio_toggled.emit)
        self.btn_ratio.toggled.connect(self._sync_ratio_icon)
        self._sync_ratio_icon(True)
        row.addWidget(self.btn_ratio)
        self.grip = _ResizeGrip(self)
        row.addWidget(self.grip, 0, Qt.AlignBottom | Qt.AlignRight)
        self.card_lay.addLayout(row)

        # 第 2 行：选择文字实时预览
        self.preview = QLabel(T("strip.preview_ph", "—"))
        self.preview.setProperty("kind", "preview")
        _pf = self.preview.font()
        _pf.setPixelSize(12)         # 信息文字缩小一号（默认 13px）
        self.preview.setFont(_pf)
        self.card_lay.addWidget(self.preview)

        # 拖动（空白区）：锁定态 → 移动图片窗口（图片窗口 moveEvent 会联动本栏）；
        # 解锁态 → 移动状态栏自身。
        self.card.installEventFilter(self)
        self.status.installEventFilter(self)
        self.preview.installEventFilter(self)

        # Esc：窗口级快捷键（子控件有焦点也能触发）→ 宿主统一处理
        from PySide6.QtGui import QShortcut, QKeySequence
        self._sc_esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._sc_esc.setContext(Qt.WindowShortcut)
        self._sc_esc.activated.connect(self.esc_pressed.emit)

        self._user_width = None           # 用户拖拽设定的宽度（None=跟随图片）

        # 主题切换 → 刷新三个 SVG 图标（apply_theme 只清缓存，已挂按钮上的
        # 旧色 QIcon 不会自动替换；弱引用订阅，销毁后自动失效）
        from ui.base.theme import on_theme_changed
        on_theme_changed(self._on_theme_refresh)

    def _on_theme_refresh(self, _theme):
        """主题变更：按当前按钮状态重建链锁/导航图/等比例图标。"""
        self._sync_lock_icon(self.btn_lock.isChecked())
        self._sync_nav_icon(self.btn_nav.isChecked())
        self._sync_ratio_icon(self.btn_ratio.isChecked())

    # ---- 接口 ----
    def attach_overlay(self, overlay):
        self._overlay = overlay

    @property
    def locked(self) -> bool:
        """链锁跟随是否锁定（默认锁定）。"""
        return self.btn_lock.isChecked()

    def _sync_lock_icon(self, locked: bool):
        """锁定态 → 锁链 icon；解锁态 → 断链 icon（SVG 图标随状态切换）。

        颜色对齐 QSS kind='lock'：锁定 = 主题强对比色（深=白/浅=黑），
        解锁 = 灰（深主题 #dddddd / 浅主题 #888888）。
        """
        from ui.base.icon import icon as svg_icon
        from ui.base.theme import current_theme
        dark = current_theme() == "dark"
        if locked:
            color = "#ffffff" if dark else "#000000"
        else:
            color = "#dddddd" if dark else "#888888"
        self.btn_lock.setIcon(
            svg_icon("link" if locked else "link-off", color=color, size=16))
        self.btn_lock.setToolTip(
            "链锁跟随：锁定 = 随图片移动；解锁 = 独立拖动" if locked
            else "链锁跟随：解锁 = 独立拖动；锁定 = 随图片移动")

    def _sync_nav_icon(self, on: bool):
        """导航图开关图标：开 = 主题强对比色；关 = 灰。"""
        from ui.base.icon import icon as svg_icon
        from ui.base.theme import current_theme
        dark = current_theme() == "dark"
        color = ("#ffffff" if dark else "#000000") if on \
            else ("#dddddd" if dark else "#888888")
        self.btn_nav.setIcon(svg_icon("nav", color=color, size=16))
        self.btn_nav.setToolTip(
            "导航图已开：缩略小地图查看/跳转大图位置" if on
            else "导航图已关")

    def _sync_ratio_icon(self, on: bool):
        """等比例按钮图标：锁定 = ratio（强对比色）；解锁 = ratio-off（灰）。"""
        from ui.base.icon import icon as svg_icon
        from ui.base.theme import current_theme
        dark = current_theme() == "dark"
        color = ("#ffffff" if dark else "#000000") if on \
            else ("#dddddd" if dark else "#888888")
        self.btn_ratio.setIcon(
            svg_icon("ratio" if on else "ratio-off", color=color, size=16))
        self.btn_ratio.setToolTip(
            "等比例调节窗口：锁定 = 窗口与图片同比例（默认）· 快捷键 E" if on
            else "等比例调节窗口：解锁 = 窗口可自由调整比例 · 快捷键 E")

    def set_status(self, text: str):
        self.status.setText(text)

    def set_preview(self, text: str):
        self.preview.setText(_elide(text))

    # ---- 拖动（锁定 = 拖状态栏移动图片；解锁 = 拖状态栏自身）----
    def eventFilter(self, obj, e):
        if obj is self.btn_lock or obj is self.btn_ratio or obj is self.grip:
            return False                   # 开关/手柄自理
        if e.type() == QEvent.MouseButtonPress and e.button() == Qt.LeftButton:
            if self.locked and self._overlay is not None:
                # 锁定：锚定图片窗口（与 move 时 target 一致，避免偏移跳变）
                target = self._overlay
                self._drag_offset = (e.globalPosition().toPoint()
                                     - target.frameGeometry().topLeft())
            else:
                self._drag_offset = (e.globalPosition().toPoint()
                                     - self.frameGeometry().topLeft())
            return True
        if e.type() == QEvent.MouseMove and self._drag_offset is not None \
                and (e.buttons() & Qt.LeftButton):
            if self.locked and self._overlay is not None:
                self._overlay.move(e.globalPosition().toPoint() - self._drag_offset)
            else:
                self.move(e.globalPosition().toPoint() - self._drag_offset)
            return True
        if e.type() == QEvent.MouseButtonRelease:
            self._drag_offset = None
        return False
