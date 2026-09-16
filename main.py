"""ClickOCR 入口（M1 完整版 + M2 core_engine 接入）。

运行：python main.py
  - 托盘常驻（最小化到托盘，不弹主窗）
  - 全局热键 Ctrl+Shift+R 触发框选
  - 框选 → 截图 → OCR → 贴图浮窗（点击文字即复制）
  - OCR 完成后自动保存历史（screenshot_history + 截图/缩略图，M2）

core_engine 定位（自包含）：
  - 只使用 ClickOCR 内置的 `ClickOCR/core_engine/`（vendor 副本），
    这样 ClickOCR 文件夹整体移动/分发到任何位置都能独立运行。
  - 旧布局（core_engine 放在 ClickOCR 上一级）的兼容回退**已移除**（2026-09-10）：
    那份副本已删除，留着只会掩盖「内置副本被误删」这类故障。
  - 打包后 core_engine 被 PyInstaller 打进 exe 内的 PYZ，磁盘上没有该目录，
    因此开发态的缺失检查只在非 frozen 时进行。

**不要把 core_engine/ 加进 sys.path**（2026-09-16 移除）：
  `core_engine/` 已经是一个**包**，`from core_engine.ai_pipeline import ...`
  靠脚本目录（`sys.path[0]`）就能解析。把包目录本身插进 sys.path 只会产生一个
  副作用——把包内每个模块**提升为顶层模块**，于是 `core_engine/config.py` 与根目录
  `config.py` 同名冲突，全项目 13 处 `from config import ConfigManager` 在开发态
  会拿到 `core_engine.config` 那个基类，而不是根目录那个「把 app_root 固定为
  ClickOCR 根目录」的薄兼容层——开发态与打包态行为分叉。
"""
from __future__ import annotations

import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
_CORE_ENGINE = _APP_DIR / "core_engine"

if not getattr(sys, "frozen", False):
    # 开发态：只认内置 vendor 副本；缺失即明确报错，不再静默回退旧布局
    if not (_CORE_ENGINE / "__init__.py").is_file():
        raise SystemExit(
            f"启动失败：找不到内置 core_engine 包（期望：{_CORE_ENGINE}）。\n"
            "ClickOCR 依赖随仓库提供的 ClickOCR/core_engine/，"
            "请补齐该目录后重试。"
        )

from app import App  # noqa: E402


def main():
    # 软件根目录（开发 = 本文件目录；打包后 = _internal，见 ConfigManager）
    base = Path(__file__).resolve().parent
    App(app_root=base).run()


if __name__ == "__main__":
    main()
