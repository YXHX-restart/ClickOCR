"""专项修复验证脚本（临时）：覆盖报告第六节各专项的核心行为。

运行：python -B scripts/_verify_special_fixes.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

import numpy as np

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        FAIL.append(name)


print("== 1. DropImportWorker：后台导入 + 去重 + 队列只存路径 ==")
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

import cv2
from core_engine.db_manager import DBManager, HistoryStorage
from appmod.workers import DropImportWorker
from core_engine.image_decode import decode_image_bytes
from appmod.history_mixin import HistoryMixin
from appmod.drop_mixin import DropMixin

tmp = Path(tempfile.mkdtemp(prefix="dropimp_"))
src = Path(tmp / "src"); src.mkdir()
for i in range(3):
    img = np.zeros((60, 80, 3), dtype=np.uint8)
    img[:, :, i % 3] = 200
    cv2.imwrite(str(src / f"pic{i}.png"), img)

db = DBManager(tmp / "h.db"); db.init_db()
hs = HistoryStorage(db, tmp / "shots", max_records=50)


class _Host(DropMixin, HistoryMixin):
    pass


host = _Host()
host.history = hs
host.config = type("C", (), {
    "get": staticmethod(lambda s, k, d=None: d),
    "getint": staticmethod(lambda s, k, d=0: d),
    "getbool": staticmethod(lambda s, k, d=False: d),
})()
host._encode_image_pair = HistoryMixin._encode_image_pair.__get__(host)

w = DropImportWorker(
    hs, [str(src / f"pic{i}.png") for i in range(3)], {},
    encode_pair=host._encode_image_pair,
    find_dup=lambda p, size, idx: None,
    decode=decode_image_bytes, name_pattern="{datetime}_{ms}")
added, touched = [], 0
w.finished_ok.connect(lambda a, t: (added.extend(a), globals().__setitem__("touched", t)))
w.run()                      # 同步执行 worker 主体（不依赖事件循环）
check("导入 3 张 → 3 条记录", len(added) == 3, f"added={len(added)}")
check("队列元素为 (rid, 原图路径) 而非解码图",
      all(isinstance(a, tuple) and len(a) == 2
          and isinstance(a[0], int) and isinstance(a[1], str) for a in added))
check("落库截图文件已写入", len(list((tmp / "shots").glob("*.png"))) == 3)
check("缩略图已写入", len(list((tmp / "shots" / "thumb").glob("*.png"))) == 3)
check("记录为『待识别』(ocr_boxes IS NULL)",
      all(r["ocr_boxes"] is None for r in hs.list(10)))
# OCR 阶段：从原图路径按需解码
img = host._decode_drop_image(added[0][1])
check("_decode_drop_image 可按需解码原图", img is not None and img.shape[2] == 3)
check("队列解码失败返回 None（不抛异常）",
      host._decode_drop_image(str(tmp / "不存在.png")) is None)

# 去重：同名同大小 → 置顶（不新增）
dup_index = {"pic0.png": [(added[0][0], Path(added[0][1]), (src / "pic0.png").stat().st_size)]}
w2 = DropImportWorker(hs, [str(src / "pic0.png")], dup_index,
                      encode_pair=host._encode_image_pair,
                      find_dup=lambda p, size, idx: (
                          idx.get(Path(p).name, [(None, None, -1)])[0][0]
                          if any(fs == size for _r, _p, fs in idx.get(Path(p).name, []))
                          else None),
                      decode=decode_image_bytes)
a2, t2 = [], []
w2.finished_ok.connect(lambda a, t: (a2.extend(a), t2.append(t)))
w2.run()
check("同名同大小 → 置顶不新增", not a2 and t2 == [1], f"a2={a2} t2={t2}")
check("置顶后记录数仍为 3", hs.count() == 3, f"count={hs.count()}")

print("== 2. _write_files 独占创建（并发同名不覆盖）==")
p1 = hs.screenshots_dir / "same.png"
(hs.screenshots_dir).mkdir(parents=True, exist_ok=True)
name_a = hs._write_files(b"AAAA", None, "png", "same")
name_b = hs._write_files(b"BBBB", None, "png", "same")
check("同名模板两次写入得到不同文件", name_a != name_b, f"{name_a} / {name_b}")
check("两份内容都保留",
      (hs.screenshots_dir / name_b).read_bytes() == b"BBBB"
      and (hs.screenshots_dir / name_a).read_bytes() == b"AAAA")

print("== 3. 样式常量单一来源 ==")
from ui.core.anno_core import (TOOL_STYLES, NUM_STYLES, MOSAIC_MODES,
                               tool_style_ids, num_style_ids)
from ui.components.anno.popups import _ToolOptionsPopup, _NumPopup
check("浮层复用 anno_core.TOOL_STYLES（同一对象）",
      _ToolOptionsPopup.TOOL_STYLES is TOOL_STYLES)
check("序号浮层复用 anno_core.NUM_STYLES", _NumPopup.STYLES is NUM_STYLES)
check("工具样式各 7 种",
      all(len(tool_style_ids(t)) == 7 for t in TOOL_STYLES))
check("序号样式 5 种", len(num_style_ids()) == 5)
check("马赛克模式 3 种", MOSAIC_MODES == ("pen", "rect", "textbox"))

from ui.components.toolbar import _ToolsPack
tp = _ToolsPack()
tp.set_params(rect_style="ellipse", arrow_style="elbow",
              line_style="wavy", pen_style="glow",
              num_style="paren", mosaic_mode="textbox")
check("恢复 16 种扩展样式（ellipse/elbow/wavy/glow）全部生效",
      tp.tool_styles["rect"] == "ellipse"
      and tp.tool_styles["arrow"] == "elbow"
      and tp.tool_styles["line"] == "wavy"
      and tp.tool_styles["pen"] == "glow")
check("非法样式被拒（保持默认）",
      (tp.set_params(rect_style="不存在的样式") or
       tp.tool_styles["rect"] == "ellipse"))
check("序号幽灵样式 dot 已移除（回退默认）",
      (tp.set_params(num_style="dot") or tp.num_style == "paren"))

print("== 4. 马赛克画笔：预览按格去重 + 落定网格合并 ==")
from ui.components.annotation import AnnotationSession
from PySide6.QtCore import QPointF
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QGraphicsScene

scene = QGraphicsScene(0, 0, 400, 300)
base = QPixmap(400, 300); base.fill()
sess = AnnotationSession(None, scene, base)
sess.tool = "mosaic"
sess.mosaic_mode = "pen"
sess.color = (255, 0, 0)
sess.width = 8
sess.mouse_press(QPointF(50, 50))
counts = []
for i in range(5):
    sess.mouse_move(QPointF(60 + i * 6, 60 + i * 4))
    counts.append(len(sess._draw_item.childItems()))
# 轨迹点 (60,60) 与 (66,64) 吸附到同一格（block=8）→ 第二个点不再新建图元。
# 旧实现是「每个采样点一个 QGraphicsPixmapItem」，密集涂抹时项数随点数线性膨胀。
check("预览按**格子**递增：同格重复点不新建图元",
      counts == [1, 1, 2, 3, 4], f"counts={counts}")
check("预览组保留在 _draw_item 中（未被每帧重建丢弃）",
      sess._draw_item is not None)
sess.mouse_release(QPointF(90, 80))
check("松手后落定为正式标注", len(sess.data) == 1
      and sess.data[0]["type"] == "mosaic_pen"
      and len(sess.data[0]["points"]) == 6, f"data={sess.data}")
check("落定后预览项已清理", sess._draw_item is None)

# 落定图元：网格化后**合并成少量矩形**（原实现 = 每个采样点一个图元）
final = sess._make_item(sess.data[0])
n_final = len(final.childItems())
check("落定合并后图元数少于采样点数（6 点 → 4 个矩形）",
      n_final == 4 and n_final < len(sess.data[0]["points"]),
      f"n={n_final} points={len(sess.data[0]['points'])}")

from ui.components.anno.items import _mosaic_pen_rects as _mpr
_h_pts = [[i, 40] for i in range(0, 200, 3)]      # 横向涂抹
_v_pts = [[40, i] for i in range(0, 200, 3)]      # 纵向涂抹
check("横向涂抹合并成 1 个矩形（行内连续格子并成横条）",
      len(_mpr(_h_pts, 8)) == 1, f"{_mpr(_h_pts, 8)}")
check("纵向涂抹合并成 1 个矩形（相邻行同跨度横条再并）",
      len(_mpr(_v_pts, 8)) == 1, f"{_mpr(_v_pts, 8)}")
check("空/脏数据不抛且返回空（不拖垮整条标注）",
      _mpr([], 8) == [] and _mpr(None, 8) == []
      and _mpr([[None, 1]], 8) == [], f"{_mpr([[None, 1]], 8)}")

print("== 5. 信号登记表（E-5）==")
from appmod.float_mixin import FloatMixin
from ui.windows.result_overlay import ResultOverlay


class _FO(FloatMixin):
    def _reocr(self): pass
    def _open_table_window(self): pass
    def _save_annotations(self, ov, t): pass
    def _favorite_overlay(self, ov): pass
    def _pin_overlay(self, ov, p): pass
    def _ocr_on_demand(self, ov): pass
    def _on_overlay_done(self, ov): pass
    def _on_copied(self, ov, t): pass
    def _on_overlay_closed(self, ov): pass


fo = _FO()
ov = ResultOverlay(np.zeros((40, 40, 3), dtype=np.uint8), (0, 0, 40, 40), 1.0)
fo._bind(ov, ov.reocr_requested, fo._reocr)
fo._bind(ov, ov.closed, lambda: None)
check("_bind 登记两个信号", len(ov._bound_signals) == 2)
fo._dispose_overlay(ov)
check("_dispose_overlay 清空登记表", ov._bound_signals == [])

print("== 6. 历史面板：缩略图缓存 + 批量抑制（D-3）==")
from ui.windows.history_panel import HistoryPanel
panel = HistoryPanel(hs, None, str(tmp / "shots"), width=520, cards=3, config=None)
panel.reload()
first = panel._thumb_cache and True
key_hits = 0
from PySide6.QtCore import QSize as _QS
for r in hs.list(10):
    p = panel._thumb_dir / Path(r["screenshot_path"]).name
    panel._cached_thumb(p, _QS(90, 63))          # 固定尺寸，模拟同一行的重复请求
    before = len(panel._thumb_cache)
    panel._cached_thumb(p, _QS(90, 63))
    if len(panel._thumb_cache) == before:
        key_hits += 1
check("缩略图缓存命中（同尺寸重复请求不增长缓存）", key_hits >= 1, f"hits={key_hits}")
check("缓存确有条目", len(panel._thumb_cache) >= 1, f"n={len(panel._thumb_cache)}")
builds = []
_orig_build = panel._build
panel._build = lambda *a, **k: builds.append(1)
with panel._batch():
    panel.reload()
    panel.reload()
    during = len(builds)
check("批量期间 reload 不触发列表重建", during == 0, f"during={during}")
check("批量结束统一重建一次（6 个列表）", len(builds) == 6, f"builds={len(builds)}")
panel._build = _orig_build
# 选中样式增量：只对新旧差集改属性
sel_calls = []
backend = panel._set_row_selected
panel._set_row_selected = staticmethod(lambda w, on: sel_calls.append(on))
panel._sync_selection_style(panel.list_all)
n_after_first = len(sel_calls)
panel._sync_selection_style(panel.list_all)
check("无选中变化时不重复刷新样式", len(sel_calls) == n_after_first,
      f"{n_after_first} -> {len(sel_calls)}")

print("== 7. QSS 双主题选择器零漂移（E-8 守卫）==")
from ui.base.theme import theme_qss_selector_drift
d, l = theme_qss_selector_drift()
check("暗/亮两套 QSS 选择器完全一致", not d and not l, f"dark_only={d} light_only={l}")

print("== 8. 主题临时 PNG 确定性命名（C-6）==")
from ui.base.theme import _color_tag, _arrow_path
check("同色得到稳定文件名", _color_tag("#bdbdbd") == "bdbdbd")
check("非法字符被过滤", _color_tag("#ff/00") == "ff00")
pth1, pth2 = _arrow_path("#bdbdbd"), _arrow_path("#bdbdbd")
check("两次取路径一致（不重复生成）", pth1 == pth2, f"{pth1} / {pth2}")
check("文件名来自颜色而非 hash 随机值", "bdbdbd" in pth1, pth1)

print("== 9. 多屏几何助手（B-6）==")
from ui.base.widgets import screen_available_geometry
g = screen_available_geometry(panel)
check("可取到窗口所在屏几何", not g.isNull() and g.width() > 0, f"g={g}")

print("== 10. 截图悬停探测节流（D-5）==")
from ui.windows.selection import SelectionOverlay
sel = SelectionOverlay(None)
from PySide6.QtCore import QPoint
check("首次探测放行", sel._should_probe_hover(QPoint(10, 10)))
check("同位置立即再探测被节流", not sel._should_probe_hover(QPoint(11, 12)))
check("位移超过阈值重新探测", sel._should_probe_hover(QPoint(200, 200)))
# 取色源改为「按光标所在屏」解析（多屏下用主屏底图会越界/读错色）：
# 底图与像素索引都由 _pick_src 给出，_sample_picker_image 不再自己读主屏。
_src, _ix, _iy = sel._pick_src(QPoint(50, 50))
check("逐屏底图已抓取（多屏取色前提）", len(sel._screen_imgs) >= 1)
check("取色底图解析到光标所在屏", _src is not None)
check("放大镜取块可用（numpy 路径）",
      sel._sample_picker_image(_src, _ix, _iy, 7).width() == 7
      if _src is not None else True)

print("== 11. 「拖入图片后自动 OCR」两处开关双向同步 ==")


class _CapCfg:
    """最小 ConfigManager 替身：只实现开关用到的 get/getbool/cp.set/save。"""

    def __init__(self, on: bool):
        self.cp = self                       # 真实实现里 cp 是 ConfigParser
        self._v = {"software": {"drop_ocr_auto": "true" if on else "false"}}
        self.saved = 0

    def get(self, sec, key, default=None):
        return self._v.get(sec, {}).get(key, default)

    def getbool(self, sec, key, default=False):
        raw = self.get(sec, key, None)
        if raw is None:
            return default
        return str(raw).lower() in ("1", "true", "yes", "on")

    def getint(self, sec, key, default=None):
        raw = self.get(sec, key, None)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def getcolor(self, sec, key, default=(0, 0, 0)):
        raw = self.get(sec, key, None)
        if raw is None:
            return default
        try:
            return tuple(int(v) for v in str(raw).split(","))
        except ValueError:
            return default

    def set(self, sec, key, val):
        self._v.setdefault(sec, {})[key] = val

    def save(self):
        self.saved += 1


_cap_cfg = _CapCfg(True)
_calls = []
_cap_panel = HistoryPanel(hs, None, str(tmp / "shots"), width=520, cards=3,
                          config=_cap_cfg, on_drop_ocr_changed=lambda: _calls.append(1))
check("角标初值取自配置（[software] drop_ocr_auto）",
      _cap_panel._cap_btn.isChecked() is True)
# 方向一：面板角标改动 → 写配置 + 通知 App 去刷设置窗
_cap_panel._cap_btn.setChecked(False)          # 等价于用户点一下
check("面板角标改动 → 写入配置",
      _cap_cfg.get("software", "drop_ocr_auto") == "false")
check("面板角标改动 → 通知 App 同步另一处 UI", len(_calls) == 1, f"{_calls}")
# 方向二：设置侧改了配置 → 面板角标被刷成一致，且**不回写配置**
_saved_before = _cap_cfg.saved
_cap_cfg.set("software", "drop_ocr_auto", "true")
_cap_panel.sync_drop_ocr_button()
check("设置侧改成「开」后，面板角标同步为开",
      _cap_panel._cap_btn.isChecked() is True)
check("同步刷新不回写配置（已屏蔽信号）",
      _cap_cfg.saved == _saved_before, f"saved {_saved_before}→{_cap_cfg.saved}")
# 显示时也同步：覆盖「在设置里改完、再打开面板」这条路径
_cap_cfg.set("software", "drop_ocr_auto", "false")
_cap_panel.show()
QApplication.processEvents()
check("面板每次显示都按配置刷新（构造时读一次是不够的）",
      _cap_panel._cap_btn.isChecked() is False)
_cap_panel.hide()

# 设置侧对称：勾选框按配置刷新 + 屏蔽信号
from ui.windows.settings.software_tab import SoftwareTabMixin


class _ChkStub:
    def __init__(self, on):
        self._on = on
        self.blocked = []

    def isChecked(self):
        return self._on

    def setChecked(self, v):
        self._on = bool(v)

    def blockSignals(self, b):
        self.blocked.append(b)


class _SwShell:
    sync_drop_ocr_checkbox = SoftwareTabMixin.sync_drop_ocr_checkbox

    def __init__(self, cfg, on):
        self._cfg = cfg
        self.chk_drop_ocr = _ChkStub(on)


_sw = _SwShell(_cap_cfg, False)
_cap_cfg.set("software", "drop_ocr_auto", "true")   # 面板侧把开关改成开
_sw.sync_drop_ocr_checkbox()
check("设置侧勾选框被刷成与配置一致", _sw.chk_drop_ocr.isChecked() is True)
check("刷新时屏蔽信号（否则 setChecked 会被当成用户操作）",
      _sw.chk_drop_ocr.blocked == [True, False], f"{_sw.chk_drop_ocr.blocked}")
# 接线：App 同时把回调给了两个面板
_app_src = (ROOT / "app.py").read_text(encoding="utf-8")
check("App 把同步回调同时接给历史面板与设置窗",
      _app_src.count("on_drop_ocr_changed=self._sync_drop_ocr_ui") == 2,
      f"count={_app_src.count('on_drop_ocr_changed=self._sync_drop_ocr_ui')}")
_sp_src = (ROOT / "ui" / "windows" / "settings_panel.py").read_text(encoding="utf-8")
check("设置页保存后（落盘成功）才通知同步",
      "_eff_drop_ocr" in _sp_src and "pending.append(_eff_drop_ocr)" in _sp_src)

print("== 12. 二维码/表格边框线宽可配 + 取色器上移 ==")
from core_engine import config as _cfgmod
from core_engine.ai_pipeline import OcrBox
from PySide6.QtGui import QColor

_d = _cfgmod._DEFAULTS["ui"]
check("默认值含二维码/表格线宽（1~10，同截图边框）",
      _d.get("qr_border_width") == "1" and _d.get("table_border_width") == "1",
      f"{_d.get('qr_border_width')} / {_d.get('table_border_width')}")
_tpl = (ROOT / "resources" / "config.ini").read_text(encoding="utf-8")
check("出厂模板含这两项（模板与默认值零漂移，test_config 另有强制守卫）",
      "qr_border_width = 1" in _tpl and "table_border_width = 1" in _tpl)
check("落盘路径覆盖这两项 + 颜色（设置面板 _save）",
      all(f'self._set("ui", "{k}"' in _sp_src for k in
          ("qr_border_width", "table_border_width",
           "qr_border_color", "table_border_color")))

# --- 行为断言：线宽**真的画上去了**，不是只读进属性 ---
_ov_cfg = _CapCfg(False)
_ov_cfg.set("ui", "qr_border_color", "10,20,30")
_ov_cfg.set("ui", "qr_border_width", "5")
_ov_cfg.set("ui", "table_border_color", "90,220,120")
_ov_cfg.set("ui", "table_border_width", "3")
ov2 = ResultOverlay(np.zeros((40, 40, 3), dtype=np.uint8), (0, 0, 40, 40), 1.0,
                    config=_ov_cfg)
check("ResultOverlay 从配置读入线宽",
      ov2._qr_border_width == 5 and ov2._table_border_width == 3,
      f"{ov2._qr_border_width} / {ov2._table_border_width}")
ov2.set_boxes([
    OcrBox(text="http://x", polygon=[(2, 2), (18, 2), (18, 18), (2, 18)],
           score=1.0, kind="qr"),
    OcrBox(text="abc", polygon=[(22, 2), (38, 2), (38, 18), (22, 18)],
           score=1.0),
])
_pens = [(it.pen().color().getRgb()[:3], it.pen().width()) for it in ov2._outline]
check("二维码框用配置线宽 5 与配置色",
      ((10, 20, 30), 5) in _pens, f"{_pens}")
check("普通 OCR 文字框不受影响（仍为蓝色 1px）",
      ((0, 200, 255), 1) in _pens, f"{_pens}")

# --- 表格网格线宽 ---
from ui.windows.overlay.table_view import TableViewMixin
from PySide6.QtWidgets import QGraphicsScene as _GScene


class _TvShell:
    _build_table_boxes = TableViewMixin._build_table_boxes


_tv = _TvShell()
_tv.scene = _GScene(0, 0, 200, 200)
_tv._table_boxes = []
_tv._table_rows = [(0, 10), (10, 20)]
_tv._table_cols = [(0, 20)]
_tv._table_border_color = QColor(90, 220, 120)
_tv._table_border_width = 3
_tv._build_table_boxes()
check("表格网格每格一个矩形、线宽取自配置（2 行 × 1 列）",
      len(_tv._table_boxes) == 2
      and all(it.pen().width() == 3 for it in _tv._table_boxes),
      f"{[it.pen().width() for it in _tv._table_boxes]}")

# --- 布局顺序：取色器 在 历史面板 之下、截图边框 之上 ---
from PySide6.QtWidgets import QFormLayout
from ui.windows.settings.ui_tab import UiTabMixin
from ui.windows.settings_panel import SettingsPanel
from ui.base.texts import T as _T


class _UiShell(UiTabMixin):
    _scroll_page = SettingsPanel._scroll_page
    _form = SettingsPanel._form
    # 原方法是 @staticmethod：取来当类属性会被绑定成实例方法（多收一个 self）
    _attach_hint = staticmethod(SettingsPanel._attach_hint)

    def __init__(self, cfg):
        self._cfg = cfg


_ui = _UiShell(_ov_cfg)
_page = _ui._build_ui_tab()
_form_obj = _page.findChild(QFormLayout)
_labels = []
for _i in range(_form_obj.rowCount()):
    _it = _form_obj.itemAt(_i, QFormLayout.LabelRole)
    _w = _it.widget() if _it is not None else None
    _labels.append(_w.text() if _w is not None else "")
_i_hist = _labels.index(_T("ui.hist_panel", "历史面板"))
_i_pick = _labels.index(_T("ui.picker", "取色器"))
_i_sel = _labels.index(_T("ui.sel_border", "截图边框"))
check("取色器已上移到历史面板之下、截图边框之上",
      _i_hist < _i_pick < _i_sel,
      f"历史={_i_hist} 取色器={_i_pick} 截图边框={_i_sel} 全部={_labels}")
check("线宽 SpinBox 初值取自配置（颜色 + 线宽成对出现）",
      _ui.spin_qr_width.value() == 5 and _ui.spin_table_width.value() == 3,
      f"{_ui.spin_qr_width.value()} / {_ui.spin_table_width.value()}")

print()
if FAIL:
    print(f"验证失败 {len(FAIL)} 项: {FAIL}")
    sys.exit(1)
print("全部专项验证通过")
