"""lint 门禁：把 `ruff check` 并进单测入口（与 `test_guards` 同一思路）。

为什么要有门禁：本仓的问题是「约定已建立、执行靠自觉」—— 同一类问题（未使用
导入、未定义名字、静默截断的 zip）被反复审查报出来，就因为改完没有任何东西
立刻拦住。这里用子进程跑 ruff，把「只能靠人记」变成「一条命令必然执行」。

**未装 ruff 时跳过而不是失败**：本套件还要服务于「只装了运行依赖」的环境
（例如只跑功能回归的机器）。开发机请装 `requirements-dev.txt`。
规则集与豁免理由见仓库根的 `ruff.toml`。
"""
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _ruff_cmd():
    """优先 `python -m ruff`（不依赖 PATH），退回 PATH 上的 ruff；都没有返回 None。"""
    try:
        r = subprocess.run([sys.executable, "-m", "ruff", "--version"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return [sys.executable, "-m", "ruff"]
    except Exception:      # noqa: BLE001 ruff 缺失/无法执行 → 试 PATH
        pass
    exe = shutil.which("ruff")
    return [exe] if exe else None


class TestLint(unittest.TestCase):
    def test_ruff_clean(self):
        cmd = _ruff_cmd()
        if cmd is None:
            self.skipTest("未安装 ruff（开发机请装 requirements-dev.txt）")
        proc = subprocess.run(
            [*cmd, "check", "--no-cache", "--output-format", "concise"],
            cwd=str(_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)
        detail = "\n".join((proc.stdout or "").strip().splitlines()[:40])
        self.assertEqual(
            proc.returncode, 0,
            "ruff 发现问题（规则与豁免理由见 ruff.toml）：\n" + detail
            + (f"\n--- stderr ---\n{(proc.stderr or '')[-500:]}"
               if proc.stderr else ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
