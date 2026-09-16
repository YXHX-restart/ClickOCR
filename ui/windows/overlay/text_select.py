"""文本命中与选区：字符几何、命中检测、选区绘制与复制 —— 从 result_overlay.py 按职责搬移（纯搬移，行为零变化）。

归属断言见 scripts/_verify_review_fixes.py 的 5f 区块：方法一旦被别的 mixin 或
ResultOverlay 同名覆盖，MRO 会**静默遮蔽**（不报错、只是行为变了），故必须断言。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QTimer
from PySide6.QtGui import QFont, QFontMetricsF

from platform_compat.fonts import ui_font_family
from ui.core.table_detect import _bbox

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

class TextSelectMixin:
    """文本命中与选区（字符几何 / 命中 / 选区）"""
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
