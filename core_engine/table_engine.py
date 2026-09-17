"""core_engine.table_engine — 表格识别引擎（SLANet / RapidTable，无 Qt 依赖）。

对应需求 §7（表格识别）+ §3.4 的表格接口契约：
- recognize_table / load_table_model / unload_table_model → M2（由 M4 提前）
- 底层用 RapidTable（RapidAI，PP-Structure SLANet 序列预测），模型 slanet-plus.onnx
  已随 rapid_table whl 内置（~6.8MB），无需联网下载。
- 单元格文字复用 RapidOCR（本项目 v6-tiny 引擎），表格结构 → 文字匹配由 RapidTable 完成。
- 支持 GPU：engine_cfg.use_cuda=True 时走 CUDAExecutionProvider，无 GPU 自动回退 CPU。

数据结构 TableCell / TableResult 在 ai_pipeline 定义（§3.4 契约）。
"""
from __future__ import annotations

import threading
import time
from typing import List, Optional

import numpy as np

from .ai_pipeline import TableCell, TableResult

# RapidTable（v3.x，随包内置 slanet-plus 模型）
try:
    from rapid_table import RapidTable
    from rapid_table.utils import EngineType, ModelType, RapidTableInput
    _RT_AVAILABLE = True
except Exception:  # pragma: no cover - 依赖缺失
    RapidTable = None
    EngineType = ModelType = RapidTableInput = None
    _RT_AVAILABLE = False


