"""M1-3 / M1-4 贴图浮窗 + 编辑器式文本选区 + 点击/选择复制。

行为（像记事本一样选文字）：
- 按下设锚点 → 拖动按字符扩展选区（字符位置按文本长度等比近似）。
- 任意方向（反向自动归一化为阅读顺序）。
- 跨行 / 多行选择。
- 拖出框外自动扩展到首行 / 末行。
- 双击选中整行（整块）。
- Ctrl+单击 / 拖拽 追加多区域（Excel 式，按选择顺序换行拼接）。
- 无 Ctrl 点击空白清除选区。
- 选区成形即复制到剪贴板；Esc 关闭（有选区时先清除选区）。

坐标约定：场景坐标 = 截图原始像素（与 OcrBox.polygon 同坐标系）。
窗口逻辑尺寸 = 原始/dpr，场景显式 scale(1/dpr)，故原始像素 1:1 还原到屏幕，
mapToScene 反算仍回原始像素，命中检测一致。
"""
from __future__ import annotations

import math

import cv2
import numpy as np
from PySide6.QtCore import Qt, QEvent, QPoint, QPointF, QRectF, Signal, QTimer
from PySide6.QtGui import (
    QColor, QBrush, QPen, QImage, QPixmap, QPainter, QPainterPath, QPolygonF,
    QFont, QFontMetricsF,
)
from PySide6.QtWidgets import QApplication, QGraphicsView, QGraphicsScene, \
    QGraphicsPolygonItem, QGraphicsRectItem, QGraphicsItem, QGraphicsTextItem, \
    QPushButton, QFrame, QWidget

from config import ConfigManager
from platform_compat import IS_WINDOWS
from platform_compat.fonts import ui_font_family
from ui.base.hotkey_keys import parse_qt_combo
from ui.base.widgets import screen_available_geometry, desktop_union_available
from ui.core.table_detect import (          # 表格检测纯算法（无 Qt 依赖）
    _bbox, _bbox_center, build_blobs,
    _detect_table_lines, _coords_table_grid, map_cell_texts,
)


def _qt_flag_value(flag) -> int:
    """Qt 标志枚举 → int。

    PySide6 6.x 起 `Qt.KeyboardModifier` 是 Python `enum.Flag`（不再是 IntEnum），
    `int(Qt.ShiftModifier)` 直接抛 TypeError；必须取 `.value`。`Qt.Key` 两者
    都支持，统一走本函数避免再次踩坑。
    """
    return flag.value if hasattr(flag, "value") else int(flag)


# ---- 字符宽度度量缓存（set_boxes 全量重算的收敛点）----
# _char_intervals 每块都要字体度量：<family, pixel_size> → QFont / QFontMetricsF
# 各只建一次；单个字符的 advance 也按同一 key 缓存（字符集有限：ASCII + 常用汉字）。
# 数值与「每次新建 font/metrics 现算」逐位一致（同样的 pixelSize / 同样的 family
# 回退链），因此文字选区几何不变。
_FONT_CACHE: dict = {}
_FM_CACHE: dict = {}
_CHAR_ADV_CACHE: dict = {}
_CHAR_ADV_CACHE_MAX = 8192     # 每个 (family, pixel_size) 最多缓存的字符数


def _font_and_metrics(family: str, pixel_size: int):
    """按 (family, pixel_size) 复用 QFont / QFontMetricsF（各只建一次）。"""
    key = (family, pixel_size)
    fm = _FM_CACHE.get(key)
    if fm is not None:
        return fm
    font = QFont(family)
    font.setPixelSize(pixel_size)
    _FONT_CACHE[key] = font            # 持有 QFont 引用：QFontMetricsF 不接管所有权
    fm = QFontMetricsF(font)
    _FM_CACHE[key] = fm
    return fm


def _char_advance(key, fm, ch: str) -> float:
    """单字符 advance（进程级缓存）；缓存满则直接现算，不无限增长。"""
    cache = _CHAR_ADV_CACHE.get(key)
    if cache is None:
        cache = _CHAR_ADV_CACHE[key] = {}
    w = cache.get(ch)
    if w is None:
        w = fm.horizontalAdvance(ch)
        if len(cache) < _CHAR_ADV_CACHE_MAX:
            cache[ch] = w
    return w


class _BorderOverlay(QWidget):
    """贴图窗口边框覆盖层（替代原「图片四周边框」）。

    - 透明中心、仅描窗口最外圈一圈边框；鼠标事件穿透（WA_TransparentForMouseEvents）
      到下方视口，不影响边缘拉伸 / 文字选取 / 平移等交互。
    - 边框颜色 / 宽度由外部 lambda 实时提供（来自 config.ini [ui]），
      与图片尺寸、对齐方式无关，始终框住整个贴图窗口。
    - 用 4 条填充矩形做纯几何覆盖（同原场景内边框方案），避免描边 sub-pixel
      偏移、高 DPI 下被抹细的问题，宽度稳定。
    """

    def __init__(self, parent, get_color, get_width):
        super().__init__(parent)
        self._get_color = get_color
        self._get_width = get_width
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        bw = self._get_width()
        if bw <= 0:
            return
        p = QPainter(self)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self._get_color()))
        r = self.rect()
        # 四边填充矩形（中心留空 → 透出下方视口/图片）
        p.drawRect(QRectF(0, 0, r.width(), bw))                 # 上
        p.drawRect(QRectF(0, r.height() - bw, r.width(), bw))    # 下
        p.drawRect(QRectF(0, 0, bw, r.height()))                # 左
        p.drawRect(QRectF(r.width() - bw, 0, bw, r.height()))    # 右


class _SelectionHighlightItem(QGraphicsItem):
    """一条文字选区的高亮（一个场景项 / 一条选区）。

    原实现按**字符**建 QGraphicsRectItem：一屏 200 块 × 20 字全选即上千个场景项，
    拖动时每帧 `removeItem` 全部再重建。这里把该选区的所有字符矩形累加进一个
    QPainterPath，项数从「字符数」降为「选区数」，绘制/索引/析构成本随之收敛。

    视觉等价性：填充色 / 不透明度 / z 值 / 抗锯齿设置与逐字符 drawRect 完全一致；
    差别只在**相邻字符共享边**处——逐个矩形绘制时交界列被两次半透明叠加（更浅），
    单一路径取并集则一次成形（无缝）。填充规则用 WindingFill（非默认的
    OddEvenFill）：多块文字的矩形可能重叠，奇偶规则会在重叠处挖空。
    """

    def __init__(self, path: QPainterPath):
        super().__init__()
        self._path = path
        self._brush = QBrush(QColor(0, 120, 255, 120))
        self.setZValue(1)                  # 第 2 层（选区高亮，同 OCR 层）

    def boundingRect(self) -> QRectF:
        return self._path.boundingRect()   # 无描边（与改前 QPen(Qt.NoPen) 一致）

    def paint(self, painter, option, widget=None):
        painter.fillPath(self._path, self._brush)


