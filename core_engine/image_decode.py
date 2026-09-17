"""core_engine/image_decode — 统一图片解码（cv2 主路径 + Pillow 回退）。

放在 core_engine（无 Qt、无平台依赖），供桌面端（ClickOCR）与服务端
（clickocr-server）共用同一套解码逻辑，避免两处实现漂移。

支持范围：
  - cv2.imdecode 原生：jpg / png / bmp / webp / tiff / ...
  - Pillow >= 11.3 内置 AVIF：.avif（零额外依赖）
  - pillow_heif 注册 opener 后：.heic / .heif / .hif（wheel 自带 libheif）

EXIF 方向处理是这里的重点（踩过的坑）：
  - pillow_heif 的 to_pillow() 会把 EXIF orientation **重置为 1**，真实方向藏进
    image.info["original_orientation"] 且不自动旋转 → 标准 ImageOps.exif_transpose
    对 HEIC 无效，必须读该字段手动补旋转，否则 iPhone 拍的 HEIC 会躺倒、
    OCR 文字顺序错乱。
  - 其余格式（AVIF / JPEG / TIFF）EXIF 正常保留，走 ImageOps.exif_transpose。

依赖缺失策略：pillow_heif 未装时 HEIC/HEIF 静默不可用（返回 None 由调用方跳过），
不影响其它格式，也不在导入期抛异常。
"""
from __future__ import annotations

import numpy as np

try:                                                   # cv2 必选（推理链已依赖）
    import cv2
except Exception:                                      # pragma: no cover
    cv2 = None                                          # type: ignore

_HEIF_REGISTERED = False

# EXIF orientation → PIL 变换（与 PIL.ImageOps.exif_transpose 的映射一致）
_ORIENTATION_METHODS = {
    2: "FLIP_LEFT_RIGHT",
    3: "ROTATE_180",
    4: "FLIP_TOP_BOTTOM",
    5: "TRANSPOSE",
    6: "ROTATE_270",        # 顺时针 90°
    7: "TRANSVERSE",
    8: "ROTATE_90",         # 逆时针 90°
}


def register_heif_opener() -> bool:
    """幂等注册 pillow_heif 的 HEIC/HEIF opener；未安装返回 False（不抛异常）。"""
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return True
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:                                   # noqa: BLE001
        return False
    _HEIF_REGISTERED = True
    return True


def heif_supported() -> bool:
    """当前环境是否支持 HEIC/HEIF 解码。"""
    return register_heif_opener()


def apply_exif_orientation(im, orientation):
    """按 EXIF orientation 值旋转 / 翻转（映射与 PIL.ImageOps.exif_transpose 一致）。"""
    from PIL import Image
    name = _ORIENTATION_METHODS.get(int(orientation))
    if name is None:
        return im
    method = getattr(Image.Transpose, name, None)
    return im.transpose(method) if method is not None else im


def _as_buffer(data):
    """bytes / bytearray / memoryview / ndarray → cv2 可解码的 uint8 缓冲。"""
    if isinstance(data, np.ndarray):
        return data
    return np.frombuffer(data, dtype=np.uint8)


def decode_image_bytes(data) -> "np.ndarray | None":
    """图片字节 → BGR ndarray（与 cv2.imdecode 输出一致），失败返回 None。

    先走 cv2.imdecode（常规格式最快）；失败再回退 Pillow 覆盖 AVIF / HEIC。

    「失败返回 None」是硬契约（调用方按 None 跳过，不做异常处理）：因此
    **整个解码入口都在 try 之内**——缓冲转换（None / str / 非法类型）、
    cv2.imdecode（损坏字节、误传 float ndarray）、Pillow 回退任意一步抛错，
    统一在此收敛为 None，不允许漏给调用方。
    """
    if cv2 is None:                                     # pragma: no cover
        return None
    try:
        buf = _as_buffer(data)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is not None:
            return img
        from PIL import Image, ImageOps
        import io as _io
        register_heif_opener()
        # PIL 要 bytes：原输入本来就是 bytes 类时直接用，别经 ndarray 再 tobytes
        # 整图拷一份（HEIC 原图常见 10MB+，回退路径峰值内存会翻倍）。
        raw = (data if isinstance(data, (bytes, bytearray, memoryview))
               else bytes(buf.tobytes()))
        with Image.open(_io.BytesIO(raw)) as im:
            ori = im.info.get("original_orientation")
            if ori and int(ori) != 1:
                im = apply_exif_orientation(im, ori)    # HEIC / HEIF 专用
            else:
                im = ImageOps.exif_transpose(im)        # 标准 EXIF 方向
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")                  # 去掉 alpha / 调色板
            arr = np.asarray(im)
        if arr.ndim == 2:                               # 灰度 → BGR 三通道
            return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    except Exception:                                   # noqa: BLE001
        return None                                     # 解码失败由调用方统一处理
