"""表格识别结果的 Qt 交互层：网格构建、单元格命中/选择、TSV 复制 —— 从 result_overlay.py 按职责搬移（纯搬移，行为零变化）。

归属断言见 scripts/_verify_review_fixes.py 的 5f 区块：方法一旦被别的 mixin 或
ResultOverlay 同名覆盖，MRO 会**静默遮蔽**（不报错、只是行为变了），故必须断言。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QApplication, QGraphicsRectItem

from ui.core.table_detect import (          # 表格检测纯算法（无 Qt 依赖）
    _coords_table_grid, _detect_table_lines, build_blobs, map_cell_texts,
)



class TableViewMixin:
    """表格交互层（只依赖 ui.core.table_detect 的纯算法）"""
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
        # 线宽可配（[ui] table_border_width，与「截图边框」同一处设置）；
        # 注意网格线是相邻格共享边，线宽加到 2 以上时相邻两条线会叠在一起
        # 显得更粗 —— 这是共享边界的固有表现，预览与导出观感一致。
        w = self._table_border_width
        for y0, y1 in self._table_rows:
            for x0, x1 in self._table_cols:
                item = QGraphicsRectItem(QRectF(x0, y0, x1 - x0, y1 - y0))
                item.setPen(QPen(QColor(c.red(), c.green(), c.blue(), 170), w))
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
