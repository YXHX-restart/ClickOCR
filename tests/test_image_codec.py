"""图片编解码离线单测：契约是「任何输入都不抛，失败返回 None」。"""
import unittest
from pathlib import Path

import numpy as np

from appmod.image_encode import PILLOW_FORMATS, pillow_encode
from core_engine.image_decode import decode_image_bytes


def _png_bytes(w=8, h=8) -> bytes:
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


class TestDecodeContract(unittest.TestCase):
    def test_png_roundtrip_shape_and_channels(self):
        img = decode_image_bytes(_png_bytes(6, 4))
        self.assertIsNotNone(img)
        self.assertEqual(img.shape[:2], (4, 6), "返回 BGR ndarray，高×宽")

    def test_garbage_returns_none_without_raising(self):
        """损坏缓冲 / 非图片数据 → None（不能把异常抛给调用方）。"""
        for bad in (b"", b"not an image", b"\x00" * 32):
            self.assertIsNone(decode_image_bytes(bad), f"{bad[:8]!r} 应返回 None")

    def test_ndarray_input_returns_none_without_raising(self):
        """误传 ndarray（历史调用点踩过）也不该抛。"""
        self.assertIsNone(decode_image_bytes(np.zeros((4, 4, 3), np.uint8)))


class TestEncode(unittest.TestCase):
    def test_cv2_formats_usable(self):
        """cv2 能编的格式：走统一解码器读回，形状一致。"""
        from core_engine.image_decode import decode_image_bytes as dec
        import cv2
        img = np.zeros((5, 7, 3), np.uint8)
        ok, buf = cv2.imencode(".png", img)
        self.assertTrue(ok)
        back = dec(buf.tobytes())
        self.assertEqual(back.shape[:2], (5, 7))

    def test_pillow_formats_return_bytes_or_none(self):
        """HEIC/HEIF/AVIF 走 Pillow：有依赖则返回字节，缺依赖返回 None（都不抛）。"""
        img = np.zeros((16, 16, 3), np.uint8)
        for fmt in PILLOW_FORMATS:
            out = pillow_encode(img, fmt)
            self.assertTrue(out is None or isinstance(out, bytes),
                            f"{fmt} 应返回 bytes 或 None，实际 {type(out)}")

    def test_call_site_whitelist_covers_pillow_formats(self):
        """调用方（history_mixin）用白名单决定是否走 Pillow —— 白名单必须覆盖
        PILLOW_FORMATS，否则新增格式会被静默改存成 png。"""
        src = (Path(__file__).resolve().parent.parent
               / "appmod" / "history_mixin.py").read_text(encoding="utf-8")
        for fmt in PILLOW_FORMATS:
            self.assertIn(f'"{fmt}"', src, f"调用方白名单缺 {fmt}")

    def test_avif_encodable_when_pillow_supports(self):
        """Pillow ≥11.3 内置 AVIF：能编就该能解回来（形状一致）。"""
        img = np.random.default_rng(0).integers(0, 255, (10, 12, 3), dtype=np.uint8)
        data = pillow_encode(img, "avif")
        if data is None:
            self.skipTest("当前 Pillow 不支持 AVIF 编码")
        back = decode_image_bytes(data)
        self.assertIsNotNone(back)
        self.assertEqual(back.shape[:2], (10, 12))


if __name__ == "__main__":
    unittest.main(verbosity=2)
