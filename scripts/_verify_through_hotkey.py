"""点击穿透热键守卫：**应用级快捷键为主 + 全局救援键默认关**。

为什么这样分（穿透态的特殊性）：穿透开启后窗口被设
`WS_EX_TRANSPARENT | WS_EX_LAYERED`，且 `nativeEvent` 对 WM_NCHITTEST 放行走默认
→ **整窗不可点击**。于是：
  - 主入口 = **应用级快捷键** `[hotkey_overlay] toggle_through`（默认 `Alt+F`），
    用 `QShortcut` + `Qt.ApplicationShortcut` 注册：焦点在贴图窗口、**状态栏**、
    **统一工具栏**等本应用的任何窗口上都生效，且**不占用系统热键**；
  - 第二入口 = **全局键** `[hotkey_global] through`（默认 `Ctrl+Alt+F`）：焦点在
    **其它软件**时应用级快捷键必然收不到，只有全局键能收到。刻意不用 `Alt+F`
    （Windows 多数程序的「文件」菜单助记键，全局占用代价过大）。

断言：
  ① 默认值：hotkey_overlay.toggle_through == alt+f、hotkey_global.through == ctrl+alt+f
  ② 应用级快捷键已注册，上下文 = ApplicationShortcut，键位 = Alt+F
  ③ 归属判断：本窗/状态栏/统一工具栏 → 响应；其它窗口 / 无活动窗口 → 不响应
  ④ toggle_through() 可开可关
  ⑤ 键位配 off 时：不解析、不注册快捷键
  ⑥ 设置面板两行俱在：贴图窗口组含本地行、全局热键组含救援行

运行：python -B scripts/_verify_through_hotkey.py （CI 由 tests/test_guards.py 拉起）
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        FAIL.append(name)


print("== 1) 配置默认值 ==")
from core_engine.config import _DEFAULTS

check("[hotkey_global] through 默认 ctrl+alt+f（全局键，且刻意不用 Alt+F）",
      _DEFAULTS["hotkey_global"]["through"] == "ctrl+alt+f",
      repr(_DEFAULTS["hotkey_global"].get("through")))
check("[hotkey_overlay] toggle_through 默认 alt+f（应用级快捷键是主入口）",
      _DEFAULTS["hotkey_overlay"]["toggle_through"] == "alt+f",
      repr(_DEFAULTS["hotkey_overlay"].get("toggle_through")))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

from core_engine.config import ConfigManager
from ui.windows.result_overlay import ResultOverlay

tmp = Path(tempfile.mkdtemp(prefix="thru_"))
IMG = np.zeros((120, 160, 3), dtype=np.uint8)

print("== 2) 应用级快捷键注册 ==")
cfg = ConfigManager(path=tmp / "c1.ini")
ov = ResultOverlay(IMG, (10, 10, 170, 130), 1.0, config=cfg)
check("解析出穿透键位（_through_hk）", ov._through_hk is not None,
      repr(ov._through_hk))
check("已注册 QShortcut（_sc_through）", ov._sc_through is not None)
check("上下文 = ApplicationShortcut（状态栏/工具栏焦点也能触发）",
      ov._sc_through is not None
      and ov._sc_through.context() == Qt.ApplicationShortcut,
      repr(getattr(ov._sc_through, "context", lambda: None)()))
check("键位 = Alt+F",
      ov._sc_through is not None
      and ov._sc_through.key().toString() == QKeySequence("alt+f").toString(),
      repr(ov._sc_through.key().toString()) if ov._sc_through else "<无>")

print("== 3) 归属判断（防止多贴图窗口一键全切 / 其它软件误触发）==")
_self, _strip, _toolbar, _foreign = object(), object(), object(), object()
owns = ResultOverlay._is_owner_window
check("活动窗口 = 本贴图窗口 → 响应", owns(_self, _self, _strip, _toolbar) is True)
check("活动窗口 = 状态栏 → 响应", owns(_strip, _self, _strip, _toolbar) is True)
check("活动窗口 = 统一工具栏 → 响应",
      owns(_toolbar, _self, _strip, _toolbar) is True)
check("活动窗口 = 其它软件窗口 → 不响应",
      owns(_foreign, _self, _strip, _toolbar) is False)
check("无活动窗口 → 不响应", owns(None, _self, _strip, _toolbar) is False)

print("== 4) toggle_through() 可开可关 ==")
check("初始未穿透", ov._through is False, repr(ov._through))
check("开", ov.toggle_through() is True and ov._through is True)
check("关", ov.toggle_through() is False and ov._through is False)
ov.close()

print("== 5) 键位配 off → 不解析、不注册 ==")
cfg_off = ConfigManager(path=tmp / "c2.ini")
cfg_off.cp.set("hotkey_overlay", "toggle_through", "off")
ov_off = ResultOverlay(IMG, (10, 10, 170, 130), 1.0, config=cfg_off)
check("off 时 _through_hk 为 None", ov_off._through_hk is None,
      repr(ov_off._through_hk))
check("off 时 _sc_through 不注册", ov_off._sc_through is None,
      repr(ov_off._sc_through))
check("off 时 toggle_through 仍可被程序调用（只是没有键位）",
      ov_off.toggle_through() is True)
ov_off.close()

print("== 6) 设置面板：本地行 + 全局救援行 ==")
from ui.windows.settings_panel import SettingsPanel


class _HKStub:
    active_combo = ""

    def check_conflict(self, _c): return False
    def set_combo(self, _c): return True
    def register_secondary(self, _c, _cb, _hid=2): return True
    def set_secondary_combo(self, _hid, _c): return True
    def unregister_secondary(self, _hid): return True
    def unregister_primary(self): return True


cfg_panel = ConfigManager(path=tmp / "c3.ini")
panel = SettingsPanel(cfg_panel, _HKStub(), None)
rows = {r[3]: (r[2], r[1]) for r in getattr(panel, "_hk_rows", [])}
check("「贴图窗口」组含穿透行（hotkey_overlay / toggle_through）",
      rows.get("toggle_through", (None, None))[0] == "hotkey_overlay",
      repr(rows.get("toggle_through")))
check("「全局热键」组含全局键行（hotkey_global / through）",
      rows.get("through", (None, None))[0] == "hotkey_global",
      repr(rows.get("through")))
panel.close()

print()
if FAIL:
    print(f"THROUGH_HOTKEY_FAIL: {FAIL}")
    sys.exit(1)
print("THROUGH_HOTKEY_ALL_PASS")
