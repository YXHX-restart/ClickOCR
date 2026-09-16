"""配置系统离线单测：插值安全、写回来源、出厂模板与代码默认值两来源合一。

覆盖的既有缺陷：含单个 `%` 的值让 ConfigParser 插值崩在启动路径上；旧版 `%%`
转义；`save()` 写回「资源目录里的模板」而不是真正的加载来源（打包会带出个人配置）。
"""
import configparser
import tempfile
import unittest
from pathlib import Path

from core_engine.config import _DEFAULTS, ConfigManager


class TestInterpolation(unittest.TestCase):
    def test_single_percent_does_not_crash(self):
        """含单个 % 的值必须能读出来（原实现在这里直接抛 InterpolationSyntaxError）。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.ini"
            p.write_text("[ui]\nname_pattern = {datetime}_{ms} 100%\n",
                         encoding="utf-8")
            cfg = ConfigManager(path=p)
            self.assertEqual(cfg.get("ui", "name_pattern", ""),
                             "{datetime}_{ms} 100%")

    def test_legacy_double_percent_restored(self):
        """旧版写入的 %% 转义要还原成单个 %（老配置文件不能读错）。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.ini"
            p.write_text("[ui]\nname_pattern = 折扣 50%%\n", encoding="utf-8")
            cfg = ConfigManager(path=p)
            self.assertEqual(cfg.get("ui", "name_pattern", ""), "折扣 50%")


class TestSaveTarget(unittest.TestCase):
    def test_save_writes_to_loaded_source(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.ini"
            p.write_text("[ui]\ntheme = dark\n", encoding="utf-8")
            cfg = ConfigManager(path=p)
            cfg.cp.set("ui", "theme", "light")
            cfg.save()
            self.assertIn("light", p.read_text(encoding="utf-8"))

    def test_defaults_are_filled_without_touching_file(self):
        """缺键由 _DEFAULTS 兜底（不写文件）。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.ini"
            p.write_text("[software]\n", encoding="utf-8")
            cfg = ConfigManager(path=p)
            self.assertEqual(cfg.getint("software", "history_max_count", -1),
                             int(_DEFAULTS["software"]["history_max_count"]))


class TestFactoryTemplate(unittest.TestCase):
    """`resources/config.ini` 是**只读出厂模板**，必须与代码默认值一致。"""

    def setUp(self):
        self.tpl = (Path(__file__).resolve().parent.parent
                    / "resources" / "config.ini")
        cc = configparser.ConfigParser(interpolation=None)
        cc.read(self.tpl, encoding="utf-8")
        self.cc = cc

    def test_template_matches_defaults(self):
        drift = []
        for sec, opts in _DEFAULTS.items():
            for k, v in opts.items():
                if self.cc.has_option(sec, k) and self.cc.get(sec, k) != v:
                    drift.append(f"[{sec}] {k}: 模板={self.cc.get(sec, k)!r} "
                                 f"代码默认={v!r}")
        self.assertFalse(drift, "出厂模板与 _DEFAULTS 漂移：\n" + "\n".join(drift))

    def test_template_has_no_machine_specific_keys(self):
        """机器相关项不得作为**键**进出厂模板（打包会把模板发给所有人）。

        注意判据是「行首即键名」，不是子串包含 —— 模板头部注释里正好拿
        `cuda_probe` 当反例说明「请勿在此手改」。
        """
        keys = [ln.strip().split("=")[0].strip()
                for ln in self.tpl.read_text(encoding="utf-8").splitlines()
                if "=" in ln and not ln.strip().startswith(("#", ";", "["))]
        self.assertNotIn("cuda_probe", keys)

    def test_seed_copies_template_to_writable_path(self):
        """首次运行：模板复制到可写位置，之后只动那一份。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "resources").mkdir()
            # 用真实模板内容，但只放必需的一节，避免依赖具体键值
            (root / "resources" / "config.ini").write_text(
                "[ui]\ntheme = dark\n", encoding="utf-8")
            cfg = ConfigManager(app_root=root)
            live = root / "config.ini"
            self.assertTrue(live.exists(), "可写配置应由模板播种生成")
            self.assertEqual(cfg.get("ui", "theme", ""), "dark")
            cfg.cp.set("ui", "theme", "light")
            cfg.save()
            self.assertIn("light", live.read_text(encoding="utf-8"))
            self.assertIn("dark",
                          (root / "resources" / "config.ini").read_text(encoding="utf-8"),
                          "模板不得被改写")


if __name__ == "__main__":
    unittest.main(verbosity=2)
