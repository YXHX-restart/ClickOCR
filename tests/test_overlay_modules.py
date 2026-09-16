"""贴图窗口（ResultOverlay）的模块拆分归属断言。

**为什么单独一组**：拆成五个 mixin 后，方法名一旦在多个类里重复，MRO 会**静默遮蔽**
（不报错、只是行为变了）。这是本次拆分唯一的系统性风险，必须每条断言都盯着它。
"""
import unittest
from pathlib import Path

from ui.windows.overlay.anno_session import AnnoMixin
from ui.windows.overlay.pin_behavior import PinMixin
from ui.windows.overlay.table_view import TableViewMixin
from ui.windows.overlay.text_select import TextSelectMixin
from ui.windows.overlay.zoom_nav import ZoomNavMixin
from ui.windows.overlay import text_select as text_select_mod
from ui.windows.result_overlay import ResultOverlay

_ROOT = Path(__file__).resolve().parent.parent

EXPECT = {
    TableViewMixin: ["_toggle_table", "_handle_toolbar_key", "_on_table_mode",
                     "_apply_table_view", "_build_table_grid",
                     "_build_table_boxes", "_show_table_boxes",
                     "_hide_table_boxes", "_table_hit_cell", "_table_press",
                     "_table_move", "_table_release", "_draw_table_sel",
                     "_clear_table_sel", "_copy_table_rect"],
    PinMixin: ["_ensure_pin_dot", "set_pinned", "_on_pin_clicked",
               "_on_pin_double", "_on_pin_drag", "_on_pin_hover_in",
               "_on_pin_hover_out", "_show_pin_card", "_hide_pin_card",
               "_close_sub_windows"],
    ZoomNavMixin: ["_apply_transform", "_set_image_zoom", "_set_zoom",
                   "fit_to_screen", "_toggle_navigator", "_toggle_ratio_lock",
                   "_set_ratio_lock", "set_navigator", "_ensure_navigator",
                   "_layout_navigator", "_update_navigator", "_on_nav_jump",
                   "_edge_at", "_start_edge_resize", "_do_edge_resize",
                   "_end_edge_resize", "_sb_edge", "_recontain"],
    TextSelectMixin: ["_build_geom", "_char_intervals", "_caret", "_hit",
                      "_hit_or_clamp", "_norm", "get_selected_text",
                      "select_all", "_preview_selection", "_refresh_preview"],
    AnnoMixin: ["_toggle_annotation", "_on_anno_btn_toggled",
                "_enter_annotation", "_save_mosaic_mode", "_on_anno_color",
                "_on_anno_width", "_on_num_start", "_on_num_size",
                "_on_num_style", "_on_tool_style", "_switch_tool_by_key",
                "handle_anno_key", "_save_anno_param", "_exit_annotation",
                "_anno_undo", "_anno_redo", "_anno_clear", "load_annotations"],
}
MIXINS = list(EXPECT)


class TestOwnership(unittest.TestCase):
    def test_methods_defined_on_expected_mixin(self):
        for cls, names in EXPECT.items():
            for name in names:
                with self.subTest(mixin=cls.__name__, method=name):
                    self.assertIn(name, cls.__dict__,
                                  f"{name} 不在 {cls.__name__} 上")

    def test_no_mro_shadowing(self):
        """`ResultOverlay.<name>` 必须解析到预期 mixin（而不是被别的类盖住）。"""
        for cls, names in EXPECT.items():
            for name in names:
                owner = next(k for k in ResultOverlay.__mro__ if name in k.__dict__)
                with self.subTest(method=name):
                    self.assertIs(owner, cls,
                                  f"{name} 期望 {cls.__name__}，实际 {owner.__name__}")

    def test_mixins_share_no_method_names(self):
        """五个 mixin 之间不得重名 —— 这就是遮蔽的唯一来源。"""
        seen: dict = {}
        dupes = []
        for cls in MIXINS:
            for name in cls.__dict__:
                if name.startswith("__"):
                    continue
                if name in seen:
                    dupes.append(f"{name}: {seen[name].__name__} / {cls.__name__}")
                seen[name] = cls
        self.assertFalse(dupes, f"mixin 间方法重名：{dupes}")

    def test_moved_methods_not_left_behind(self):
        """搬走的方法不得在 result_overlay.py 里再留一份（改一处漏一处）。"""
        src = (_ROOT / "ui" / "windows" / "result_overlay.py").read_text(
            encoding="utf-8")
        left = [n for names in EXPECT.values() for n in names if f"def {n}(" in src]
        self.assertFalse(left, f"仍在 result_overlay.py 定义：{left}")

    def test_mixin_files_exist(self):
        for f in ("table_view.py", "pin_behavior.py", "zoom_nav.py",
                  "text_select.py", "anno_session.py", "__init__.py"):
            with self.subTest(file=f):
                self.assertTrue((_ROOT / "ui" / "windows" / "overlay" / f).exists())

    def test_mro_order_matches_design_doc(self):
        names = [c.__name__ for c in ResultOverlay.__mro__]
        self.assertEqual(names[:6],
                         ["ResultOverlay", "TableViewMixin", "PinMixin",
                          "ZoomNavMixin", "TextSelectMixin", "AnnoMixin"])


class TestMovedHelpers(unittest.TestCase):
    """随方法搬走的模块级助手与常量必须一起到位（漏了只在运行期 NameError）。"""

    def test_text_select_helpers_present(self):
        for name in ("_font_and_metrics", "_char_advance",
                     "_FONT_CACHE", "_FM_CACHE", "_CHAR_ADV_CACHE",
                     "_CHAR_ADV_CACHE_MAX"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(text_select_mod, name), f"缺 {name}")

    def test_helpers_are_not_reachable_from_old_module(self):
        """助手搬走后，result_overlay 里不应再有同名定义（避免两份缓存）。"""
        src = (_ROOT / "ui" / "windows" / "result_overlay.py").read_text(
            encoding="utf-8")
        self.assertNotIn("def _char_advance", src)
        self.assertNotIn("def _font_and_metrics", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
