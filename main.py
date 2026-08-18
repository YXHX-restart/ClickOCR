"""ClickOCR 入口（M1 完整版 + M2 core_engine 接入）。

运行：python main.py
  - 托盘常驻（最小化到托盘，不弹主窗）
  - 全局热键 Ctrl+Shift+R 触发框选
  - 框选 → 截图 → OCR → 贴图浮窗（点击文字即复制）
  - OCR 完成后自动保存历史（screenshot_history + 截图/缩略图，M2）

注：core_engine 为共享包，位于本目录上级（E:\\WorkSpace\\core_engine），
启动时注入 sys.path 以支持 `import core_engine`。
"""
from __future__ import annotations

import sys
from pathlib import Path

# core_engine 共享包路径注入（两软件各自 import，位于同级目录）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import App  # noqa: E402


def main():
    # 软件根目录（开发 = 本文件目录；打包后 = _internal，见 ConfigManager）
    base = Path(__file__).resolve().parent
    App(app_root=base).run()


if __name__ == "__main__":
    main()
