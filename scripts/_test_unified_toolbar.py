"""统一工具栏/纯信息状态栏/PinDot 全链路冒烟：
标注模式展开 → 绘制 → 序列化 → 退出 → 钉图（隐藏双栏只留钉子）→ 取消钉图恢复。"""
import sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPointF
import numpy as np
from ui.windows.result_overlay import ResultOverlay
from ui.components.status_strip import StatusStrip
from ui.components.toolbar import ToolBar

app = QApplication([])
img = np.zeros((300, 400, 3), dtype=np.uint8)

print("== 1) 双窗关联：状态栏(纯信息) + 工具栏(操作) ==")
ov = ResultOverlay(img, (0, 0, 400, 300), 1.0)
strip = StatusStrip()
tb = ToolBar()
ov.set_status_strip(strip)
ov.set_toolbar(tb)
ov.show(); strip.show(); tb.show()
ov._sync_status_strip_pos(); ov._sync_toolbar_pos()
app.processEvents()
assert ov._status_strip is strip and ov._toolbar is tb
assert not hasattr(strip, "btn_anno"), "状态栏应纯信息（无按钮）"
assert strip.status.isVisible() and strip.preview.isVisible()
assert tb.btn_anno is not None and tb.btn_done is not None, "工具栏应有常驻按钮"
assert not tb.tools.isVisible(), "常态：工具段收起"
w_normal = tb.sizeHint().width()     # 记录常态宽度（短条）
h_normal = tb.sizeHint().height()
assert 300 < w_normal < 700, f"常态应为水平短条（300~700px 宽），实际 {w_normal}"
assert h_normal < 60, f"工具栏应为单行（高<60），实际 {h_normal} —— 竖排回归？"
print(f"   OK  状态栏纯信息 + 工具栏常态短条（{w_normal}x{h_normal}）")

print("== 2) 进入标注 → 工具栏内联展开变长 ==")
ov._enter_annotation()
app.processEvents()
assert ov._anno_mode and ov._anno is not None
assert tb.tools.isVisible(), "标注态工具段应展开"
assert tb.btn_anno.isChecked(), "标注按钮应高亮"
w_anno = tb.sizeHint().width()
h_anno = tb.sizeHint().height()
assert w_anno > w_normal, f"展开后宽度应大于常态（{w_anno} vs {w_normal}）"
assert h_anno < 60, f"标注态仍应单行（高<60），实际 {h_anno}"
print(f"   OK  工具段展开（宽度 {w_normal}→{w_anno}，高度 {h_normal}→{h_anno}），标注按钮高亮")

print("== 3) 绘制矩形 → 撤销 → 重做 → 序列化 ==")
s = ov._anno
s.set_tool("rect")
s.mouse_press(QPointF(20, 20)); s.mouse_move(QPointF(120, 90)); s.mouse_release(QPointF(120, 90))
assert len(s.data) == 1 and s.data[0]["type"] == "rect", "应画出一个矩形"
j = json.loads(s.json())
assert j["version"] == 1 and j["items"][0]["type"] == "rect", "序列化格式应保持"
s.undo(); assert len(s.data) == 0, "撤销应移除矩形"
s.redo(); assert len(s.data) == 1, "重做应恢复矩形"
print("   OK  绘制/撤销/重做/序列化（命令模式+图元工厂）不回归")

print("== 4) 退出标注 → 工具段收起 + 标注 JSON 落库 + 宽度收回 ==")
saved = []
ov.annotations_changed.connect(saved.append)
ov._exit_annotation()
from PySide6.QtTest import QTest
QTest.qWait(60); app.processEvents()   # 等 set_anno_mode 延迟收紧（布局异步重算）
assert not ov._anno_mode
assert not tb.tools.isVisible(), "退出后工具段应收起"
assert not tb.btn_anno.isChecked(), "标注按钮应取消高亮"
w_after = tb.width()
assert w_after < 700, f"退出后工具栏应收回短条宽度（<700），实际 {w_after}"
assert w_after == tb.sizeHint().width(), \
    f"窗口宽度应与 sizeHint 一致（无按钮拉伸），{w_after} vs {tb.sizeHint().width()}"
assert saved and json.loads(saved[-1])["items"], "退出应 emit 标注 JSON"
print(f"   OK  退出标注：工具段收起（宽度 {w_anno}→{w_after}）+ JSON 已发出")

print("== 5) 钉图 → 图片/状态栏/工具栏隐藏，只留 PinDot ==")
dot = ov._ensure_pin_dot()
dot.show()
ov.set_pinned(True)
app.processEvents()
assert ov._pinned
assert not strip.isVisible(), "钉图时状态栏应隐藏"
assert not tb.isVisible(), "钉图时工具栏应隐藏"
assert not ov.btn_close.isVisible(), "钉图时关闭按钮应隐藏"
assert dot.isVisible(), "钉子应显示"
assert dot.width() == 32 and dot.height() == 32
print("   OK  双栏隐藏，只留 32x32 钉子")

print("== 6) 取消钉图 → 全部恢复 ==")
ov.set_pinned(False)
dot.hide()
ov._sync_status_strip_pos(); ov._sync_toolbar_pos()
app.processEvents()
assert not ov._pinned
assert strip.isVisible() and tb.isVisible(), "取消钉图后双栏应恢复"
assert ov.btn_close.isVisible(), "关闭按钮应恢复"
print("   OK  取消钉图：双栏 + 关闭按钮恢复")

print("== 7) 状态栏 Esc：非标注态关闭状态栏 ==")
ov._on_strip_esc()
assert not strip.isVisible(), "Esc 应关闭状态栏"
ov._status_strip.show()      # 恢复
print("   OK  Esc 关闭状态栏")

print("\nUNIFIED_TOOLBAR_ALL_PASS")
