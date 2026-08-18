"""ClickOCR 配置管理 —— core_engine.config 的薄兼容层（M2-1 抽取）。

原实现已迁入 `core_engine/config.py`（无 Qt 依赖，供 ClickOCR / AI-Gallery 共用）。
本文件保持 `from config import ConfigManager` 的旧导入路径可用，
并将 app_root 固定为 ClickOCR 根目录（config.ini 位于 resources/ 下）。

段：[general] / [ocr] / [hotkey] / [selection] / [result] /
     [preprocess] / [screenshot] / [db] / [hotkey_ext]
"""
from __future__ import annotations

from pathlib import Path

from core_engine.config import ConfigManager as _BaseConfigManager


class ConfigManager(_BaseConfigManager):
    def __init__(self, path=None, app_root=None):
        super().__init__(app_root=app_root or Path(__file__).parent,
                         path=path)
