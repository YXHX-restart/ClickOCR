"""配色架构守卫：themes.toml + 单一 QSS 模板。

架构（详见 ui/base/theme.py 顶部注释与 ui/base/themes.toml）：
  themes.toml 的语义槽  →  theme.py 解析（含派生槽）  →  _build_qss() 单一模板

两层防线：

  1. 结构冻结（永久不变）：QSS 去掉**全部色值字面量 + 注释**后，必须与结构基线
     （scripts/_p0_baseline_*.qss，本次改造后落盘）逐字节一致。
     → 之后任何改动若碰了选择器/声明/分号/空白，立刻被拦下。

  2. 色值快照（可审核更新）：每个语义槽在每套主题下的取值与
     scripts/_slot_snapshot.json 比对。
     → 改颜色必然失败；确认是有意改动后跑 `--update` 重录，diff 即改动清单。

另有分层与卫生检查：模板只能引用槽名（不得写死色值）、派生槽的值必须确实
等于其规则算出的结果、两套主题槽集合一致、无「定义了没人用」的槽、
暗亮选择器零漂移、单一模板、运行期占位符。

运行：
  python -B scripts/_verify_theme_tokens.py            # 校验
  python -B scripts/_verify_theme_tokens.py --update   # 重录结构基线 + 色值快照
"""
import inspect
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ui.base.theme as th  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
BASE_STRUCT = {t: SCRIPTS / f"_p0_baseline_{t}.qss" for t in ("dark", "light")}
SNAPSHOT = SCRIPTS / "_slot_snapshot.json"
THEME_PY = ROOT / "ui" / "base" / "theme.py"
TOML = ROOT / "ui" / "base" / "themes.toml"

# 只在 Python 里用、不出现在 QSS 模板中的槽（图标前景 / 强调填充上的前景）
PYTHON_ONLY_SLOTS = {"icon", "on_accent"}

# 模板里允许出现的**字面量色值**。它们全部用于「始终是深色」的控件——
# 截图浮窗(ResultOverlay)、标注工具栏角标、图片浮窗关闭按钮、钉图徽标——
# 这些控件悬浮在截图之上，白底图上也要可读，**刻意不随明暗主题变化**。
# 若把它们也变成槽，切换主题时会跟着变，反而在白底图上失去可读性。
TEMPLATE_LITERALS = {
    "rgba(30,30,32,205)",   # 角标按钮底
    "rgba(30,30,32,190)",   # 关闭按钮 / 钉图徽标底
    "rgba(55,55,60,220)",   # 上述控件的 hover 底
    "rgba(255,255,255,70)",  # 角标按钮描边
    "rgba(255,255,255,60)",  # 钉图徽标描边
    "#eee",                 # 角标按钮前景
    "#fff", "#000",         # 上述控件的前景 / hover 前景
    "#e04444", "#b93a3a",   # 关闭按钮 hover / pressed
    "#3d6de0", "#5b8cff",   # 角标按钮选中底 / 描边（固定深色区域内）
    "#ffd54f", "#ffe082",   # 钉图/收藏在固定深色浮窗上的取值
}

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        FAIL.append(name)


# ------------------------------------------------------------------ 工具 --
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}(?![0-9a-fA-F])|rgba?\([^)]*\)")


def canon_color(v: str) -> str:
    """色值规范化：3 位 hex 展开、去 rgba 空白、alpha=255 归 hex。"""
    v = v.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{3,8})", v)
    if m:
        h = m.group(1).lower()
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return "#" + h
    m = re.fullmatch(r"rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*(\d+))?\)", v)
    if not m:
        return v
    r, g, b = (int(x) for x in m.groups()[:3])
    a = m.group(4)
    a = int(a) if a is not None else 255
    return ("#%02x%02x%02x" % (r, g, b)) if a == 255 else f"rgba({r},{g},{b},{a})"


def norm(text: str) -> str:
    """去注释：QSS 注释被 Qt 忽略，不参与结构比对。"""
    text = re.sub(r"\n[ \t]*/\*.*?\*/[ \t]*(?=\n)", "", text, flags=re.S)
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def strip_colors(text: str) -> str:
    """去注释 + 把全部色值字面量替换为占位符：用于结构冻结比对。"""
    return _COLOR_RE.sub("\x00", norm(text))


