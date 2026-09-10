"""ClickOCR 统一数据结构与引擎接口契约（§3.4）。

业务层 / UI 层只依赖 OcrEngine Protocol，不感知底层是 RapidOCR 还是其他引擎。
更换引擎仅需新增一个实现类 + 修改工厂注入点，符合低耦合原则。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Protocol, runtime_checkable

import numpy as np


@dataclass
class OcrBox:
    """单个文字块识别结果。"""
    text: str                          # 识别文本
    polygon: List[List[int]]           # 4 顶点坐标 [[x, y], ...]
    score: float                       # 置信度 0~1
    kind: str = "ocr"                  # 来源标记：ocr=文字 | qr=二维码/条码（M2+++）


@runtime_checkable
class OcrEngine(Protocol):
    """OCR 引擎抽象接口（M1 仅 recognize + get_info）。"""

    def recognize(self, image: np.ndarray) -> List[OcrBox]:
        """输入 BGR numpy 图像，返回带坐标的文字块列表。"""
        ...

    def get_info(self) -> dict:
        """返回引擎元信息：名称 / 模型集 / 设备 / GPU 可用性等。"""
        ...
