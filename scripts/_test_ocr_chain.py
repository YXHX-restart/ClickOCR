# -*- coding: utf-8 -*-
"""第 6 条链路冒烟：ocr_auto 门控 + 未识别/已识别无文字 语义 + 面板文案。

A) App.ocr_auto 实时读配置（改开关同会话立即生效）
B) restore_record：ocr_boxes=NULL 才补识别，且受 ocr_auto 门控
C) _ocr_payload_for_save：未识别过的空结果写 NULL，识别过的空结果写 []
D) 历史面板文案：NULL → (未识别) / '[]' → (无文字)
E) DB 往返：save(ocr_boxes=None) 读回是 None 而不是 '[]'
"""
import os
import sys
import json
import types
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # 项目根（scripts/ 的上一级）
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np                                            # noqa: E402
import cv2                                                    # noqa: E402
from PySide6.QtWidgets import QApplication                     # noqa: E402

qapp = QApplication.instance() or QApplication(sys.argv)

from app import App                                           # noqa: E402
from ui.windows.history_panel import HistoryPanel               # noqa: E402
from core_engine.db_manager import DBManager, HistoryStorage   # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    print(("  OK  " if cond else "  FAIL") + f" {name}" + (f" | {extra}" if extra else ""),
          flush=True)
    if not cond:
        FAILED.append(name)


class FakeCfg:
    """config 替身：getbool 由 flag 控制，便于模拟设置面板改开关。"""

    def __init__(self, root):
        self.root = Path(root)
        self.flag = True

    def getbool(self, sec, key, default=True):
        return self.flag

    def get(self, sec, key, default=None):
        return default

    def get_screenshots_dir(self):
        return self.root


# ---------------------------------------------------------------- A
print("== A) App.ocr_auto 实时读配置（同会话生效）==", flush=True)
holder = types.SimpleNamespace(config=FakeCfg("."))
check("初始 True", App.ocr_auto.fget(holder) is True)
holder.config.flag = False
check("改 false 立即生效（无需重启）", App.ocr_auto.fget(holder) is False)
holder.config.flag = True
check("改回 true 立即生效", App.ocr_auto.fget(holder) is True)

# ---------------------------------------------------------------- A2
print("== A2) 静态防线：@property 与 self.X= 赋值冲突（会启动即崩）==", flush=True)
import re                                                       # noqa: E402

_src = (ROOT / "app.py").read_text(encoding="utf-8")
_props = set(re.findall(r"@property\s+def\s+(\w+)", _src))
_clash = []
for _n in sorted(_props):
    for _m in re.finditer(r"self\.%s\s*=" % re.escape(_n), _src):
        _clash.append((_n, _src[:_m.start()].count("\n") + 1))
check("app.py 无 property/赋值冲突", not _clash, repr(_clash))
check("ocr_auto 是 property（实时读配置）",
      isinstance(App.__dict__.get("ocr_auto"), property), repr(_props))


tmp = Path(tempfile.mkdtemp(prefix="clickocr_smoke_"))
img = np.zeros((40, 60, 3), dtype=np.uint8)
img[5:20, 5:30] = 255
cv2.imencode(".png", img)[1].tofile(str(tmp / "shot.png"))

scheduled = []


class FakeTimer:
    """把 QTimer.singleShot 变成同步调用，便于断言。"""

    @staticmethod
    def singleShot(ms, fn):
        scheduled.append(ms)
        fn()


# 补丁到 QTimer 类本身（而非某个模块命名空间）：App 拆分后 restore_record 等
# 方法各自从 PySide6.QtCore import QTimer，但都指向同一个类对象，这样补丁
# 对 app.py 与 appmod/*_mixin.py 里的调用一并生效。
import PySide6.QtCore as _QtCore  # noqa: E402
_QtCore.QTimer.singleShot = staticmethod(FakeTimer.singleShot)


