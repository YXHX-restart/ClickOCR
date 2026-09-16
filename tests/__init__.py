"""tests 包引导：把仓库根加入 sys.path，并让 Qt 走无头平台。

`scripts/` 下是一堆「改完手动跑一遍」的验证脚本；本目录是它们中**可离线、可重复、
有断言**的那部分的正式归属（见 docs/design/重构方案-App与UI大文件拆分.md 与
docs/code_review_2026-09-16.md）。运行：

    .venv\\Scripts\\python -m unittest discover -s tests -t . -v      # 全部
    .venv\\Scripts\\python tests\\test_db_manager.py                  # 单个

约定：
- **离线**：不加载 OCR 模型、不依赖真实显示器、不写用户配置与历史库。
  需要模型/显示环境的用 `unittest.skipUnless` 明确跳过，而不是默默失败。
- 用标准库 unittest（项目不依赖 pytest；装了 pytest 也能直接采集 TestCase）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Qt 无头：本套测试不依赖真实显示器（需要真桌面 + 人眼的仍留在 scripts/ 手跑）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
