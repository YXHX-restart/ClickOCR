"""历史面板缩略图缓存的失效行为。

背景（§6.2）：缓存键 = (路径, mtime_ns, 宽, 高)，上限 400 且**只被动淘汰** ——
于是删除记录后，对应条目仍会白占内存很久（每条是缩放后的 QPixmap，几十 KB）。

这里钉两件事：
1. `_drop_thumbs` / `drop_thumb_cache` 的**行为**（按路径精确丢 / 整表清空）；
2. 三个删除入口**确实调用了它**（含「必须在 history.delete 之前」这个顺序要求）。

为什么用轻量壳而不是构造真面板：本组只需缓存结构 + 调用顺序，而构造真
`HistoryPanel`（顶层浮窗）在无事件循环的测试进程里反复建/毁会踩 Qt 析构竞态
（实测崩 0xC0000409）。真实「删除 + reload」路径由 scripts/_test_hist_tabs.py
的真桌面冒烟覆盖。
"""
import unittest
from pathlib import Path

from ui.windows.history_panel import HistoryPanel

_ROOT = Path(__file__).resolve().parent.parent


class _FakeHistory:
    """只提供 _drop_thumbs 需要的 get()。"""

    def __init__(self, rows):
        self.rows = rows
        self.deleted = []

    def get(self, rid):
        return self.rows.get(rid)

    def delete(self, rid):
        self.deleted.append(rid)
        self.rows.pop(rid, None)


def _shell(thumb_dir: Path, rows: dict) -> HistoryPanel:
    """`HistoryPanel.__new__` 造一个不初始化 Qt 的壳：只挂本组需要的属性。"""
    p = HistoryPanel.__new__(HistoryPanel)
    p._thumb_cache = {}
    p._thumb_dir = Path(thumb_dir)
    p._history = _FakeHistory(rows)
    return p


class TestDropThumbs(unittest.TestCase):
    def setUp(self):
        self.shots = Path("C:/shots")
        self.rows = {1: {"screenshot_path": "a.png"},
                     2: {"screenshot_path": "b.png"}}
        self.p = _shell(self.shots / "thumb", self.rows)

    def _key(self, name, size=120):
        # 键里的路径必须是**缩略图**路径：这是填充侧（_thumb_path → _cached_thumb）
        # 实际使用的路径。本测试原先用 `shots/<name>`（截图目录），与填充侧
        # `thumb/<name>` 永不相等 —— 于是 `_drop_thumbs` 看起来「丢掉了」，
        # 实际一条都没命中，缓存从不失效。生产代码已收敛到 _thumb_path()，
        # 这里跟着对齐，否则测试会继续为错误契约背书。
        return (str(self.shots / "thumb" / name), 12345, size, size)

    def test_drops_only_matching_paths(self):
        self.p._thumb_cache[self._key("a.png")] = "pm-a"
        self.p._thumb_cache[self._key("b.png")] = "pm-b"
        self.p._thumb_cache[self._key("a.png", size=240)] = "pm-a2"
        self.p._drop_thumbs([1])
        left = {k[0] for k in self.p._thumb_cache}
        self.assertEqual(left, {str(self.shots / "thumb" / "b.png")},
                         f"只该丢掉被删记录的条目，实际剩 {left}")

    def test_drops_all_sizes_of_same_path(self):
        """同一张图可能有多个尺寸的条目（不同 Tab/列表），必须一起丢。"""
        self.p._thumb_cache[self._key("a.png", 120)] = "x"
        self.p._thumb_cache[self._key("a.png", 240)] = "y"
        self.p._drop_thumbs([1])
        self.assertEqual(self.p._thumb_cache, {})

    def test_bulk_drops_each(self):
        self.p._thumb_cache[self._key("a.png")] = "x"
        self.p._thumb_cache[self._key("b.png")] = "y"
        self.p._drop_thumbs([1, 2])
        self.assertEqual(self.p._thumb_cache, {})

    def test_missing_record_is_ignored(self):
        """记录已不存在（并发删除）时不该抛，也不该误删别人的缓存。"""
        self.p._thumb_cache[self._key("b.png")] = "y"
        self.p._drop_thumbs([999])
        self.assertEqual(len(self.p._thumb_cache), 1)

    def test_empty_inputs_are_noop(self):
        self.p._drop_thumbs([])
        self.p._drop_thumbs(None)
        self.p._thumb_cache[self._key("a.png")] = "x"
        self.p._drop_thumbs([1])
        self.assertEqual(self.p._thumb_cache, {})

    def test_drop_thumb_cache_clears_all(self):
        self.p._thumb_cache[self._key("a.png")] = "x"
        self.p._thumb_cache[self._key("b.png")] = "y"
        self.p.drop_thumb_cache()
        self.assertEqual(self.p._thumb_cache, {})


class TestDeletePathsAreWired(unittest.TestCase):
    """接线断言：三个「记录消失」的入口都必须作废缓存，且顺序正确。"""

    def setUp(self):
        self.src = (_ROOT / "ui" / "windows" / "history_panel.py").read_text(
            encoding="utf-8")
        self.mixin = (_ROOT / "appmod" / "history_mixin.py").read_text(
            encoding="utf-8")

    def _body(self, name):
        return self.src.split(f"def {name}(")[1].split("\n    def ")[0]

    def test_single_delete_drops_before_db_delete(self):
        body = self._body("_delete")
        self.assertIn("_drop_thumbs", body)
        self.assertLess(body.index("_drop_thumbs"), body.index("_history.delete"),
                        "必须先丢缓存再删记录（删完就查不到 screenshot_path）")

    def test_bulk_delete_drops_before_db_delete(self):
        body = self._body("_bulk_delete")
        self.assertIn("_drop_thumbs", body)
        self.assertLess(body.index("_drop_thumbs"), body.index("_history.delete"))

    def test_clear_all_drops_whole_cache(self):
        body = self.mixin.split("def _clear_all_history(")[1].split("\n    def ")[0]
        self.assertIn("drop_thumb_cache", body,
                      "清空历史后应整表作废缩略图缓存")


if __name__ == "__main__":
    unittest.main(verbosity=2)
