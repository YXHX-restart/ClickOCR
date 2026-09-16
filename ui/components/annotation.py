"""ui.components.annotation — 标注会话（AnnotationSession）与对外再导出层。

2026-09-11 重构 S1：本文件原 2,010 行，按职责切成三块 ——

  - `anno/items.py`   图元数据类 + 工厂 + 离屏渲染 + 几何辅助（原 46–831 行）
  - `anno/popups.py`  浮层/按钮/选择行等 Qt 控件（原 832–1347 行）
  - 本文件保留        `AnnotationSession`（会话 / 命令栈 / 信号，原 1348 行起）

拆分方式是**纯搬移**（`只搬移不改写`）：行为零变化，全部原对外名字在此再导出，
`result_overlay.py` / `toolbar.py` / `history_panel.py` / 测试脚本一行都不用改。
"""

from __future__ import annotations

import copy
import math

from PySide6.QtCore import Qt, QPointF, QRectF, Signal
from PySide6.QtGui import (QColor, QPen, QBrush, QFont, QPainterPath,
                           QPolygonF, QPixmap, QImage, QPainter)
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
                               QLabel, QFrame, QSlider, QSpinBox,
                               QButtonGroup, QFontComboBox, QComboBox,
                               QGraphicsRectItem, QGraphicsLineItem,
                               QGraphicsPathItem, QGraphicsPolygonItem,
                               QGraphicsPixmapItem, QGraphicsEllipseItem,
                               QGraphicsTextItem, QGraphicsItemGroup,
                               QGraphicsScene, QStyleOptionGraphicsItem)

from platform_compat.fonts import ui_font_family

from core_engine.logging_setup import get_logger

# 标注核心纯逻辑（命令撤销体系 + 序列化 + 样式合法值唯一来源，无 Qt 依赖）
from ui.core.anno_core import (ANNO_VERSION, UNDO_LIMIT, SERIALIZABLE_TYPES,
                          MOSAIC_MODES, num_style_ids, tool_style_ids,
                          _Cmd, _AddCmd, _DeleteCmd, _MoveCmd, _EditCmd,
                          _ClearCmd, _RenumberCmd,
                          anno_to_json, anno_from_json)

log = get_logger("annotation")


# ---- 命令模式撤销/重做（Command 栈）：命令类见 ui/anno_core.py ----
# 快照式 undo（deepcopy 整个 data 列表）改为「命令 + 数据变更」，省内存：
# 每条命令只存被操作的 item/坐标，undo/redo 走 apply/unapply；
# 单笔绘制（含全部轨迹点）= 一条 AddCmd，连续笔迹天然合并为一步撤回。



from .anno.items import (
    _BaseElement, _norm_offsets, _text_item_ink_rect, _dedupe_points,
    _offset_polyline, _resample_points, _polyline_item, _line_sine_item,
    _line_zigzag_item, _bubble_path, _RectElement, _ArrowElement,
    _PenElement, _LineElement, _MosaicElement, _MosaicPenElement,
    _TextElement, _NumElement, _ItemFactory, TOOLS, TOOL_LABELS,
    TOOL_TIPS, render_annotated_image, _hex, _make_mosaic_pixmap
)
from .anno.popups import (
    _ColorSizeRow, _AnnoPopupBase,
    _FontPopup, _MosaicModePopup, _StyleIconRow, _ToolOptionsPopup,
    _NumPopup
)

