"""core_engine — ClickOCR / AI-Gallery 共享引擎层（纯逻辑，无 Qt/UI 依赖）。

子模块：
- config.py      路径 / INI 配置管理
- logging_setup.py 结构化日志（5MB × 3 轮转）
- ai_pipeline.py OCR 引擎抽象与数据结构（业务层契约）
- db_manager.py  SQLite 统一读写（建库 + HistoryStorage 等存储实现）

约束：本包禁止 import PySide6 / PyQt / 任何 UI 框架；两软件各自 import。
"""
from __future__ import annotations

__version__ = "0.2.0"
