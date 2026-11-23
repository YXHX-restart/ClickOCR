"""_test_toolbar_shortcuts — 工具栏快捷键（Q/W/E/F）与「截图无操作也保存」开关冒烟。

覆盖：
1) overlay._handle_toolbar_key 对 Q/W/E/F 路由（qr/boxes/table/fav），标注态返回 False；
2) 快捷键默认值：toggle_boxes=w / toggle_table=e / fav=f，ratio=b；
3) 设置面板：新建了 toggle_boxes/toggle_table/fav 快捷键行 + chk_save_no_op 复选框，
    保存后写入 [software] save_no_op_close。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # core_engine 等包均在 ClickOCR 下（根目录旧残留已弃用）

import tempfile
import numpy as np
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QKeyEvent

app = QApplication([])

from config import ConfigManager
from ui.windows.result_overlay import ResultOverlay
from ui.components.toolbar import ToolBar

failures = []

# ---- 1) _handle_toolbar_key 路由 ----
img = np.zeros((300, 400, 3), dtype=np.uint8)
ov = ResultOverlay(img, (0, 0, 400, 300), 1.0)
tb = ToolBar()
ov.set_toolbar(tb)

calls = []
ov._toggle_qr = lambda: calls.append("qr")
ov._toggle_boxes = lambda: calls.append("boxes")
ov._toggle_table = lambda: calls.append("table")
fav_n = [0]
ov.fav_requested.connect(lambda: fav_n.__setitem__(0, fav_n[0] + 1))

for key, name in ((Qt.Key_Q, "qr"), (Qt.Key_W, "boxes"), (Qt.Key_E, "table")):
    e = QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier)
    ok = ov._handle_toolbar_key(e) and calls[-1] == name
    print(f"[{'OK' if ok else 'FAIL'}] 键 {name:<6} → _handle_toolbar_key 路由")
    if not ok:
        failures.append(f"route-{name}")

e_f = QKeyEvent(QEvent.KeyPress, Qt.Key_F, Qt.NoModifier)
ok = ov._handle_toolbar_key(e_f) and fav_n[0] == 1
print(f"[{'OK' if ok else 'FAIL'}] 键 F → fav_requested 触发")
if not ok:
    failures.append("route-fav")

# 带修饰键 → 不触发（避免 Ctrl+W 等误开）
ov._toggle_boxes = lambda: calls.append("boxes2")
e_w = QKeyEvent(QEvent.KeyPress, Qt.Key_W, Qt.ControlModifier)
if ov._handle_toolbar_key(e_w):
    failures.append("modifier-guard")
    print("[FAIL] 带 Ctrl 仍触发工具箱键")
else:
    print("[OK] 带修饰键不触发（Ctrl+W 保护）")

# 标注态 → 返回 False（QWERTYU 交 handle_anno_key）
ov._anno_mode = True
e = QKeyEvent(QEvent.KeyPress, Qt.Key_W, Qt.NoModifier)
if ov._handle_toolbar_key(e):
    failures.append("anno-guard")
    print("[FAIL] 标注态仍触发工具箱键")
else:
    print("[OK] 标注态不触发（E=箭头 优先）")
ov._anno_mode = False

# ---- 2) 默认值 ----
tmp = Path(tempfile.mkdtemp(prefix="tbk_"))
cfg = ConfigManager(path=tmp / "config.ini")
for key, default, exp in (("toggle_boxes", "w", "w"), ("toggle_table", "e", "e"),
                          ("fav", "f", "f"), ("ratio", "b", "b")):
    got = cfg.get("hotkey_overlay", key, default)
    ok = got == exp
    print(f"[{'OK' if ok else 'FAIL'}] {key} 默认 = {got}")
    if not ok:
        failures.append(f"default-{key}")

# ---- 2b) 更多按键类型：Qt 解析 f1/方向键/空格 ----
from ui.base.hotkey_keys import parse_qt_combo
for combo, want in (("f1", "Key_F1"), ("left", "Key_Left"),
                    ("ctrl+shift+f5", "Key_F5"), ("space", "Key_Space")):
    r = parse_qt_combo(combo)
    ok = r is not None and r[1] == getattr(Qt, want)
    print(f"[{'OK' if ok else 'FAIL'}] parse_qt_combo({combo!r}) → {ok}")
    if not ok:
        failures.append(f"parse-{combo}")

# ---- 3) 设置面板：行 + 复选框 + 保存（统一 _hk_rows）----
from ui.windows.settings_panel import SettingsPanel


class HKStub:
    active_combo = ""
    def check_conflict(self, c): return False
    def set_combo(self, c): self.active_combo = c; return True
    def register_secondary(self, c, cb, hid=2): return True
    def set_secondary_combo(self, hid, c): return True
    def unregister_secondary(self, hid): return True
    def unregister_primary(self): self.active_combo = ""; return True


cfg2 = ConfigManager(path=tmp / "config2.ini")
panel = SettingsPanel(cfg2, HKStub(), None)
row_keys = {row[3] for row in getattr(panel, "_hk_rows", [])}
for k in ("toggle_boxes", "toggle_table", "fav", "copy_shortcut", "color_fmt"):
    ok = k in row_keys
    print(f"[{'OK' if ok else 'FAIL'}] 设置面板含快捷键行 {k}")
    if not ok:
        failures.append(f"settings-row-{k}")
ok = hasattr(panel, "chk_save_no_op") and panel.chk_save_no_op.isCheckable()
print(f"[{'OK' if ok else 'FAIL'}] 设置面板含「截图无操作也保存」复选框")
if not ok:
    failures.append("settings-checkbox")
# 勾选并保存
panel.chk_save_no_op.setChecked(True)
panel._save()
got = cfg2.getbool("software", "save_no_op_close", False)
ok = got is True
print(f"[{'OK' if ok else 'FAIL'}] 保存后 [software] save_no_op_close = {got}")
if not ok:
    failures.append("save-noop")

# ---- 4) 统一控件可设置 F 键并保存 / KeyCaptureEdit 拾取 F-键 ---- 
# KeyCaptureEdit 直捕 F1 / 方向键
from ui.windows.settings_panel import KeyCaptureEdit
from PySide6.QtCore import QEvent as _QEvent
for key, expect in ((Qt.Key_F1, "f1"), (Qt.Key_Right, "right"),
                    (Qt.Key_Space, "space")):
    for ce in (KeyCaptureEdit(),):
        ce._capturing = True
        ce.keyPressEvent(QKeyEvent(_QEvent.KeyPress, key, Qt.NoModifier))
        ok = ce.key() == expect
        print(f"[{'OK' if ok else 'FAIL'}] KeyCaptureEdit 拾取 "
              f"Key_{key} → {ce.key()!r}")
        if not ok:
            failures.append(f"capture-{expect}")

# 用统一行保存一个 F 键快捷键（复现上一次 _save 的最终状态）
cfg3 = ConfigManager(path=tmp / "config3.ini")
p3 = SettingsPanel(cfg3, HKStub(), None)
for row, _lbl, _sec, key, *_ in p3._hk_rows:
    if key == "reocr":        # 把「重新 OCR」改为 ctrl+f2
        row._chk.setChecked(True)
        for cb, name in zip(row._mods, ("ctrl", "alt", "shift", "win"),
                            strict=True):
            cb.setChecked(name == "ctrl")
        row._cap._key = "f2"
p3._save()
got = cfg3.get("hotkey_overlay", "reocr", None)
ok = got == "ctrl+f2"
print(f"[{'OK' if ok else 'FAIL'}] 统一控件保存 ctrl+f2 → reocr = {got!r}")
if not ok:
    failures.append("save-f2")

print("\n===", "全部通过" if not failures else f"失败: {failures}", "===")
sys.exit(0 if not failures else 1)