def slots(theme: str) -> dict:
    return dict(th.slots_of(theme))


def color_slots(theme: str) -> dict:
    """该主题的**颜色**槽（排除非颜色的 fragments 片段）。"""
    return {k: v for k, v in slots(theme).items()
            if not k.startswith("diff_")}


THEMES = th.theme_names()
SRC = THEME_PY.read_text(encoding="utf-8")
TOML_SRC = TOML.read_text(encoding="utf-8")
# 关键：槽引用与字面量色值要查**模板源码**。渲染后的 QSS 里 {P.x} 已被替换成
# 色值，再正则匹配只会捞到一堆颜色，看不出结构。
TPL_SRC = inspect.getsource(th._build_qss)

# --------------------------------------------------- 1. 单一模板 -----------
print("== 1. 单一模板：所有主题同源、仅槽取值不同 ==")
check("_build_qss 只定义一次", SRC.count("def _build_qss") == 1)
check("themes.toml 至少两套主题", len(THEMES) >= 2, f"{THEMES}")
for t in THEMES:
    check(f"{t}: QSS 由 _build_qss 构建",
          th.qss_of(t) == th._build_qss(th.slots_of(t)))
uniq = {th.qss_of(t) for t in THEMES}
check("各主题 QSS 互不相同", len(uniq) == len(THEMES))

# ---------------------------------------------- 2. 结构冻结（永久不变）--
print("== 2. 结构冻结：去掉色值与注释后与结构基线逐字节一致 ==")
if "--update" in sys.argv:
    for t in THEMES:
        BASE_STRUCT[t].write_text(th.qss_of(t), encoding="utf-8")
    print(f"  [更新] 已重录 {len(THEMES)} 份结构基线")
else:
    for t in THEMES:
        f = BASE_STRUCT.get(t)
        if f is None or not f.exists():
            check(f"{t}: 结构基线存在", False, f"缺 {f}")
            continue
        base = strip_colors(f.read_text(encoding="utf-8"))
        now = strip_colors(th.qss_of(t))
        if now == base:
            check(f"{t}: 结构与基线一致（{len(now)} 字节）", True)
        else:
            i = next((k for k in range(min(len(now), len(base)))
                      if now[k] != base[k]), min(len(now), len(base)))
            check(f"{t}: 结构与基线一致", False,
                  f"\n      首个差异 @ {i}\n      现  : …"
                  f"{now[max(0, i - 80):i + 80]!r}…\n      基线: …"
                  f"{base[max(0, i - 80):i + 80]!r}…")

# ------------------------------------- 3. 选择器零漂移 + 规则数 -----------
print("== 3. 选择器零漂移 ==")
d, l = th.theme_qss_selector_drift()
check("各主题规则选择器完全一致", not d and not l,
      f"dark_only={d[:4]} light_only={l[:4]}")
rule_counts = {}
for t in THEMES:
    q = th.qss_of(t)
    rule_counts[t] = len(re.findall(r"^[^\n{}/][^\n{]*\{", q, flags=re.M))
check("各主题规则数一致", len(set(rule_counts.values())) == 1,
      f"{rule_counts}")

# ------------------------------------------------- 4. 槽分层与完整性 ----
print("== 4. 槽完整性：集合一致 / 模板引用齐全 / 无死槽 ==")
base_slots = set(color_slots(THEMES[0]))
for t in THEMES[1:]:
    check(f"{t}: 槽名集合与 {THEMES[0]} 一致",
          set(color_slots(t)) == base_slots,
          f"差异={set(color_slots(t)) ^ base_slots}")

tpl_refs = set(re.findall(r"\{P\.(\w+)\}", TPL_SRC))
# 模板里还留着非颜色的 diff_* 片段引用（它们是「局部差异」而非槽）
used_frag = {u for u in tpl_refs if u.startswith("diff_")}
used = tpl_refs - used_frag
missing = sorted(used - base_slots)
check("模板引用的槽都已定义", not missing, f"未定义={missing}")

dead = sorted(base_slots - used - PYTHON_ONLY_SLOTS)
check("无死槽（定义了却没人用）", not dead, f"未使用={dead}")

for t in THEMES:
    unknown = sorted(set(re.findall(r"\{P\.(\w+)\}", th.qss_of(t)))
                     - base_slots - used_frag)
    check(f"{t}: 模板无未定义引用", not unknown, f"{unknown}")

