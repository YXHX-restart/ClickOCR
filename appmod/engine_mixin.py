"""EngineMixin —— OCR + 表格识别编排与引擎生命周期（常驻线程提交、后台加载、失败降级）。

从 app.py 拆出（2026-09-11 重构 S3-S5），纯搬移不改行为。
本 mixin 只被 App 混入使用，方法体里的 self.* 由混入后的 App 提供，
本模块不单独实例化、也不 import app。
"""
from __future__ import annotations

import json
import sys
import threading
from functools import partial
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import Qt, QRect, QPoint, QThread, Signal, QTimer
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QAction
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu

from core_engine.ai_pipeline import MODEL_SET_TO_TIER, OcrBox
from core_engine.db_manager import DBManager, HistoryStorage
from core_engine.logging_setup import setup_logging, get_logger

from ocr import RapidOcrEngine
from screenshot import ScreenshotService
from config import ConfigManager
from hotkey import HotkeyHost
from platform_compat import capabilities, scroll_capture_supported
from platform_compat.fonts import ui_font_family
from ui.base.theme import apply_theme, resolve_theme, install_system_follow
from ui.windows.selection import SelectionOverlay
from ui.windows.result_overlay import ResultOverlay
from appmod.workers import OcrWorker, TableWorker, EngineLoader

log = get_logger("app")


