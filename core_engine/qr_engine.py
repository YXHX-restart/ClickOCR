"""core_engine.qr_engine — 二维码/条码识别（zxing-cpp 封装，无 Qt 依赖）。

输出与 OCR 同构：复用 OcrBox（kind="qr"，polygon 四角 + text 解码内容），
便于并入 ocr_boxes 落库（M2+++ 定稿：内容进 ocr_text、几何进 ocr_boxes(kind="qr")）。
zxing-cpp 未安装 / 识别失败时静默返回空列表（QR 功能降级，其余功能不受影响）。
"""
from __future__ import annotations

import numpy as np

try:
    import zxingcpp
    _ZXING_OK = True
except Exception:  # pragma: no cover - 依赖缺失
    zxingcpp = None
    _ZXING_OK = False

from .ai_pipeline import OcrBox


def qr_decode(image: np.ndarray) -> list:
    """识别图中全部二维码/条码，返回 OcrBox 列表（kind="qr"）。

    image 为 BGR numpy 图像（cv2 格式）；zxing 不可用或识别失败返回空列表。
    角点顺序：左上 → 右上 → 右下 → 左下（与 OcrBox.polygon 约定一致）。
    """
    if not _ZXING_OK or image is None:
        return []
    try:
        rgb = np.ascontiguousarray(image[:, :, ::-1])   # BGR -> RGB
        found = zxingcpp.read_barcodes(rgb)
    except Exception:       # noqa: BLE001 识别失败不阻断主流程
        return []
    out = []
    for b in found or []:
        if b is None:
            continue
        try:
            text = str(b.text or "")
            if not text:
                continue
            pos = b.position
            pts = [(int(round(p.x)), int(round(p.y)))
                   for p in (pos.top_left, pos.top_right,
                             pos.bottom_right, pos.bottom_left)]
            out.append(OcrBox(text=text, polygon=pts,
                              score=1.0, kind="qr"))
        except Exception:   # noqa: BLE001 单码异常跳过
            continue
    return out


def qr_available() -> bool:
    """zxing-cpp 是否可用（设置面板检测用）。"""
    return _ZXING_OK
