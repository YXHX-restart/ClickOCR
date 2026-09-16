"""ui.base.card_window — 工具面板统一浮层基类。

所有工具面板（设置 / 历史 / 快捷键）共用同一套「无边框半透明 + 深/浅色
圆角卡片」外观，避免各面板样式代码漂移：

- `CardWindow(QWidget)`：窗口 flags（Frameless + 置顶 + Tool）、
  `WA_TranslucentBackground`、objectName="card" 的圆角卡片容器、
  可拖动标题栏（DragBar）、首次显示居中、Esc 关闭。
- 卡片背景 / 圆角 / 文字颜色**全部由 ui.base.theme 的全局 QSS 按 #card
  统一提供**，明暗主题切换时三个面板同步换肤。
- 子类把内容放进 `self.card_lay` 即可；需要自绘事件的子类可覆写
  mousePressEvent / keyPressEvent 等。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRect
from PySide6.QtWidgets import (QWidget, QFrame, QVBoxLayout, QHBoxLayout,
                               QLabel, QPushButton, QApplication)

from ui.base.texts import T


class DragBar(QWidget):
    """可拖动标题栏（无边框窗口的移动把手，三面板共用）。

    必须用子类覆写鼠标事件——PySide6 里给实例动态赋值同名方法不会生效
    （Qt 事件走 C++ 虚函数表分发）。
    """

    def __init__(self, title: str, with_close: bool = True):
        super().__init__()
        self._offset = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(8)
        t = QLabel(title)
        t.setProperty("kind", "title")
        lay.addWidget(t)
        lay.addStretch(1)
        self._close = None
        if with_close:
            from ui.base.icon import icon as svg_icon
            from ui.base.theme import icon_color
            self._close = QPushButton()
            self._close.setIcon(svg_icon("close", color=icon_color(), size=12))
            self._close.setProperty("kind", "close")
            self._close.setToolTip(T("ui.close_tip", "关闭 (Esc)"))
            # 点击时才取 window()：构造期间本控件尚未挂入窗口层级，
            # 此时 self.window() 返回的是自己而非宿主窗口。
            self._close.clicked.connect(lambda: self.window().close())
            lay.addWidget(self._close)
            # 主题切换 → 刷新关闭按钮图标（弱引用订阅，随宿主销毁自动失效）
            from ui.base.theme import on_theme_changed
            on_theme_changed(self._refresh_close_icon)

    def _refresh_close_icon(self, _theme=None):
        """主题变更：按新主题色重建关闭按钮图标。"""
        if self._close is not None:
            from ui.base.icon import icon as svg_icon
            from ui.base.theme import icon_color
            self._close.setIcon(svg_icon("close", color=icon_color(), size=12))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._offset = (e.globalPosition().toPoint()
                            - self.window().frameGeometry().topLeft())

    def mouseMoveEvent(self, e):
        if self._offset is not None and (e.buttons() & Qt.LeftButton):
            self.window().move(e.globalPosition().toPoint() - self._offset)

    def mouseReleaseEvent(self, _e):
        self._offset = None


class CardWindow(QWidget):
    """无边框半透明浮层基类：card 容器 + 可拖动标题栏 + 首次居中 + Esc 关闭。"""

    def __init__(self, title: str | None = None, with_close: bool = True):
        super().__init__()
        # 用类名作为 objectName：主题 QSS 以 #类名 ID 选择器匹配，
        # 比 QSS 类型选择器（Python 子类在全局样式表可能不匹配）更可靠。
        self.setObjectName(type(self).__name__)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.card = QFrame()
        self.card.setObjectName("card")   # 背景/圆角由全局主题提供
        # 关键：QFrame 默认不绘制样式表 background，必须开 WA_StyledBackground
        self.card.setAttribute(Qt.WA_StyledBackground)
        self.card_lay = QVBoxLayout(self.card)
        self.card_lay.setContentsMargins(16, 12, 16, 14)
        root.addWidget(self.card)

        self._dragbar = None
        if title:
            self._dragbar = DragBar(title, with_close)
            self.card_lay.addWidget(self._dragbar)

        self._centered = False

    def showEvent(self, _e):
        if not self._centered:
            # 首次居中到鼠标所在屏（多显示器下不要固定主屏）
            scr = self._target_screen_geometry()
            if scr.isNull():
                scr = QApplication.primaryScreen().availableGeometry()
            self.move(scr.center().x() - self.width() // 2,
                      scr.center().y() - self.height() // 2)
            self._centered = True

    def _target_screen_geometry(self):
        """取鼠标所在屏的可用区域。

        走 `screen_for_point`（**不会因为点落在屏间缝隙里就返回空**）。
        原先返回空 QRect 会让 showEvent 退回主屏居中；而跨屏操作时鼠标终点
        恰好会落进 Qt 逻辑空间的缝隙（主屏逻辑宽 2560、副屏逻辑原点 3840，中间
        2560~3840 不属任何屏），于是窗口被硬拽到主屏中心——表现为
        「跨屏截图后图片弹回主屏、尺寸不变」。返回空 QRect 只保留给
        「真的一屏都没有」的极端情况。
        """
        try:
            from ui.base.widgets import screen_for_point
        except Exception:            # noqa: BLE001  控件层不可用时不阻断
            screen_for_point = None
        try:
            from PySide6.QtGui import QCursor
            scr = (screen_for_point(QCursor.pos()) if screen_for_point is not None
                   else QApplication.screenAt(QCursor.pos()))
            return scr.availableGeometry() if scr is not None else QRect()
        except Exception:            # noqa: BLE001
            return QRect()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
