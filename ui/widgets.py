"""ui.widgets — 主题化小控件。

- `ThemeSpinBox`：数值调整（去原生箭头 + ▲▼ 主题按钮）。
- `ColorButton`：色块按钮，点击弹出**主题化 ColorPickerDialog**（非系统原生
  QColorDialog），色块即当前颜色。
- `ColorPickerDialog`：自定义颜色选择（CardWindow 浮层，跟随明暗主题）——
  预设色板网格 + 十六进制输入 + 实时预览。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QGridLayout,
                               QSpinBox, QPushButton, QAbstractSpinBox,
                               QLineEdit)

from ui.card_window import CardWindow

# 预设色板（24 色：红/橙/黄/绿/青/蓝/紫/粉/棕/灰/黑白）
_PALETTE = [
    (255, 95, 95), (255, 145, 64), (255, 205, 64), (255, 235, 59),
    (139, 195, 74), (76, 175, 80), (38, 166, 154), (0, 188, 212),
    (66, 165, 245), (63, 81, 181), (156, 39, 176), (233, 30, 99),
    (244, 67, 54), (255, 152, 0), (121, 85, 72), (158, 158, 158),
    (96, 125, 139), (33, 33, 33), (255, 255, 255), (250, 250, 250),
    (245, 245, 245), (0, 0, 0), (100, 190, 255), (255, 87, 34),
]


class ColorPickerDialog(CardWindow):
    """主题化颜色选择浮层（替代系统 QColorDialog）。"""

    picked = Signal(tuple)   # 每次选色即时发出

    def __init__(self, initial: tuple = (255, 95, 95), parent=None):
        super().__init__(title="选择颜色", with_close=True)
        self.setMinimumWidth(360)
        self._color = tuple(int(c) for c in initial)
        self.card_lay.setSpacing(8)

        # ---- 当前色预览 + 十六进制输入 ----
        prev = QHBoxLayout()
        prev.setSpacing(6)
        self._preview = QPushButton()
        self._preview.setFixedSize(52, 28)
        self._preview.setCursor(Qt.PointingHandCursor)
        self._preview.setToolTip("点击色板或输入色值")
        self._hex = QLineEdit()
        self._hex.setMaximumWidth(120)
        self._hex.setPlaceholderText("#RRGGBB")
        self._hex.returnPressed.connect(self._apply_hex)
        prev.addWidget(self._preview)
        prev.addWidget(self._hex)
        prev.addStretch(1)
        self.card_lay.addLayout(prev)

        # ---- 预设色板网格 ----
        grid = QGridLayout()
        grid.setSpacing(5)
        for i, c in enumerate(_PALETTE):
            b = QPushButton()
            b.setFixedSize(32, 32)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(
                "QPushButton { background: rgb(%d,%d,%d); border: 1px solid"
                " rgba(255,255,255,70); border-radius: 6px; }"
                "QPushButton:hover { border: 2px solid #8ab4ff; }" % c)
            b.clicked.connect(lambda _=False, cc=c: self._set(cc))
            grid.addWidget(b, i // 6, i % 6)
        self.card_lay.addLayout(grid)

        # ---- 确定 / 取消 ----
        row = QHBoxLayout()
        b_ok = QPushButton("确定")
        b_cancel = QPushButton("取消")
        b_ok.clicked.connect(self.close)
        b_cancel.clicked.connect(self.close)
        row.addStretch(1)
        row.addWidget(b_ok)
        row.addWidget(b_cancel)
        self.card_lay.addLayout(row)

        self._refresh()

    # ---- 内部 ----
    def _set(self, c: tuple):
        self._color = tuple(int(x) for x in c)
        self._refresh()
        self.picked.emit(self._color)

    def _apply_hex(self):
        h = self._hex.text().strip().lstrip("#")
        if len(h) == 6:
            try:
                self._set(tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)))
            except ValueError:
                pass

    def _refresh(self):
        r, g, b = self._color
        self._preview.setStyleSheet(
            "QPushButton { background: rgb(%d,%d,%d); border: 1px solid"
            " rgba(255,255,255,80); border-radius: 5px; }" % (r, g, b))
        self._hex.setText("#%02X%02X%02X" % (r, g, b))

    @property
    def color(self) -> tuple:
        return self._color


class ColorButton(QPushButton):
    """色块按钮：点击弹出主题化 ColorPickerDialog（非系统原生），色块即所选颜色。"""

    colorChanged = Signal(tuple)   # 颜色变更（取色器选择 / 代码设置）

    def __init__(self, color: tuple = (100, 190, 255)):
        super().__init__()
        self.setFixedSize(30, 24)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点击选择颜色")
        self._picker = None   # 颜色选择浮层（复用单实例，再点击关闭）
        self.set_color(tuple(color))

    def set_color(self, color: tuple):
        self._color = tuple(int(c) for c in color)
        r, g, b = self._color
        self.setStyleSheet(
            "QPushButton { background: rgb(%d,%d,%d); border: 1px solid"
            " rgba(255,255,255,90); border-radius: 4px; }"
            "QPushButton:hover { border: 2px solid #8ab4ff; }" % (r, g, b))
        self.colorChanged.emit(self._color)

    @property
    def color(self) -> tuple:
        return self._color

    def _pick(self):
        # 再点击关闭：若已打开则收起，避免累积多个窗体
        if getattr(self, "_picker", None) is not None and self._picker.isVisible():
            self._picker.close()
            return
        if self._picker is None:
            self._picker = ColorPickerDialog(self._color, self)
            self._picker.picked.connect(self.set_color)
        self._picker._refresh()          # 同步按钮当前色
        self._picker.show()
        self._picker.raise_()
        self._picker._centered = True    # 阻止 CardWindow 默认居中
        # 锚定在按钮上方居中（空间不足则放按钮下方）
        from PySide6.QtWidgets import QApplication
        scr = QApplication.primaryScreen().availableGeometry()
        gw = self._picker.width()
        gh = self._picker.height()
        g = self.mapToGlobal(self.rect().topLeft())
        x = g.x() + (self.width() - gw) // 2
        y = g.y() - gh - 8
        if y < scr.top():
            y = g.y() + self.height() + 8
        x = max(scr.left(), min(x, scr.right() - gw))
        y = max(scr.top(), min(y, scr.bottom() - gh))
        self._picker.move(x, y)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._pick()
        else:
            super().mouseReleaseEvent(e)


class ThemeSpinBox(QWidget):
    """符合卡片 UI 的数值调整控件：输入框 + ▲▼ 主题化步进按钮。"""

    def __init__(self, min_value: int = 1, max_value: int = 999,
                 value: int = 1, width: int = 64):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)

        self.spin = QSpinBox()
        self.spin.setRange(min_value, max_value)
        self.spin.setValue(value)
        self.spin.setButtonSymbols(QAbstractSpinBox.NoButtons)  # 去掉原生箭头
        self.spin.setAlignment(Qt.AlignCenter)
        self.spin.setMinimumWidth(width)

        btn_col = QVBoxLayout()
        btn_col.setContentsMargins(0, 0, 0, 0)
        btn_col.setSpacing(2)
        self.b_up = QPushButton("▲")
        self.b_down = QPushButton("▼")
        for b in (self.b_up, self.b_down):
            b.setProperty("kind", "spinner")
            b.setFixedSize(24, 14)
            b.setCursor(Qt.PointingHandCursor)
        self.b_up.setToolTip("增加")
        self.b_down.setToolTip("减少")
        self.b_up.clicked.connect(self.spin.stepUp)
        self.b_down.clicked.connect(self.spin.stepDown)
        btn_col.addWidget(self.b_up)
        btn_col.addWidget(self.b_down)

        lay.addWidget(self.spin, 1)
        lay.addLayout(btn_col)

    # ---- 与 QSpinBox 兼容的接口 ----
    def value(self) -> int:
        return self.spin.value()

    def setValue(self, v: int):
        self.spin.setValue(v)

    def setRange(self, a: int, b: int):
        self.spin.setRange(a, b)

    @property
    def maximum(self) -> int:
        return self.spin.maximum()


_ColorButton = ColorButton   # 旧名兼容（settings_panel / annotation 曾用 _ColorButton）
