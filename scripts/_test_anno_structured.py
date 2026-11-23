"""标注结构化存储验证：序列化 round-trip / 恢复重渲染 / 撤销可用 / 原图不变 / DB 存取。"""
import sys, tempfile, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPointF
import numpy as np
from ui.windows.result_overlay import ResultOverlay
from ui.components.annotation import (anno_to_json, anno_from_json)
from core_engine.db_manager import DBManager, HistoryStorage

app = QApplication([])

ALL_ITEMS = [
    {"type": "rect", "x0": 10, "y0": 10, "x1": 60, "y1": 60, "color": "#ff5f5f", "width": 2},
    {"type": "arrow", "x0": 5, "y0": 5, "x1": 100, "y1": 80, "color": "#5fa0ff", "width": 3},
    {"type": "pen", "points": [[0, 0], [5, 8], [12, 15]], "color": "#ffd54f", "width": 4},
    {"type": "mosaic", "x0": 20, "y0": 20, "x1": 80, "y1": 70, "color": "#ffffff", "width": 2, "block": 8},
    {"type": "mosaic_pen", "points": [[30, 30], [40, 40]], "color": "#ffffff", "width": 2, "block": 8},
    {"type": "text", "x": 50, "y": 90, "text": "批注", "font_size": 18,
     "font": "Microsoft YaHei", "color": "#00c853"},
]

print("== 1) 序列化 round-trip ==")
js = anno_to_json(ALL_ITEMS)
assert json.loads(js)["version"] == 1
back = anno_from_json(js)
assert back == ALL_ITEMS, "round-trip 不等"
assert anno_from_json("") == [] and anno_from_json("bad{") == []
assert anno_from_json('{"items":[{"type":"evil"}]}') == []
print(f"   OK {len(ALL_ITEMS)} 类 items 完整往返")

print("== 2) 恢复历史重渲染：load_annotations → scene 有标注项 ==")
img = np.zeros((300, 400, 3), dtype=np.uint8)
ov = ResultOverlay(img, (0, 0, 400, 300), 1.0)
ov.show()
ov.load_annotations(js)
assert ov._anno is not None and ov._anno.data == ALL_ITEMS
rendered = len(ov._anno._items)
assert rendered >= len(ALL_ITEMS), f"渲染项 {rendered} 应≥{len(ALL_ITEMS)}"
print(f"   OK 渲染 {rendered} 个 scene 项（含 arrow 线+三角）")

print("== 3) 原图不变：load 标注后 img/_pix 未污染 ==")
before_img = ov.img.copy()
before_pix = ov._pix.toImage()
assert np.array_equal(ov.img, before_img), "self.img 被修改！"
assert ov._pix.toImage() == before_pix, "self._pix 被修改！"
print("   OK 原图与底图像素均未变")

print("== 4) 恢复后撤销可用：画新步骤 → undo 回加载态 / redo 恢复 ==")
s = ov._anno
s.set_tool("rect")
s.mouse_press(QPointF(10, 200))
s.mouse_move(QPointF(90, 260))       # 拖动更新 x1/y1
s.mouse_release(QPointF(90, 260))
assert len(s.data) == len(ALL_ITEMS) + 1, "新步骤未加入"
s.undo()
assert len(s.data) == len(ALL_ITEMS), "undo 应回到加载态"
s.redo()
assert len(s.data) == len(ALL_ITEMS) + 1, "redo 应恢复新步骤"
print("   OK 恢复后画新标注可 undo/redo（步骤随时撤回）")

print("== 5) DB 存取：update_annotations → get → 重渲染 ==")
tmp = Path(tempfile.mkdtemp(prefix="astr_"))
db = DBManager(tmp / "hist.db"); db.init_db()
hs = HistoryStorage(db, tmp / "shots", max_records=10)
png = bytes.fromhex("89504e470d0a1a0a") + b"00" * 64
rid = hs.save(png, png, "t", [{"text": "t", "polygon": [[0,0]], "score": .9}], "tiny")
assert hs.update_annotations(rid, js)
rec = hs.get(rid)
assert rec["annotations"] == js
ov2 = ResultOverlay(img.copy(), (0, 0, 400, 300), 1.0)
ov2.load_annotations(rec["annotations"])
assert ov2._anno.data == ALL_ITEMS
print("   OK DB 存 JSON 字符串，恢复后重渲染一致")

print("\nANNO_STRUCTURED_ALL_PASS")
