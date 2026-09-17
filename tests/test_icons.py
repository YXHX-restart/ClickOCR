"""图标缓存离线单测：命中复用、**上限与 LRU 淘汰**、清空。

为什么要盯上限：缓存键是 (name, color, size)，其中 color 是自由参数 ——
条目数没有天然边界，而每个条目含 1x/2x 两张 QPixmap，无上限时会随使用缓慢累积。
"""
import unittest

from PySide6.QtGui import QGuiApplication

from ui.base import icon as icon_mod
from ui.base.icon import cache_size, clear_cache, icon

_app = QGuiApplication.instance() or QGuiApplication([])

#: 注册表里必然存在的图标（渲染成功才会带 pixmap）
_NAME = "pin-dot"


class TestIconCache(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def test_same_key_reuses_entry(self):
        icon(_NAME, color="#ff0000", size=18)
        n = cache_size()
        again = icon(_NAME, color="#ff0000", size=18)
        self.assertEqual(cache_size(), n, "同参数不该新增缓存条目")
        self.assertIs(again, icon(_NAME, color="#ff0000", size=18))

    def test_key_includes_color_and_size(self):
        icon(_NAME, color="#ff0000", size=18)
        icon(_NAME, color="#00ff00", size=18)
        icon(_NAME, color="#ff0000", size=24)
        icon(_NAME, size=18)
        self.assertEqual(cache_size(), 4)

    def test_rendered_icon_is_not_empty(self):
        ic = icon(_NAME, color="#ff0000", size=18)
        self.assertFalse(ic.isNull(), f"图标 {_NAME!r} 渲染失败（资源缺失？）")

    def test_missing_icon_cached_as_empty_and_not_raising(self):
        ic = icon("__no_such_icon__")
        self.assertTrue(ic.isNull(), "缺失图标应返回空 QIcon 而不是抛异常")
        icon("__no_such_icon__")
        self.assertEqual(cache_size(), 1, "空图标同样只缓存一条")

    def test_cache_is_bounded(self):
        for i in range(icon_mod._CACHE_MAX + 40):
            icon(_NAME, color=f"#{i:06x}", size=18)
        self.assertLessEqual(cache_size(), icon_mod._CACHE_MAX,
                             "缓存超过上限了（无界累积）")

    def test_evicts_least_recently_used(self):
        """淘汰的是「最久未用」那条，而不是最近用过的。

        判据用**对象身份**：被淘汰的条目下次会重新构建（拿到不同对象），
        保留的条目仍命中缓存（拿到同一对象）。
        """
        keep, first = "#111111", "#222222"
        ref_first = icon(_NAME, color=first, size=18)     # 先取，最旧
        ref_keep = icon(_NAME, color=keep, size=18)       # 后取，最近使用
        for i in range(icon_mod._CACHE_MAX - 1):          # 灌到总共溢出 1 条
            icon(_NAME, color=f"#a{i:05x}", size=18)
        self.assertLessEqual(cache_size(), icon_mod._CACHE_MAX)
        # 先查「该保留的」：检查被淘汰项会触发重建并写入缓存，反而挤掉这一条
        self.assertIs(icon(_NAME, color=keep, size=18), ref_keep,
                      "最近取用过的条目不该被淘汰")
        self.assertIsNot(icon(_NAME, color=first, size=18), ref_first,
                         "最久未用的条目应被淘汰（否则就是 FIFO/随机）")

    def test_clear_cache(self):
        icon(_NAME, color="#ff0000", size=18)
        self.assertGreater(cache_size(), 0)
        clear_cache()
        self.assertEqual(cache_size(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
