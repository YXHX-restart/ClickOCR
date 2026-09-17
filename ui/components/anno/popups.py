"""ui.components.anno.popups — 标注工具的浮层/按钮/选择行等 Qt 控件。

从 ui/components/annotation.py 拆出（2026-09-11 重构 S1），纯搬移不改行为。
对外名字由 ui.components.annotation 再导出，调用方无需改动。
"""

from __future__ import annotations


from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (QFont)
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
                               QLabel, QFrame, QSlider, QSpinBox,
                               QButtonGroup, QFontComboBox)

from platform_compat.fonts import ui_font_family

from ui.base.texts import T
from ui.base.widgets import signals_blocked

# 标注核心纯逻辑（命令撤销体系 + 序列化 + 样式合法值唯一来源，无 Qt 依赖）
from ui.core.anno_core import (TOOL_STYLES, NUM_STYLES)


# ---- 命令模式撤销/重做（Command 栈）：命令类见 ui/anno_core.py ----
# 快照式 undo（deepcopy 整个 data 列表）改为「命令 + 数据变更」，省内存：
# 每条命令只存被操作的 item/坐标，undo/redo 走 apply/unapply；
# 单笔绘制（含全部轨迹点）= 一条 AddCmd，连续笔迹天然合并为一步撤回。



# ---- 工具栏 ----
class _ColorSizeRow(QWidget):
    """颜色 + 大小 配置行（各标注工具子选项浮层通用，替代工具栏主行格子）。

    颜色 = 色块按钮（点击弹取色器）；大小 = 水平滑块（1-32，拖动即时生效）。
    信号：color_changed / width_changed。
    """

    color_changed = Signal(tuple)
    width_changed = Signal(int)

    def __init__(self, parent=None, color: tuple = (255, 95, 95), width: int = 2,
                 show_size: bool = True, show_color: bool = True,
                 size_range: tuple = (1, 32)):
        super().__init__(parent)
        self._size_range = (int(size_range[0]), int(size_range[1]))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        if show_color:
            lay.addWidget(QLabel(T("anno.color", "颜色")))
            from ui.base.widgets import ColorButton
            self.color_btn = ColorButton(color)
            self.color_btn.colorChanged.connect(self.color_changed.emit)
            lay.addWidget(self.color_btn)
        else:
            self.color_btn = None
        if show_size:
            lay.addSpacing(4)
            lay.addWidget(QLabel(T("anno.size", "大小")))
            self.slider = QSlider(Qt.Horizontal)
            self.slider.setRange(*self._size_range)
            self.slider.setValue(max(self._size_range[0],
                                     min(self._size_range[1], int(width))))
            self.slider.setFixedWidth(110)
            self.slider.valueChanged.connect(self.width_changed.emit)
            lay.addWidget(self.slider)
            self.lbl = QLabel(str(width))
            self.lbl.setFixedWidth(18)
            self.lbl.setAlignment(Qt.AlignCenter)
            self.slider.valueChanged.connect(lambda v: self.lbl.setText(str(v)))
            lay.addWidget(self.lbl)
        else:
            self.slider = None
            self.lbl = None

    def set_state(self, color: tuple, width: int):
        """打开时回显当前颜色/大小（信号抑制：回显不改状态）。

        `ColorButton.set_color()` **每次都 emit colorChanged**，不挡就会在每次打开
        浮层时向宿主回发一次同值颜色 → 宿主按「用户改色」重渲染一遍（闪动）。
        原先只挡了 slider，color_btn 漏了。
        """
        with signals_blocked(self.color_btn, self.slider):
            if self.color_btn is not None:
                self.color_btn.set_color(tuple(color))
            if self.slider is not None:
                self.slider.setValue(max(self._size_range[0],
                                         min(self._size_range[1], int(width))))
                self.lbl.setText(str(self.slider.value()))


class _AnnoPopupBase(QWidget):
    """标注子选项浮层基类（Qt.Tool 独立顶层窗）。

    焦点落在浮层上时（如刚点过样式按钮/序号输入框），Q/W/E/R/T/Y/U 与 Esc
    转发给宿主贴图窗口统一处理，保证标注快捷键在浮层焦点下也生效。
    """

    def _find_overlay(self):
        w = self.parent()
        while w is not None:
            ov = getattr(w, "_overlay", None)
            if ov is not None:
                return ov
            w = w.parent()
        return None

    def keyPressEvent(self, e):
        ov = self._find_overlay()
        if ov is not None and getattr(ov, "_anno_mode", False) \
                and ov.handle_anno_key(e):
            return
        super().keyPressEvent(e)