def make_stub(cfg, ocr_calls=None):
    ov = types.SimpleNamespace(boxes=[], history_id=None,
                               load_annotations=lambda a: None)
    # 用 App 的实例壳（不跑 __init__）：这样 self.ocr_auto 走 App 的真实 property
    st = object.__new__(App)
    st.config = cfg
    st.history = None
    st.tray = types.SimpleNamespace(showMessage=lambda *a, **k: None)
    st._history_panels = []
    st._history_panel = None
    st._engine_loading = False
    st.engine = None
    st.model_tier = "tiny"
    st.accel = "cpu"
    st._make_overlay = lambda i, raw, boxes, persistent=False: (
        setattr(ov, "boxes", list(boxes)) or ov)
    st._ov = ov
    st._place_restored_overlay = lambda o: None
    _rec = ocr_calls if ocr_calls is not None else []
    st._set_ocr_running_status = lambda o: _rec.append("status")
    st._ocr_overlay = lambda o, i: _rec.append("ocr")
    return st


def rec(boxes_json):
    return {"id": 7, "screenshot_path": "shot.png", "ocr_boxes": boxes_json,
            "annotations": None}


# ---------------------------------------------------------------- B
print("== B) restore_record：NULL 才补识别 + ocr_auto 门控 ==", flush=True)

cfg = FakeCfg(tmp)
calls = []
st = make_stub(cfg, calls)
scheduled.clear()
App.restore_record(st, rec(None))
# status 出现两次是刻意的：restore_record 立即给状态栏反馈，
# _ocr_history_overlay 内再设一次（同一文案，幂等）
check("NULL + ocr_auto=开 → 立即识别", calls == ["status", "status", "ocr"], str(calls))
check("history_id 已关联原记录（结果回填原记录）", st._ov.history_id == 7, repr(st._ov.history_id))

calls = []
st = make_stub(cfg, calls)
cfg.flag = False                       # 关闭「截图后自动 OCR」
scheduled.clear()
App.restore_record(st, rec(None))
check("NULL + ocr_auto=关 → 不识别（等用户点「文字框」）", calls == [], str(calls))
cfg.flag = True

calls = []
st = make_stub(cfg, calls)
scheduled.clear()
App.restore_record(st, rec("[]"))
check("'[]'（已识别·无文字）+ 开 → 不重跑模型", calls == [], str(calls))

calls = []
st = make_stub(cfg, calls)
scheduled.clear()
App.restore_record(st, rec(json.dumps(
    [{"text": "hi", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]], "score": 0.9}])))
check("已有文字 → 不识别（boxes 直接显示）", calls == [], str(calls))

# _ocr_history_overlay 自身的双保险
calls = []
st = make_stub(cfg, calls)
ov2 = types.SimpleNamespace(boxes=[])
cfg.flag = False
App._ocr_history_overlay(st, ov2, img)
check("_ocr_history_overlay 二次校验：ocr_auto=关不识别", calls == [], str(calls))
cfg.flag = True
App._ocr_history_overlay(st, ov2, img)
check("_ocr_history_overlay：ocr_auto=开正常识别", calls == ["status", "ocr"], str(calls))
ov3 = types.SimpleNamespace(boxes=[object()])
calls = []
st = make_stub(cfg, calls)
App._ocr_history_overlay(st, ov3, img)
check("_ocr_history_overlay：已有 boxes 直接 return", calls == [], str(calls))

# ---------------------------------------------------------------- C
print("== C) 落库 payload：未识别 → NULL / 识别过 → [] ==", flush=True)


class B:
    def __init__(self, text, kind="ocr"):
        self.text = text
        self.polygon = [[0, 0], [1, 0], [1, 1], [0, 1]]
        self.score = 0.9
        self.kind = kind


st = types.SimpleNamespace()
st._record_ocr_payload = App._record_ocr_payload.__get__(st)
pl = App._ocr_payload_for_save.__get__(st)

ov_never = types.SimpleNamespace(_ocr_done=False, boxes=[])
t, b = pl(ov_never, [])
check("未识别过 + 空结果 → ocr_boxes=None", b is None and t == "", repr((t, b)))

ov_done = types.SimpleNamespace(_ocr_done=True, boxes=[])
t, b = pl(ov_done, [])
check("识别过 + 无文字 → ocr_boxes=[]", b == [], repr((t, b)))

ov_hist = types.SimpleNamespace(_ocr_done=False, boxes=[])
t, b = pl(ov_hist, [B("hello")])
check("有文字 → 列表且带 kind", b and b[0]["kind"] == "ocr" and t == "hello", repr(b))

t, b = pl(ov_done, [B("http://x", kind="qr")])
check("QR → ocr_text 带 [QR] 前缀、kind 保留",
      "[QR] http://x" in t and b[0]["kind"] == "qr", repr((t, b)))

