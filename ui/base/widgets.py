"""ui.base.widgets — 主题化小控件。

- `ThemeSpinBox`：数值调整（去原生箭头 + ▲▼ 主题按钮）。
- `ColorButton`：色块按钮，点击弹出**主题化 ColorPickerDialog**（非系统原生
  QColorDialog），色块即当前颜色。
- `ColorPickerDialog`：自定义颜色选择（CardWindow 浮层，跟随明暗主题）——
  预设色板网格 + 十六进制输入 + 实时预览。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QSize, QRect
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QGridLayout,
                               QSpinBox, QPushButton, QAbstractSpinBox,
                               QLineEdit, QComboBox, QApplication)

from ui.base.card_window import CardWindow


def screen_for_point(point) -> object:
    """取包含该点的屏幕；点落在屏幕之间的「逻辑缝隙」里时取**最近的**屏。

    为什么不能「取不到就退回主屏」：
    Qt 的逻辑虚拟桌面在**不同分辨率 + 相同缩放**的机器上会出现**不属于任何屏**
    的缝隙。实测布局（dlg 1.5）：主屏逻辑 (0,0,2560,1440)、副屏逻辑
    (3840,720,1707,960) —— 主屏逻辑宽度只有 2560，副屏逻辑原点却是 3840，
    于是 **2560~3840 无人认领**（物理上两屏是相邻的，这纯粹是 Qt 缩放逻辑的
    产物）。`QGuiApplication.screenAt()` 对缝里的点返回 None。

    而跨屏框选时，鼠标终点与窗口中心恰好会落进这条缝 —— 退回主屏就会把窗口
    从副屏硬拽到主屏（表现为「图片弹回主屏并居中 / 尺寸不变」）。
    按距离取最近的屏，行为才是连续的。

    返回 QScreen，取不到任何屏时返回 None。
    """
    if point is not None:
        try:
            scr = QApplication.screenAt(point)
        except Exception:                # noqa: BLE001
            scr = None
        if scr is not None:
            return scr
    try:
        screens = [s for s in QApplication.screens() if s is not None]
    except Exception:                    # noqa: BLE001
        screens = []
    if not screens:
        return QApplication.primaryScreen()
    if point is None:
        return QApplication.primaryScreen() or screens[0]
    # 到各屏矩形的最短边距（比较平方，避免开方）；相等时保留先出现的
    best, best_d = screens[0], None
    for s in screens:
        r = s.geometry()
        dx = max(r.left() - point.x(), 0, point.x() - r.right())
        dy = max(r.top() - point.y(), 0, point.y() - r.bottom())
        d = dx * dx + dy * dy
        if best_d is None or d < best_d:
            best, best_d = s, d
    return best


def desktop_union_available() -> QRect:
    """所有屏可用区域的并集（整个虚拟桌面）。取不到屏时返回空 QRect。

    用途：给「跟随主窗、可能横跨多屏」的附属浮窗做夹取。
    这类浮窗不能按**单块屏**夹取——跨屏截图时，主窗横跨两屏，而按单屏夹取会把
    附属浮窗整个拽到那一块屏上（实测：状态栏/工具栏被拉回主屏）。
    夹到并集既保证不出屏，又不会跨屏拽动。

    注意：**不要**用 QRect() 起手。QRect() 是 (0,0,-1,-1)，它不是 isNull()
    （宽高非 0）而是 isEmpty()，united() 会保留其原点，导致并集原点被错误拉成
    (0,0)（副屏挂在左侧/上方时就会算错）。
    """
    try:
        screens = [s for s in QApplication.screens() if s is not None]
    except Exception:                    # noqa: BLE001
        screens = []
    if not screens:
        return QRect()
    u = QRect(screens[0].availableGeometry())
    for s in screens[1:]:
        u = u.united(s.availableGeometry())
    return u


def screen_available_geometry(widget) -> QRect:
    """widget 当前所在屏幕的可用区域（多显示器下切勿用 primaryScreen 夹取）。

    副屏窗口按主屏 availableGeometry 夹取会被强行拉回主屏边界，
    甚至算出负宽度（`scr.right() - x + 1` 为负）。取不到时按**最近屏**判定，
    再不济才回退主屏；两者都取不到返回空 QRect，调用方需自行判空。
    """
    scr = None
    try:
        scr = widget.screen()            # Qt 6.4+：控件所在屏
    except Exception:                    # noqa: BLE001
        scr = None
    if scr is None:
        try:
            scr = screen_for_point(widget.frameGeometry().center())
        except Exception:                # noqa: BLE001
            scr = None
    if scr is None:
        scr = QApplication.primaryScreen()
    if scr is None:
        return QRect()
    return scr.availableGeometry()


class FitComboBox(QComboBox):
    """下拉框：限制最小/首选宽度，避免长选项文字把整行撑宽、触发横滚动条。

    默认下拉框 sizeHint/minimumSizeHint 取「最宽选项」宽度，当某选项文字很长
    （如 GPU 设备名带倍率说明）时，会强制表单字段列变宽，在固定宽度的设置窗里
    挤掉横向空间出现滚动条。这里把宽度上限钳在 ``max_w``，超长文字在框内以
    省略号展示、下拉列表仍显示完整内容。
    """

    def __init__(self, max_w: int = 220, parent: QWidget | None = None):
        super().__init__(parent)
        self._max_w = max_w

    def minimumSizeHint(self) -> QSize:
        h = super().minimumSizeHint()
        return QSize(min(h.width(), self._max_w), h.height())

    def sizeHint(self) -> QSize:
        h = super().sizeHint()
        return QSize(min(h.width(), self._max_w), h.height())

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
        # 锚定在按钮上方居中（空间不足则放按钮下方）；取色器随按钮所在屏
        # 夹取——原先固定主屏会让副屏上的取色器跳到主屏边缘
        scr = screen_available_geometry(self)
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
                 value: int = 1, width: int = 64,
                 align: Qt.AlignmentFlag = Qt.AlignCenter):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)

        self.spin = QSpinBox()
        self.spin.setRange(min_value, max_value)
        self.spin.setValue(value)
        self.spin.setButtonSymbols(QAbstractSpinBox.NoButtons)  # 去掉原生箭头
        self.spin.setAlignment(align)
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
