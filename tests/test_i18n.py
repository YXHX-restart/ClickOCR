"""i18n 离线单测：T() 的回退/留痕行为 + 文字表自身完整性。

「文案是否都提取了」「代码默认值与 JSON 是否漂移」由 scripts/_i18n_audit.py 负责
（见 tests/test_guards.py 里的 --check 调用），这里只测运行期取词契约与表的结构。
"""
import json
import logging
import unittest
from pathlib import Path

import ui.base.texts as texts

_TABLE = (Path(__file__).resolve().parent.parent
          / "resources" / "ui_text.json")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs: list[str] = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


class TestTLookup(unittest.TestCase):
    def setUp(self):
        texts.load_ui_text()
        self.cap = _Capture()
        texts.log.addHandler(self.cap)

    def tearDown(self):
        texts.log.removeHandler(self.cap)
        texts.load_ui_text()               # 还原真实文字表，别污染其它用例

    def test_known_key_uses_table_value(self):
        table = json.loads(_TABLE.read_text(encoding="utf-8"))
        key, value = next(iter(table.items()))
        self.assertEqual(texts.T(key, "不该用到"), value)

    def test_missing_key_falls_back_to_default(self):
        self.assertEqual(texts.T("__nope__.x", "兜底文案"), "兜底文案")

    def test_missing_key_without_default_returns_key(self):
        self.assertEqual(texts.T("__nope__.y"), "__nope__.y")

    def test_missing_key_warns_once(self):
        texts.T("__nope__.z", "a")
        texts.T("__nope__.z", "a")
        hits = [m for m in self.cap.msgs if "__nope__.z" in m]
        self.assertEqual(len(hits), 1, "同一 key 只应告警一次（渲染循环不刷屏）")

    def test_table_load_failure_warns_once_not_per_key(self):
        """文字表整体加载失败 → 只报「加载失败」一条，不逐 key 误报。"""
        n0 = len(self.cap.msgs)
        texts.load_ui_text("__no_such_ui_text__.json")
        texts.T("whatever.key", "兜底")
        added = self.cap.msgs[n0:]
        self.assertEqual(len(added), 1, f"应只报一条，实际 {added}")
        self.assertIn("加载失败", added[0])

    def test_recovers_after_reload(self):
        texts.load_ui_text("__no_such_ui_text__.json")
        texts.load_ui_text()
        self.assertEqual(texts.T("btn.close", "关闭"), "关闭")


class TestTableIntegrity(unittest.TestCase):
    """文字表自身的结构问题（重复键会让条目静默丢失）。"""

    def setUp(self):
        self.raw = _TABLE.read_text(encoding="utf-8")
        self.dups: list[str] = []

        def _hook(pairs):
            seen = set()
            for k, _v in pairs:
                if k in seen:
                    self.dups.append(k)
                seen.add(k)
            return dict(pairs)

        self.table = json.loads(self.raw, object_pairs_hook=_hook)

    def test_valid_json(self):
        self.assertTrue(self.table, "文字表不该为空")

    def test_no_duplicate_keys(self):
        self.assertFalse(self.dups, f"重复键（后者覆盖前者）：{self.dups}")

    def test_no_empty_values(self):
        empty = [k for k, v in self.table.items() if not str(v).strip()]
        self.assertFalse(empty, f"空文案会让界面出现空白：{empty}")

    def test_keys_have_no_stray_whitespace(self):
        """键名不得带空白（`T(" a.b")` 之类的错拼拿不到文案，且很难发现）。

        不强制「必须含点」：`autostart` 这类单段键是既有约定。
        """
        bad = [k for k in self.table if k != k.strip() or " " in k or "\t" in k]
        self.assertFalse(bad, f"键名带空白：{bad}")

    def test_no_literal_backslash_n(self):
        """值里若含**字面的** \\n（两个字符），界面上会原样显示出来。"""
        bad = [k for k, v in self.table.items() if "\\n" in str(v)]
        self.assertFalse(bad, f"应写成真换行而不是字面 \\\\n：{bad}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