# ---------------------------------------------------------------- D
print("== D) 历史面板文案 ==", flush=True)
from PySide6.QtWidgets import QLabel                            # noqa: E402

panel = HistoryPanel.__new__(HistoryPanel)
panel._thumb_dir = tmp / "thumb"
panel._thumb_dir.mkdir(parents=True, exist_ok=True)
panel._thumb_width = 64
now = 1757000000.0

base = {"screenshot_path": "shot.png", "model_tier": "v6-tiny",
        "is_favorite": 0, "created_at": now}


def row_text(**kw):
    r = dict(base)
    r.update(kw)
    row = HistoryPanel._make_row(panel, r, None)
    for w in row.findChildren(QLabel):
        if w.property("kind") == "text":
            return w.text()
    return None


def row_info(**kw):
    r = dict(base)
    r.update(kw)
    row = HistoryPanel._make_row(panel, r, None)
    for w in row.findChildren(QLabel):
        if w.property("kind") == "info":
            return w.text()
    return None


try:
    check("ocr_boxes=NULL → (未识别)", row_text(id=1, ocr_text="", ocr_boxes=None) == "(未识别)")
    check("ocr_boxes='[]' → (无文字)", row_text(id=2, ocr_text="", ocr_boxes="[]") == "(无文字)")
    check("有文字 → 原文", row_text(id=3, ocr_text="东莞", ocr_boxes="[]") == "东莞")
    # model_tier 改成「执行记录」后：未识别没有档位，副标题不能出现 " · " 空尾巴
    info_ok = row_info(id=4, ocr_text="", ocr_boxes=None, model_tier="")
    info_no = row_info(id=5, ocr_text="x", ocr_boxes="[]", model_tier="medium")
    check("未识别 → 副标题无档位（无空尾巴）",
          info_ok is not None and "·" not in info_ok, repr(info_ok))
    check("已识别 → 副标题带档位",
          info_no is not None and info_no.endswith("· medium"), repr(info_no))
except Exception as e:                                     # noqa: BLE001
    check("面板文案渲染", False, repr(e))

# ---------------------------------------------------------------- E
print("== E) DB 往返：None 存 NULL，不落成 '[]' ==", flush=True)
dbdir = tmp / "db"
dbdir.mkdir(exist_ok=True)
db = DBManager(dbdir / "clickocr.db", enable_vec0=False)
db.init_db()
hist = HistoryStorage(db, dbdir / "screenshots", max_records=50)
buf = cv2.imencode(".png", img)[1].tobytes()
rid_none = hist.save(image_bytes=buf, thumb_bytes=buf, ocr_text="",
                     ocr_boxes=None, model_tier="v6-tiny", device="cpu")
rid_empty = hist.save(image_bytes=buf, thumb_bytes=buf, ocr_text="",
                      ocr_boxes=[], model_tier="v6-tiny", device="cpu")
r1 = hist.get(rid_none)
r2 = hist.get(rid_empty)
check("save(ocr_boxes=None) → 读回 None", r1["ocr_boxes"] is None, repr(r1["ocr_boxes"]))
check("save(ocr_boxes=[]) → 读回 '[]'", r2["ocr_boxes"] == "[]", repr(r2["ocr_boxes"]))

# ---------------------------------------------------------------- F
print("== F) 执行记录：model_tier/device 只在识别完成后写 ==", flush=True)

# F1) _ocr_runtime_info 取引擎实际生效值（不是用户配置值）
def stub_app(engine, tier="tiny", accel="cpu"):
    s = types.SimpleNamespace()
    s.engine = engine
    s.model_tier = tier
    s.accel = accel
    s._ocr_runtime_info = App._ocr_runtime_info.__get__(s)
    s._save_ocr_meta = App._save_ocr_meta.__get__(s)
    return s


class Eng:
    def __init__(self, info=None, boom=False):
        self.info, self.boom = info, boom

    def get_info(self):
        if self.boom:
            raise RuntimeError("boom")
        return self.info


st = stub_app(Eng({"model_set": "PP-OCRv6-medium", "device": "cuda"}),
              tier="tiny", accel="cuda")
check("引擎报告 medium/cuda → 以引擎为准（不是配置的 tiny）",
      st._ocr_runtime_info() == ("medium", "cuda"), repr(st._ocr_runtime_info()))