class TableEngine:
    """表格结构识别引擎。

    惰性加载：首次 recognize_table 才构建 RapidTable 实例（约 2-3s），
    之后常驻复用；unload_table_model 释放，供设置面板手动卸载。
    """

    def __init__(self, ocr_recognize=None, use_cuda: bool = False,
                 use_dml: bool = False, use_cls: bool = False,
                 model_type: str = "slanet_plus"):
        """
        Args:
            ocr_recognize: callable(np.ndarray) -> list[OcrBox]，单元格文字识别回调
                （注入 RapidOcrEngine.recognize 或等价实现，避免依赖具体类）。
            use_cuda: 是否优先尝试 CUDAExecutionProvider（无 GPU 自动回退）。
            use_dml: 是否优先尝试 DmlExecutionProvider（AMD/Intel/通用，无则回退）。
            use_cls: 传给内部 OCR 的 cls 开关（RapidTable 默认自带 OCR，本项目外部注入）。
            model_type: slanet_plus | ppstructure_zh | ppstructure_en | unitable
        """
        self._ocr_recognize = ocr_recognize
        self._use_cuda = use_cuda
        self._use_dml = use_dml
        self._model_type = model_type
        self._table: Optional[RapidTable] = None
        self._loaded = False
        self._load_lock = threading.Lock()   # 保护「惰性加载」，防并发建两套 session
        self._load_elapse = 0.0

    # ---- 生命周期 ----
    def load_table_model(self) -> None:
        """加载 SLANet 表格模型（惰性，幂等）。失败抛 RuntimeError 由调用方降级。

        **双检锁**：`_loaded` 是个普通布尔，两个线程同时首次调用时会双双通过
        `if self._loaded: return`，各建一个 RapidTable session（每个约 150-200MB，
        且两套 ort session 各占一份显存）。加载本身是秒级操作，锁的代价可忽略。
        """
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:                 # 双检：等锁期间别人已加载好
                return
            if not _RT_AVAILABLE:
                raise RuntimeError("未安装 rapid_table，表格识别不可用（pip install rapid_table）")
            t0 = time.perf_counter()
            cfg = RapidTableInput(
                model_type=ModelType(self._model_type),
                use_ocr=True,               # 必须 True：use_ocr=False 时不生成 pred_htmls
                ocr_params={},              # 外部 ocr_results 优先；空参数仅作内部兜底
                engine_cfg={"use_cuda": self._use_cuda, "use_dml": self._use_dml},
            )
            self._table = RapidTable(cfg=cfg)
            self._load_elapse = time.perf_counter() - t0
            self._loaded = True

    def unload_table_model(self) -> None:
        """卸载表格模型释放内存（约 150-200MB）。"""
        with self._load_lock:                # 与加载互斥：别把刚建好的 session 漏掉
            self._table = None
            self._loaded = False

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    # ---- 识别 ----
    def recognize_table(self, image: np.ndarray) -> TableResult:
        """输入 BGR numpy 图像，返回表格结构识别结果 TableResult。

        内部流程：SLANet 预测表格结构（HTML 序列）→ 外部分词回调得到单元格文字
        → RapidTable 将结构框与文字匹配 → 解析为 TableResult。
        """
        if not self._loaded:
            self.load_table_model()
        # 取本地快照再用：unload_table_model 可能在**另一线程**把 self._table 置
        # None（配额裁剪 / 退出流程），原先直接 self._table(image) 会 AttributeError
        # （'NoneType' object is not callable）。快照让对象在本函数内保持存活。
        tbl = self._table
        if tbl is None:
            raise RuntimeError("表格模型不可用")

        # 1) 单元格文字：复用外部 OCR（v6-tiny），输出 (boxes, texts, scores) 元组列表
        ocr_results = None
        boxes = []   # 预先绑定：_parse_cells 会用到；ocr_recognize 未注入时保持空
        if self._ocr_recognize is not None:
            boxes = self._ocr_recognize(image)
            if boxes:
                pts = np.asarray([b.polygon for b in boxes], dtype=np.float32)
                txts = tuple(b.text for b in boxes)
                scores = tuple(float(b.score) for b in boxes)
                ocr_results = [(pts, txts, scores)]

        # 2) 表格结构识别 + 文字匹配
        out = tbl(image, ocr_results=ocr_results)

        html = (out.pred_htmls or [""])[0]
        cell_boxes = (out.cell_bboxes or [np.empty((0, 4, 2), dtype=np.float32)])[0]

        # 3) 解析 HTML 表格 → 行列网格
        rows, cols = self._grid_size(html)
        cells = self._parse_cells(html, cell_boxes)

        return TableResult(
            cells=cells,
            row_count=rows,
            col_count=cols,
            html=html,
            markdown=self._to_markdown(html),
            score=1.0 if html else 0.0,
        )

    # ---- HTML 解析 ----
    @staticmethod
    def _grid_size(html: str) -> tuple[int, int]:
        """从预测 HTML 推断行列数（以表格数据行中最大单元格数近似列数）。"""
        if not html:
            return 0, 0
        rows = 0
        max_cols = 0
        # 按行拆（<tr>...</tr>）
        import re
        trs = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
        for tr in trs:
            rows += 1
            n = len(re.findall(r"<t[dh][^>]*>", tr, re.I))
            max_cols = max(max_cols, n)
        return max(rows, 1), max(max_cols, 1)

    def _parse_cells(self, html: str,
                     cell_boxes: np.ndarray) -> List[TableCell]:
        """解析 HTML 为 TableCell 网格（含行列、跨行跨列、坐标、文字）。"""
        import re
        cells: List[TableCell] = []
        if not html:
            return cells

        def _cell_poly(ri: int, ci: int, idx: int) -> List[List[int]]:
            """取单元格坐标框。rapid_table 的 cell_bboxes 可能形状：
            [N,8]（每格 4 点展平 x1y1..x4y4，实测）、[N,4,2] 或 [R,C,4,2]。"""
            try:
                if getattr(cell_boxes, "ndim", 0) >= 4:
                    pts = cell_boxes[ri][ci]
                else:
                    pts = cell_boxes[idx]
                pts = np.asarray(pts)
                if pts.size == 8:
                    flat = pts.reshape(-1).astype(float)
                    return [[int(flat[i]), int(flat[i + 1])]
                            for i in range(0, 8, 2)]
                if pts.ndim == 2 and pts.shape[1] == 2:
                    return [[int(p[0]), int(p[1])] for p in pts]
                return []
            except Exception:
                return []

        trs = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
        cell_idx = 0
        for ri, tr in enumerate(trs):
            tds = re.findall(
                r"<t[dh](\s[^>]*)?>(.*?)</t[dh]>", tr, re.S | re.I)
            for ci, (attrs, content) in enumerate(tds):
                rowspan = int(re.search(r"rowspan=['\"](\d+)", attrs or "", re.I).group(1)) \
                    if re.search(r"rowspan=['\"](\d+)", attrs or "", re.I) else 1
                colspan = int(re.search(r"colspan=['\"](\d+)", attrs or "", re.I).group(1)) \
                    if re.search(r"colspan=['\"](\d+)", attrs or "", re.I) else 1
                # 去 HTML 标签取纯文本
                text = re.sub(r"<[^>]+>", "", content).strip()
                poly = _cell_poly(ri, ci, cell_idx)
                cell_idx += 1
                cells.append(TableCell(
                    text=text, row=ri, col=ci,
                    row_span=rowspan, col_span=colspan,
                    polygon=poly, score=1.0 if text else 0.0,
                ))
        return cells

    @staticmethod
    def _to_markdown(html: str) -> str:
        """HTML 表格 → Markdown 表格（| 分隔，--- 表头）。"""
        import re
        if not html:
            return ""
        lines: List[str] = []
        trs = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
        for idx, tr in enumerate(trs):
            tds = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
            cells_txt = [re.sub(r"<[^>]+>", "", c).strip().replace("|", "\\|") for c in tds]
            lines.append("| " + " | ".join(cells_txt) + " |")
            if idx == 0:
                lines.append("|" + "---|" * len(cells_txt))
        return "\n".join(lines)

    # ---- 元信息 ----
    @staticmethod
    def _effective_device(use_cuda: bool, use_dml: bool) -> str:
        """报告本次构建用的执行设备（cuda / dml / cpu）。

        原先只写 `"cuda" if (self._use_cuda and self._loaded) else "cpu"`：
        - **DML 用户永远显示 cpu**（`use_dml` 完全没看）—— 这条会经
          `_ocr_runtime_info` 写进历史记录的「执行记录」字段，等于记错账；
        - 勾了 cuda 但环境里根本没有 CUDA EP 时也会谎报 cuda
          （onnxruntime 会静默回退 CPU）。

        这里查一次 onnxruntime 实际可用的 EP（结果由 ORT 内部缓存，开销可忽略）。
        **注意**：它证明的是「该 EP 在本环境可用」，不等于「本 session 一定拿到了
        它」—— RapidTable 内部建 session 的 provider 落位我们无法从外部读到。
        比改前准确得多（DML 不再误报 cpu），但仍不是「实测落位」，故不写成
        「已验证」。
        """
        if not (use_cuda or use_dml):
            return "cpu"
        want = "CUDAExecutionProvider" if use_cuda else "DmlExecutionProvider"
        try:
            import onnxruntime as ort
            if want in ort.get_available_providers():
                return "cuda" if use_cuda else "dml"
        except Exception:            # noqa: BLE001 ORT 缺失/异常 → 按最保守的报
            pass
        return "cpu"

    def get_info(self) -> dict:
        return {
            "engine": "RapidTable(SLANet)",
            "model_type": self._model_type,
            "model_size_mb": 6.8,
            "device": self._effective_device(self._use_cuda, self._use_dml),
            "loaded": self._loaded,
            "load_elapse_s": round(self._load_elapse, 2),
        }
