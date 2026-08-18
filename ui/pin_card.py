"""ui.pin_card — 钉图悬停信息卡片（缩略图 + 信息）。

图片收进钉子后，光标悬停钉子显示此卡片：缩略图 + OCR 文字预览 + 尺寸/记录信息。
类似历史记录条目。
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QFrame)


class PinCard(QWidget):
    """钉图信息卡片：缩略图 + 信息（无边框半透明卡片，置顶）。"""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("PinCard")

        card = QFrame(self)
        card.setObjectName("card")
        card.setAttribute(Qt.WA_StyledBackground)   # QFrame 需此属性绘制 QSS 背景
        lay = QHBoxLayout(card)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(10)

        self.thumb = QLabel()
        self.thumb.setFixedSize(120, 84)
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setProperty("kind", "thumb")
        lay.addWidget(self.thumb)

        mid = QVBoxLayout()
        mid.setSpacing(4)
        self.text = QLabel("")
        self.text.setProperty("kind", "text")
        self.text.setWordWrap(True)
        self.text.setMaximumWidth(240)
        self.info = QLabel("")
        self.info.setProperty("kind", "info")
        mid.addWidget(self.text)
        mid.addWidget(self.info)
        lay.addLayout(mid)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(card)

    def set_image(self, img_bgr: np.ndarray):
        """设置缩略图（BGR ndarray）。"""
        try:
            import cv2
            h, w = img_bgr.shape[:2]
            tw = 120
            th = max(1, int(round(h * tw / w)))
            thumb = cv2.resize(img_bgr, (tw, th), interpolation=cv2.INTER_AREA)
            rgb = cv2.cvtColor(thumb, cv2.COLOR_BGR2RGB)
            qimg = QImage(rgb.data, rgb.shape[1], rgb.shape[0],
                          rgb.strides[0], QImage.Format_RGB888)
            pm = QPixmap.fromImage(qimg)
            self.thumb.setPixmap(pm.scaled(
                self.thumb.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        except Exception:
            self.thumb.setText("无图")

    def set_info(self, text: str, info: str):
        """设置 OCR 文字预览和尺寸/记录信息。"""
        t = (text or "(无文字)").replace("\n", " ⏎ ")
        if len(t) > 60:
            t = t[:60] + "…"
        self.text.setText(t)
        self.info.setText(info)

    def show_near(self, global_pt) -> None:
        """定位到指定全局位置（卡片右下角对齐钉子左上）。"""
        self.adjustSize()
        x = global_pt.x() - self.width() - 6
        y = global_pt.y() - self.height() + 6
        self.move(max(0, x), max(0, y))
