"""ocr —— OCR 引擎实现层。

数据结构与引擎契约（`OcrBox` / `OcrEngine`）**只在底层 `core_engine.ai_pipeline`
定义一份**，此处仅做再导出：原先 `ocr/types.py` 也写了一份同名 Protocol（还缺
`switch_model`），两处定义会各自漂移，已于 2026-09-10 删除。
"""
from core_engine.ai_pipeline import OcrBox, OcrEngine

from .rapid_engine import RapidOcrEngine

__all__ = ["OcrBox", "OcrEngine", "RapidOcrEngine"]
