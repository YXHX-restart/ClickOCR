"""core_engine.table_export — 表格导出服务（TableExportService，无 Qt 依赖）。

对应需求 §7.5 接口契约：
- export_excel：openpyxl 生成 .xlsx（保留合并单元格 + 边框样式）
- export_markdown：生成 .md
- to_tsv：Tab 分隔（可直接粘贴 Excel/WPS）
- to_html：HTML 表格字符串（预览面板渲染）

依赖：openpyxl（缺失时 export_excel 抛 RuntimeError，其余格式零依赖）。
"""
from __future__ import annotations

import html as _html
import re as _re
from typing import List, Optional

from .ai_pipeline import TableResult

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, Side
    # 列序号 → 列字母（1-based）一律走 openpyxl 自带实现，见 export_excel 内的说明
    from openpyxl.utils import get_column_letter
    _OPENPYXL = True
except Exception:  # pragma: no cover - 依赖缺失
    _OPENPYXL = False

# 内联事件属性（onerror=/onload=/onclick=…）：模型预测的 HTML 与 OCR 单元格
# 文本都是不可信输入，预览渲染前需过滤，防 <img onerror=…> 类 payload 执行
_RE_ON_ATTR = _re.compile(
    r"\son\w+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", _re.IGNORECASE)


def _sanitize_html(h: str) -> str:
    """过滤 HTML 字符串中的内联事件属性（其余原样保留）。"""
    return _RE_ON_ATTR.sub("", h)