class EngineMixin:
    def _grab_and_ocr(self, raw):
        try:
            img = self.svc.grab_region(raw)
        except Exception as e:  # noqa: BLE001
            self.tray.showMessage("ClickOCR", f"截图失败：{e}",
                                  QSystemTrayIcon.Warning, 3000)
            return
        # 成功截图：恢复除旧贴图外的浮窗（旧贴图由新贴图取代）
        self._restore_hidden()
        ov = self._make_overlay(img, raw)
        if not self.ocr_auto:
            # 「截图后自动 OCR」关闭：仅贴图，不自动识别；
            # 点「文字框/表格」按钮时才临时拉起 OCR（懒加载引擎在此刻构建）。
            ov._set_status("已贴图 · 点「文字框」或「表格」开始识别")
            return
        self._ocr_overlay(ov, img)


    def _ocr_overlay(self, ov, img):
        """对指定贴图窗口执行 OCR（懒加载引擎首次识别时自动构建模型）。"""
        self._submit_ocr(ov, None, img, "ocr")


    def _submit_ocr(self, ov, rid, img, kind):
        """把一次 OCR 任务提交给常驻工作线程（单线程复用，只认最新一张）。

        kind：'ocr' → 首次识别（_on_ocr_done）；'reocr' → Ctrl+R 重识别
        （_on_reocr_done，带 rid 覆盖历史）。task_id 单调递增，用于丢弃
        过期结果：期间又提交了新任务时，旧任务完成回调直接忽略，避免串台。
        """
        engine = self._ensure_engine()
        if engine is None:
            return
        w = self._worker
        if w is None:
            w = OcrWorker(engine)
            w.done.connect(self._on_worker_done)
            w.failed.connect(self._on_worker_failed)
            self._worker = w
            w.start()
        self._ocr_seq += 1
        seq = self._ocr_seq
        self._ocr_pending = {"seq": seq, "ov": ov, "rid": rid, "kind": kind}
        w.submit(seq, img)


    def _on_worker_done(self, seq, img, boxes):
        pend = getattr(self, "_ocr_pending", None)
        if pend is None or pend.get("seq") != seq:
            return                        # 过期结果：期间已提交更新任务
        self._ocr_pending = None
        ov = pend.get("ov")
        if ov is None:
            return
        if pend.get("kind") == "reocr":
            self._on_reocr_done(ov, pend.get("rid"), boxes, img)
        else:
            self._on_ocr_done(ov, boxes)


    def _on_worker_failed(self, seq, msg):
        pend = getattr(self, "_ocr_pending", None)
        if pend is None or pend.get("seq") != seq:
            return
        self._ocr_pending = None
        ov = pend.get("ov")
        log.error("OCR 识别失败（task %s）：%s", seq, msg)
        if ov is not None:
            try:
                ov._set_status(f"识别失败：{msg}")
            except Exception:  # noqa: BLE001
                pass


    def _ocr_on_demand(self, ov):
        """「截图后自动 OCR」关闭时：点文字框/表格临时拉起 OCR。

        提示文案按引擎**实际状态**给（关键）：keep_alive=true 时模型启动即在后台
        加载，点按钮时通常已就绪，不能还报「首次加载模型」（旧版写死该文案，
        常驻用户会看到误导提示）；只有 keep_alive=false 的懒加载路径才需要等加载。
        """
        self._ensure_save(ov)           # 点文字框/表格 = 操作，确保落库
        if ov is None or getattr(ov, "boxes", None):
            return                       # 已有结果无需再识别
        img = getattr(ov, "img", None)
        if img is None:
            return
        self._set_ocr_running_status(ov)
        self._ocr_overlay(ov, img)


    def _ensure_engine(self):
        """获取已就绪的 OCR 引擎（兼容三种加载路径）：

        - 后台已加载完成（keep_alive=true）：直接返回；
        - 后台加载中（极少见：启动后前几秒就触发 OCR）：等待线程完成；
        - 未加载且非后台模式（keep_alive=false）：按需同步构建。

        始终在 self.engine 包装对象内构建模型，不替换对象，
        以保住 table_engine 等已绑定的 ocr_recognize 回调。
        构建失败不崩溃：engine=None + 弹窗提示（OCR 不可用，其余功能正常）。
        """
        eng = getattr(self, "engine", None)
        # 包装对象已构建模型（_engine 非空）
        if eng is not None and getattr(eng, "_engine", None) is not None:
            return eng
        # 后台加载进行中：阻塞等待线程完成（避免并发重复构建）
        if getattr(self, "_engine_loading", False):
            loader = getattr(self, "_loader", None)
            if loader is not None and loader.isRunning():
                loader.wait()
            eng = getattr(self, "engine", None)
            if eng is not None and getattr(eng, "_engine", None) is not None:
                return eng
            if getattr(self, "_ocr_error", None):
                self._warn_ocr_failed()
                return None
            return eng
        # 非后台模式（keep_alive=false）：首次 OCR 按需同步构建
        if getattr(self, "_ocr_error", None):
            self._warn_ocr_failed()
            return None
        if eng is None:
            return None
        try:
            eng._ensure_engine()
            self._set_engine_status("就绪")
            return eng
        except Exception as e:  # noqa: BLE001
            self.engine = None
            self._ocr_error = str(e) or type(e).__name__
            log.exception("OCR 引擎构建失败（懒加载路径）")
            self._warn_ocr_failed()
            return None


    def _warn_ocr_failed(self):
        """OCR 引擎加载失败弹窗提示（非阻塞；程序继续运行，仅 OCR 不可用）。"""
        from ui.base.texts import T
        from ui.base.theme_dialog import ThemeDialog
        try:
            ThemeDialog.warning(
                None, T("ocr.fail_title", "OCR 引擎加载失败"),
                T("ocr.fail_msg",
                  "OCR 引擎加载失败：{}\n\n"
                  "可检查模型文件是否完整、GPU 加速环境（设置→GPU 加速可切换档位）。\n"
                  "程序将继续运行：截图 / 贴图 / 标注等功能正常，仅文字识别不可用。")
                .format(getattr(self, "_ocr_error", "未知原因")))
        except Exception:  # noqa: BLE001 弹窗失败不阻断
            pass

    # ---- 方案 B：OCR 模型后台加载（keep_alive=true）----

    def _start_engine_load(self):
        """启动后台线程加载模型；UI/托盘已先就位，不阻塞启动。"""
        eng = getattr(self, "engine", None)
        if eng is None:
            # 初始化即失败：延迟弹窗提示
            if getattr(self, "_ocr_error", None):
                QTimer.singleShot(400, self._warn_ocr_failed)
            return
        if getattr(eng, "_engine", None) is not None:
            return  # 已构建（理论上不会发生）
        self._engine_loading = True
        self._set_engine_status("模型加载中…")
        loader = EngineLoader(engine_ref=eng)
        loader.ready.connect(self._on_engine_ready)
        loader.failed.connect(self._on_engine_failed)
        self._loader = loader
        loader.start()


    def _on_engine_ready(self, eng):
        self._engine_loading = False
        self._set_engine_status("就绪")
        log.info("OCR 引擎后台加载完成（device=%s）",
                 eng.get_info().get("device", "-"))


    def _on_engine_failed(self, msg):
        self._engine_loading = False
        self._ocr_error = msg
        self._set_engine_status("OCR 不可用")
        log.error("OCR 引擎后台加载失败：%s", msg)
        self._warn_ocr_failed()


    def _set_engine_status(self, text):
        """在托盘 tooltip 反映引擎加载状态（非阻塞、失败静默）。"""
        try:
            if getattr(self, "tray", None) is not None:
                self.tray.setToolTip(f"ClickOCR · {text}")
        except Exception:  # noqa: BLE001
            pass

    # ---- M2：OCR 完成只展示文字；是否落库由「用户是否操作过」决定 ----

    def _table_recognize(self, ov):
        """Ctrl+Shift+T / 表格识别：异步执行 SLANet 表格结构识别 + 弹出预览面板。"""
        if self.table_engine is None:
            self.tray.showMessage(
                "ClickOCR", "表格识别不可用（rapid_table 未安装）",
                QSystemTrayIcon.Warning, 3000)
            return
        if getattr(self, "_table_busy", False):
            self.tray.showMessage("ClickOCR", "表格识别进行中，请稍候",
                                  QSystemTrayIcon.Information, 2000)
            return
        try:
            # 用贴图窗口保存的原始截图（避免抓屏抓到屏幕上的贴图/网格线框）
            img = getattr(ov, "img", None)
            if img is None:
                img = self.svc.grab_region(ov.raw_bbox)
        except Exception as e:  # noqa: BLE001
            self.tray.showMessage("ClickOCR", f"截图失败：{e}",
                                  QSystemTrayIcon.Warning, 3000)
            return
        ov._set_status("表格识别中…（首次约 2-3s 加载模型）")
        w = self._table_worker
        if w is None:
            w = TableWorker(self.table_engine)
            w.done.connect(self._on_table_done)
            w.failed.connect(self._on_table_failed)
            self._table_worker = w
            w.start()
        self._table_busy = True
        w.submit(img)


    def _on_table_done(self, result):
        """SLANet 表格识别完成（仅双击/长按/Ctrl+Shift+T 开窗时触发）：
        存结果 + 卸载 SLANet（不常驻内存）+ 需要时弹窗。"""
        self._table_busy = False
        # 识别期间图片已切换/内容已变：结果作废（不缓存、不弹窗）
        if self._table_gen != self._img_gen:
            log.info("表格识别完成但图片已变更，结果作废")
            if self._table_pending_window and self._overlay is not None:
                # 待开窗请求仍有效：对当前图片重新识别
                self._table_gen = self._img_gen
                self._table_recognize(self._overlay)
            return
        self._table_result = result
        # SLANet 模型不常驻：识别完立即卸载释放 ~150-200MB
        try:
            if self.table_engine is not None:
                self.table_engine.unload_table_model()
        except Exception:  # noqa: BLE001
            pass
        if not result.cells and not result.html:
            self.tray.showMessage("ClickOCR", "未检测到表格结构",
                                  QSystemTrayIcon.Warning, 2500)
            return
        if self._table_pending_window:     # 用户长按/双击时尚未识别完
            self._table_pending_window = False
            self._show_table_panel(result)


    def _open_table_window(self):
        """双击图片 / 长按「表格」按钮 / Ctrl+Shift+T：打开表格窗口
        （此时才使用 SLANet 识别；识别完卸载，不常驻内存）。"""
        self._ensure_save(self._overlay)   # 点表格 = 操作，确保落库
        # 仅当缓存结果是「当前图片」的才复用；否则重新识别
        # （修复：长按/双击表格显示上一张图的识别结果）
        if self._table_result is not None and self._table_gen == self._img_gen:
            self._show_table_panel(self._table_result)
            return
        if self._table_worker is not None and getattr(self, "_table_busy", False):
            self._table_pending_window = True
            return
        self._table_pending_window = True
        self._table_gen = self._img_gen    # 本次识别绑定当前图片代次
        if self._overlay is not None:
            self._table_recognize(self._overlay)


    def _show_table_panel(self, result):
        try:
            from ui.windows.table_panel import TablePreviewPanel
            panel = TablePreviewPanel(result, self.config)
            panel.closed.connect(
                lambda p=panel: self._close_table_panel(p))
            panel.show()
            panel.raise_()
            self._table_panels.append(panel)
        except Exception as e:  # noqa: BLE001
            log.exception("表格预览面板打开失败: %s", e)


    def _on_table_failed(self, msg):
        self._table_busy = False
        if self._overlay is not None:
            self._overlay._set_status("表格识别失败")
        self.tray.showMessage("ClickOCR", f"表格识别失败：{msg}",
                              QSystemTrayIcon.Warning, 4000)


    def _close_table_panel(self, panel):
        if panel in self._table_panels:
            self._table_panels.remove(panel)
        panel.deleteLater()

    # ---- M2·γ：标注 JSON 持久化到历史记录 ----