class _FontPopup(_AnnoPopupBase):
    """文字属性浮层：点击「文字」工具按钮时在按钮下方弹出，不遮挡工具栏。

    第 1 行：字体/字号/加粗/斜体；第 2 行：颜色/大小（_ColorSizeRow）。
    Qt.Tool（非 Popup）：点击外部保持打开，再次点击/切换功能才关闭。
    """

    size_changed = Signal(int)
    bold_toggled = Signal(bool)
    italic_toggled = Signal(bool)
    color_changed = Signal(tuple)
    width_changed = Signal(int)

    SIZES = ("8", "10", "12", "14", "16", "18", "20", "24", "28", "32", "40", "48", "64")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("FontPopup")
        card = QFrame(self)
        card.setObjectName("font_card")
        card.setAttribute(Qt.WA_StyledBackground)
        # 第 1 行：字体 | 色块(无标签) | 加粗 | 斜体
        lay1 = QHBoxLayout()
        lay1.setSpacing(6)
        lay1.addWidget(QLabel(T("anno.font", "字体")))
        self.font_combo = QFontComboBox()
        self.font_combo.setMaximumWidth(150)
        self.font_combo.setCurrentFont(QFont(ui_font_family()))
        lay1.addWidget(self.font_combo)
        from ui.base.widgets import ColorButton
        self.color_btn = ColorButton((255, 95, 95))
        self.color_btn.colorChanged.connect(self.color_changed.emit)
        lay1.addWidget(self.color_btn)
        self.btn_bold = QPushButton("B")
        self.btn_bold.setCheckable(True)
        self.btn_bold.setFocusPolicy(Qt.NoFocus)
        self.btn_bold.setToolTip(T("anno.bold", "加粗"))
        f = self.btn_bold.font(); f.setBold(True); self.btn_bold.setFont(f)
        self.btn_bold.setFixedWidth(30)
        self.btn_bold.toggled.connect(self.bold_toggled.emit)
        lay1.addWidget(self.btn_bold)
        self.btn_italic = QPushButton("I")
        self.btn_italic.setCheckable(True)
        self.btn_italic.setFocusPolicy(Qt.NoFocus)
        self.btn_italic.setToolTip(T("anno.italic", "斜体"))
        fi = self.btn_italic.font(); fi.setItalic(True); self.btn_italic.setFont(fi)
        self.btn_italic.setFixedWidth(30)
        self.btn_italic.toggled.connect(self.italic_toggled.emit)
        lay1.addWidget(self.btn_italic)
        # 第 2 行：大小滑块（8-64，与其他工具滑块样式一致）
        lay2 = QHBoxLayout()
        lay2.setSpacing(6)
        lay2.addWidget(QLabel(T("anno.size", "大小")))
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(8, 64)
        self.size_slider.setValue(18)
        self.size_slider.setFixedWidth(140)
        self.size_slider.valueChanged.connect(self.size_changed.emit)
        lay2.addWidget(self.size_slider)
        self.size_lbl = QLabel("18")
        self.size_lbl.setFixedWidth(18)
        self.size_lbl.setAlignment(Qt.AlignCenter)
        self.size_slider.valueChanged.connect(
            lambda v: self.size_lbl.setText(str(v)))
        lay2.addWidget(self.size_lbl)
        lay2.addStretch(1)
        root = QVBoxLayout(card)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)
        root.addLayout(lay1)
        root.addLayout(lay2)
        root2 = QVBoxLayout(self)
        root2.setContentsMargins(0, 0, 0, 0)
        root2.addWidget(card)

    def set_state(self, size: int, bold: bool, italic: bool,
                  color: tuple = (255, 95, 95), _width: int = 2):
        """打开时回显当前文字属性 + 颜色（信号抑制：回显不改状态）。

        size_slider / btn_bold / btn_italic / color_btn 四个控件都会在赋值时发信号
        （`setChecked` 发 `toggled`、`set_color` 发 `colorChanged`），原先只挡了
        slider —— 每次打开文字浮层都会向宿主回发一轮同值 bold/italic/color，
        宿主按「用户改了属性」重渲染，表现为浮层一弹出界面就闪一下。
        """
        with signals_blocked(self.size_slider, self.btn_bold, self.btn_italic,
                             self.color_btn):
            self.size_slider.setValue(max(8, min(64, int(size))))
            self.size_lbl.setText(str(self.size_slider.value()))
            self.btn_bold.setChecked(bool(bold))
            self.btn_italic.setChecked(bool(italic))
            self.color_btn.set_color(tuple(color))


