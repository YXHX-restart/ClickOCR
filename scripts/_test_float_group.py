"""浮窗组管理器（FloatGroup）验证：z 序 / 绘制让位 / 位置跟随收口。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from PySide6.QtWidgets import QApplication, QWidget, QLabel
from PySide6.QtGui import QCursor

app = QApplication([])

from ui.components.float_group import FloatGroup

print("== 1) 注册 + raise_all 整组提级 ==")
owner = QWidget(); owner.resize(300, 200); owner.show()
m1 = QWidget(); m1.resize(100, 50); m1.show()
m2 = QLabel("x"); m2.resize(50, 30); m2.show()
g = FloatGroup(owner)
g.add(m1)
g.add(m2)
g.add(m1)             # 重复注册同一成员应被忽略
assert len(g._members) == 2, "重复注册应忽略"
g.raise_all()
print("   OK  成员注册去重 + raise_all 无异常")

print("== 2) follow() 位置跟随回调 ==")
f2 = QWidget(); f2.resize(200, 100)
seen = []
g2 = FloatGroup(f2)
g2.add(owner, follow=lambda: seen.append("followed"))
g2.follow()
assert seen == ["followed"], "follow 回调应执行"
g2.follow()
assert seen == ["followed", "followed"]
print("   OK  位置跟随统一入口（成员回调按注册顺序执行）")

print("== 3) 绘制让位 maybe_yield / restore_all ==")
class FakeOwner(QWidget):
    is_drawing = False
fo = FakeOwner(); fo.resize(300, 200); fo.show()
mem = QWidget(); mem.setGeometry(0, 0, 120, 60); mem.show()
g3 = FloatGroup(fo)
g3.add(mem)
g3.set_yield_enabled(True)
# 未绘制 → 不让位
g3.maybe_yield()
assert mem.isVisible(), "未绘制时不让位"
# 绘制中，光标进入成员 → 隐藏
fo.is_drawing = True
mem.move(QCursor.pos().x() - 5, QCursor.pos().y() - 5)   # 成员盖住光标
g3.maybe_yield()
assert not mem.isVisible(), "绘制中光标进入成员应隐藏"
assert mem in g3._hidden
# 再次调用不重复隐藏（已隐藏不处理）
g3.maybe_yield()
# 绘制结束恢复
fo.is_drawing = False
g3.restore_all()
assert mem.isVisible(), "restore_all 应恢复显示"
assert not g3._hidden, "恢复后隐藏表应清空"

print("== 4) set_yield_enabled(False) 自动恢复 ==")
fo.is_drawing = True
mem.move(QCursor.pos().x() - 5, QCursor.pos().y() - 5)
g3.maybe_yield()
assert not mem.isVisible()
g3.set_yield_enabled(False)
assert mem.isVisible(), "关闭让位应自动恢复"
assert not g3._hidden

print("== 5) shutdown 清理 ==")
g3.shutdown()
assert not g3._members and not g3._hidden
print("   OK  清理无残留")

print("\nFLOAT_GROUP_ALL_PASS")
