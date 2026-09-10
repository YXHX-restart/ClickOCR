"""CaptureMixin —— 截图链路（框选 / 全屏 / 滚动截图）。

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


class CaptureMixin:
    def start_capture(self):
        # 已有框选遮罩在显示则忽略重复触发，避免叠加多个全屏遮罩
        if self._sel is not None and self._sel.isVisible():
            return
        # 截图前隐藏『操作 UI』（状态栏/标注栏/面板），避免入镜；
        # 图片窗体（贴图/钉图/历史图）与钉子保留，不打断
        self._hidden_windows = []
        for w in self._all_float_windows():
            if w is not None and w.isVisible():
                self._hidden_windows.append(w)
                w.hide()
        sel = SelectionOverlay(self.config)
        self._sel = sel
        sel.region_selected.connect(self.on_region)
        sel.cancelled.connect(self._on_capture_cancelled)
        sel.scroll_requested.connect(self.on_scroll_region)
        sel.show()


    def capture_fullscreen(self):
        """全屏截图热键回调：不框选，直接抓取主屏全屏并识别。"""
        # 同样先隐藏『操作 UI』（状态栏/标注栏/面板），图片窗体保留
        self._hidden_windows = []
        for w in self._all_float_windows():
            if w is not None and w.isVisible():
                self._hidden_windows.append(w)
                w.hide()
        try:
            img = self.svc.grab_full()
        except Exception as e:  # noqa: BLE001
            self._restore_hidden()
            self.tray.showMessage("ClickOCR", f"全屏截图失败：{e}",
                                  QSystemTrayIcon.Warning, 3000)
            return
        self._restore_hidden()
        h, w = img.shape[:2]
        ov = self._make_overlay(img, (0, 0, int(w * self.dpr), int(h * self.dpr)))
        if not self.ocr_auto:
            ov._set_status("已贴图 · 点「文字框」或「表格」开始识别")
            return
        self._ocr_overlay(ov, img)


    def _restore_hidden(self):
        for w in self._hidden_windows:
            if w is not None:
                try:
                    w.show()
                except RuntimeError:   # 已被销毁
                    pass
        self._hidden_windows = []


    def _on_capture_cancelled(self):
        """取消截图：恢复被隐藏的程序浮窗。"""
        self._restore_hidden()


    def on_region(self, rect: QRect):
        # 逻辑坐标 → mss 原始像素
        d = self.dpr
        raw = (
            int(rect.x() * d), int(rect.y() * d),
            int((rect.x() + rect.width()) * d),
            int((rect.y() + rect.height()) * d),
        )
        # 等选择遮罩彻底隐藏再抓屏，避免把它拍进去
        QTimer.singleShot(60, partial(self._grab_and_ocr, raw))


    def on_scroll_region(self, rect: QRect):
        """滚动截图：框选完成 → 等遮罩隐藏后启动滚屏拼接。"""
        d = self.dpr
        raw = (
            int(rect.x() * d), int(rect.y() * d),
            int((rect.x() + rect.width()) * d),
            int((rect.y() + rect.height()) * d),
        )
        QTimer.singleShot(60, partial(self._scroll_capture, raw))


    def _scroll_capture(self, raw):
        """滚动截图主流程（roll_screenshot 式三选遮罩）：

        框选后显示全屏遮罩：左键 = 自动滚动（滚轮模拟）；右键 = 退出；
        滚轮 = 手动滚动（监听滚轮抓帧）。拼接用 SIFT 特征匹配。
        Enter/单击进度牌 = 完成贴图；Esc = 退出（不贴图）。
        """
        from screenshot.scroll_capture import (ScrollWorker, ScrollBadge,
                                               ModeMask, ModePicker,
                                               ManualCapture, StopListener)
        if not scroll_capture_supported():
            self._restore_hidden()
            self.tray.showMessage("ClickOCR", "滚动截图仅支持 Windows",
                                  QSystemTrayIcon.Warning, 3000)
            return
        self._restore_hidden()      # 恢复程序浮窗（滚动目标内容必须可见）
        import threading
        stop = threading.Event()
        flags = {"cancel": False}
        mask = ModeMask(raw, self.dpr)
        mask.show()
        picker = ModePicker()
        self._scroll_picker = picker

        def start_worker(mode: str):
            try:
                mask.close()
            except RuntimeError:
                pass
            badge = ScrollBadge()
            badge.stop_requested.connect(stop.set)
            badge.cancel_requested.connect(
                lambda: (stop.set(), flags.__setitem__("cancel", True)))
            badge.show_center_bottom()
            worker = ScrollWorker(
                self.svc, raw, mode=mode,
                speed=self.config.getint("engine", "scroll_speed", 2),
                max_height=self.config.getint("engine",
                                              "scroll_max_height", 12000),
                stop=stop)
            worker.progress.connect(badge.set_progress)
            worker.done.connect(
                partial(self._on_scroll_done, badge, raw, flags))
            worker.failed.connect(
                partial(self._on_scroll_failed, badge, flags))
            self._scroll_worker = worker
            worker.start()
            if mode == "manual":
                self._manual_cap = ManualCapture(self.svc, raw, worker)
                self._manual_cap.start()
            else:
                self._stop_listener = StopListener(worker)
                self._stop_listener.start()

        def on_cancelled():
            try:
                mask.close()
            except RuntimeError:
                pass
            self.tray.showMessage("ClickOCR", "已退出滚动截图",
                                  QSystemTrayIcon.Information, 2000)

        picker.chosen.connect(start_worker)
        picker.cancelled.connect(on_cancelled)
        picker.start()


    def _on_scroll_done(self, badge, raw, flags, img):
        try:
            badge.close()
        except RuntimeError:
            pass
        # Esc 退出：不贴图
        if flags.get("cancel"):
            self.tray.showMessage("ClickOCR", "已退出滚动截图",
                                  QSystemTrayIcon.Information, 2000)
            return
        if img is None or img.size == 0:
            self.tray.showMessage("ClickOCR", "滚动截图未获得画面",
                                  QSystemTrayIcon.Warning, 3000)
            return
        h, w = img.shape[:2]
        ov = self._make_overlay(img, raw)
        # 长图比框选区域高：整图等比缩放入屏（否则只能看到顶部一屏）
        ov.fit_to_screen()
        self.tray.showMessage("ClickOCR", f"滚动截图完成：{w}×{h}",
                              QSystemTrayIcon.Information, 2500)
        if not self.ocr_auto:
            ov._set_status("滚动截图完成 · 点「文字框」或「表格」开始识别")
            return
        self._ocr_overlay(ov, img)


    def _on_scroll_failed(self, badge, flags, msg):
        try:
            badge.close()
        except RuntimeError:
            pass
        if flags.get("cancel"):
            return                       # Esc 退出：静默，不弹失败提示
        self.tray.showMessage("ClickOCR", f"滚动截图失败：{msg}",
                              QSystemTrayIcon.Warning, 3000)


