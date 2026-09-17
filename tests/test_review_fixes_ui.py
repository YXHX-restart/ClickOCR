"""2026-09-18 审查修复的回归测试（可离屏/无 GUI 验证的部分）。

覆盖两处「静默失效」型缺陷 —— 它们不抛异常、界面也不报错，只能靠断言钉住：

1. 空标注会话退出不能触发 `annotations_changed`（否则误触进入标注模式再按
   Esc 会凭空多出一条空标注历史记录）；
2. `ColorPickerDialog` 的「取消」必须真的回滚颜色（选色是即时生效的），
   且取消要同时回滚自身状态，否则下次打开显示取消前的旧色。

注意：这里**不构造顶层窗口控件**。本仓既有约定（见 test_history_thumbs 的
说明）：无事件循环的测试进程里建/毁顶层浮窗会踩 Qt 析构竞态，实测直接
0xC0000409 崩掉整个 discover（本轮实测：加了「构造 ColorPickerDialog」的
用例后，全量 discover 在最后几行无汇总直接终止）。故第 2 项退化为
**接线断言**（源码级），真交互由 scripts/ 下的真桌面冒烟覆盖。
"""
import unittest
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ui.windows.overlay.anno_session import AnnoMixin

_ROOT = Path(__file__).resolve().parent.parent


class _FakeAnno:
    """只提供 `_exit_annotation` 需要的 data / json()。"""

    def __init__(self, data):
        self.data = data

    def json(self):
        return '[{"type": "rect"}]'


class _AnnoHost(AnnoMixin, QObject):
    """承载 AnnoMixin 的最小宿主：一个真信号 + 其余空实现。"""

    annotations_changed = Signal(str)

    def _set_status(self, *_a, **_k):
        pass


class TestEmptyAnnotationExit(unittest.TestCase):
    def _host(self, data):
        h = _AnnoHost()
        h._anno_mode = True
        h._anno = _FakeAnno(data)
        h._fgroup = None
        h._toolbar = None
        h._events = []
        h.annotations_changed.connect(h._events.append)
        return h

    def test_empty_session_emits_nothing(self):
        h = self._host([])
        h._exit_annotation()
        self.assertEqual(h._events, [],
                         "空标注会话退出不应发信号（否则会凭空落一条空记录）")
        self.assertFalse(h._anno_mode)

    def test_non_empty_session_emits(self):
        h = self._host([{"type": "rect"}])
        h._exit_annotation()
        self.assertEqual(len(h._events), 1)

    def test_idempotent_after_exit(self):
        h = self._host([{"type": "rect"}])
        h._exit_annotation()
        h._exit_annotation()
        self.assertEqual(len(h._events), 1, "已退出标注模式后重复调用不应再发")


class TestColorPickerCancelWiring(unittest.TestCase):
    """取色器「取消」的接线断言（真构造见文件头说明）。"""

    def setUp(self):
        self.src = (_ROOT / "ui" / "base" / "widgets.py").read_text(
            encoding="utf-8")

    def test_cancel_button_wired_to_cancel(self):
        self.assertIn("b_cancel.clicked.connect(self._cancel)", self.src,
                      "取消按钮不能直接连 close（那样只关窗不回滚颜色）")

    def test_cancel_rolls_back_both_signal_and_state(self):
        body = self.src.split("def _cancel(")[1].split("\n    def ")[0]
        self.assertIn("self.picked.emit(", body)
        self.assertIn("self._color = tuple(self._orig)", body,
                      "只发信号不回滚自身状态：下次打开会显示取消前的旧色")
        self.assertIn("self._refresh()", body)
        self.assertIn("self.close()", body)

    def test_cancel_only_emits_when_changed(self):
        body = self.src.split("def _cancel(")[1].split("\n    def ")[0]
        self.assertIn("if tuple(self._color) != tuple(self._orig):", body)

    def test_picker_positioned_before_show(self):
        """先置 _centered / adjustSize 再 show：否则弹出时先闪现在别处再跳。"""
        body = self.src.split("def _pick(")[1].split("\n    def ")[0]
        self.assertLess(body.index("_centered = True"), body.index(".show()"),
                        "必须先置 _centered 再 show，否则 CardWindow 会居中覆盖")
        self.assertLess(body.index("adjustSize()"), body.index(".show()"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
