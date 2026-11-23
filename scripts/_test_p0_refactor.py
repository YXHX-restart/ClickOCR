"""P0 重构回归：①点击穿透 toggle_through ②命令模式撤销/重做 ③图元工厂化。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPointF
import numpy as np

from ui.windows.result_overlay import ResultOverlay
from ui.components.annotation import (AnnotationSession, _ItemFactory, _AddCmd, UNDO_LIMIT)

app = QApplication([])
img = np.zeros((400, 500, 3), dtype=np.uint8)


def new_session():
    ov = ResultOverlay(img, (0, 0, 500, 400), 1.0)
    return AnnotationSession(ov, ov.scene, ov._pix)


def draw_rect(s, a=(10, 10), b=(60, 60)):
    s.set_tool("rect")
    s.mouse_press(QPointF(*a)); s.mouse_move(QPointF(*b))
    s.mouse_release(QPointF(*b))


print("== 1) 点击穿透 toggle_through ==")
ov = ResultOverlay(img, (0, 0, 500, 400), 1.0)
assert ov._through is False
assert ov.toggle_through() is True and ov._through is True
assert ov.toggle_through() is False and ov._through is False
print("   OK  穿透状态切换（True↔False），非 Windows 下安全")

print("== 2) 命令模式撤销/重做 ==")
s = new_session()
for i in range(3):
    draw_rect(s, (10 + i * 90, 10), (60 + i * 90, 60))
assert len(s.data) == 3 and len(s._undo) == 3, "画 3 框应 3 条命令"
assert all(type(c).__name__ == "_AddCmd" for c in s._undo)
s.undo(); s.undo(); s.undo()
assert len(s.data) == 0, "连撤 3 次应清空"
s.redo(); s.redo(); s.redo()
assert len(s.data) == 3, "重做 3 次应恢复"
# 移动
s.set_tool("select"); s.mouse_press(QPointF(30, 30))
s.mouse_move(QPointF(90, 90)); s.mouse_release(QPointF(90, 90))
assert s.data[0]["x0"] == 70
s.undo(); assert s.data[0]["x0"] == 10
s.redo(); assert s.data[0]["x0"] == 70
# 删除（矩形0 已移动到 70,70~120,120，点其内部）
s.mouse_press(QPointF(90, 90)); assert s.delete_selected()
assert len(s.data) == 2
s.undo(); assert len(s.data) == 3
s.redo(); assert len(s.data) == 2
# 文字编辑
s2 = new_session()
s2.set_tool("text"); s2.mouse_press(QPointF(20, 20))
s2.mouse_move(QPointF(160, 100)); s2.mouse_release(QPointF(160, 100))
s2._text_edit.setPlainText("old"); s2._commit_text()
s2.set_tool("select"); s2.mouse_double_click(QPointF(40, 40))
s2._text_edit.setPlainText("new"); s2._commit_text()
assert s2.data[0]["text"] == "new"
s2.undo(); assert s2.data[0]["text"] == "old"
s2.redo(); assert s2.data[0]["text"] == "new"
# 清空
s2.clear(); assert len(s2.data) == 0
s2.undo(); assert len(s2.data) == 1
# 上限裁剪
s3 = new_session()
for i in range(600):
    s3._push_cmd(_AddCmd(i, {"type": "rect", "x0": 0, "y0": 0,
                             "x1": 1, "y1": 1, "color": "#fff", "width": 2}))
assert len(s3._undo) == UNDO_LIMIT == 500
# load 重建（恢复历史后分步撤销）
s4 = new_session()
s4.load([{"type": "rect", "x0": 0, "y0": 0, "x1": 10, "y1": 10,
          "color": "#fff", "width": 2}])
s4.load([{"type": "rect", "x0": 0, "y0": 0, "x1": 10, "y1": 10,
          "color": "#fff", "width": 2},
         {"type": "text", "x": 0, "y": 0, "text": "hi",
          "color": "#fff"}])
assert len(s4._undo) == 2, "load 应重建 2 条 AddCmd"
s4.undo(); assert len(s4.data) == 1
s4.undo(); assert len(s4.data) == 0
print("   OK  Add/Move/Delete/Edit/Clear 命令 + 上限 + load 重建全通过")

print("== 3) 图元工厂化 ==")
# 图元已从 6 类扩到 8 类（+line 直线、+num 序号标注）
assert sorted(_ItemFactory._registry) == ["arrow", "line", "mosaic",
                                          "mosaic_pen", "num", "pen",
                                          "rect", "text"]
assert _ItemFactory.create("unknown", {}, None) is None, "未知类型应安全 None"
s5 = new_session()
# 6 类图元渲染（工厂产出）
s5.load([
    {"type": "rect", "x0": 0, "y0": 0, "x1": 20, "y1": 20,
     "color": "#fff", "width": 2},
    {"type": "arrow", "x0": 0, "y0": 0, "x1": 20, "y1": 20,
     "color": "#fff", "width": 2},
    {"type": "pen", "points": [[0, 0], [10, 10], [20, 5]],
     "color": "#fff", "width": 2},
    {"type": "mosaic", "x0": 0, "y0": 0, "x1": 20, "y1": 20,
     "color": "#fff", "width": 2, "block": 8},
    {"type": "mosaic_pen", "points": [[0, 0], [10, 10]],
     "color": "#fff", "width": 2, "block": 8},
    {"type": "text", "x": 0, "y": 0, "text": "hi", "font_size": 18,
     "font": "Microsoft YaHei", "color": "#fff"},
    {"type": "text", "x": 50, "y": 50, "text": "box", "font_size": 18,
     "font": "Microsoft YaHei", "color": "#fff", "text_width": 120},
])
assert len(s5._items) == 8, f"6 类渲染应 8 个顶层 item（箭头2+文本框group），实际 {len(s5._items)}"
texts = []
for it in s5._items:
    if hasattr(it, "toPlainText"):
        texts.append(it.toPlainText())
    for ch in getattr(it, "childItems", lambda: [])():
        if hasattr(ch, "toPlainText"):
            texts.append(ch.toPlainText())
assert "box" in texts and "hi" in texts, f"文字渲染缺失：{texts}"
print("   OK  工厂注册表完整 + 6 类图元渲染 + 未知类型安全")

print("\nP0_REFACTOR_ALL_PASS")
