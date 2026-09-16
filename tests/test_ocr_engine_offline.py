"""OCR 引擎的**离线**契约单测：不加载模型、不推理。

模型推理本身要跑真实模型（见 test_engine.py 的 skip 分支）；这里只钉住不依赖权重
的部分：模型清单结构、完整性校验的行为（缺失/篡改 → 报告而不抛）、以及并发保护的
存在（`_lock` 管引擎替换、`_infer_lock` 管推理，两者分工不能退化成一把锁）。
"""
import unittest
from pathlib import Path

from ocr import rapid_engine as re_mod


class TestModelTables(unittest.TestCase):
    def test_v6_models_declared(self):
        models = re_mod._V6_MODELS
        self.assertIn("v6-tiny", models)
        for tier, entry in models.items():
            self.assertGreaterEqual(len(entry), 4, f"{tier} 档位字段不齐：{entry}")

    def test_manifest_exists_and_lists_hashes(self):
        manifest = re_mod._RES / "PP-OCRv6_models_manifest.txt"
        self.assertTrue(manifest.exists(), f"缺少模型清单 {manifest}")
        hashes = [ln for ln in manifest.read_text(encoding="utf-8").splitlines()
                  if len(ln.split()) >= 2 and len(ln.split()[-1]) == 64]
        self.assertGreater(len(hashes), 5, "清单里应有模型文件的 SHA256")


class TestVerifyModels(unittest.TestCase):
    """`verify_models()` 的契约：返回问题清单，**不抛异常、不阻断启动**。"""

    def test_missing_file_reported_not_raised(self):
        problems = re_mod.verify_models(["__definitely_absent__.onnx"])
        self.assertIsInstance(problems, list)
        self.assertEqual(len(problems), 1)
        self.assertIn("__definitely_absent__.onnx", problems[0][0])

    def test_real_models_pass_when_present(self):
        """本机模型齐备时校验应为空；缺失（干净克隆只带 tiny）则跳过。"""
        names = [re_mod._V6_MODELS["v6-tiny"][i] for i in (1, 2, 3)]
        self.assertEqual(re_mod.verify_models(names), [])
        full = re_mod.verify_models()
        if full:
            missing = [p for p in full if "缺失" in p[1] or "不存在" in p[1]]
            self.assertTrue(missing, f"不该有非「缺失」类问题：{full}")
            self.skipTest(f"本机缺少大模型文件（{len(full)} 个），全量校验跳过")

    def test_tampered_file_reported(self):
        """字节数/SHA256 不符要能报出来（模型被替换/下载不全是「结果诡异」的常见根因）。"""
        names = [re_mod._V6_MODELS["v6-tiny"][i] for i in (1, 2, 3)]
        real = [n for n in names if (re_mod._RES / "models_v6" / n).exists()]
        if not real:
            self.skipTest("本机没有 v6-tiny 模型文件")
        target = re_mod._RES / "models_v6" / real[0]
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\x00" * 16)   # 追加垃圾 → 长度与哈希都变
            problems = re_mod.verify_models([real[0]])
            self.assertTrue(problems, "篡改后应报出问题")
        finally:
            target.write_bytes(original)
        self.assertEqual(re_mod.verify_models([real[0]]), [], "还原后应恢复通过")


class TestConcurrencyGuards(unittest.TestCase):
    """锁的分工：`_lock` 只护引擎引用替换，`_infer_lock` 护整段推理 —— 不能合并
    （合成一把会让切模型等秒级推理跑完，界面卡住）。"""

    def test_two_locks_declared(self):
        src = Path(re_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("self._lock = threading.Lock()", src)
        self.assertIn("self._infer_lock = threading.Lock()", src)

    def test_recognize_holds_infer_lock(self):
        src = Path(re_mod.__file__).read_text(encoding="utf-8")
        body = src.split("def recognize")[1].split("\n    def ")[0]
        self.assertIn("with self._infer_lock", body,
                      "recognize 必须整段持推理锁（否则切换模型窗口期可并存两实例）")

    def test_switch_model_uses_same_lock_order(self):
        src = Path(re_mod.__file__).read_text(encoding="utf-8")
        body = src.split("def switch_model")[1]
        self.assertIn("self._infer_lock", body)
        self.assertIn("self._lock", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
