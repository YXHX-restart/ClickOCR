"""M1-1 冒烟测试：RapidOcrEngine 识别合成图。

运行：.venv/Scripts/python -m pytest tests/ -q
或：  .venv/Scripts/python tests/test_engine.py
"""
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ocr import RapidOcrEngine


def _make_test_image(text: str = "Hello ClickOCR 你好", size=(480, 140)) -> np.ndarray:
    img = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except Exception:
        font = ImageFont.load_default()
    d.text((20, 45), text, fill=(0, 0, 0), font=font)
    arr = np.asarray(img)
    return arr[..., ::-1].copy()  # RGB -> BGR


def test_recognize_basic():
    engine = RapidOcrEngine()
    boxes = engine.recognize(_make_test_image())
    assert isinstance(boxes, list) and len(boxes) > 0, "应识别到至少一个文字块"
    for b in boxes:
        assert len(b.polygon) == 4, "polygon 必须是 4 顶点"
        assert 0.0 <= b.score <= 1.0
        assert isinstance(b.text, str)
    print("OK recognize:", [(b.text, round(b.score, 2)) for b in boxes])
    print("get_info:", engine.get_info())


if __name__ == "__main__":
    test_recognize_basic()
