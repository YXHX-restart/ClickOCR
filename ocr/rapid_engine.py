"""RapidOCR 引擎实现 —— M1-1。

支持两套离线模型集（按 config [ocr] model_set 选择）：
- v6-tiny（默认）：统一 `rapidocr` 包（>=3.9.2，OCRVersion.PPOCRV6）
  + PP-OCRv6 tiny ONNX（resources/models_v6/，ModelScope 官方导出，SHA256 校验）。
  v6 无独立 cls 模型，强制 use_cls=False。
- mobile（旧/回退）：`rapidocr_onnxruntime`（1.x） + PP-OCRv3 mobile ONNX
  （resources/models/ 的 config.yaml bundle）。

适配器同时兼容两种返回格式：
- 统一 rapidocr 包：RapidOCROutput（.boxes / .txts / .scores）
- 旧 rapidocr_onnxruntime 包：(result, elapse)，result 为 [[box, text, score], ...]
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from .types import OcrBox

# 新统一包（PP-OCRv6 支持）：rapidocr 3.9.x
try:
    from rapidocr import RapidOCR as _RapidOCR_NEW
    from rapidocr import EngineType, ModelType, OCRVersion
    _NEW = True
except Exception:  # pragma: no cover - 依赖缺失
    _RapidOCR_NEW = None
    _NEW = False

# 旧包（PP-OCRv3 mobile 回退）：rapidocr-onnxruntime 1.x
try:
    from rapidocr_onnxruntime import RapidOCR as _RapidOCR_OLD
except Exception:  # pragma: no cover - 依赖缺失
    _RapidOCR_OLD = None

_RES = Path(__file__).resolve().parent.parent / "resources"
_CFG_V3 = _RES / "models" / "config.yaml"
_DIR_V6 = _RES / "models_v6"


def _physical_cores() -> int:
    """物理核心数近似（逻辑核/2），避免 ONNX Runtime 默认全逻辑核导致的超订开销。"""
    try:
        n = os.cpu_count() or 4
        return max(1, n // 2)
    except Exception:
        return -1


class RapidOcrEngine:
    def __init__(self, config_path=None, use_cls: bool = True,
                 model_set: str = "v6-tiny", use_cuda: bool = False,
                 use_dml: bool = False, lazy: bool = False):
        self._use_cls = use_cls
        self._model_set = model_set
        self._use_cuda = use_cuda
        self._use_dml = use_dml
        self._gpu_active = False
        self._lazy = lazy
        self._kind = "v6" if (model_set == "v6-tiny" and _NEW and _DIR_V6.exists()) else "v3"
        # 懒加载：引擎对象先建（参数已存），模型等首次 recognize 时才真正构建，
        # 避免「OCR 引擎常驻」关闭时启动即占用 ~200-400MB 内存。
        self._engine = None
        if not lazy:
            self._ensure_engine(config_path)

    def _ensure_engine(self, config_path=None):
        """首次识别前构建底层引擎（懒加载用，幂等）。"""
        if self._engine is not None:
            return self._engine
        if self._kind == "v6":
            self._engine = self._build_v6()
        else:
            self._engine = self._build_v3(config_path)
        return self._engine

    # ---- 构建 ----
    def _build_v6(self):
        params = {
            "Global.use_cls": False,                    # v6 无独立方向分类
            "EngineConfig.onnxruntime.intra_op_num_threads": _physical_cores(),
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Det.ocr_version": OCRVersion.PPOCRV6,
            "Det.model_type": ModelType.TINY,
            "Det.model_path": str(_DIR_V6 / "PP-OCRv6_det_tiny.onnx"),
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Rec.ocr_version": OCRVersion.PPOCRV6,
            "Rec.model_type": ModelType.TINY,
            "Rec.model_path": str(_DIR_V6 / "PP-OCRv6_rec_tiny.onnx"),
            "Rec.rec_keys_path": str(_DIR_V6 / "ppocrv6_tiny_dict.txt"),
        }
        # GPU provider 选择：CUDA（NVIDIA）或 DirectML（AMD/Intel/通用）；
        # 不可用时 onnxruntime 自动回退 CPU（不抛错）
        if self._use_cuda:
            params["EngineConfig.onnxruntime.providers"] = [
                "CUDAExecutionProvider", "CPUExecutionProvider"]
        elif self._use_dml:
            params["EngineConfig.onnxruntime.providers"] = [
                "DmlExecutionProvider", "CPUExecutionProvider"]
        engine = _RapidOCR_NEW(params=params)
        self._gpu_active = self._probe_gpu()
        return engine

    def _probe_gpu(self) -> bool:
        """用现成 v6 模型建 session 探测所选 GPU provider 是否真正可用。"""
        if not (self._use_cuda or self._use_dml):
            return False
        ep = "CUDAExecutionProvider" if self._use_cuda else "DmlExecutionProvider"
        try:
            import onnxruntime as ort
            if ep not in ort.get_available_providers():
                return False
            model_path = _DIR_V6 / "PP-OCRv6_det_tiny.onnx"
            sess = ort.InferenceSession(
                str(model_path),
                providers=[ep, "CPUExecutionProvider"])
            return sess.get_providers()[0] == ep
        except Exception:
            return False

    def _build_v3(self, config_path):
        if _RapidOCR_OLD is None:
            raise RuntimeError(
                "未找到可用 OCR 后端：rapidocr(3.9+) 或 rapidocr-onnxruntime 均不可用")
        kwargs = {}
        cfg = config_path or (str(_CFG_V3) if _CFG_V3.exists() else None)
        if cfg is not None:
            kwargs["config_path"] = cfg
        try:
            return _RapidOCR_OLD(**kwargs)
        except TypeError:  # 个别版本不接受 config_path
            return _RapidOCR_OLD()

    # ---- 识别 ----
    def recognize(self, image: np.ndarray) -> list[OcrBox]:
        engine = self._ensure_engine()
        use_cls = self._use_cls if self._kind == "v3" else False
        out = engine(image, use_det=True, use_cls=use_cls, use_rec=True)
        result = out[0] if isinstance(out, tuple) else out

        # 统一包：boxes(N,4,2) / txts / scores
        boxes = getattr(result, "boxes", None)
        if boxes is not None:
            return [
                OcrBox(
                    text=str(t),
                    polygon=np.asarray(pts, dtype=int).tolist(),
                    score=float(s),
                )
                for pts, t, s in zip(boxes, result.txts, result.scores)
            ]

        # 旧包：[[box, text, score], ...]
        ocr_boxes = []
        for item in result:
            box, text, score = item[0], item[1], item[2]
            poly = [[int(float(p[0])), int(float(p[1]))] for p in box]
            ocr_boxes.append(OcrBox(text=str(text), polygon=poly, score=float(score)))
        return ocr_boxes

    def get_info(self) -> dict:
        if self._kind == "v6":
            return {
                "engine": "RapidOCR(ONNX)",
                "model_set": "PP-OCRv6-tiny",
                "device": "cuda" if self._gpu_active else "cpu",
                "gpu_available": self._gpu_active,
            }
        return {
            "engine": "RapidOCR(ONNX)",
            "model_set": "PP-OCRv3-mobile",
            "device": "cuda" if self._gpu_active else "cpu",
            "gpu_available": self._gpu_active,
        }
