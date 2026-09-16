"""图片编码：cv2 原生格式 + Pillow 扩展格式（HEIC/HEIF/AVIF）。

cv2 能编 png/jpg/bmp/webp，但**编不了** heic/heif/avif（实测 cv2 5.0 三种全 FAIL）；
这三种走 Pillow：
  - HEIC/HEIF：`pillow_heif` 注册 opener 后 `Image.save(format="HEIF")`；缺依赖返回 None
  - AVIF：Pillow 11.3+ 内置（本项目 12.3 已确认）

与 `core_engine.image_decode` 对称但**刻意不放进共享层**：解码是桌面端/服务端共用的
契约（要过漂移哨兵），而「按用户所选格式落盘历史截图」只服务桌面端历史记录，
放 core_engine 只会让服务端多背一个编码依赖。
"""
from __future__ import annotations

import io

import numpy as np

from core_engine.logging_setup import get_logger

log = get_logger("app")

#: 必须走 Pillow 的格式（cv2 不支持编码）
PILLOW_FORMATS = ("heic", "heif", "avif")


def pillow_encode(img: np.ndarray, fmt: str, quality: int = 90) -> bytes | None:
    """BGR ndarray → 图片字节。依赖缺失 / 编码失败返回 None。

    fmt: heic / heif / avif；quality: 1-100（有损质量）。
    「无 pillow_heif」是预期内的能力缺失，静默返回 None；**其余编码失败必须留痕**
    （裸 `except: return None` 会让「换了个格式就存不下来」变成无迹可查）。
    调用方仍需处理 None（跳过该帧 / 回退格式）。
    """
    try:
        from PIL import Image

        from core_engine.image_decode import register_heif_opener
        if fmt in ("heic", "heif") and not register_heif_opener():
            return None                     # 无 pillow_heif → 编不了 HEIC/HEIF
        # BGR -> RGB（截图来自 cv2，统一按 BGR 输入）；转连续内存再交给 Pillow
        pil = Image.fromarray(np.ascontiguousarray(img[:, :, ::-1]))
        out = io.BytesIO()
        pil.save(out,
                 format=("HEIF" if fmt in ("heic", "heif") else "AVIF"),
                 quality=max(1, min(100, int(quality))))
        return out.getvalue()
    except Exception as e:                  # noqa: BLE001 编码失败不炸调用方
        log.warning("Pillow 编码 %s 失败（返回 None，调用方需回退/跳过）：%s",
                    fmt, e, exc_info=True)
        return None
