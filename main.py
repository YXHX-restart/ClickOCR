"""ClickOCR 入口（M1 完整版 + M2 core_engine 接入）。

运行：python main.py
  - 托盘常驻（最小化到托盘，不弹主窗）
  - 全局热键 Ctrl+Shift+R 触发框选
  - 框选 → 截图 → OCR → 贴图浮窗（点击文字即复制）
  - OCR 完成后自动保存历史（screenshot_history + 截图/缩略图，M2）

core_engine 定位（自包含优先）：
  - 优先用 ClickOCR 内置的 `ClickOCR/core_engine/`（vendor 副本），
    这样 ClickOCR 文件夹整体移动/分发到任何位置都能独立运行。
  - 找不到内置副本时，兼容旧布局：core_engine 放在 ClickOCR 的上一级。
"""
from __future__ import annotations

import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
_internal_ce = _APP_DIR / "core_engine"
if (_internal_ce / "__init__.py").exists():
    # 自包含：用 ClickOCR 内部的 core_engine（移动即用）
    sys.path.insert(0, str(_internal_ce))
else:
    # 兼容旧布局：core_engine 位于 ClickOCR 上一级（如 E:\\WorkSpace\\core_engine）
    sys.path.insert(0, str(_APP_DIR.parent))

from app import App  # noqa: E402


def main():
    # 软件根目录（开发 = 本文件目录；打包后 = _internal，见 ConfigManager）
    base = Path(__file__).resolve().parent
    App(app_root=base).run()


if __name__ == "__main__":
    main()