class TableExportService:
    """表格结果导出（纯函数风格，可作单例使用）。"""

    # ---- 网格化：把 TableCell（含 row_span/col_span）摊平成二维文本网格 ----
    @staticmethod
    def _grid(table: TableResult) -> List[List[str]]:
        if not table.cells:
            return [[f"" for _ in range(max(1, table.col_count))]
                    for _ in range(max(1, table.row_count))]
        rows, cols = max(table.row_count, 1), max(table.col_count, 1)
        grid = [["" for _ in range(cols)] for _ in range(rows)]
        occupied = [[False] * cols for _ in range(rows)]
        # 按 (row, col) 排序，保证填充顺序稳定
        for cell in sorted(table.cells, key=lambda c: (c.row, c.col)):
            r, c = cell.row, cell.col
            # 若该格已被跨行跨列占用，找下一个空位（防御性）
            while r < rows and occupied[r][c]:
                c += 1
                if c >= cols:
                    c, r = 0, r + 1
            if r >= rows:
                break
            grid[r][c] = cell.text
            for rr in range(r, min(r + max(cell.row_span, 1), rows)):
                for cc in range(c, min(c + max(cell.col_span, 1), cols)):
                    occupied[rr][cc] = True
        return grid

    # ---- Excel ----
    def export_excel(self, table: TableResult, file_path: str) -> None:
        """导出 .xlsx，保留合并单元格 + 边框/居中样式。"""
        if not _OPENPYXL:
            raise RuntimeError("未安装 openpyxl，无法导出 Excel（pip install openpyxl）")
        grid = self._grid(table)
        wb = Workbook()
        ws = wb.active
        ws.title = "表格"

        thin = Side(style="thin", color="999999")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        center = Alignment(horizontal="center", vertical="center", wrap_text=True)

        # 预计算合并区间：找跨行跨列 cell 的 (r,c) 起点 → 合并
        merges = self._merge_ranges(table, len(grid), len(grid[0]) if grid else 0)

        for r, row in enumerate(grid, start=1):
            for c, val in enumerate(row, start=1):
                cell = ws.cell(row=r, column=c, value=val or None)
                # 公式注入防护：OCR 单元格文本是不可信输入，openpyxl 对以 =
                # 开头的字符串按公式存储（=HYPERLINK/DDE 注入在打开时执行）；
                # 强制 data_type='s' 按纯文本写，原字符完整保留
                if val and str(val).startswith("="):
                    cell.data_type = "s"
                cell.border = border
                cell.alignment = center
                if r == 1:
                    cell.font = Font(bold=True)

        for r1, c1, r2, c2 in merges:
            if r2 > r1 or c2 > c1:
                ws.merge_cells(start_row=r1 + 1, start_column=c1 + 1,
                               end_row=r2 + 1, end_column=c2 + 1)

        # 列宽自适应（中文按 2 字符宽估算）
        # 列字母换算统一用 openpyxl.utils.get_column_letter（**1-based**，故 +1）：
        # 手写进位极易只覆盖一段——旧实现 chr(65+ci) / "A"+chr(65+ci-26) 只对
        # 0~51 有效，ci≥52 会生成 "A[" 这类非法列名，>52 列的表格导出时设置列宽
        # 直接抛 KeyError/ValueError。库自带实现覆盖任意列数，无需自己维护进位。
        if grid:
            for ci in range(len(grid[0])):
                width = max((len(grid[ri][ci]) * 2 for ri in range(len(grid))), default=6)
                ws.column_dimensions[get_column_letter(ci + 1)].width = min(max(width + 2, 8), 40)

        wb.save(file_path)

    @staticmethod
    def _merge_ranges(table: TableResult, rows: int, cols: int) -> List[tuple]:
        """返回 [(r1, c1, r2, c2)] 合并区间（0-based，含跨行跨列）。"""
        merges: List[tuple] = []
        for cell in table.cells:
            r2 = cell.row + max(cell.row_span, 1) - 1
            c2 = cell.col + max(cell.col_span, 1) - 1
            if r2 < rows and c2 < cols and (r2 > cell.row or c2 > cell.col):
                merges.append((cell.row, cell.col, r2, c2))
        return merges

    # ---- Markdown ----
    def export_markdown(self, table: TableResult, file_path: str) -> None:
        md = table.markdown or self.to_markdown(table)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(md)

    @staticmethod
    def to_markdown(table: TableResult) -> str:
        if table.markdown:
            return table.markdown
        grid = TableExportService._grid(table)
        if not grid:
            return ""
        lines = []
        for ri, row in enumerate(grid):
            lines.append("| " + " | ".join(v.replace("|", "\\|") for v in row) + " |")
            if ri == 0:
                lines.append("|" + "---|" * len(row))
        return "\n".join(lines)

    # ---- TSV ----
    @staticmethod
    def to_tsv(table: TableResult) -> str:
        grid = TableExportService._grid(table)
        if not grid:
            return ""
        return "\n".join("\t".join(v.replace("\t", " ").replace("\n", " ") for v in row)
                         for row in grid)

    # ---- HTML ----
    @staticmethod
    def to_html(table: TableResult) -> str:
        if table.html:
            # 模型原始 HTML 含 OCR 文本（不可信输入）：过滤内联事件属性，
            # 防 <img onerror=…> 类 payload 在预览渲染器中执行
            return _sanitize_html(table.html)
        grid = TableExportService._grid(table)
        if not grid:
            return "<table></table>"
        parts = ["<table border='1' cellspacing='0' cellpadding='4'>"]
        for ri, row in enumerate(grid):
            tag = "th" if ri == 0 else "td"
            # 单元格文本统一 HTML 转义（含 <>& 等，防注入/防破坏表格结构）
            parts.append("<tr>" + "".join(
                f"<{tag}>{_html.escape(v)}</{tag}>" for v in row) + "</tr>")
        parts.append("</table>")
        return "".join(parts)

    # ---- 兼容 Protocol 的便捷单例 ----
    @staticmethod
    def instance() -> "TableExportService":
        return TableExportService()


# 兼容需求契约命名
export_excel = TableExportService().export_excel
export_markdown = TableExportService().export_markdown
to_tsv = TableExportService().to_tsv
to_html = TableExportService().to_html
