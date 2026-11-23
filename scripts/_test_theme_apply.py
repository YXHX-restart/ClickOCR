"""配色主题冒烟测试：切换、禁用态、选中行可读性、新增主题不改代码。

覆盖点：
  1. apply_theme 两套主题都能应用，且样式表无残留占位符
  2. 切换主题后 QSS 确实变了
  3. `:disabled` 规则存在（QSS 设了 color 后 QPalette 的 Disabled 组失效，
     不显式声明就会「禁用控件看起来仍可用」）
  4. 选中行文字对比度达标——本轮把表格选中底由深改浅，白字会变得不可读，
     已改为跟随 text 槽；这里用 WCAG 对比度把它钉住
  5. icon_color / accent_color 取自色板，不再硬编码
  6. resolve_theme 能解析主题名与 system
  7. themes.toml 里加一段新主题即可生效，theme.py 不用改

运行：python -B scripts/_test_theme_apply.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import ui.base.theme as th  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + ("" if cond else f" {detail}"))
    if not cond:
        FAIL.append(name)


# ------------------------------------------------------------ 颜色工具 --
def rgba_of(v):
    return th._rgba(v)


def over(fg, bg):
    """把 fg（可带 alpha）合成到不透明的 bg 上，返回 (r,g,b)。

    注意：QSS 的 alpha 是 0~255，所以系数是 a/255（不是 a）。
    """
    r, g, b, a = rgba_of(fg)
    return tuple(round(bg[i] + (v - bg[i]) * a / 255)
                 for i, v in enumerate((r, g, b)))


def _lin(c):
    c = c / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def rel_lum(rgb):
    r, g, b = (_lin(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg_hex, bg_rgb):
    l1, l2 = rel_lum(rgba_of(fg_hex)[:3]), rel_lum(bg_rgb)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


app = QApplication([])

print("== 1. 两套主题都能应用 ==")
for t in th.theme_names():
    th.apply_theme(app, t)
    qss = app.styleSheet()
    check(f"{t}: 样式表非空", len(qss) > 10000, f"{len(qss)} 字节")
    check(f"{t}: 无 {{{{P. 残留", "{P." not in qss)
    check(f"{t}: 无运行期占位符残留",
          "__COMBO_ARROW__" not in qss and "__CHECK__" not in qss)
    check(f"{t}: current_theme() 同步", th.current_theme() == t)

print("== 2. 切换主题确实换了样式表 ==")
th.apply_theme(app, "dark")
dark_qss = app.styleSheet()
th.apply_theme(app, "light")
light_qss = app.styleSheet()
check("dark 与 light 样式表不同", dark_qss != light_qss)

print("== 3. 禁用态规则存在 ==")
for t, qss in (("dark", dark_qss), ("light", light_qss)):
    n = qss.count(":disabled")
    check(f"{t}: 有 :disabled 规则", n >= 7, f"实际 {n} 条")
    check(f"{t}: 禁用文字取自 text_faint 槽",
          th.slots_of(t).text_faint in qss)

print("== 4. 选中行文字对比度（WCAG，阈值 4.5:1）==")
for t in th.theme_names():
    s = th.slots_of(t)
    # 表格选中行：selected 叠在 inset 叠在 bg 之上，文字用 text
    bg = over(s.bg, (28, 28, 30) if t == "dark" else (236, 236, 238))
    table = over(s.inset, bg)
    row = over(s.selected, table)
    cr = contrast(s.text, row)
    check(f"{t}: 表格选中行文字对比度 {cr:.2f}:1", cr >= 4.5)
    # 历史列表选中行同理
    listrow = over(s.selected, bg)
    cr2 = contrast(s.text, listrow)
    check(f"{t}: 列表选中行文字对比度 {cr2:.2f}:1", cr2 >= 4.5)

print("== 5. Python 侧取色来自色板 ==")
for t in th.theme_names():
    th.apply_theme(app, t)
    check(f"{t}: icon_color 取自 icon 槽",
          th.icon_color() == th.slots_of(t).icon)
    check(f"{t}: accent_color 取自 warning 槽",
          th.accent_color() == th.slots_of(t).warning)

print("== 6. resolve_theme 解析 ==")


class _Cfg:
    def __init__(self, v):
        self.v = v

    def get(self, sec, key, default=""):
        return self.v


for v, want in (("dark", "dark"), ("light", "light"),
                ("", th._META["system_light" if not th.is_system_dark()
                              else "system_dark"])):
    got = th.resolve_theme(_Cfg(v))
    check(f"配置 {v!r} → {got!r}", got == want, f"期望 {want!r}")

# 关键一致性：resolve_theme 与 install_system_follow 必须对「是否跟随系统」同判。
# 原先是两个不同判据 —— 配置写了个不存在的主题名时，前者当跟随系统、后者不当，
# 表现为「解析出来跟了系统，但系统切换时又不重应用」。
for v in ("", "system", "SYSTEM", "drak", "ocean"):
    _sys = th.is_system_mode(_Cfg(v))
    _resolved = th.resolve_theme(_Cfg(v))
    _falls_back_to_system = _resolved not in th.theme_names() or \
        v.strip().lower() not in th.theme_names()
    check(f"{v!r}: is_system_mode({_sys}) 与 resolve_theme 结论一致",
          _sys == _falls_back_to_system,
          f"is_system_mode={_sys} resolved={_resolved!r}")

print("== 7. 加主题不改代码 ==")
# 用真实主题做底，只替换几个槽，验证 _resolve / _build_qss 能处理新主题名
full = dict(th._RAW["themes"]["dark"])
full.update({"bg": "#101010", "text": "#f0f0f0", "accent_text": "#00aa88"})
th._RAW["themes"]["demo"] = full
try:
    s = th._resolve("demo")
    check("新增主题可解析", s.bg == "#101010" and s.text == "#f0f0f0")
    check("新增主题的派生槽生效",
          s.selected == th._derive(full["derived"]["selected"], s),
          f"selected={s.selected}")
    q = th._build_qss(s)
    check("新增主题可生成 QSS", len(q) > 10000 and "{P." not in q)
finally:
    del th._RAW["themes"]["demo"]

print()
print("== 8. 设置页主题下拉已接入 themes.toml ==")
import tempfile  # noqa: E402

from config import ConfigManager  # noqa: E402
from ui.windows.settings_panel import SettingsPanel  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="theme_ui_"))
_cfg = ConfigManager(path=_tmp / "config.ini")
_panel = SettingsPanel(_cfg, None, None)
_combo = _panel.combo_theme

items = [(_combo.itemText(i), _combo.itemData(i)) for i in range(_combo.count())]
datas = [d for _, d in items]
check("下拉项 = system + themes.toml 全部主题",
      datas == ["system", *th.theme_names()], f"实际 {datas}")
check("下拉项无重复", len(datas) == len(set(datas)))
check("每项都有显示名", all(t.strip() for t, _ in items), f"{items}")
check("每项有 data 值", all(d for _, d in items), f"{items}")
print(f"  （当前下拉内容：{' / '.join(t for t, _ in items)}）")

# 逐项走真实链路：下拉 → 配置 → resolve_theme → apply_theme
for t in th.theme_names():
    i = _combo.findData(t)
    check(f"下拉含主题 {t}", i >= 0)
    if i < 0:
        continue
    _combo.setCurrentIndex(i)
    _panel._save()
    check(f"选中 {t} 后 current_theme() == {t}",
          th.current_theme() == t, f"实际 {th.current_theme()}")
    check(f"选中 {t} 后已写入配置",
          _cfg.get("ui", "theme", "") == t,
          f"实际 {_cfg.get('ui', 'theme', '')!r}")

# 「跟随系统」也要能解析（不崩、落到某个真实主题）
_sys_i = _combo.findData("system")
check("下拉含 system 项", _sys_i >= 0)
if _sys_i >= 0:
    _combo.setCurrentIndex(_sys_i)
    _panel._save()
    check("system 解析为真实主题",
          th.current_theme() in th.theme_names(),
          f"实际 {th.current_theme()}")

print("== 9. 新增主题后下拉与生效链路都跟着变（不改代码）==")
_full = dict(th._RAW["themes"]["dark"])
_full["meta"] = {"label": "深海"}
_full.update({"bg": "#0b1020", "text": "#e6ecff"})
th._RAW["themes"]["ocean"] = _full
th._SLOT_CACHE.clear()
th._QSS_CACHE.clear()
try:
    check("theme_names() 含新主题", "ocean" in th.theme_names())
    check("theme_label() 读 toml", th.theme_label("ocean") == "深海")

    _p2 = SettingsPanel(_cfg, None, None)
    _c2 = _p2.combo_theme
    _d2 = [_c2.itemData(i) for i in range(_c2.count())]
    check("重建设置页后下拉多出该主题", "ocean" in _d2, f"实际 {_d2}")
    _lbl = _c2.itemText(_d2.index("ocean")) if "ocean" in _d2 else ""
    check("下拉显示名取自 toml label", _lbl == "深海", f"实际 {_lbl!r}")

    _i2 = _c2.findData("ocean")
    if _i2 >= 0:
        _c2.setCurrentIndex(_i2)
        _p2._save()
        check("选中新主题后生效", th.current_theme() == "ocean",
              f"实际 {th.current_theme()}")
        check("新主题的 QSS 已应用", th.slots_of("ocean").bg in app.styleSheet())
finally:
    del th._RAW["themes"]["ocean"]
    th._SLOT_CACHE.clear()
    th._QSS_CACHE.clear()

print()
if FAIL:
    print(f"主题冒烟失败 {len(FAIL)} 项: {FAIL}")
    sys.exit(1)
print("THEME_APPLY_ALL_PASS")