# ------------------------------------------------- 5. 派生槽自洽 ---------
print("== 5. 派生槽：值必须等于规则算出的结果 ==")
raw = th._RAW["themes"]
derived_any = sorted({k for t in THEMES for k in raw[t].get("derived", {})})
check("存在派生槽", bool(derived_any), f"{derived_any}")
for t in THEMES:
    rules = raw[t].get("derived", {})
    for name, rule in rules.items():
        src_key = rule["from"]
        if src_key not in raw[t]:
            check(f"{t}.{name}: 派生源存在", False, f"缺 {src_key}")
            continue
        check(f"{t}.{name} = {src_key} 派生值正确",
              slots(t)[name] == th._derive(rule, slots(t)))
    # 派生槽不得与手写槽重名（否则覆盖顺序决定行为，容易埋雷）
    dup = sorted(set(rules) & {k for k, v in raw[t].items()
                               if isinstance(v, str)})
    check(f"{t}: 派生槽不与手写槽重名", not dup, f"{dup}")

# ------------------------------------------------- 6. 模板无硬编码色值 ----
print("== 6. 模板字面量色值：只允许「固定深色控件」那一批 ==")
tpl_lits = {canon_color(c) for c in _COLOR_RE.findall(TPL_SRC)}
extra = sorted(tpl_lits - {canon_color(c) for c in TEMPLATE_LITERALS})
check("模板无未登记的字面量色值", not extra, f"未登记={extra}")
stale = sorted({canon_color(c) for c in TEMPLATE_LITERALS} - tpl_lits)
check("白名单无过期项（登记了但已不存在）", not stale, f"过期={stale}")
print(f"  （已登记 {len(TEMPLATE_LITERALS)} 个固定深色控件的字面量色值）")

# fragments 是「两套主题结构性不同」的声明片段（如亮色独有的滑块手柄描边），
# 这种不对称无法用槽表达，只能在片段里内嵌色值——登记允许项，防止悄悄藏色值。
FRAGMENT_LITERALS = {"rgba(0,0,0,90)"}
frag_colors = set()
for t in THEMES:
    for v in raw[t].get("fragments", {}).values():
        frag_colors |= {canon_color(c) for c in _COLOR_RE.findall(v)}
check("片段内嵌色值已登记",
      frag_colors <= {canon_color(c) for c in FRAGMENT_LITERALS},
      f"未登记={sorted(frag_colors - {canon_color(c) for c in FRAGMENT_LITERALS})}")

# ------------------------------------------------- 7. 色值快照 -----------
print("== 7. 色值快照：槽取值与快照一致 ==")
cur_snap = {t: {k: canon_color(v) for k, v in sorted(color_slots(t).items())}
            for t in THEMES}
if "--update" in sys.argv:
    SNAPSHOT.write_text(json.dumps(cur_snap, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"  [更新] 已重录色值快照（{len(cur_snap)} 套主题）")
else:
    old = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for t in THEMES:
        o, c = old.get(t), cur_snap[t]
        if o is None:
            check(f"{t}: 快照存在", False, "快照里没有这套主题")
            continue
        added = sorted(set(c) - set(o))
        removed = sorted(set(o) - set(c))
        changed = [k for k in c if k in o and c[k] != o[k]]
        check(f"{t}: 槽集合无增删", not added and not removed,
              f"新增={added} 删除={removed}")
        check(f"{t}: 取值无漂移", not changed,
              "\n      " + "\n      ".join(
                  f"{k}: {o[k]} → {c[k]}" for k in changed[:10]))

# ------------------------------------------------- 8. 运行期占位符 --------
print("== 8. 运行期占位符 ==")
for t in THEMES:
    q = th.qss_of(t)
    for ph in ("__COMBO_ARROW__", "__CHECK__"):
        check(f"{t}: {ph} 出现 1 次", q.count(ph) == 1, f"实际 {q.count(ph)}")
check("无 ⟦⟧ 标记残留", "⟦" not in SRC)
check("Python 侧无硬编码主题色",
      not re.search(r'return "#[0-9a-fA-F]{6}" if _THEME', SRC))

print()
if FAIL:
    print(f"守卫失败 {len(FAIL)} 项: {FAIL}")
    sys.exit(1)
print("配色架构守卫全部通过")
