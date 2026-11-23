"""审查修复验证脚本（临时）：针对本轮修复的核心逻辑做无 UI 验证。

覆盖：db_manager 路径契约/lastrowid/回滚/旧格式兼容、config 插值与写回来源、
table_export 公式注入与 HTML 转义、rapid_engine 双检锁冒烟、annotation 命令栈
（无 Qt 部分不测，见 _test_anno_* 系列）。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
FAIL = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        FAIL.append(name)


print("== 1. db_manager：路径契约 + lastrowid + 删除 + 旧格式兼容 ==")
from core_engine.db_manager import DBManager, HistoryStorage

# ---- 临时目录 / DBManager 的配套收尾 ----
# DBManager 现在是「每线程一个长连接」（不再每操作短连接），连接会一直占着 db
# 文件的句柄；Windows 下句柄未释放就删目录会 WinError 32。故统一登记、清理前先关。
_OPEN_DBS: list = []


def _open_db(path, *a, **kw):
    d = DBManager(path, *a, **kw)
    _OPEN_DBS.append(d)
    return d


class _TempDir(tempfile.TemporaryDirectory):
    def cleanup(self):
        for d in list(_OPEN_DBS):
            d.close()
        _OPEN_DBS.clear()
        return super().cleanup()


# 本脚本内统一替换（`import tempfile as _tf` 拿到的是同一个模块对象，一并生效）：
# 每个临时目录在清理前都会先关掉登记的 DBManager。
tempfile.TemporaryDirectory = _TempDir


with tempfile.TemporaryDirectory() as td:
    db = _open_db(Path(td) / "t.db")
    db.init_db()
    shots = Path(td) / "custom_shots"          # 自定义目录（原 bug 触发条件）
    hs = HistoryStorage(db, shots, max_records=50)
    rid = hs.save(image_bytes=b"x" * 10, thumb_bytes=b"y" * 5,
                  ocr_text="hi", ocr_boxes=None, ext="png")
    check("save 返回真实自增 id", isinstance(rid, int) and rid > 0, f"rid={rid}")
    row = hs.get(rid)
    name = row["screenshot_path"]
    check("screenshot_path 存纯文件名", "/" not in name and "\\" not in name,
          f"path={name!r}")
    check("主图写入自定义目录", (shots / name).exists())
    check("缩略图写入自定义目录", (shots / "thumb" / name).exists())
    hs.delete(rid)
    check("delete 删除主图（自定义目录，原 bug）", not (shots / name).exists())
    check("delete 删除缩略图", not (shots / "thumb" / name).exists())
    # 旧库格式兼容：screenshots/<name> 前缀
    rid2 = hs.save(image_bytes=b"x" * 10, thumb_bytes=b"", ocr_text="",
                   ocr_boxes=None, ext="png")
    row2 = hs.get(rid2)
    db.execute("UPDATE screenshot_history SET screenshot_path=? WHERE id=?",
               (f"screenshots/{row2['screenshot_path']}", rid2))
    n2 = Path(hs.get(rid2)["screenshot_path"]).name
    hs.delete(rid2)
    check("旧格式（screenshots/ 前缀）删除仍生效",
          not (shots / n2).exists())
    # purge 走 _delete_files（自定义目录）
    hs.max_records = 1
    for _ in range(3):
        hs.save(image_bytes=b"z" * 4, thumb_bytes=b"", ocr_text="",
                ocr_boxes=None, ext="png")
    leftovers = [p for p in shots.glob("*.png")]
    check("purge 按 FIFO 清理（自定义目录）",
          len(leftovers) <= 1, f"left={len(leftovers)}")
    # execute 返回 rowcount、execute_returning_id 返回自增 id（语义区分）
    rid3 = hs.save(image_bytes=b"z" * 4, thumb_bytes=b"", ocr_text="",
                   ocr_boxes=None, ext="png")
    n_up = db.execute("UPDATE screenshot_history SET ocr_text=? WHERE id=?",
                      ("marked", rid3))
    check("execute 返回 rowcount（UPDATE 影响 1 行）", n_up == 1, f"n={n_up}")
    rid4 = db.execute_returning_id(
        "INSERT INTO screenshot_history (screenshot_path, ocr_text, ocr_boxes,"
        " model_tier, device, created_at, updated_at, file_size, is_favorite,"
        " annotations, has_table, status) VALUES ('x.png','','',"
        " '', '', 0, 0, 0, 0, NULL, 0, 0)")
    check("execute_returning_id 返回自增 id", rid4 == rid3 + 1,
          f"rid3={rid3} rid4={rid4}")

print("== 1b. created_at / updated_at 拆分（分组·搜索·FIFO 不再被操作时间污染）==")
import time as _time

with tempfile.TemporaryDirectory() as td:
    db2 = _open_db(Path(td) / "t2.db")
    db2.init_db()
    hs2 = HistoryStorage(db2, Path(td) / "shots", max_records=50)
    rid_a = hs2.save(image_bytes=b"a" * 4, thumb_bytes=b"", ocr_text="旧图",
                     ocr_boxes=None, ext="png")
    created_a = hs2.get(rid_a)["created_at"]
    _time.sleep(0.02)
    rid_b = hs2.save(image_bytes=b"b" * 4, thumb_bytes=b"", ocr_text="新图",
                     ocr_boxes=None, ext="png")
    check("list 初始顺序：后落库的在最前",
          [r["id"] for r in hs2.list(10)] == [rid_b, rid_a],
          f"{[r['id'] for r in hs2.list(10)]}")
    _time.sleep(0.02)
    # 对「旧图」做一次操作（标注）——旧实现会改写 created_at，把它伪装成新截图
    hs2.update_annotations(rid_a, "[]")
    row_a = hs2.get(rid_a)
    check("操作后 created_at 不再被改写", row_a["created_at"] == created_a,
          f"{created_a} → {row_a['created_at']}")
    check("操作后 updated_at 已刷新", row_a["updated_at"] > created_a,
          f"updated_at={row_a['updated_at']} created={created_a}")
    check("list 按 updated_at 倒序（刚操作的旧图置顶）",
          [r["id"] for r in hs2.list(10)] == [rid_a, rid_b],
          f"{[r['id'] for r in hs2.list(10)]}")
    # FIFO 按 created_at：刚操作过的「旧图」仍应被优先淘汰（旧实现会因变新而豁免）
    hs2.max_records = 1
    hs2.save(image_bytes=b"c" * 4, thumb_bytes=b"", ocr_text="最新",
             ocr_boxes=None, ext="png")
    left = {r["id"] for r in hs2.list(10)}
    check("FIFO 淘汰用 created_at（旧图被清、不因刚标注而豁免）",
          rid_a not in left, f"left={left}（rid_a={rid_a} 应被淘汰）")
    check("purge 后剩余条数符合上限", len(left) == 1, f"left={left}")

with tempfile.TemporaryDirectory() as td:
    # 旧库（无 updated_at 列）→ init_db 应就地迁移并回填
    import sqlite3
    p = Path(td) / "old.db"
    conn = sqlite3.connect(str(p))
    conn.execute(
        "CREATE TABLE screenshot_history (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " screenshot_path TEXT NOT NULL, ocr_text TEXT, ocr_boxes TEXT,"
        " model_tier TEXT NOT NULL, device TEXT NOT NULL, created_at REAL NOT NULL,"
        " file_size INTEGER, is_favorite INTEGER DEFAULT 0, annotations TEXT,"
        " has_table INTEGER DEFAULT 0, status INTEGER DEFAULT 0)")
    for i, ts in enumerate((1000.0, 2000.0, 3000.0)):
        conn.execute(
            "INSERT INTO screenshot_history (screenshot_path, ocr_text, ocr_boxes,"
            " model_tier, device, created_at, file_size, is_favorite, annotations,"
            " has_table, status) VALUES (?,?,?,?,?,?,?,0,NULL,0,0)",
            (f"old{i}.png", "", None, "", "", ts, 0))
    conn.commit()
    conn.close()
    db3 = _open_db(p)
    db3.init_db()
    rows = db3.query("SELECT created_at, updated_at FROM screenshot_history")
    check("旧库自动补 updated_at 列",
          all("updated_at" in r.keys() for r in rows))
    check("旧库 updated_at 由 created_at 回填",
          all(r["updated_at"] == r["created_at"] for r in rows),
          f"{[(r['created_at'], r['updated_at']) for r in rows]}")
    # 幂等：再跑一次不应报错、值不变
    db3.init_db()
    rows2 = db3.query("SELECT created_at, updated_at FROM screenshot_history")
    check("迁移幂等（重复 init_db 不改变结果）",
          [(r["created_at"], r["updated_at"]) for r in rows]
          == [(r["created_at"], r["updated_at"]) for r in rows2])

print("== 1c. main.py 不再把 core_engine/ 注入 sys.path ==")
_src = (Path(__file__).resolve().parent.parent / "main.py").read_text(
    encoding="utf-8")
check("main.py 无 sys.path.insert", "sys.path.insert" not in _src)
import config as _root_config          # 根目录兼容层
import core_engine.config as _ce_config
check("from config 解析到根目录兼容层（非 core_engine.config）",
      _root_config.ConfigManager is not _ce_config.ConfigManager
      and _root_config.ConfigManager.__mro__[1] is _ce_config.ConfigManager,
      f"mro={_root_config.ConfigManager.__mro__}")
_c = _root_config.ConfigManager()
check("兼容层把 app_root 固定为 ClickOCR 根目录",
      _c.app_root == Path(__file__).resolve().parent.parent,
      f"app_root={_c.app_root}")

print("== 2. config：% 插值不再崩溃 + %% 兼容 + 写回加载来源 ==")
from core_engine.config import ConfigManager

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "config.ini"
    p.write_text("[software]\nname_pattern = shot_%d\n", encoding="utf-8")
    cm = ConfigManager(app_root=td)
    v = cm.get("software", "name_pattern")
    check("含单个 % 的值可正常读取（原启动即崩）", v == "shot_%d", f"v={v!r}")
    # %% 旧转义兼容
    p2 = Path(td) / "config2.ini"
    p2.write_text("[software]\nname_pattern = a%%b\n", encoding="utf-8")
    cm2 = ConfigManager(app_root=td, path=p2)
    check("旧版 %% 转义还原为 %", cm2.get("software", "name_pattern") == "a%b",
          f"v={cm2.get('software', 'name_pattern')!r}")
    # 写回加载来源：显式 path 读哪个写哪个
    cm2.cp.set("software", "name_pattern", "changed")
    cm2.save()
    check("save 写回实际加载来源（显式 path）",
          "changed" in p2.read_text(encoding="utf-8"))
    # dev 模式：app_root/config.ini 存在时 save 必须写它（原写 resources/）
    dev_root = Path(td) / "dev"
    (dev_root).mkdir()
    user_cfg = dev_root / "config.ini"
    user_cfg.write_text("[software]\nname_pattern = user_val\n",
                        encoding="utf-8")
    (dev_root / "resources").mkdir()
    (dev_root / "resources" / "config.ini").write_text(
        "[software]\nname_pattern = default_val\n", encoding="utf-8")
    cm3 = ConfigManager(app_root=dev_root)
    check("dev 模式优先读 app_root/config.ini",
          cm3.get("software", "name_pattern") == "user_val")
    cm3.cp.set("software", "name_pattern", "user_changed")
    cm3.save()
    check("dev 模式 save 写回用户 config.ini（原漂移到 resources/）",
          "user_changed" in user_cfg.read_text(encoding="utf-8"))
    check("dev 模式 save 不再覆盖 resources/config.ini",
          "default_val" in (dev_root / "resources" / "config.ini")
          .read_text(encoding="utf-8"))

print("== 3. table_export：公式注入 + HTML 转义/事件属性过滤 ==")
from core_engine.ai_pipeline import TableCell, TableResult
from core_engine.table_export import TableExportService

svc = TableExportService()
cells = [TableCell(text="=HYPERLINK(\"http://evil\",\"x\")", row=0, col=0),
         TableCell(text="<img onerror=alert(1)>", row=0, col=1)]
t = TableResult(cells=cells, row_count=1, col_count=2)
# Excel 公式注入
with tempfile.TemporaryDirectory() as td:
    xp = str(Path(td) / "t.xlsx")
    try:
        svc.export_excel(t, xp)
        from openpyxl import load_workbook
        wb = load_workbook(xp)
        ws = wb.active
        c = ws.cell(row=1, column=1)
        check("以 = 开头的单元格按文本存储（data_type='s'）",
              c.data_type == "s" and str(c.value).startswith("="),
              f"type={c.data_type}")
    except RuntimeError:
        check("openpyxl 缺失（跳过公式注入验证）", True)
# HTML 转义（自建 grid 路径）
h = svc.to_html(t)
check("单元格文本 HTML 转义", "&lt;img onerror=" in h, h)
check("转义后无裸 <img", "<img" not in h)
# 模型原始 HTML 事件属性过滤
t2 = TableResult(cells=[], row_count=0, col_count=0,
                 html="<table><tr><td onerror='x'>a</td>"
                      "<td onload=go>b</td></tr></table>")
h2 = svc.to_html(t2)
check("模型 HTML 过滤内联事件属性",
      "onerror" not in h2 and "onload" not in h2 and ">a<" in h2, h2)

# 第 14 批：只剥 on* 挡不住「不需要事件属性」的注入 —— script / iframe /
# javascript: URI 必须一并处理，否则换 WebEngine 或导出给别人看即成 XSS
from core_engine.table_export import _sanitize_html as _san

_t3 = TableResult(cells=[], row_count=0, col_count=0,
                  html="<table><tr><td><script>alert(1)</script>ok</td></tr></table>"
                       "<iframe src='javascript:alert(2)'></iframe>"
                       "<a href=\"javascript:alert(3)\">x</a>"
                       "<img src='data:text/html,<script>alert(4)</script>'>"
                       "<img src='data:image/png;base64,AAA'>")
_h3 = svc.to_html(_t3)
check("模型 HTML 剔除 <script> 整段（含内容）",
      "alert(1)" not in _h3 and "<script" not in _h3, _h3)
check("模型 HTML 剔除 <iframe>（含 javascript: src）",
      "<iframe" not in _h3 and "alert(2)" not in _h3, _h3)
check("模型 HTML 剔除 javascript: 协议的 href",
      "javascript:" not in _h3 and ">x<" in _h3, _h3)
check("模型 HTML 剔除 data:text/html 内联文档",
      "data:text/html" not in _h3 and "alert(4)" not in _h3, _h3)
check("合法的 data:image/* 不被误伤（只拦 data:text/html）",
      "data:image/png" in _h3, _h3)
check("表格本体未被消毒破坏（tr/td 保留）",
      "<tr>" in _h3 and "<td>" in _h3 and "ok" in _h3, _h3)
# 混淆写法：HTML 解析器会先归一化实体/空白，正则也要归一化后判定
check("混淆的 java\\tscript: 也被拦住",
      "javascript" not in _san('<a href="java\tscript:alert(5)">y</a>').lower(),
      _san('<a href="java\tscript:alert(5)">y</a>'))
check("空串/None 输入不抛", _san("") == "" and _san(None) is None)

print("== 4. rapid_engine：双检锁 + DML 探测缓存（不加载模型） ==")
import threading
from ocr import rapid_engine as re_mod

eng = re_mod.RapidOcrEngine(model_set="v6-tiny", lazy=True)
results, errors = [], []
_orig = re_mod.RapidOcrEngine._build_v6
calls = []

def _fake_build(self):
    calls.append(1)
    import time
    time.sleep(0.05)
    return "ENGINE"

re_mod.RapidOcrEngine._build_v6 = _fake_build
try:
    ts = [threading.Thread(target=lambda: results.append(eng._ensure_engine()))
          for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
finally:
    re_mod.RapidOcrEngine._build_v6 = _orig
check("并发 _ensure_engine 只构建一次（双检锁）", len(calls) == 1,
      f"calls={len(calls)}")
check("并发调用全部拿到同一引擎", all(r == "ENGINE" for r in results),
      f"results={results[:3]}")
# DML 探测缓存
re_mod._DML_DEVICE_CACHE = None
calls2 = []

def _fake_probe(det_path=None, first_ok=False):
    # 签名必须与真实 probe_dml_devices 一致（含 first_ok）：调用方
    # _detect_dml_device 会带 first_ok=True，fake 少一个参数就会 TypeError
    calls2.append(1)
    return [{"id": 1, "ok": True}]

_orig_probe = re_mod.probe_dml_devices
re_mod.probe_dml_devices = _fake_probe
try:
    d1 = re_mod._detect_dml_device()
    d2 = re_mod._detect_dml_device()
finally:
    re_mod.probe_dml_devices = _orig_probe
check("DML 探测结果缓存（两次调用只探测一次）",
      d1 == 1 and d2 == 1 and len(calls2) == 1, f"calls={len(calls2)}")

print("== 1d. 搜索不再命中标注 JSON 的键名 ==")
import tempfile as _tf

with _tf.TemporaryDirectory() as td:
    _db = _open_db(Path(td) / "s.db")
    _db.init_db()
    _hs = HistoryStorage(_db, Path(td) / "shots", max_records=50)
    # 必须用**真实序列化器**造数据：这里原先手写 `{"marks": ...}`，与
    # anno_to_json 实际产出的 `{"version":N,"items":[...]}` 不一致，于是
    # db_manager 里写错的 JSON 路径（`$.marks`）照样「通过」—— 假数据把真 bug
    # 挡住了。（改用 anno_to_json 后当场暴露：搜标注文字 0 命中。）
    from ui.core.anno_core import anno_to_json as _anno_to_json
    _anno = _anno_to_json([
        {"type": "text", "x": 1, "y": 2, "text": "发票号码",
         "font_size": 18, "font": "Arial", "color": "#ff0000"},
        {"type": "rect", "x0": 3, "y0": 4, "x1": 5, "y1": 6, "color": "#ff0000"},
    ])
    check("测试数据来自真实序列化器（键名与实际入库格式一致）",
          '"items"' in _anno and "marks" not in _anno, _anno[:60])
    _r1 = _hs.save(image_bytes=b"a" * 4, thumb_bytes=b"", ocr_text="",
                   ocr_boxes=None, ext="png")
    _hs.update_annotations(_r1, _anno)
    _r2 = _hs.save(image_bytes=b"b" * 4, thumb_bytes=b"", ocr_text="",
                   ocr_boxes=None, ext="png")
    _hs.update_annotations(_r2, "这不是 JSON")          # 脏数据不得把搜索带崩

    _hit_text = {r["id"] for r in _hs.search("发票号码", 50)}
    check("搜标注里的真实文字 → 命中", _r1 in _hit_text, f"{_hit_text}")
    for _kw in ("items", "marks", "rect", "mosaic", "color", "type", "points"):
        _got = {r["id"] for r in _hs.search(_kw, 50)}
        check(f"搜 JSON 键名 {_kw!r} → 不命中", _r1 not in _got,
              f"误命中 {_got}（原实现整串 LIKE 会命中）")
    try:
        _hs.search("任意词", 50)                         # 含非法 JSON 记录
        check("标注 JSON 非法时搜索不抛错", True)
    except Exception as _e:                              # noqa: BLE001
        check("标注 JSON 非法时搜索不抛错", False, repr(_e))

print("== 1e. 配置：资源只读、数据分离（打包不再带出个人配置）==")
_root = Path(__file__).resolve().parent.parent
import configparser as _cpmod
import shutil as _shut

from core_engine.config import ConfigManager as _CM
from core_engine.config import _DEFAULTS as _DEF

_cfg_src = (_root / "core_engine" / "config.py").read_text(encoding="utf-8")
check("提供 _seed_from_template（首启由模板播种到可写位置）",
      "def _seed_from_template" in _cfg_src
      and "self._seed_from_template()" in _cfg_src)
with _tf.TemporaryDirectory() as _td:
    (Path(_td) / "resources").mkdir()
    _shut.copyfile(_root / "resources" / "config.ini",
                   Path(_td) / "resources" / "config.ini")
    _c = _CM(app_root=_td)
    check("首次运行生成用户配置（而非直接读写模板）",
          _c.path.exists() and _c._load_source == _c.path,
          f"path={_c.path} src={_c._load_source}")
    _c.cp.set("software", "save_quality", "42")
    _c.save()
    _tpl_txt = (Path(_td) / "resources" / "config.ini").read_text(encoding="utf-8")
    check("save() 不回写只读模板（个人配置不会进发布包）",
          "save_quality = 42" not in _tpl_txt)
    check("二次启动读回用户配置",
          _CM(app_root=_td).get("software", "save_quality") == "42")

_tpl_path = _root / "resources" / "config.ini"
_cc = _cpmod.ConfigParser(inline_comment_prefixes=("#", ";"), interpolation=None)
_cc.read(_tpl_path, encoding="utf-8")
_drift = [f"{s}.{k}: 模板={_cc.get(s, k)!r} 兜底={v!r}"
          for s, opts in _DEF.items() if _cc.has_section(s)
          for k, v in opts.items()
          if _cc.has_option(s, k) and _cc.get(s, k) != v]
check("出厂模板与 _DEFAULTS 无漂移（两来源合一）", not _drift,
      "\n      " + "\n      ".join(_drift))
check("模板不含机器相关项 cuda_probe",
      not any(ln.strip().startswith("cuda_probe")
              for ln in _tpl_path.read_text(encoding="utf-8").splitlines()))

print("== 5e. 滚动截图重入保护 + 模型完整性校验 ==")
_cap_src = (_root / "appmod" / "capture_mixin.py").read_text(encoding="utf-8")
check("_scroll_capture 有重入保护（_scroll_active）",
      'getattr(self, "_scroll_active", False)' in _cap_src
      and "self._scroll_active = True" in _cap_src)
check("重入标志只在收尾入口清除（唯一清除点）",
      "teardown_scroll(self)\n        self._scroll_active = False" in _cap_src)

from ocr.rapid_engine import _V6_MODELS as _V6M
from ocr.rapid_engine import verify_models as _vm

_re_src = (_root / "ocr" / "rapid_engine.py").read_text(encoding="utf-8")
check("_build_v6 首次加载时校验本档模型文件",
      "_check_models_once([det_name, rec_name, dict_name])" in _re_src)
check("rapid_engine 已导入 get_logger（否则校验告警会 NameError）",
      "from core_engine.logging_setup import get_logger" in _re_src)
_v6n = [_V6M["v6-tiny"][1], _V6M["v6-tiny"][2], _V6M["v6-tiny"][3]]
check("tiny 档模型 SHA256 校验通过", _vm(_v6n) == [], f"{_vm(_v6n)}")
check("清单全量 SHA256 校验通过", _vm() == [], f"问题 {_vm()}")
check("校验不阻断：缺失文件只进列表不抛异常",
      isinstance(_vm(["__nope__.onnx"]), list))

print("== 5b. 浮窗存活校验已收敛到唯一入口 _live_overlay() ==")
import re as _re

_root = Path(__file__).resolve().parent.parent
from appmod.float_mixin import FloatMixin
check("FloatMixin 提供 _live_overlay()", callable(
    getattr(FloatMixin, "_live_overlay", None)))

_fm_src = (_root / "appmod" / "float_mixin.py").read_text(encoding="utf-8")
check("_on_overlay_closed 会清空 self._overlay",
      "if ov is self._overlay:" in _fm_src and "self._overlay = None" in _fm_src,
      "关闭路径未置空 → 悬空引用")
# 结构守卫：其它 mixin 不应再直接读 self._overlay（只允许写入/None 比较）
for _f in ("engine_mixin.py", "history_mixin.py", "drop_mixin.py",
           "capture_mixin.py"):
    _src = (_root / "appmod" / _f).read_text(encoding="utf-8")
    _bad = [ln.strip() for ln in _src.splitlines()
            if _re.search(r"(?<![\w.])self\._overlay\b(?!\s*=(?!=))", ln)
            and "_live_overlay" not in ln]
    check(f"{_f} 无绕过 _live_overlay 的 self._overlay 读取", not _bad,
          "\n      " + "\n      ".join(_bad[:5]))

print("== 5c. 框选坐标换算收敛到 SelectionOverlay._to_raw() ==")
from PySide6.QtWidgets import QApplication as _QApp
from PySide6.QtCore import QRect as _QRect

_app = _QApp.instance() or _QApp([])
from ui.windows.selection import SelectionOverlay
from appmod.capture_mixin import CaptureMixin

_sel = SelectionOverlay(None)
_d = _sel._dpr
_union = _QRect()
for _s in _app.screens():
    _union = _union.united(_s.geometry())
print(f"   dpr={_d} 遮罩 geometry={_sel.geometry().getRect()} "
      f"屏并集={_union.getRect()} 屏数={len(_app.screens())}")
check("遮罩覆盖整个虚拟桌面（所有屏并集）",
      _sel.geometry() == _union and not _union.isEmpty(),
      f"geo={_sel.geometry().getRect()} union={_union.getRect()}")
# 核心不回归：dpr==1 且原点为 0（单屏）时，换算必须与旧算法逐值一致
if abs(_d - 1.0) < 1e-9 and _union.x() == 0 and _union.y() == 0:
    ok = True
    for r in (_QRect(0, 0, 100, 50), _QRect(13, 27, 301, 199), _QRect(7, 9, 5, 5)):
        got = _sel._to_raw(r)
        want = (int(round(r.x() * _d)), int(round(r.y() * _d)),
                int(round(r.width() * _d)), int(round(r.height() * _d)))
        if (got.left(), got.top(), got.width(), got.height()) != want:
            ok = False
            print(f"      {r.getRect()} → {got.getRect()} 期望 {want}")
    check("dpr=1/原点 0 下与旧算法逐值一致", ok)
else:
    print("   （非 1:1 环境：跳过逐值比对，仅校验映射自洽）")
# 往返自洽：out = _to_raw(r) 的 right/bottom 应能逆回 r 的逻辑边界
_r = _QRect(31, 47, 200, 150)
_o = _sel._to_raw(_r)
check("_to_raw 结果尺寸非负且为物理像素",
      _o.width() >= 0 and _o.height() >= 0)
_lx, _ly = _sel._phys_to_local(_o.left(), _o.top())
check("_phys_to_local 是 _to_phys 的逆（往返误差 ≤1px）",
      abs(_lx - (_r.x() + _sel._geo.x())) <= 1
      and abs(_ly - (_r.y() + _sel._geo.y())) <= 1,
      f"({_lx}, {_ly}) vs ({_r.x() + _sel._geo.x()}, {_r.y() + _sel._geo.y()})")
# 形状转换（capture_mixin 侧）不得再乘 dpr
_rb = CaptureMixin._raw_bbox_of(_QRect(10, 20, 30, 40))
check("_raw_bbox_of 只转形状、不乘 dpr",
      _rb == (10, 20, 40, 60), f"{_rb}")
check("capture_mixin 已无 _logical_rect_to_raw（旧换算点已删）",
      not hasattr(CaptureMixin, "_logical_rect_to_raw"))
_selection_src = (_root / "ui" / "windows" / "selection.py").read_text(
    encoding="utf-8")
check("遮罩不再固定单屏（无 primaryScreen().geometry() 直接设几何）",
      "primaryScreen().geometry()" not in _selection_src)

print("== 5d. 屏间「逻辑缝隙」不再退回主屏（多显示器跨屏修复）==")
from ui.base.widgets import screen_for_point, screen_available_geometry
check("screen_for_point 存在（永不因点在屏间缝隙返回空）",
      callable(screen_for_point))
_p_none = screen_for_point(None)
check("screen_for_point(None) 返回真实屏（不返回 None）", _p_none is not None)
# 极端：一个远离所有屏的点也必须给出某块屏（而非 None / 主屏兜底）
_far = screen_for_point(_QRect(10 ** 6, 10 ** 6, 1, 1).topLeft())
check("屏外很远的点也返回某块屏", _far is not None,
      f"{_far}")
_gy = screen_available_geometry(_sel)
check("screen_available_geometry 返回有效可用区域",
      _gy.width() > 0 and _gy.height() > 0, f"{_gy.getRect()}")
# 结构守卫：不允许再出现「screenAt 取不到 → 退回主屏」的居中/夹取写法
_cw = (_root / "ui" / "base" / "card_window.py").read_text(encoding="utf-8")
check("CardWindow._target_screen_geometry 走 screen_for_point",
      "screen_for_point(QCursor.pos())" in _cw)
_wg = (_root / "ui" / "base" / "widgets.py").read_text(encoding="utf-8")
check("screen_available_geometry 走 screen_for_point",
      "screen_for_point(widget.frameGeometry().center())" in _wg)

# 跨屏时「附属浮窗被拽回主屏」的两条修法：
# ① 状态栏/工具栏关掉 CardWindow 自动居中（它们是「居中 + 尺寸不变」症状的来源）
from ui.base.widgets import desktop_union_available
from ui.components.status_strip import StatusStrip
from ui.components.toolbar import ToolBar
from ui.base.card_window import CardWindow
check("StatusStrip 关闭自动居中", getattr(StatusStrip(), "_centered", False) is True)
check("ToolBar 关闭自动居中", getattr(ToolBar(), "_centered", False) is True)
_u = desktop_union_available()
check("desktop_union_available 返回有效并集",
      not _u.isEmpty(), f"{_u.getRect()}")
# ② 附属浮窗夹取用「虚拟桌面并集」，不再按单屏
_ro_src = (_root / "ui" / "windows" / "result_overlay.py").read_text(encoding="utf-8")
_ro_clamp = _ro_src.count("scr = self._clamp_bounds()")
check("三个附属浮窗（关闭按钮/状态栏/工具栏）夹取都用 _clamp_bounds（并集）",
      _ro_clamp == 3 and "desktop_union_available" in _ro_src,
      f"_clamp_bounds 使用次数={_ro_clamp}（应 3）")
# 关闭按钮是独立顶层窗体且创建晚于 setGeometry：必须在建窗时显式摆一次
_fm_src = (_root / "appmod" / "float_mixin.py").read_text(encoding="utf-8")
check("建窗时显式调用 _layout_close_btn（否则按钮停在默认角落）",
      "ov._layout_close_btn()" in _fm_src)
# 行为守卫：_centered=True 的 CardWindow 在 showEvent 里不得被居中
_cw2 = CardWindow()
_cw2._centered = True
_cw2.move(4242, 2424)
_cw2.showEvent(type("E", (), {})())
check("_centered=True 时 showEvent 不改动位置",
      (_cw2.x(), _cw2.y()) == (4242, 2424), f"{(_cw2.x(), _cw2.y())}")

# ③ 新窗口必须在 show() **之后**把位置摆回截图处：Qt 的 initialGeometry 会在首次
#    显示时把「中心不在任何屏上」的顶层窗口居中到主屏（跨屏时中心正落在屏间缝隙
#    里），且发生在 setGeometry 之后。**只还原、不夹取**——任何夹取/对齐都会把
#    窗口从选区位置搬走。
check("提供 _restore_overlay_geometry（显示后还原，不夹取）",
      "def _restore_overlay_geometry(ov, intend):" in _fm_src)
check("_make_overlay 记录原始几何并在 show() 后还原",
      "_intend = ov.geometry()" in _fm_src
      and "self._restore_overlay_geometry(ov, _intend)" in _fm_src)
check("还原逻辑不做夹取/居中（不得出现 min/max 对齐到屏幕）",
      "def _restore_overlay_geometry" in _fm_src
      and "a.right() - w" not in _fm_src.split("def _make_overlay")[0],
      "还原过程中出现了屏幕对齐")

print("== 1f. 异常策略统一（裸 except 不再静默吞掉故障）==")
_app_src = (_root / "app.py").read_text(encoding="utf-8")
check("退出流程不再假定 worker 有 stop()（AttributeError 不再被吞）",
      'w_stop = getattr(w, "stop", None)' in _app_src
      and "if w_stop is None:" in _app_src)
check("stop() 失败记 warning（含线程名与异常）",
      'log.warning("退出：线程 %s stop() 失败：%s", attr, e)' in _app_src)

_fg_src = (_root / "ui" / "components" / "float_group.py").read_text(encoding="utf-8")
check("float_group.follow 异常留痕（每成员只报一次，拖动不刷屏）",
      "self._follow_warned: set = set()" in _fg_src
      and 'get_logger("float_group").warning' in _fg_src
      and "if key not in self._follow_warned:" in _fg_src)

_ie_src = (_root / "appmod" / "image_encode.py").read_text(encoding="utf-8")
check("pillow_encode 失败记 warning（不再裸 except 吞细节）",
      'log.warning("Pillow 编码 %s 失败' in _ie_src
      and "# noqa: BLE001 编码失败不炸调用方\n        return None" not in _ie_src)

_as_src = (_root / "core_engine" / "autostart.py").read_text(encoding="utf-8")
check("autostart 写入失败记 warning 并返回 False",
      'log.warning("写入自启动项失败（on=%s）：%s", on, e)' in _as_src)
check("autostart 区分「未注册」与「读注册表出错」",
      "except FileNotFoundError:" in _as_src and "读取自启动项失败" in _as_src)
check("autostart 删除失败不再假装成功（只放过 FileNotFoundError）",
      "except OSError:\n                    pass" not in _as_src)

_sel_src = (_root / "ui" / "windows" / "selection.py").read_text(encoding="utf-8")
check("SelectionOverlay 不再重复构造 ConfigManager（cfg0 已消、只构造一次）",
      "cfg0" not in _sel_src
      and _sel_src.count("cfg = config or ConfigManager()") == 1)
check("配置读取移出 try（不再被伪装成抓屏失败）",
      'self._pick_hotkey = (cfg.get("hotkey_select", "color_pick", "c") or "c").lower()'
      in _sel_src)
check("逐屏抓屏失败单独保护并留痕（多屏取色前提）",
      "def _grab_screens(" in _sel_src
      and "self._screen_imgs[scr] = img" in _sel_src
      and 'log.warning("抓取屏幕底图失败（该屏取色器降级）：%s", e)' in _sel_src
      and 'log.warning("所有屏幕底图抓取失败：取色器与放大镜本次不可用")'
      in _sel_src)

_ro_src = (_root / "ui" / "windows" / "result_overlay.py").read_text(encoding="utf-8")
# 表格交互已搬进 ui/windows/overlay/table_view.py（result_overlay 拆分），
# 源码 grep 断言要跟着换文件
_tv_src = (_root / "ui" / "windows" / "overlay" / "table_view.py").read_text(
    encoding="utf-8")
check("set_boxes 不再用 try/except 包 _clear_table_sel",
      "try:\n            self._clear_table_sel()" not in _ro_src)

# ---- 关闭顺序：closed 必须在释放 img 之前发出（H2）----
# `_on_overlay_closed` 里「截图无操作也保存」要读 ov.img；若先置 None 再 emit，
# 回调拿到 None 直接 return —— 该设置静默失效（界面无任何提示）。
# 只断言顺序而非文本：谁改成别的写法都行，唯独不能把顺序改回去。
_ce = _ro_src.split("def closeEvent(")[1].split("\n    def ")[0]
check("closeEvent 里 closed.emit() 先于 self.img = None（「无操作也保存」依赖它）",
      "self.closed.emit()" in _ce and "self.img = None" in _ce
      and _ce.index("self.closed.emit()") < _ce.index("self.img = None"))
check("closeEvent 不再重复 emit closed",
      _ce.count("self.closed.emit()") == 1, f"出现 {_ce.count('self.closed.emit()')} 次")

# ---- QSS 图片路径加引号（%TEMP% 含空格/非 ASCII 用户名时 QSS 解析失败）----
_th_src = (_root / "ui" / "base" / "theme.py").read_text(encoding="utf-8")
check("QSS image:url 路径带引号（含空格路径不再静默丢图标）",
      'image: url("__CHECK__")' in _th_src
      and 'image: url("__COMBO_ARROW__")' in _th_src)
check("主题色值格式非法时给出可定位的错误（不再裸 ValueError）",
      "主题色值格式非法" in _th_src)

# ---- 写锁可重入（写套写不再自锁死）----
_db_src = (_root / "core_engine" / "db_manager.py").read_text(encoding="utf-8")
check("DBManager 写锁为 RLock（防写套写自锁）",
      "self._write_lock = threading.RLock()" in _db_src)
check("_delete_files 返回失败数（孤儿文件可被汇总上报）",
      "def _delete_files(self, screenshot_path: str) -> int:" in _db_src
      and "orphans = sum(self._delete_files(" in _db_src)

# ---- getboolean 容错（与 getint 对齐，手改配置不再崩）----
_cfg_src = (_root / "core_engine" / "config.py").read_text(encoding="utf-8")
check("getbool 捕获 ValueError 并回退默认值",
      "def getbool(" in _cfg_src
      and _cfg_src.split("def getbool(")[1].split("def ")[0].count(
          "except ValueError") == 1)

# ---- 贴图配额键名消歧（M27）----
# 旧名 max_history_windows / max_hist_windows 只差 3 个字符、语义又都带
# "history"，历次代码审查都误判成「改名遗留的重复键」。改名后**旧名只允许
# 出现在 config.py 的迁移表里**，其它地方出现即为回退。
_OLD_QUOTA_NAMES = ("max_history_windows", "max_hist_windows")
_hits = []
for _f in ([_root / "app.py", _root / "resources" / "config.ini"]
           + sorted((_root / "appmod").glob("*.py"))
           + sorted((_root / "ui").rglob("*.py"))):
    _txt = _f.read_text(encoding="utf-8")
    if any(n in _txt for n in _OLD_QUOTA_NAMES):
        _hits.append(_f.relative_to(_root).as_posix())
check("旧配额键名只剩 config.py 迁移表一处（不会再次被误判成重复键）",
      not _hits, f"仍在引用：{_hits}")
check("阈值键名自带归属：截图贴图 = max_shot_windows",
      "max_shot_windows" in _cfg_src
      and "max_shot_windows" in (_root / "appmod" / "float_mixin.py")
      .read_text(encoding="utf-8")
      and "max_shot_windows" in (_root / "ui" / "windows" / "settings_panel.py")
      .read_text(encoding="utf-8"))
check("阈值键名自带归属：历史贴图 = max_restore_windows",
      "max_restore_windows" in _cfg_src
      and "max_restore_windows" in (_root / "appmod" / "float_mixin.py")
      .read_text(encoding="utf-8")
      and "max_restore_windows" in
      (_root / "ui" / "windows" / "settings_panel.py").read_text(encoding="utf-8"))
check("旧键迁移是「新键优先」，日志不丢用户改过的值",
      "if not self.cp.has_option(new_sec, new_key):" in _cfg_src)

# ---- 缩略图缓存失效：填充与失效必须用同一个路径构造函数（M4）----
_hp_src = (_root / "ui" / "windows" / "history_panel.py").read_text(
    encoding="utf-8")
check("缩略图路径收敛到 _thumb_path（填充与失效同源）",
      "def _thumb_path(" in _hp_src
      and "dead.add(str(self._thumb_path(rec)))" in _hp_src
      and "p = self._thumb_path(r)" in _hp_src
      and 'shots / rec["screenshot_path"]' not in _hp_src)

# ---- 关闭按钮竞态（钉图信号晚于关窗）----
_pb_src = (_root / "ui" / "windows" / "overlay" / "pin_behavior.py").read_text(
    encoding="utf-8")
check("set_pinned 不再裸访问 btn_close（关窗后信号晚到会崩）",
      'if getattr(self, "btn_close", None) is not None:' in _pb_src)

check("表格选中越界保护写在真正索引处（_draw_table_sel clamp）",
      "r0, r1 = max(0, min(r0, nr - 1)), max(0, min(r1, nr - 1))" in _tv_src
      and "c0, c1 = max(0, min(c0, nc - 1)), max(0, min(c1, nc - 1))" in _tv_src)

print("== 1g. 热键键表（原 scripts/_diag_hotkey.py 的冒烟，并入守卫后该脚本已删）==")
import hotkey as _hk
from PySide6.QtCore import Qt as _Qt
from ui.base.hotkey_keys import qt_to_token as _q2t
from ui.base.hotkey_keys import split_combo as _split
from ui.base.hotkey_keys import token_to_vk as _t2v

for _combo in ("ctrl+shift+r", "ctrl+alt+s", "ctrl+`", "ctrl+shift+f5", "alt+f"):
    _mods, _vk = _hk._parse_combo(_combo)
    check(f"解析 {_combo!r} 得到有效 VK（mods={_mods}）", _vk is not None and _vk > 0)

check("反引号两个别名都指向 VK_OEM_3(0xC0)",
      _t2v("`") == 0xC0 and _t2v("~") == 0xC0)
check("反向映射把 Key_QuoteLeft 记作 `（不是别名 ~）—— 原「热键静默失效」根因",
      _q2t(_Qt.Key_QuoteLeft) == "`", f"实际={_q2t(_Qt.Key_QuoteLeft)!r}")
check("加号键用 plus 表达（+ 是分隔符，不能自身作 token）",
      _t2v("plus") == 0xBB and _split("ctrl+plus")[1] == "plus")
check("未知键名不静默成键（返回 None 交由上层告警）",
      _split("ctrl+nosuchkey")[1] is None)

print("== 1h. 浮层回显不发同值信号 / 序号编号不复用 / 钉子描边绘制顺序 ==")
from ui.base.widgets import ColorButton as _CB
from ui.base.widgets import signals_blocked as _sb

_cb = _CB((10, 20, 30))
_fired = []
_cb.colorChanged.connect(lambda c: _fired.append(tuple(c)))
with _sb(_cb):
    _cb.set_color((1, 2, 3))
check("signals_blocked：块内不回发信号，但值已写入",
      _fired == [] and _cb.color == (1, 2, 3), f"{_fired} / {_cb.color}")
_cb.set_color((4, 5, 6))
check("块外照常回发", _fired == [(4, 5, 6)], f"{_fired}")
_fired.clear()
with _sb(_cb):
    with _sb(_cb):
        _cb.set_color((7, 8, 9))
    _cb.set_color((9, 9, 9))
check("嵌套屏蔽：内层退出后外层仍生效", _fired == [], f"{_fired}")
check("退出后恢复为未屏蔽（不是无条件解屏）", _cb.signalsBlocked() is False)

from ui.components.anno.popups import _ColorSizeRow as _CSR
from ui.components.anno.popups import _FontPopup as _FP

_row = _CSR(None)
_rf = []
_row.color_changed.connect(lambda c: _rf.append(("c", tuple(c))))
_row.width_changed.connect(lambda w: _rf.append(("w", w)))
_row.set_state((11, 22, 33), 5)
check("_ColorSizeRow.set_state 回显不发 color_changed / width_changed",
      _rf == [], f"{_rf}")
check("回显确实写入了控件",
      _row.color_btn.color == (11, 22, 33) and _row.slider.value() == 5)

# 原先只挡了 slider：setChecked 会发 toggled、set_color 会发 colorChanged
_fp = _FP(None)
_ff = []
_fp.size_changed.connect(lambda s: _ff.append(("s", s)))
_fp.bold_toggled.connect(lambda b: _ff.append(("b", b)))
_fp.italic_toggled.connect(lambda b: _ff.append(("i", b)))
_fp.color_changed.connect(lambda c: _ff.append(("c", tuple(c))))
_fp.set_state(24, True, True, (44, 55, 66))
check("_FontPopup.set_state 回显不发 size/bold/italic/color", _ff == [], f"{_ff}")
check("回显确实写入了控件",
      _fp.btn_bold.isChecked() and _fp.btn_italic.isChecked()
      and _fp.color_btn.color == (44, 55, 66) and _fp.size_slider.value() == 24)

# 序号编号：原先 = 起始序号 + 已有序号数，删中间再放会与已存在的编号重复
from PySide6.QtCore import QPointF as _QPF
from ui.components.annotation import AnnotationSession as _AS


class _AnnoStub:
    def __init__(self, start=1):
        self.data = []
        self.num_start = start
        self.num_style = "circle"
        self.color = (255, 95, 95)
        self.num_size = 18
        self.font_family = "Arial"

    def _push_cmd(self, *_a):
        pass

    def _render_all(self):
        pass


_nst = _AnnoStub()
for _ in range(3):
    _AS._place_num(_nst, _QPF(0, 0))
check("连续放置序号得到 1/2/3", [d["num"] for d in _nst.data] == [1, 2, 3],
      f"{[d['num'] for d in _nst.data]}")
_nst.data.pop(1)                       # 删掉中间那个（用户改错位置就删了重放）
_AS._place_num(_nst, _QPF(0, 0))
_nums = [d["num"] for d in _nst.data]
check("删掉中间序号后再放置**不复用**旧编号", len(set(_nums)) == len(_nums),
      f"编号集合={_nums}（原 bug 会得到重复的 3）")
check("新编号 = 现有最大序号 + 1", max(_nums) == 4, f"{_nums}")
_nst.num_start = 10                    # 抬高起始序号但不重排
_AS._place_num(_nst, _QPF(0, 0))
check("新编号不低于起始序号（保留「最低起始序号」语义）",
      max(d["num"] for d in _nst.data) == 10, f"{[d['num'] for d in _nst.data]}")
_nst2 = _AnnoStub(start=7)
_AS._place_num(_nst2, _QPF(0, 0))
check("没有任何序号时取起始序号", _nst2.data[0]["num"] == 7, f"{_nst2.data[0]['num']}")

# 钉子描边：原先先自绘、后 super()，QSS 背景把描边整圈盖掉
_pd_src = (_root / "ui" / "components" / "pin_dot.py").read_text(encoding="utf-8")
_pd_body = _pd_src.split("def paintEvent")[1]
check("钉子描边在 super().paintEvent 之后绘制（否则被 QSS 背景盖掉）",
      _pd_body.index("super().paintEvent") < _pd_body.index("QPainter(self)"))

# 快捷键一览：每次点都新建面板，旧实例（独立顶层窗）没人关
import ui.components.shortcut_panel as _sp_mod
from ui.windows.settings.hotkey_tab import HotkeyTabMixin as _HTM

_closed = []


class _FakePanel:
    def __init__(self, cfg=None):
        self.cfg = cfg

    def show(self):
        pass

    def raise_(self):
        pass

    def close(self):
        _closed.append(id(self))

    def deleteLater(self):
        pass


_orig_panel = _sp_mod.ShortcutPanel
_sp_mod.ShortcutPanel = _FakePanel
try:
    _htab = _AnnoStub()          # 只是个能挂属性的壳
    _htab._cfg = None
    _HTM._open_shortcut_panel(_htab)
    _first = _htab._shortcut_panel
    _HTM._open_shortcut_panel(_htab)
    _second = _htab._shortcut_panel
finally:
    _sp_mod.ShortcutPanel = _orig_panel
check("再次打开「操作一览」会先关掉旧面板（不再留下没人管的顶层窗）",
      _first is not _second and id(_first) in _closed,
      f"closed={_closed} first={id(_first)} second={id(_second)}")

# 就地文字编辑：contentsChanged 的连接必须显式断开，否则包装器引用数不归零、
# C++ 对象不析构（scene.removeItem 只解除场景归属），每编辑一次泄漏一组
import gc as _gc
import weakref as _wr
from PySide6.QtWidgets import QGraphicsScene as _GScene


class _EditStub:
    def __init__(self):
        self.scene = _GScene()
        self.data = []
        self.color = (255, 95, 95)
        self.font_family = "Arial"
        self.font_size = 18
        self.font_bold = False
        self.font_italic = False
        self._text_edit = None
        self._text_edit_box = None
        self._editing_index = None
        self._hidden_edit_items = []

    def _render_all(self):
        pass

    def _push_cmd(self, *_a):
        pass

    def _sync_text_box(self, *_a):
        pass


for _name in ("_start_text_edit", "cancel_text_edit", "_commit_text",
              "_on_edit_text_changed"):
    setattr(_EditStub, _name, getattr(_AS, _name))

_es = _EditStub()
_AS._start_text_edit(_es, _QPF(0, 0), width=120)
check("进入文本框编辑：建好边框，且槽是绑定方法（不是捕获 item 的闭包）",
      _es._text_edit is not None and _es._text_edit_box is not None)
_es._text_edit.setPlainText("abc")     # 编辑态下改内容不应抛（边框跟随回调被调用）
_w = _wr.ref(_es._text_edit)
_AS.cancel_text_edit(_es)
_gc.collect()
check("取消编辑后编辑项被析构（绑定方法槽不构成跨语言引用环）", _w() is None,
      "wrapper 仍存活 → 每编辑一次泄漏一组 item/document/边框")

_es2 = _EditStub()
_AS._start_text_edit(_es2, _QPF(0, 0), width=120)
_es2._text_edit.setPlainText("hi")
_w2 = _wr.ref(_es2._text_edit)
_AS._commit_text(_es2)
_gc.collect()
check("提交编辑后编辑项被析构（同一收尾路径）", _w2() is None)
check("提交后内容已落进 data", _es2.data and _es2.data[0]["text"] == "hi",
      f"{_es2.data}")

print("== 1i. DBManager：每线程长连接 + 读写分离 ==")
with tempfile.TemporaryDirectory() as td:
    dbd = _open_db(Path(td) / "conn.db")
    dbd.init_db()
    dbd.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    dbd.execute("INSERT INTO t (v) VALUES ('a')")

    _c1 = dbd._conn()
    check("同一线程复用同一连接（不再每操作新建）", dbd._conn() is _c1)
    dbd.close()
    check("close() 后重取会新建连接（可重建，幂等）", dbd._conn() is not _c1)
    check("close() 幂等（再调不抛）",
          (dbd.close(), dbd.close(), True)[-1])

    # 读不加写锁：占住写锁后，另一个线程的读仍应立刻返回
    _got: dict = {}

    def _reader():
        _got["rows"] = dbd.query("SELECT COUNT(*) AS c FROM t")
        dbd.close()               # 线程用完显式关自己的连接（Windows 下句柄才释放）

    dbd._write_lock.acquire()
    try:
        _th = threading.Thread(target=_reader)
        _th.start()
        _th.join(timeout=3)
        check("写锁被占用时读不被阻塞（WAL 读写分离，原实现是全串行）",
              not _th.is_alive() and bool(_got.get("rows")), f"rows={_got}")
    finally:
        dbd._write_lock.release()

    # 长连接下的失败路径：必须回滚，否则同线程后续读会看到未提交数据
    try:
        dbd.execute("INSERT INTO nope (x) VALUES (1)")
        _raised = False
    except Exception:                    # noqa: BLE001 就是要它抛
        _raised = True
    check("写失败会抛出（不吞异常）", _raised)
    check("写失败后连接没有卡在事务里（已回滚）",
          not dbd._conn().in_transaction)
    check("写失败后仍能正常写", dbd.execute("UPDATE t SET v='b'") == 1)
    check("写失败不留半截数据", dbd.query_one("SELECT COUNT(*) AS c FROM t")["c"] == 1)

    _ids: dict = {}

    def _worker():
        _ids["id"] = id(dbd._conn())
        _ids["ok"] = dbd.query_one("SELECT COUNT(*) AS c FROM t")["c"]
        dbd.close()

    _tw = threading.Thread(target=_worker)
    _tw.start()
    _tw.join(timeout=3)
    check("跨线程各持一个连接（thread-local）",
          _ids.get("id") != id(dbd._conn()), f"worker={_ids.get('id')}")
    check("工作线程的读正常（各自连接互不干扰）", _ids.get("ok") == 1)

print("== 1j. i18n：T() 缺 key 留痕，加载失败不误报 ==")
import logging as _lg

import ui.base.texts as _txt

_records: list = []


class _CapHandler(_lg.Handler):
    def emit(self, r):
        _records.append(r)


_h = _CapHandler()
_txt.log.addHandler(_h)
try:
    _v1 = _txt.T("__nope__.a", "默认甲")
    _txt.T("__nope__.a", "默认甲")          # 同一 key 第二次不该再报
    _v3 = _txt.T("__nope__.b")
    _msgs = [r.getMessage() for r in _records]
    check("缺 key 返回代码默认值（不抛）", _v1 == "默认甲")
    check("缺 key 只告警一次（渲染循环里不刷屏）",
          sum(1 for m in _msgs if "__nope__.a" in m) == 1, f"{_msgs}")
    check("未给默认值的缺 key 也留痕",
          any("__nope__.b" in m for m in _msgs), f"{_msgs}")
    check("连默认值都没给时返回 key 本身", _v3 == "__nope__.b")

    # 文字表整体加载失败（文件不存在）→ 属全局回退，不再逐 key 告警
    _before = len(_records)
    _txt.load_ui_text("__no_such_ui_text__.json")
    _txt.T("whatever.key", "兜底")
    _after = [r.getMessage() for r in _records[_before:]]
    check("加载失败只报「加载失败」一条，不逐 key 误报",
          len(_after) == 1 and "加载失败" in _after[0], f"{_after}")
finally:
    _txt.log.removeHandler(_h)
_txt.load_ui_text()                          # 恢复真实文字表，别影响后续区块
check("恢复真实文字表后取值正常", _txt.T("btn.close", "关闭") == "关闭")

print("== 5f. result_overlay 拆分：方法归属断言（防 MRO 静默遮蔽）==")
from ui.windows.overlay.anno_session import AnnoMixin as _AM
from ui.windows.overlay.pin_behavior import PinMixin as _PB
from ui.windows.overlay.table_view import TableViewMixin as _TV
from ui.windows.overlay.text_select import TextSelectMixin as _TS
from ui.windows.overlay.zoom_nav import ZoomNavMixin as _ZN
from ui.windows.result_overlay import ResultOverlay as _RO

_EXPECT = {
    _TV: ["_toggle_table", "_handle_toolbar_key", "_on_table_mode",
          "_apply_table_view", "_build_table_grid", "_build_table_boxes",
          "_show_table_boxes", "_hide_table_boxes", "_table_hit_cell",
          "_table_press", "_table_move", "_table_release",
          "_draw_table_sel", "_clear_table_sel", "_copy_table_rect"],
    _PB: ["_ensure_pin_dot", "set_pinned", "_on_pin_clicked", "_on_pin_double",
          "_on_pin_drag", "_on_pin_hover_in", "_on_pin_hover_out",
          "_show_pin_card", "_hide_pin_card", "_close_sub_windows"],
    _ZN: ["_apply_transform", "_set_image_zoom", "_set_zoom", "fit_to_screen",
          "_toggle_navigator", "_toggle_ratio_lock", "_set_ratio_lock",
          "set_navigator", "_ensure_navigator", "_layout_navigator",
          "_update_navigator", "_on_nav_jump", "_edge_at",
          "_start_edge_resize", "_do_edge_resize", "_end_edge_resize",
          "_sb_edge", "_recontain"],
    _TS: ["_build_geom", "_char_intervals", "_caret", "_hit", "_hit_or_clamp",
          "_norm", "get_selected_text", "select_all", "_preview_selection",
          "_refresh_preview"],
    _AM: ["_toggle_annotation", "_on_anno_btn_toggled", "_enter_annotation",
          "_save_mosaic_mode", "_on_anno_color", "_on_anno_width",
          "_on_num_start", "_on_num_size", "_on_num_style", "_on_tool_style",
          "_switch_tool_by_key", "handle_anno_key", "_save_anno_param",
          "_exit_annotation", "_anno_undo", "_anno_redo", "_anno_clear",
          "load_annotations"],
}
_MIXINS = [_TV, _PB, _ZN, _TS, _AM]

# 1) 每个方法确实定义在预期类上
_bad_owner = [(n, cls.__name__) for cls, names in _EXPECT.items() for n in names
              if n not in cls.__dict__]
check("71 个方法都定义在预期 mixin 上", not _bad_owner, f"缺失/错位：{_bad_owner}")

# 2) ResultOverlay 通过 MRO 取到的就是预期类的那一个（**没被别的类静默遮蔽**）
_shadowed = []
for _cls, _names in _EXPECT.items():
    for _n in _names:
        _owner = next((k for k in _RO.__mro__ if _n in k.__dict__), None)
        if _owner is not _cls:
            _shadowed.append(f"{_n}: 期望 {_cls.__name__}，实际 "
                             f"{getattr(_owner, '__name__', None)}")
check("ResultOverlay 解析出的方法来自预期 mixin（无 MRO 遮蔽）",
      not _shadowed, "\n      " + "\n      ".join(_shadowed))

# 3) 五个 mixin 之间不得有同名方法（MRO 遮蔽的根源）
_all = [n for cls in _MIXINS for n in cls.__dict__ if not n.startswith("__")]
_dupes = sorted({n for n in _all if _all.count(n) > 1})
check("五个 mixin 之间无同名方法（遮蔽的根源）", not _dupes, f"{_dupes}")

# 4) 搬走的方法不得在 result_overlay.py 里再留一份定义
_ro_src2 = (_root / "ui" / "windows" / "result_overlay.py").read_text(encoding="utf-8")
_left = [n for cls, names in _EXPECT.items() for n in names
         if f"def {n}(" in _ro_src2]
check("result_overlay.py 已无搬走方法的重复定义", not _left, f"{_left}")

# 5) 随组搬走的模块级助手/常量也要能在新模块里取到（否则运行期 NameError）
import ui.windows.overlay.text_select as _ts_mod
check("字符度量助手随组搬移（_font_and_metrics / _char_advance / 缓存）",
      all(hasattr(_ts_mod, _x) for _x in
          ("_font_and_metrics", "_char_advance", "_FONT_CACHE", "_FM_CACHE",
           "_CHAR_ADV_CACHE", "_CHAR_ADV_CACHE_MAX")))

print("== 5. drop_mixin/annotation 等导入冒烟 ==")
import appmod.drop_mixin, appmod.engine_mixin, appmod.history_mixin  # noqa
import appmod.float_mixin  # noqa
check("appmod 模块导入正常", True)

print("== 6. 第 13 批：单实例提前 / Problem 标记 / 表格双检锁 / 热键线程可停 ==")
import ast
import time

# ---- 6a. app.py：单实例检测提前，且探测 socket 显式关闭 ----
_app_src = (_root / "app.py").read_text(encoding="utf-8")
_i_main = _app_src.index("def __init__(self, app_root=None):")
_i_check = _app_src.index("self._check_single()", _i_main)
_i_tray = _app_src.index("self.tray = self._build_tray()", _i_main)
_i_theme = _app_src.index("apply_theme(self.app, resolve_theme(self.config))", _i_main)
check("单实例检测早于主题初始化", _i_check < _i_theme,
      f"check={_i_check} theme={_i_theme}")
check("单实例检测早于托盘构建（第二实例不再白建一遍）", _i_check < _i_tray,
      f"check={_i_check} tray={_i_tray}")

import app as _app_mod
from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalServer

_qcore = QCoreApplication.instance() or QCoreApplication([])


class _InstShell:
    """只装单实例检测所需属性的轻壳：不跑 App.__init__（那会建托盘/引擎/DB）。"""
    INSTANCE_NAME = "ClickOCR-Guard-SingleInstance"
    INSTANCE_PING = b"show"
    _check_single = _app_mod.App._check_single

    def __init__(self):
        self.is_second = False
        self._single_failed = None
        self.server = None

    def _on_second(self):
        pass


# 探测用 socket 必须显式 close()：它是无父临时对象，不关就等到 GC 才释放句柄。
# 用记录子类观测真实调用（不靠读源码猜）。
_closed = []


class _RecSock(_app_mod.QLocalSocket):
    def close(self):
        _closed.append(1)
        return super().close()


_orig_sock_cls = _app_mod.QLocalSocket
_app_mod.QLocalSocket = _RecSock
QLocalServer.removeServer(_InstShell.INSTANCE_NAME)
try:
    _first = _InstShell()
    _first._check_single()
    # 先取值再收尾：finally 里会 close() 掉 server，之后 isListening() 必为 False
    _first_ok = (_first.is_second is False and _first.server is not None
                 and _first.server.isListening())
    _second = _InstShell()
    _second._check_single()
    _second_is_second = _second.is_second
finally:
    _app_mod.QLocalSocket = _orig_sock_cls
    if _first.server is not None:
        _first.server.close()
    QLocalServer.removeServer(_InstShell.INSTANCE_NAME)

check("首个实例：is_second=False 且成功监听", _first_ok,
      f"is_second={_first.is_second} server={_first.server} "
      f"failed={_first._single_failed}")
check("第二实例：is_second=True（单实例检测仍生效）", _second_is_second is True)
check("两实例都显式 close() 了探测 socket", len(_closed) == 2, f"closed={len(_closed)}")

# ---- 6b. settings：问题项改由产生处标记，不再按文案关键词反推 ----
import ui.windows.settings_panel as _sp_mod
from ui.windows.settings.widgets import Problem as _Problem
from ui.windows.settings.hotkey_tab import HotkeyTabMixin as _HkTab

check("Problem 是 str 子类（下游 join/format/托盘气泡不受影响）",
      issubclass(_Problem, str))

_sp_src = (_root / "ui" / "windows" / "settings_panel.py").read_text(encoding="utf-8")
check("_save 用 isinstance(m, Problem) 挑问题项",
      "isinstance(m, Problem)" in _sp_src)
check("已无「按文案关键词反推问题项」的旧写法",
      "for kw in" not in _sp_src and 'any(k in' not in _sp_src)

_hk_src = (_root / "ui" / "windows" / "settings" / "hotkey_tab.py").read_text(encoding="utf-8")
check("hotkey_tab 的 4 处问题项消息均已打 Problem 标记",
      _hk_src.count("Problem(") >= 4, f"count={_hk_src.count('Problem(')}")
check("hotkey_tab 仍导入 T（上一轮误删导致模块不可用）",
      "from ui.base.texts import T" in _hk_src)


class _CpStub:
    def __init__(self):
        self.written = {}

    def set(self, sec, key, val):
        self.written[(sec, key)] = val


class _CfgStub:
    def __init__(self):
        self.cp = _CpStub()


class _HkStub:
    def __init__(self, ok):
        self._ok = ok
        self.calls = []

    def set_secondary_combo(self, hid, combo):
        self.calls.append((hid, combo))
        return self._ok


class _HkShell:
    """只装 _secondary_apply 所需属性的轻壳。"""
    _secondary_apply = _HkTab._secondary_apply

    def __init__(self, hotkey):
        self._hotkey = hotkey
        self._cfg = _CfgStub()


_shell = _HkShell(None)
_mk = _shell._secondary_apply(2, "hotkey_history", "screenshot_combo", "历史",
                              hint="ctrl+shift+a")
_m_bad = _mk("ctrl+")                       # 格式无效
check("格式无效消息是 Problem（会被弹窗报出）",
      len(_m_bad) == 1 and isinstance(_m_bad[0], _Problem), f"{_m_bad}")
_m_off = _mk("off")                         # 正常项
check("正常项（off）不是 Problem（不误报）",
      len(_m_off) == 1 and not isinstance(_m_off[0], _Problem), f"{_m_off}")

_fail_hk = _HkStub(False)
_fail_shell = _HkShell(_fail_hk)
_m_fail = _fail_shell._secondary_apply(2, "hotkey_history", "screenshot_combo",
                                       "历史")("ctrl+shift+a")
check("注册失败消息是 Problem",
      len(_m_fail) == 1 and isinstance(_m_fail[0], _Problem), f"{_m_fail}")
check("注册失败不写 config（否则重启后静默丢热键）",
      _fail_shell._cfg.cp.written == {}, f"{_fail_shell._cfg.cp.written}")

_ok_shell = _HkShell(_HkStub(True))
_m_ok = _ok_shell._secondary_apply(2, "hotkey_history", "screenshot_combo",
                                   "历史")("ctrl+shift+a")
check("注册成功消息不是 Problem，且写入了 config",
      len(_m_ok) == 1 and not isinstance(_m_ok[0], _Problem)
      and _ok_shell._cfg.cp.written ==
      {("hotkey_history", "screenshot_combo"): "ctrl+shift+a"},
      f"{_m_ok} {_ok_shell._cfg.cp.written}")

# Problem 与 str 混排后 join/format 行为不变（弹窗文案）
_blob = "\n".join(f"· {p}" for p in [_m_bad[0], _m_off[0], _m_fail[0]])
check("Problem 与普通消息混排可正常 join/format",
      _blob.startswith("· ") and "历史" in _blob and "ctrl+shift+a" in _blob,
      _blob[:60])

# ---- 6c. table_engine：惰性加载双检锁（并发只建一套 session） ----
from core_engine import table_engine as _te_mod

_built = []


class _FakeRT:
    def __init__(self, cfg=None):
        _built.append(1)
        time.sleep(0.05)                    # 放大竞态窗口：无锁时必然建出多套


_orig = (_te_mod.RapidTable, _te_mod.RapidTableInput, _te_mod.ModelType,
         _te_mod._RT_AVAILABLE)
_te_mod.RapidTable = _FakeRT
_te_mod.RapidTableInput = lambda **kw: kw
_te_mod.ModelType = lambda v: v
_te_mod._RT_AVAILABLE = True
_te_eng = _te_mod.TableEngine(ocr_recognize=None, model_type="slanet_plus")
try:
    _ts = [threading.Thread(target=_te_eng.load_table_model) for _ in range(8)]
    [_t.start() for _t in _ts]
    [_t.join() for _t in _ts]
finally:
    (_te_mod.RapidTable, _te_mod.RapidTableInput, _te_mod.ModelType,
     _te_mod._RT_AVAILABLE) = _orig
check("并发 load_table_model 只构建一次（双检锁）", len(_built) == 1,
      f"built={len(_built)}")
check("加载后 is_loaded=True", _te_eng.is_loaded is True)

# unload 持同一把锁：加载/卸载交错不得死锁，卸载后能再次加载
_te_eng.unload_table_model()
check("unload 后 is_loaded=False", _te_eng.is_loaded is False)
_te_mod.RapidTable = _FakeRT
_te_mod.RapidTableInput = lambda **kw: kw
_te_mod.ModelType = lambda v: v
_te_mod._RT_AVAILABLE = True
try:
    _te_eng.load_table_model()
finally:
    (_te_mod.RapidTable, _te_mod.RapidTableInput, _te_mod.ModelType,
     _te_mod._RT_AVAILABLE) = _orig
check("卸载后可再次加载（同一把锁未死锁）", _te_eng.is_loaded is True
      and len(_built) == 2, f"built={len(_built)}")

# ---- 6d. dev_window：热键线程可停，且窗口关闭时确实调用了 ----
_dw_tree = ast.parse((_root / "ui" / "windows" / "dev_window.py").read_text(encoding="utf-8"))


def _cls(tree, name):
    return next((n for n in tree.body
                 if isinstance(n, ast.ClassDef) and n.name == name), None)


_hkt = _cls(_dw_tree, "HotkeyThread")
_hkt_methods = {n.name for n in _hkt.body if isinstance(n, ast.FunctionDef)}
check("HotkeyThread 提供 stop()（原先无停止路径，钩子线程会累积）",
      "stop" in _hkt_methods, f"{sorted(_hkt_methods)}")
_hkt_stop_src = ast.unparse(next(n for n in _hkt.body
                                 if isinstance(n, ast.FunctionDef) and n.name == "stop"))
check("stop() 会停监听并等待线程收尾",
      "listener" in _hkt_stop_src and ".stop()" in _hkt_stop_src
      and "wait(" in _hkt_stop_src, _hkt_stop_src[:80])

_mw = _cls(_dw_tree, "MainWindow")
_ce = next((n for n in _mw.body
            if isinstance(n, ast.FunctionDef) and n.name == "closeEvent"), None)
check("MainWindow 实现 closeEvent", _ce is not None)
_ce_src = ast.unparse(_ce) if _ce is not None else ""
check("closeEvent 调用 hk.stop()", "hk.stop()" in _ce_src, _ce_src[:80])

print("== 7. 第 14 批：扩展加载约束 / 钉图缓冲 / GC 限频 / 探测导入与新鲜度 ==")
import gc as _gc
import json as _json
import types as _types

# ---- 7a. db_manager：sqlite-vec 扩展的来源约束 + 开关复原 ----
_db_src = (_root / "core_engine" / "db_manager.py").read_text(encoding="utf-8")
check("load_extension 后立刻 enable_load_extension(False) 复原",
      "enable_load_extension(False)" in _db_src)
check("加载前校验扩展位于 sqlite_vec 包目录内",
      "pkg not in real.parents" in _db_src)
check("按平台后缀真实文件判定存在性（基名 is_file() 恒为 False）",
      'glob(ext.name + ".*")' in _db_src)

with tempfile.TemporaryDirectory() as _td:
    # 伪造一个 sqlite_vec 包：loadable_path() 指向**包外**文件 ——
    # 真实包一定是把扩展放在自己目录里，包外路径就是「加载任意 DLL」。
    _fake_pkg = Path(_td) / "sqlite_vec"
    _fake_pkg.mkdir()
    (_fake_pkg / "__init__.py").write_text("", encoding="utf-8")
    _outside = Path(_td) / "evil.dll"
    _outside.write_bytes(b"not a real dll")
    _fake_mod = _types.ModuleType("sqlite_vec")
    _fake_mod.__file__ = str(_fake_pkg / "__init__.py")
    _fake_mod.loadable_path = lambda: str(_outside)
    _saved_mod = sys.modules.get("sqlite_vec")
    sys.modules["sqlite_vec"] = _fake_mod
    _db2 = None
    try:
        _db2 = _open_db(Path(_td) / "vec.db")
        _db2.init_db()
        check("包外的扩展文件被拒绝加载（vec0 不可用）",
              _db2.vec0_available is False)
    finally:
        if _db2 is not None:
            _db2.close()
        if _saved_mod is None:
            sys.modules.pop("sqlite_vec", None)
        else:
            sys.modules["sqlite_vec"] = _saved_mod
    # 正向回归：真实 sqlite_vec 必须仍被正常加载。收紧来源校验时极易误伤
    # 「正常安装」—— 第一版就对 `loadable_path()` 返回的**不带平台后缀**的
    # 基名做了 is_file()（恒为 False），直接把可用功能判成不可用。
    if _saved_mod is not None:
        _db3 = _open_db(Path(_td) / "real.db")
        _db3.init_db()
        check("真实安装的 sqlite_vec 仍被正常加载（未被收紧误伤）",
              _db3.vec0_available is True)
        _db3.close()

# ---- 7b. pin_card：QImage 显式 copy + 失败有日志 ----
import numpy as _np
from ui.components.pin_card import PinCard as _PinCard

_pc_src = (_root / "ui" / "components" / "pin_card.py").read_text(encoding="utf-8")
check("QImage 显式 copy()（不再借用局部 numpy 缓冲）",
      "QImage.Format_RGB888).copy()" in _pc_src)
check("缩略图失败有日志（不再 except 全吞）", "钉图缩略图生成失败" in _pc_src)

_card = _PinCard()
_card.set_image(_np.zeros((40, 80, 3), dtype=_np.uint8))
_pm_ok = _card.thumb.pixmap()
check("set_image 正常路径：生成非空 pixmap",
      _pm_ok is not None and not _pm_ok.isNull())
_card.deleteLater()

# 失败分支用**全新**卡片：不复用刚设过 pixmap 的那个，否则断言会与
# 「QLabel.setText 是否顺带清掉旧 pixmap」这个无关细节绑在一起
_card_bad = _PinCard()
_card_bad.set_image(_np.zeros(5, dtype=_np.uint8))   # 形状非法 → 失败分支
_pm_bad = _card_bad.thumb.pixmap()
check("set_image 非法输入不抛、退回占位文字",
      (_pm_bad is None or _pm_bad.isNull()) and bool(_card_bad.thumb.text()))
_card_bad.deleteLater()

# ---- 7c. float_mixin：关窗后的全量 GC 限频 ----
from appmod.float_mixin import FloatMixin as _FM


class _OvStub:
    def __init__(self):
        self._bound_signals = []

    def deleteLater(self):
        pass


class _FmShell:
    _dispose_overlay = _FM._dispose_overlay
    _GC_MIN_INTERVAL = _FM._GC_MIN_INTERVAL

    def __init__(self):
        self._last_gc_at = 0.0


_orig_gc_collect = _gc.collect
_gc_hits: list = []
_gc.collect = lambda *a, **k: (_gc_hits.append(1), 0)[1]
try:
    _fm = _FmShell()
    for _ in range(3):                     # 连续关三个贴图窗口
        _fm._dispose_overlay(_OvStub())
finally:
    _gc.collect = _orig_gc_collect
check("连续关窗只触发一次全量 GC（限频生效）", len(_gc_hits) == 1,
      f"calls={len(_gc_hits)}")

_gc_hits2: list = []
_gc.collect = lambda *a, **k: (_gc_hits2.append(1), 0)[1]
try:
    _fm._last_gc_at -= _FmShell._GC_MIN_INTERVAL + 1.0    # 模拟间隔已过
    _fm._dispose_overlay(_OvStub())
finally:
    _gc.collect = _orig_gc_collect
check("超过间隔后恢复收集（限频不是取消）", len(_gc_hits2) == 1,
      f"calls={len(_gc_hits2)}")

# ---- 7d. software_tab：探测导入移进 try + 检测结果新鲜度 ----
import ui.windows.settings.software_tab as _st_mod

_st_tree = ast.parse(
    (_root / "ui" / "windows" / "settings" / "software_tab.py").read_text(encoding="utf-8"))
_probe_fn = next(n for n in ast.walk(_st_tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_run_gpu_probe")
_rt_imports = [n for n in ast.walk(_probe_fn)
               if isinstance(n, ast.ImportFrom) and n.module == "ocr.rapid_engine"]
check("_run_gpu_probe 里确有 rapid_engine 导入", len(_rt_imports) >= 2,
      f"count={len(_rt_imports)}")
_try_spans = [(t.lineno, t.end_lineno) for t in ast.walk(_probe_fn)
              if isinstance(t, ast.Try)]
_bare_imports = [i.lineno for i in _rt_imports
                 if not any(a <= i.lineno <= b for a, b in _try_spans)]
check("rapid_engine 的导入全部位于 try 内（回调不再抛未捕获异常）",
      not _bare_imports, f"裸露导入行号={_bare_imports}")

_mix = _st_mod.SoftwareTabMixin
check("时间戳缺失/无法解析 → 判为过期（返回 None）",
      _mix._probe_age_days({}) is None
      and _mix._probe_age_days({"time": ""}) is None
      and _mix._probe_age_days({"time": "不是时间"}) is None)
check("当前时间戳判为新鲜（0 ≤ 天数 < 1）",
      0 <= _mix._probe_age_days(
          {"time": time.strftime("%Y-%m-%d %H:%M:%S")}) < 1)
check("年代久远的时间戳远超 TTL 天数",
      _mix._probe_age_days({"time": "2020-01-01 10:00:00"})
      > _mix._PROBE_TTL_DAYS)


class _CfgGetStub:
    def __init__(self, raw):
        self._raw = raw

    def get(self, sec, key, default=""):
        return self._raw


class _ProbeShell:
    _cfg_get_probe = _mix._cfg_get_probe
    # 必须保住 staticmethod：直接赋函数会被当成实例方法，多传一个 self
    _probe_age_days = staticmethod(_mix._probe_age_days)
    _PROBE_TTL_DAYS = _mix._PROBE_TTL_DAYS

    def __init__(self, raw):
        self._cfg = _CfgGetStub(raw)


_OLD_JSON = _json.dumps({"time": "2020-01-01 10:00:00",
                         "devices": [{"ok": True, "speedup": 9.9}]})
_NEW_JSON = _json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "devices": [{"ok": True, "speedup": 2.1}]})
check("过期检测结果不再被消费（返回空 dict）",
      _ProbeShell(_OLD_JSON)._cfg_get_probe("cuda") == {})
check("新鲜检测结果正常返回（倍率仍展示）",
      bool(_ProbeShell(_NEW_JSON)._cfg_get_probe("cuda").get("devices")))
check("损坏 JSON / 非 dict 仍返回空 dict（不抛）",
      _ProbeShell("{oops")._cfg_get_probe("dml") == {}
      and _ProbeShell("[1,2]")._cfg_get_probe("dml") == {})

# ---- 7e. rapid_engine：DML 探测的 first_ok 提前返回 ----
import ocr.rapid_engine as _re_mod

_re_src = (_root / "ocr" / "rapid_engine.py").read_text(encoding="utf-8")
check("设备数不再硬编码：range(8) 已收敛为常量",
      "range(8)" not in _re_src and "range(_DML_MAX_DEVICES)" in _re_src)
check("_detect_dml_device 走 first_ok 路径（不再为拿一个索引全量探测）",
      "first_ok=True" in _re_src)


class _FakeSess:
    built: list = []

    def __init__(self, path, providers=None):
        _FakeSess.built.append(providers[0][1].get("device_id"))

    def get_providers(self):
        return ["DmlExecutionProvider", "CPUExecutionProvider"]


_fake_ort = _types.ModuleType("onnxruntime")
_fake_ort.get_available_providers = lambda: ["DmlExecutionProvider",
                                             "CPUExecutionProvider"]
_fake_ort.InferenceSession = _FakeSess


def _fake_infer(model_path, providers, sess=None):
    """CPU 基准 20ms / DML 10ms → speedup 恒为 2.0，便于断言。"""
    return 10.0 if sess is not None else 20.0


with tempfile.TemporaryDirectory() as _td:
    _det_file = Path(_td) / "det.onnx"
    _det_file.write_bytes(b"x")
    _saved_rt = (sys.modules.get("onnxruntime"), _re_mod._NEW, _re_mod._DIR_V6,
                 _re_mod._V6_MODELS, _re_mod._probe_infer_ms, _re_mod._DML_DEVICE_CACHE)
    sys.modules["onnxruntime"] = _fake_ort
    _re_mod._NEW = True
    _re_mod._DIR_V6 = Path(_td)
    _re_mod._V6_MODELS = {"v6-tiny": (None, "det.onnx", "rec.onnx", "d.txt")}
    _re_mod._probe_infer_ms = _fake_infer
    _re_mod._DML_DEVICE_CACHE = None
    try:
        _FakeSess.built = []
        _all = _re_mod.probe_dml_devices(_det_file)
        _n_all = len(_FakeSess.built)
        _FakeSess.built = []
        _first = _re_mod.probe_dml_devices(_det_file, first_ok=True)
        _n_first = len(_FakeSess.built)
        _cache_before = _re_mod._DML_DEVICE_CACHE
        _detected = _re_mod._detect_dml_device(_det_file)
    finally:
        (sys.modules["onnxruntime"], _re_mod._NEW, _re_mod._DIR_V6,
         _re_mod._V6_MODELS, _re_mod._probe_infer_ms,
         _re_mod._DML_DEVICE_CACHE) = _saved_rt
        if _saved_rt[0] is None:
            sys.modules.pop("onnxruntime", None)

check("默认全量探测仍遍历全部设备索引",
      _n_all == _re_mod._DML_MAX_DEVICES, f"built={_n_all}")
check("first_ok=True 只建一个 session 就返回（不再白探 7 个）",
      _n_first == 1, f"built={_n_first}")
check("first_ok 返回的可用设备与倍率完整",
      bool(_first) and _first[0]["ok"] is True
      and _first[0]["speedup"] == 2.0, f"{_first[:1]}")
check("first_ok 取到的设备与全量探测的第一个可用设备一致",
      _first[0]["id"] == next(r["id"] for r in _all if r.get("ok")))
check("_detect_dml_device 经 first_ok 拿到正确的设备索引",
      _cache_before is None and _detected == _first[0]["id"],
      f"detected={_detected}")

print("== 8. 第 15 批：UI 线程阻塞统一下沉（保存/探测/清空/刷新/遮罩）==")

# ---- 8a. save_queue：落库真的在后台，续作真的在「id 就绪后」跑 ----
from appmod.save_queue import SaveQueue as _SaveQueue

check("HistorySaveWorker 常驻 FIFO（不是每次新建线程）",
      "class HistorySaveWorker" in
      (_root / "appmod" / "workers.py").read_text(encoding="utf-8"))

_hs_src = (_root / "appmod" / "history_mixin.py").read_text(encoding="utf-8")
check("_save_record 不再直接调 self.history.save（改由后台线程落库）",
      "self.history.save(" not in _hs_src)
check("三处「保存后接着做」的调用方都改用了 on_saved 续作",
      _hs_src.count("on_saved=") >= 3, f"count={_hs_src.count('on_saved=')}")


class _OvStub:
    """最小贴图窗口壳：只带 _save_record 需要的字段（不建真 Qt 窗口）。"""

    def __init__(self, img):
        self.img = img
        self.boxes = []
        self._ocr_done = False
        self.history_id = None


def _mk_shell(hist, encode_pair):
    sh = object.__new__(_app_mod.App)          # App 壳：复用 MRO 上的落库方法
    sh.config = _types.SimpleNamespace(get=lambda s, k, d=None: d)
    sh.history = hist
    sh.model_tier = "tiny"
    sh.accel = "cpu"
    sh._saveq = _SaveQueue(hist, encode_pair)
    return sh


with tempfile.TemporaryDirectory() as _td:
    _db = _open_db(Path(_td) / "save.db")
    _db.init_db()
    _hist = HistoryStorage(_db, Path(_td) / "shots", max_records=50)
    _saves = []
    _orig_save = _hist.save

    def _counted_save(**kw):
        _saves.append(1)
        return _orig_save(**kw)

    _hist.save = _counted_save
    _sh = _mk_shell(_hist, lambda img: (True, "png", b"main-bytes", b"thumb-bytes"))
    _img = _np.zeros((16, 16, 3), dtype=_np.uint8)
    # 数「提交」次数而不是「落库」次数：save() 在后台线程里跑，在提交那一刻
    # 还没发生（数它就成了时序断言）。拦住重复提交是同步发生的事实。
    _submits = []
    _orig_submit = _sh._saveq.submit

    def _counted_submit(*a, **k):
        _submits.append(1)
        return _orig_submit(*a, **k)

    _sh._saveq.submit = _counted_submit

    _ov = _OvStub(_img)
    _sh._save_record(_ov)
    check("首次落库是异步的：返回时 history_id 仍为空、且队列非空",
          _ov.history_id is None and _sh._saveq.busy is True)
    # 在途窗口期再点两次（收藏 / 标注就是这么连续调的）：不能重复落库
    _got = []
    _sh._save_record(_ov, on_saved=_got.append)
    _sh._save_record(_ov, on_saved=lambda r: _got.append(r))
    check("在途期间重复提交被拦住（只入队一次）", len(_submits) == 1,
          f"submit() 调用 {len(_submits)} 次")
    check("flush 能把在途任务收干", _sh._saveq.flush(5000) is True)
    check("收干后 history_id 已回填、记录已入库",
          _ov.history_id is not None and _hist.get(_ov.history_id) is not None)
    check("续作全部执行且拿到同一个 rid",
          _got == [_ov.history_id, _ov.history_id], f"{_got}")
    check("记录只落了一条（异步窗口期的重复调用没有多写）",
          _hist.count() == 1 and len(_saves) == 1,
          f"count={_hist.count()} saves={len(_saves)}")

    # 关键顺序不变量：续作必须跑在「记录已入库之后」——否则「完成」按钮刷新面板
    # 会刷在新记录之前，用户看不到刚完成的那张图
    _seen = []
    _ov2 = _OvStub(_img)
    _sh._save_record(_ov2, on_saved=lambda r: _seen.append(_hist.get(r) is not None))
    _sh._saveq.flush(5000)
    check("续作跑在记录入库之后（刷新不早于入库）", _seen == [True], f"{_seen}")

    # 已落库的窗口：续作同步执行（保持改前的行为，调用方无需感知异步）
    _sync = []
    _sh._save_record(_ov, on_saved=_sync.append)
    check("已落库窗口的续作同步执行（行为与改前一致）",
          _sync == [_ov.history_id] and _sh._saveq.busy is False)

    # 编码失败：任务不能把队列挂死，且要清掉在途标记以便之后重试
    _fail = _mk_shell(_hist, lambda img: (False, "png", None, None))
    _ovf = _OvStub(_img)
    _fail._save_record(_ovf)
    _fail._saveq.flush(5000)
    check("编码失败：队列不挂死、在途标记被清掉（可重试）",
          _fail._saveq.busy is False
          and getattr(_ovf, "_save_pending", False) is False
          and _ovf.history_id is None)
    _fail._saveq.stop()
    _sh._saveq.stop()

# ---- 8b. 设置页：GPU 探测 / 清空历史 已下沉 ----
_st2 = (_root / "ui" / "windows" / "settings" / "software_tab.py").read_text(encoding="utf-8")
# 用 AST 找**真实调用**，而不是子串匹配：注释里提到 processEvents（解释为什么
# 不再用它）不该把断言带成假失败
_st_tree2 = ast.parse(_st2)
_probe2 = next(n for n in ast.walk(_st_tree2)
               if isinstance(n, ast.FunctionDef) and n.name == "_run_gpu_probe")
_pump_calls = [n for n in ast.walk(_probe2)
               if isinstance(n, ast.Call)
               and getattr(n.func, "attr", "") == "processEvents"]
check("_run_gpu_probe 里已无 processEvents 调用（不再留重入窗口）",
      not _pump_calls, f"调用行号={[n.lineno for n in _pump_calls]}")
check("GPU 探测改用 GpuProbeWorker（后台线程）",
      "GpuProbeWorker(probe_fn, det_path, parent=self)" in _st2)
check("探测结果只在完成回调里消费（_on_gpu_probe_done）",
      "def _on_gpu_probe_done" in _st2)
check("清空历史改用 HistoryClearWorker（不再同步 self._history.clear）",
      "HistoryClearWorker(" in _st2 and "self._history.clear(" not in _st2)
check("提供 stop_workers() 供退出时收尾",
      "def stop_workers" in _st2)
_app_src2 = (_root / "app.py").read_text(encoding="utf-8")
check("退出流程收尾后台保存队列与设置页线程",
      "_saveq.stop(" in _app_src2 or "sq.stop(" in _app_src2)
check("退出流程调用设置页 stop_workers", "sp_stop()" in _app_src2)

# ---- 8c. history_panel：推送路径去抖，显式动作仍立即 ----
from ui.windows.history_panel import HistoryPanel as _HP
from PySide6.QtCore import QTimer as _QTimer

check("HistoryPanel 提供 reload_soon（去抖刷新）", hasattr(_HP, "reload_soon"))
_hp_src = (_root / "ui" / "windows" / "history_panel.py").read_text(encoding="utf-8")
check("reload_soon 走单次定时器，不是直接 reload",
      "self._reload_timer.start(delay_ms)" in _hp_src)
check("推送路径（_reload_history_panel）用去抖，显式动作仍用 reload()",
      "p.reload_soon()" in _hs_src)
# 去抖语义：连打 5 次只应触发 1 次
# 注意：HistoryPanel 是 QWidget 子类，只能用它自己的 __new__（object.__new__
# 会被 PySide6 拒绝："is not safe"）
_hp = _HP.__new__(_HP)             # 壳：reload_soon 只碰 _reload_timer
_hits = []
_hp._reload_timer = _QTimer()
_hp._reload_timer.setSingleShot(True)
_hp._reload_timer.timeout.connect(lambda: _hits.append(1))
for _ in range(5):
    _hp.reload_soon(30)
_deadline = time.monotonic() + 2.0
while not _hits and time.monotonic() < _deadline:
    QCoreApplication.processEvents()
    time.sleep(0.01)
check("连续 5 次推送只触发 1 次刷新（去抖生效）", len(_hits) == 1, f"hits={len(_hits)}")

# ---- 8d. selection：鼠标移动改局部重绘 ----
from PySide6.QtCore import QPoint as _QPoint

_sel_src = (_root / "ui" / "windows" / "selection.py").read_text(encoding="utf-8")
check("mouseMoveEvent 不再无条件整屏 update()",
      "self.update(dirty.united(self._content_dirty()))" in _sel_src
      and "_content_dirty" in _sel_src)
_sel2 = SelectionOverlay(None)
# 位置一律按窗口尺寸取，避免依赖具体屏幕分辨率（offscreen 只有 800×600）
_sw, _sh_ = _sel2.width(), _sel2.height()
_cursor = _QPoint(4, 4)                                # 光标在左上
_old_pick = _QRect(_sw - 60, _sh_ - 60, 30, 30)        # 上一帧取色器画在右下
_sel2._cursor_pt = _cursor
_sel2._start = _sel2._end = None
_sel2._adjusted = None
_sel2._hover_win = None
_sel2._picker_painted = _old_pick
_dirty = _sel2._content_dirty()
check("脏区非空且完全落在窗口内（不会产生越界无效区域）",
      not _dirty.isNull() and _sel2.rect().contains(_dirty), f"{_dirty.getRect()}")
check("脏区包含上一帧取色器区域（旧位置一定会被擦掉，否则留残影）",
      _dirty.contains(_old_pick), f"dirty={_dirty.getRect()} old={_old_pick.getRect()}")
check("脏区包含当前光标处包络（新位置会被重画）",
      _dirty.contains(_cursor), f"{_dirty.getRect()}")
# 无选区/无光标/无上一帧 → 空脏区（而不是原点处的 2×2 无效重绘）
_sel2._cursor_pt = None
_sel2._picker_painted = None
check("无任何内容时脏区为空（不触发无谓重绘）",
      _sel2._content_dirty().isNull())
_sel2.deleteLater()

# ---- 8e. QThread 收尾必须释放本线程的 DB 连接 ----
# 这条是第 15 批顺手挖出来的真泄漏：`threading.local` 的按线程数据由 threading
# 的线程收尾钩子清理，而 QThread 是原生线程、不走那套钩子 → 线程跑完后那条
# sqlite 连接留到进程退出，文件（含 -wal/-shm）一直被占用（Windows 删目录报
# WinError 32，外部工具也打不开库）。断言方式就是「文件能不能删掉」。
import shutil as _shutil

_dbd = tempfile.mkdtemp(prefix="threaddb_")
try:
    _pdb = Path(_dbd) / "t.db"
    _dbx = DBManager(_pdb)
    _dbx.init_db()
    _hx = HistoryStorage(_dbx, Path(_dbd) / "s", max_records=10)
    _qx = _SaveQueue(_hx, lambda img: (True, "png", b"m", b"t"))
    _qx.submit(None, ocr_text="", ocr_boxes=None, model_tier="", device="",
               name_pattern="", on_done=None, on_failed=None)
    _qx.flush(5000)
    _qx.stop()                     # 线程收尾（finally 里放连接）
    _dbx.close()                   # 关掉**主线程**自己的连接
    try:
        _pdb.unlink()
        _unlink_ok, _unlink_err = True, ""
    except OSError as _e:
        _unlink_ok, _unlink_err = False, str(_e)
    check("后台线程退出后 sqlite 文件不再被占用（连接已释放）",
          _unlink_ok, _unlink_err)
finally:
    _shutil.rmtree(_dbd, ignore_errors=True)

check("DBManager 提供 close_thread_conn（QThread 收尾入口）",
      hasattr(DBManager, "close_thread_conn"))
_wk_src = (_root / "appmod" / "workers.py").read_text(encoding="utf-8")
check("三个碰 DB 的 worker 都在 run() 收尾释放连接",
      _wk_src.count("_release_thread_db(") >= 4,
      f"count={_wk_src.count('_release_thread_db(')}")

print("== 9. 第 17 批：.gitignore 补漏 / 死代码清理 / 设备记录 / 解码收敛 / 启动摘要 ==")

# ---- 9a. .gitignore：运行态目录与 SQLite 旁挂文件必须被忽略 ----
_gi = (_root / ".gitignore").read_text(encoding="utf-8")
check(".venv-* 变体虚拟环境被忽略（实测本机有 .venv-dml/）",
      ".venv-*/" in _gi)
check("SQLite 的 -wal/-shm 旁挂文件被忽略（只有 *.db 挡不住）",
      "*.db-wal" in _gi and "*.db-shm" in _gi)
# 有 git 时用 git 自己判定（无 git / 非仓库则跳过这半条）。
# 注意 `--quiet` 只接**单个** pathname（多个会 fatal: rc=128），故逐个查。
_git_ok, _git_out = None, ""
try:
    import subprocess as _sp
    _rcs = []
    for _p in (".venv-dml/x", "clickocr.db-wal"):
        _r = _sp.run(["git", "check-ignore", "-q", _p],
                     cwd=str(_root), capture_output=True)
        _rcs.append(_r.returncode)
    _git_ok = all(rc == 0 for rc in _rcs)
    _git_out = f"rc={_rcs}"
except Exception as _e:      # noqa: BLE001 没有 git 就不强求
    _git_ok = None
if _git_ok is not None:
    check("git 自身确认这些路径被忽略", _git_ok is True, _git_out)

# ---- 9b. 零调用者死代码已删（顺带消掉 pip stderr 凭据外泄面）----
from core_engine import ort_runtime as _ort

check("install_runtime 已删除（零调用者 + 返回 pip stderr 原文）",
      not hasattr(_ort, "install_runtime"))
check("install_command 已删除（零调用者）",
      not hasattr(_ort, "install_command"))
check("ort_runtime 不再依赖 subprocess（不会再去起 pip）",
      "subprocess" not in
      (_root / "core_engine" / "ort_runtime.py").read_text(encoding="utf-8"))
check("保留的唯一 UI 入口 runtime_status 仍可用",
      _ort.runtime_status("cpu") == "无需安装（CPU 模式）"
      and _ort.device_ready("cpu") is True)

# ---- 9b-2. 实际落位回报（H5：EP 在列表里 ≠ 真的用上了 GPU）----
# 原实现只看 get_available_providers()，「已就绪」而实际已回落 CPU 时 UI
# 毫无提示。现在由引擎构建完成时回报真结果，runtime_status 优先用它。
_ort.record_actual("dml", True)
check("落位成功：状态显示「实际生效」",
      "实际生效" in _ort.runtime_status("dml"), _ort.runtime_status("dml"))
_ort.record_actual("dml", False)
check("落位失败：状态不再谎报「已就绪」，改为「已回退 CPU」",
      "已回退 CPU" in _ort.runtime_status("dml")
      and "已就绪" not in _ort.runtime_status("dml"),
      _ort.runtime_status("dml"))
check("actual_ok 暴露原始落位结果",
      _ort.actual_ok("dml") is False and _ort.actual_ok("cuda") is None)
check("available_devices 之外的新 device 不参与落位判断",
      _ort.actual_ok("migraphx") is None)

# 引擎构建路径必须真的回报（否则上面这套状态永远停在「可用」）
_re_src = (_root / "ocr" / "rapid_engine.py").read_text(encoding="utf-8")
check("OCR 引擎构建完成后回报实际落位",
      "def _report_actual_device(" in _re_src
      and "record_actual(dev, bool(self._gpu_active))" in _re_src
      and "self._report_actual_device()" in _re_src)
_ort._ACTUAL.clear()      # 清掉本段写入的落位状态，避免污染后续断言

# ---- 9c. table_engine.get_info：DML 不再被记成 cpu ----
from core_engine.table_engine import TableEngine as _TE


def _with_fake_ort(providers, fn):
    """在假 onnxruntime 环境下跑 fn（函数内是 `import onnxruntime as ort`）。"""
    _fake = _types.ModuleType("onnxruntime")
    _fake.get_available_providers = lambda: list(providers)
    _saved = sys.modules.get("onnxruntime")
    sys.modules["onnxruntime"] = _fake
    try:
        return fn()
    finally:
        if _saved is None:
            sys.modules.pop("onnxruntime", None)
        else:
            sys.modules["onnxruntime"] = _saved


_DML_EPS = ["DmlExecutionProvider", "CPUExecutionProvider"]
check("纯 CPU 档位 → cpu",
      _with_fake_ort(_DML_EPS, lambda: _TE._effective_device(False, False)) == "cpu")
check("DML 档位 + DML EP 可用 → dml（改前恒为 cpu，历史记录里记错账）",
      _with_fake_ort(_DML_EPS, lambda: _TE._effective_device(False, True)) == "dml")
check("勾了 CUDA 但环境没有 CUDA EP → 如实报 cpu（不再谎报）",
      _with_fake_ort(_DML_EPS, lambda: _TE._effective_device(True, False)) == "cpu")
check("CUDA EP 可用 → cuda",
      _with_fake_ort(["CUDAExecutionProvider", "CPUExecutionProvider"],
                     lambda: _TE._effective_device(True, False)) == "cuda")
check("onnxruntime 缺失/异常 → 保守报 cpu（不抛）",
      _with_fake_ort([], lambda: _TE._effective_device(False, True)) == "cpu")

_te_shell = _TE.__new__(_TE)
_te_shell._use_cuda = False
_te_shell._use_dml = True
_te_shell._loaded = True
_te_shell._model_type = "slanet_plus"
_te_shell._load_elapse = 0.0
check("get_info()['device'] 对 DML 引擎返回 dml",
      _with_fake_ort(_DML_EPS, lambda: _te_shell.get_info()["device"]) == "dml")

# ---- 9d. 解码实现两处收敛到同一个对象 ----
import appmod.drop_mixin as _dm_mod
import appmod.history_mixin as _hm_mod

check("drop_mixin 与 history_mixin 指向**同一个** decode_image_bytes",
      _dm_mod._decode_image_bytes is _hm_mod._decode_image_bytes)
_dm_src = (_root / "appmod" / "drop_mixin.py").read_text(encoding="utf-8")
# 用 AST 找**真实调用** cv2.imdecode：注释里提到它（解释为什么删掉这个降级分支）
# 不该把断言带成假失败
_dm_cv_calls = [n for n in ast.walk(ast.parse(_dm_src))
                if isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") == "imdecode"
                and getattr(getattr(n.func, "value", None), "id", "") == "cv2"]
check("drop_mixin 不再有「cv2.imdecode 降级」分支（那会静默丢掉 heic/avif）",
      not _dm_cv_calls, f"行号={[n.lineno for n in _dm_cv_calls]}")
check("drop_mixin 不再有无用导入 cv2 / numpy", "import cv2" not in _dm_src
      and "import numpy" not in _dm_src)

# ---- 9e. 启动日志：生效配置摘要（含解析后的绝对路径与记录数）----
import app as _app_mod2

_shots_dir = _root / "screenshots"
_logged = []
_orig_app_log = _app_mod2.log


class _CapLog:
    def info(self, msg, *a, **k):
        try:
            _logged.append(msg % a if a else msg)
        except Exception:        # noqa: BLE001
            _logged.append(str(msg))

    def __getattr__(self, _n):
        return lambda *a, **k: None


_app_mod2.log = _CapLog()
try:
    _cfg_shell = _types.SimpleNamespace(
        get=lambda s, k, d=None: {"ocr_device": "cpu", "ocr_model": "v6-tiny"}.get(k, d),
        get_screenshots_dir=lambda: _shots_dir,
        db_path=_root / "clickocr.db")
    _app_shell2 = object.__new__(_app_mod2.App)
    _app_shell2.config = _cfg_shell
    _app_shell2.accel = "cpu"
    _app_shell2.history = _types.SimpleNamespace(count=lambda: 7)
    _app_mod2.App._log_effective_config(_app_shell2)
finally:
    _app_mod2.log = _orig_app_log
_line = _logged[0] if _logged else ""
check("启动摘要落了一行日志", bool(_line), f"{_logged}")
check("摘要含 screenshots_dir 解析后的绝对路径", str(_shots_dir) in _line, _line)
check("摘要含库内记录数（判「配置没生效」还是「数据没写进去」）",
      "7 条记录" in _line, _line)
check("摘要含 db 路径与生效设备", "clickocr.db" in _line and "cpu" in _line, _line)

# ---- 9f. rapid_engine：_DML_DEVICE 只在构建期有效，构建后恢复 ----
import ocr.rapid_engine as _re_mod2

_re2 = (_root / "ocr" / "rapid_engine.py").read_text(encoding="utf-8")
check("旧写法（构建后仍读模块级 _DML_DEVICE 判 gpu_active）已消除",
      "self._gpu_active = _DML_DEVICE is not None and _DML_DEVICE >= 0" not in _re2)
check("_DML_PATCHED 真正被读（补丁未装时给出告警，不再静默）",
      "not _DML_PATCHED" in _re2)

_saved_re2 = (_re_mod2._RapidOCR_NEW, _re_mod2._detect_dml_device,
              _re_mod2._check_models_once, _re_mod2._V6_MODELS,
              _re_mod2._DML_DEVICE)
try:
    _re_mod2._RapidOCR_NEW = lambda params=None: "ENGINE"
    _re_mod2._detect_dml_device = lambda p=None: 3
    _re_mod2._check_models_once = lambda names: None
    _re_mod2._V6_MODELS = {"v6-tiny": (None, "det.onnx", "rec.onnx", "d.txt")}
    _re_mod2._DML_DEVICE = None            # 模拟「上一个引擎没留下状态」
    _eng_dml = _re_mod2.RapidOcrEngine(model_set="v6-tiny", use_dml=True, lazy=True)
    _built = _eng_dml._build_v6()
    check("构建成功（被 fake 的构建器返回）", _built == "ENGINE")
    check("构建后 _DML_DEVICE 已恢复原值（不再长期驻留 / 不干扰别的实例）",
          _re_mod2._DML_DEVICE is None, f"{_re_mod2._DML_DEVICE}")
    check("gpu_active 取自本次构建的局部探测值（不受恢复影响）",
          _eng_dml._gpu_active is True)
    # 反向：探测结果 -1（适配器全不可用）→ gpu_active=False，且不设 use_dml
    _re_mod2._detect_dml_device = lambda p=None: -1
    _eng_cpu = _re_mod2.RapidOcrEngine(model_set="v6-tiny", use_dml=True, lazy=True)
    _eng_cpu._build_v6()
    check("适配器全不可用时 gpu_active=False（按 CPU 构建）",
          _eng_cpu._gpu_active is False)
finally:
    (_re_mod2._RapidOCR_NEW, _re_mod2._detect_dml_device, _re_mod2._check_models_once,
     _re_mod2._V6_MODELS, _re_mod2._DML_DEVICE) = _saved_re2

print()
if FAIL:
    print(f"验证失败 {len(FAIL)} 项: {FAIL}")
    sys.exit(1)
print("全部验证通过")
