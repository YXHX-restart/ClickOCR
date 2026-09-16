"""ui.components.pin_dot — 钉图钉子 ⊛（钉图态唯一保留的浮层）。

钉图时：图片窗口 + 状态栏 + 统一工具栏一起隐藏，只留本钉子（桌面上的
独立小圆窗）。交互：
- 单击：取消钉图（恢复图片 + 状态栏 + 工具栏）。
- 双击：收起图片（钉子留在桌面）/ 图片收起时双击恢复。
- 拖动：带动图片窗口同步移动（恢复时不飞）。
- 悬停：图片收起时显示信息卡片（缩略图 + OCR 文字，复用 ui.components.pin_card）。
样式固定深色半透明（白图上也可见），不随明暗主题。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QEvent, Signal, QTimer
from PySide6.QtGui import QPainter, QColor, QFont
from PySide6.QtWidgets import QLabel, QApplication

from ui.base.texts import T


class PinDot(QLabel):
    """圆形钉子 ⊛：无边框顶层小窗，交互信号全部转发宿主。"""

    clicked = Signal()                  # 单击：取消钉图（恢复全部浮层）
    double_clicked = Signal()           # 双击：收起/恢复图片
    drag = Signal(object)               # 拖动：增量位移 QPoint（带动图片）
    hover_in = Signal()                 # 悬停进入（图片收起时显示信息卡片）
    hover_out = Signal()                # 悬停移出

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PinDot")
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(32, 32)
        self.setAlignment(Qt.AlignCenter)
        from ui.base.icon import icon_pixmap
        self.setPixmap(icon_pixmap("pin-dot", color="#ffd54f", size=20))
        self.setToolTip(
            T("ui.pin_dot_tip", "单击：取消钉图 · 双击：收起/恢复图片 · 拖动：移动"))
        self.setStyleSheet(
            "QLabel { background: rgba(30,30,32,190); color: #ffd54f;"
            " border: 1px solid rgba(255,255,255,60); border-radius: 16px;"
            " font-size: 22px; }")

        self._press = None          # 按下全局坐标
        self._last = None
        self._dragging = False
        self._pending = False       # 单击延迟判定
        self._suppress = False      # 双击后抑制 release 单击
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(QApplication.doubleClickInterval())
        self._timer.timeout.connect(self._do_click)

    # ---- 交互 ----
    def _do_click(self):
        if self._pending:
            self._pending = False
            self.clicked.emit()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint()
            self._last = self._press
            self._dragging = False
            self._pending = False
            self._suppress = False
            self._timer.stop()
            e.accept()

    def mouseMoveEvent(self, e):
        if (e.buttons() & Qt.LeftButton) and self._press is not None:
            cur = e.globalPosition().toPoint()
            if not self._dragging:
                if (cur - self._press).manhattanLength() > 6:
                    self._dragging = True
                    self._timer.stop()
                    self._pending = False
            if self._dragging:
                delta = cur - self._last
                self._last = cur
                if not delta.isNull():
                    self.move(self.x() + delta.x(), self.y() + delta.y())
                    self.drag.emit(delta)
                e.accept()
                return
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            if self._suppress:             # 双击后的 release：抑制单击
                self._suppress = False
                self._press = None
                e.accept()
                return
            if self._dragging:             # 拖动结束：不触发单击
                self._dragging = False
                self._press = None
                e.accept()
                return
            self._press = None
            self._pending = True
            self._timer.start()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        self._timer.stop()
        self._pending = False
        self._suppress = True
        self.double_clicked.emit()
        e.accept()

    def enterEvent(self, e):
        self.hover_in.emit()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.hover_out.emit()
        super().leaveEvent(e)

    def paintEvent(self, e):
        # 顺序要紧：QSS 的圆底由 super()（QPushButton）绘制，必须在它**之后**补描边。
        # 原先先自绘椭圆、再调 super() —— 背景后画，把描边整圈盖掉，
        # 注释宣称的「描边双保险」实际一点都看不见。
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = p.pen()
        pen.setWidthF(1.0)
        pen.setColor(QColor(255, 255, 255, 40))
        p.setPen(pen)
        p.drawEllipse(1, 1, self.width() - 3, self.height() - 3)
        p.end()