class _MosaicModePopup(_AnnoPopupBase):
    """马赛克模式选择浮层：点击马赛克工具时在按钮下方弹出。

    第 1 行：模式（画笔/框选/文字框）；第 2 行：颜色/大小（_ColorSizeRow）。
    Qt.Tool（非 Popup）：点击外部保持打开，再次点击/切换功能才关闭。
    """

    MODES = (("pen", "画笔"), ("rect", "框选"), ("textbox", "文字框"))
    modeSelected = Signal(str)
    color_changed = Signal(tuple)
    width_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("MosaicModePopup")
        card = QFrame(self)
        card.setObjectName("mosaic_card")
        card.setAttribute(Qt.WA_StyledBackground)
        root = QVBoxLayout(card)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)
        # 第 1 行：模式（互斥单选：同一时刻只亮一个）
        lay = QHBoxLayout()
        lay.setSpacing(4)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._btns: dict[str, QPushButton] = {}
        for mode, label in self.MODES:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setFocusPolicy(Qt.NoFocus)   # 点击不抢焦点（快捷键仍归贴图窗口）
            b.clicked.connect(lambda _=False, m=mode: self.modeSelected.emit(m))
            self._btns[mode] = b
            self._group.addButton(b)
            lay.addWidget(b)
        root.addLayout(lay)
        # 第 2 行：大小（马赛克块大小；颜色取自原图像素，不提供颜色配置）
        self.cs = _ColorSizeRow(card, show_color=False)
        self.cs.color_changed.connect(self.color_changed.emit)
        self.cs.width_changed.connect(self.width_changed.emit)
        root.addWidget(self.cs)
        root2 = QVBoxLayout(self)
        root2.setContentsMargins(0, 0, 0, 0)
        root2.addWidget(card)

    def set_current(self, mode: str):
        """高亮当前模式（记忆上次选择）。"""
        for m, b in self._btns.items():
            b.setChecked(m == mode)

    def set_state(self, mode: str, color: tuple, width: int):
        """打开时回显当前模式 + 颜色/大小。"""
        self.set_current(mode)
        self.cs.set_state(color, width)


class _StyleIconRow(QWidget):
    """样式选择行：一组 SVG 图标按钮（互斥单选：选中 = 白图标；未选中 = 主题色）。

    序号/矩形/箭头/画笔的子选项浮层共用，替代文字按钮。
    """

    style_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)     # 互斥：同一时刻只亮一个样式
        self._btns: dict[str, QPushButton] = {}

    def set_styles(self, styles):
        """重建按钮组：styles = [(style_id, icon_name, tooltip), ...]。"""
        from ui.base.icon import icon as svg_icon
        from ui.base.theme import icon_color
        lay = self.layout()
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                self._group.removeButton(w)
                w.deleteLater()
        self._btns = {}

        def refresh(b: QPushButton):
            iname = b.property("style_icon")
            if not iname:
                return
            color = "#ffffff" if b.isChecked() else icon_color()
            b.setIcon(svg_icon(iname, color=color, size=18))

        for sid, iname, tip in styles:
            b = QPushButton()
            b.setCheckable(True)
            b.setFixedSize(28, 28)
            b.setFocusPolicy(Qt.NoFocus)   # 点击不抢焦点：焦点留在图片/工具栏窗口
            b.setProperty("style_icon", iname)
            b.setToolTip(tip)
            b.clicked.connect(
                lambda _=False, m=sid: self.style_selected.emit(m))
            b.toggled.connect(lambda _=False, bb=b: refresh(bb))
            self._btns[sid] = b
            self._group.addButton(b)
            lay.addWidget(b)
        for b in self._btns.values():
            refresh(b)

    def set_current(self, style: str):
        """高亮当前样式（打开时回显，记忆上次选择）。"""
        for m, b in self._btns.items():
            b.setChecked(m == style)


