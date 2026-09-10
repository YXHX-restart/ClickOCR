"""ui.components.navigator — 图片查看导航图（缩略小地图）。

显示整图缩略 + 当前视口高亮矩形；点击/拖动跳转主视图，
松手后归还主窗口焦点（保证 Esc 等快捷键继续生效）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QRectF, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QPixmap
from PySide6.QtWidgets import QWidget

MAX_W = 180
MAX_H = 260


class Navigator(QWidget):
    """导航小地图（独立顶层小窗，无边框）。"""

    jumped = Signal(float, float)      # 跳转到场景坐标 (x, y)
    interacted = Signal()              # 交互结束（宿主还焦主窗口）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self._img: QPixmap | None = None
        self._scale = 1.0              # 缩略图像素 → 场景像素 比例
        self._view = QRectF()
        self._pressing = False

    def set_image(self, pm: QPixmap):
        if pm.isNull():
            return
        w, h = pm.width(), pm.height()
        s = min(1.0, MAX_W / w, MAX_H / h)
        self._img = pm.scaled(max(1, int(w * s)), max(1, int(h * s)),
                              Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._scale = w / self._img.width()
        self.setFixedSize(self._img.size())
        self.update()

    def set_view(self, view: QRectF):
        """当前可见场景区域（场景坐标）。"""
        self._view = view
        self.update()

    def paintEvent(self, _e):
        if self._img is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.drawPixmap(0, 0, self._img)
        # 视口区域（缩略图坐标，夹紧在窗口内）
        v = QRectF(self._view.x() / self._scale,
                   self._view.y() / self._scale,
                   self._view.width() / self._scale,
                   self._view.height() / self._scale)
        vr = v.intersected(QRectF(self.rect())).toAlignedRect()
        # 整体压暗 → 视口区域重画清晰
        p.fillRect(self.rect(), QColor(0, 0, 0, 120))
        if vr.isValid():
            p.drawPixmap(vr, self._img, vr)
        p.setPen(QPen(QColor(100, 190, 255), 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawRect(v)
        p.end()

    # ---- 交互：点击/拖动跳转 ----
    def _jump(self, pos: QPointF):
        if self._img is None:
            return
        x = min(max(pos.x(), 0.0), self.width() - 1.0)
        y = min(max(pos.y(), 0.0), self.height() - 1.0)
        self.jumped.emit(x * self._scale, y * self._scale)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._pressing = True
            self._jump(e.position())

    def mouseMoveEvent(self, e):
        if self._pressing:
            self._jump(e.position())

    def mouseReleaseEvent(self, e):
        self._pressing = False
        self.interacted.emit()
