"""ui/theme_dialog.py — 主题化消息/确认弹窗（替代系统原生 QMessageBox）。

- 复用 CardWindow 浮层基类 + 全局主题（#ThemeDialog 已纳入 theme 面板选择器）。
- 三种风格：info（ℹ 蓝）/ question（？橙，是/否）/ warning（⚠ 红）。
- 模态执行（QEventLoop），question 返回 bool；Esc 关闭等同「否」。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QEventLoop
from PySide6.QtWidgets import QLabel, QPushButton, QHBoxLayout

from ui.base.card_window import CardWindow

_KIND = {
    "info": ("ℹ", "#5b8cff"),
    "question": ("？", "#e8a33d"),
    "warning": ("⚠", "#e05b5b"),
}


class ThemeDialog(CardWindow):
    """主题化弹窗。静态工厂：info / question / warning。"""

    closed = Signal()          # 关闭（含 Esc）时发射，供模态循环退出
    result = Signal(str)

    def __init__(self, title: str, text: str, kind: str = "info",
                 buttons: tuple[str, ...] = ("确定",)):
        super().__init__(title=title)
        self._result = ""
        icon, color = _KIND.get(kind, _KIND["info"])

        row = QHBoxLayout()
        row.setSpacing(10)
        icon_lab = QLabel(icon)
        icon_lab.setStyleSheet(f"color: {color}; font-size: 22px;")
        icon_lab.setFixedWidth(28)
        msg = QLabel(text)
        msg.setWordWrap(True)
        msg.setProperty("kind", "text")
        msg.setMinimumWidth(320)
        row.addWidget(icon_lab, 0, Qt.AlignTop)
        row.addWidget(msg, 1)
        self.card_lay.addLayout(row)

        btns = QHBoxLayout()
        btns.addStretch(1)
        for label in buttons:
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, lab=label: self._done(lab))
            btns.addWidget(b)
        self.card_lay.addLayout(btns)

    def _done(self, label: str):
        self._result = label
        self.result.emit(label)
        self.close()

    def closeEvent(self, e):
        self.closed.emit()
        super().closeEvent(e)

    def show_modal(self) -> str:
        """模态显示（QEventLoop 阻塞），返回点击的按钮文字（Esc = ""）。"""
        self._loop = QEventLoop(self)
        self.closed.connect(self._loop.quit)
        self.setWindowModality(Qt.ApplicationModal)
        self.show()
        self._loop.exec()
        return self._result

    # ---- 静态工厂 ----
    @classmethod
    def info(cls, parent=None, title: str = "提示", text: str = ""):
        cls(title, text, "info", ("确定",)).show_modal()

    @classmethod
    def question(cls, parent=None, title: str = "确认", text: str = "") -> bool:
        d = cls(title, text, "question", ("是", "否"))
        return d.show_modal() == "是"

    @classmethod
    def warning(cls, parent=None, title: str = "警告", text: str = ""):
        cls(title, text, "warning", ("确定",)).show_modal()