class _ToolOptionsPopup(_AnnoPopupBase):
    """通用工具选项浮层（矩形/箭头/画笔）：样式图标行 + 颜色/大小配置行。"""

    # 样式表唯一来源见 ui/core/anno_core.TOOL_STYLES（本类别名仅为兼容旧引用）
    TOOL_STYLES = TOOL_STYLES

    color_changed = Signal(tuple)
    width_changed = Signal(int)
    style_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("ToolOptionsPopup")
        card = QFrame(self)
        card.setObjectName("tool_options_card")
        card.setAttribute(Qt.WA_StyledBackground)
        root = QVBoxLayout(card)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)
        # 样式图标行（按当前工具重建；无样式的工具隐藏）
        self.style_row = _StyleIconRow(card)
        self.style_row.style_selected.connect(self.style_selected.emit)
        root.addWidget(self.style_row)
        self.cs = _ColorSizeRow(card)
        self.cs.color_changed.connect(self.color_changed.emit)
        self.cs.width_changed.connect(self.width_changed.emit)
        root.addWidget(self.cs)
        root2 = QVBoxLayout(self)
        root2.setContentsMargins(0, 0, 0, 0)
        root2.addWidget(card)

    def set_state(self, color: tuple, width: int, tool: str = "",
                  style: str | None = None):
        """打开时回显当前颜色/大小 + 按工具重建样式图标行。"""
        styles = self.TOOL_STYLES.get(tool)
        if styles:
            self.style_row.set_styles(styles)
            self.style_row.set_current(style or styles[0][0])
            self.style_row.show()
        else:
            self.style_row.hide()
        self.cs.set_state(color, width)


class _NumPopup(_AnnoPopupBase):
    """序号工具选项浮层：点击「序号」工具时在按钮下方弹出。

    第 1 行：序号（左上角 QSpinBox，两位数宽）+ 样式图标（同行）；
    第 2 行：颜色/大小（_ColorSizeRow）。
    Qt.Tool（非 Popup）：点击外部保持打开，再次点击/切换功能才关闭。
    """

    # 序号样式唯一来源见 ui/core/anno_core.NUM_STYLES
    STYLES = NUM_STYLES

    start_changed = Signal(int)
    style_selected = Signal(str)
    size_changed = Signal(int)
    color_changed = Signal(tuple)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("NumPopup")
        card = QFrame(self)
        card.setObjectName("num_card")
        card.setAttribute(Qt.WA_StyledBackground)
        root = QVBoxLayout(card)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)
        # 第 1 行：序号（左上）+ 样式图标（同行）
        lay0 = QHBoxLayout()
        lay0.setSpacing(6)
        lay0.addWidget(QLabel(T("anno.number", "序号")))
        self.start_spin = QSpinBox()
        self.start_spin.setRange(0, 9999)
        self.start_spin.setValue(1)
        self.start_spin.setFixedWidth(52)      # 两位数宽
        self.start_spin.setToolTip(
            T("anno.number_tip", "序号标注的最低起始序号（其余按 +1 递增）"))
        self.start_spin.valueChanged.connect(self.start_changed.emit)
        lay0.addWidget(self.start_spin)
        lay0.addStretch(1)
        self.style_row = _StyleIconRow(card)
        self.style_row.style_selected.connect(self.style_selected.emit)
        self.style_row.set_styles(self.STYLES)
        lay0.addWidget(self.style_row)
        root.addLayout(lay0)
        # 第 2 行：颜色 + 大小（序号数字字号，像素；圆圈直径约为字号 2 倍）
        self.cs = _ColorSizeRow(card, show_size=True, show_color=True,
                                size_range=(8, 64))
        if self.cs.slider is not None:
            self.cs.slider.setToolTip(
                T("anno.number_size_tip",
                  "数字字号（像素）；圆圈直径约为字号的 2 倍，"
                  "多位数自动拉宽为胶囊形"))
        self.cs.color_changed.connect(self.color_changed.emit)
        self.cs.width_changed.connect(self.size_changed.emit)
        root.addWidget(self.cs)
        root2 = QVBoxLayout(self)
        root2.setContentsMargins(0, 0, 0, 0)
        root2.addWidget(card)

    def set_state(self, start: int, size: int, color: tuple, style: str):
        """打开时回显当前起始序号 + 大小 + 颜色 + 样式（信号抑制：回显不改状态）。"""
        self.style_row.set_current(style)
        self.start_spin.blockSignals(True)
        self.start_spin.setValue(max(0, min(9999, int(start))))
        self.start_spin.blockSignals(False)
        self.cs.set_state(color, size)


