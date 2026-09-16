"""把 `scripts/` 下的**断言型守卫**并进单测入口，让 CI 只需要一条命令。

这些脚本本来就是「改完手动跑一遍」的验证器（有断言、失败返回非 0），内容值得留；
但它们不在 `unittest`/pytest 的采集范围内，于是「跑没跑」全靠自觉。这里用子进程
把它们拉进来，并强制 Qt 走无头平台（`tests/__init__.py` 已设 QT_QPA_PLATFORM，
环境变量会被子进程继承）。

需要**真桌面 + 人眼判断**的 UI 冒烟（截图框选、钉图拖拽之类）不在此列，仍留在
`scripts/` 手跑 —— 那些脚本断言不了「看起来对不对」。
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

#: (脚本, 额外参数) —— 全部断言型、可无头运行
GUARDS = [
    ("_verify_review_fixes.py", []),
    ("_verify_special_fixes.py", []),
    ("_verify_theme_tokens.py", []),
    ("_validate_search.py", []),
    ("_i18n_audit.py", ["--check"]),
    ("_test_theme_apply.py", []),
    ("_test_m2a.py", []),
    ("_test_hist_tabs.py", []),
    ("_test_unified_toolbar.py", []),
    ("_test_toolbar_shortcuts.py", []),
    ("_test_ocr_chain.py", []),
    ("_test_anno_structured.py", []),
    ("_test_float_group.py", []),
    ("_test_p0_refactor.py", []),
]


class TestGuards(unittest.TestCase):
    maxDiff = None

    def test_assertion_guards_pass(self):
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        for script, extra in GUARDS:
            path = _ROOT / "scripts" / script
            with self.subTest(guard=script):
                self.assertTrue(path.exists(), f"守卫脚本不存在：{path}")
                proc = subprocess.run(
                    [sys.executable, str(path), *extra],
                    cwd=str(_ROOT), env=env, capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=300)
                tail = "\n".join((proc.stdout or "").strip().splitlines()[-12:])
                self.assertEqual(
                    proc.returncode, 0,
                    f"{script} 退出码 {proc.returncode}\n"
                    f"--- 末尾输出 ---\n{tail}\n"
                    f"--- stderr ---\n{(proc.stderr or '')[-800:]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
