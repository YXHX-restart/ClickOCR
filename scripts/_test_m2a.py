"""M2·α headless 验证：core_engine 无 Qt、建库 10+1 表、HistoryStorage CRUD/搜索/收藏/FIFO、日志、ClickOCR 兼容层。"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # ClickOCR
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))                   # core_engine

print("== 1) core_engine 无 Qt 可导入 ==")
import core_engine
import core_engine.config, core_engine.logging_setup, core_engine.ai_pipeline, core_engine.db_manager
import ast
import inspect
_mods = (core_engine.config, core_engine.logging_setup,
         core_engine.ai_pipeline, core_engine.db_manager)
# 判据是「有没有 import Qt」而不是「源码里有没有出现 PySide 这几个字」：
# 子串扫描会把**说明性注释**也判成违规（真的误报过——`core_engine/__init__.py`
# 与 db_manager 的注释都写了「本包禁止 import PySide6」，那是文档不是依赖）。
_qt_imports = []
for _m in _mods:
    for _n in ast.walk(ast.parse(inspect.getsource(_m))):
        if isinstance(_n, ast.Import):
            _qt_imports += [a.name for a in _n.names
                            if a.name.split(".")[0] in ("PySide6", "PyQt5", "PyQt6")]
        elif isinstance(_n, ast.ImportFrom):
            if (_n.module or "").split(".")[0] in ("PySide6", "PyQt5", "PyQt6"):
                _qt_imports.append(_n.module or "")
assert not _qt_imports, f"core_engine 出现 Qt 依赖：{_qt_imports}"
print("   OK：四个子模块均无 PySide/PyQt **导入**（AST 判定，不受注释文字影响）")

print("== 2) 建库：表齐全 + vec0 ==")
tmp = Path(tempfile.mkdtemp(prefix="m2a_"))
db = core_engine.db_manager.DBManager(tmp / "clickocr.db")
vec0_ok = db.init_db()
names = set(db.table_names())
expect = {"screenshot_history", "images", "image_ocr", "image_tags",
          "image_ocr_extend", "image_search", "table_cache",
          "image_embedding", "image_embedding_vec", "image_hash", "image_layout",
          "trg_image_ocr_insert", "trg_image_ocr_delete",
          "trg_images_caption_update", "trg_tags_insert"}
missing = expect - names
print(f"   vec0 可用: {vec0_ok} | 表/触发器数: {len(names)}")
print("   缺失:", sorted(missing) if missing else "无")
assert vec0_ok and not missing
# WAL 生效
conn = db._connect()
mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
conn.close()
print(f"   journal_mode: {mode}")
assert mode.lower() == "wal"

print("== 3) HistoryStorage CRUD / 搜索 / 收藏 / FIFO ==")
hs = core_engine.db_manager.HistoryStorage(db, tmp / "screenshots", max_records=3)
png = bytes.fromhex("89504e470d0a1a0a") + b"00" * 64   # 伪 PNG 头+数据
def save(text):
    return hs.save(png, png, text,
                   [{"text": text, "polygon": [[0,0],[1,1]], "score": 0.9}],
                   "tiny", "cpu")
r1 = save("东莞易锋钢材有限公司")
r2 = save("发货时间2026年5月15日")
r3 = save("客户名称： 测试")
lst = hs.list()
print(f"   3 条保存，倒序 id: {[r['id'] for r in lst]}")
assert [r["id"] for r in lst] == [r3, r2, r1]
assert (tmp / "screenshots").exists() and (tmp / "screenshots" / "thumb").exists()
# 搜索
hits = hs.search("东莞")
print(f"   搜索'东莞' → {[r['ocr_text'] for r in hits]}")
assert len(hits) == 1 and hits[0]["id"] == r1
# 收藏
hs.set_favorite(r1, True)
favs = hs.list_favorites()
print(f"   收藏列表 id: {[r['id'] for r in favs]}")
assert [r["id"] for r in favs] == [r1]
# FIFO：max=3，普通已满，再存 2 条应清理最旧非收藏（r2, r3），收藏 r1 保留
save("第四条")
save("第五条")
remaining = {r["id"]: r for r in hs.list(10)}
non_fav = [r for r in remaining.values() if not r["is_favorite"]]
print(f"   FIFO 后剩余 id: {sorted(remaining)} | 普通数: {len(non_fav)} (收藏不占配额)")
assert sorted(remaining) == [1, 3, 4, 5]     # r2 被清理，收藏 r1 保留
assert len(non_fav) == 3 and hs.count() == 4  # 收藏不受条数限制
assert remaining[1]["is_favorite"] == 1
# 删除 + 清空
hs.delete(r1)
hs.clear()
print(f"   删除/清空后 count: {hs.count()}")
assert hs.count() == 0

print("== 4) 配置新段 ==")
from core_engine.config import ConfigManager as CECM
from core_engine.config import _DEFAULTS as _DEF
cfg = CECM(app_root=tmp)
assert cfg.getint("software", "history_max_count", 50) == 50
# 断言「新建的 app_root 里这些新段被预置成出厂值」，**不写死具体键值** ——
# 出厂值会随功能调整（select_all 已从 shift+c 改为 ctrl+a），写死会变成过期断言。
for _sec, _key in (("hotkey_overlay", "select_all"),
                   ("engine", "preprocess_preset"),
                   ("software", "selection_mode")):
    _want = _DEF[_sec][_key]
    assert cfg.get(_sec, _key, "<缺>") == _want, \
        f"{_sec}.{_key}: {cfg.get(_sec, _key, '<缺>')!r} != 出厂值 {_want!r}"
print("   [software]/[hotkey_overlay]/[engine] 预置 OK")

print("== 5) 日志轮转 ==")
import logging
from core_engine.logging_setup import setup_logging, get_logger
setup_logging(app_root=tmp, level=logging.INFO, console=False)
lg = get_logger("test")
lg.info("hello m2a")
assert (tmp / "logs" / "clickocr.log").exists()
assert "hello m2a" in (tmp / "logs" / "clickocr.log").read_text(encoding="utf-8")
print("   logs/clickocr.log 生成 OK")

print("== 6) ClickOCR 兼容层 ==")
sys.path.insert(0, str(ROOT))
from config import ConfigManager   # noqa: E402
ccfg = ConfigManager()
assert ccfg.get("hotkey_global", "screenshot_combo", "ctrl+shift+r") == "ctrl+shift+r"
# 开发模式加载路径 = app_root/config.ini（不存在则回退 resources/config.ini；保存写 resources/）
assert ccfg.path == ROOT / "config.ini"
print(f"   ClickOCR config 兼容 OK（path={ccfg.path}）")
import main  # noqa: F401   # 触发 sys.path 注入 + app 导入链
print("   main 导入 OK")

print("\nM2A_ALL_PASS")
