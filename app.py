"""ClickOCR 应用主体（M1-5 托盘 + 热键 + 单实例；M2 历史记录后端接入）。

主流程：
  Ctrl+Shift+R / 托盘"截图" → SelectionOverlay 框选 → 抓取区域
  → 后台 OCR → ResultOverlay 浮窗（画框 + 点击复制）→ 自动保存历史（M2）。
引擎在 App 构造时仅创建包装对象（lazy），模型于 run() 内后台线程加载
（方案 B：界面/托盘先就位，启动不被模型加载阻塞；keep_alive=false 时
改为首次 OCR 按需构建）。截图服务保持构造时 keep-alive。
M2 接入：结构化日志 + SQLite 建库 + HistoryStorage（截图自动入库 + FIFO）。
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
from ui.windows.selection import SelectionOverlay
from ui.windows.result_overlay import ResultOverlay

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


try:  # 统一解码实现下沉在 core_engine（桌面端/服务端共用，含 HEIC/AVIF + EXIF 纠正）
    from core_engine.image_decode import decode_image_bytes as _decode_image_bytes
except Exception:  # noqa: BLE001 core_engine 缺失时退化：仅支持 cv2 原生格式
    def _decode_image_bytes(raw) -> "np.ndarray | None":
        return cv2.imdecode(raw, cv2.IMREAD_COLOR)


class OcrWorker(QThread):
    """常驻 OCR 工作线程：单线程复用，只认最新一张图。

    修复「截图几次显存耗尽」：onnxruntime CUDA EP 会为每个新建线程分配
    独立 CUDA stream/workspace（实测约 ~188 MiB/线程），且线程结束后不回收。
    旧实现每次截图 new OcrWorker → 每次新线程 → 显存逐步累积直至 8G 耗尽。
    改为单线程常驻 + 任务队列：CUDA workspace 只分配一次，显存稳定；同时
    省去线程反复创建/销毁的开销，识别速度不受影响（只处理最新一张，天然
    丢弃来不及开始就绪的过期任务）。
    """
    done = Signal(int, object, list)   # (task_id, img, boxes)
    failed = Signal(int, str)          # (task_id, message)

    def __init__(self, engine: RapidOcrEngine):
        super().__init__()
        self.engine = engine
        self._cond = threading.Condition()
        self._img = None
        self._task_id = 0
        self._stopping = False

    def submit(self, task_id: int, img):
        """提交新任务；若上一任务尚未被取走则直接覆盖（只认最新一张）。"""
        with self._cond:
            self._task_id = task_id
            self._img = img
            self._cond.notify()

    def stop(self):
        with self._cond:
            self._stopping = True
            self._img = None
            self._task_id = 0
            self._cond.notify()

    def run(self):
        while True:
            with self._cond:
                while self._img is None and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
                img = self._img
                tid = self._task_id
                self._img = None
                self._task_id = 0
            try:
                boxes = self.engine.recognize(img)
                self.done.emit(tid, img, boxes)
            except Exception as e:  # noqa: BLE001
                self.failed.emit(tid, str(e) or type(e).__name__)


class TableWorker(QThread):
    """常驻表格识别线程：单线程复用（SLANet 惰性加载 + 结构识别）。

    与 OcrWorker 同理：onnxruntime CUDA EP 为每个新建线程分配 thread_local 的
    cublas/cudnn handle + CUDA stream + workspace，线程结束不回收 → 每次 new
    QThread 跑表格识别都会泄漏一批句柄/显存。改为单线程常驻复用。
    """
    done = Signal(object)          # TableResult
    failed = Signal(str)

    def __init__(self, table_engine):
        super().__init__()
        self.table_engine = table_engine
        self._cond = threading.Condition()
        self._img = None
        self._stopping = False

    def submit(self, img):
        with self._cond:
            self._img = img
            self._cond.notify()

    def stop(self):
        with self._cond:
            self._stopping = True
            self._img = None
            self._cond.notify()

    def run(self):
        while True:
            with self._cond:
                while self._img is None and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
                img = self._img
                self._img = None
            try:
                result = self.table_engine.recognize_table(img)
                self.done.emit(result)
            except Exception as e:  # noqa: BLE001
                self.failed.emit(str(e) or type(e).__name__)


class EngineLoader(QThread):
    """后台加载 OCR 模型（keep_alive=true 时启用）。

    直接在传入的引擎包装对象内构建模型（eng._ensure_engine()），
    而非新建对象——避免替换 self.engine 后，table_engine 等已绑定的
    ocr_recognize 回调指向失效的旧对象。模型构建在原生层会释放 GIL，
    故 UI 线程全程不阻塞。
    """
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, engine_ref):
        super().__init__()
        self.engine_ref = engine_ref

    def run(self):
        try:
            self.engine_ref._ensure_engine()      # 在包装对象内构建模型
            self.ready.emit(self.engine_ref)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e) or type(e).__name__)


class App:
    INSTANCE_NAME = "ClickOCR-SingleInstance"

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
        self._engine_lazy_init = not self.keep_alive
        self.svc = ScreenshotService()                  # keep-alive
        self.dpr = self.app.primaryScreen().devicePixelRatio()
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
            "ClickOCR", "全局热键注册失败（可能被占用），请用托盘\"截图\"按钮",
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
        sock = QLocalSocket()
        sock.connectToServer(self.INSTANCE_NAME)
        if sock.waitForConnected(300):
            sock.write(b"show")
            sock.flush()
            sock.waitForBytesWritten(300)
            self.is_second = True
            return
        self.is_second = False
        self.server = QLocalServer()
        self.server.listen(self.INSTANCE_NAME)
        self.server.newConnection.connect(self._on_second)

    def _on_second(self):
        self.start_capture()

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
            self.tray.showMessage("ClickOCR", "历史记录不可用（数据库初始化失败）",
                                  QSystemTrayIcon.Warning, 3000)
            return
        # 单例：历史面板只应有一个窗口。已打开则聚焦现有窗口，避免重复创建。
        # （max_history_windows 只约束普通贴图窗口，与历史面板无关）
        #  CardWindow.close() 仅隐藏窗口、不销毁，引用长期保留，故用销毁信号清理
        self._history_panels = [p for p in self._history_panels
                                if p is not None and self._panel_alive(p)]
        if self._history_panels:
            panel = self._history_panels[0]
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

    @staticmethod
    def _panel_alive(o):
        """判断 QObject 是否仍存活（未被 C++ 侧销毁）。"""
        try:
            from shiboken6 import isDeleted
            return o is not None and not isDeleted(o)
        except Exception:
            return o is not None

    def _on_history_destroyed(self, panel):
        self._history_panels = [p for p in self._history_panels
                                if p is not None and p is not panel
                                and self._panel_alive(p)]
        if self._history_panel is panel:
            self._history_panel = None

    # ---- M2·δ：拖图片进历史窗口 → 复制入库 + 后台序列 OCR ----
    def _on_history_files_dropped(self, paths: list):
        """历史面板拖放回调：每张图只读解码一次，落库（空 ocr_text = 待识别），
        随后按 [software] drop_ocr_auto 决定是否后台串行 OCR（与截图的 ocr_auto
        相互独立；FIFO 由 save 内部处理）。

        paths: 经面板过滤、去重后的本地图片绝对路径列表。
        """
        if self.history is None:
            return
        # 去重索引：库内已存在「同名且同大小」的文件时，不新增记录，
        # 而是把对应历史记录刷新 created_at 置顶（允许「重复拖入放前边」）。
        dup_index = self._build_history_dup_index()
        touched = 0
        pending = []   # [(rid, img)] —— 已落库、待 OCR 的记录
        for p in paths:
            # 读图：cv2.imdecode(np.fromfile(...)) 兼容含非 ASCII 字符的路径
            try:
                raw = np.fromfile(p, np.uint8)
            except Exception:  # noqa: BLE001 路径无法用 fromfile 打开 → 退回 read_bytes
                try:
                    with open(p, "rb") as fh:
                        raw = np.frombuffer(fh.read(), np.uint8)
                except Exception as e:  # noqa: BLE001
                    log.warning("拖放图片读取失败，跳过: %s (%s)", p, e)
                    continue
            # 同名同大小 → 视为同一文件：置顶已有记录，不新增、不重复 OCR
            dup_rid = self._find_dup_rid(p, len(raw), dup_index)
            if dup_rid is not None:
                if self.history.touch(dup_rid):
                    log.info("拖放图片与库内记录同名同大小，已置顶记录 #%s: %s",
                             dup_rid, p)
                    touched += 1
                continue
            # 解码：cv2 优先，失败回退 Pillow（AVIF / HEIC / HEIF 等现代格式）
            img = _decode_image_bytes(raw)
            if img is None:
                log.warning("拖放图片解码失败（非图像/损坏/缺少对应解码器），跳过: %s", p)
                continue
            # 编码主图 + 缩略图（遵循 [software] save_format / save_quality）
            ok, ext, image_bytes, thumb_bytes = self._encode_image_pair(img)
            if not ok:
                continue
            # 落库：让 HistoryStorage 负责写入 screenshots/ + thumb/（不自己 copy）。
            # ocr_boxes=None → 落库为 NULL = 「从未识别」（待识别态，面板显示 "(未识别)"）。
            # 注意不能写 []：'[]' 的语义是「已识别、图中无文字」，会被当成已完成识别
            # 而不再补跑（见 restore_record）。FIFO 清理在 save 内自动生效。
            # model_tier/device 同理留空：此处还没识别，等识别完由 update_ocr 补写
            # 执行记录（这是「执行记录」而非「配置快照」的关键）。
            try:
                rid = self.history.save(
                    image_bytes=image_bytes,
                    thumb_bytes=thumb_bytes,
                    ocr_text="",
                    ocr_boxes=None,
                    model_tier="",
                    device="",
                    ext=ext,
                    name_pattern=self.config.get("software",
                                                  "name_pattern", ""),
                )
            except Exception as e:  # noqa: BLE001
                log.exception("拖放落库失败: %s", e)
                continue
            pending.append((rid, img))

        if not pending and touched == 0:
            return

        # 立即刷新面板：新增记录以「待识别」(无文字) 态出现；置顶记录移到最前
        if self._history_panel is not None and self._panel_alive(self._history_panel):
            try:
                self._history_panel.reload()
            except Exception:  # noqa: BLE001
                pass

        # 有记录因同名同大小被置顶时，给一次轻提示
        if touched > 0:
            self._notify_dropped_touched(touched)

        # 落库后按 [software] drop_ocr_auto 决定是否后台序列 OCR（默认开，原行为）
        if not self.config.getbool("software", "drop_ocr_auto", True):
            self._drop_ocr_queue = []      # 关掉开关时同时清空挂起队列
            if pending:
                self._notify_dropped_imported(len(pending))
            return
        self._drop_ocr_queue = pending
        self._drop_ocr_start_next()

    # ---- M2·δ 拖放去重：同名同大小 → 置顶已有记录（不新增、不重复 OCR） ----
    def _build_history_dup_index(self) -> dict:
        """构建库内现有截图的检索索引：{文件名: [(rid, 绝对路径, 字节大小)]}。

        匹配标准 = 文件名 + 文件大小 双匹配，**不比对内容**（零文件内容读取，
        千张库也只需一次 SQL 查询 + N 次 stat，机械硬盘无感）。
        命中 → 调用方 touch 该记录置顶（不新增、不重复 OCR）；改名 / 新副本的
        同图不匹配 → 正常新增入库（同样满足「重复拖入放前边」的用法）。
        库内文件绝对路径 = screenshots_dir / <screenshot_path 的文件名>。
        """
        index: dict = {}
        if self.history is None:
            return index
        try:
            rows = self.history.list(limit=100000)  # 全量（拖放是低频人工操作）
        except Exception:  # noqa: BLE001
            return index
        shot_dir = self.config.get_screenshots_dir()
        for r in rows:
            sp = r.get("screenshot_path") or ""
            name = Path(sp).name
            if not name:
                continue
            ap = shot_dir / name
            try:
                size = ap.stat().st_size
            except OSError:
                continue
            index.setdefault(name, []).append((r.get("id"), ap, size))
        return index

    def _find_dup_rid(self, p: str, size: int, dup_index: dict):
        """拖入文件与库内某记录「同名且同大小」→ 返回该记录 id，否则 None。"""
        for rid, _ap, fsize in dup_index.get(Path(p).name, []):
            if fsize == size:
                return rid
        return None

    def _notify_dropped_touched(self, touched: int):
        """拖放中同名同大小的已有记录被置顶时，给一次轻提示（复用历史面板 toast）。"""
        msg = (f"已将 {touched} 张同名同大小的已有记录置顶"
               f"（未重复入库）")
        log.info(msg)
        panel = getattr(self, "_history_panel", None)
        if panel is not None and self._panel_alive(panel):
            try:
                panel._toast(msg)
            except Exception:  # noqa: BLE001
                pass

    def _notify_dropped_imported(self, n: int):
        """「拖入图片后自动 OCR」关闭时：提示已入库但未识别（复用历史面板 toast）。"""
        from ui.base.texts import T
        msg = T("ui.msg_drop_imported", "已导入 {} 张图片（未自动识别）").format(n)
        log.info(msg)
        panel = getattr(self, "_history_panel", None)
        if panel is not None and self._panel_alive(panel):
            try:
                panel._toast(msg)
            except Exception:  # noqa: BLE001
                pass

    def _drop_ocr_start_next(self):
        """取出队首记录启 OCR；前图完成（_on_drop_ocr_done）才启下一张。

        复用常驻 _drop_worker（单线程）：拖放批量 OCR 同样避免每次新建线程
        导致 CUDA EP 每线程 ~188 MiB workspace 不回收的显存累积问题。
        """
        queue = getattr(self, "_drop_ocr_queue", None)
        if not queue:
            self._drop_ocr_queue = []
            return
        rid, img = queue.pop(0)
        engine = self._ensure_engine()
        if engine is None:
            # 引擎不可用：剩余记录保持待识别态，仅提示一次
            self._warn_ocr_failed()
            self._drop_ocr_queue = []
            return
        w = getattr(self, "_drop_worker", None)
        if w is None:
            w = OcrWorker(engine)
            w.done.connect(self._on_drop_ocr_done)
            w.failed.connect(self._on_drop_ocr_failed)
            self._drop_worker = w
            w.start()
        self._drop_ocr_rid = rid
        w.submit(1, img)

    def _on_drop_ocr_done(self, seq, img, boxes):
        """单张 OCR 完成：覆盖记录文字 + 刷新面板，再启下一张。"""
        rid = self._drop_ocr_rid
        self._update_history_ocr(rid, boxes)
        # 前图 done → 启下一张（队列空则自然结束）
        self._drop_ocr_start_next()

    def _on_drop_ocr_failed(self, seq, msg):
        """拖放 OCR 单张失败：记日志后继续下一张（不阻断整批）。"""
        log.error("拖放 OCR 失败（task %s）：%s", seq, msg)
        self._drop_ocr_start_next()


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
        """设置保存后模型档位变更：更新 model_tier 并热切换引擎内部模型。

        关键：只重建 self.engine 的内部 RapidOCR 引擎（switch_model 丢弃
        _engine 再重建），包装对象不变——表格引擎（table_engine）绑定的
        self.engine.recognize 引用依然有效，表格识别自动跟着新档位走。
        切换失败走 _warn_ocr_failed 弹窗路径，程序继续运行（OCR 不可用）。
        """
        self.model_tier = MODEL_SET_TO_TIER.get(new_set, "tiny")
        eng = getattr(self, "engine", None)
        if eng is None:
            return   # 引擎本不可用：仅更新 tier，历史记录标记仍正确
        try:
            self._set_engine_status("模型切换中…")
            eng.switch_model(new_set)     # 同步重建内部引擎（首次约 1-2s）
            self._set_engine_status("就绪")
            log.info("OCR 模型档位已切换: %s（tier=%s）", new_set, self.model_tier)
            try:
                self.tray.showMessage(
                    "ClickOCR", f"OCR 模型已切换：{new_set}",
                    QSystemTrayIcon.Information, 2500)
            except Exception:   # noqa: BLE001 托盘不可用不阻断
                pass
        except Exception as e:  # noqa: BLE001
            self._ocr_error = str(e) or type(e).__name__
            self._set_engine_status("OCR 不可用")
            log.exception("OCR 模型档位切换失败: %s", e)
            self._warn_ocr_failed()

    def _clear_all_history(self):
        """清空全部历史后：刷新所有已打开的历史面板（清空动作在设置面板内完成）。"""
        panels = list(self._history_panels)
        if self._history_panel is not None \
                and self._history_panel not in panels:
            panels.append(self._history_panel)
        for p in panels:
            if self._panel_alive(p):
                try:
                    p.reload()
                except Exception:  # noqa: BLE001
                    log.exception("清空历史后刷新面板失败")

    def restore_record(self, rec: dict):
        """从历史记录恢复贴图：加载截图 + 文字层，可再次选字复制（FR-17）。

        - 关联原记录 id（ov.history_id）：标注/Ctrl+R/收藏等操作都落在**原记录**上，
          不会因 history_id=None 而新建重复记录。
        - 定位到历史面板左侧（垂直居中）；左边放不下则放右侧，不再夹到屏幕左上角。
        """
        try:
            rel = rec["screenshot_path"]
            # 截图目录可配置（[software] screenshots_dir），按文件名解析到当前目录
            full = self.config.get_screenshots_dir() / Path(rel).name
            if not full.exists():
                raise OSError(f"截图文件缺失: {rel}")
            img = cv2.imdecode(np.fromfile(str(full), dtype=np.uint8),
                               cv2.IMREAD_COLOR)
            if img is None:
                raise OSError(f"截图文件缺失: {rel}")
            h, w = img.shape[:2]
            import json as _json
            raw_boxes = rec["ocr_boxes"]
            boxes = [
                OcrBox(text=str(b.get("text", "")),
                       polygon=b.get("polygon", [[0, 0], [0, 0], [0, 0], [0, 0]]),
                       score=float(b.get("score", 0.0)))
                for b in _json.loads(raw_boxes or "[]")
            ]
            ov = self._make_overlay(img, (0, 0, w, h), boxes, persistent=True)
            ov.history_id = rec["id"]        # 关联原记录：后续操作不新建、直接更新原记录
            self._place_restored_overlay(ov) # 先定位（不依赖 load_annotations 成败）
            try:
                ov.load_annotations(rec.get("annotations"))
            except Exception:  # noqa: BLE001
                log.exception("恢复标注失败（图片已正常显示/定位）")
            # 该记录「从未识别」：ocr_boxes IS NULL（例如关闭「拖入图片后自动 OCR」
            # 导入的图，只入库不识别）。注意 ocr_boxes='[]' 是「已识别、图中无文字」，
            # 属于已完成识别，不再重跑模型。
            #
            # 是否「打开即补识别」由「截图后自动 OCR」门控：
            # - 开 → 立即识别，完成后由 _on_ocr_done 写回原记录（history_id 已关联）
            # - 关 → 保持待识别，用户点工具栏「文字框」按钮才识别
            # 放在 history_id 赋值之后，且延迟到窗口显示后提交。
            if raw_boxes is None and self.ocr_auto:
                self._set_ocr_running_status(ov)
                QTimer.singleShot(0, partial(self._ocr_history_overlay, ov, img))
        except Exception as e:  # noqa: BLE001
            log.exception("恢复历史记录失败: %s", e)
            self.tray.showMessage("ClickOCR", f"恢复失败：{e}",
                                  QSystemTrayIcon.Warning, 3000)
            # 图片缺失：弹窗之外，顺带删除这条（磁盘已无截图）的历史记录
            if "截图文件缺失" in str(e) and self.history is not None:
                rid = rec.get("id")
                if rid is not None:
                    try:
                        self.history.delete(rid)
                    except Exception:  # noqa: BLE001
                        log.exception("删除缺失图片的历史记录失败: id=%s", rid)
                    else:
                        for p in self._history_panels:
                            if self._panel_alive(p):
                                p.reload()

    def _ocr_history_overlay(self, ov, img):
        """打开「从未识别」的历史记录：立即识别（结果写回原记录）。

        数据库里没有独立的「已识别」标记，约定用 ocr_boxes 的值区分：
        - NULL（''）→ 从未识别 → 需要时补识别
        - '[]'      → 已识别、图中无文字 → 不重跑模型
        - '[...]'   → 已识别、有文字 → 打开即显示
        同时受「截图后自动 OCR」门控（关闭时打开历史图不自动识别）。
        """
        if ov is None or getattr(ov, "boxes", None):
            return                      # 已有结果（或已被其它路径识别）
        if not self.ocr_auto:
            return                      # 「截图后自动 OCR」关：只显示，等用户点「文字框」
        self._set_ocr_running_status(ov)
        self._ocr_overlay(ov, img)

    def _place_restored_overlay(self, ov):
        """历史恢复图定位：优先放历史面板左侧（垂直居中）；左边放不下则放面板右侧。
        面板不存在/不可见时保持原位置，绝不因夹取而落到屏幕左上角。"""
        panel = self._history_panel
        if panel is None or not self._panel_alive(panel) or not panel.isVisible():
            # 兜底：取任一存活且可见的历史面板
            panel = next((p for p in self._history_panels
                          if self._panel_alive(p) and p.isVisible()), None)
        if panel is None:
            return
        scr = QApplication.primaryScreen().availableGeometry()
        pg = panel.frameGeometry()
        x = pg.left() - ov.width() - 12          # 默认：面板左侧
        if x < scr.left() + 4:                   # 左边放不下 → 放面板右侧（紧邻，不飞角落）
            x = pg.right() + 12
        y = pg.top() + max(0, (pg.height() - ov.height()) // 2)
        x = max(scr.left() + 4, min(x, scr.right() - ov.width()))
        y = max(scr.top() + 4, min(y, scr.bottom() - ov.height()))
        ov.move(x, y)

    # ---- 启动 ----
    def run(self):
        if self.is_second:
            print("[ClickOCR] 已在运行中：已聚焦已有窗口（若需要全新启动，请先右键托盘\"退出\"）。")
            self.tray.showMessage(
                "ClickOCR", "已在运行中", QSystemTrayIcon.Information, 2000
            )
            sys.exit(0)
            return
        self.hotkey.install(self.app, self.combo)
        # OCR 引擎启动加载失败（keep_alive=true）：延迟弹窗提示，程序继续运行
        if getattr(self, "_ocr_error", None) and self.engine is None:
            from PySide6.QtCore import QTimer as _QT
            _QT.singleShot(400, self._warn_ocr_failed)
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
    def _all_float_windows(self) -> list:
        """截图前需要隐藏的『操作 UI』：状态栏 / 统一工具栏 / 面板。

        图片窗体（贴图 / 钉图 / 历史图）与钉图钉子作为内容**保留**，不隐藏——
        钉图与历史图是独立窗体，截图不应打断它们。
        """
        wins = []
        for ov in self._overlays:      # 钉图已从 _overlays 移出，这里只含普通贴图 + 历史图
            if ov._status_strip is not None:
                wins.append(ov._status_strip)
            if ov._toolbar is not None:
                wins.append(ov._toolbar)
        for p in self._history_panels:
            wins.append(p)
        if self._settings_panel is not None:
            wins.append(self._settings_panel)
        for p in self._table_panels:      # 表格预览浮窗同样不入镜
            wins.append(p)
        return wins

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

    def _make_overlay(self, img, raw, boxes=None, persistent=False):
        """创建贴图窗口 + 纯信息状态栏 + 统一工具栏（均有全局开关）。

        数量上限分两类独立管理（见 _trim_overlays）：
        - 截图贴图（persistent=False）→ [software] max_history_windows，默认 1
        - 历史贴图（persistent=True，从历史恢复的图）→ [software] max_hist_windows，默认 3
        钉图（在 _pins）独立管理不占配额。用户手动关闭窗口会从 _overlays 移除。
        [ui] strip_enabled=false：不建状态栏；toolbar_enabled=false：不建工具栏。
        """
        # 新图进入（框选/全屏/滚动/历史恢复的唯一汇合点）：
        # 旧表格识别结果与「待开窗」请求全部作废，
        # 防止长按「表格」复用上一张图的识别结果（应针对当前图重新识别）。
        self._img_gen += 1
        self._table_result = None
        self._table_gen = -1
        self._table_pending_window = False
        from ui.windows.result_overlay import ResultOverlay
        from ui.components.status_strip import StatusStrip
        from ui.components.toolbar import ToolBar
        ov = ResultOverlay(img, raw, self.dpr, self.config)
        ov._persistent = persistent
        ov.reocr_requested.connect(self._reocr)          # Ctrl+R 重新 OCR
        ov.table_requested.connect(                      # Ctrl+Shift+T → 打开表格窗口
            self._open_table_window)
        ov.table_window_requested.connect(               # 双击/长按表格 → 打开窗口
            self._open_table_window)
        ov.annotations_changed.connect(                   # 标注持久化
            partial(self._save_annotations, ov))
        ov.fav_requested.connect(                         # 收藏当前截图
            partial(self._favorite_overlay, ov))
        ov.pin_toggled.connect(                           # 钉图切换（隐藏浮层，只留钉子）
            partial(self._pin_overlay, ov))
        ov.ocr_needed.connect(                            # 无 OCR 结果点文字框/表格
            partial(self._ocr_on_demand, ov))
        ov.done_requested.connect(self._on_overlay_done)   # 完成：确保保存历史后关闭
        ov.copied.connect(partial(self._on_copied, ov))    # 复制文字：确保落库
        ov.closed.connect(partial(self._on_overlay_closed, ov))  # 用户关闭 → 移出列表
        # 纯信息状态栏（[ui] strip_enabled，默认开）
        if self.config.getbool("ui", "strip_enabled", True):
            strip = StatusStrip()
            ov.set_status_strip(strip)
        # 统一工具栏（[ui] toolbar_enabled，默认开）
        if self.config.getbool("ui", "toolbar_enabled", True):
            toolbar = ToolBar()
            ov.set_toolbar(toolbar)
        ov.show()
        if ov._status_strip is not None:
            ov._status_strip.show()
            ov._status_strip.raise_()
        if ov._toolbar is not None:
            ov._toolbar.show()
            ov._toolbar.raise_()
        ov._sync_status_strip_pos()
        ov._sync_toolbar_pos()
        if boxes is not None:
            ov.set_boxes(boxes)
        self._overlays.append(ov)
        self._trim_overlays()            # 新窗口计入后按两类配额裁剪（最旧优先）
        self._overlay = ov
        return ov

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

    def _on_quit(self):
        """退出前停掉所有常驻工作线程，并显式释放 OCR/表格引擎 session。

        常驻线程（OcrWorker/TableWorker/drop worker）若不先 stop，解释器销毁时
        QThread 会被强杀；而 onnxruntime 的 CUDA session 若不显式置空，其析构
        延迟到解释器 shutdown，CUDA context 释放滞后（NVIDIA 服务侧句柄残留）。
        这里主动释放，确保退出时 GPU 资源立即归还。
        """
        for attr in ("_worker", "_drop_worker", "_table_worker"):
            w = getattr(self, attr, None)
            if w is not None and w.isRunning():
                try:
                    w.stop()
                except Exception:  # noqa: BLE001
                    pass
                w.wait(2000)
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
    def _encode_image(self, img):
        """按 [software] save_format/save_quality 编码图片。

        返回 (ok, ext, buf)；有损格式（jpg/webp）带质量参数，
        png/bmp 无损忽略质量。
        """
        fmt = str(self.config.get("software", "save_format", "png")).lower()
        if fmt not in ("png", "jpg", "jpeg", "bmp", "webp"):
            fmt = "png"
        ext = "jpg" if fmt == "jpeg" else fmt
        params = []
        if fmt in ("jpg", "jpeg", "webp"):
            q = max(1, min(100, self.config.getint(
                "software", "save_quality", 90)))
            key = (cv2.IMWRITE_JPEG_QUALITY if fmt in ("jpg", "jpeg")
                   else cv2.IMWRITE_WEBP_QUALITY)
            params = [key, q]
        ok, buf = cv2.imencode("." + ext, img, params)
        return ok, ext, buf

    THUMB_WIDTH = 160   # 历史缩略图统一宽度（主图同格式）

    def _encode_image_pair(self, img):
        """编码主图 + 重建 160px 缩略图并编码，返回落库用的两份字节。

        原先是「拖放落库 / 保存浮窗 / 收藏即保存」三处各写一份同样的
        缩略图重建 + 二次编码（宽 160、主图同格式的算法重复三处），统一到此。
        返回 (ok, ext, image_bytes, thumb_bytes)；主图编码失败时 ok=False、
        其余为 None。缩略图编码失败沿用原行为（忽略，写空字节）。
        """
        ok, ext, buf = self._encode_image(img)
        if not ok:
            return False, ext, None, None
        h, w = img.shape[:2]
        tw = self.THUMB_WIDTH
        th = max(1, int(round(h * tw / w)))
        thumb = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
        _, _, tbuf = self._encode_image(thumb)
        return True, ext, buf.tobytes(), tbuf.tobytes()

    def _record_ocr_payload(self, boxes):
        """boxes → 落库用 (ocr_text, ocr_boxes)。

        统一构造（原先在 _save_record / _on_reocr_done / _on_drop_ocr_done 各写一份，
        拖放那份还漏了 kind → 二维码标记丢失）：QR 内容进 ocr_text 的 [QR] 前缀行，
        几何与 kind 进 ocr_boxes。
        """
        ocr_text = "\n".join(b.text for b in boxes)
        qr_lines = [f"[QR] {b.text}" for b in boxes
                    if getattr(b, "kind", "ocr") == "qr"]
        if qr_lines:
            ocr_text = ((ocr_text + "\n") if ocr_text else "") \
                + "\n".join(qr_lines)
        ocr_boxes = [
            {"text": b.text, "polygon": b.polygon, "score": b.score,
             "kind": getattr(b, "kind", "ocr")}
            for b in boxes
        ]
        return ocr_text, ocr_boxes

    def _ocr_payload_for_save(self, ov, boxes):
        """落库用 (ocr_text, ocr_boxes)：未识别过的空结果写 NULL。

        「从未识别」= _ocr_done 为假且没有任何 boxes——例如「截图后自动 OCR」
        关闭时截图后直接收藏/钉图/完成，或「拖入图片后自动 OCR」关闭时导入的图。
        这种空结果若写 '[]'，语义会变成「已识别、图中无文字」，之后再打开也不会
        补识别；故写 NULL 表示「待识别」（见 restore_record）。
        """
        ocr_text, ocr_boxes = self._record_ocr_payload(boxes)
        if not ocr_boxes and not getattr(ov, "_ocr_done", False):
            return ocr_text, None
        return ocr_text, ocr_boxes

    def _ocr_runtime_info(self) -> tuple:
        """本次识别的**执行记录** (model_tier, device)。

        与「配置快照」的区别：这里的 device 取引擎**实际生效**的设备
        （GPU 探测失败会自动回退 CPU，此时如实记 cpu，而不是设置里选的 cuda/dml）；
        档位也取引擎当前加载的模型，避免用户改档位后（热切换）与记录对不上。
        引擎不可用/取不到时回退到 App 层的配置值，绝不抛异常。
        """
        tier, dev = self.model_tier, self.accel
        eng = getattr(self, "engine", None)
        if eng is None:
            return tier, dev
        try:
            info = eng.get_info() or {}
        except Exception:  # noqa: BLE001
            log.exception("读取引擎运行信息失败（回退配置值）")
            return tier, dev
        ms = str(info.get("model_set") or "")
        for t in ("medium", "small", "tiny"):     # get_info 给的是 PP-OCRv6-<tier>
            if ms.endswith(t):
                tier = t
                break
        dev = str(info.get("device") or dev)
        return tier, dev

    def _save_ocr_meta(self, ov) -> tuple:
        """落库用执行记录 (model_tier, device)：本窗口跑过识别才有值，否则空串。

        与 _ocr_payload_for_save 同一判据（_ocr_done）——保证 model_tier 为空
        当且仅当 ocr_boxes 为 NULL，两者语义一致、不会各说各话。
        """
        if getattr(ov, "_ocr_done", False):
            return self._ocr_runtime_info()
        return "", ""

    def _update_history_ocr(self, rid, boxes) -> bool:
        """把识别结果写回指定历史记录并刷新面板。

        拖放批量识别 / Ctrl+R 重识别 / 打开历史图补齐识别 共用此路径，
        保证「待识别 → 有文字」时面板同步刷新；同时补写本次的执行记录
        （model_tier / device），这是这两个字段唯一的写入点。
        """
        if not rid or self.history is None:
            return False
        try:
            ocr_text, ocr_boxes = self._record_ocr_payload(boxes)
            tier, dev = self._ocr_runtime_info()
            ok = bool(self.history.update_ocr(rid, ocr_text, ocr_boxes,
                                              tier, dev))
        except Exception as e:  # noqa: BLE001
            log.exception("更新历史记录 OCR 结果失败 #%s: %s", rid, e)
            return False
        if self._history_panel is not None and self._panel_alive(self._history_panel):
            try:
                self._history_panel.reload()
            except Exception:  # noqa: BLE001
                pass
        return ok

    def _set_ocr_running_status(self, ov):
        """识别中提示：按引擎实际状态给文案（常驻已就绪不报「首次加载模型」）。"""
        from ui.base.texts import T
        eng = getattr(self, "engine", None)
        if eng is not None and getattr(eng, "_engine", None) is not None:
            ov._set_status(T("strip.status_ocr", "识别中…"))
        elif getattr(self, "_engine_loading", False):
            ov._set_status(T("strip.status_ocr_loading",
                             "识别中…（模型加载中，请稍候）"))
        else:
            ov._set_status(T("strip.status_ocr_first",
                             "识别中…（首次加载模型约 1-3s）"))

    def _save_record(self, ov):
        """将当前截图落库（纯保存，不展示）。已存过则跳过。"""
        if ov is None or self.history is None:
            return
        if getattr(ov, "history_id", None) is not None:
            return
        try:
            img = getattr(ov, "img", None)
            if img is None:
                return
            ok, ext, image_bytes, thumb_bytes = self._encode_image_pair(img)
            if not ok:
                return
            boxes = getattr(ov, "boxes", []) or []
            # M2+++ QR 存储定稿：内容进 ocr_text（[QR] 前缀行）、几何进
            # ocr_boxes（kind="qr"），不新建字段（见 _record_ocr_payload）
            ocr_text, ocr_boxes = self._ocr_payload_for_save(ov, boxes)
            # 执行记录（实际档位/生效设备）：本窗口跑过识别才有值，否则空串
            _tier, _dev = self._save_ocr_meta(ov)
            rid = self.history.save(
                image_bytes=image_bytes, thumb_bytes=thumb_bytes,
                ocr_text=ocr_text, ocr_boxes=ocr_boxes,
                model_tier=_tier, device=_dev,
                ext=ext,
                name_pattern=self.config.get("software",
                                             "name_pattern", ""),
            )
            ov.history_id = rid   # 供 Ctrl+R 覆盖同一记录
            log.info("已保存历史记录 #%s（%d 块文字）", rid, len(boxes))
        except Exception as e:  # noqa: BLE001
            log.exception("保存历史记录失败: %s", e)

    def _ensure_save(self, ov):
        """用户操作过：已 OCR（_ocr_done）或已有可识别文字（boxes 非空）即立即落库，
        否则标记待 OCR 完成后落库。
        覆盖「从历史恢复 / 未打 _ocr_done 标记但有文字」等场景——鼠标复制文字也应落库。"""
        if ov is None or self.history is None:
            return
        if getattr(ov, "_ocr_done", False) or getattr(ov, "boxes", None):
            self._save_record(ov)
        else:
            ov._pending_save = True

    def _maybe_decode_qr(self, ov, boxes):
        """二维码识别（[software] enable_qr 开时）：对原图跑 zxing，kind="qr" 并入 boxes。

        zxing 不可用 / 无码时原样返回；识别失败不阻断 OCR 主流程。
        """
        if not getattr(ov, "_qr_enabled", False):
            return boxes
        try:
            from core_engine.qr_engine import qr_decode
            img = getattr(ov, "img", None)
            qr = qr_decode(img) if img is not None else []
        except Exception:   # noqa: BLE001
            qr = []
        if not qr:
            return boxes
        merged = list(boxes) + qr
        try:
            ov._set_status(f"已识别 {len(boxes)} 块文字 · {len(qr)} 个二维码")
        except Exception:   # noqa: BLE001
            pass
        return merged

    def _on_ocr_done(self, ov, boxes):
        if ov is not None:
            boxes = self._maybe_decode_qr(ov, boxes)
            ov.set_boxes(boxes)
            ov._ocr_done = True
            rid = getattr(ov, "history_id", None)
            if rid is not None:
                # 历史恢复的图（打开时无结果 → 补齐识别）：结果写回**原记录**，
                # 否则面板会一直停在「待识别」（_save_record 对已有关联 id 直接跳过）
                self._update_history_ocr(rid, boxes)
            elif getattr(ov, "_pending_save", False):
                # 用户操作过（待保存）→ OCR 出最新文字后落库
                self._save_record(ov)

    # ---- M2·γ：Ctrl+R 重新抓取当前区域并识别，覆盖旧记录（D-M2-3）----
    def _reocr(self):
        ov = self._overlay
        if ov is None:
            return
        self._save_record(ov)   # Ctrl+R 重新 OCR = 操作，确保落库
        # 优先用贴图窗口保存的原始截图重新识别（与表格识别 _table_recognize 一致）：
        # 重新抓屏会抓到窗口自身（遮挡）、历史恢复时 raw_bbox 已失效（区域早非原内容），
        # 导致识别错乱；ov.img 即「当初截图/恢复时的原始图像」。
        img = getattr(ov, "img", None)
        if img is None:
            try:
                img = self.svc.grab_region(ov.raw_bbox)
            except Exception as e:  # noqa: BLE001
                self.tray.showMessage("ClickOCR", f"重新截图失败：{e}",
                                      QSystemTrayIcon.Warning, 3000)
                return
        rid = ov.history_id
        self._submit_ocr(ov, rid, img, "reocr")

    def _on_reocr_done(self, ov, rid, boxes, img):
        if ov is not None:
            ov.img = img               # 同步新内容：后续表格识别/复制/钉图用最新截图
            boxes = self._maybe_decode_qr(ov, boxes)
            ov.set_boxes(boxes)
            ov._set_status(f"已重新识别 {len(boxes)} 块 · 覆盖历史记录")
            # 图片内容已变：旧表格窗口结果失效 + 表格模式网格按新文字块重检
            if getattr(ov, "_table_mode", False):
                ov._apply_table_view()
        # 图片内容已变：旧 SLANet 结果失效（含代次，识别中也会被作废）
        self._img_gen += 1
        self._table_result = None
        self._table_gen = -1
        if rid:
            ok = self._update_history_ocr(rid, boxes)
            log.info("重新 OCR 覆盖历史 #%s：%s", rid, "OK" if ok else "FAIL")

    # ---- M2（由 M4 提前）：表格识别 ----
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
    def _save_annotations(self, ov, json_text: str):
        self._save_record(ov)   # 标注前确保历史记录已存在（标注需 history_id）
        rid = getattr(ov, "history_id", None)
        if not rid or self.history is None:
            return
        try:
            self.history.update_annotations(rid, json_text)
            log.info("已保存标注到历史 #%s（%d 字节）", rid, len(json_text))
        except Exception as e:  # noqa: BLE001
            log.exception("保存标注失败: %s", e)
        # 标注更新了操作时间 → 若历史面板打开则刷新顺序（该记录置顶）
        if self._history_panel is not None and self._panel_alive(self._history_panel):
            try:
                self._history_panel.reload()
            except Exception:  # noqa: BLE001
                pass

    def _favorite_overlay(self, ov):
        """收藏/取消收藏当前截图：未保存过则先存入历史再收藏；
        已收藏则再点取消收藏。改完统一走 _sync_favorite 同步所有 UI。"""
        if self.history is None or ov is None:
            return
        rid = getattr(ov, "history_id", None)
        if rid is None:
            # 未保存过 → 保存并收藏
            try:
                img = ov.img
                ok, ext, image_bytes, thumb_bytes = self._encode_image_pair(img)
                if not ok:
                    return
                ocr_text = "\n".join(b.text for b in ov.boxes)
                ocr_boxes = [
                    {"text": b.text, "polygon": b.polygon, "score": b.score}
                    for b in ov.boxes
                ]
                if not ocr_boxes and not getattr(ov, "_ocr_done", False):
                    ocr_boxes = None   # 未识别过：写 NULL（待识别），别写成 '[]'
                _tier, _dev = self._save_ocr_meta(ov)
                rid = self.history.save(
                    image_bytes=image_bytes, thumb_bytes=thumb_bytes,
                    ocr_text=ocr_text, ocr_boxes=ocr_boxes,
                    model_tier=_tier, device=_dev,
                    ext=ext,
                    name_pattern=self.config.get("software",
                                                 "name_pattern", ""))
                ov.history_id = rid
            except Exception as e:  # noqa: BLE001
                log.exception("收藏保存失败: %s", e)
                return
            self.history.set_favorite(rid, True)
            self._sync_favorite(rid, True)
            return
        # 已有记录：切换收藏状态（收藏 ↔ 取消收藏）
        rec = self.history.get(rid)
        fav = bool(rec and rec["is_favorite"])
        new_fav = not fav
        self.history.set_favorite(rid, 1 if new_fav else 0)
        self._sync_favorite(rid, new_fav)

    def _sync_favorite(self, rid, fav: bool):
        """收藏状态变更后同步所有相关 UI：
        - 已打开的历史面板刷新列表（反映最新收藏状态，含收藏 Tab 增删）
        - 匹配 rid 的贴图/钉图窗口状态栏星标同步（含触发源）"""
        # 1) 历史面板：若打开则刷新（幂等；收藏 Tab 需重建以增删条目）
        if self._history_panel is not None and self._panel_alive(self._history_panel):
            try:
                self._history_panel.reload()
            except Exception:  # noqa: BLE001
                pass
        # 2) 贴图/钉图窗口：匹配 rid 的工具栏星标同步
        for ov in list(self._overlays) + list(self._pins):
            if getattr(ov, "history_id", None) == rid:
                tb = getattr(ov, "_toolbar", None)
                if tb is not None and tb.btn_fav is not None:
                    from ui.base.icon import icon as svg_icon
                    from ui.base.theme import accent_color
                    tb.btn_fav.setIcon(
                        svg_icon("star-filled" if fav else "star",
                                 color=accent_color(), size=18))
                    tb.btn_fav.setToolTip(
                        "已收藏 · 点击取消收藏" if fav else "收藏当前截图")

    def _on_overlay_done(self, ov):
        """「完成」按钮：保存当前截图到历史（操作过的都应留存），并刷新历史面板。"""
        if ov is None or self.history is None:
            return
        self._save_record(ov)   # 完成 = 操作，直接落库当前状态
        # 已打开的历史面板刷新（反映新记录）
        if self._history_panel is not None and self._panel_alive(self._history_panel):
            try:
                self._history_panel.reload()
            except Exception:  # noqa: BLE001
                pass

    def _on_copied(self, ov, _text):
        """复制 OCR 文字到剪贴板：复制 = 操作，确保落库。"""
        self._ensure_save(ov)

    def _on_overlay_closed(self, ov):
        """贴图/历史图窗口被用户关闭（✕/Esc/完成）：从 _overlays 移除并彻底释放。

        避免已关闭窗口残留列表（否则数量配额计数含死窗口、列表持续膨胀）。
        同时断开所有 partial 信号连接（消除 ov ↔ partial 自引用环）、销毁窗口、
        触发 gc 回收——修复窗口关闭后内存不释放导致 RSS 持续上涨。
        """
        if ov in self._overlays:
            self._overlays.remove(ov)
        # 「截图无操作也保存」开启时：截图窗口未做任何操作就被关闭 → 仍落库
        # （_save_record 已判 history_id 为空才写，历史恢复的窗口自动跳过）
        if self.config.getbool("software", "save_no_op_close", False):
            self._save_record(ov)
        self._dispose_overlay(ov)

    def _dispose_overlay(self, ov):
        """断开窗口的全部信号连接、销毁窗口、回收内存（幂等，可安全多次调用）。"""
        # 断开所有 partial 绑定的信号：消除 partial(.,ov) ↔ ov 持有信号 的自引用环，
        # 否则 Python 引用计数无法归零、C++ 对象不析构、图像/场景内存不释放。
        for sig_name in (
            "reocr_requested", "table_requested", "table_window_requested",
            "annotations_changed", "fav_requested", "pin_toggled",
            "ocr_needed", "done_requested", "copied", "closed",
        ):
            try:
                sig = getattr(ov, sig_name, None)
                if sig is not None:
                    sig.disconnect()
            except Exception:  # noqa: BLE001
                pass
        try:
            ov.deleteLater()
        except Exception:  # noqa: BLE001
            pass
        import gc
        gc.collect()

    # ---- 钉图：图片 + 状态栏 + 工具栏一起隐藏，只留 PinDot 钉子 ----
    def _pin_overlay(self, ov, pinned: bool):
        """钉图按钮单击：pinned=True 隐藏图片/状态栏/工具栏，只留钉子放图片
        右下角；False 恢复全部浮层。"""
        if ov is None:
            return
        if pinned:
            if ov in self._overlays:      # 钉图从贴图列表移出（不占贴图配额）
                self._overlays.remove(ov)
            if ov not in self._pins:
                self._pins.append(ov)
            self._trim_pins()
            ov._close_sub_windows()       # 关闭标注子浮层等次级浮窗
            ov.set_pinned(True, hide_img=False)   # 隐藏状态栏+工具栏（图片保留）
            dot = ov._ensure_pin_dot()
            # 钉子定位：图片内右下角（图片显示态贴角；无工具栏时也贴角）
            dot.resize(32, 32)
            dot.move(ov.x() + ov.width() - 32 - 6,
                     ov.y() + ov.height() - 32 - 6)
            dot.show()
            dot.raise_()
            self._ensure_save(ov)          # 钉图 = 操作，确保落库（已存在则跳过）
        else:
            self._unpin(ov)

    def _unpin(self, ov):
        """取消钉图：恢复图片/状态栏/工具栏，回贴图列表并按配额裁剪。"""
        if ov in self._pins:
            self._pins.remove(ov)
        ov.set_pinned(False)
        if ov._pin_dot is not None:
            ov._pin_dot.hide()
        if ov._toolbar is not None:
            ov._toolbar.btn_pin.setChecked(False)
        ov._sync_status_strip_pos()
        ov._sync_toolbar_pos()
        if ov not in self._overlays:
            self._overlays.append(ov)
            self._trim_overlays()        # 回列表后按两类配额裁剪（最旧优先）

    def _trim_overlays(self):
        """按两类配额裁剪 _overlays 窗口（各自从最旧开始）：
        - 截图贴图（非持久）：[software] max_history_windows，默认 1
        - 历史贴图（持久，从历史恢复的图）：[software] max_hist_windows，默认 3
        钉图在 _pins 独立管理，不占贴图配额（取消钉图回列表时按配额裁剪）。
        """
        self._trim_group(
            False, max(1, self.config.getint(
                "software", "max_history_windows", 1)))
        self._trim_group(
            True, max(1, self.config.getint(
                "software", "max_hist_windows", 3)))

    def _trim_group(self, persistent: bool, limit: int):
        """裁剪 _overlays 中 persistent 状态 == persistent 的窗口到 limit 个。"""
        while sum(1 for o in self._overlays
                  if bool(getattr(o, "_persistent", False)) == persistent) > limit:
            victim = None
            for old in self._overlays:      # 列表头部 = 最旧
                if bool(getattr(old, "_persistent", False)) == persistent:
                    victim = old
                    break
            if victim is None:
                break
            self._overlays.remove(victim)
            victim.close()          # → closeEvent → closed 信号 → _on_overlay_closed 完整清理
            if victim._status_strip is not None:
                victim._status_strip.close()
                victim._status_strip.deleteLater()
            if victim._toolbar is not None:
                victim._toolbar.close()
                victim._toolbar.deleteLater()

    def _trim_pins(self):
        """钉图上限（[software] max_pin_windows，默认 3，上限 20）：超出恢复最旧。"""
        maxp = max(1, min(20, self.config.getint(
            "software", "max_pin_windows", 3)))
        while len(self._pins) > maxp:
            old = self._pins.pop(0)
            self._unpin(old)             # 恢复为普通窗体（不丢失）
            log.info("钉图超上限，恢复最旧窗口")

    def _toggle_through_top(self):
        """Alt+F 全局热键：切换最上层可见贴图/钉图窗口的点击穿透。

        穿透时鼠标点击落到桌面（可操作下层程序），窗口仍可见；再按 Alt+F 取消。
        """
        wins = [w for w in list(self._overlays) + list(self._pins)
                if w.isVisible()]
        if not wins:
            return
        wins[-1].toggle_through()        # 列表尾部 = 最近打开的，视为最上层