# ---- 标注会话 ----
class AnnotationSession:
    """标注数据 + 场景图层 + 撤销重做。由 ResultOverlay 驱动。"""

    def __init__(self, view, scene, base_pixmap: QPixmap):
        self.view = view
        self.scene = scene
        self.base = base_pixmap
        self.tool = "rect"
        self.color = (255, 95, 95)
        self.width = 2
        self.font_family = ui_font_family()
        self.font_size = 18
        self.font_bold = False
        self.font_italic = False
        self.mosaic_mode = "rect"   # 马赛克模式：pen(画笔)/rect(框选)/textbox(文字框)
        self.num_start = 1          # 序号标注最低起始序号
        self.num_size = 18          # 序号圆圈/数字尺寸
        self.num_style = "circle"   # 序号样式：circle/circle_o/square/paren/plain
        # 矩形/箭头/直线/画笔的样式（各自记忆，切换工具不丢）
        self.tool_styles = {"rect": "rect", "arrow": "single", "line": "solid",
                            "pen": "solid"}
        self.data: list[dict] = []
        self._items: list = []          # QGraphicsItem
        self._undo: list[_Cmd] = []
        self._redo: list[_Cmd] = []
        self._drawing: dict | None = None
        self._draw_item = None
        self._press_pt = None           # text 工具：按下起点（区分单击/拖动）
        self._text_drag_rect = None     # text 工具：拖动中的预览矩形
        self._text_edit = None          # 就地编辑中的文字项（text 工具）
        self._text_edit_box = None      # 文本框编辑时的可见虚线边框
        self._editing_index = None      # 正在编辑的已有文字块 data 索引（双击编辑）
        self._hidden_edit_items: list = []  # 双击编辑时隐藏的旧渲染项（防编辑态与旧渲染重影）
        # 选择工具（select）：选中/拖动/删除标注
        self._selected_idx = None       # 选中的 data 索引
        self._select_box = None         # 选中高亮虚线框
        self._select_press = None       # 拖动基准点（场景坐标，用于增量 moveBy）
        self._select_start_pt = None    # 拖动起点（用于 release 计算总位移）
        self._select_moved = False
        self._select_items: list = []   # 选中项缓存（拖动时只 moveBy 这些，避免每次 move 全量遍历）
        # 就地文字编辑：编辑器失焦时提交
        self.scene.focusItemChanged.connect(self._on_focus_changed)

    # ---- 工具/外观 ----
    def set_tool(self, tool: str):
        self.tool = tool if tool in TOOLS else "rect"

    def set_color(self, color: tuple):
        self.color = tuple(int(c) for c in color)

    def set_width(self, w: int):
        self.width = max(1, int(w))

    def set_font(self, family: str):
        self.font_family = family or ui_font_family()

    def set_font_size(self, v: int):
        self.font_size = max(6, min(96, int(v)))

    def set_bold(self, b: bool):
        self.font_bold = bool(b)

    def set_italic(self, b: bool):
        self.font_italic = bool(b)

    def set_mosaic_mode(self, mode: str):
        self.mosaic_mode = mode if mode in MOSAIC_MODES else "rect"

    def set_num_start(self, v: int, renumber: bool = False):
        """设置序号起始序号；renumber=True 时同步重排已有序号标注。"""
        self.num_start = max(0, int(v))
        if renumber:
            self.renumber(self.num_start)

    def set_num_size(self, v: int):
        self.num_size = max(8, min(64, int(v)))

    def set_num_style(self, style: str):
        if style in num_style_ids():
            self.num_style = style

    def set_tool_style(self, tool: str, style: str):
        """设置矩形/箭头/直线/画笔的样式（合法值唯一来源：anno_core.TOOL_STYLES）。"""
        if tool not in self.tool_styles:
            return
        if style in tool_style_ids(tool):
            self.tool_styles[tool] = style

    def renumber(self, start: int):
        """按起始序号重排所有已放置的序号标注（顺序 = data 中的出现顺序）。"""
        changes = []
        seq = 0
        for i, d in enumerate(self.data):
            if d.get("type") != "num":
                continue
            old = int(d.get("num", 0))
            new = int(start) + seq
            seq += 1
            if old != new:
                changes.append((i, old, new))
                d["num"] = new
        if changes:
            self._push_cmd(_RenumberCmd(changes))
            self._render_all()

    @property
    def is_drawing(self) -> bool:
        """是否正在画布上拖拽绘制（区别于点击工具栏按钮等其它左键操作）。"""
        return self._drawing is not None

    # ---- 数据 ----
    def load(self, items: list):
        self.data = copy.deepcopy(items)
        # 重建撤销栈：按 items 顺序生成 AddCmd 序列（add0、add1…），
        # 使恢复历史后也能分步撤销（例如画 3 个框可逐框撤回，而非整体一步）。
        # 命令持有外部列表元素的独立副本：调用方复用/原地修改传入列表时，
        # 撤销重做内容不被外部状态污染。
        self._undo = [_AddCmd(i, copy.deepcopy(it)) for i, it in enumerate(items)]
        self._redo = []
        self._selected_idx = None
        self._clear_select_box()
        self._render_all()

    def json(self) -> str:
        return anno_to_json(self.data)

    # ---- 渲染 ----
    def _clear_layer(self):
        for it in self._items:
            self.scene.removeItem(it)
        self._items = []
        self._clear_select_box()

    def _make_item(self, d: dict):
        """按类型经图元工厂创建场景项（图元类见 _ItemFactory 注册表）。"""
        return _ItemFactory.create(d["type"], d, self.base)

    def _render_all(self):
        self._clear_layer()
        for i, d in enumerate(self.data):
            self._add_to_scene(d, i)

    def _add_to_scene(self, d: dict, idx: int | None = None):
        it = self._make_item(d)
        if it is None:
            return
        items = it if isinstance(it, tuple) else (it,)
        for i in items:
            i.setZValue(5)
            self.scene.addItem(i)
            self._tag_idx(i, idx)
        self._items.extend(items)

    def _tag_idx(self, item, idx: int | None):
        """给标注项（含 group 内子项）标记 data 索引，供选择/移动/命中。"""
        if idx is None:
            return
        item._anno_idx = idx
        for ch in item.childItems():
            self._tag_idx(ch, idx)

    # ---- 撤销 / 重做 / 清空（命令栈）----
    def _push_cmd(self, cmd: _Cmd):
        """压入一条撤销命令（清空重做栈，裁剪到上限）。"""
        self._undo.append(cmd)
        if len(self._undo) > UNDO_LIMIT:
            self._undo.pop(0)
        self._redo.clear()

    def undo(self):
        if not self._undo:
            return
        # 编辑态下撤销：data 布局即将变化，_editing_index/_hidden_edit_items
        # 会失效（提交时越界或替换错块）→ 先取消编辑
        if self._text_edit is not None:
            self.cancel_text_edit()
        cmd = self._undo.pop()
        cmd.unapply(self.data)
        self._redo.append(cmd)
        # 选中索引随数据变化失效：_render_all 只清选中框不清索引，残留的
        # 旧索引会让 delete_selected 边界检查「通过」但删掉别的标注
        self._selected_idx = None
        self._clear_select_box()
        self._render_all()

    def redo(self):
        if not self._redo:
            return
        if self._text_edit is not None:
            self.cancel_text_edit()
        cmd = self._redo.pop()
        cmd.apply(self.data)
        self._undo.append(cmd)
        self._selected_idx = None
        self._clear_select_box()
        self._render_all()

    def clear(self):
        if not self.data:
            return
        if self._text_edit is not None:
            self.cancel_text_edit()
        self._push_cmd(_ClearCmd(self.data))
        self.data = []
        self._selected_idx = None
        self._clear_select_box()
        self._render_all()

    # ---- 鼠标绘制 ----
    def mouse_press(self, pt: QPointF):
        # 若仍在文字编辑态，先提交退出（点其它元素/切工具都应先退出当前编辑）。
        # 否则编辑态残留、旧项被隐藏，会导致后续命中错乱、选不中其它元素。
        if self._text_edit is not None:
            self._commit_text()
        if self.tool == "select":
            self._on_select_press(pt)
            return
        if self.tool == "text":
            # 单击 = 单行文字；按住拖动 = 文本框（release 判定）
            self._press_pt = pt
            self._text_drag_rect = None
            self._drawing = {"type": "text"}
            self._draw_item = None
            return
        # 序号工具：单击即放置一个序号（无需拖动），编号 = 起始序号 + 已有数量
        if self.tool == "num":
            self._place_num(pt)
            return
        # 马赛克·画笔模式：涂抹式，路径上连续打码
        if self.tool == "mosaic" and self.mosaic_mode == "pen":
            d = {"type": "mosaic_pen", "points": [[pt.x(), pt.y()]],
                 "color": _hex(QColor(*self.color)), "width": self.width,
                 "block": self.width}
            self._drawing = d
            self._draw_item = None
            return
        # 马赛克·文字框模式：一次点击对一个 OCR 文字框打码（无需拖动）
        if self.tool == "mosaic" and self.mosaic_mode == "textbox":
            box = self._hit_textbox(pt)
            if box is None:
                return
            x0, y0, x1, y1 = box
            d = {"type": "mosaic", "color": _hex(QColor(*self.color)),
                 "width": self.width, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                 "block": self.width}
            self._push_cmd(_AddCmd(len(self.data), d))
            self.data.append(d)
            self._render_all()
            return
        d = {"type": self.tool, "color": _hex(QColor(*self.color)),
             "width": self.width,
             "style": self.tool_styles.get(self.tool, "")}
        if self.tool == "pen":
            d["points"] = [[pt.x(), pt.y()]]
        else:
            d.update(x0=pt.x(), y0=pt.y(), x1=pt.x(), y1=pt.y())
            if self.tool == "mosaic":
                d["block"] = self.width
        self._drawing = d
        self._draw_item = None

    def _place_num(self, pt: QPointF):
        """序号工具：在点击处放置一个序号标注。

        编号 = **max(现有最大序号 + 1, 起始序号)**。
        原先用「起始序号 + 已有序号数」：删掉中间某个序号后二者不再等价 ——
        1/2/3 删掉 2，再放置得到 1+2=3，与已存在的 3 **重复**；用户又常先删后加
        （改错位置就删了重放），所以必然撞上。取「最大序号 + 1」保证永不复用；
        再与起始序号取大，保留它「最低起始序号」的原语义（用户抬高起始序号后，
        即使不做重排，新放的也不会低于它）。
        """
        nums = [int(d.get("num", 0))
                for d in self.data if d.get("type") == "num"]
        num = max(max(nums) + 1, self.num_start) if nums else self.num_start
        d = {"type": "num", "x": pt.x(), "y": pt.y(),
             "num": num,
             "style": self.num_style,
             "color": _hex(QColor(*self.color)),
             "font_size": self.num_size,
             "font": self.font_family, "bold": True}
        self._push_cmd(_AddCmd(len(self.data), d))
        self.data.append(d)
        self._render_all()

    def _hit_textbox(self, pt: QPointF):
        """文字框打码：返回 pt 命中的 OCR 文字框 bbox (x0,y0,x1,y1)；无则 None。"""
        boxes = getattr(self.view, "boxes", None) or []
        for b in boxes:
            poly = getattr(b, "polygon", None)
            if not poly:
                continue
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            if min(xs) <= pt.x() <= max(xs) and min(ys) <= pt.y() <= max(ys):
                return min(xs), min(ys), max(xs), max(ys)
        return None

    def mouse_move(self, pt: QPointF):
        if self.tool == "select":
            self._select_move(pt)
            return
        if self._drawing is None:
            return
        if self.tool == "text":
            # 拖动：显示文本框虚线预览（单击不出现）
            r = QRectF(self._press_pt, pt).normalized()
            self._text_drag_rect = r
            if self._draw_item is not None:
                self.scene.removeItem(self._draw_item)
                self._draw_item = None
            if r.width() >= 5 and r.height() >= 5:
                self._draw_item = QGraphicsRectItem(r)
                self._draw_item.setPen(
                    QPen(QColor(120, 120, 140, 170), 1, Qt.DashLine))
                self._draw_item.setBrush(QBrush(QColor(0, 0, 0, 12)))
                self._draw_item.setZValue(6)
                self.scene.addItem(self._draw_item)
            return
        if self.tool == "pen" or self._drawing.get("type") == "mosaic_pen":
            self._drawing["points"].append([pt.x(), pt.y()])
        else:
            self._drawing["x1"] = pt.x()
            self._drawing["y1"] = pt.y()
        # 马赛克·画笔：拖动预览只**增量追加新点**的块（组内 addToGroup）。
        # 原实现每帧走 _make_item → 对全部已累积点重新 copy + 两次 scaled，
        # 复杂度 O(点数²)：涂抹 200 点、鼠标 60Hz 时每秒上万次像素块操作，
        # 越涂越卡。改为 O(点数) 后帧成本恒定。
        if self._drawing.get("type") == "mosaic_pen":
            self._append_mosaic_pen_point(pt)
            return
        if self._draw_item is not None:
            self.scene.removeItem(self._draw_item)
            self._draw_item = None
        # 马赛克·框选模式：拖动时显示空心虚线框（画框式，同矩形工具），
        # 松手才生成马赛克；画笔模式走 _make_item 实时显示涂抹块。
        if (self.tool == "mosaic" and self._drawing is not None
                and self._drawing.get("type") == "mosaic"):
            r = QRectF(self._drawing["x0"], self._drawing["y0"],
                       self._drawing["x1"] - self._drawing["x0"],
                       self._drawing["y1"] - self._drawing["y0"]).normalized()
            self._draw_item = QGraphicsRectItem(r)
            self._draw_item.setPen(QPen(QColor(*self.color), 1, Qt.DashLine))
            self._draw_item.setBrush(QBrush(QColor(*self.color, 32)))
            self._draw_item.setZValue(6)
            self.scene.addItem(self._draw_item)
            return
        if self._drawing is not None:
            made = self._make_item(self._drawing)
            if isinstance(made, tuple):
                # 箭头：线 + 三角组合为 group，拖动时**实时显示完整箭头**
                self._draw_item = QGraphicsItemGroup()
                for it in made:
                    self._draw_item.addToGroup(it)
            else:
                self._draw_item = made
            if self._draw_item is not None:
                self._draw_item.setZValue(6)
                self.scene.addItem(self._draw_item)

    def _append_mosaic_pen_point(self, pt: QPointF):
        """马赛克·画笔拖动预览：只把**新点**的马赛克块追加进预览组。

        与 mouse_release 的最终图元（_MosaicPenElement）使用同一块尺寸与
        定位公式，保证预览与落定结果一致；组由本方法按需创建。
        """
        if self._draw_item is None:
            self._draw_item = QGraphicsItemGroup()
            self._draw_item.setZValue(6)
            self.scene.addItem(self._draw_item)
        block = float(self._drawing.get("block", self.width) or self.width)
        r = QRectF(pt.x() - block / 2.0, pt.y() - block / 2.0, block, block)
        item = QGraphicsPixmapItem(_make_mosaic_pixmap(self.base, r, block))
        item.setPos(r.topLeft())
        self._draw_item.addToGroup(item)

    def mouse_release(self, _pt: QPointF):
        if self.tool == "select":
            self._select_release(_pt)
            return
        if self._drawing is None:
            return
        if self._draw_item is not None:
            self.scene.removeItem(self._draw_item)
            self._draw_item = None
        if self.tool == "text":
            d = self._drawing
            self._drawing = None
            self._editing_index = None     # 新建文字块（非编辑已有）
            r = self._text_drag_rect
            if r is not None and r.width() >= 5 and r.height() >= 5:
                # 按住拖动 → 文本框（固定宽度，支持换行/自动换行）
                self._start_text_edit(QPointF(r.x(), r.y()), width=r.width())
            else:
                # 单击 → 单行文字（现状：就地编辑）
                self._start_text_edit(QPointF(self._press_pt))
            return
        d = self._drawing
        self._drawing = None
        # 归一化坐标（反向拖动时 x1<x0 / y1<y0），保证矩形类工具方向正确。
        # 注意：arrow 不归一化——尖端在点击处（x0,y0）有方向语义，归一化会破坏方向；
        # line 同样不归一化——x/y 独立排序会把线段变成包围盒对角线（斜率翻转）。
        if d["type"] in ("rect", "mosaic"):
            d["x0"], d["x1"] = sorted((d["x0"], d["x1"]))
            d["y0"], d["y1"] = sorted((d["y0"], d["y1"]))
        # 过小过滤（pen/mosaic_pen 不过滤）。按工具分两档，不能一律用 and 或 or：
        # - 矩形/框选马赛克：**任一边**过小都是误拖（如 300×2 的细条），用 or
        # - 线段/箭头：细长是合法的（水平线 dy≈0、垂直线 dx≈0），只有两端
        #   几乎重合才算误触，必须用 and —— 改成 or 会让所有水平/垂直线失效
        # 注意：dx/dy 只能在分类分支内取（pen/mosaic_pen 的 d 只有 points，无 x0/x1）
        if d["type"] in ("rect", "mosaic"):
            if abs(d["x1"] - d["x0"]) < 3 or abs(d["y1"] - d["y0"]) < 3:
                return
        elif d["type"] in ("line", "arrow"):
            if abs(d["x1"] - d["x0"]) < 3 and abs(d["y1"] - d["y0"]) < 3:
                return
        self._push_cmd(_AddCmd(len(self.data), d))
        self.data.append(d)
        self._render_all()

    # ---- 选择工具（select）：选中/拖动移动/删除标注 ----
    def _on_select_press(self, pt: QPointF):
        idx = self._hit_anno_idx(pt)
        if idx is not None and not (0 <= idx < len(self.data)):
            idx = None                       # 防御：索引越界则视为未命中
        self._selected_idx = idx
        self._select_moved = False
        if idx is not None:
            self._select_press = pt
            self._select_start_pt = pt
            # 缓存选中项（含箭头 line+head、文本框 group 等所有顶层 item），
            # 拖动时只对这些 moveBy，O(选中项数) 而非 O(总标注数)，保证跟手。
            self._select_items = [it for it in self._items
                                  if getattr(it, "_anno_idx", None) == idx]
            self._show_select_box(idx)
        else:
            self._select_press = None
            self._select_start_pt = None
            self._select_items = []
            self._clear_select_box()

    def _select_move(self, pt: QPointF):
        # 拖动中实时 moveBy 选中项（仅改 transform，极轻量），元素跟手；
        # data 只在 release 时一次性写回，避免拖动中反复全量重建导致卡顿。
        if self._selected_idx is None or self._select_press is None:
            return
        delta = pt - self._select_press
        if delta.manhattanLength() == 0:
            return
        if not self._select_moved:
            self._clear_select_box()       # 首次真正拖动：隐藏选中框，避免拖尾
            self._select_moved = True
        for it in self._select_items:
            it.moveBy(delta.x(), delta.y())
        self._select_press = pt

    def _select_release(self, pt: QPointF):
        if self._selected_idx is not None and self._select_moved \
                and self._select_start_pt is not None:
            dx = pt.x() - self._select_start_pt.x()
            dy = pt.y() - self._select_start_pt.y()
            if dx or dy:
                self._push_cmd(_MoveCmd(self._selected_idx, dx, dy))
                self._move_data(self._selected_idx, dx, dy)
                # 不重建：item 已实时 moveBy 到目标位置，坐标与 data 一致。
        self._select_press = None
        self._select_start_pt = None
        self._select_moved = False
        self._select_items = []
        if self._selected_idx is not None:
            self._show_select_box(self._selected_idx)

    def _move_data(self, idx: int, dx: float, dy: float):
        d = self.data[idx]
        t = d["type"]
        if t in ("rect", "arrow", "line", "mosaic"):
            d["x0"] = d["x0"] + dx
            d["x1"] = d["x1"] + dx
            d["y0"] = d["y0"] + dy
            d["y1"] = d["y1"] + dy
        elif t in ("pen", "mosaic_pen"):
            d["points"] = [[x + dx, y + dy] for x, y in d["points"]]
        elif t in ("text", "num"):
            d["x"] = d["x"] + dx
            d["y"] = d["y"] + dy

    def _hit_anno_idx(self, pt: QPointF):
        """命中检测（仅标注层）：返回 pt 处标注项的 data 索引；无则 None。

        只遍历 self._items（标注层列表），不调用 scene.items() 跨层命中；
        倒序遍历（后画的视觉上层优先）。
        用 sceneBoundingRect + 4px 容差命中（而非精确 contains）——箭头/画笔等
        细线型 item 的精确命中区域极小，点偏一点就选不中；包围盒命中更宽松易选。
        """
        for it in reversed(self._items):
            idx = getattr(it, "_anno_idx", None)
            if idx is None:
                continue
            try:
                r = it.sceneBoundingRect()
                if r.isNull():
                    continue
                if r.adjusted(-4, -4, 4, 4).contains(pt):
                    return idx
            except Exception:            # noqa: BLE001
                continue
        return None

    def _show_select_box(self, idx: int):
        self._clear_select_box()
        rects = [it.sceneBoundingRect() for it in self._items
                 if getattr(it, "_anno_idx", None) == idx]
        if not rects:
            return
        r = rects[0]
        for rr in rects[1:]:
            r = r.united(rr)
        box = QGraphicsRectItem(r.adjusted(-4, -4, 4, 4))
        box.setPen(QPen(QColor(90, 150, 255), 1.4, Qt.DashLine))
        box.setBrush(Qt.NoBrush)
        box.setZValue(10)
        self.scene.addItem(box)
        self._select_box = box

    def _clear_select_box(self):
        if self._select_box is not None:
            self.scene.removeItem(self._select_box)
            self._select_box = None

    def delete_selected(self) -> bool:
        """删除当前选中的标注（Delete/Backspace）。返回是否删除。"""
        if self._selected_idx is None \
                or not (0 <= self._selected_idx < len(self.data)):
            self._selected_idx = None
            self._clear_select_box()
            return False
        self._push_cmd(_DeleteCmd(self._selected_idx,
                                  copy.deepcopy(self.data[self._selected_idx])))
        self.data.pop(self._selected_idx)
        self._selected_idx = None
        self._clear_select_box()
        self._render_all()
        return True

    # ---- 双击编辑已有文字（画其它标注后还能回来改文字）----
    def mouse_double_click(self, pt: QPointF):
        """双击已提交的文字（单行/文本框）→ 重新进入编辑，数据块原地替换。"""
        if self._text_edit is not None:
            # 双击序列中第一次 release 已创建了空编辑框 → 取消它再编辑已有项；
            # 若已有内容（用户正在打字时双击）则不打断。
            if self._text_edit.toPlainText() == "":
                self.cancel_text_edit()
            else:
                return
        it = self._hit_text_item(pt)
        if it is None:
            return
        idx = getattr(it, "_anno_idx", None)
        if idx is None or not (0 <= idx < len(self.data)):
            return
        d = self.data[idx]
        self._editing_index = idx
        self._selected_idx = None
        self._clear_select_box()
        # 隐藏旧渲染项（含文本框 group 及其边框），避免与新的编辑态 item 叠加产生重影；
        # 提交时 _render_all 会 removeItem 这些旧项；取消时 _cancel 恢复显示。
        self._hidden_edit_items = [i for i in self._items
                                   if getattr(i, "_anno_idx", None) == idx]
        for i in self._hidden_edit_items:
            i.setVisible(False)
        self._start_text_edit(QPointF(d["x"], d["y"]),
                              width=d.get("text_width"))
        self._text_edit.setPlainText(d["text"])
        try:
            from PySide6.QtGui import QTextCursor
            cur = self._text_edit.textCursor()
            cur.movePosition(QTextCursor.End)
            self._text_edit.setTextCursor(cur)
        except Exception:                    # noqa: BLE001
            pass

    def _hit_text_item(self, pt: QPointF):
        """命中检测（仅标注层）：返回 pt 处已提交的文字项（含文本框 group 内文本项）。
        倒序遍历（后画的视觉上层优先）；包围盒 + 容差命中（与 _hit_anno_idx 一致）。
        仅命中 type=="text" 的数据块——序号/其它含文本子项的图元不允许双击
        进入文字编辑（各自成类，防止误编辑转成 text）。"""
        for it in reversed(self._items):
            idx = getattr(it, "_anno_idx", None)
            if idx is not None and 0 <= idx < len(self.data) \
                    and self.data[idx].get("type") != "text":
                continue                      # 非文字块（如序号）：不可文字编辑
            try:
                r = it.sceneBoundingRect()
                if r.isNull() or not r.adjusted(-4, -4, 4, 4).contains(pt):
                    continue
                if hasattr(it, "toPlainText"):
                    return it
                for ch in it.childItems():
                    if hasattr(ch, "toPlainText"):
                        return ch
            except Exception:            # noqa: BLE001
                continue
        return None

    # ---- 文字就地编辑（text 工具：单击=单行 / 拖动=文本框，失焦提交）----
    def _start_text_edit(self, pt: QPointF, width: float | None = None):
        self.cancel_text_edit()          # 清理未提交的旧编辑
        item = QGraphicsTextItem()
        f = QFont(self.font_family)
        f.setPixelSize(self.font_size)
        f.setBold(self.font_bold)
        f.setItalic(self.font_italic)
        item.setFont(f)
        item.setDefaultTextColor(QColor(*self.color))
        item.setTextInteractionFlags(Qt.TextEditorInteraction)
        if width:
            item.setTextWidth(width)     # 文本框：固定宽度，Enter 换行/自动换行
        item.setPos(pt.x(), pt.y())
        item.setZValue(7)
        self.scene.addItem(item)
        self._text_edit = item
        self._text_edit_box = None
        if width:
            # 文本框编辑态：显示可见虚线边框（随内容增长），明确"这是文本框"。
            # 用 sceneBoundingRect（含 setPos 位置），否则边框画在场景原点=屏幕左上角。
            sb = item.sceneBoundingRect()
            box = QGraphicsRectItem(sb.adjusted(-3, -3, 3, 3))
            box.setPen(QPen(QColor(120, 120, 140, 190), 1, Qt.DashLine))
            box.setBrush(Qt.NoBrush)
            box.setZValue(7)
            self.scene.addItem(box)
            self._text_edit_box = box
            try:
                # 槽必须是**绑定方法**，不能是 `lambda: self._sync_text_box(box, item)`：
                # 闭包会把 box/item 强引用住，而闭包被 document（C++ 侧）持有、
                # document 又归 item 所有 —— `item ⇄ document → lambda → item`
                # 是**跨 C++/Python 的引用环**：Python 的 gc 看不到 C++ 那一半，
                # refcount 也永远不归零（实测 `Connection.disconnect()` 同样无效 ——
                # PySide 把可调用对象留在发送者的内部登记表里直到发送者析构，而发送者
                # 正被这个环吊着）→ 每编辑一次就地文字就泄漏一组 item/document/边框。
                # 绑定方法只强引用 self（会话，本就长活），item/box 收尾时随引用
                # 消失自然析构，因此也无需保存/断开连接对象。
                item.document().contentsChanged.connect(self._on_edit_text_changed)
            except Exception as e:       # noqa: BLE001 连不上不该阻断进入编辑态
                log.warning("文本框边框跟随连接失败（编辑时边框不会随内容增长）：%s", e)
        item.setFocus()                  # 光标进入，可直接输入

    def _sync_text_box(self, box, item):
        """文本框边框跟随文本内容尺寸（场景坐标，跟随 text 实际位置）。"""
        try:
            sb = item.sceneBoundingRect()
            box.setRect(QRectF(sb.x() - 3, sb.y() - 3,
                               sb.width() + 6, sb.height() + 6))
        except Exception:                # noqa: BLE001
            pass

    def _on_focus_changed(self, new, old, reason):
        # 编辑器失焦（点击其它位置/切换到别处）→ 提交
        if self._text_edit is not None and new is not self._text_edit:
            self._commit_text()

    def _on_edit_text_changed(self):
        """就地编辑内容变化 → 虚线边框跟随（contentsChanged 的槽，见 _start_text_edit）。

        用绑定方法而非闭包：不捕获 item/box，避免跨 C++/Python 的引用环。
        目标对象从 `_text_edit*` 字段现取，收尾后为 None 即自动失效。
        """
        if self._text_edit is not None and self._text_edit_box is not None:
            self._sync_text_box(self._text_edit_box, self._text_edit)

    def _commit_text(self):
        it = self._text_edit
        self._text_edit = None
        if it is None:
            return
        if self._text_edit_box is not None:
            self.scene.removeItem(self._text_edit_box)
            self._text_edit_box = None
        txt = it.toPlainText().strip()
        pos = it.pos()
        width = it.textWidth() if it.textWidth() > 0 else None
        self.scene.removeItem(it)
        if not txt:
            self._editing_index = None
            # 空文本：不更新 data，但需恢复双击编辑时隐藏的旧项可见
            for i in self._hidden_edit_items:
                try:
                    i.setVisible(True)
                except Exception:        # noqa: BLE001
                    pass
            self._hidden_edit_items = []
            return
        f = it.font()
        d = {"type": "text", "x": pos.x(), "y": pos.y(), "text": txt,
             "font_size": f.pixelSize() if f.pixelSize() > 0 else self.font_size,
             "font": f.family() or self.font_family,
             "color": _hex(QColor(*self.color)),
             "bold": f.bold(), "italic": f.italic()}
        if width:
            d["text_width"] = width
        if self._editing_index is not None:
            idx = self._editing_index
            self._editing_index = None
            # 防御复核：编辑期间 data 可能已被 undo/load 等路径改变，旧索引
            # 可能越界或不再指向文字块 → 越界时降级为追加新块而非崩溃/错替
            if 0 <= idx < len(self.data) \
                    and self.data[idx].get("type") == "text":
                self._push_cmd(_EditCmd(idx, self.data[idx], d))
                self.data[idx] = d               # 原地替换：编辑已有文字块
            else:
                self._push_cmd(_AddCmd(len(self.data), d))
                self.data.append(d)              # 索引失效：按新增处理
        else:
            self._push_cmd(_AddCmd(len(self.data), d))
            self.data.append(d)              # 新增文字块
        self._hidden_edit_items = []          # 旧项已由 _render_all 移除，清空引用
        self._render_all()

    def cancel_text_edit(self) -> bool:
        """取消进行中的文字编辑。返回是否确实取消了。"""
        if self._text_edit is None:
            return False
        if self._text_edit_box is not None:
            self.scene.removeItem(self._text_edit_box)
            self._text_edit_box = None
        self.scene.removeItem(self._text_edit)
        self._text_edit = None
        self._editing_index = None
        # 恢复双击编辑时隐藏的旧渲染项（编辑取消 → 旧内容重新显示）
        for i in self._hidden_edit_items:
            try:
                i.setVisible(True)
            except Exception:            # noqa: BLE001
                pass
        self._hidden_edit_items = []
        return True

# ---- 对外再导出：拆分前从本文件 import 的名字全部保留 ----
__all__ = [
    '_BaseElement', '_norm_offsets', '_text_item_ink_rect',
    '_dedupe_points', '_offset_polyline', '_resample_points',
    '_polyline_item', '_line_sine_item', '_line_zigzag_item',
    '_bubble_path', '_RectElement', '_ArrowElement', '_PenElement',
    '_LineElement', '_MosaicElement', '_MosaicPenElement',
    '_TextElement', '_NumElement', '_ItemFactory', 'TOOLS',
    'TOOL_LABELS', 'TOOL_TIPS', 'render_annotated_image', '_hex',
    '_make_mosaic_pixmap',
    '_ColorSizeRow', '_AnnoPopupBase', '_FontPopup', '_MosaicModePopup',
    '_StyleIconRow', '_ToolOptionsPopup', '_NumPopup',
    'AnnotationSession', 'ANNO_VERSION', 'UNDO_LIMIT',
    'SERIALIZABLE_TYPES', '_Cmd', '_AddCmd', '_DeleteCmd', '_MoveCmd',
    '_EditCmd', '_ClearCmd', '_RenumberCmd', 'anno_to_json',
    'anno_from_json'
]
