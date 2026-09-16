"""ClickOCR 应用主体（M1-5 托盘 + 热键 + 单实例；M2 历史记录后端接入）。

主流程：
  Ctrl+Shift+R / 托盘"截图" → SelectionOverlay 框选 → 抓取区域
  → 后台 OCR → ResultOverlay 浮窗（画框 + 点击复制）→ 自动保存历史（M2）。
引擎在 App 构造时仅创建包装对象（lazy），模型于 run() 内后台线程加载。

2026-09-11 重构 S3-S5：本文件原 1,846 行，按职责拆为 appmod/ 包的 5 个 Mixin +
workers，本文件只保留 App 骨架（初始化 / 托盘 / 单实例 / 窗口开关 / 退出）与装配。
拆分方式是**纯搬移**：对外 API 与行为零变化。
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
from PySide6.QtNetwork import QLocalServer, QLocalSocket
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
from ui.base.texts import T
from ui.windows.selection import SelectionOverlay
from ui.windows.result_overlay import ResultOverlay

from appmod.engine_mixin import EngineMixin
from appmod.history_mixin import HistoryMixin
from appmod.drop_mixin import DropMixin
from appmod.float_mixin import FloatMixin
from appmod.capture_mixin import CaptureMixin
from appmod.workers import ModelSwitchWorker

log = get_logger("app")


def _make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 120, 215))
    p = QPainter(pm)
    p.setPen(Qt.white)
    p.setFont(QFont("Arial", 18, QFont.Bold))
    p.drawText(pm.rect(), Qt.AlignCenter, "OCR")
    p.end()
    return QIcon(pm)




class App(EngineMixin, HistoryMixin, DropMixin, FloatMixin, CaptureMixin):
    INSTANCE_NAME = "ClickOCR-SingleInstance"
    # 单实例管道协议（读写两端共用本常量）：第二实例连上后必须发这个 payload，
    # 服务端校验通过才唤起截图。原先两端各写各的、服务端根本不看内容——
    # 任意本地进程连上这个命名管道就能触发一次全屏截图。
    INSTANCE_PING = b"show"

    @property
    def ocr_auto(self) -> bool:
        """截图后自动 OCR（[software] ocr_auto，默认开）。

        实时读配置（不再在 __init__ 缓存）：设置面板保存后立即生效，
        否则「改开关 → 同一次会话里截屏 / 打开历史图」仍走旧值。
        除了截屏自动识别，它同时是「打开历史图是否自动补识别」的门控
        （见 restore_record）。
        """
        return self.config.getbool("software", "ocr_auto", True)


    def __init__(self, app_root=None):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        # 系统 UI 字体由 platform_compat.fonts 决策（Windows = 微软雅黑，行为与
        # 历史一致）：避免 ⊛/☆ 等符号回退到其他字体（Segoe UI Symbol 等）
        # 显示偏小/粗细不一致
        # 注意：必须显式指定 9pt（QFont(family) 默认 12pt，会让文字整体变大）
        self.app.setFont(QFont(ui_font_family(), 9))

        self.config = ConfigManager(app_root=app_root)
        setup_logging(logs_dir=self.config.logs_dir)
        # 平台能力矩阵：哪些功能在当前平台不可用，日志里一眼可见
        log.info("平台能力：%s", capabilities())
        # ---- UI 主题（明/暗/跟随系统）----
        apply_theme(self.app, resolve_theme(self.config))
        install_system_follow(self.app, self.config)
        use_cls = self.config.getbool("engine", "use_cls", False)
        model_set = self.config.get("software", "ocr_model", "v6-tiny")
        # GPU 加速（[software] ocr_device = cpu | cuda(NVIDIA) | dml(AMD/Intel/通用)）：
        # 对应 GPU 版 onnxruntime 就绪时启用，探测失败自动回退 CPU（引擎内部处理）
        dev = self.config.get("software", "ocr_device", "cpu").lower()
        self.accel = dev if dev in ("cuda", "dml") else "cpu"
        self.combo = self.config.get(
            "hotkey_global", "screenshot_combo", "ctrl+shift+r")
        self.model_tier = MODEL_SET_TO_TIER.get(model_set, "tiny")
        # OCR 引擎常驻（[software] ocr_keep_alive，默认 true）：
        # false = 启动不加载模型（省内存），首次识别时才构建（懒加载）
        self.keep_alive = self.config.getbool(
            "software", "ocr_keep_alive", True)
        # 注：ocr_auto（截图后自动 OCR）**不在此缓存**——它是类顶部的 property，
        # 每次实时读配置（设置保存后立即生效）。这里再赋一次会直接抛
        # AttributeError: property 'ocr_auto' has no setter，启动即崩。
        # 方案 B：启动只建引擎包装对象（lazy=True，不加载模型），模型改在
        # run() 里后台线程加载——窗口/托盘先就位，启动不再被模型加载阻塞。
        # keep_alive=false 时不预加载，首次 OCR 才按需构建（见 _ensure_engine）。
        # 引擎加载失败不崩溃：捕获异常 → engine=None（OCR 不可用，其余功能正常），
        # 启动后由 run()/_start_engine_load 统一弹窗提示（见 _warn_ocr_failed）。
        self._ocr_error = None
        self._engine_loading = False
        self._loader = None
        try:
            self.engine = RapidOcrEngine(use_cls=use_cls, model_set=model_set,
                                         use_cuda=self.accel == "cuda",
                                         use_dml=self.accel == "dml",
                                         lazy=True)
        except Exception as e:  # noqa: BLE001
            self.engine = None
            self._ocr_error = str(e) or type(e).__name__
            log.exception("OCR 引擎初始化失败（程序继续运行，OCR 不可用）")
        self.svc = ScreenshotService()                  # keep-alive
        # 主屏 DPR：仅作「取不到屏」时的默认值（多屏混合 DPI 必须按点取屏，
        # 见 _dpr_at）。primaryScreen() 在无头/异常环境可能返回 None，
        # 原写法 .devicePixelRatio() 会直接抛 AttributeError → 启动即失败。
        self.dpr = self._dpr_at()
        # 本次抓屏所用那块屏的 dpr（start_capture 时按遮罩所在屏更新）。
        # raw_bbox 是**物理像素**，把图换算回逻辑尺寸必须用同一块屏的 dpr，
        # 否则副屏（尤其混合 DPI）上浮窗尺寸会算错。默认 = 主屏 dpr。
        self._capture_dpr = self.dpr
        log.info("OCR 引擎: %s / accel=%s / gpu_active=%s",
                 model_set, self.accel,
                 (self.engine.get_info().get("gpu_available")
                  if self.engine is not None else "-"))

        # ---- M2（由 M4 提前）：表格识别引擎（SLANet 惰性加载，OCR 复用主引擎）----
        # OCR 引擎加载失败（engine=None）时不初始化表格引擎（依赖 OCR recognize）
        self.table_engine = None
        if self.engine is not None:
            try:
                from core_engine.table_engine import TableEngine
                self.table_engine = TableEngine(
                    ocr_recognize=self.engine.recognize,
                    use_cuda=self.accel == "cuda",
                    use_dml=self.accel == "dml")
                log.info("表格引擎就绪（SLANet 惰性加载，首次识别时构建）")
            except Exception as e:  # noqa: BLE001
                log.warning("表格引擎初始化失败：%s", e)
                self.table_engine = None

        # ---- M2：数据库 + 历史记录（建库幂等）----
        try:
            self.db = DBManager(self.config.db_path)
            self.db.init_db()
            self.history = HistoryStorage(
                self.db, self.config.get_screenshots_dir(),
                max_records=self.config.getint(
                    "software", "history_max_count", 50))
            log.info("数据库就绪: %s（vec0=%s）",
                     self.config.db_path, self.db.vec0_available)
        except Exception as e:  # noqa: BLE001
            log.exception("数据库初始化失败，历史记录功能禁用")
            self.db = None
            self.history = None

        self.tray = self._build_tray()
        self.hotkey = HotkeyHost()
        self.hotkey.set_callback(self.start_capture)
        self.hotkey.on_registered = None   # 注册成功不打扰（仅冲突/失败才提醒）
        self.hotkey.on_failed = lambda: self.tray.showMessage(
            "ClickOCR",
            T("app.hotkey_fail",
              "全局热键注册失败（可能被占用），请用托盘\"截图\"按钮"),
            QSystemTrayIcon.Warning, 4000)
        # 主热键被占用、静默换成备用组合时告知用户实际生效的键
        # （print 对 GUI 用户不可见，否则以为还是自己设的键）
        self.hotkey.on_fallback = lambda label: self.tray.showMessage(
            "ClickOCR",
            T("app.hotkey_fallback", "截图热键被占用，已改用 {label}").format(
                label=label),
            QSystemTrayIcon.Warning, 4000)

        self._overlay = None
        self._overlays = []          # 贴图窗口列表（上限控制，默认 1 个）
        self._pins = []              # 钉图窗口列表（[software] max_pin_windows，默认 3，上限 20）
        self._worker = None
        self._ocr_seq = 0            # OCR 任务序号：单调递增，用于丢弃过期结果
        self._ocr_pending = None     # 最新一次 OCR 任务的上下文（seq/ov/rid/kind）
        self._table_worker = None
        self._table_busy = False     # 表格识别是否进行中（常驻线程无 isRunning 语义）
        self._drop_worker = None     # 拖放批量 OCR 的常驻工作线程（单线程复用）
        self._drop_import_worker = None   # 拖放导入线程（读/解码/编码/落库）
        self._drop_ocr_queue = []    # 待识别队列 [(rid, 原图绝对路径)]
        self._drop_ocr_busy = False  # 是否有 OCR 任务在途（_drop_worker 单槽）
        self._model_switch_worker = None  # 模型档位热切换线程（一次性）
        self._pending_model_set = None    # 排队中的切换目标（上一 worker 未收尾时）
        # 以下状态原先只在各自流程首次执行时赋值（靠调用顺序保证「先赋后用」），
        # 集中在此初始化：既让共享状态契约显式化，也避免任何提前读取报 AttributeError
        self._hidden_windows = []    # 截图前隐藏的『操作 UI』（成功/失败/取消后恢复）
        self._loader = None          # 启动时后台加载模型的线程（keep_alive）
        self._scroll_worker = None   # 滚动截图工作线程
        self._manual_cap = None      # 滚动截图（手动模式）滚轮推帧线程
        self._stop_listener = None   # 滚动截图（手动模式）停止监听线程
        self._table_panels = []      # 已打开的表格预览面板
        self._table_result = None    # 最近一次表格识别结果（双击/长按开窗用）
        self._table_pending_window = False   # 识别完成前已请求开窗
        self._table_gen = -1         # 表格结果对应的图片代次（-1=无有效结果）
        self._img_gen = 0            # 图片代次：新图/内容变更每次 +1，用于作废旧表格结果
        self._sel = None
        self._history_panel = None
        self._history_panels = []    # 已打开的历史面板（上限控制，默认 1 个）
        self._settings_panel = None
        self.is_second = False
        self._check_single()
        # 退出时停掉常驻 OCR 工作线程（避免 QThread 在解释器销毁时被强杀）
        self.app.aboutToQuit.connect(self._on_quit)

    # ---- 单实例 ----

    def _check_single(self):
        """单实例检测：已有实例 → 发 INSTANCE_PING 唤起并退出；否则自己监听。

        listen 失败**不能静默继续**：残留的 socket 文件（上次被强杀）或名字被占
        时若当成「首个实例」接着跑，会出现两个托盘图标 + 两份热键注册。
        故失败先清残留重试一次，仍失败则记录 _single_failed，由 run() 提示并退出。
        """
        self._single_failed = None
        sock = QLocalSocket()
        sock.connectToServer(self.INSTANCE_NAME)
        if sock.waitForConnected(300):
            sock.write(self.INSTANCE_PING)      # 协议见 INSTANCE_PING
            sock.flush()
            sock.waitForBytesWritten(300)
            self.is_second = True
            return
        self.is_second = False
        self.server = QLocalServer()
        if self.server.listen(self.INSTANCE_NAME):
            self.server.newConnection.connect(self._on_second)
            return
        log.warning("单实例监听失败（%s），清理残留后重试", self.server.errorString())
        QLocalServer.removeServer(self.INSTANCE_NAME)
        if self.server.listen(self.INSTANCE_NAME):
            self.server.newConnection.connect(self._on_second)
            return
        self._single_failed = self.server.errorString() or "未知错误"
        log.error("单实例监听仍失败：%s", self._single_failed)

    def _on_second(self):
        """第二实例连上来：交给 _on_second_data 校验 payload 后再截图。

        非阻塞写法：不在槽里 waitForReadyRead 等数据——本槽在主线程执行，
        阻塞等待会被「连上但不发数据」的任意本地进程拖住界面（最长 300ms/次）。
        """
        while self.server.hasPendingConnections():
            conn = self.server.nextPendingConnection()
            if conn is None:
                return
            conn.readyRead.connect(partial(self._on_second_data, conn))
            conn.disconnected.connect(conn.deleteLater)

    def _on_second_data(self, conn):
        """校验第二实例的请求字节：只认 INSTANCE_PING，其余一律忽略并断开。

        原先收到**任何**连接都直接 start_capture，等于把「触发一次全屏截图」暴露
        给任意本地进程（连上管道即可反复触发）。
        """
        data = bytes(conn.readAll().data()).strip()
        if data != self.INSTANCE_PING:
            log.warning("忽略单实例管道的非法请求：%r", data[:32])
        else:
            self.start_capture()
        conn.disconnectFromServer()

    # ---- 多屏 DPI ----

    def _dpr_at(self, x: int | None = None, y: int | None = None) -> float:
        """取坐标点所在显示器的 DPR —— 多屏 DPI 换算的**唯一入口**。

        多屏混合 DPI（如主屏 100% / 副屏 150%）时，用主屏 DPR 换算副屏上的
        坐标会偏移、截错内容，必须按点取屏。缺省 x/y（或取不到屏）时回退主屏
        → 再回退 1.0，无头环境下 primaryScreen() 返回 None 也不会抛异常。
        调用约定：x/y 用**全局逻辑坐标**（如 overlay 的 mapToGlobal 结果）；
        供 appmod/capture_mixin.py 的框选坐标 → 物理像素换算调用。
        """
        scr = None
        try:
            if x is not None and y is not None:
                scr = self.app.screenAt(QPoint(int(x), int(y)))
            if scr is None:
                scr = self.app.primaryScreen()
            dpr = scr.devicePixelRatio() if scr is not None else 0.0
        except Exception:      # noqa: BLE001 取屏失败按 1.0 处理（不阻断截图）
            dpr = 0.0
        return dpr or 1.0

    # ---- 托盘 ----

    def _build_tray(self) -> QSystemTrayIcon:
        from ui.base.texts import T
        tray = QSystemTrayIcon(_make_icon(), self.app)
        menu = QMenu()
        a_cap = QAction(T("tray.capture", "截图"), self.app)
        a_cap.triggered.connect(self.start_capture)
        a_hist = QAction(T("tray.history", "历史记录"), self.app)
        a_hist.triggered.connect(self.open_history)
        a_set = QAction(T("tray.settings", "设置"), self.app)
        a_set.triggered.connect(self.open_settings)
        a_quit = QAction(T("tray.quit", "退出"), self.app)
        a_quit.triggered.connect(self.app.quit)
        menu.addAction(a_cap)
        menu.addSeparator()
        menu.addAction(a_hist)
        menu.addAction(a_set)
        menu.addSeparator()
        menu.addAction(a_quit)
        tray.setContextMenu(menu)
        tray.setToolTip("ClickOCR")
        tray.activated.connect(
            lambda reason: self.start_capture()
            if reason == QSystemTrayIcon.Trigger else None
        )
        tray.show()
        return tray

    # ---- M2：历史面板 / 设置面板 ----

    def open_history(self):
        if self.history is None:
            self.tray.showMessage(
                "ClickOCR",
                T("app.hist_unavailable", "历史记录不可用（数据库初始化失败）"),
                QSystemTrayIcon.Warning, 3000)
            return
        # 单例：历史面板只应有一个窗口。已打开则聚焦现有窗口，避免重复创建。
        # （max_history_windows 只约束普通贴图窗口，与历史面板无关）
        #  CardWindow.close() 仅隐藏窗口、不销毁，引用长期保留，故用销毁信号清理
        self._history_panels = [p for p in self._history_panels
                                if p is not None and self._panel_alive(p)]
        if self._history_panels:
            panel = self._history_panels[0]
            # 面板是单例、close 仅隐藏不销毁——重新打开时必须 reload，
            # 否则看不到刚截的图（_save_record 落库路径不刷新面板）
            try:
                panel.reload()
            except Exception:  # noqa: BLE001
                log.exception("重新打开历史面板时刷新失败")
            panel.show()
            panel.raise_()
            panel.activateWindow()
            self._history_panel = panel
            return
        from ui.windows.history_panel import HistoryPanel
        panel = HistoryPanel(
            self.history, self.restore_record, self.config.get_screenshots_dir(),
            width=self.config.getint("ui", "history_panel_width", 620),
            cards=self.config.getint("ui", "history_visible_cards", 5),
            on_favorite_changed=self._sync_favorite,
            on_files_dropped=self._on_history_files_dropped,
            config=self.config)
        self._history_panels.append(panel)
        self._history_panel = panel
        # 窗口被真正销毁时清理单例引用（普通 close 仅隐藏，不会触发）
        panel.destroyed.connect(lambda: self._on_history_destroyed(panel))
        panel.show()
        panel.raise_()
        panel.activateWindow()


    def _on_history_destroyed(self, panel):
        self._history_panels = [p for p in self._history_panels
                                if p is not None and p is not panel
                                and self._panel_alive(p)]
        if self._history_panel is panel:
            self._history_panel = None

    # ---- M2·δ：拖图片进历史窗口 → 复制入库 + 后台序列 OCR ----

    def open_settings(self):
        from ui.windows.settings_panel import SettingsPanel
        if self._settings_panel is None:
            self._settings_panel = SettingsPanel(
                self.config, self.hotkey, self.history, tray=self.tray,
                on_clear_history=self._clear_all_history,
                on_model_changed=self._on_model_changed)
        self._settings_panel.show()
        self._settings_panel.raise_()
        self._settings_panel.activateWindow()


    def _on_model_changed(self, new_set: str):
        """设置保存后模型档位变更：更新 model_tier 并后台热切换引擎内部模型。

        关键：只重建 self.engine 的内部 RapidOCR 引擎（switch_model 丢弃
        _engine 再重建），包装对象不变——表格引擎（table_engine）绑定的
        self.engine.recognize 引用依然有效，表格识别自动跟着新档位走。
        切换会加载新模型（首次约 1-2s），原先在 UI 线程同步执行会冻结界面，
        现投递到 ModelSwitchWorker 后台线程，完成后回主线程更新状态。
        切换失败走 _warn_ocr_failed 弹窗路径，程序继续运行（OCR 不可用）。

        并发切换改为**排队**：上一次切换还没结束时不再 wait(5000) 冻结界面
        最长 5 秒，而是记下目标档位后立即返回，由上一次的信号回调续做
        （见 _on_model_switched / _on_model_switch_failed）。
        """
        self.model_tier = MODEL_SET_TO_TIER.get(new_set, "tiny")
        eng = getattr(self, "engine", None)
        if eng is None:
            return   # 引擎本不可用：仅更新 tier，历史记录标记仍正确
        prev = getattr(self, "_model_switch_worker", None)
        if prev is not None and prev.isRunning():
            # 排队：不阻塞 UI 等它收尾；只保留**最新**档位（用户连点几次以
            # 最后一次为准），旧 worker 的 done/failed 回调里自动续做。
            self._pending_model_set = new_set
            self._set_engine_status("模型切换中…")
            log.info("模型切换进行中，%s 已排队等待", new_set)
            return
        self._pending_model_set = None
        self._start_model_switch(eng, new_set)

    def _start_model_switch(self, eng, new_set: str):
        """投递一次后台档位切换（单一入口：立即切换与排队续做都走这里）。"""
        self._set_engine_status("模型切换中…")
        w = ModelSwitchWorker(eng, new_set)
        w.done.connect(self._on_model_switched)
        w.failed.connect(self._on_model_switch_failed)
        self._model_switch_worker = w
        w.start()

    def _on_model_switched(self, new_set: str):
        self._model_switch_worker = None
        # 排队中的档位：上一次切换已收尾，接着切（用户最后一次的选择）
        pending = self._pending_model_set
        if pending is not None:
            self._pending_model_set = None
            log.info("继续排队的模型切换：%s", pending)
            self._start_model_switch(self.engine, pending)
            return
        self._set_engine_status("就绪")
        log.info("OCR 模型档位已切换: %s（tier=%s）", new_set, self.model_tier)
        try:
            self.tray.showMessage(
                "ClickOCR",
                T("app.model_switched", "OCR 模型已切换：") + str(new_set),
                QSystemTrayIcon.Information, 2500)
        except Exception:   # noqa: BLE001 托盘不可用不阻断
            pass

    def _on_model_switch_failed(self, msg: str):
        self._model_switch_worker = None
        # 排队的档位不自动重试：刚失败的这次重建已经弹窗报「OCR 不可用」，
        # 立刻再切一次大概率同样失败并重复弹窗；用户重新保存即会再触发。
        if self._pending_model_set is not None:
            log.warning("模型切换失败，丢弃排队的档位 %s", self._pending_model_set)
            self._pending_model_set = None
        self._ocr_error = msg
        self._set_engine_status("OCR 不可用")
        log.error("OCR 模型档位切换失败: %s", msg)
        self._warn_ocr_failed()


    def run(self):
        if self.is_second:
            print("[ClickOCR] 已在运行中：已聚焦已有窗口（若需要全新启动，请先右键托盘\"退出\"）。")
            self.tray.showMessage(
                "ClickOCR", T("app.already_running", "已在运行中"),
                QSystemTrayIcon.Information, 2000
            )
            sys.exit(0)
            return
        # 单实例监听失败（见 _check_single）：不能再往下跑，否则会出现两个托盘
        # 图标 + 两份热键注册。明确提示后退出，而不是静默双实例。
        if getattr(self, "_single_failed", None):
            from ui.base.texts import T as _T
            from ui.base.theme_dialog import ThemeDialog
            ThemeDialog.warning(
                None, "ClickOCR",
                _T("app.instance_fail",
                   "单实例监听失败（{}）：为避免出现两个托盘图标与重复热键，"
                   "程序将退出。请确认是否已有 ClickOCR 在运行。"
                   ).format(self._single_failed))
            sys.exit(0)
            return
        self.hotkey.install(self.app, self.combo)
        # OCR 引擎启动加载失败（keep_alive=true）：延迟弹窗提示，程序继续运行
        if getattr(self, "_ocr_error", None) and self.engine is None:
            from PySide6.QtCore import QTimer as _QT
            _QT.singleShot(400, self._warn_ocr_failed)
        # 附加热键回调预绑定（即使配置为 off）：设置面板日后启用该热键时
        # set_secondary_combo 才能取到回调——否则注册成功但回调为 None，
        # 按键按下无响应（off → on 的启用路径原先就踩这个洞）
        self.hotkey.set_secondary_callback(2, self.open_history)
        self.hotkey.set_secondary_callback(3, self.capture_fullscreen)
        self.hotkey.set_secondary_callback(4, self._toggle_through_top)
        # M2：历史记录全局热键（默认 Ctrl+`，可配置）
        self.history_combo = self.config.get(
            "hotkey_global", "history", "ctrl+`")
        if self.history_combo and self.history_combo.lower() not in ("off", "none", "false"):
            self.hotkey.register_secondary(self.history_combo, self.open_history, 2)
        # 全屏截图热键（[hotkey_global] fullscreen_combo，默认 off；注册后直接截全屏）
        self.full_combo = self.config.get(
            "hotkey_global", "fullscreen_combo", "off")
        if self.full_combo and self.full_combo.lower() not in ("off", "none", "false"):
            self.hotkey.register_secondary(self.full_combo, self.capture_fullscreen, 3)
        # 点击穿透全局热键（[hotkey_global] through，默认 alt+f；off 关闭）
        # 贴图/钉图窗口穿透时鼠标落到桌面，热键随时取消（穿透状态下窗口失焦也有效）
        self.through_combo = self.config.get(
            "hotkey_global", "through", "alt+f")
        if self.through_combo and self.through_combo.lower() not in ("off", "none", "false"):
            self.hotkey.register_secondary(self.through_combo,
                                           self._toggle_through_top, 4)
        # 方案 B：keep_alive=true 时在后台线程加载模型，界面/托盘已先就位，
        # 启动不再被模型加载阻塞；首次 OCR 通常在后台加载完成时已就绪。
        if self.keep_alive:
            self._start_engine_load()
        sys.exit(self.app.exec())

    # ---- 流程 ----

    def _on_quit(self):
        """退出前停掉所有常驻工作线程，并显式释放 OCR/表格引擎 session。

        常驻线程（OcrWorker/TableWorker/drop worker）若不先 stop，解释器销毁时
        QThread 会被强杀；而 onnxruntime 的 CUDA session 若不显式置空，其析构
        延迟到解释器 shutdown，CUDA context 释放滞后（NVIDIA 服务侧句柄残留）。
        这里主动释放，确保退出时 GPU 资源立即归还。
        """
        # 常驻线程全部纳入停止范围：除三个识别 worker 外还有 EngineLoader
        # （启动即退出的场景下模型仍在后台加载，不 wait 会触发
        # "QThread: Destroyed while thread is still running"）
        for attr in ("_worker", "_drop_worker", "_table_worker", "_loader",
                     "_drop_import_worker", "_model_switch_worker"):
            w = getattr(self, attr, None)
            if w is None or not w.isRunning():
                continue
            # 不是所有 worker 都有 stop()（有的是直接重写 run 循环的 QThread）：
            # 原先用 try/except pass 吞掉 AttributeError，结果是「以为已经通知停止、
            # 其实什么都没做」，只能靠后面的 wait 超时才发现。
            w_stop = getattr(w, "stop", None)
            if w_stop is None:
                log.warning("退出：线程 %s 无 stop()，只能等其自然结束", attr)
            else:
                try:
                    w_stop()
                except Exception as e:   # noqa: BLE001 停不下来也要继续退出流程
                    log.warning("退出：线程 %s stop() 失败：%s", attr, e)
            if not w.wait(2000):
                # 超时仍未结束：记日志（terminate 有 OS 层风险，仅诊断用）
                log.warning("退出时线程 %s 等待超时（可能被强杀）", attr)
        # 显式释放 onnxruntime session（CUDA context / 显存随 session 析构归还）
        try:
            eng = getattr(self, "engine", None)
            if eng is not None:
                eng._engine = None
            if getattr(self, "table_engine", None) is not None:
                self.table_engine.unload_table_model()
        except Exception:  # noqa: BLE001
            pass
        import gc
        gc.collect()


