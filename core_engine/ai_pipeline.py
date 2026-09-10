"""core_engine.ai_pipeline — OCR 引擎抽象与数据结构（业务层契约，无 Qt 依赖）。

对应需求 §3.4 / §13.4 的统一接口契约。业务层与 UI 层只依赖 OcrEngine Protocol，
不感知底层是 RapidOCR / PaddlePaddle 还是其他引擎；更换引擎仅新增实现类 + 工厂注入。

里程碑归属（§3.4）：recognize + get_info → M1；switch_model → M2；
recognize_table / load_table_model / unload_table_model → M3；switch_device → M4。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Protocol, runtime_checkable

import numpy as np


@dataclass
class OcrBox:
    """单个文字块识别结果。"""
    text: str                          # 识别文本
    polygon: List[List[int]]           # 4 顶点坐标 [[x, y], ...]
    score: float                       # 置信度 0~1
    kind: str = "ocr"                  # 来源标记：ocr=文字 | qr=二维码/条码（M2+++）


@dataclass
class TableCell:
    """表格单元格。"""
    text: str
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    polygon: List[List[int]] = field(default_factory=list)
    score: float = 0.0


@dataclass
class TableResult:
    """表格识别结果。"""
    cells: List[TableCell]
    row_count: int
    col_count: int
    html: str = ""
    markdown: str = ""
    score: float = 0.0


@runtime_checkable
class OcrEngine(Protocol):
    """OCR 引擎抽象接口。M1 已实现 recognize + get_info；M2 增 switch_model。"""

    def recognize(self, image: np.ndarray) -> List[OcrBox]:
        """输入 BGR 图像（numpy 数组），返回带坐标的文字块列表。"""
        ...

    def get_info(self) -> dict:
        """返回引擎元信息：名称 / 模型集 / 档位 / 设备 / GPU 可用性等。"""
        ...

    def switch_model(self, model_name: str) -> None:
        """M2 — 切换模型档位：tiny / small / medium。"""
        ...


# ---- 模型档位映射（RapidOCR ONNX 三档，M2）----
TIER_NAMES = ("tiny", "small", "medium")

# model_set(config) -> 标准档位名（PP-OCRv3 mobile 回退已移除，2026-08-27）
MODEL_SET_TO_TIER = {
    "v6-tiny": "tiny",
    "v6-small": "small",
    "v6-medium": "medium",
    "server": "medium",
}


# ============ 表格识别接口契约（§7 / M2 由 M4 提前）============
@runtime_checkable
class TableEngineProtocol(Protocol):
    """表格识别引擎抽象接口（业务层只依赖此接口）。"""

    def load_table_model(self) -> None:
        """惰性加载 SLANet 表格模型（首次调用约 2-3s）。"""
        ...

    def unload_table_model(self) -> None:
        """卸载表格模型释放内存（约 150-200MB）。"""
        ...

    def recognize_table(self, image: np.ndarray) -> TableResult:
        """输入 BGR numpy 图像，返回 TableResult（cells/html/markdown）。"""
        ...

    def get_info(self) -> dict:
        """返回表格引擎元信息：model_type / device / loaded 等。"""
        ...


# ============ 表格导出接口契约（§7.5）============
@runtime_checkable
class TableExportServiceProtocol(Protocol):
    """表格结果导出服务接口（Excel / Markdown / TSV / HTML）。"""

    def export_excel(self, table: TableResult, file_path: str) -> None:
        """导出 Excel (.xlsx)，保留行列结构与合并单元格。"""
        ...

    def export_markdown(self, table: TableResult, file_path: str) -> None:
        """导出 Markdown (.md)。"""
        ...

    def to_tsv(self, table: TableResult) -> str:
        """转为 TSV 字符串（Tab 分隔），用于复制到剪贴板。"""
        ...

    def to_html(self, table: TableResult) -> str:
        """转为 HTML 表格字符串，用于预览面板渲染。"""
        ...
