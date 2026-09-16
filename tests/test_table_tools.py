"""表格工具离线单测：导出安全（公式注入 / HTML 转义）+ 检测算法的边界兜底。"""
import tempfile
import unittest
from pathlib import Path
from statistics import StatisticsError

from core_engine.ai_pipeline import TableCell, TableResult
from core_engine.table_export import TableExportService


def _table(cells) -> TableResult:
    return TableResult(cells=cells,
                       row_count=max(c.row for c in cells) + 1,
                       col_count=max(c.col for c in cells) + 1)


class TestExportSafety(unittest.TestCase):
    """导出内容会被 Excel / 浏览器执行，必须当成不可信输入处理。"""

    def setUp(self):
        self.svc = TableExportService()

    def test_formula_cell_stored_as_text(self):
        """以 = 开头的单元格必须按文本存储（否则是 CSV/Excel 公式注入）。"""
        t = _table([TableCell(text='=HYPERLINK("http://evil","x")',
                              row=0, col=0)])
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "t.xlsx")
            try:
                self.svc.export_excel(t, path)
            except RuntimeError:            # openpyxl 缺失
                self.skipTest("openpyxl 未安装")
            from openpyxl import load_workbook
            c = load_workbook(path).active.cell(row=1, column=1)
            self.assertEqual(c.data_type, "s", "公式型单元格 = 注入风险")
            self.assertTrue(str(c.value).startswith("="), "原文本应保留")

    def test_html_is_escaped(self):
        t = _table([TableCell(text="<img onerror=alert(1)>", row=0, col=0)])
        html = self.svc.to_html(t)
        self.assertIn("&lt;img onerror=", html)
        self.assertNotIn("<img onerror=", html)

    def test_tsv_and_markdown_keep_text(self):
        t = _table([TableCell(text="普通文字", row=0, col=0),
                    TableCell(text="第二格", row=0, col=1)])
        self.assertIn("普通文字\t第二格", self.svc.to_tsv(t))
        self.assertIn("普通文字", self.svc.to_markdown(t))

    def test_column_letter_beyond_z(self):
        """列字母 ≥52 也必须合法（原自造算法在 ci≥52 时产出 "A[" 这种非法列名）。"""
        cells = [TableCell(text=f"c{i}", row=0, col=i) for i in range(55)]
        t = _table(cells)
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "wide.xlsx")
            try:
                self.svc.export_excel(t, path)
            except RuntimeError:
                self.skipTest("openpyxl 未安装")
            from openpyxl import load_workbook
            ws = load_workbook(path).active
            self.assertEqual(ws.cell(row=1, column=55).value, "c54")


class TestTableDetectEdges(unittest.TestCase):
    """检测算法对退化输入必须**返回 None / 空**，而不是抛异常。"""

    def test_coords_grid_empty_blobs_returns_none(self):
        from ui.core.table_detect import _coords_table_grid
        self.assertIsNone(_coords_table_grid([]))

    def test_coords_grid_single_blob_returns_none(self):
        from ui.core.table_detect import _coords_table_grid
        self.assertIsNone(_coords_table_grid([(0, 0, 0, 10, 10, "x")]))

    def test_median_empty_is_the_underlying_trap(self):
        """记录 `median([])` 会抛——算法入口必须自己兜住，不能指望调用方。"""
        with self.assertRaises(StatisticsError):
            from statistics import median
            median([])


if __name__ == "__main__":
    unittest.main(verbosity=2)
