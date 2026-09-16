"""ui/table_panel.py — 表格识别结果预览面板（M2 由 M4 提前）。

功能（§7.2/7.3）：
- 识别完成后弹出，QTableWidget 渲染表格（可双击编辑单元格）
- 底部按钮：导出 Excel / 导出 Markdown / 复制 TSV / 关闭
- 操作失败（未安装 openpyxl 等）降级为提示，不崩溃

UI 约定：继承 `CardWindow` 基类（统一无边框卡片浮层），样式由
`ui/theme.py` 统一定义（#TablePreviewPanel 已纳入面板选择器 + QTableWidget
明暗规则），组件零内嵌 QSS。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QApplication,
)

from core_engine.ai_pipeline import TableCell, TableResult
from core_engine.table_export import TableExportService

from ui.base.card_window import CardWindow
from ui.base.texts import T
from ui.base.theme_dialog import ThemeDialog
from ui.base.widgets import screen_available_geometry


class _ResizeGrip(QWidget):
    """右下角缩放手柄：无边框窗体用鼠标拖动改变尺寸（手动实现，可靠可测）。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self.setCursor(Qt.SizeFDiagCursor)
        self.setToolTip(T("ui.resize_grip_tip", "拖动调整窗体大小"))
        self._start = None
        self._start_geo = None

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._start = e.globalPosition().toPoint()
            self._start_geo = self.window().geometry()

    def mouseMoveEvent(self, e):
        if self._start is None or not (e.buttons() & Qt.LeftButton):
            return
        w = self.window()
        delta = e.globalPosition().toPoint() - self._start
        nw = max(w.minimumWidth(), self._start_geo.width() + delta.x())
        nh = max(w.minimumHeight(), self._start_geo.height() + delta.y())
        # clamp 屏幕（右下不超过**窗口所在屏**的可用区；用主屏几何会在副屏上
        # 算出负宽度并阻止向右/下放大）
        scr = screen_available_geometry(w)
        if not scr.isNull():
            nw = min(nw, max(1, scr.right() - self._start_geo.x() + 1))
            nh = min(nh, max(1, scr.bottom() - self._start_geo.y() + 1))
        w.resize(nw, nh)

    def mouseReleaseEvent(self, _e):
        self._start = None


class TablePreviewPanel(CardWindow):
    """表格预览浮层（CardWindow 基类，跟随明暗主题）。"""

    closed = Signal()

    def __init__(self, table: TableResult, config=None,
                 export_service: TableExportService | None = None):
        super().__init__(title="表格识别结果")
        self._table = table
        self._svc = export_service or TableExportService()

        # 表格
        self.table = QTableWidget(table.row_count, table.col_count)
        self.table.horizontalHeader().setVisible(True)
        self.table.verticalHeader().setVisible(True)
        for cell in table.cells:
            item = QTableWidgetItem(cell.text)
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(cell.row, cell.col, item)
        self.table.resizeColumnsToContents()
        self.table.setMinimumSize(420, 220)
        self.card_lay.addWidget(self.table)

        # 提示
        self.hint = QLabel(
            T("table.shape_hint", "{row} 行 × {col} 列 · 双击单元格可编辑").format(
                row=table.row_count, col=table.col_count))
        self.hint.setProperty("kind", "info")
        self.card_lay.addWidget(self.hint)

        # 按钮行
        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.btn_xlsx = QPushButton(T("table.export_xlsx", "导出 Excel"))
        self.btn_md = QPushButton(T("table.export_md", "导出 Markdown"))
        self.btn_tsv = QPushButton(T("table.copy_tsv", "复制 TSV"))
        self.btn_close = QPushButton(T("btn.close", "关闭"))
        for b in (self.btn_xlsx, self.btn_md, self.btn_tsv, self.btn_close):
            b.setMinimumHeight(36)
            btns.addWidget(b)
        btns.addStretch(1)
        self.card_lay.addLayout(btns)

        self.btn_xlsx.clicked.connect(self._export_xlsx)
        self.btn_md.clicked.connect(self._export_md)
        self.btn_tsv.clicked.connect(self._copy_tsv)
        self.btn_close.clicked.connect(self.close)

        # 默认窗体 = 内容所需尺寸的 2 倍（clamp 到鼠标所在屏内）
        self.adjustSize()
        scr = screen_available_geometry(self)
        if scr.isNull():
            from PySide6.QtGui import QGuiApplication
            scr = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(min(int(self.width() * 2), scr.width() - 20),
                    min(int(self.height() * 2), scr.height() - 20))
        # 缩放下限 = 内容所需（表格/按钮完整可见）
        self.setMinimumSize(self.sizeHint())
        # 右下角缩放手柄（resizeEvent 跟随定位）
        self._grip = _ResizeGrip(self)
        self._grip.raise_()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if getattr(self, "_grip", None) is not None:
            self._grip.move(self.width() - self._grip.width(),
                            self.height() - self._grip.height())
            self._grip.raise_()

    # ---- 操作 ----
    def _grid_rows(self) -> list[list[str]]:
        """从当前表格控件读取编辑后的值（覆盖识别结果）。"""
        rows, cols = self.table.rowCount(), self.table.columnCount()
        grid = []
        for r in range(rows):
            grid.append([
                self.table.item(r, c).text() if self.table.item(r, c) else ""
                for c in range(cols)
            ])
        return grid

    def _export_xlsx(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 Excel", str(Path.home() / "table.xlsx"), "Excel (*.xlsx)")
        if not path:
            return
        try:
            edited = self._table_with_edits()
            self._svc.export_excel(edited, path)
            ThemeDialog.info(None, "导出成功", f"已导出到：{path}")
        except Exception as e:
            ThemeDialog.warning(None, "导出失败", str(e))

    def _export_md(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 Markdown", str(Path.home() / "table.md"), "Markdown (*.md)")
        if not path:
            return
        try:
            edited = self._table_with_edits()
            self._svc.export_markdown(edited, path)
            ThemeDialog.info(None, "导出成功", f"已导出到：{path}")
        except Exception as e:
            ThemeDialog.warning(None, "导出失败", str(e))

    def _copy_tsv(self):
        edited = self._table_with_edits()
        tsv = self._svc.to_tsv(edited)
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(tsv)   # 直接复制，不弹窗

    def _table_with_edits(self) -> TableResult:
        """把控件编辑值回填到 TableResult（只改 text，保留行列结构）。

        返回**新对象**且不污染 self._table：原先 copy.copy 为浅拷贝，
        随后的 cell.text = ... 原地改的是 self._table 里同一批 TableCell，
        导致「导出/复制一次后，预览面板显示的文字被静默覆盖」。
        原 cells 为空但行列数 > 0 时按编辑网格重建 cells（否则编辑内容全丢）。
        """
        grid = self._grid_rows()
        import copy
        edited = copy.copy(self._table)
        if self._table.cells:
            new_cells = []
            for cell in self._table.cells:
                c = copy.copy(cell)
                if 0 <= c.row < len(grid) and 0 <= c.col < len(grid[c.row]):
                    c.text = grid[c.row][c.col]
                new_cells.append(c)
            edited.cells = new_cells
        else:
            edited.cells = [
                TableCell(text=val, row=r, col=c)
                for r, row in enumerate(grid) for c, val in enumerate(row)
            ]
        return edited

    def closeEvent(self, e):
        self.closed.emit()
        super().closeEvent(e)
