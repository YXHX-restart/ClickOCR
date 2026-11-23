"""历史面板筛选 Tab 验证：全部 / 无文字 / 有文字 / 条码 / 有标注 / 收藏。"""
import sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from ui.windows.history_panel import HistoryPanel
from core_engine.db_manager import DBManager, HistoryStorage

app = QApplication([])
tmp = Path(tempfile.mkdtemp(prefix="tabs_"))
db = DBManager(tmp / "hist.db"); db.init_db()
hs = HistoryStorage(db, tmp / "shots", max_records=10)
png = bytes.fromhex("89504e470d0a1a0a") + b"00" * 64

def save(text, anno=None):
    return hs.save(png, png, text, [{"text": text, "polygon": [[0, 0]], "score": .9}],
                   "tiny", annotations=anno)

rid_text = save("东莞易锋")                       # 有文字、无标注
rid_notext = save("")                            # 无文字
rid_anno = save("客户名称", '[{"type":"rect","points":[[0,0],[9,9]]}]')  # 有文字、有标注
rid_blank = save("   ")                          # 空白 → 无文字

panel = HistoryPanel(hs, lambda r: None, screenshots_dir=tmp / "shots")
print(f"== tab 数 = {panel.tabs.count()}（应 6）")
assert panel.tabs.count() == 6

def rids(lst):
    return {lst.item(i).data(Qt.UserRole)
            for i in range(lst.count()) if lst.item(i).data(Qt.UserRole) is not None}

print("== 全部 tab ==")
all_ids = rids(panel.list_all)
assert {rid_text, rid_notext, rid_anno, rid_blank} <= all_ids
print(f"   OK {len(all_ids)} 条")

print("== 无文字 tab（含空白）==")
no_text = rids(panel.list_no_text)
assert no_text == {rid_notext, rid_blank}, f"无文字 tab 含 {no_text}"
print(f"   OK {no_text}")

print("== 有文字 tab ==")
has_text = rids(panel.list_has_text)
assert has_text == {rid_text, rid_anno}, f"有文字 tab 含 {has_text}"
print(f"   OK {has_text}")

print("== 有标注 tab ==")
has_anno = rids(panel.list_has_anno)
assert has_anno == {rid_anno}, f"有标注 tab 含 {has_anno}"
print(f"   OK {has_anno}")

print("== 空态文案（tab 名读自 ui_text，不硬编码）==")
from ui.base.texts import T
assert panel.tabs.tabText(1) == T("hist.tab_no_text", "图")
assert panel.tabs.tabText(2) == T("hist.tab_has_text", "字")
assert panel.tabs.tabText(4) == T("hist.tab_has_anno", "标注")   # 3 是「条码」

print("== 搜索叠加：搜'东莞' → 各 tab 基于过滤结果 ==")
panel.search.setText("东莞"); panel.reload()
assert rids(panel.list_all) == {rid_text}
assert rids(panel.list_has_text) == {rid_text}
assert rids(panel.list_no_text) == set(), f"无文字应空，实际 {rids(panel.list_no_text)}"
assert rids(panel.list_has_anno) == set()
panel.search.clear()

print("== 收藏切换后星号同步到各 tab ==")
hs.set_favorite(rid_text, True)
panel._toggle_fav(rid_text, True)     # 走面板逻辑
for lst in (panel.list_all, panel.list_has_text):
    for i in range(lst.count()):
        it = lst.item(i)
        if it is not None and it.data(Qt.UserRole) == rid_text:
            w = lst.itemWidget(it)
            assert not getattr(w, "_fav_btn", None).icon().isNull(), \
                "收藏按钮应同步为实心星 SVG 图标"
            break
fav_ids = rids(panel.list_fav)
assert rid_text in fav_ids
print(f"   OK 收藏 tab 含 {fav_ids}")

print("\nHIST_TABS_ALL_PASS")