st = stub_app(Eng({"model_set": "PP-OCRv6-small", "device": "cpu"}),
              tier="small", accel="cuda")
check("GPU 回退 → device 如实记 cpu（不是配置的 cuda）",
      st._ocr_runtime_info() == ("small", "cpu"), repr(st._ocr_runtime_info()))

st = stub_app(Eng({"model_set": "PP-OCRv6-tiny", "device": "dml"}),
              tier="tiny", accel="dml")
check("DML 生效 → device=dml", st._ocr_runtime_info() == ("tiny", "dml"))

st = stub_app(None, tier="small", accel="cuda")
check("引擎不可用 → 回退配置值", st._ocr_runtime_info() == ("small", "cuda"))

st = stub_app(Eng(boom=True), tier="medium", accel="cpu")
check("get_info 抛异常不炸 → 回退配置值", st._ocr_runtime_info() == ("medium", "cpu"))

st = stub_app(Eng({"model_set": "", "device": ""}), tier="small", accel="cpu")
check("get_info 空值 → 回退配置值", st._ocr_runtime_info() == ("small", "cpu"))

# F2) _save_ocr_meta：与「未识别」判据同源（_ocr_done）
st = stub_app(Eng({"model_set": "PP-OCRv6-medium", "device": "cuda"}))
ov_done._ocr_done = True
st._ocr_runtime_info = App._ocr_runtime_info.__get__(st)
check("跑过识别 → 写执行记录", st._save_ocr_meta(ov_done) == ("medium", "cuda"),
      repr(st._save_ocr_meta(ov_done)))
check("没跑过识别 → 空串（不是配置快照）", st._save_ocr_meta(ov_never) == ("", ""),
      repr(st._save_ocr_meta(ov_never)))

# F3) DB：update_ocr 写执行记录；save 留空不报错（NOT NULL 列用 '' 表示无记录）
hist.update_ocr(rid_none, "hello", [{"text": "hello", "polygon": [[0, 0]], "score": 0.9}],
                "medium", "dml")
r3 = hist.get(rid_none)
check("update_ocr 写入 model_tier/device",
      r3["model_tier"] == "medium" and r3["device"] == "dml",
      repr((r3["model_tier"], r3["device"])))
check("update_ocr 后 ocr_boxes 从 NULL 变成结果",
      r3["ocr_boxes"] and r3["ocr_boxes"] != "[]", repr(r3["ocr_boxes"])[:40])

rid_plain = hist.save(image_bytes=buf, thumb_bytes=buf, ocr_text="",
                      ocr_boxes=None, ext="png")
r4 = hist.get(rid_plain)
check("save 不传 tier/device → 存空串（NOT NULL 合法）",
      r4["model_tier"] == "" and r4["device"] == "", repr((r4["model_tier"], r4["device"])))

# F4) _update_history_ocr 把执行记录一路带到 update_ocr
st = make_stub(FakeCfg(dbdir / "screenshots"), [])
wrote = {}
st.history = types.SimpleNamespace(
    update_ocr=lambda rid, t, b, mt, dv: wrote.update(
        rid=rid, tier=mt, dev=dv) or True)
check("_update_history_ocr 传入执行记录",
      App._update_history_ocr.__get__(st)(7, [B("hi")]) is True
      and wrote == {"rid": 7, "tier": "tiny", "dev": "cpu"}, repr(wrote))

# 用真实读回记录跑一遍 restore_record，确认「打开即补识别」判定正确
cfg2 = FakeCfg(dbdir / "screenshots")
calls = []
st = make_stub(cfg2, calls)
App.restore_record(st, r1)
check("真实 NULL 记录 + ocr_auto=开 → 补识别", calls == ["status", "status", "ocr"], str(calls))
calls = []
st = make_stub(cfg2, calls)
App.restore_record(st, r2)
check("真实 '[]' 记录 → 不补识别", calls == [], str(calls))

# ---------------------------------------------------------------- 收尾
print("", flush=True)
if FAILED:
    print(f"SMOKE FAILED: {len(FAILED)} -> {FAILED}", flush=True)
else:
    print("SMOKE OK: 全部通过", flush=True)
sys.stdout.flush()
os._exit(1 if FAILED else 0)
