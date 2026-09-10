"""core_engine.logging_setup — 结构化日志（5MB × 3 轮转，无 Qt 依赖）。

用法：ClickOCR / AI-Gallery 启动时调用 setup_logging()，
日志文件 `./logs/clickocr.log`（按 app_root 解析）。
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "clickocr"
_INITIALIZED = False


def setup_logging(app_root: str | Path | None = None,
                  level: int = logging.INFO,
                  console: bool = True,
                  logs_dir: str | Path | None = None) -> logging.Logger:
    """初始化轮转日志；重复调用幂等。返回应用级 logger。

    logs_dir 优先（直接指定日志目录）；否则用 app_root/logs。
    """
    global _INITIALIZED
    logger = logging.getLogger(_LOGGER_NAME)
    if _INITIALIZED:
        return logger

    if logs_dir is not None:
        log_dir = Path(logs_dir)
    else:
        root = Path(app_root) if app_root else Path.cwd()
        log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "clickocr.log"

    logger.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    _INITIALIZED = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """取子模块 logger（如 get_logger("history") → clickocr.history）。"""
    if name:
        return logging.getLogger(f"{_LOGGER_NAME}.{name}")
    return logging.getLogger(_LOGGER_NAME)
