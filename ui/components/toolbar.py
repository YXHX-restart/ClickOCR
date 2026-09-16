"""ui.components.toolbar — 统一工具栏（常态短条 / 标注态同一行内联展开）。

- 常态（未标注）：`钉 收藏 文字框 表格 ｜ 标注 ｜ 完成`（常驻操作）。
- 标注态：点「标注」后**同一行内联展开**、工具栏变长，工具插到「标注」与
  「完成」之间：`… ｜ 选择 序号 箭头 画笔 直线 矩形 马赛克 文字 ｜ 撤销 重做 清空 ｜ 完成`。
- 「颜色」「大小」不再常驻主行：每个标注工具点击后在其**子选项浮层**内配置
  （颜色色块 + 大小滑块），会话内记住最后值；软件设置可开启参数永久化记录。
- 复用 ui.components.annotation 组件（TOOLS/TOOL_TIPS、_FontPopup、
  _MosaicModePopup、_ToolOptionsPopup、_ColorSizeRow）与 ui.base.widgets.ColorButton。
- 图标统一走 ui.base.icon 单色 SVG（tool-* / pin / star / undo / redo），
  随主题着色；不再使用 Unicode 字符图标（⮺☆↶↷ 等字体回退风险）。
- 独立顶层窗（无边框半透明），由宿主链锁跟随图片窗口；标注态展开/收起不
  改变窗口标志与 z 序管理（FloatGroup 收口）。
- 无独立 ✕ 关闭按钮：「完成」= 复制图片 + 关闭；关闭入口在图片窗口右上角。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QPushButton, QLabel,
                               QButtonGroup, QFontComboBox)

from platform_compat.fonts import ui_font_family

from ui.base.card_window import CardWindow
from ui.base.texts import T
from ui.base.widgets import screen_available_geometry
from ui.core.anno_core import (MOSAIC_MODES, num_style_ids, tool_style_ids)


class _TableBtn(QPushButton):
    """「表格」按钮三种操作：
    - 短按（单击）＝表格模式开关（按下变蓝，同文字框按钮）；
    - 双击 ＝ 打开表格窗口（不切换模式）；
    - 长按 500ms ＝ 打开表格窗口（不切换模式）。
    单击/双击用 250ms 延迟判定（QPushButton 无原生双击语义，需手动区分）。"""

    long_pressed = Signal()
    double_clicked = Signal()

    def __init__(self, text: str = T("strip.btn_table", "格")):
        super().__init__(text)
        self.setProperty("kind", "corner")
        self.setCheckable(True)
        self.setToolTip(T("toolbar.table", "单击：表格模式（框选复制 TSV）· 双击 / 长按：打开表格窗口"))
        self._long_timer = QTimer(self)
        self._long_timer.setSingleShot(True)
        self._long_timer.setInterval(500)
        self._long_timer.timeout.connect(self._fire_long)
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        # 单击判定间隔取系统双击间隔（约 500ms）：原先硬编码 250ms 小于系统
        # 值，250-500ms 之间的慢双击会先误触发单击切换表格模式、随后双击
        # 又打开表格窗口（模式被意外切换 + 开窗同时发生）
        try:
            from PySide6.QtWidgets import QApplication as _QA
            _dci = int(_QA.doubleClickInterval())
        except Exception:            # noqa: BLE001 无实例/异常时用常见默认
            _dci = 500
        self._click_timer.setInterval(max(250, _dci))
        self._click_timer.timeout.connect(self._do_click)
        self._fired_long = False
        self._pending_click = False
        self._pressed = False

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._pressed = True
            self._fired_long = False
            self._long_timer.start()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self._long_timer.stop()
        if e.button() != Qt.LeftButton:
            super().mouseReleaseEvent(e)
            return
        self._pressed = False
        if self._fired_long:             # 长按已触发：本次释放不切换
            self._fired_long = False
            self.setDown(False)
            return
        # 单击/双击判定：延迟 250ms，期间再次按下则算双击
        self._pending_click = True
        self._click_timer.start()
        self.setDown(False)              # 手动恢复按下视觉（不依赖 clicked 自动 toggle）

    def mouseDoubleClickEvent(self, e):
        self._click_timer.stop()
        self._long_timer.stop()
        self._pending_click = False
        self.setDown(False)
        self.double_clicked.emit()       # 双击 = 打开表格窗口

    def _do_click(self):
        if self._pending_click:          # 单击判定到期：切换表格模式
            self._pending_click = False
            self.setChecked(not self.isChecked())

    def _fire_long(self):
        self._fired_long = True
        self._pending_click = False
        self._click_timer.stop()
        self.long_pressed.emit()


class _ToolsPack(QWidget):
    """标注工具段（工具栏内联展开）：工具组 + 颜色/线宽格子 + 撤销/重做/清空。

    浮层（字体/马赛克/线宽滑块/调色板）锚定本段所在工具栏正下方；
    信号全部转发宿主 ToolBar。
    """

    tool_changed = Signal(str)
    color_changed = Signal(tuple)         # 颜色（子选项浮层内变更）
    width_changed = Signal(int)
    mosaic_mode_changed = Signal(str)
    text_size_changed = Signal(int)       # 文字字号
    text_bold_changed = Signal(bool)      # 文字加粗
    text_italic_changed = Signal(bool)    # 文字斜体
    num_start_changed = Signal(int)       # 序号起始序号
    num_size_changed = Signal(int)        # 序号圆圈尺寸
    num_style_changed = Signal(str)       # 序号样式
    tool_style_changed = Signal(str, str) # (tool, style) 矩形/箭头/画笔样式
    undo_requested = Signal()
    redo_requested = Signal()
    clear_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("anno_tools")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # ---- 工具按钮组 ----
        from ui.components.annotation import TOOLS, TOOL_TIPS
        from ui.base.icon import icon as svg_icon
        from ui.base.theme import icon_color
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._tool = "rect"
        for tool in TOOLS:
            b = QPushButton()
            b.setCheckable(True)
            b.setToolTip(TOOL_TIPS[tool])
            b.setProperty("tool", tool)
            b.setFixedSize(24, 24)
            b.clicked.connect(lambda _=False, tt=tool: self._set_tool(tt))
            b.toggled.connect(lambda _=False, bb=b: self._refresh_tool_icon(bb))
            self._group.addButton(b)
            lay.addWidget(b)
        self._group.buttons()[TOOLS.index("rect")].setChecked(True)
        self._refresh_tool_icons()

        # ---- 标注参数状态（颜色/大小在**各工具子选项浮层**内配置，主行不再放格子；
        #      会话内保持最后值，退出标注/切换工具不丢失）----
        self._color = (255, 95, 95)
        self._width = 2
        self._font_popup = None
        self._mosaic_popup = None
        self._options_popup = None
        self._num_popup = None
        self.font_combo = QFontComboBox()   # 占位，懒创建浮层后替换；兼容查询接口
        self.font_combo.setCurrentFont(QFont(ui_font_family()))
        self.text_size = 18
        self.text_bold = False
        self.text_italic = False
        # 马赛克模式（画笔/框选/文字框）：默认框选，记忆上次选择
        self.mosaic_mode = "rect"
        # 序号工具参数：起始序号 / 圆圈尺寸 / 样式
        self.num_start = 1
        self.num_size = 18
        self.num_style = "circle"
        # 矩形/箭头/直线/画笔样式（各自记忆）
        self.tool_styles = {"rect": "rect", "arrow": "single", "line": "solid",
                            "pen": "solid"}

        # ---- 撤销 / 重做 / 清空 ----
        lay.addSpacing(6)
        self._icon_btns = []   # (button, icon_name)：主题切换时统一刷新
        for icon_name, tip_key, sig in (
                ("undo", "toolbar.undo", self.undo_requested),
                ("redo", "toolbar.redo", self.redo_requested),
                (None, "toolbar.clear", self.clear_requested)):
            b = QPushButton(T("toolbar.clear_btn", "清空") if icon_name is None else "")
            if icon_name is not None:
                b.setIcon(svg_icon(icon_name, color=icon_color(), size=18))
                b.setFixedSize(24, 24)     # 图标钮与工具按钮同尺寸
                self._icon_btns.append((b, icon_name))
            else:
                b.setFixedHeight(24)
            b.setToolTip(T(tip_key, {"toolbar.undo": "撤销 (Ctrl+Z)",
                                     "toolbar.redo": "重做 (Ctrl+Shift+Z)",
                                     "toolbar.clear": "清空全部标注"}[tip_key]))
            b.clicked.connect(sig.emit)
            lay.addWidget(b)

        # 主题切换 → 刷新全部工具图标（apply_theme 只清缓存，已挂按钮上的
        # 旧色 QIcon 不会自动替换；弱引用订阅，销毁后自动失效）
        from ui.base.theme import on_theme_changed
        on_theme_changed(self._on_theme_refresh)

    def _on_theme_refresh(self, _theme):
        """主题变更：按新主题色重建工具按钮与撤销/重做图标。"""
        from ui.base.icon import icon as svg_icon
        from ui.base.theme import icon_color
        self._refresh_tool_icons()
        for b, name in getattr(self, "_icon_btns", []):
            try:
                b.setIcon(svg_icon(name, color=icon_color(), size=18))
            except Exception:  # noqa: BLE001
                pass

    def _refresh_tool_icon(self, b: QPushButton):
        """单工具按钮图标着色：未选中 = 主题色；选中（蓝底）= 白色。"""
        from ui.base.icon import icon as svg_icon
        from ui.base.theme import icon_color
        tool = b.property("tool")
        if not tool:
            return
        color = "#ffffff" if b.isChecked() else icon_color()
        b.setIcon(svg_icon(f"tool-{tool}", color=color, size=20))

    def _refresh_tool_icons(self):
        for b in self._group.buttons():
            self._refresh_tool_icon(b)

    # ---- 状态 ----
    @property
    def tool(self) -> str:
        return self._tool

    def color(self) -> tuple:
        return self._color

    def line_width(self) -> int:
        """当前线宽（1-32）。不覆盖 QWidget.width()，避免读取错误值。"""
        return self._width

    def set_params(self, color: tuple | None = None, width: int | None = None,
                   text_size: int | None = None, text_bold: bool | None = None,
                   text_italic: bool | None = None,
                   mosaic_mode: str | None = None,
                   num_start: int | None = None, num_size: int | None = None,
                   num_style: str | None = None,
                   rect_style: str | None = None,
                   arrow_style: str | None = None,
                   line_style: str | None = None,
                   pen_style: str | None = None):
        """外部恢复参数（软件设置"永久化记录标注工具参数"开启时启动调用）。"""
        if color is not None:
            self._color = tuple(int(c) for c in color)
        if width is not None:
            self._width = max(1, min(32, int(width)))
        if text_size is not None:
            self.text_size = max(6, min(96, int(text_size)))
        if text_bold is not None:
            self.text_bold = bool(text_bold)
        if text_italic is not None:
            self.text_italic = bool(text_italic)
        if mosaic_mode in MOSAIC_MODES:
            self.mosaic_mode = mosaic_mode
        if num_start is not None:
            self.num_start = max(0, min(9999, int(num_start)))
        if num_size is not None:
            self.num_size = max(8, min(64, int(num_size)))
        if num_style in num_style_ids():
            self.num_style = num_style
        # 样式白名单直接读唯一来源 anno_core.TOOL_STYLES（原先此处硬编码各 3
        # 种的缩水名单，实际各 7 种——ellipse/elbow/wavy/glow 等 16 种样式
        # 重启后被静默回退默认，「参数永久化记录」对它们失效）
        for _tool, _val in (("rect", rect_style), ("arrow", arrow_style),
                            ("line", line_style), ("pen", pen_style)):
            if _val in tool_style_ids(_tool):
                self.tool_styles[_tool] = _val

    def set_tool_state(self, tool: str):
        """外部设置当前工具（如会话恢复），同步按钮高亮。"""
        for b in self._group.buttons():
            if b.property("tool") == tool:
                b.setChecked(True)
                self._tool = tool
                return

    def close_picker(self):
        """关闭所有子选项浮层（字体属性/马赛克模式/通用选项/序号/各浮层内取色器）。

        退出标注模式或关闭图片时调用——子浮层是 Qt.Tool 独立顶层窗，
        不随工具栏隐藏，不关会残留在屏幕上。
        """
        for p in (self._font_popup, self._mosaic_popup, self._options_popup,
                  self._num_popup):
            if p is not None:
                p.close()
        for p in (self._font_popup, self._mosaic_popup, self._options_popup,
                  self._num_popup):
            if p is not None:
                cb = getattr(getattr(p, "cs", None), "color_btn", None)
                if cb is not None and getattr(cb, "_picker", None) is not None:
                    cb._picker.close()

    # ---- 工具切换：每个工具点击都弹对应子选项浮层（内含颜色/大小）----
    def _check_button(self, tool: str):
        """同步按钮高亮到指定工具（键盘快捷键切换等非点击路径用）。"""
        for b in self._group.buttons():
            if b.property("tool") == tool:
                b.setChecked(True)      # toggled → _refresh_tool_icon 变白
                return

    def _set_tool(self, tool: str):
        # 选择工具无需颜色/大小：点击仅关闭其它浮层，自身不弹子选项
        if tool == "select":
            if self._tool == "select":
                for p in (self._font_popup, self._mosaic_popup,
                          self._options_popup, self._num_popup):
                    if p is not None and p.isVisible():
                        p.close()
            else:
                self._tool = tool
                self._check_button(tool)
                self.tool_changed.emit(tool)
                for p in (self._font_popup, self._mosaic_popup,
                          self._options_popup, self._num_popup):
                    if p is not None:
                        p.close()
            return
        pop = self._popup_for(tool)
        # 再次点击同一功能按钮：toggle 浮层——可见则关闭；已关闭则重新弹出
        if tool == self._tool:
            self._check_button(tool)
            if pop is not None and pop.isVisible():
                pop.close()
            else:
                self._pop_options_for(tool)
            return
        self._tool = tool
        self._check_button(tool)
        self.tool_changed.emit(tool)
        # 切换工具：关闭其它工具的浮层
        for p in (self._font_popup, self._mosaic_popup, self._options_popup,
                  self._num_popup):
            if p is not None and p is not pop:
                p.close()
        self._pop_options_for(tool)

    def _popup_for(self, tool: str):
        """返回 tool 对应的子选项浮层（未创建则 None）。"""
        if tool == "text":
            return self._font_popup
        if tool == "mosaic":
            return self._mosaic_popup
        if tool == "num":
            return self._num_popup
        return self._options_popup

    def _pop_options_for(self, tool: str):
        if tool == "text":
            self._pop_font()
        elif tool == "mosaic":
            self._pop_mosaic_mode()
        elif tool == "num":
            self._pop_num()
        else:
            self._pop_options()

    # ---- 通用浮层定位（锚定触发按钮正下方，放不下翻按钮上方）----
    def _place_popup(self, popup, tool: str):
        """把子选项浮层定位到对应工具按钮正下方。"""
        from ui.components.annotation import TOOLS
        # 用本工具栏所在屏的可用区：工具栏在副屏时按主屏夹取会把浮层
        # 强行拉回主屏边缘（甚至跑到另一块屏上）
        scr = screen_available_geometry(self)
        popup.resize(popup.sizeHint())
        pw = popup.width()
        ph = popup.height()
        btn = self._group.buttons()[TOOLS.index(tool)]
        btn_cx = btn.mapToGlobal(btn.rect().center()).x()
        x = max(scr.left(), min(btn_cx - pw // 2, scr.right() - pw))
        y = btn.mapToGlobal(btn.rect().bottomLeft()).y() + 4
        if y + ph > scr.bottom():
            y = btn.mapToGlobal(btn.rect().topLeft()).y() - ph - 4
            if y < scr.top():
                y = scr.top() + 4
        popup.move(x, y)
        popup.show()
        popup.raise_()

    # ---- 通用工具选项（矩形/箭头/画笔：样式 + 颜色 + 大小）----
    def _pop_options(self):
        from ui.components.annotation import _ToolOptionsPopup
        if self._options_popup is None:
            self._options_popup = _ToolOptionsPopup(self)
            self._options_popup.color_changed.connect(self._on_color_changed)
            self._options_popup.width_changed.connect(self._on_width_changed)
            self._options_popup.style_selected.connect(self._set_tool_style)
        self._options_popup.set_state(
            self._color, self._width, self._tool,
            self.tool_styles.get(self._tool, ""))
        self._place_popup(self._options_popup, self._tool)

    def _set_tool_style(self, style: str):
        """选中矩形/箭头/画笔样式：更新当前工具状态 + 通知宿主持久化记忆。"""
        if self._tool not in self.tool_styles or not style:
            return
        self.tool_styles[self._tool] = style
        self.tool_style_changed.emit(self._tool, style)

    def _on_color_changed(self, c: tuple):
        self._color = tuple(int(v) for v in c)
        self.color_changed.emit(self._color)

    def _on_width_changed(self, v: int):
        self._width = max(1, min(32, int(v)))
        self.width_changed.emit(self._width)

    def _pop_font(self):
        """点击「文字」工具 → 在文字按钮正下方弹出文字属性浮层（含颜色/大小）。"""
        from ui.components.annotation import _FontPopup
        if self._font_popup is None:
            self._font_popup = _FontPopup(self)
            self.font_combo = self._font_popup.font_combo   # 兼容查询接口
            self._font_popup.size_changed.connect(self._set_text_size)
            self._font_popup.bold_toggled.connect(self._set_text_bold)
            self._font_popup.italic_toggled.connect(self._set_text_italic)
            self._font_popup.color_changed.connect(self._on_color_changed)
            self._font_popup.width_changed.connect(self._on_width_changed)
        self._font_popup.set_state(self.text_size, self.text_bold,
                                   self.text_italic, self._color, self._width)
        self._place_popup(self._font_popup, "text")

    def _set_text_size(self, v: int):
        self.text_size = max(6, min(96, int(v)))
        self.text_size_changed.emit(self.text_size)

    def _set_text_bold(self, b: bool):
        self.text_bold = bool(b)
        self.text_bold_changed.emit(self.text_bold)

    def _set_text_italic(self, b: bool):
        self.text_italic = bool(b)
        self.text_italic_changed.emit(self.text_italic)

    def _pop_mosaic_mode(self):
        """点击马赛克工具 → 在按钮正下方弹出模式选择（画笔/框选/文字框，含颜色/大小）。"""
        from ui.components.annotation import _MosaicModePopup
        if self._mosaic_popup is None:
            self._mosaic_popup = _MosaicModePopup(self)
            self._mosaic_popup.modeSelected.connect(self._set_mosaic_mode)
            self._mosaic_popup.color_changed.connect(self._on_color_changed)
            self._mosaic_popup.width_changed.connect(self._on_width_changed)
        self._mosaic_popup.set_state(self.mosaic_mode, self._color, self._width)
        self._place_popup(self._mosaic_popup, "mosaic")

    def _set_mosaic_mode(self, mode: str):
        """选中马赛克模式：更新状态 + 通知宿主持久化记忆。
        浮层保持打开（方便查看/连续切换），再次点击按钮或切换工具才关闭。"""
        if mode not in ("pen", "rect", "textbox"):
            return
        self.mosaic_mode = mode
        self.mosaic_mode_changed.emit(mode)

    def _pop_num(self):
        """点击「序号」工具 → 在按钮正下方弹出序号选项浮层（样式/起始序号/颜色/大小）。"""
        from ui.components.annotation import _NumPopup
        if self._num_popup is None:
            self._num_popup = _NumPopup(self)
            self._num_popup.style_selected.connect(self._set_num_style)
            self._num_popup.start_changed.connect(self._set_num_start)
            self._num_popup.size_changed.connect(self._set_num_size)
            self._num_popup.color_changed.connect(self._on_color_changed)
        self._num_popup.set_state(self.num_start, self.num_size, self._color,
                                  self.num_style)
        self._place_popup(self._num_popup, "num")

    def _set_num_start(self, v: int):
        self.num_start = max(0, min(9999, int(v)))
        self.num_start_changed.emit(self.num_start)

    def _set_num_size(self, v: int):
        self.num_size = max(8, min(64, int(v)))
        self.num_size_changed.emit(self.num_size)

    def _set_num_style(self, style: str):
        """选中序号样式：更新状态 + 通知宿主持久化记忆（浮层保持打开）。"""
        if style not in ("circle", "circle_o", "square", "paren", "plain"):
            return
        self.num_style = style
        self.num_style_changed.emit(style)


class ToolBar(CardWindow):
    """统一工具栏：常态短条 / 标注态同一行内联展开变长。

    常驻操作：钉 收藏 文字框 表格 ｜ 标注 ｜ 完成（「完成」固定最右）。
    标注态插入工具段（_ToolsPack）：选择 序号 箭头 画笔 直线 矩形 马赛克 文字 ｜
    撤销 重做 清空。无独立 ✕ 关闭按钮；颜色/大小在各工具子选项浮层内配置。
    """

    pin_toggled = Signal(bool)            # 钉图按钮：切换钉图（隐藏状态栏+工具栏，只留钉子）
    fav_clicked = Signal()                # 收藏当前截图
    qr_toggled = Signal(bool)             # 二维码识别开关（M3 T5）
    boxes_toggled = Signal(bool)          # 文字框开关
    table_mode_changed = Signal(bool)     # 表格按钮短按：切换表格模式
    table_window_requested = Signal()     # 长按/双击表格：打开表格窗口
    anno_toggled = Signal(bool)           # 标注按钮：进入/退出标注态
    done_clicked = Signal()               # 完成：复制图片 + 关闭窗口

    def __init__(self, parent=None):
        super().__init__(title=None, with_close=False)
        # 位置完全由 ResultOverlay._sync_toolbar_pos() 决定，关掉 CardWindow 的
        # 「首次显示自动居中」：跨屏截图时鼠标终点落在 Qt 逻辑空间的屏间缝隙里，
        # 自动居中会因取不到屏而退回**主屏并居中**（见 status_strip 同处注释）。
        self._centered = True
        self.setObjectName("ToolBar")
        self._overlay = None
        # CardWindow 的 card_lay 是垂直布局；PySide6 下 setLayout 无法替换已挂布局
        # （card.layout() 仍返回旧 QVBoxLayout）→ 不替换：垂直容器内**嵌一个水平行**，
        # 视觉上即为单条工具栏（卡片背景包裹一行按钮）。
        self.card_lay.setContentsMargins(8, 5, 8, 5)
        self.card_lay.setSpacing(5)
        lay = QHBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(5)
        self.card_lay.addLayout(lay)

        def corner(label: str, tip: str, checkable: bool = False):
            b = QPushButton(label)
            b.setProperty("kind", "corner")
            b.setToolTip(tip)
            if checkable:
                b.setCheckable(True)
            return b

        # ---- 常驻操作 ----
        from ui.base.icon import icon as svg_icon
        from ui.base.theme import accent_color
        self.btn_pin = QPushButton()
        self.btn_pin.setIcon(svg_icon("pin", color=accent_color(), size=18))
        self.btn_pin.setProperty("kind", "pin")
        self.btn_pin.setFixedSize(32, 24)      # 与其它按钮同高（24）
        self.btn_pin.setCheckable(True)
        self.btn_pin.setToolTip(T("toolbar.pin", "钉图：隐藏状态栏+工具栏，只留钉子（图片保留）"))
        self.btn_pin.toggled.connect(self.pin_toggled.emit)
        lay.addWidget(self.btn_pin)

        self.btn_fav = QPushButton()
        self.btn_fav.setIcon(svg_icon("star", color=accent_color(), size=18))
        self.btn_fav.setProperty("kind", "fav")
        self.btn_fav.setFixedSize(32, 24)      # 与其它按钮同高（24）
        self.btn_fav.setToolTip(T("toolbar.fav", "收藏当前截图"))
        self.btn_fav.clicked.connect(self.fav_clicked.emit)
        lay.addWidget(self.btn_fav)

        # 二维码识别开关（[ui] enable_qr；开启后 OCR 时顺带识别二维码/条码）
        # kind='corner'：与「文字框/表格」一致，启用时按钮蓝色亮起；
        # 图标 = 透明底二维码 **反色**（固定白色码）：corner 按钮底固定深色，
        # 白码在深底上恒清晰（黑码会与深底糊在一起），不随主题
        self.btn_qr = QPushButton()
        self.btn_qr.setIcon(svg_icon("qr", color="#ffffff", size=18))
        self.btn_qr.setProperty("kind", "corner")
        self.btn_qr.setFixedSize(32, 24)      # 与其它按钮同高（24）
        self.btn_qr.setCheckable(True)
        self.btn_qr.setToolTip(T(
            "strip.btn_qr_tip",
            "二维码识别开关（Q）：开启后识别截图中的二维码/条码"))
        self.btn_qr.toggled.connect(self.qr_toggled.emit)
        lay.addWidget(self.btn_qr)

        self.btn_boxes = corner(T("strip.btn_boxes", "文"),
                                T("toolbar.boxes", "显示/隐藏识别文字蓝色框"), checkable=True)
        self.btn_boxes.setFixedHeight(24)          # 与工具格子同高
        self.btn_boxes.toggled.connect(self.boxes_toggled.emit)
        lay.addWidget(self.btn_boxes)

        self.btn_table = _TableBtn(T("strip.btn_table", "格"))
        self.btn_table.setFixedHeight(24)          # 与工具格子同高
        self.btn_table.toggled.connect(self.table_mode_changed.emit)
        self.btn_table.long_pressed.connect(self.table_window_requested.emit)
        self.btn_table.double_clicked.connect(self.table_window_requested.emit)
        lay.addWidget(self.btn_table)

        # ---- 标注入口（常驻，展开/收起工具段）----
        self.btn_anno = corner(T("strip.btn_anno", "标"),
                               T("toolbar.anno", "开始标注工具"), checkable=True)
        self.btn_anno.setFixedHeight(24)          # 与工具格子同高
        self.btn_anno.toggled.connect(self.anno_toggled.emit)
        lay.addWidget(self.btn_anno)

        # ---- 标注工具段（标注态内联展开）----
        self.tools = _ToolsPack(self)
        self.tools.hide()
        lay.addWidget(self.tools)

        # ---- 完成（固定最右，双格子大小 48×24）----
        self.btn_done = corner(T("strip.btn_done", "完成"),
                               T("toolbar.done", "完成：复制整张图片并关闭窗口"))
        self.btn_done.setFixedSize(48, 24)
        self.btn_done.clicked.connect(self.done_clicked.emit)
        lay.addWidget(self.btn_done)

        # 焦点同步：撤销/重做/清空等按钮点击后把焦点还回图片窗口，
        # 保证 Ctrl+Z 等标注快捷键（由 overlay.keyPressEvent 处理）可继续生效。
        for _sig in (self.tools.undo_requested, self.tools.redo_requested,
                     self.tools.clear_requested):
            _sig.connect(self._refocus_overlay)
        # 切标注工具（含 QWERTYU 快捷键路径）后同样还焦图片窗口
        self.tools.tool_changed.connect(self._refocus_overlay)

        # 标注态默认收起；宿主要展开时调 set_anno_mode(True)
        self._anno_mode_visible = False

        # 主题切换 → 钉图/收藏图标（金色强调色随主题变化）
        from ui.base.theme import on_theme_changed
        on_theme_changed(self._on_theme_refresh)

    def _on_theme_refresh(self, _theme):
        """主题变更：按新强调色重建钉图/收藏图标。"""
        from ui.base.icon import icon as svg_icon
        from ui.base.theme import accent_color
        try:
            self.btn_pin.setIcon(svg_icon("pin", color=accent_color(), size=18))
            self.btn_fav.setIcon(svg_icon("star", color=accent_color(), size=18))
        except Exception:  # noqa: BLE001
            pass

    # ---- 接口 ----
    def attach_overlay(self, overlay):
        self._overlay = overlay

    def select_tool(self, tool: str):
        """外部选择标注工具（QWERTYU 快捷键等）：切工具 + 高亮按钮 + 弹子选项浮层。"""
        self.tools._set_tool(tool)

    def set_anno_mode(self, on: bool):
        """标注态：on=True 内联展开工具段（工具栏变长）；False 收起（关闭子浮层）。

        收紧是**异步**的：tools.setVisible 后布局重算发生在事件循环中，此时
        sizeHint 仍是旧值——立即 adjustSize 不生效，窗口宽度保持展开值、
        剩余按钮被拉伸变宽。故等一轮事件循环后收紧 + 通知宿主链锁重排。"""
        self._anno_mode_visible = bool(on)
        self.tools.setVisible(on)
        if not on:
            self.close_picker()
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._on_layout_settled)

    def _on_layout_settled(self):
        """一轮事件循环后：按最新 sizeHint 收紧窗口 + 通知宿主重新链锁定位。"""
        try:
            self.adjustSize()
            ov = self._overlay
            if ov is not None and hasattr(ov, "_sync_toolbar_pos"):
                ov._sync_toolbar_pos()
        except RuntimeError:             # 窗口已销毁
            pass

    # ---- 标注工具兼容接口（原 AnnotationToolBar 查询接口）----
    @property
    def tool(self) -> str:
        return self.tools.tool

    def color(self) -> tuple:
        return self.tools.color()

    def line_width(self) -> int:
        return self.tools.line_width()

    @property
    def text_size(self) -> int:
        return self.tools.text_size

    @property
    def text_bold(self) -> bool:
        return self.tools.text_bold

    @property
    def text_italic(self) -> bool:
        return self.tools.text_italic

    @property
    def mosaic_mode(self) -> str:
        return self.tools.mosaic_mode

    @property
    def num_start(self) -> int:
        return self.tools.num_start

    @property
    def num_size(self) -> int:
        return self.tools.num_size

    @property
    def num_style(self) -> str:
        return self.tools.num_style

    @property
    def rect_style(self) -> str:
        return self.tools.tool_styles["rect"]

    @property
    def arrow_style(self) -> str:
        return self.tools.tool_styles["arrow"]

    @property
    def line_style(self) -> str:
        return self.tools.tool_styles["line"]

    @property
    def pen_style(self) -> str:
        return self.tools.tool_styles["pen"]

    @property
    def font_combo(self):
        return self.tools.font_combo

    def close_picker(self):
        """关闭标注工具段所有子浮层（字体/马赛克/线宽/调色板）。"""
        self.tools.close_picker()

    def _refocus_overlay(self):
        """撤销/重做/清空等操作后：焦点还给图片窗口（标注快捷键继续生效）。"""
        ov = self._overlay
        if ov is not None and ov.isVisible():
            ov.activateWindow()
            ov.setFocus(Qt.OtherFocusReason)

    def keyPressEvent(self, e):
        """焦点落在工具栏（点过工具按钮等）时：标注态 QWERTYU/Esc、常用态
        工具栏键（Q/W/E/F 二维码/文字框/表格/收藏）都转发给图片窗口统一处理，
        避免快捷键因焦点不在贴图窗口而失效。"""
        ov = self._overlay
        if ov is not None:
            if getattr(ov, "_anno_mode", False):
                if ov.handle_anno_key(e):
                    return
            elif hasattr(ov, "_handle_toolbar_key") and ov._handle_toolbar_key(e):
                return
        super().keyPressEvent(e)

    def moveEvent(self, e):
        """工具栏移动（链锁跟随/用户拖动）→ 已打开的子选项浮层跟随平移，
        保持与触发按钮的相对位置（"子选项框跟随工具栏"）。"""
        super().moveEvent(e)
        old = e.oldPos()
        if old.isNull():
            return
        d = self.pos() - old
        for p in (self.tools._font_popup, self.tools._mosaic_popup,
                  self.tools._options_popup, self.tools._num_popup):
            if p is not None and p.isVisible():
                p.move(p.pos() + d)

    # ---- 拖动（空白区 → 移动图片窗口；锚定宿主）----
    def mousePressEvent(self, e):
        ov = self._overlay
        if ov is not None and e.button() == Qt.LeftButton:
            self._drag_offset = (e.globalPosition().toPoint()
                                 - ov.frameGeometry().topLeft())
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if getattr(self, "_drag_offset", None) is not None \
                and (e.buttons() & Qt.LeftButton) and self._overlay is not None:
            self._overlay.move(e.globalPosition().toPoint() - self._drag_offset)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag_offset = None
        super().mouseReleaseEvent(e)