class ResultOverlay(QGraphicsView):
    closed = Signal()
    copied = Signal(str)
    reocr_requested = Signal()          # Ctrl+R：请求重新抓取当前区域并 OCR
    table_requested = Signal()          # Ctrl+Shift+T：请求表格识别（M2 由 M4 提前）
    table_window_requested = Signal()   # 双击图片 / 长按表格按钮：打开表格窗口
    fav_requested = Signal()            # 状态栏收藏按钮：收藏当前截图
    pin_toggled = Signal(bool)          # 钉图按钮单击：切换钉图（隐藏图片+状态栏+工具栏，只留钉子）
    annotations_changed = Signal(str)   # 标注完成/退出时携带 JSON
    ocr_needed = Signal()               # 无 OCR 结果时点「文字框/表格」：请求临时拉起 OCR
    done_requested = Signal(object)     # 完成按钮：请求保存历史并关闭（object=self）

    def __init__(self, img_bgr: np.ndarray, raw_bbox: tuple, dpr: float,
                 config: ConfigManager | None = None):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose, True)   # close 后自动 deleteLater（防泄漏）
        # 注意：样式必须带 #ResultOverlay 选择器——无选择器的规则会传播到
        # 所有子控件（如右上角关闭按钮），把它们的背景也覆盖成透明。
        self.setStyleSheet("#ResultOverlay { background: transparent; }")
        # viewport 也保持透明（无选择器样式原本会覆盖到它，现在显式设置）
        self.viewport().setAutoFillBackground(False)
        self.viewport().setAttribute(Qt.WA_TranslucentBackground)
        # 去掉 QGraphicsView 默认的 1px QFrame 边框：否则 viewport 从 (1,1) 偏移、
        # 尺寸比 view 小 2px，导致图片四周的边框错位、右/下 2px 被裁掉。
        self.setFrameShape(QFrame.NoFrame)
        self.setRenderHint(QPainter.Antialiasing)
        # 图片位置：窗口>图片时居中（AlignCenter）；图片>窗口时滚动条/中键拖动查看
        self.setAlignment(Qt.AlignCenter)
        self.setMouseTracking(True)   # 悬停状态带时切换抓手光标
        # 导航图：滚动条平移/滚轮滚动后同步视口框
        self.horizontalScrollBar().valueChanged.connect(self._update_navigator)
        self.verticalScrollBar().valueChanged.connect(self._update_navigator)
        # 滚动条事件拦截：解锁态图片>窗口时滚动条占据窗口右/下边缘，原生会截获
        # 鼠标事件导致右/下/角无法边缘拉伸（已知 Qt 结构限制）。给滚动条装事件
        # 过滤器，最外侧 _EDGE px 转交边缘拉伸（_start_edge_resize 隐藏滚动条后
        # 由 viewport 接管拖动）；其余区域滚动条照常滚动。
        self.verticalScrollBar().installEventFilter(self)
        self.horizontalScrollBar().installEventFilter(self)
        # 注：原先此处还给 QApplication 装了 app 级事件过滤器（每个贴图窗口一个，
        # 最多 7 个共存 → 应用内**任何**事件都被逐个过滤一遍），唯一用途是
        # 「边缘拉伸时在窗口外松左键」的兜底。经论证该兜底冗余，已移除：
        #   1) _start_edge_resize 里 `self.viewport().grabMouse()` 显式抓取
        #      viewport（Windows 上即 SetCapture），松手事件必定回到 viewport，
        #      再经 QAbstractScrollArea 转发到本视图的 mouseReleaseEvent
        #      → _end_edge_resize（它自己会 releaseMouse）；
        #   2) 即便没有显式抓取，Windows 也会把 button-up 送回收到 button-down
        #      的窗口，Qt 侧的隐式 grabber（收到 press 的 widget 在按钮按下期间
        #      一直收到后续鼠标事件）同样保证 release 回到 viewport；
        #   3) 滚动条边沿拉伸那条分支不依赖 app 级过滤：滚动条的事件过滤器装在
        #      滚动条**对象**上（上面两行），仍在。
        # 若日后真出现「松手在窗口外 → 窗口卡在拉伸态」，恢复方式 = 这两行
        # （`QApplication.instance().installEventFilter(self)`）加回来即可。

        self.img = img_bgr          # 保持引用，避免 QImage 底层 buffer 被回收
        self.raw_bbox = raw_bbox
        self.dpr = dpr
        # 贴图窗口边框外观（overlay，config.ini [ui]，不写死）
        cfg = config or ConfigManager()
        self.config = cfg   # 供标注等子系统读写配置（记忆马赛克模式等）
        self._overlay_border_color = QColor(*cfg.getcolor(
            "ui", "overlay_border_color", (100, 190, 255)))
        self._overlay_border_width = cfg.getint("ui", "overlay_border_width", 1)
        # 二维码框颜色（[ui] qr_border_color，默认紫，M3 T6）+ 表格模式
        # 网格颜色（[ui] table_border_color，默认绿）
        self._qr_border_color = QColor(*cfg.getcolor(
            "ui", "qr_border_color", (156, 39, 176)))
        self._table_border_color = QColor(*cfg.getcolor(
            "ui", "table_border_color", (90, 220, 120)))
        # 二维码识别开关（[software] enable_qr，默认关；工具栏按钮/Q 键切换，跨会话记忆）
        self._qr_enabled = cfg.getbool("software", "enable_qr", False)
        # 贴图窗口边框（替代原「图片四周边框」）：在窗口最外层画一圈边框，
        # 与图片尺寸/对齐无关，始终框住窗口。覆盖层透明中心、仅描边、鼠标穿透。
        if self._overlay_border_width > 0:
            self._border_overlay = _BorderOverlay(
                self,
                lambda: self._overlay_border_color,
                lambda: self._overlay_border_width,
            )
            self._border_overlay.setGeometry(self.rect())
            self._border_overlay.raise_()
        else:
            self._border_overlay = None
        # 识别文字蓝色框默认显示（[ui] show_text_boxes，贴图按钮可临时切换）
        self._show_boxes = cfg.getbool("ui", "show_text_boxes", True)
        # 截图后自动 OCR（[software] ocr_auto）：关闭时新贴图没有识别结果，
        # 「文字框」按钮不应默认点亮（点亮=已在显示框，会误导），由用户点击触发识别
        self._auto_ocr = cfg.getbool("software", "ocr_auto", True)
        # M2：复制快捷键（[hotkey_overlay] copy_shortcut，默认 shift+c，可改键/可关闭；
        #      Ctrl+A 固定为全选，Ctrl+C 固定为复制）
        self._copy_shortcut = parse_qt_combo(
            cfg.get("hotkey_overlay", "copy_shortcut", "shift+c"))
        # 图片缩放修饰键（[hotkey_overlay] zoom_modifier：ctrl/alt/shift/off）
        # 按住修饰键+滚轮 = 窗口内图片缩放；直接滚轮 = 平移查看
        self._zoom_mod = str(cfg.get("hotkey_overlay", "zoom_modifier", "ctrl")
                             ).strip().lower()
        if self._zoom_mod not in ("ctrl", "alt", "shift", "off"):
            self._zoom_mod = "ctrl"
        # 窗口几何缩放修饰键（[hotkey_overlay] geom_zoom_modifier，默认 shift）
        self._geom_zoom_mod = str(
            cfg.get("hotkey_overlay", "geom_zoom_modifier", "shift")
        ).strip().lower()
        if self._geom_zoom_mod not in ("ctrl", "alt", "shift", "off"):
            self._geom_zoom_mod = "shift"
        # 分段快捷键（截图选择/贴图窗口/标注，设置里可改键/可关闭；off → None 不匹配）
        # 快捷键已按速查面板状态分组：贴图窗口组 [hotkey_overlay]、标注组 [hotkey_anno]
        _HK_SEC = {
            "select_all": "hotkey_overlay", "copy_selected": "hotkey_overlay",
            "reocr": "hotkey_overlay", "table_ocr": "hotkey_overlay",
            "toggle_anno": "hotkey_overlay", "undo_anno": "hotkey_anno",
            "redo_anno": "hotkey_anno", "copy_image": "hotkey_overlay",
            "copy_raw_image": "hotkey_overlay", "toggle_strip": "hotkey_overlay",
            "toggle_qr": "hotkey_overlay", "toggle_boxes": "hotkey_overlay",
            "toggle_table": "hotkey_overlay", "fav": "hotkey_overlay",
        }

        def _hk(key, default):
            c = cfg.get(_HK_SEC.get(key, "hotkey_overlay"), key, default)
            if not c or c.lower() in ("off", "none", "false"):
                return None
            return parse_qt_combo(c)
        self._select_all_hk = _hk("select_all", "ctrl+a")       # 全选文字
        self._copy_sel_hk = _hk("copy_selected", "ctrl+c")      # 复制选中文字
        self._reocr_hk = _hk("reocr", "ctrl+r")                 # 重新 OCR
        self._table_hk = _hk("table_ocr", "ctrl+shift+t")       # 表格识别
        self._anno_hk = _hk("toggle_anno", "ctrl+shift+d")      # 标注模式
        self._undo_hk = _hk("undo_anno", "ctrl+z")              # 标注撤销
        self._redo_hk = _hk("redo_anno", "ctrl+shift+z")        # 标注重做
        self._copy_image_hk = _hk("copy_image", "ctrl+shift+c")  # 复制整张图片（含标注）
        self._copy_raw_image_hk = _hk("copy_raw_image", "ctrl+alt+c")  # 复制原图（不含标注）
        self._strip_hk = _hk("toggle_strip", "s")               # 状态栏开关（仅本窗口）
        self._qr_hk = _hk("toggle_qr", "q")                     # 二维码识别开关（仅本窗口）
        self._boxes_hk = _hk("toggle_boxes", "w")               # 文字框开关（仅本窗口）
        self._tool_table_hk = _hk("toggle_table", "e")          # 表格开关（仅本窗口）
        self._fav_hk = _hk("fav", "f")                          # 收藏当前截图（仅本窗口）
        self._shortcut_panel = None

        self.boxes = []
        self.box_order = []        # 阅读顺序下的原始索引列表
        self.geom = {}             # orig_idx -> 几何信息
        self.ranges = []           # 选区列表：[o0,c0,o1,c1]（o 为阅读顺序位置）
        self.active = -1
        self.anchor = None         # (order_pos, char_idx)
        self.focus = None
        self.dragging = False
        self._drag_offset = None   # 非 None 表示正在拖动整个窗口（Alt+拖动）
        self._hl = []              # 动态高亮项
        self._outline = []         # 静态框线项
        self._table_mode = False   # 表格模式（规整网格 + 单元格选取 + TSV）
        self._table_boxes = []     # 表格网格线框项（绿色，无重叠）
        self._table_result = None  # 最近一次表格识别结果（TableResult）
        # 规整网格（从 cells 行列条带构建，体验同表格窗口，只读）
        self._table_rows = None    # [(y0,y1), ...] 每行条带（显示行序）
        self._table_cols = None    # [(x0,x1), ...] 每列条带
        self._table_row_ids = []   # 显示行序 → 真实 row 号
        self._table_col_ids = []   # 显示列序 → 真实 col 号
        self._tbl_sel = None       # 表格选中矩形 (r0,c0,r1,c1)
        self._tbl_anchor = None
        self._tbl_dragging = False
        self._tbl_sel_items = []   # 选中高亮矩形项
        self.history_id = None     # 关联的历史记录 id（Ctrl+R 覆盖用，M2·γ）
        self._ocr_done = False      # OCR 是否已完成（用于「操作才保存」判定）
        self._pending_save = False  # 用户操作过、等待 OCR 完成后落库
        self._persistent = False   # 持久窗体（历史打开的图）：截图不隐藏、不占贴图配额
        self._through = False      # 点击穿透（Windows WS_EX_TRANSPARENT，Alt+F 切换）
        self._nav_on = False       # 导航图开关（状态栏按钮）：缩略小地图
        self._navigator = None
        self._img_zoom = 1.0       # 窗口内图片缩放系数（Ctrl+滚轮，窗口尺寸不变）
        self._ratio_lock = True    # 等比例调节窗口（默认锁定：窗口与图片同比例）
        self._img_lw = 1.0         # 图片逻辑宽高（边缘拉伸锁定比例用）
        self._img_lh = 1.0
        self._resize_edge = None   # 边缘拉伸：当前命中的边/角（None/l/r/t/b/tl/tr/bl/br）
        self._resize_geom = None   # 边缘拉伸：按下时的 (全局点, 窗口几何)
        self._manual_resize = False  # 用户手动拉伸中（resizeEvent 里等比适配图片）
        self._pan_start = None     # 中键拖动平移起点（全局坐标, 滚动值）

        # ---- 浮窗组管理器：z 序 / 绘制让位 / 位置跟随统一收口 ----
        from ui.components.float_group import FloatGroup
        self._fgroup = FloatGroup(self)

        # ---- M2-7 标注模式 ----
        self._anno_mode = False
        self._anno = None          # AnnotationSession

        # 标注入口快捷键兜底（Qt.Tool 窗口默认不抢焦点，窗口内任意子控件
        # 获得焦点时也能触发；主路径仍是 keyPressEvent）。
        # 键位取自**同一份可配置热键**（_anno_hk / _table_hk），不再硬编码
        # ——原先写死 Ctrl+Shift+D / Ctrl+Shift+T，用户改键后旧键位仍然生效，
        # 同一功能出现两个键；configured 为 off 时也不该再注册。
        from PySide6.QtGui import QShortcut, QKeySequence

        def _seq(combo):
            """(mods, key) → QKeySequence；None（关闭）→ None。

            用 _qt_flag_value 取 Qt 枚举的数值：PySide6 6.x 的 Qt.KeyboardModifier
            是 enum.Flag，`int(...)` 会抛 TypeError（原实现此处直接崩溃）。
            """
            if combo is None:
                return None
            return QKeySequence(_qt_flag_value(combo[0]) | _qt_flag_value(combo[1]))

        self._sc_anno = None
        if _seq(self._anno_hk) is not None:
            self._sc_anno = QShortcut(_seq(self._anno_hk), self)
            self._sc_anno.setContext(Qt.WindowShortcut)
            self._sc_anno.activated.connect(self._toggle_annotation)
        # 表格识别入口（键位同上，默认 Ctrl+Shift+T）
        self._sc_table = None
        if _seq(self._table_hk) is not None:
            self._sc_table = QShortcut(_seq(self._table_hk), self)
            self._sc_table.setContext(Qt.WindowShortcut)
            self._sc_table.activated.connect(lambda: self.table_requested.emit())
        # 导航图开关（M，可配置 [hotkey_overlay] nav）：ApplicationShortcut
        # 保证焦点在状态栏按钮等子控件上时仍生效（文字编辑等控件会吃掉按键）
        # 滚动条已改为自动出现（图片超出窗口即显示），不再需要快捷键/图标开关
        self._sc_nav = None
        _nav_hk = str(cfg.get("hotkey_overlay", "nav", "m") or "").strip()
        if _nav_hk and _nav_hk.lower() not in ("off", "none", "false"):
            self._sc_nav = QShortcut(QKeySequence(_nav_hk), self)
            self._sc_nav.setContext(Qt.ApplicationShortcut)
            # 多贴图窗口时每个实例都注册了应用级快捷键——必须先判本窗口
            # 是否激活，否则按一次 M 会同时切换所有窗口的导航图
            self._sc_nav.activated.connect(
                lambda: self.isActiveWindow() and self._toggle_navigator())
        # 等比例调节窗口开关（B，可配置 [hotkey_overlay] ratio）
        self._sc_ratio = None
        _ratio_hk = str(cfg.get("hotkey_overlay", "ratio", "b") or "").strip()
        if _ratio_hk and _ratio_hk.lower() not in ("off", "none", "false"):
            self._sc_ratio = QShortcut(QKeySequence(_ratio_hk), self)
            self._sc_ratio.setContext(Qt.ApplicationShortcut)
            self._sc_ratio.activated.connect(
                lambda: self.isActiveWindow() and self._toggle_ratio_lock())

        # 角标按钮（文字框/标注）与状态文字已分离到独立 StatusStrip 浮层，
        # 图片窗口/scene 只含图片 —— 标注画布天然不含状态栏。
        self.setObjectName("ResultOverlay")
        self._status_strip = None      # StatusStrip（纯信息，由 app 关联）
        self._toolbar = None           # ToolBar（统一工具栏，由 app 关联）
        # 截图后是否默认打开标注栏（[ui] anno_toolbar_default）
        self._anno_default = cfg.getbool("ui", "anno_toolbar_default", False)

        h, w = img_bgr.shape[:2]
        self._img_lw = w / self.dpr      # 图片逻辑尺寸（拉伸 contain 基准）
        self._img_lh = h / self.dpr
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888)
        self._pix = QPixmap.fromImage(qimg)

        # ---- 场景只含图片（状态带已分离）----
        self.scene = QGraphicsScene(0, 0, w, h)
        # 必须保存底图 item 的 Python 引用：否则 scene.items() 等调用创建的临时
        # wrapper 被 GC 回收时，会连带误删底图 C++ 对象 → 图片消失、窗口透出下层程序。
        self._pix_item = self.scene.addPixmap(self._pix)
        # 注：图片四周边框已移除——改为在「贴图窗口」最外层画边框（_BorderOverlay），
        # 与图片尺寸/对齐解耦，始终框住窗口，不再随图片底部留白产生错位间隙。
        self.setScene(self.scene)
        self.setSceneRect(0, 0, w, h)

        # 定位到被截区域：逻辑坐标 = 原始 / dpr（Qt6 自动按 dpr 放大到物理像素）
        lw = raw_bbox[2] - raw_bbox[0]
        lh = raw_bbox[3] - raw_bbox[1]
        self._base_w = int(math.ceil(lw / dpr))   # 原始逻辑尺寸（缩放基准）
        self._base_h = int(math.ceil(lh / dpr))
        self._zoom = 1.0                          # 滚轮缩放系数（0.2x ~ 5x）
        self.setGeometry(
            int(raw_bbox[0] / dpr), int(raw_bbox[1] / dpr),
            self._base_w, self._base_h,
        )
        # 场景原始像素 -> 视口逻辑像素（点击命中检测依赖此映射）
        self.resetTransform()
        self.scale(1.0 / dpr, 1.0 / dpr)
        # 滚动条自动判断：图片超出贴图窗口即自动出现（原 S 快捷键/状态栏图标已移除）
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        # ---- 右上角圆形关闭按钮（点击关闭图片，Esc 同效）----
        # 独立顶层窗体（非子控件）：优先放在图片窗口右上角外侧一点，
        # 屏幕边缘放不下时回到图片内部右上角；跟随图片窗口移动/缩放。
        self.btn_close = QPushButton()
        from ui.base.icon import icon as svg_icon
        self.btn_close.setIcon(svg_icon("close", color="#ffffff", size=14))
        self.btn_close.setObjectName("overlay_close")
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.setFocusPolicy(Qt.NoFocus)   # 点击后不显示焦点虚线框
        self.btn_close.setToolTip("关闭图片 (Esc)")
        self.btn_close.setFixedSize(32, 32)
        self.btn_close.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.btn_close.setAttribute(Qt.WA_TranslucentBackground)
        self.btn_close.clicked.connect(self.close)
        self.btn_close.show()
        self._layout_close_btn()

        # ---- 钉图：图片 + 状态栏 + 工具栏一起隐藏，只留钉子 PinDot ----
        self._pinned = False
        self._pin_card = None
        self._pin_dot = None

    def _ensure_pin_dot(self):
        """懒创建钉图钉子（PinDot 独立小窗）；信号接回本窗口。"""
        if self._pin_dot is not None:
            return self._pin_dot
        from ui.components.pin_dot import PinDot
        dot = PinDot()
        dot.clicked.connect(self._on_pin_clicked)
        dot.double_clicked.connect(self._on_pin_double)
        dot.drag.connect(self._on_pin_drag)
        dot.hover_in.connect(self._on_pin_hover_in)
        dot.hover_out.connect(self._on_pin_hover_out)
        self._pin_dot = dot
        self._fgroup.add(dot)             # 参与 z 序（钉图态提级）
        return dot

    # ---- 钉图状态（钉子 = 独立 PinDot；图片+状态栏+工具栏一起隐藏）----
    def set_pinned(self, pinned: bool, hide_img: bool = False):
        """钉图：pinned=True 隐藏图片（可选）/状态栏/工具栏，只留 PinDot 钉子；
        False 恢复全部浮层。钉子定位由 app._pin_overlay 统一处理。"""
        self._pinned = pinned
        if pinned:
            self.btn_close.hide()          # 关闭按钮消失
            if self._navigator is not None:
                self._navigator.hide()     # 导航图一起隐藏
            if self._status_strip is not None:
                self._status_strip.hide()  # 状态栏一起隐藏
            if self._toolbar is not None:
                self._toolbar.close_picker()
                self._toolbar.hide()       # 工具栏一起隐藏
        else:
            self._layout_close_btn()       # 立即重定位关闭按钮（钉图期间图片可能移动过）
            self._hide_pin_card()
            if self._status_strip is not None:
                self._status_strip.show()
            if self._toolbar is not None:
                self._toolbar.show()
            if self._navigator is not None and self._nav_on:
                self._navigator.show()
                self._layout_navigator()
        if hide_img:
            self.hide()                    # 图片收进钉子（PinDot 留在桌面）
        else:
            self.show()
            self._fgroup.raise_all()
            if not pinned:
                self._fgroup.follow()      # 取消钉图：恢复正常位置/尺寸

    # ---- PinDot 钉子交互（单击/双击/拖动/悬停）----
    def _on_pin_clicked(self):
        """单击钉子：取消钉图（恢复图片 + 状态栏 + 工具栏）。"""
        self._hide_pin_card()
        if self._toolbar is not None:
            self._toolbar.btn_pin.setChecked(False)   # 信号链 → _unpin → 恢复
        else:
            self.set_pinned(False)

    def _on_pin_double(self):
        """双击钉子：收起图片（钉子留在桌面）/ 图片收起时双击恢复。"""
        if self.isVisible():
            # 收起前记住图片位置：恢复时回原位（不依赖钉子当前位置反推，
            # 避免钉子位置漂移时图片飞到屏幕外 = "图片不保持"）
            self._pin_restore = (self.x(), self.y())
            self.hide()                    # 收起图片，钉子浮桌面
        else:
            if getattr(self, "_pin_restore", None) is not None:
                self.move(*self._pin_restore)      # 回原位
            else:
                dot = self._pin_dot                # 兜底：右下角对准钉子
                if dot is not None:
                    self.move(dot.x() + 38 - self.width(),
                              dot.y() + 38 - self.height())
            self.show()                    # 恢复图片显示
            self._fgroup.raise_all()
            self._fgroup.follow()

    def _on_pin_drag(self, delta):
        """拖动钉子 → 图片窗口跟着移动（隐藏态也同步 _pin_restore，
        确保双击恢复时落到拖动后的位置，不回弹）。"""
        self.move(self.x() + delta.x(), self.y() + delta.y())
        self._pin_restore = (self.x(), self.y())

    def _on_pin_hover_in(self):
        if self.isVisible():               # 图片显示时不显示卡片
            return
        self._show_pin_card()

    def _on_pin_hover_out(self):
        self._hide_pin_card()

    # ---- 悬停信息卡片（图片收起时显示缩略图 + 信息，类似历史记录）----
    def _show_pin_card(self):
        """悬停钉子：显示缩略图 + OCR 文字/尺寸信息卡片。"""
        from ui.components.pin_card import PinCard
        if self._pin_card is None:
            self._pin_card = PinCard()
        self._pin_card.set_image(self.img)
        text = "\n".join(b.text for b in self.boxes) if self.boxes else ""
        info = f"{self.img.shape[1]}×{self.img.shape[0]}"
        if self.history_id:
            info += f" · #{self.history_id}"
        self._pin_card.set_info(text, info)
        # 定位：钉子左上方
        dot = self._pin_dot
        g = dot.mapToGlobal(dot.rect().topLeft()) if dot is not None \
            else self.mapToGlobal(self.rect().topLeft())
        self._pin_card.show_near(g)
        self._pin_card.show()
        self._pin_card.raise_()

    def _hide_pin_card(self):
        if self._pin_card is not None:
            self._pin_card.hide()

    def _close_sub_windows(self):
        """关闭与图片关联的次级浮窗（标注工具段子浮层、快捷键面板等），
        无动画钉图时使用，避免浮窗残留。"""
        if self._toolbar is not None:
            try:
                self._toolbar.close_picker()   # 关闭标注子浮层 + 调色板
            except Exception:            # noqa: BLE001
                pass
        if self._shortcut_panel is not None:
            self._shortcut_panel.close()
            self._shortcut_panel = None

    def _layout_close_btn(self):
        """关闭按钮定位：窗外/窗内位置以图片**右上角点为镜像**（180° 点对称）。

        - 窗内（屏幕放不下时）：右缘距窗口右 10px、上缘距窗口上 10px
        - 窗外（有空间时）：以右上角点为镜像 → 左缘距窗口右 10px、
          下缘距窗口上 10px（即 x = 窗口右 + 10, y = 窗口上 - 32 - 10）
        """
        if getattr(self, "btn_close", None) is None:
            return
        if getattr(self, "_pinned", False):   # 钉图状态：关闭按钮保持隐藏
            self.btn_close.hide()
            return
        s = self.btn_close.width()   # 实际固定尺寸 32（sizeHint 不可靠）
        # 用并集边界（_clamp_bounds），不能按单屏：跨屏时本窗口横跨两屏，按单屏
        # 判定「窗外放得下吗」会得到错误结论（并可能把按钮丢到另一块屏上）。
        scr = self._clamp_bounds()
        m = 5    # 镜像间距（窗外/窗内距右上角点均为 5px）
        # 窗外（镜像位）：左缘 = 窗口右 + m，下缘 = 窗口上 - m
        ox = self.x() + self.width() + m
        oy = self.y() - s - m
        if ox + s <= scr.right() and scr.top() <= oy and oy + s <= scr.bottom():
            x, y = ox, oy
        else:   # 放不下 → 窗内：右缘 = 窗口右 - m，上缘 = 窗口上 + m
            x = self.x() + self.width() - s - m
            y = self.y() + m
        self.btn_close.move(x, y)
        self.btn_close.show()
        self.btn_close.raise_()

    # ---- M1-3：填充识别结果并构建字符几何 ----
    def set_boxes(self, boxes):
        self.boxes = boxes
        # 重置旧选区（否则旧索引会越界访问新字符区间）
        self.ranges = []
        self.active = -1
        self.anchor = None
        self.focus = None
        self.dragging = False
        # 同步清理表格选中态（Ctrl+R 重识别会走到这里）：旧高亮矩形残留。
        # 越界防护**不在这里** —— 本函数只 removeItem + 重置字段，不会抛
        # IndexError；真正会越界的是 _draw_table_sel 索引 self._table_rows，
        # 保护写在那边（原先这里包一层 try/except 与注释声明不符，且把
        # 清理过程中的真实错误一并吞掉）。
        self._clear_table_sel()
        self._build_geom()
        # 画静态框线（浅色，提示可选单元）
        for it in self._outline:
            self.scene.removeItem(it)
        self._outline = []
        for orig in self.box_order:
            box = self.boxes[orig]
            poly = QPolygonF([QPointF(p[0], p[1]) for p in box.polygon])
            item = QGraphicsPolygonItem(poly)
            # 按来源着色：二维码（kind="qr"）用可配置紫色，OCR 文字保持蓝色
            if getattr(box, "kind", "ocr") == "qr":
                c = self._qr_border_color
                item.setBrush(QBrush(QColor(c.red(), c.green(), c.blue(), 22)))
                item.setPen(QPen(QColor(c.red(), c.green(), c.blue(), 130), 1))
            else:
                item.setBrush(QBrush(QColor(0, 200, 255, 22)))
                item.setPen(QPen(QColor(0, 200, 255, 130), 1))
            item.setZValue(1)   # 第 2 层（OCR/表格）：底图(z=0)之上、标注(z=5)之下
            item.setVisible(self._show_boxes and not self._table_mode)
            # 表格模式时文字框隐藏（由表格框替换）
            self.scene.addItem(item)
            self._outline.append(item)
        self._set_status(
            f"已识别 {len(boxes)} 块 · 拖灰条移动窗口 · 像记事本选字 · "
            "Ctrl+Shift+D 标注 · Esc关闭"
        )
        self._redraw()
        # 按钮状态与「框是否可见」保持一致：历史贴图等路径下 boxes 由 app 在
        # set_toolbar() 之后回填，此时把「文字框」按钮同步点亮（屏蔽信号，
        # 避免回调覆盖上面的识别结果状态文案）
        if self._toolbar is not None and self._show_boxes and not self._table_mode:
            _btn = self._toolbar.btn_boxes
            if not _btn.isChecked():
                _btn.blockSignals(True)
                _btn.setChecked(True)
                _btn.blockSignals(False)
        # 配置开启时：截图后自动进入标注模式（[ui] anno_toolbar_default）
        if self._anno_default and not self._anno_mode:
            # 用成员 singleShot 定时器（closeEvent 中 stop），不能用裸
            # QTimer.singleShot(0, ...)：窗口 deleteLater 与定时器触发的
            # 执行顺序无保证，可能在对象销毁后回调 _enter_annotation 抛
            # RuntimeError（Internal C++ object already deleted）
            if getattr(self, "_anno_auto_timer", None) is None:
                t = QTimer(self)
                t.setSingleShot(True)
                t.timeout.connect(self._enter_annotation)
                self._anno_auto_timer = t
            self._anno_auto_timer.start(0)

    # ---- 文字蓝色框开关（贴图按钮 / 设置默认值联动）----
    def _on_boxes_toggled(self, checked: bool):
        if checked and not self.boxes:
            # 无 OCR 结果（截图后自动 OCR 关闭）：请求 app 临时拉起 OCR。
            # 用户点亮按钮的意图就是要看框 → 先置可见，识别完成后新框按此显示
            # （否则 ui.show_text_boxes=false 时点了按钮却看不到框）
            self._show_boxes = True
            self.ocr_needed.emit()
            return
        self._show_boxes = checked
        self._set_boxes_visible(checked)
        self._set_status("文字框：" + ("已显示" if checked else "已隐藏"))

    # ---- 二维码识别开关（工具栏按钮 / Q 键 / 设置默认值联动）----
    def _on_qr_toggled(self, checked: bool):
        self._qr_enabled = bool(checked)
        # 跨会话记忆：[software] enable_qr（下次启动 / 新建窗口按此值初始化）
        try:
            self.config.cp.set("software", "enable_qr",
                               "true" if checked else "false")
            self.config.save()
        except Exception:  # noqa: BLE001
            pass
        self._set_status("二维码识别：" + ("已开启" if checked else "已关闭"))

    def _toggle_qr(self):
        """Q 快捷键：切换二维码识别开关（无修饰键才触发，避免 Ctrl+Q 等误开）。"""
        if self._toolbar is not None:
            self._toolbar.btn_qr.setChecked(not self._toolbar.btn_qr.isChecked())
        else:
            self._on_qr_toggled(not self._qr_enabled)

    def _toggle_boxes(self):
        """W 快捷键：切换文字框显示（无修饰键才触发，避免 Ctrl+W 等误开）。"""
        if self._toolbar is not None:
            self._toolbar.btn_boxes.setChecked(
                not self._toolbar.btn_boxes.isChecked())
        else:
            self._on_boxes_toggled(not self._show_boxes)

    def _toggle_table(self):
        """E 快捷键：切换表格模式（等价「表格」按钮单击；无修饰键才触发，
        避免 Ctrl+E 等误开；标注态由 QWERTYU 的 E=箭头 优先拦截）。"""
        if self._toolbar is not None:
            self._toolbar.btn_table.setChecked(
                not self._toolbar.btn_table.isChecked())
        else:
            self._on_table_mode(not getattr(self, "_table_mode", False))

    def _handle_toolbar_key(self, e) -> bool:
        """常用态工具栏键（Q/W/E/F 二维码/文字框/表格/收藏）统一分发。
        标注态返回 False（QWERTYU 由 handle_anno_key 处理，避免工具冲突）。
        无修饰键才触发；贴图窗口 / 工具栏焦点共用此路径。"""
        if getattr(self, "_anno_mode", False):
            return False
        if e.modifiers() & (Qt.ControlModifier | Qt.AltModifier
                            | Qt.ShiftModifier | Qt.MetaModifier):
            return False
        if self._match_shortcut(e, self._qr_hk):
            self._toggle_qr()
            return True
        if self._match_shortcut(e, self._boxes_hk):
            self._toggle_boxes()
            return True
        if self._match_shortcut(e, self._tool_table_hk):
            self._toggle_table()
            return True
        if self._match_shortcut(e, self._fav_hk):
            self.fav_requested.emit()
            return True
        return False

    def _set_boxes_visible(self, visible: bool):
        for it in self._outline:
            it.setVisible(visible)

    # ---- 表格模式（纯坐标检测网格 = 只读表格窗口体验）----
    def _on_table_mode(self, on: bool):
        """「表格」按钮短按切换：进入 = 用 OCR 块坐标规则检测表格并画网格
        （不需要 SLANet）；检测不出表格时保留文字框。退出 = 恢复文字框。"""
        if on and not self.boxes:
            # 无 OCR 结果（截图后自动 OCR 关闭）：请求 app 临时拉起 OCR
            self.ocr_needed.emit()
            return
        if on:
            # **按实际检测结果决定是否算「进入表格模式」**：原先先置 True 再看
            # 检测结果，检测失败时界面是文字模式、交互却按表格模式走
            # （点击清空选区、滚轮缩放被禁）→ 整个窗口交互锁死。
            self._table_mode = self._apply_table_view()
        else:
            self._table_mode = False
            self._set_boxes_visible(self._show_boxes)
            self._hide_table_boxes()
            self._clear_table_sel()
            self._set_status("已退出表格模式")

    def _apply_table_view(self) -> bool:
        """坐标规则检测出表格 → 隐藏文字框显示网格，返回 True；
        否则保留文字框、返回 False（调用方据此决定是否进入表格模式）。"""
        self._build_table_grid()
        if self._table_rows is not None:
            self._set_boxes_visible(False)
            self._build_table_boxes()
            self._show_table_boxes()
            n_r, n_c = len(self._table_rows), len(self._table_cols)
            self._set_status(
                f"表格模式：{n_r} 行 × {n_c} 列 · 拖动框选单元格复制 TSV · "
                "双击打开窗口 · 再点表格退出")
            return True
        self._hide_table_boxes()
        self._set_boxes_visible(self._show_boxes)
        self._set_status("未检测到表格结构，仍在文字模式（框选复制文本）")
        return False

    def _build_table_grid(self):
        """**表格网格构建（双通道，增强式）**——胶水层：

        1. **像素线检测**（优先，ui.core.table_detect）：截图含完整网格线时，
           行/列条带直接用真实表格线位置——边界最精确，换行格不拆行；
           线不足（如无边框表）返回 None。
        2. **纯坐标启发式**（回退，ui.core.table_detect）：按文字块位置聚类行列。

        任一通道成功 → 设置 _table_rows/_table_cols/_table_row_ids/
        _table_col_ids/_table_cell_texts（格文本 = 格内文字块拼接）。
        两条通道都失败 → 保持 None（文字模式，不进入表格视图）。
        算法本体（检测/聚类/格文本）在 ui/table_detect.py（纯函数，无 Qt）。"""
        self._table_rows = None
        self._table_cols = None
        self._table_cell_texts = {}
        if not self.boxes:
            return
        blobs = build_blobs(self.boxes, self.box_order)
        if not blobs:
            return
        # 1) 像素线检测优先（有网格线时边界最精确）
        rows, cols = _detect_table_lines(getattr(self, "img", None))
        if rows is None or cols is None:
            # 2) 纯坐标启发式回退（无边框表 / 线检测失败）
            rows, cols = _coords_table_grid(blobs)
            if rows is None:
                return
        self._table_rows = rows
        self._table_cols = cols
        self._table_row_ids = list(range(len(rows)))
        self._table_col_ids = list(range(len(cols)))
        self._table_cell_texts = map_cell_texts(blobs, rows, cols)

    def _build_table_boxes(self):
        for it in self._table_boxes:
            self.scene.removeItem(it)
        self._table_boxes = []
        if not self._table_rows:
            return
        # 每个网格格画一个矩形框（边界共享、无重叠，体验同表格窗口网格线）
        c = self._table_border_color
        for y0, y1 in self._table_rows:
            for x0, x1 in self._table_cols:
                item = QGraphicsRectItem(QRectF(x0, y0, x1 - x0, y1 - y0))
                item.setPen(QPen(QColor(c.red(), c.green(), c.blue(), 170), 1))
                item.setBrush(QBrush(Qt.NoBrush))
                item.setZValue(1)   # 第 2 层（表格网格，同 OCR 层）
                item.setVisible(False)
                self.scene.addItem(item)
                self._table_boxes.append(item)

    def _show_table_boxes(self):
        for it in self._table_boxes:
            it.setVisible(True)

    def _hide_table_boxes(self):
        for it in self._table_boxes:
            it.setVisible(False)

    # ---- 表格选取（拖矩形选单元格，同表格窗口）----
    def _table_hit_cell(self, pt) -> tuple[int, int] | None:
        for ri, (y0, y1) in enumerate(self._table_rows or []):
            if y0 <= pt.y() <= y1:
                for ci, (x0, x1) in enumerate(self._table_cols or []):
                    if x0 <= pt.x() <= x1:
                        return ri, ci
        return None

    def _table_press(self, pt):
        rc = self._table_hit_cell(pt)
        if rc is None:
            self._clear_table_sel()
            return
        self._tbl_anchor = rc
        self._tbl_dragging = True
        self._tbl_sel = (rc[0], rc[1], rc[0], rc[1])
        self._draw_table_sel()

    def _table_move(self, pt):
        if not self._tbl_dragging or self._tbl_anchor is None:
            return
        rc = self._table_hit_cell(pt)
        if rc is None:
            return
        r0, c0 = self._tbl_anchor
        self._tbl_sel = (min(r0, rc[0]), min(c0, rc[1]),
                         max(r0, rc[0]), max(c0, rc[1]))
        self._draw_table_sel()

    def _table_release(self):
        sel = self._tbl_sel
        if sel is None:
            return
        self._copy_table_rect(*sel)
        self._set_status(
            f"已复制表格 TSV（{sel[2]-sel[0]+1} 行 × {sel[3]-sel[1]+1} 列）· "
            "Esc 关闭 / 双击开窗")

    def _draw_table_sel(self):
        for it in self._tbl_sel_items:
            self.scene.removeItem(it)
        self._tbl_sel_items = []
        if self._tbl_sel is None or not self._table_rows:
            return
        r0, c0, r1, c1 = self._tbl_sel
        # 越界收敛：重识别后新网格可能比旧选区小，_tbl_sel 里的行/列号会指到
        # _table_rows/_table_cols 之外 —— 保护必须写在真正索引的地方（见
        # set_boxes 的说明）。旧选区整体越界时收敛到最后一格，不画空。
        nr, nc = len(self._table_rows), len(self._table_cols or ())
        if nr == 0 or nc == 0:
            return
        r0, r1 = max(0, min(r0, nr - 1)), max(0, min(r1, nr - 1))
        c0, c1 = max(0, min(c0, nc - 1)), max(0, min(c1, nc - 1))
        for ri in range(r0, r1 + 1):
            y0, y1 = self._table_rows[ri]
            for ci in range(c0, c1 + 1):
                x0, x1 = self._table_cols[ci]
                rect = QGraphicsRectItem(QRectF(x0, y0, x1 - x0, y1 - y0))
                rect.setBrush(QBrush(QColor(0, 160, 120, 90)))
                rect.setPen(QPen(QColor(0, 180, 130, 200), 1))
                rect.setZValue(3)
                self.scene.addItem(rect)
                self._tbl_sel_items.append(rect)

    def _clear_table_sel(self):
        for it in self._tbl_sel_items:
            self.scene.removeItem(it)
        self._tbl_sel_items = []
        self._tbl_sel = None
        self._tbl_anchor = None
        self._tbl_dragging = False

    def _copy_table_rect(self, r0, c0, r1, c1):
        """复制选中矩形内的单元格为 TSV（列对齐，同表格窗口复制）。"""
        lines = []
        for ri in range(r0, r1 + 1):
            row = []
            for ci in range(c0, c1 + 1):
                row.append(self._table_cell_texts.get((ri, ci), ""))
            lines.append("\t".join(row))
        tsv = "\n".join(lines)
        if tsv:
            QApplication.clipboard().setText(tsv)
            self.copied.emit(tsv)
        self._redraw()

    def _do_copy(self):
        if self._table_mode and self._table_rows is not None:
            # 表格模式：若当前有选中矩形则复制该矩形；否则全选网格
            if self._tbl_sel is None:
                self._tbl_sel = (0, 0, len(self._table_rows) - 1,
                                 len(self._table_cols) - 1)
                self._draw_table_sel()
            self._copy_table_rect(*self._tbl_sel)
        else:
            self._do_copy_text()

    def _do_copy_text(self):
        """普通文本复制（表格模式外）。"""
        txt = self.get_selected_text()
        if txt:
            QApplication.clipboard().setText(txt)
            self.copied.emit(txt)
            self._preview_selection(immediate=True)
            self._set_status(f"已复制 {len(txt)} 字符 · Esc 关闭 / 空白点击清除")
        self._redraw()

    def _copy_image(self):
        """复制图片到剪贴板：有标注时用统一渲染入口
        ui.components.annotation.render_annotated_image（底图 + 标注 JSON 离屏重绘），
        与历史记录「复制图片（带标注）」同源，保证序号 num 等所有标注类型一致；
        无标注用原始截图（BGR ndarray → QImage）。"""
        from ui.components.annotation import render_annotated_image

        img = getattr(self, "img", None)
        if img is not None and self._anno is not None and self._anno.data:
            # 统一渲染：底图(原始截图) + 标注 JSON，复用与历史面板相同的渲染逻辑
            h, w = img.shape[:2]
            rgb = np.ascontiguousarray(img[:, :, ::-1])   # BGR → RGB
            qimg = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888)
            out = render_annotated_image(qimg, self._anno.json(), opaque=True)
            QApplication.clipboard().setImage(out)
            self._set_status(f"已复制图片 {out.width()}×{out.height()}（含标注）")
            return
        if img is None:
            return
        h, w = img.shape[:2]
        rgb = np.ascontiguousarray(img[:, :, ::-1])   # BGR → RGB
        qimg = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888)
        QApplication.clipboard().setImage(qimg)
        self._set_status(f"已复制图片 {w}×{h}")

    def _copy_raw_image(self):
        """复制**原图**到剪贴板：始终用原始截图（BGR → RGB），
        不含标注/文字框等任何 UI 叠加（区别于 _copy_image 的「含标注渲染」）。"""
        img = getattr(self, "img", None)
        if img is None:
            return
        h, w = img.shape[:2]
        rgb = np.ascontiguousarray(img[:, :, ::-1])   # BGR → RGB
        qimg = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888)
        QApplication.clipboard().setImage(qimg)
        self._set_status(f"已复制原图 {w}×{h}（不含标注）")

    def _on_done(self):
        """状态栏「完成」：若在标注模式先退出并落库标注（annotations_changed →
        _save_annotations，其中 _save_record 确保 history_id 存在），再复制图片，
        最后请求 App 保存历史并关闭。"""
        if self._anno_mode:
            self._exit_annotation()      # 触发标注保存，避免「标注后直接点完成」丢标注
        self._copy_image()
        self.done_requested.emit(self)   # App 负责补存历史（OCR 可能未完成）
        self.close()

    def _build_geom(self):
        items = []
        for i, b in enumerate(self.boxes):
            x0, y0, x1, y1 = _bbox(b.polygon)
            items.append((i, (x0 + x1) / 2.0, (y0 + y1) / 2.0))
        # 阅读顺序：先按行(y)，行内按 x
        items.sort(key=lambda t: (t[2], t[1]))
        self.box_order = [t[0] for t in items]
        self.geom = {}
        for orig in self.box_order:
            b = self.boxes[orig]
            x0, y0, x1, y1 = _bbox(b.polygon)
            n = len(b.text)
            chars = self._char_intervals(b.text, x0, x1, y0, y1)
            self.geom[orig] = dict(x0=x0, y0=y0, x1=x1, y1=y1,
                                   n=n, chars=chars)

    @staticmethod
    def _char_intervals(text: str, x0, x1, y0, y1):
        """按字符真实宽窄比例分配每个字符的 x 区间（英文/标点窄、汉字方正）。

        用 QFontMetrics 量出各字符相对宽度，再等比缩放到框宽，保证：
        - 区间和恰好等于框宽；
        - 'i'、'.'、',' 等窄字符占窄区间，'m'、'W'、汉字占宽区间，
          点击命中与视觉位置严格对齐。

        **收敛点（性能）**：QFont / QFontMetricsF 按 (family, pixel_size) 进程级
        复用，单字符 advance 也按同一 key 缓存（见模块顶部的 _font_and_metrics /
        _char_advance）。原实现每块都 `QFont(...)` + `QFontMetricsF(...)` 再逐字符
        `horizontalAdvance`，一屏 200 块 × 20 字 ≈ 4000 次字体度量，且每次
        set_boxes（首次识别 / Ctrl+R / 历史恢复 / 补齐识别）全量重算。
        数值不变：同样的 family、同样的 pixelSize、同样的 horizontalAdvance 结果，
        仅把重复计算换成查表。
        """
        n = len(text)
        if n == 0:
            return []
        pixel_size = max(8, int(round(y1 - y0)))     # 按行高近似字号
        family = ui_font_family()
        key = (family, pixel_size)
        fm = _font_and_metrics(family, pixel_size)
        widths = [max(1.0, _char_advance(key, fm, ch)) for ch in text]
        total = sum(widths)
        scale = (x1 - x0) / total if total > 0 else 1.0
        chars = []
        cx = x0
        for wd in widths:
            chars.append((cx, cx + wd * scale))
            cx += wd * scale
        return chars

    def _caret(self, g, x: float) -> int:
        """把 x 映射为插入点位置（0..n，即"第几个字符之后"）。

        落在某字符左半 → 该字符前；右半 → 该字符后。到行尾 x≈x1 时返回 n，
        保证整行/整词/末尾标点都能被完整选中（修复"差个字母/句号"）。
        """
        k = 0
        for (cx0, cx1) in g["chars"]:
            if x < cx0 + (cx1 - cx0) / 2.0:
                return k
            k += 1
        return k

    # ---- 命中检测 ----
    def _hit(self, scene_pt: QPointF):
        """返回 (order_pos, caret)，caret∈[0..n]；完全在框外返回 None。"""
        x, y = scene_pt.x(), scene_pt.y()
        pad = 4
        for pos, orig in enumerate(self.box_order):
            g = self.geom[orig]
            if (g["x0"] - pad <= x <= g["x1"] + pad and
                    g["y0"] - pad <= y <= g["y1"] + pad):
                return (pos, self._caret(g, x))
        return None

    def _hit_or_clamp(self, scene_pt: QPointF):
        """拖拽时用：命中则返回命中；在框外则向首/末行或最近行夹紧。"""
        hit = self._hit(scene_pt)
        if hit is not None:
            return hit
        x, y = scene_pt.x(), scene_pt.y()
        tops = [self.geom[o]["y0"] for o in self.box_order]
        bots = [self.geom[o]["y1"] for o in self.box_order]
        if y < min(tops):
            return (0, 0)
        if y > max(bots):
            last = self.box_order[-1]
            return (len(self.box_order) - 1, self.geom[last]["n"])
        for pos, orig in enumerate(self.box_order):
            g = self.geom[orig]
            if g["y0"] <= y <= g["y1"]:
                mid = (g["x0"] + g["x1"]) / 2
                return (pos, 0 if x < mid else g["n"])
        return (0, 0)

    # ---- 选区归一化与文本拼接 ----
    @staticmethod
    def _norm(a, b):
        ap, ac = a
        bp, bc = b
        if ap < bp:
            return [ap, ac, bp, bc]
        if ap > bp:
            return [bp, bc, ap, ac]
        if ac <= bc:
            return [ap, ac, ap, bc]
        return [ap, bc, ap, ac]

    def get_selected_text(self) -> str:
        out = []
        for (o0, c0, o1, c1) in self.ranges:
            parts = []
            for pos in range(o0, o1 + 1):
                orig = self.box_order[pos]
                g = self.geom[orig]
                txt = self.boxes[orig].text
                if pos == o0 and pos == o1:
                    seg = txt[c0:c1]
                elif pos == o0:
                    seg = txt[c0:]
                elif pos == o1:
                    seg = txt[:c1]
                else:
                    seg = txt
                parts.append(seg)
            out.append("\n".join(parts))   # 区域内多块按行拼接
        return "\n".join(out)              # 多区域按选择顺序换行拼接

    @property
    def is_drawing(self) -> bool:
        """是否正在画布上拖拽绘制（浮窗组让位判定用）。"""
        return self._anno is not None and self._anno.is_drawing

    # ---- 渲染（状态文字转发到纯信息状态栏）----
    def set_status_strip(self, strip):
        """关联纯信息状态栏（状态文字 + 预览 + Esc）；无按钮。"""
        self._status_strip = strip
        if strip is None:
            return
        self._fgroup.add(strip, follow=self._sync_status_strip_pos)
        strip.attach_overlay(self)
        # 状态栏 Esc：标注模式先退标注；否则关闭状态栏自身
        strip.esc_pressed.connect(self._on_strip_esc)
        # 导航图开关：缩略小地图（滚动条已自动，无开关）
        strip.nav_toggled.connect(self.set_navigator)
        # 等比例调节窗口：锁定/解锁（默认锁定）
        strip.ratio_toggled.connect(self._set_ratio_lock)
        strip.set_status("识别中…")

    def set_toolbar(self, toolbar):
        """关联统一工具栏（操作按钮 + 标注工具区）；信号接回本窗口逻辑。"""
        self._toolbar = toolbar
        if toolbar is None:
            return
        self._fgroup.add(toolbar, follow=self._sync_toolbar_pos)
        toolbar.attach_overlay(self)
        # 「文字框」按钮初始态 = 框可见性 && 自动 OCR 已开。
        # 自动 OCR 关闭时截图后没有任何识别结果，按钮默认**不点亮**（点亮会让人
        # 误以为已在识别）；用户点击按钮才触发 OCR（_on_boxes_toggled → ocr_needed）。
        # 历史贴图等带结果的路径由 set_boxes() 回填时同步点亮。
        toolbar.btn_boxes.setChecked(self._show_boxes and self._auto_ocr)
        toolbar.btn_boxes.toggled.connect(self._on_boxes_toggled)
        toolbar.btn_qr.setChecked(self._qr_enabled)
        # 软件设置关闭「二维码识别」→ 工具栏不显示 QR 按钮（宽度随之收缩）。
        # 可见性按启动时配置决定：关闭 = 禁用该功能（连按钮都不露）；开启 = 显示，
        # 运行时点按钮开关只影响启用状态、不影响按钮显示。
        toolbar.btn_qr.setVisible(self._qr_enabled)
        toolbar.qr_toggled.connect(self._on_qr_toggled)
        toolbar.btn_anno.toggled.connect(self._on_anno_btn_toggled)
        # 表格按钮：短按 = 表格模式（框选复制 TSV）；长按 = 打开表格窗口
        toolbar.table_mode_changed.connect(self._on_table_mode)
        toolbar.table_window_requested.connect(self.table_window_requested.emit)
        toolbar.fav_clicked.connect(self.fav_requested.emit)   # 收藏当前截图
        toolbar.done_clicked.connect(self._on_done)            # 完成：复制图片 + 关闭窗口
        toolbar.pin_toggled.connect(self.pin_toggled.emit)     # 钉图切换（隐藏浮层，只留钉子）
        # 标注工具段（标注态内联展开）：工具/颜色/线宽/马赛克/文字属性/撤销/重做/清空
        a = toolbar.tools
        a.undo_requested.connect(self._anno_undo)
        a.redo_requested.connect(self._anno_redo)
        a.clear_requested.connect(self._anno_clear)
        a.mosaic_mode_changed.connect(self._save_mosaic_mode)
        # 颜色/大小变更：更新会话（即时生效）+ 按开关持久化
        a.color_changed.connect(self._on_anno_color)
        a.width_changed.connect(self._on_anno_width)
        # 文字属性信号：运行时读取 self._anno（session 在进入标注时才创建），
        # 任何路径下都不因 self._anno 缺失而崩溃（兜底防御，即使异常时序也安全）
        a.text_size_changed.connect(
            lambda v: (self._anno.set_font_size(v)
                       if self._anno is not None else None))
        a.text_bold_changed.connect(
            lambda b: (self._anno.set_bold(b)
                       if self._anno is not None else None))
        a.text_italic_changed.connect(
            lambda b: (self._anno.set_italic(b)
                       if self._anno is not None else None))
        # 文字属性 + 颜色/大小：按开关持久化记录
        a.text_size_changed.connect(
            lambda v: self._save_anno_param("text_size", str(v)))
        a.text_bold_changed.connect(
            lambda b: self._save_anno_param("text_bold", "true" if b else "false"))
        a.text_italic_changed.connect(
            lambda b: self._save_anno_param("text_italic", "true" if b else "false"))
        # 序号工具：起始序号变更 → 更新会话并重排已有序号；尺寸/样式变更 → 更新会话
        a.num_start_changed.connect(self._on_num_start)
        a.num_size_changed.connect(self._on_num_size)
        a.num_style_changed.connect(self._on_num_style)
        # 矩形/箭头/画笔样式变更 → 更新会话 + 按开关持久化
        a.tool_style_changed.connect(self._on_tool_style)
        # 记忆上次马赛克模式（默认框选）
        a.mosaic_mode = self.config.get("annotation", "mosaic_mode", "rect")
        # 参数永久化记录开关（[ui] persist_params，默认开）：
        # 开启 → 启动恢复上次保存的标注工具参数（颜色/线宽/字号/加粗/斜体/马赛克模式/序号参数）
        if self.config.getbool("ui", "persist_params", True):
            cfg = self.config
            try:
                c = cfg.get("annotation", "tool_color", "255,95,95")
                color = tuple(int(x) for x in str(c).split(","))[:3]
            except ValueError:
                color = (255, 95, 95)
            a.set_params(
                color=color,
                width=cfg.getint("annotation", "tool_width", 2),
                text_size=cfg.getint("annotation", "text_size", 18),
                text_bold=cfg.getbool("annotation", "text_bold", False),
                text_italic=cfg.getbool("annotation", "text_italic", False),
                mosaic_mode=cfg.get("annotation", "mosaic_mode", "rect"),
                num_start=cfg.getint("annotation", "num_start", 1),
                num_size=cfg.getint("annotation", "num_size", 18),
                num_style=cfg.get("annotation", "num_style", "circle"),
                rect_style=cfg.get("annotation", "rect_style", "rect"),
                arrow_style=cfg.get("annotation", "arrow_style", "single"),
                line_style=cfg.get("annotation", "line_style", "solid"),
                pen_style=cfg.get("annotation", "pen_style", "solid"))

    def _set_status(self, txt: str):
        self._hint = txt
        if self._status_strip is not None:
            self._status_strip.set_status(txt)

    def _set_preview(self, text: str):
        if self._status_strip is not None:
            self._status_strip.set_preview(text)

    def _preview_selection(self, immediate: bool = False):
        """刷新状态栏第 2 行预览；拖动中**防抖** 40ms，最终态立即刷新。

        收敛点（性能）：原实现每个 mouseMoveEvent 都全量拼接选中文本
        （get_selected_text 跨块切片 + "\n".join），再 setText 到状态栏 QLabel
        （每帧触发布局与重绘）。拖动时相邻两帧的预览串只差末尾一两个字符，
        重复计算纯属浪费；人眼对 40ms 的预览滞后无感。
        故：拖动中（immediate=False）只重启 40ms 单次定时器；**所有最终态**
        （按下成区 / 松手 / 双击整行 / Ctrl+A 全选 / 复制）走 immediate=True，
        预览不会滞后于最终结果（定时器同步取消，不会有一次多余的迟到刷新）。
        """
        if immediate:
            t = getattr(self, "_preview_timer", None)
            if t is not None:
                t.stop()
            self._refresh_preview()
            return
        t = getattr(self, "_preview_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(40)
            t.timeout.connect(self._refresh_preview)
            self._preview_timer = t
        t.start()

    def _refresh_preview(self):
        if self.active >= 0 and self.ranges:
            self._set_preview(self.get_selected_text())
        else:
            self._set_preview("")

    def _redraw(self):
        """按选区重绘高亮：**一条选区 = 一个场景项**（见 _SelectionHighlightItem）。

        收敛点（性能）：原实现为每个**字符**建一个 QGraphicsRectItem，拖动时
        mouseMoveEvent 每帧先 removeItem 全部旧项再逐字符重建（一屏 200 块 × 20 字
        全选 ≈ 4000 次场景增删），select_all 同理。现在把该选区所有字符矩形累加进
        一个 QPainterPath，项数 = 选区数（多区域 Ctrl 点选时才有多个）：
        实测全选 4000 → 200 项、3 选区 257 → 3 项，单帧 _redraw 80ms → 4ms。
        """
        for it in self._hl:
            self.scene.removeItem(it)
        self._hl = []
        for (o0, c0, o1, c1) in self.ranges:
            path = QPainterPath()
            path.setFillRule(Qt.WindingFill)   # 多块矩形可能重叠，奇偶会挖空
            for pos in range(o0, o1 + 1):
                orig = self.box_order[pos]
                g = self.geom[orig]
                if pos == o0 and pos == o1:
                    ks = range(c0, c1)
                elif pos == o0:
                    ks = range(c0, g["n"])
                elif pos == o1:
                    ks = range(0, c1)
                else:
                    ks = range(0, g["n"])
                y0, h = g["y0"], g["y1"] - g["y0"]
                for k in ks:
                    cx0, cx1 = g["chars"][k]
                    path.addRect(QRectF(cx0, y0, cx1 - cx0, h))
            if path.isEmpty():
                continue
            item = _SelectionHighlightItem(path)
            self.scene.addItem(item)
            self._hl.append(item)

    # ---- 交互 ----
    # ---- 窗口拖动（像普通程序一样移动浮窗）----
    def _start_window_drag(self, e):
        """开始拖动整个窗口（Alt+拖动图片，或拖动独立状态栏）。"""
        self._drag_offset = (
            e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        )

    def mousePressEvent(self, e):
        # 整组提级：图片被点击/激活会压过状态栏/标注栏（钉图态延迟对抗异步激活）
        self._fgroup.raise_all(deferred=self._pinned)
        # 中键 = 拖动平移图片（查看大图/长图）
        if e.button() == Qt.MiddleButton:
            self._pan_start = (
                e.globalPosition().toPoint(),
                self.horizontalScrollBar().value(),
                self.verticalScrollBar().value())
            self.setCursor(Qt.ClosedHandCursor)
            e.accept()
            return
        # 边缘拉伸（普通态左键命中窗口边/角）：优先于顶部把手/文字选取
        if e.button() == Qt.LeftButton \
                and not self._anno_mode and not self._table_mode:
            edge = self._edge_at(e.position())
            if edge is not None:
                self._start_edge_resize(e, edge)
                e.accept()
                return
        # 标注模式：绘制优先（与文字选取互斥，INT-D05）
        if self._anno_mode and self._anno is not None:
            pt = self.mapToScene(e.pos())
            tb = self._toolbar
            self._anno.set_tool(tb.tool if tb is not None else "rect")
            self._anno.set_color(tb.color() if tb is not None else (255, 95, 95))
            self._anno.set_width(tb.line_width() if tb is not None else 2)
            if tb is not None:
                self._anno.set_font(tb.font_combo.currentFont().family())
                self._anno.set_font_size(tb.text_size)
                self._anno.set_bold(tb.text_bold)
                self._anno.set_italic(tb.text_italic)
                self._anno.set_mosaic_mode(tb.mosaic_mode)
                self._anno.set_num_start(tb.num_start)
                self._anno.set_num_size(tb.num_size)
                self._anno.set_num_style(tb.num_style)
                self._anno.set_tool_style("rect", tb.rect_style)
                self._anno.set_tool_style("arrow", tb.arrow_style)
                self._anno.set_tool_style("line", tb.line_style)
                self._anno.set_tool_style("pen", tb.pen_style)
            self._anno.mouse_press(pt)
            return
        pt = self.mapToScene(e.pos())
        # 表格模式：拖矩形选单元格（体验同表格窗口）
        if self._table_mode and self._table_rows is not None:
            self._table_press(pt)
            return
        # Alt+拖动 = 图片任意位置移动窗口（顶部把手已让位给边缘拉伸）
        if e.modifiers() & Qt.AltModifier:
            self._start_window_drag(e)
            return
        hit = self._hit(pt)
        ctrl = bool(e.modifiers() & Qt.ControlModifier)
        if hit is None:
            if not ctrl:
                self.ranges = []
                self.active = -1
                self._redraw()
                self._set_preview("")
                self._set_status("已清除选区")
            return
        pos, ch = hit
        if ctrl:
            self.ranges.append([pos, ch, pos, ch])
            self.active = len(self.ranges) - 1
        else:
            self.ranges = [[pos, ch, pos, ch]]
            self.active = 0
        self.anchor = (pos, ch)
        self.focus = (pos, ch)
        self.dragging = True
        self._redraw()
        self._preview_selection(immediate=True)   # 按下即成形：立即刷新（非拖动帧）

    def mouseMoveEvent(self, e):
        # 中键拖动平移：按全局位移反向调滚动条（内容跟手）
        if self._pan_start is not None and (e.buttons() & Qt.MiddleButton):
            g0, h0, v0 = self._pan_start
            g = e.globalPosition().toPoint()
            self.horizontalScrollBar().setValue(
                max(0, h0 - (g.x() - g0.x())))
            self.verticalScrollBar().setValue(
                max(0, v0 - (g.y() - g0.y())))
            return
        # 边缘拉伸拖动：跟手调整窗口几何（resizeEvent 内等比适配图片）
        if self._resize_edge is not None and (e.buttons() & Qt.LeftButton):
            self._do_edge_resize(e.globalPosition().toPoint())
            e.accept()
            return
        # 标注模式：实时绘制（绘制中光标移动 → 浮窗组让位检查，事件驱动替代轮询）
        if self._anno_mode and self._anno is not None:
            self._anno.mouse_move(self.mapToScene(e.pos()))
            self._fgroup.maybe_yield()
            return
        # 拖动窗口：跟手移动
        if self._drag_offset is not None:
            self.move(e.globalPosition().toPoint() - self._drag_offset)
            return
        pt = self.mapToScene(e.pos())
        # 表格模式：拖动扩展选中矩形
        if self._table_mode and self._tbl_dragging:
            self._table_move(pt)
            return
        # 悬停光标：边缘拉伸（四边/角，含顶边）
        if e.buttons() == Qt.NoButton:
            edge = self._edge_at(e.position())
            if edge is not None:
                self.setCursor(self._RESIZE_CURSORS[edge])
            else:
                self.unsetCursor()
        if not self.dragging or self.active < 0:
            return
        self.focus = self._hit_or_clamp(pt)
        self.ranges[self.active] = self._norm(self.anchor, self.focus)
        self._redraw()
        self._preview_selection()   # 拖动中：防抖刷新预览（松手时立即刷新）

    def mouseReleaseEvent(self, e):
        # 中键平移结束
        if e.button() == Qt.MiddleButton and self._pan_start is not None:
            self._pan_start = None
            self.unsetCursor()
            return
        # 边缘拉伸结束：恢复滚动条策略（拉伸中隐藏防闪烁）+ 附属浮层重排
        if e.button() == Qt.LeftButton and self._resize_edge is not None:
            self._end_edge_resize()
            return
        # 标注模式：完成当前绘制项
        if self._anno_mode and self._anno is not None:
            self._anno.mouse_release(self.mapToScene(e.pos()))
            # 绘制中让位隐藏的浮层，画完立即恢复显示并整组提级
            self._fgroup.restore_all()
            self._fgroup.raise_all()
            return
        # 表格模式：完成矩形选取 → 复制 TSV
        if self._table_mode and self._tbl_dragging:
            self._tbl_dragging = False
            self._table_release()
            return
        if self._drag_offset is not None:
            self._drag_offset = None
            return
        if not self.dragging:
            return
        self.dragging = False
        # 零长度选区（点击未拖拽）移除
        if self.active >= 0:
            o0, c0, o1, c1 = self.ranges[self.active]
            if o0 == o1 and c0 == c1:
                del self.ranges[self.active]
                self.active = -1
                self._redraw()
                self._set_preview("")
                return
        self._do_copy()

    def mouseDoubleClickEvent(self, e):
        # 标注模式：双击已提交文字 → 重新编辑（画其它标注后仍可回来改文字）
        if self._anno_mode and self._anno is not None:
            self._anno.mouse_double_click(self.mapToScene(e.pos()))
            return
        # 表格模式：双击打开表格窗口
        if self._table_mode:
            self.table_window_requested.emit()
            return
        pt = self.mapToScene(e.pos())
        hit = self._hit(pt)
        if hit is None:
            return
        pos, _ = hit
        g = self.geom[self.box_order[pos]]
        rng = [pos, 0, pos, max(0, g["n"])]
        ctrl = bool(e.modifiers() & Qt.ControlModifier)
        if ctrl:
            self.ranges.append(rng)
            self.active = len(self.ranges) - 1
        else:
            self.ranges = [rng]
            self.active = 0
        self._redraw()
        self._preview_selection(immediate=True)   # 双击整行 = 最终态
        self._do_copy()

    # ---- 滚轮：图片缩放（zoom_modifier，默认 Ctrl）/ 窗口缩放（geom_zoom_modifier，
    # ---- 默认 Shift）/ 直接滚轮 = 平移查看（滚动条自动出现后直接可滚）----
    def wheelEvent(self, e):
        if self._anno_mode or self._table_mode:
            super().wheelEvent(e)      # 标注/表格模式不缩放（避免与绘制冲突）
            return
        mods = e.modifiers()
        dy = e.angleDelta().y()
        if dy == 0:
            return
        factor = 1.15 if dy > 0 else 1 / 1.15
        # 图片缩放（窗口内，窗口尺寸不变——只改 transform，绝不动窗口几何）
        if self._zoom_mod != "off" and self._held(mods, self._zoom_mod):
            self.activateWindow()          # 抢焦点：防止 Windows 将滚轮串给桌面
            self._set_image_zoom(self._img_zoom * factor, e.position())
            e.accept()
            return
        # 窗口几何缩放（窗口 + 图片一起缩放）
        if self._geom_zoom_mod != "off" \
                and self._held(mods, self._geom_zoom_mod):
            self.activateWindow()
            self._set_zoom(self._zoom * factor, e.position())
            e.accept()
            return
        super().wheelEvent(e)          # 直接滚轮：平移查看

    @staticmethod
    def _held(mods, name: str) -> bool:
        """修饰键是否被按下（ctrl/alt/shift）。"""
        return (name == "ctrl" and bool(mods & Qt.ControlModifier)) \
            or (name == "alt" and bool(mods & Qt.AltModifier)) \
            or (name == "shift" and bool(mods & Qt.ShiftModifier))

    def _apply_transform(self):
        """统一视图变换：系数 = 几何缩放 × 图片缩放（窗口宽高比与图片解耦后，
        图片大小只由这两个系数决定，不随窗口边缘拉伸变化）。"""
        self.resetTransform()
        self.scale(self._zoom * self._img_zoom / self.dpr,
                   self._zoom * self._img_zoom / self.dpr)

    def _set_image_zoom(self, zoom: float, anchor: QPointF):
        """窗口内图片缩放：**只改 transform，绝不动窗口几何**（窗口尺寸不变，
        图片在窗口内缩放；超出部分靠滚动条/中键平移查看）。
        以鼠标位置为锚：缩放前后鼠标指向的场景点保持不动（滚动条补偿）。"""
        zoom = max(0.1, min(10.0, zoom))
        if abs(zoom - self._img_zoom) < 1e-3:
            return
        t0 = self.transform().m11()
        if t0 <= 0:
            return
        self._img_zoom = zoom
        self._apply_transform()
        t1 = self.transform().m11()
        # 滚动条补偿：锚点（视口坐标）下的场景点保持不动。
        # 正确公式 v1 = (a + v0) * t1/t0 - a（推导：锚点场景坐标 s=(a+v0)/t0，
        # 缩放后要求 a = s*t1 - v1）。原实现 v0 + a*(1/t0 - 1/t1) 仅在
        # v0=0（未平移）时近似成立——大图平移后再缩放，鼠标指向点会漂移
        hb, vb = self.horizontalScrollBar(), self.verticalScrollBar()
        hb.setValue(int((anchor.x() + hb.value()) * t1 / t0 - anchor.x()))
        vb.setValue(int((anchor.y() + vb.value()) * t1 / t0 - anchor.y()))
        self._layout_close_btn()
        self._fgroup.follow()
        self._update_navigator()

    def _set_zoom(self, zoom: float, anchor: QPointF):
        """以窗口内 anchor 点为锚缩放窗口（几何缩放）：窗口尺寸 = 当前基准 × zoom，
        锚点下场景坐标保持不动（鼠标指向处不漂移）。图片缩放系数 _img_zoom 保留。"""
        zoom = max(0.05, min(5.0, zoom))
        if abs(zoom - self._zoom) < 1e-3:
            return
        self._zoom = zoom
        nw = max(1, int(round(self._base_w * zoom)))
        nh = max(1, int(round(self._base_h * zoom)))
        # 锚点在窗口内的相对位置（缩放后保持不变）
        rx = anchor.x() / max(1, self.width())
        ry = anchor.y() / max(1, self.height())
        # 新窗口左上角 = 锚点全局位置 - 相对偏移 × 新尺寸
        g = self.mapToGlobal(anchor.toPoint())
        nx = g.x() - rx * nw
        ny = g.y() - ry * nh
        self.setGeometry(int(nx), int(ny), nw, nh)
        self._apply_transform()
        self._layout_close_btn()
        self._fgroup.follow()
        self._layout_navigator()
        self._update_navigator()

    # ---- 窗口边缘拉伸（鼠标拖边/角调整窗口大小；图片等比 contain 不变形）----
    _EDGE = 6          # 边缘热区宽度（逻辑像素）
    _RESIZE_CURSORS = {
        "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor,
        "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
        "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor,
        "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor,
    }

    def _edge_at(self, pos) -> str | None:
        """返回鼠标位置命中的拉伸边/角；顶部中间 = 拖动把手区（返回 None，
        保留原顶部把手移动窗口行为），仅顶边两角可对角拉伸。
        尺寸以 viewport 为基准（e.position() 是视口坐标，滚动条出现时视口
        小于窗口，右/下热区落在滚动条内侧）。"""
        vp = self.viewport()
        w, h = vp.width(), vp.height()
        x, y = pos.x(), pos.y()
        ed = self._EDGE
        left = x <= ed
        right = x >= w - ed
        top = y <= ed
        bottom = y >= h - ed
        if top:
            if x <= ed * 2:
                return "tl"
            if x >= w - ed * 2:
                return "tr"
            return "t"             # 顶边中间也可拉伸（移动窗口用 Alt+拖动）
        if bottom:
            if x <= ed * 2:
                return "bl"
            if x >= w - ed * 2:
                return "br"
            return "b"
        if left:
            return "l"
        if right:
            return "r"
        return None

    def _start_edge_resize(self, e, edge: str):
        """开始边缘拉伸：记录 (全局点, 当前窗口几何)；拉伸中隐藏滚动条
        （避免随窗口缩放出现/消失造成闪烁），松手恢复。

        松手保证 = 下面的 `viewport().grabMouse()`（不是 QApplication 级事件
        过滤器）：抓取后松手事件无论光标在哪都回到 viewport → 本视图
        mouseReleaseEvent → _end_edge_resize；_end_edge_resize 自己会 releaseMouse。
        （模块顶部 __init__ 里的说明记录了「为什么可以不要 app 级兜底」。）
        """
        self._resize_edge = edge
        self._resize_geom = (e.globalPosition().toPoint(), self.geometry())
        self._manual_resize = True
        self._sb_prev = (self.horizontalScrollBarPolicy(),
                         self.verticalScrollBarPolicy())
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 抓取 viewport（而非 view 本体）鼠标：保证松手事件一定回到本窗、
        # 即便在窗口外/桌面上释放，_end_edge_resize 也能被触发，避免状态卡死。
        self.viewport().grabMouse()

    def _do_edge_resize(self, g):
        """拖动中按边缘类型调整窗口几何。
        - 等比例锁定（默认）：宽高比锁定图片比例（任意边/角拉伸都是等比缩放，
          图片恒填满窗口，无透明区、不变形）。
        - 解锁：窗口自由调整比例（水平/垂直独立），图片等比 contain 适配。"""
        g0, geo = self._resize_geom
        dx = g.x() - g0.x()
        dy = g.y() - g0.y()
        e = self._resize_edge
        x, y = geo.x(), geo.y()
        w, h = geo.width(), geo.height()
        if self._ratio_lock:
            ratio = (self._img_lw / self._img_lh) if self._img_lh > 0 else 1.0
            if "l" in e:
                w = max(80, geo.width() - dx)
            elif "r" in e:
                w = max(80, geo.width() + dx)
            if "t" in e:
                h = max(60, geo.height() - dy)
            elif "b" in e:
                h = max(60, geo.height() + dy)
            if "l" in e or "r" in e:
                h = max(60, int(round(w / ratio)))   # 水平主导
            else:
                w = max(80, int(round(h * ratio)))   # 垂直主导
        else:
            # 解锁：自由比例（独立调整宽高）
            if "l" in e:
                x = geo.x() + dx
                w = geo.width() - dx
            if "r" in e:
                w = geo.width() + dx
            if "t" in e:
                y = geo.y() + dy
                h = geo.height() - dy
            if "b" in e:
                h = geo.height() + dy
            w = max(80, w)
            h = max(60, h)
        if "l" in e:
            x = geo.right() - w
        if "t" in e:
            y = geo.bottom() - h
        self.setGeometry(x, y, w, h)

    def _end_edge_resize(self):
        """边缘拉伸结束：恢复滚动条策略（拉伸中隐藏防闪烁）+ 附属浮层重排。
        由 mouseReleaseEvent、滚动条事件过滤器、以及全局 MouseButtonRelease
        捕获共用（松手在窗口外/状态栏上也一定结束，避免 _resize_edge 卡死）。"""
        # 释放拉伸中可能抓取的 viewport 鼠标（保证不卡死）
        vp = self.viewport()
        if vp.mouseGrabber() is vp:
            vp.releaseMouse()
        if self._resize_edge is None:
            return
        self._resize_edge = None
        self._resize_geom = None
        self._manual_resize = False
        if getattr(self, "_sb_prev", None):
            self.setHorizontalScrollBarPolicy(self._sb_prev[0])
            self.setVerticalScrollBarPolicy(self._sb_prev[1])
            self._sb_prev = None
        self._layout_close_btn()
        self._fgroup.follow()
        self._layout_navigator()
        self._update_navigator()

    def eventFilter(self, obj, event):
        """事件过滤器（当前只装在**本窗口的两个滚动条对象**上，不再装到 QApplication）：
        - 滚动条事件拦截：解锁态图片>窗口时滚动条占据窗口右/下边缘，原生会
          截获鼠标事件，导致 ResultOverlay 收不到边沿位置、右/下/角无法拉伸
          （已知 Qt 结构限制）。此处把滚动条最外侧 _EDGE px 的鼠标事件转交边缘
          拉伸：按下即 _start_edge_resize（隐藏滚动条），后续拖动由 viewport 的
          mouseMoveEvent 接管；其余区域滚动条照常滚动。
        - 分支 1（拉伸中松左键 → 结束拉伸）保留：装到滚动条上时，它是「press 被
          滚动条吃下、拖动却落在滚动条上」这一路径的收尾兜底。历史上它被当作
          app 级全局兜底（每个贴图窗口一个过滤器），已按 _start_edge_resize 的
          grabMouse 论证移除——若日后要恢复全局兜底，把 QApplication 上也装一份
          本过滤器即可（详见 __init__ 里的说明）。
        """
        # —— 全局兜底：拉伸中松左键 → 结束拉伸（不吞事件，照常传递）——
        if event.type() == QEvent.MouseButtonRelease \
                and event.button() == Qt.LeftButton \
                and self._resize_edge is not None:
            self._end_edge_resize()
            return False
        sb_v = self.verticalScrollBar()
        sb_h = self.horizontalScrollBar()
        if obj is sb_v or obj is sb_h:
            et = event.type()
            if et in (QEvent.MouseButtonPress, QEvent.MouseMove,
                      QEvent.MouseButtonRelease):
                if self._anno_mode or self._table_mode or self._pinned:
                    return False
                edge = self._sb_edge(obj, event)
                if edge is None:
                    return False
                if et == QEvent.MouseButtonPress:
                    if event.button() == Qt.LeftButton:
                        self._start_edge_resize(event, edge)
                        return True     # 吃下事件：滚动条进入 AlwaysOff，拖动交 viewport
                    return False
                if et == QEvent.MouseMove:
                    if self._resize_edge is not None \
                            and (event.buttons() & Qt.LeftButton):
                        # 滚动条已隐藏，正常由 viewport 接管；此处兜底驱动一次
                        self._do_edge_resize(event.globalPosition().toPoint())
                        return True
                    if event.buttons() == Qt.NoButton:
                        cur = self._RESIZE_CURSORS.get(edge)
                        if cur is not None:
                            self.setCursor(cur)
                    return True
                if et == QEvent.MouseButtonRelease:
                    if event.button() == Qt.LeftButton \
                            and self._resize_edge is not None:
                        self._end_edge_resize()
                        return True
                    return False
        return super().eventFilter(obj, event)

    def _sb_edge(self, sb, event) -> str | None:
        """滚动条最外侧 _EDGE px 命中 = 对应窗口边/角；否则 None（滚动条照常）。
        垂直滚动条在右 → 'r'（细分 'tr'/'br'）；水平滚动条在下 → 'b'（细分 'bl'/'br'）。"""
        p = event.position()
        x, y = p.x(), p.y()
        ed = self._EDGE
        if sb is self.verticalScrollBar():
            if x < sb.width() - ed:       # 不在右边缘热区
                return None
            if y <= ed * 2:
                return "tr"
            if y >= sb.height() - ed * 2:
                return "br"
            return "r"
        # 水平滚动条
        if y < sb.height() - ed:          # 不在下边缘热区
            return None
        if x <= ed * 2:
            return "bl"
        if x >= sb.width() - ed * 2:
            return "br"
        return "b"

    def _recontain(self):
        """窗口被手动拉伸后重新适配：
        - 等比例锁定：窗口 = 图片等比尺寸，图片填满窗口（zoom = 窗口宽/图片宽）；
        - 解锁：**窗口大小与图片大小解耦**——图片保持当前显示尺寸不变，
          窗口只是视口：图片大于窗口时滚动条自动出现（中键拖动/滚轮查看），
          图片小于窗口时居中（AlignCenter）。
        以图片逻辑尺寸为缩放基准，几何缩放连续。"""
        lw, lh = self._img_lw, self._img_lh
        if lw <= 0 or lh <= 0:
            return
        self._base_w = int(math.ceil(lw))
        self._base_h = int(math.ceil(lh))
        if self._ratio_lock:
            self._zoom = max(0.01, self.width() / lw)
        # 解锁：不动 zoom/img_zoom（图片尺寸固定，仅窗口为视口）
        self._apply_transform()

    def fit_to_screen(self):
        """滚动截图长图：窗口等比缩放到**整图**在屏幕内可见。

        普通贴图窗口尺寸=框选区域；长图比区域高得多，若不缩放只能看到
        顶部一屏（视觉上"图片被限制在屏幕高度"）。此处把缩放基准改成
        整图逻辑尺寸，等比放入可用屏幕（占 90%），窗口水平居中。
        """
        h, w = self.img.shape[:2]
        lw, lh = w / self.dpr, h / self.dpr
        # 用窗口所在屏（多显示器下按主屏算会把图缩到与所在屏不匹配的尺寸）
        scr = screen_available_geometry(self)
        zoom = min(1.0, scr.width() * 0.9 / lw, scr.height() * 0.9 / lh)
        zoom = max(0.05, zoom)
        nw = max(1, int(round(lw * zoom)))
        nh = max(1, int(round(lh * zoom)))
        x = scr.left() + max(4, (scr.width() - nw) // 2)
        y = scr.top() + max(4, (scr.height() - nh) // 4)
        self._base_w = int(math.ceil(lw))  # 基准 = 图片逻辑尺寸（几何缩放连续）
        self._base_h = int(math.ceil(lh))
        self._zoom = zoom
        self._img_zoom = 1.0              # 整图适配后重置窗口内图片缩放
        self.setGeometry(x, y, nw, nh)
        self._apply_transform()
        self._layout_close_btn()
        self._fgroup.follow()
        self._layout_navigator()
        self._update_navigator()

    # ---- 导航图（缩略小地图）----
    def _toggle_navigator(self):
        """导航图开关（快捷键可配置，默认 M；标注模式跳过，避免误触）。"""
        if self._anno_mode:
            return
        strip = self._status_strip
        if strip is not None:
            strip.btn_nav.setChecked(not strip.btn_nav.isChecked())

    def _toggle_ratio_lock(self):
        """等比例调节窗口开关（快捷键 E / 状态栏按钮）：翻转按钮即翻转状态。"""
        strip = self._status_strip
        if strip is not None:
            strip.btn_ratio.setChecked(not strip.btn_ratio.isChecked())

    def _set_ratio_lock(self, on: bool):
        """状态栏等比例按钮状态 → 同步锁定状态；解锁/重新锁定时立即重新适配
        图片（锁定=填满窗口；解锁=图片等比 contain 居中）。"""
        self._ratio_lock = bool(on)
        self._recontain()
        self._set_status("等比例调节窗口：锁定（窗口与图片同比例）" if on
                         else "等比例调节窗口：解锁（窗口可自由比例）")

    def set_navigator(self, on: bool):
        """状态栏导航图开关：开 = 显示缩略小地图（点击/拖动跳转大图）。"""
        self._nav_on = bool(on)
        if on:
            self._ensure_navigator()
            nav = self._navigator
            nav.show()
            nav.raise_()
            self._layout_navigator()
            self._update_navigator()
        elif self._navigator is not None:
            self._navigator.hide()

    def _ensure_navigator(self):
        if self._navigator is not None:
            return
        from ui.components.navigator import Navigator
        nav = Navigator()
        nav.set_image(self._pix)
        nav.jumped.connect(self._on_nav_jump)
        # 松手后还焦主窗口（Esc 等快捷键继续生效）
        nav.interacted.connect(
            lambda: (self.activateWindow(),
                     self.setFocus(Qt.OtherFocusReason)))
        self._navigator = nav

    def _layout_navigator(self):
        nav = self._navigator
        if nav is None or not self._nav_on or self._pinned:
            return
        x = self.x() + self.width() - nav.width() - 8
        y = self.y() + self.height() - nav.height() - 8
        nav.move(max(0, x), max(0, y))

    def _update_navigator(self):
        nav = self._navigator
        if nav is None or not self._nav_on:
            return
        view = self.mapToScene(self.viewport().rect()).boundingRect()
        nav.set_view(view)

    def _on_nav_jump(self, sx: float, sy: float):
        """导航图点击/拖动：跳转主视图（场景点居中）。"""
        vp = self.viewport()
        t = self.transform()
        self.horizontalScrollBar().setValue(
            max(0, int(sx * t.m11() - vp.width() / 2)))
        self.verticalScrollBar().setValue(
            max(0, int(sy * t.m22() - vp.height() / 2)))

    # ---- M2：全选（Ctrl+A 固定）----
    def select_all(self):
        """选中当前贴图全部文字（仅蓝色高亮，不复制）。
        表格模式下 = 选中全部网格单元格。"""
        if self._table_mode and self._table_rows is not None:
            self._tbl_sel = (0, 0, len(self._table_rows) - 1,
                             len(self._table_cols) - 1)
            self._draw_table_sel()
            self._set_status(
                f"已全选表格 {len(self._table_rows)} 行 × "
                f"{len(self._table_cols)} 列 · Shift+C 复制 TSV")
            return
        n = len(self.box_order)
        if n == 0:
            return
        self.ranges = [[i, 0, i, self.geom[self.box_order[i]]["n"]] for i in range(n)]
        self.active = n - 1
        self._redraw()
        self._preview_selection(immediate=True)   # 全选 = 最终态
        self._set_status(f"已全选 {n} 块 · Shift+C 复制 / Esc 关闭")

    def copy_all(self):
        """全选并复制全部文字进剪贴板（Shift+C / 可配置复制快捷键）。"""
        self.select_all()
        self._do_copy()

    def _match_shortcut(self, e, combo) -> bool:
        """按键事件是否命中该热键。

        修饰键必须**完全相等**，不能用子集判断：单键配置（如状态栏开关 `s`，
        `mods=0`）时 `(e.modifiers() & 0) == 0` 恒真，于是 Ctrl+S 也会命中——
        原先只靠「先判组合键、后判单键」的判定顺序巧合避开。本轮改为全等比较，
        并屏蔽 KeypadModifier / GroupSwitchModifier（小键盘与 Windows AltGr 会
        带上它们，与用户配置无关）。
        """
        if combo is None:
            return False
        mods, key = combo
        mask = _qt_flag_value(Qt.ShiftModifier | Qt.ControlModifier
                              | Qt.AltModifier | Qt.MetaModifier)
        return (e.key() == key
                and (_qt_flag_value(e.modifiers()) & mask)
                == (_qt_flag_value(mods) & mask))

    def show_shortcut_panel(self):
        """快捷键速查面板（UI 入口：设置面板调用，不绑定 Ctrl+/）。"""
        from ui.components.shortcut_panel import ShortcutPanel
        if self._shortcut_panel is None:
            self._shortcut_panel = ShortcutPanel(cfg=self.config)
        self._shortcut_panel.show()
        self._shortcut_panel.raise_()

    def _on_strip_esc(self):
        """纯信息状态栏 Esc：标注模式先退出标注；否则关闭状态栏（Esc 关闭，
        不设 ✕ 按钮；可在设置里用「统一工具栏/状态栏」开关重新启用）。"""
        if self._anno_mode:
            self._exit_annotation()
            return
        if self._status_strip is not None:
            self._status_strip.hide()
            self._sync_toolbar_pos()    # 状态栏关闭 → 工具栏立即上移贴图片下方

    def _toggle_status_strip(self):
        """状态栏开关（快捷键，默认 S，仅对激活的贴图窗口有效）：
        显示/隐藏纯信息状态栏；Esc 关闭后用此键重新启用。"""
        if self._status_strip is None:
            return
        self._status_strip.setVisible(not self._status_strip.isVisible())
        if self._status_strip.isVisible():
            self._status_strip.raise_()
        self._sync_toolbar_pos()        # 状态栏显隐变化 → 工具栏立即跟随重定位

    def _on_esc(self):
        """Esc 统一处理（图片窗口按键 / 统一工具栏转发共用）：
        标注模式先取消编辑/退出标注；有选区清选区；否则关闭图片。"""
        if self._anno_mode:
            # 先取消进行中的文字就地编辑，否则退出标注
            if self._anno is not None and self._anno.cancel_text_edit():
                return
            self._exit_annotation()               # 退出标注模式
            return
        if self.ranges:
            self.ranges = []
            self.active = -1
            self._redraw()
            self._set_preview("")
            self._set_status("已清除选区")
        else:
            self.close()

    def keyPressEvent(self, e):
        # 标注模式快捷键
        if self._anno_mode:
            if e.key() == Qt.Key_Escape:
                self._on_esc()
                return
            if self._match_shortcut(e, self._redo_hk):
                self._anno.redo()                 # 重做（可配置）
                return
            if self._match_shortcut(e, self._undo_hk):
                self._anno.undo()                 # 撤销（可配置）
                return
            # 文字就地编辑中：其余按键（字母/数字/空格/退格/方向键/Enter 等）
            # 必须转发给场景的文本项，否则字母数字无法输入（输入法文字走 IME 例外）
            if self._anno is not None and self._anno._text_edit is not None:
                super().keyPressEvent(e)
                return
            # 选择工具：Delete / Backspace 删除选中标注
            if (e.key() in (Qt.Key_Delete, Qt.Key_Backspace)
                    and self._anno is not None
                    and self._anno.tool == "select"):
                if self._anno.delete_selected():
                    return
            # Q/W/E/R/T/Y/U 等标注态通用键（与工具栏/子浮层转发共用同一分发）
            if self.handle_anno_key(e):
                return
        # 进入/退出标注模式（可配置，默认 Ctrl+Shift+D）
        if self._match_shortcut(e, self._anno_hk):
            self._toggle_annotation()
            return
        # 常用态工具栏键（Q/W/E/F 二维码/文字框/表格/收藏）
        if self._handle_toolbar_key(e):
            return
        if e.key() == Qt.Key_Escape:
            self._on_esc()
        elif self._match_shortcut(e, self._copy_raw_image_hk):
            self._copy_raw_image()                  # 复制原图（不含标注，可配置）
        elif self._match_shortcut(e, self._copy_image_hk):
            self._copy_image()                      # 复制整张图片（可配置）
        elif self._match_shortcut(e, self._copy_sel_hk):
            self._do_copy()                         # 复制选中文字（可配置）
        elif self._match_shortcut(e, self._copy_shortcut):
            self.copy_all()                         # 复制全部文字（可配置）
        elif self._match_shortcut(e, self._select_all_hk):
            self.select_all()                       # 全选（可配置，仅选中不复制）
        elif self._match_shortcut(e, self._reocr_hk):
            self.reocr_requested.emit()             # 重新 OCR（可配置）
        elif self._match_shortcut(e, self._table_hk):
            self.table_requested.emit()             # 表格识别（可配置）
        elif self._match_shortcut(e, self._strip_hk):
            # 状态栏开关（默认 S）：无修饰键才触发，避免 Ctrl+S 等误开
            if not (e.modifiers() & (Qt.ControlModifier | Qt.AltModifier
                                     | Qt.ShiftModifier | Qt.MetaModifier)):
                self._toggle_status_strip()

    def closeEvent(self, _e):
        # 停掉待触发的「自动进入标注」定时器（set_boxes 排队），避免窗口
        # 销毁后回调 _enter_annotation
        if getattr(self, "_anno_auto_timer", None) is not None:
            self._anno_auto_timer.stop()
        # 停掉待触发的选区预览防抖定时器（拖动中关窗时排队），避免销毁后回调
        if getattr(self, "_preview_timer", None) is not None:
            self._preview_timer.stop()
        # 关闭图片前：若仍在标注模式且有未落库标注 → 先退出标注触发保存
        # （任何关闭方式：✕ / Esc / 完成 / 程序退出，标注都不丢失）
        if getattr(self, "_anno_mode", False):
            try:
                self._exit_annotation()
            except Exception:  # noqa: BLE001
                pass
        if self._shortcut_panel is not None:
            self._shortcut_panel.close()
            self._shortcut_panel.deleteLater()
            self._shortcut_panel = None
        if getattr(self, "_fgroup", None) is not None:
            self._fgroup.shutdown()
        # 独立关闭按钮/状态栏/工具栏跟随关闭并销毁：图片窗口关闭时不能残留
        if getattr(self, "btn_close", None) is not None:
            self.btn_close.close()
            self.btn_close.deleteLater()
            self.btn_close = None
        if getattr(self, "_navigator", None) is not None:
            self._navigator.close()
            self._navigator.deleteLater()
            self._navigator = None
        if self._pin_card is not None:
            self._pin_card.close()
            self._pin_card.deleteLater()
            self._pin_card = None
        if self._pin_dot is not None:
            self._pin_dot.close()
            self._pin_dot.deleteLater()
            self._pin_dot = None
        if self._status_strip is not None:
            self._status_strip.close()
            self._status_strip.deleteLater()
            self._status_strip = None
        if self._toolbar is not None:
            self._toolbar.close()
            self._toolbar.deleteLater()
            self._toolbar = None
        # 释放图像与场景资源（修复关闭后内存不回收：4K 图 self.img + QPixmap +
        # QGraphicsScene 图元在关闭时未释放，累积导致 RSS 持续上涨）
        self.img = None
        self._pix = None
        self._pix_item = None
        self._anno = None
        if getattr(self, "scene", None) is not None:
            try:
                self.scene.clear()
            except Exception:  # noqa: BLE001
                pass
            self.setScene(None)
            self.scene = None
        if getattr(self, "_border_overlay", None) is not None:
            self._border_overlay.deleteLater()
            self._border_overlay = None
        self.closed.emit()

    # ---- 点击穿透（Windows WS_EX_TRANSPARENT，Alt+F 全局热键切换）----
    def toggle_through(self) -> bool:
        """切换点击穿透：鼠标点击落到桌面（窗口仍可见），Alt+F 可随时取消。

        Windows 用 SetWindowLongPtrW 设 WS_EX_TRANSPARENT | WS_EX_LAYERED
        （仅穿透鼠标，不影响显示）；对自身（贴图态）与状态栏钉子（钉图态）
        同时生效，保证钉图后钉子也不挡鼠标。返回当前穿透状态。
        """
        self._through = not getattr(self, "_through", False)
        if IS_WINDOWS:
            try:
                import ctypes
                user32 = ctypes.windll.user32
                GWL_EXSTYLE = -20
                WS_EX_TRANSPARENT = 0x00000020
                WS_EX_LAYERED = 0x00080000
                targets = [self]
                if self._status_strip is not None:
                    targets.append(self._status_strip)
                for w in targets:
                    try:
                        hwnd = int(w.winId())
                    except Exception:            # noqa: BLE001
                        continue
                    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) or 0
                    if self._through:
                        user32.SetWindowLongPtrW(
                            hwnd, GWL_EXSTYLE,
                            style | WS_EX_TRANSPARENT | WS_EX_LAYERED)
                    else:
                        user32.SetWindowLongPtrW(
                            hwnd, GWL_EXSTYLE, style & ~WS_EX_TRANSPARENT)
            except Exception:                    # noqa: BLE001
                pass
        self._set_status("点击穿透开启（Alt+F 取消）" if self._through
                         else "已取消点击穿透")
        return self._through

    def nativeEvent(self, eventType, message):
        """透明分层窗口（WA_TranslucentBackground → WS_EX_LAYERED）默认会对
        alpha=0 的像素做穿透命中：点击透明处会落到背后程序。非点击穿透模式下，
        对 WM_NCHITTEST 强制返回 HTCLIENT，使整窗（含透明区域）可命中、点击被
        本窗捕获；点击穿透模式（_through，Alt+F）则放行走默认（WS_EX_TRANSPARENT
        生效，整窗穿透）。

        注意：eventType 字符串在不同 PySide6 版本里可能不同，故用 b"windows"
        子串宽松匹配；message 直接按 MSG 结构偏移读取 message 字段（@ 偏移 8），
        避免整段 MSG.from_address 在某些环境抛异常被吞、导致回退默认穿透。
        """
        if IS_WINDOWS and b"windows" in bytes(eventType):
            try:
                import ctypes
                p = int(message)
                # MSG（LP64/Win64）：hwnd(8B) + message(UINT 4B @ 偏移 8)
                msg_id = ctypes.c_ulong.from_address(p + 8).value
                if msg_id == 0x84:    # WM_NCHITTEST
                    if getattr(self, "_through", False):
                        return (0, False)   # 穿透模式：交默认处理（WS_EX_TRANSPARENT 生效）
                    return (1, True)        # HTCLIENT：整窗可命中（含透明区域）
            except Exception:             # noqa: BLE001
                pass
        return super().nativeEvent(eventType, message)

    # ---- M2-7：标注模式 ----
    def _toggle_annotation(self):
        """Ctrl+Shift+D / 快捷键：切换标注模式（QShortcut 与 keyPressEvent 共用）。"""
        if self._anno_mode:
            self._exit_annotation()
        else:
            self._enter_annotation()

    def _on_anno_btn_toggled(self, on: bool):
        """「标注」按钮状态变化 → 同步标注模式（与文字框按钮同款状态显示）。"""
        if on:
            self._enter_annotation()
        else:
            self._exit_annotation()

    def showEvent(self, e):
        # 抢键盘焦点：确保 keyPressEvent（Ctrl+Shift+D / Ctrl+R / Ctrl+A 等）能收到
        super().showEvent(e)
        self.activateWindow()
        self.setFocus(Qt.OtherFocusReason)
        self._fgroup.raise_all()        # 整组提级（主窗 + 状态栏等）
        self._layout_close_btn()        # 独立关闭按钮跟随显示（避免创建时未显示）
        self._fgroup.follow()           # 状态栏浮层跟随图片窗口（钉图态钉子定位）

    def moveEvent(self, e):
        super().moveEvent(e)
        self._layout_close_btn()
        self._fgroup.follow()           # 状态栏（含钉图钉子）跟随图片
        self._layout_navigator()

    def _clamp_bounds(self) -> QRect:
        """附属浮窗（状态栏/工具栏）的夹取范围 = **整个虚拟桌面的可用区域并集**。

        不能按「本窗口所在那块屏」夹取：跨屏截图时本窗口横跨两屏，按单屏夹取会把
        附属浮窗整个拽到那一块屏上（实测：图片横跨主副屏时，状态栏/工具栏被拉回
        主屏）。
        也不能靠「取点最近的屏」绕开：Qt 的逻辑虚拟桌面在不同分辨率 + 相同缩放下
        存在**不属于任何屏的缝隙**（实测主屏逻辑宽 2560、副屏逻辑原点 3840，中间
        2560~3840 无人认领），而缝隙点到**主屏**的矩形距离更近 —— 按屏判定必然
        偏主屏。所以这一层不该按屏算，直接夹到并集：既不出屏，又绝不跨屏拽动。
        """
        u = desktop_union_available()
        return u if not u.isEmpty() else screen_available_geometry(self)

    def _sync_status_strip_pos(self):
        """纯信息状态栏跟随图片窗口：宽度与图片一致（用户拉伸后保持用户宽度），
        最大 1400px（大图不铺满全屏）；位于图片正下方、水平居中。
        钉图态状态栏已隐藏（只留 PinDot 钉子），直接返回。"""
        if self._status_strip is None:
            return
        if self._pinned:
            return
        s = self._status_strip
        # 宽度始终跟随「贴图窗口」宽度（用户拉伸后保持用户宽度）；解锁态图片尺寸
        # 与窗口解耦、不再跟随图片显示宽。最大 1400px（大图不铺满全屏）。
        # 高度用 sizeHint（内容稳定）。不用 adjustSize()：它每次先恢复到内容宽度
        # 再被 resize 压回，拖拽移动时宽度跳变导致状态栏右边闪烁。
        uw = getattr(s, "_user_width", None)
        base_w = self.width()
        w = uw if uw else min(base_w, 1400)     # 最大宽度限制
        w = max(s.minimumWidth(), min(w, 1400))
        s.resize(w, s.sizeHint().height())
        scr = self._clamp_bounds()   # 夹到虚拟桌面并集，不按单屏（跨屏时会被拽走）
        # 水平居中于图片（图片 AlignCenter 居中于窗口 → 以窗口中心为准）；
        # 垂直锚定**窗口底边**（解锁留白时状态栏贴窗口而非悬在图片底边，消除间隙）
        x = self.x() + (self.width() - w) // 2
        y = self.y() + self.height() + 4
        if y + s.height() > scr.bottom():      # 窗口贴底：状态栏上移贴屏底
            y = scr.bottom() - s.height()
        s.move(max(scr.left(), min(x, scr.right() - w)),
               max(scr.top(), y))

    def _sync_toolbar_pos(self):
        """统一工具栏跟随图片窗口：位于状态栏正下方（无状态栏时贴图片下方），
        宽度自适应内容（常态短条 / 标注态内联展开变长）。"""
        if self._toolbar is None:
            return
        if self._pinned:
            return
        tb = self._toolbar
        scr = self._clamp_bounds()   # 夹到虚拟桌面并集，不按单屏（跨屏时会被拽走）
        tw = tb.sizeHint().width()
        th = tb.sizeHint().height()
        # 锚点：状态栏（水平以其中心为准；状态栏下方放不下 → 图片下方）
        if self._status_strip is not None and self._status_strip.isVisible():
            cx = self._status_strip.x() + self._status_strip.width() // 2
            anchor = self._status_strip.y() + self._status_strip.height()
        else:
            cx = self.x() + self.width() // 2
            anchor = self.y() + self.height()
        x = max(scr.left(), min(cx - tw // 2, scr.right() - tw))
        y = anchor + 4
        if y + th > scr.bottom():      # 下方放不下 → 翻到锚点上方
            y = anchor - th - 4
        tb.resize(tw, th)
        tb.move(max(scr.left(), min(x, scr.right() - tw)),
                max(scr.top(), y))
        tb.raise_()

    def _enter_annotation(self):
        if self._anno_mode:
            return
        from ui.components.annotation import AnnotationSession
        if self._anno is None:
            self._anno = AnnotationSession(self, self.scene, self._pix)
        self._anno_mode = True
        # 统一工具栏标注态：同一行内联展开工具段（无独立标注工具栏顶层窗）
        if self._toolbar is not None:
            self._toolbar.set_anno_mode(True)
            self._toolbar.btn_anno.setChecked(True)  # 标注按钮按下高亮
            self._anno.set_num_start(self._toolbar.num_start)
            self._anno.set_num_size(self._toolbar.num_size)
            self._anno.set_num_style(self._toolbar.num_style)
            self._anno.set_tool_style("rect", self._toolbar.rect_style)
            self._anno.set_tool_style("arrow", self._toolbar.arrow_style)
            self._anno.set_tool_style("line", self._toolbar.line_style)
            self._anno.set_tool_style("pen", self._toolbar.pen_style)
        self._fgroup.set_yield_enabled(True)   # 开启绘制让位（事件驱动，替代 80ms 轮询）
        self._fgroup.raise_all()
        self._sync_toolbar_pos()           # 工具段展开：工具栏变长重排
        self._set_status("标注模式：拖拽绘制 · Ctrl+Z 撤销 · Esc 退出")

    def _save_mosaic_mode(self, mode: str):
        """马赛克模式：更新会话 + 按开关持久化（下次打开标注保持上次选择）。"""
        if self._anno is not None:
            self._anno.set_mosaic_mode(mode)
        self._save_anno_param("mosaic_mode", mode)

    def _on_anno_color(self, c: tuple):
        """颜色变更：即时更新会话 + 按开关持久化。"""
        if self._anno is not None:
            self._anno.set_color(tuple(c))
        self._save_anno_param("tool_color", ",".join(str(int(v)) for v in c))

    def _on_anno_width(self, w: int):
        """线宽变更：即时更新会话 + 按开关持久化。"""
        if self._anno is not None:
            self._anno.set_width(w)
        self._save_anno_param("tool_width", str(w))

    def _on_num_start(self, v: int):
        """序号起始序号变更：更新会话起始值并重排已有序号 + 按开关持久化。"""
        self._save_anno_param("num_start", str(int(v)))
        if self._anno is not None:
            self._anno.set_num_start(int(v), renumber=True)

    def _on_num_size(self, v: int):
        """序号圆圈尺寸变更：即时更新会话 + 按开关持久化。"""
        self._save_anno_param("num_size", str(int(v)))
        if self._anno is not None:
            self._anno.set_num_size(v)

    def _on_num_style(self, v: str):
        """序号样式变更：即时更新会话（后续放置用新样式）+ 按开关持久化。"""
        self._save_anno_param("num_style", str(v))
        if self._anno is not None:
            self._anno.set_num_style(v)

    def _on_tool_style(self, tool: str, style: str):
        """矩形/箭头/画笔样式变更：即时更新会话 + 按开关持久化。"""
        self._save_anno_param(f"{tool}_style", str(style))
        if self._anno is not None:
            self._anno.set_tool_style(tool, style)

    # Q/W/E/R/T/Y/U/I/O → 标注工具**位置**（第 1~9 个，与工具名无关，顺序变化
    # 键位跟随；O 为预留位，工具不足 9 个时无效果，供以后插入的新工具使用）
    _TOOL_KEYS = ("q", "w", "e", "r", "t", "y", "u", "i", "o")
    _TOOL_KEY_NAMES = {"select": "选择", "num": "序号", "arrow": "箭头",
                       "pen": "画笔", "line": "直线", "rect": "矩形",
                       "mosaic": "马赛克", "text": "文字"}

    def _switch_tool_by_key(self, key) -> bool:
        """按 QWERTYU 位置映射切换标注工具；命中返回 True。"""
        from ui.components.annotation import TOOLS
        try:
            idx = [getattr(Qt, f"Key_{c.upper()}") for c in self._TOOL_KEYS] \
                .index(key)
        except ValueError:
            return False
        if idx >= len(TOOLS):
            return False
        tool = TOOLS[idx]
        tb = self._toolbar
        if tb is not None:
            tb.select_tool(tool)          # 切工具 + 高亮按钮 + 弹子选项浮层
        self._set_status(
            f"标注工具：{self._TOOL_KEY_NAMES.get(tool, tool)} ("
            f"{self._TOOL_KEYS[idx].upper()})")
        return True

    def handle_anno_key(self, e) -> bool:
        """标注态通用键盘分发（贴图窗口 / 工具栏 / 子选项浮层共用）。

        焦点可能落在工具栏按钮或子浮层上（独立顶层窗），Q/W/E/R/T/Y/U 与
        Esc 都转发到这里统一处理；文字就地编辑时保护输入不拦截。
        返回 True = 已处理。
        """
        if not self._anno_mode or self._anno is None:
            return False
        editing = self._anno._text_edit is not None
        if e.key() == Qt.Key_Escape:
            if editing:
                self._anno.cancel_text_edit()
            else:
                self._on_esc()
            return True
        if e.modifiers() == Qt.NoModifier and not editing:
            if self._switch_tool_by_key(e.key()):
                return True
        return False

    def _save_anno_param(self, key: str, value: str):
        """标注工具参数永久化记录（[ui] persist_params 开关控制）。"""
        if not self.config.getbool("ui", "persist_params", True):
            return
        try:
            self.config.cp.set("annotation", key, str(value))
            self.config.save()
        except OSError:
            pass

    def _exit_annotation(self):
        if not self._anno_mode:
            return
        self._anno_mode = False
        if getattr(self, "_fgroup", None) is not None:
            self._fgroup.set_yield_enabled(False)   # 关闭让位并恢复未恢复的浮窗
        if self._toolbar is not None:
            self._toolbar.set_anno_mode(False)   # 收起工具段（含关闭子浮层）
            self._toolbar.btn_anno.setChecked(False)  # 取消高亮
        if self._anno is not None:
            self.annotations_changed.emit(self._anno.json())
        self._set_status("已退出标注模式")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # 窗口边框覆盖层跟随窗口尺寸（始终框在最外层）
        ov = getattr(self, "_border_overlay", None)
        if ov is not None:
            ov.setGeometry(self.rect())
            ov.raise_()
        # 手动边缘拉伸中：图片等比 contain 适配新窗口（宽高比不变）
        if getattr(self, "_manual_resize", False):
            self._recontain()
        self._layout_close_btn()
        self._fgroup.follow()           # 统一工具栏（含钉图钉子）跟随图片
        self._layout_navigator()
        self._update_navigator()

    def _anno_undo(self):
        if self._anno is not None:
            self._anno.undo()

    def _anno_redo(self):
        if self._anno is not None:
            self._anno.redo()

    def _anno_clear(self):
        if self._anno is not None:
            self._anno.clear()

    def load_annotations(self, json_text: str | None):
        """恢复历史时载入标注（M2-7 持久化）。"""
        from ui.components.annotation import AnnotationSession, anno_from_json
        items = anno_from_json(json_text)
        if not items:
            return
        if self._anno is None:
            self._anno = AnnotationSession(self, self.scene, self._pix)
        self._anno.load(items)
