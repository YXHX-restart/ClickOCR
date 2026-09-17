"""热键键表离线单测（无 Qt 窗口，只测 token 映射与组合串拆分）。

覆盖的既有缺陷：反向映射里 `` ` `` 被别名 `~` 覆盖 → 默认历史热键 ``ctrl+` ``
的 token 匹配失效、热键静默失效；`+` 键无法表达；未知 token 被静默丢弃。
"""
import unittest

import hotkey
from ui.base.hotkey_keys import (MODIFIER_TOKENS, parse_qt_combo, qt_to_token,
                                 split_combo, token_display, token_to_vk)


class TestTokenMaps(unittest.TestCase):
    def test_backtick_aliases(self):
        """`` ` `` 与 `~` 都指向 VK_OEM_3(0xC0)。"""
        self.assertEqual(token_to_vk("`"), 0xC0)
        self.assertEqual(token_to_vk("~"), 0xC0)

    def test_reverse_map_prefers_backtick(self):
        """反向映射必须得到 `` ` ``（不是别名 `~`）—— 原「热键静默失效」根因。"""
        from PySide6.QtCore import Qt
        self.assertEqual(qt_to_token(Qt.Key_QuoteLeft), "`")

    def test_letters_digits_and_fkeys(self):
        self.assertEqual(token_to_vk("a"), 0x41)
        self.assertEqual(token_to_vk("z"), 0x5A)
        self.assertEqual(token_to_vk("5"), 0x35)
        self.assertEqual(token_to_vk("f1"), 0x70)
        self.assertEqual(token_to_vk("f12"), 0x7B)
        self.assertIsNone(token_to_vk("nosuchkey"))

    def test_plus_is_a_named_token(self):
        """`+` 是组合分隔符，不能自身作 token —— 用别名 plus 表达。"""
        self.assertEqual(split_combo("ctrl+plus")[1], "plus")
        self.assertEqual(token_to_vk("plus"), 0xBB)

    def test_display_names(self):
        self.assertEqual(token_display("space"), "空格")
        self.assertEqual(token_display("a"), "A")
        self.assertEqual(token_display("f5"), "F5")


class TestSplitCombo(unittest.TestCase):
    def test_off_tokens(self):
        for off in ("off", "none", "false", ""):
            self.assertEqual(split_combo(off)[1], None, f"{off!r} 应表示未启用")

    def test_unknown_token_yields_no_key(self):
        """未知键名不得静默成键：返回 None，由上层告警（原来是直接吞掉）。"""
        mods, key = split_combo("ctrl+nosuchkey")
        self.assertIn("ctrl", mods)
        self.assertIsNone(key)

    def test_modifier_order_is_preserved_and_deduped(self):
        mods, key = split_combo("shift+ctrl+shift+r")
        self.assertEqual(mods, ["shift", "ctrl"])
        self.assertEqual(key, "r")

    def test_all_modifier_tokens_recognised(self):
        mods, _ = split_combo("+".join(MODIFIER_TOKENS) + "+a")
        self.assertEqual(mods, list(MODIFIER_TOKENS))


class TestParseQtCombo(unittest.TestCase):
    def test_parse_returns_mods_and_key(self):
        from PySide6.QtCore import Qt
        mods, key = parse_qt_combo("ctrl+shift+r")
        self.assertEqual(mods, Qt.ControlModifier | Qt.ShiftModifier)
        self.assertEqual(key, Qt.Key_R)

    def test_off_returns_none(self):
        self.assertIsNone(parse_qt_combo("off"))
        self.assertIsNone(parse_qt_combo(""))

    def test_unknown_key_returns_none(self):
        self.assertIsNone(parse_qt_combo("ctrl+nosuchkey"))


class TestGlobalHotkeyParse(unittest.TestCase):
    """全局热键（Win32）侧的组合解析：解析出有效 VK 才算可用。"""

    def test_known_combos_resolve_to_vk(self):
        for combo in ("ctrl+shift+r", "ctrl+alt+s", "ctrl+`", "ctrl+shift+f5",
                      "alt+f", "ctrl+plus"):
            mods, vk = hotkey._parse_combo(combo)
            self.assertIsNotNone(vk, f"{combo!r} 应解析出 VK")
            self.assertGreater(vk, 0, f"{combo!r} → mods={mods}")

    def test_unknown_token_has_no_vk(self):
        _mods, vk = hotkey._parse_combo("ctrl+nosuchkey")
        self.assertIsNone(vk)


if __name__ == "__main__":
    unittest.main(verbosity=2)
