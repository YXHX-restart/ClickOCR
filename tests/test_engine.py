"""M1-1 冒烟测试：RapidOcrEngine 识别合成图。

这是唯一需要**真实模型**的用例：模型缺失、或绘图字体不可用时明确 skip，
而不是默默失败（此前无 skip / 无 fixture，干净克隆上直接红）。

运行：
    .venv\\Scripts\\python -m unittest discover -s tests -t . -v
    .venv\\Scripts\\python tests\\test_engine.py
"""
import unittest
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
_MODELS = _ROOT / "resources" / "models_v6"


def _models_available() -> bool:
    return all((_MODELS / f).exists() for f in
               ("PP-OCRv6_det_tiny.onnx", "PP-OCRv6_rec_tiny.onnx",
                "ppocrv6_tiny_dict.txt"))


def _make_test_image(text: str = "Hello ClickOCR 你好", size=(480, 140)) -> np.ndarray:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except Exception:                     # noqa: BLE001 字体不可用 → 用内置位图字体
        font = ImageFont.load_default()
    d.text((20, 45), text, fill=(0, 0, 0), font=font)
    arr = np.asarray(img)
    return arr[..., ::-1].copy()          # RGB -> BGR


@unittest.skipUnless(_models_available(),
                     f"缺少 PP-OCRv6 tiny 模型（{_MODELS}），跳过端到端识别")
class TestRecognize(unittest.TestCase):
    def test_recognize_basic(self):
        from ocr import RapidOcrEngine
        engine = RapidOcrEngine(model_set="v6-tiny")
        boxes = engine.recognize(_make_test_image())
        self.assertIsInstance(boxes, list)
        self.assertGreater(len(boxes), 0, "应识别到至少一个文字块")
        for b in boxes:
            self.assertEqual(len(b.polygon), 4, "polygon 必须是 4 顶点")
            self.assertGreaterEqual(b.score, 0.0)
            self.assertLessEqual(b.score, 1.0)
            self.assertIsInstance(b.text, str)

    def test_info_reports_model_set(self):
        from ocr import RapidOcrEngine
        info = RapidOcrEngine(model_set="v6-tiny").get_info()
        # `model_set` 是引擎侧的可读标签（如 "PP-OCRv6-tiny"），不是我们的档位键
        self.assertIsInstance(info.get("model_set"), str)
        self.assertIn("tiny", info["model_set"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
