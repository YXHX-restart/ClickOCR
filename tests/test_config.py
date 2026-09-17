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


class TestWindowQuotaKeyRename(unittest.TestCase):
    """2026-09-18 键名消歧：`max_history_windows`/`max_hist_windows` →

    `max_shot_windows`（截图贴图，默认 1）/ `max_restore_windows`（历史贴图，默认 3）。
    旧名只差 3 个字符、语义又都带 "history"，历次代码审查都误判成「改名遗留的
    重复键」—— 改名是为了让名字本身说清归属；旧键由 `_LEGACY_MAP` 搬家，用户
    现有取值**不能丢**（丢 = 静默重置成默认值）。
    """

    def _write(self, tmp: Path, text: str) -> ConfigManager:
        p = tmp / "config.ini"
        p.write_text(text, encoding="utf-8")
        return ConfigManager(path=p)

    def test_old_names_migrate_with_values(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._write(Path(td), "[software]\n"
                                        "max_history_windows = 7\n"
                                        "max_hist_windows = 9\n")
            self.assertEqual(cfg.getint("software", "max_shot_windows", -1), 7)
            self.assertEqual(cfg.getint("software", "max_restore_windows", -1), 9)
            self.assertFalse(cfg.cp.has_option("software", "max_history_windows"),
                             "旧键应被清掉，否则下次审查还会看到两个「同义键」")
            self.assertFalse(cfg.cp.has_option("software", "max_hist_windows"))

    def test_rename_is_idempotent(self):
        """已迁移过的配置再加载一次，取值保持不变（迁移不得反复覆盖）。"""
        with tempfile.TemporaryDirectory() as td:
            cfg = self._write(Path(td), "[software]\nmax_shot_windows = 4\n")
            self.assertEqual(cfg.getint("software", "max_shot_windows", -1), 4)
            cfg2 = ConfigManager(path=Path(td) / "config.ini")
            self.assertEqual(cfg2.getint("software", "max_shot_windows", -1), 4)

    def test_new_key_wins_over_stale_old_key(self):
        """两键同时存在时以**新键**为准：旧键只清不覆盖。

        否则「旧值回退」会把用户改过的新设置顶掉（改名类迁移的典型坑）。
        """
        with tempfile.TemporaryDirectory() as td:
            cfg = self._write(Path(td), "[software]\n"
                                        "max_shot_windows = 5\n"
                                        "max_history_windows = 99\n")
            self.assertEqual(cfg.getint("software", "max_shot_windows", -1), 5)

    def test_defaults_match_documented_pair(self):
        """两个键的默认值必须与「截图贴图 1 / 历史贴图 3」的既有行为一致。"""
        self.assertEqual(_DEFAULTS["software"]["max_shot_windows"], "1")
        self.assertEqual(_DEFAULTS["software"]["max_restore_windows"], "3")


class TestLoggingSetup(unittest.TestCase):
    """日志初始化在 **windowed 打包**下也必须能用：那时 `sys.stdout is None`。"""

    def test_no_console_when_stdout_missing(self):
        import logging
        import sys
        from unittest import mock

        from core_engine import logging_setup as ls

        with tempfile.TemporaryDirectory() as td:
            # setup_logging 幂等（_INITIALIZED 为真就直接返回）：强制重跑一次
            logger = ls.get_logger()
            for h in list(logger.handlers):
                logger.removeHandler(h)
                h.close()
            ls._INITIALIZED = False
            with mock.patch.object(sys, "stdout", None):
                logger = ls.setup_logging(app_root=td, level=logging.INFO,
                                          console=True)
            streams = [h for h in logger.handlers
                       if isinstance(h, logging.StreamHandler)
                       and not isinstance(h, logging.FileHandler)]
            self.assertFalse(streams, "sys.stdout 为 None 时不该挂流式 handler"
                                      "（每条日志都会走一遍异常路径）")
            logger.info("windowed 下写日志不该出错")     # 不抛即通过
            self.assertIn("windowed", (Path(td) / "logs" / "clickocr.log")
                          .read_text(encoding="utf-8"))
            for h in list(logger.handlers):              # 别污染其它用例
                logger.removeHandler(h)
                h.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
