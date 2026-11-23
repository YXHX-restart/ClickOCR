"""i18n 巡检：未提取的中文字面量 + T() 键一致性（可复跑，替代肉眼清点）。

两个模式（默认都跑）：

  A. **未提取字面量**：AST 扫出「含中日韩字符的字符串常量」，排除
     - 文档字符串（docstring）、注释（AST 里本就不含注释）；
     - `T(key, default)` / `T("key")` 的参数 —— 那里的中文正是「默认值」，应保留；
     - 日志与内部异常消息（按调用点归类为 non-UI，默认折叠，`--all` 展开）。

  B. **键一致性**：把代码里所有 `T("key", "默认值")` 与 resources/ui_text.json 比对
     - 值不一致（JSON 与代码默认值漂移）→ 报 not-matching；
     - JSON 有、代码从不引用 → 报 unused（可能是删文案后的残留）；
     - 代码引用、JSON 缺 key → 报 missing（运行期回退默认值，不算错，但要可见）。

用法：
    .venv\\Scripts\\python.exe scripts/_i18n_audit.py
    .venv\\Scripts\\python.exe scripts/_i18n_audit.py --all      # 含日志/内部消息
    .venv\\Scripts\\python.exe scripts/_i18n_audit.py --json out.json
    .venv\\Scripts\\python.exe scripts/_i18n_audit.py --check    # 巡检守卫：有问题退出码 1
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", ".venv-dml", ".venv_broken", "build", "dist", "wheels",
             "scripts", "tests", "__pycache__", ".git", ".workbuddy"}

_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")

#: 这些调用点上的中文是「面向用户」的（UI 文案）
UI_CALLS = {
    "setText", "setToolTip", "setWindowTitle", "setPlaceholderText", "setStatusTip",
    "setWhatsThis", "addItem", "addItems", "addAction", "setTitle", "showMessage",
    "setFormat", "setPrefix", "setSuffix", "setSpecialValueText", "setHtml",
    "setLabelText", "setInformativeText", "setDetailedText", "setButtonText",
    "setTabText", "setSectionLabel", "setCurrentText", "insertItem", "setContent",
    "setToolTipText", "setDescription", "setSubtitle", "setBadgeText",
}
UI_NAMES = {"QLabel", "QPushButton", "QCheckBox", "QRadioButton", "QGroupBox",
            "QAction", "QToolTip", "QMessageBox", "QDialogButtonBox", "QTabWidget",
            "QMenu", "QToolButton", "QStandardItem", "QListWidgetItem"}
#: 这些调用点上的中文属于日志/内部诊断，不是 UI 文案
LOG_CALLS = {"debug", "info", "warning", "error", "exception", "critical", "print"}


def _iter_py():
    for p in sorted(ROOT.rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p


def _s(node) -> str:
    """尽力还原节点源码（一行内）。"""
    try:
        return ast.unparse(node)
    except Exception:                    # noqa: BLE001
        return "<expr>"


class _Scan(ast.NodeVisitor):
    def __init__(self, path: Path):
        self.path = path
        self.docstrings: set[int] = set()
        self.t_args: set[int] = set()      # T(...) 里出现的字符串常量 id()
        self.t_keys: list[tuple[str, str, int]] = []   # (key, default, lineno)
        self.hits: list[dict] = []
        self.all_strings: set[str] = set()  # 文件里出现的所有字符串常量
        self.dyn_keys: list[int] = []      # T(非常量) 的行号 → 「动态 key」
        self.parents: dict = {}

    # ---- 收集上下文 ----
    def visit_Module(self, node):
        if (node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)):
            self.docstrings.add(id(node.body[0].value))
        self.generic_visit(node)

    def _mark_docstring(self, node):
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            self.docstrings.add(id(body[0].value))
        self.generic_visit(node)         # 必须继续下钻：漏了这句会看不到函数内的调用

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _mark_docstring

    def visit_Call(self, node):
        fname = ""
        if isinstance(node.func, ast.Name):
            fname = node.func.id
        elif isinstance(node.func, ast.Attribute):
            fname = node.func.attr
        if fname == "T":
            for a in node.args[:2]:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    self.t_args.add(id(a))
            if node.args:
                if isinstance(node.args[0], ast.Constant):
                    key = str(node.args[0].value)
                    default = (str(node.args[1].value)
                               if len(node.args) > 1
                               and isinstance(node.args[1], ast.Constant) else "")
                    self.t_keys.append((key, default, node.lineno))
                else:
                    # T(f"ui.theme_{x}") / T(some_var) 之类：key 在运行期才算得出来
                    self.dyn_keys.append(node.lineno)
        self.generic_visit(node)

    def visit_Constant(self, node):
        v = node.value
        if not isinstance(v, str):
            return
        self.all_strings.add(v)          # 供「key 是否被间接引用」判定
        if not _CJK.search(v):
            return
        if id(node) in self.docstrings or id(node) in self.t_args:
            return
        self.hits.append({"line": node.lineno, "text": v,
                          "kind": self._kind(node)})

    def _kind(self, node) -> str:
        """判定这条中文属于哪一类（靠**调用点**，不靠内容）。"""
        parent = self.parents.get(node)
        for _ in range(3):                # 向上找最近的调用
            if parent is None:
                break
            if isinstance(parent, ast.Call):
                fn = parent.func
                name = (fn.id if isinstance(fn, ast.Name)
                        else getattr(fn, "attr", ""))
                if name == "T":
                    return "t"
                if name in LOG_CALLS:
                    return "log"
                if name in UI_CALLS:
                    return "ui"
                if name in UI_NAMES:
                    return "ui"
                return "other"
            if isinstance(parent, (ast.Dict, ast.List, ast.Tuple)):
                kind = self._kind_in_container(parent)
                if kind:
                    return kind
            parent = self.parents.get(parent)
        return "other"

    def _kind_in_container(self, node) -> str | None:
        """元组/列表里塞 UI 文案的写法很常见（如按钮表 ``("key", "文案")``）：
        看容器被谁用了。"""
        holder = self.parents.get(node)
        while holder is not None:
            if isinstance(holder, ast.Call):
                fn = holder.func
                name = (fn.id if isinstance(fn, ast.Name)
                        else getattr(holder, "attr", ""))
                if name in UI_CALLS or name in UI_NAMES:
                    return "ui"
                if name in ("T",):
                    return "t"
                if name in LOG_CALLS:
                    return "log"
                if name in ("append", "extend", "add"):
                    return "maybe-ui"
                return "other"
            holder = self.parents.get(holder)
        return None

    def run(self, src: str):
        tree = ast.parse(src)
        self.parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        self.visit(tree)


def scan_literals():
    """→ (per_file, t_keys, all_strings, dynamic)"""
    per_file, t_keys = {}, []
    all_strings: set[str] = set()
    dynamic: list[dict] = []
    for p in _iter_py():
        src = p.read_text(encoding="utf-8")
        sc = _Scan(p)
        try:
            sc.run(src)
        except SyntaxError as e:         # pragma: no cover
            print(f"  ! 解析失败 {p.relative_to(ROOT)}: {e}")
            continue
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        all_strings |= sc.all_strings
        for ln in sc.dyn_keys:
            dynamic.append({"file": rel, "line": ln})
        hits = [h for h in sc.hits if h["kind"] != "t"]
        if hits:
            per_file[rel] = hits
        for key, default, line in sc.t_keys:
            t_keys.append({"file": rel, "line": line, "key": key, "default": default})
    return per_file, t_keys, all_strings, dynamic


def check_keys(t_keys, all_strings, dynamic):
    tp = ROOT / "resources" / "ui_text.json"
    table = json.loads(tp.read_text(encoding="utf-8"))
    used, indirect = {}, {}
    for it in t_keys:
        used.setdefault(it["key"], []).append(it)
    # 间接引用：key 作为数据传给辅助函数（`_row("hk.screenshot", "截图", ...)` 里
    # 辅助函数内部才调 T(key)）。这类 key 会以**字符串常量**的形式出现在代码里，
    # 故「出现在任意字符串常量中」即视为已引用——比「只认 T( 的直接参数」准确得多。
    for k in table:
        if k not in used and k in all_strings:
            indirect[k] = True

    mismatched, missing = [], []
    for key, items in used.items():
        if key not in table:
            missing.append({"key": key, "first": items[0], "count": len(items)})
        else:
            want = table[key]
            for it in items:
                if it["default"] and it["default"] != want:
                    mismatched.append({"key": key, "file": it["file"],
                                       "line": it["line"], "code": it["default"],
                                       "json": want})
    for key, items in used.items():
        defs = {it["default"] for it in items if it["default"]}
        if len(defs) > 1:
            mismatched.append({"key": key, "file": "(多处)", "line": 0,
                               "code": " / ".join(sorted(defs)),
                               "json": table.get(key, "<无>")})
    unused = [k for k in table if k not in used and k not in indirect]
    return table, used, mismatched, missing, unused, indirect


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="也列出日志/内部异常消息（默认只看 UI 与疑似 UI）")
    ap.add_argument("--json", help="把结果写到该文件")
    ap.add_argument("--check", action="store_true",
                    help="巡检/守卫模式：有未提取的 UI 类字面量或键漂移则以退出码 1 结束")
    args = ap.parse_args()

    per_file, t_keys, all_strings, dynamic = scan_literals()
    table, used, mismatched, missing, unused, indirect = check_keys(
        t_keys, all_strings, dynamic)

    print("== A. 未提取的中文字面量 ==")
    shown = {"ui", "maybe-ui"} if not args.all else {"ui", "maybe-ui", "other", "log"}
    total = {"ui": 0, "maybe-ui": 0, "other": 0, "log": 0}
    for f, hits in per_file.items():
        for h in hits:
            total[h["kind"]] = total.get(h["kind"], 0) + 1
        pick = [h for h in hits if h["kind"] in shown]
        if pick:
            print(f"  {f}  （UI {sum(1 for h in hits if h['kind'] == 'ui')}"
                  f" / 疑似 {sum(1 for h in hits if h['kind'] == 'maybe-ui')}"
                  f" / 其它 {sum(1 for h in hits if h['kind'] == 'other')}"
                  f" / 日志 {sum(1 for h in hits if h['kind'] == 'log')}）")
            for h in sorted(pick, key=lambda x: x["line"]):
                print(f"     {h['line']:>5} [{h['kind']:>8}] {h['text']}")
    print(f"  合计：UI {total.get('ui', 0)}、疑似 UI {total.get('maybe-ui', 0)}、"
          f"其它 {total.get('other', 0)}、日志 {total.get('log', 0)}")

    print("\n== B. T() 键一致性 ==")
    print(f"  ui_text.json {len(table)} 条；直接引用 {len(used)} 个 key"
          f"（{len(t_keys)} 处 T() 调用），间接引用 {len(indirect)} 个"
          f"（key 作为数据传给辅助函数）；动态 key {len(dynamic)} 处")
    if mismatched:
        print(f"  [!] 代码默认值与 JSON 不一致 {len(mismatched)} 处：")
        for m in mismatched[:40]:
            print(f"     {m['file']}:{m['line']} {m['key']}  "
                  f"代码={m['code']!r} JSON={m['json']!r}")
    else:
        print("  [OK] 无漂移")
    if missing:
        print(f"  [i] JSON 缺 key {len(missing)} 个（运行期回退代码默认值）：")
        for m in missing[:40]:
            print(f"     {m['key']}  ← {m['first']['file']}:{m['first']['line']}"
                  f"  默认={m['first']['default']!r}")
    if unused:
        print(f"  [i] JSON 有、代码未引用 {len(unused)} 个（疑似残留）：")
        for k in unused[:40]:
            print(f"     {k} = {table[k]!r}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"literals": per_file, "t_keys": t_keys, "mismatched": mismatched,
             "missing": missing, "unused": unused, "table": table},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已写入 {args.json}")

    if args.check:
        ui_left = total.get("ui", 0) + total.get("maybe-ui", 0)
        if ui_left or mismatched:
            print(f"  [check] 失败：未提取 UI 类字面量 {ui_left} 处、"
                  f"键漂移 {len(mismatched)} 处")
            return 1
        print("  [check] 通过：无未提取 UI 类字面量、无键漂移")
    return 0


if __name__ == "__main__":
    sys.exit(main())
