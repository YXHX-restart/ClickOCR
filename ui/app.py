"""ClickOCR 实时测试窗口（M1·β 雏形）。

功能：
- 常驻置顶小窗，点「截图识别」抓取全屏 -> RapidOCR -> 画框预览 + 文字列表。
- 列表项单击：复制该文字到剪贴板。
- 热键 Ctrl+Shift+R 同样触发（pynput 全局钩子，后台线程）。
- 仅 M1 测试用，不含托盘 / 区域框选 / 贴图（后续里程碑）。

运行：python -m ui.app
"""
from __future__ import annotations

import sys
import time

import cv2
import numpy as np
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QImage, QPixmap, QClipboard
from PySide6.QtWidgets import (
    QApplication, QWidget, QPushButton, QListWidget, QListWidgetItem,
    QLabel, QVBoxLayout, QHBoxLayout, QMessageBox,
)

from ocr import RapidOcrEngine
from screenshot import ScreenshotService


class HotkeyThread(QThread):
    """全局热键监听（Ctrl+Shift+R），触发时通过信号回到主线程。"""
    triggered = Signal()

    def run(self):
        from pynput import keyboard

        combo = {keyboard.Key.ctrl, keyboard.Key.shift}
        target = keyboard.KeyCode.from_char("r")
        pressed: set = set()

        def on_press(k):
            if k in combo:
                pressed.add(k)
            if k == target and combo.issubset(pressed):
                self.triggered.emit()

        def on_release(k):
            pressed.discard(k)

        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            listener.join()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ClickOCR 测试 (M1·β)")
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.resize(520, 720)

        self.engine = RapidOcrEngine()          # 打开即加载（keep-alive）
        self.svc = ScreenshotService()

        # --- 控件 ---
        self.btn = QPushButton("截图识别  (Ctrl+Shift+R)")
        self.btn.clicked.connect(self.do_capture)

        self.status = QLabel("引擎就绪，点击按钮或按热键测试")
        self.preview = QLabel()
        self.preview.setMinimumHeight(280)
        self.preview.setStyleSheet("border:1px solid #888; background:#222;")
        self.preview.setAlignment(Qt.AlignCenter)

        self.list = QListWidget()
        self.list.itemClicked.connect(self.on_item_clicked)

        # --- 布局 ---
        top = QHBoxLayout()
        top.addWidget(self.btn)
        top.addStretch(1)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.status)
        lay.addWidget(self.preview)
        lay.addWidget(QLabel("识别结果（单击复制）："))
        lay.addWidget(self.list, 1)

        # --- 热键 ---
        self.hk = HotkeyThread()
        self.hk.triggered.connect(self.do_capture)
        self.hk.start()

    # ---- 主线程捕获 ----
    def do_capture(self):
        self.status.setText("截图中 ...")
        QApplication.processEvents()
        try:
            t0 = time.time()
            img = self.svc.grab_full()
            boxes = self.engine.recognize(img)
            cost = time.time() - t0

            marked = self._draw_boxes(img, boxes)
            self._show_preview(marked)

            self.list.clear()
            for b in boxes:
                item = QListWidgetItem(f"{b.text}   [{b.score:.2f}]")
                item.setData(Qt.UserRole, b.text)
                item.setForeground(Qt.white)
                self.list.addItem(item)

            self.status.setText(
                f"识别到 {len(boxes)} 个文字块 · 耗时 {cost:.2f}s"
            )
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"出错：{e}")
            QMessageBox.critical(self, "ClickOCR", f"捕获失败：\n{e}")

    def on_item_clicked(self, item: QListWidgetItem):
        text = item.data(Qt.UserRole)
        QApplication.clipboard().setText(text)
        self.status.setText(f"已复制：{text[:40]}")

    # ---- 辅助 ----
    @staticmethod
    def _draw_boxes(img: np.ndarray, boxes) -> np.ndarray:
        canvas = img.copy()
        for b in boxes:
            pts = np.array(b.polygon, dtype=np.int32).reshape(-1, 1, 2)
            cv2.polylines(canvas, [pts], True, (0, 200, 255), 2)
        return canvas

    def _show_preview(self, bgr: np.ndarray):
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        max_w = 480
        if w > max_w:
            scale = max_w / w
            rgb = cv2.resize(rgb, (max_w, int(h * scale)))
        qimg = QImage(rgb.data, rgb.shape[1], rgb.shape[0],
                     rgb.strides[0], QImage.Format_RGB888)
        self.preview.setPixmap(QPixmap.fromImage(qimg))


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
