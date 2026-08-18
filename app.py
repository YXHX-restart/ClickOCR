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
from functools import partial
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import Qt, QRect, QPoint, QThread, Signal, QTimer
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QAction
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu

from core_engine.ai_pipeline import MODEL_SET_TO_TIER
from core_engine.db_manager import DBManager, HistoryStorage
from core_engine.logging_setup import setup_logging, get_logger

from ocr import RapidOcrEngine
from ocr.types import OcrBox
from screenshot import ScreenshotService
from config import ConfigManager
from hotkey import HotkeyHost
from ui.theme import apply_theme, resolve_theme, install_system_follow
from ui.selection import SelectionOverlay
from ui.result_overlay import ResultOverlay

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


class OcrWorker(QThread):
    done = Signal(object, list)

    def __init__(self, engine: RapidOcrEngine, img):
        super().__init__()
        self.engine = engine
        self.img = img

    def run(self):
        boxes = self.engine.recognize(self.img)
        self.done.emit(self.img, boxes)


class TableWorker(QThread):
    """表格识别 worker：SLANet 惰性加载 + 结构识别（UI 线程不执行推理）。"""
    done = Signal(object)          # TableResult
    failed = Signal(str)

    def __init__(self, table_engine, img):
        super().__init__()
        self.table_engine = table_engine
        self.img = img

    def run(self):
        try:
            result = self.table_engine.recognize_table(self.img)
            self.done.emit(result)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


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

    def __init__(self, app_root=None):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        # Windows 下系统 UI 统一微软雅黑：避免 ⊛/☆ 等符号回退到
        # 其他字体（Segoe UI Symbol 等）显示偏小/粗细不一致
        # 注意：必须显式指定 9pt（QFont(family) 默认 12pt，会让文字整体变大）
        if sys.platform == "win32":
            self.app.setFont(QFont("Microsoft YaHei", 9))

        self.config = ConfigManager(app_root=app_root)
        setup_logging(logs_dir=self.config.logs_dir)
        # ---- UI 主题（明/暗/跟随系统）----
        apply_theme(self.app, resolve_theme(self.config))
        install_system_follow(self.app, self.config)
        use_cls = self.config.getbool("ocr", "use_cls", False)
        model_set = self.config.get("ocr", "model_set", "v6-tiny")
        # GPU 加速（[ocr] device = cpu | cuda(NVIDIA) | dml(AMD/Intel/通用)）：
        # 对应 GPU 版 onnxruntime 就绪时启用，探测失败自动回退 CPU（引擎内部处理）
        dev = self.config.get("ocr", "device", "cpu").lower()
        self.accel = dev if dev in ("cuda", "dml") else "cpu"
        self.combo = self.config.get("hotkey", "combo", "ctrl+shift+r")
        self.model_tier = MODEL_SET_TO_TIER.get(model_set, "tiny")
        # OCR 引擎常驻（[ocr] ocr_keep_alive，默认 true）：
        # false = 启动不加载模型（省内存），首次识别时才构建（懒加载）
        self.keep_alive = self.config.getbool("ocr", "ocr_keep_alive", True)
        self.ocr_auto = self.config.getbool("ocr", "ocr_auto", True)
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
                max_records=self.config.getint("db", "screenshot_max_count", 50))
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
        self._pins = []              # 钉图窗口列表（[ui] max_pin_windows，默认 3，上限 20）
        self._worker = None
        self._table_worker = None
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
        from ui.texts import T
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
        from ui.history_panel import HistoryPanel
        panel = HistoryPanel(
            self.history, self.restore_record, self.config.get_screenshots_dir(),
            width=self.config.getint("ui", "history_panel_width", 620),
            cards=self.config.getint("ui", "history_visible_cards", 5),
            on_favorite_changed=self._sync_favorite,
            on_files_dropped=self._on_history_files_dropped)
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
        随后按落库顺序后台串行 OCR（不受 ocr_auto 影响；FIFO 由 save 内部处理）。

        paths: 经面板过滤、去重后的本地图片绝对路径列表。
        """
        if self.history is None:
            return
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
            img = cv2.imdecode(raw, cv2.IMREAD_COLOR)
            if img is None:
                log.warning("拖放图片解码失败（非图像或损坏），跳过: %s", p)
                continue
            # 编码主图（遵循 [screenshot] save_format / save_quality）
            ok, ext, buf = self._encode_image(img)
            if not ok:
                continue
            image_bytes = buf.tobytes()
            # 缩略图：复用 _save_record 第 746–751 行（宽 160px，主图同格式）
            h, w = img.shape[:2]
            tw = 160
            th = max(1, int(round(h * tw / w)))
            thumb = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
            _, _, tbuf = self._encode_image(thumb)
            # 落库：让 HistoryStorage 负责写入 screenshots/ + thumb/（不自己 copy）。
            # ocr_text="" → 待识别态（面板显示 "(无文字)"）；FIFO 清理在 save 内自动生效。
            try:
                rid = self.history.save(
                    image_bytes=image_bytes,
                    thumb_bytes=tbuf.tobytes(),
                    ocr_text="",
                    ocr_boxes=[],
                    model_tier=self.model_tier,
                    device="cpu",
                    ext=ext,
                    name_pattern=self.config.get("screenshot", "name_pattern", ""),
                )
            except Exception as e:  # noqa: BLE001
                log.exception("拖放落库失败: %s", e)
                continue
            pending.append((rid, img))

        if not pending:
            return

        # 立即刷新面板：新记录以「待识别」(无文字) 态出现
        if self._history_panel is not None and self._panel_alive(self._history_panel):
            try:
                self._history_panel.reload()
            except Exception:  # noqa: BLE001
                pass

        # 落库即触发后台序列 OCR（与 ocr_auto 无关）
        self._drop_ocr_queue = pending
        self._drop_ocr_start_next()

    def _drop_ocr_start_next(self):
        """取出队首记录启 OCR；前图完成（_on_drop_ocr_done）才启下一张。"""
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
        self._drop_ocr_rid = rid
        worker = OcrWorker(engine, img)
        self._drop_ocr_worker = worker
        worker.done.connect(
            lambda _im, boxes, _rid=rid: self._on_drop_ocr_done(_rid, boxes))
        worker.start()

    def _on_drop_ocr_done(self, rid, boxes):
        """单张 OCR 完成：覆盖记录文字 + 刷新面板，再启下一张。"""
        try:
            ocr_text = "\n".join(b.text for b in boxes)
            ocr_boxes = [
                {"text": b.text, "polygon": b.polygon, "score": b.score}
                for b in boxes
            ]
            self.history.update_ocr(rid, ocr_text, ocr_boxes)
        except Exception as e:  # noqa: BLE001
            log.exception("拖放 OCR 结果更新失败 #%s", e)
        # 刷新历史面板（待识别 → 有文字）
        if self._history_panel is not None and self._panel_alive(self._history_panel):
            try:
                self._history_panel.reload()
            except Exception:  # noqa: BLE001
                pass
        # 前图 done → 启下一张（队列空则自然结束）
        self._drop_ocr_start_next()


    def open_settings(self):
        from ui.settings_panel import SettingsPanel
        if self._settings_panel is None:
            self._settings_panel = SettingsPanel(
                self.config, self.hotkey, self.history, tray=self.tray)
        self._settings_panel.show()
        self._settings_panel.raise_()
        self._settings_panel.activateWindow()

    def restore_record(self, rec: dict):
        """从历史记录恢复贴图：加载截图 + 文字层，可再次选字复制（FR-17）。

        - 关联原记录 id（ov.history_id）：标注/Ctrl+R/收藏等操作都落在**原记录**上，
          不会因 history_id=None 而新建重复记录。
        - 定位到历史面板左侧（垂直居中）；左边放不下则放右侧，不再夹到屏幕左上角。
        """
        try:
            rel = rec["screenshot_path"]
            # 截图目录可配置（[paths] screenshots_dir），按文件名解析到当前目录
            full = self.config.get_screenshots_dir() / Path(rel).name
            if not full.exists():
                raise OSError(f"截图文件缺失: {rel}")
            img = cv2.imdecode(np.fromfile(str(full), dtype=np.uint8),
                               cv2.IMREAD_COLOR)
            if img is None:
                raise OSError(f"截图文件缺失: {rel}")
            h, w = img.shape[:2]
            import json as _json
            boxes = [
                OcrBox(text=str(b.get("text", "")),
                       polygon=b.get("polygon", [[0, 0], [0, 0], [0, 0], [0, 0]]),
                       score=float(b.get("score", 0.0)))
                for b in _json.loads(rec["ocr_boxes"] or "[]")
            ]
            ov = self._make_overlay(img, (0, 0, w, h), boxes, persistent=True)
            ov.history_id = rec["id"]        # 关联原记录：后续操作不新建、直接更新原记录
            self._place_restored_overlay(ov) # 先定位（不依赖 load_annotations 成败）
            try:
                ov.load_annotations(rec.get("annotations"))
            except Exception:  # noqa: BLE001
                log.exception("恢复标注失败（图片已正常显示/定位）")
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
        self.history_combo = self.config.get("hotkey_ext", "history_hotkey", "ctrl+`")
        if self.history_combo and self.history_combo.lower() not in ("off", "none", "false"):
            self.hotkey.register_secondary(self.history_combo, self.open_history, 2)
        # 全屏截图热键（[hotkey_full] combo，默认 off；注册后直接截全屏）
        self.full_combo = self.config.get("hotkey_full", "combo", "off")
        if self.full_combo and self.full_combo.lower() not in ("off", "none", "false"):
            self.hotkey.register_secondary(self.full_combo, self.capture_fullscreen, 3)
        # 点击穿透全局热键（[hotkey_ext] through_hotkey，默认 alt+f；off 关闭）
        # 贴图/钉图窗口穿透时鼠标落到桌面，热键随时取消（穿透状态下窗口失焦也有效）
        self.through_combo = self.config.get("hotkey_ext", "through_hotkey", "alt+f")
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
        if not sys.platform.startswith("win"):
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
                speed=self.config.getint("screenshot", "scroll_speed", 2),
                max_height=self.config.getint("screenshot",
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

        贴图窗口上限（[ui] max_history_windows，默认 1）只约束**普通贴图**；
        钉图（在 _pins）与历史打开的图（persistent=True，独立窗体）不受此限制。
        [result] strip_enabled=false：不建状态栏；toolbar_enabled=false：不建工具栏。
        """
        # 新图进入（框选/全屏/滚动/历史恢复的唯一汇合点）：
        # 旧表格识别结果与「待开窗」请求全部作废，
        # 防止长按「表格」复用上一张图的识别结果（应针对当前图重新识别）。
        self._img_gen += 1
        self._table_result = None
        self._table_gen = -1
        self._table_pending_window = False
        from ui.result_overlay import ResultOverlay
        from ui.status_strip import StatusStrip
        from ui.toolbar import ToolBar
        maxw = max(1, self.config.getint("ui", "max_history_windows", 1))
        self._trim_overlays(maxw - 1)     # append 新图后普通贴图恰为 maxw 个
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
        # 纯信息状态栏（[result] strip_enabled，默认开）
        if self.config.getbool("result", "strip_enabled", True):
            strip = StatusStrip()
            ov.set_status_strip(strip)
        # 统一工具栏（[result] toolbar_enabled，默认开）
        if self.config.getbool("result", "toolbar_enabled", True):
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
        engine = self._ensure_engine()
        if engine is None:
            return
        self._worker = OcrWorker(engine, img)
        self._worker.done.connect(
            lambda _im, boxes, _ov=ov: self._on_ocr_done(_ov, boxes))
        self._worker.start()

    def _ocr_on_demand(self, ov):
        """「截图后自动 OCR」关闭时：点文字框/表格临时拉起 OCR。
        懒加载引擎（keep_alive=false）此时才构建模型（首次约 1-3s）。"""
        self._ensure_save(ov)           # 点文字框/表格 = 操作，确保落库
        if ov is None or getattr(ov, "boxes", None):
            return                       # 已有结果无需再识别
        img = getattr(ov, "img", None)
        if img is None:
            return
        ov._set_status("识别中…（首次加载模型约 1-3s）")
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
        from ui.texts import T
        from ui.theme_dialog import ThemeDialog
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
        """按 [screenshot] save_format/save_quality 编码图片。

        返回 (ok, ext, buf)；有损格式（jpg/webp）带质量参数，
        png/bmp 无损忽略质量。
        """
        fmt = str(self.config.get("screenshot", "save_format", "png")).lower()
        if fmt not in ("png", "jpg", "jpeg", "bmp", "webp"):
            fmt = "png"
        ext = "jpg" if fmt == "jpeg" else fmt
        params = []
        if fmt in ("jpg", "jpeg", "webp"):
            q = max(1, min(100, self.config.getint(
                "screenshot", "save_quality", 90)))
            key = (cv2.IMWRITE_JPEG_QUALITY if fmt in ("jpg", "jpeg")
                   else cv2.IMWRITE_WEBP_QUALITY)
            params = [key, q]
        ok, buf = cv2.imencode("." + ext, img, params)
        return ok, ext, buf

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
            ok, ext, buf = self._encode_image(img)
            if not ok:
                return
            image_bytes = buf.tobytes()
            # 缩略图（宽 160px，与主图同格式，文件名一致）
            h, w = img.shape[:2]
            tw = 160
            th = max(1, int(round(h * tw / w)))
            thumb = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
            _, _, tbuf = self._encode_image(thumb)
            boxes = getattr(ov, "boxes", []) or []
            ocr_text = "\n".join(b.text for b in boxes)
            ocr_boxes = [
                {"text": b.text, "polygon": b.polygon, "score": b.score}
                for b in boxes
            ]
            rid = self.history.save(
                image_bytes=image_bytes, thumb_bytes=tbuf.tobytes(),
                ocr_text=ocr_text, ocr_boxes=ocr_boxes,
                model_tier=self.model_tier, device="cpu",
                ext=ext,
                name_pattern=self.config.get("screenshot",
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

    def _on_ocr_done(self, ov, boxes):
        if ov is not None:
            ov.set_boxes(boxes)
            ov._ocr_done = True
            # 用户操作过（待保存）→ OCR 出最新文字后落库
            if getattr(ov, "_pending_save", False):
                self._save_record(ov)

    # ---- M2·γ：Ctrl+R 重新抓取当前区域并识别，覆盖旧记录（D-M2-3）----
    def _reocr(self):
        ov = self._overlay
        if ov is None:
            return
        self._save_record(ov)   # Ctrl+R 重新 OCR = 操作，确保落库
        try:
            img = self.svc.grab_region(ov.raw_bbox)
        except Exception as e:  # noqa: BLE001
            self.tray.showMessage("ClickOCR", f"重新截图失败：{e}",
                                  QSystemTrayIcon.Warning, 3000)
            return
        rid = ov.history_id
        self._worker = OcrWorker(self._ensure_engine(), img)
        self._worker.done.connect(
            lambda _im, boxes, _ov=ov, _rid=rid:
            self._on_reocr_done(_ov, _rid, boxes, _im))
        self._worker.start()

    def _on_reocr_done(self, ov, rid, boxes, img):
        if ov is not None:
            ov.img = img               # 同步新内容：后续表格识别/复制/钉图用最新截图
            ov.set_boxes(boxes)
            ov._set_status(f"已重新识别 {len(boxes)} 块 · 覆盖历史记录")
            # 图片内容已变：旧表格窗口结果失效 + 表格模式网格按新文字块重检
            if getattr(ov, "_table_mode", False):
                ov._apply_table_view()
        # 图片内容已变：旧 SLANet 结果失效（含代次，识别中也会被作废）
        self._img_gen += 1
        self._table_result = None
        self._table_gen = -1
        if rid and self.history is not None:
            ocr_text = "\n".join(b.text for b in boxes)
            ocr_boxes = [
                {"text": b.text, "polygon": b.polygon, "score": b.score}
                for b in boxes
            ]
            ok = self.history.update_ocr(rid, ocr_text, ocr_boxes)
            log.info("重新 OCR 覆盖历史 #%s：%s", rid, "OK" if ok else "FAIL")

    # ---- M2（由 M4 提前）：表格识别 ----
    def _table_recognize(self, ov):
        """Ctrl+Shift+T / 表格识别：异步执行 SLANet 表格结构识别 + 弹出预览面板。"""
        if self.table_engine is None:
            self.tray.showMessage(
                "ClickOCR", "表格识别不可用（rapid_table 未安装）",
                QSystemTrayIcon.Warning, 3000)
            return
        if self._table_worker is not None and self._table_worker.isRunning():
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
        self._table_worker = TableWorker(self.table_engine, img)
        self._table_worker.done.connect(self._on_table_done)
        self._table_worker.failed.connect(self._on_table_failed)
        self._table_worker.start()

    def _on_table_done(self, result):
        """SLANet 表格识别完成（仅双击/长按/Ctrl+Shift+T 开窗时触发）：
        存结果 + 卸载 SLANet（不常驻内存）+ 需要时弹窗。"""
        # 识别期间图片已切换/内容已变：结果作废（不缓存、不弹窗）
        if self._table_gen != self._img_gen:
            log.info("表格识别完成但图片已变更，结果作废")
            self._table_worker = None
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
        if self._table_worker is not None and self._table_worker.isRunning():
            self._table_pending_window = True
            return
        self._table_pending_window = True
        self._table_gen = self._img_gen    # 本次识别绑定当前图片代次
        if self._overlay is not None:
            self._table_recognize(self._overlay)

    def _show_table_panel(self, result):
        try:
            from ui.table_panel import TablePreviewPanel
            panel = TablePreviewPanel(result, self.config)
            panel.closed.connect(
                lambda p=panel: self._close_table_panel(p))
            panel.show()
            panel.raise_()
            self._table_panels.append(panel)
        except Exception as e:  # noqa: BLE001
            log.exception("表格预览面板打开失败: %s", e)

    def _on_table_failed(self, msg):
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
                ok, ext, buf = self._encode_image(img)
                if not ok:
                    return
                h, w = img.shape[:2]
                tw = 160
                th = max(1, int(round(h * tw / w)))
                thumb = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
                _, _, tbuf = self._encode_image(thumb)
                ocr_text = "\n".join(b.text for b in ov.boxes)
                ocr_boxes = [
                    {"text": b.text, "polygon": b.polygon, "score": b.score}
                    for b in ov.boxes
                ]
                rid = self.history.save(
                    image_bytes=buf.tobytes(), thumb_bytes=tbuf.tobytes(),
                    ocr_text=ocr_text, ocr_boxes=ocr_boxes,
                    model_tier=self.model_tier, device=self.accel,
                    ext=ext,
                    name_pattern=self.config.get("screenshot",
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
                    from ui.icon import icon as svg_icon
                    from ui.theme import accent_color
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
            self._trim_overlays(
                max(1, self.config.getint("ui", "max_history_windows", 1)))

    def _trim_overlays(self, keep: int):
        """裁剪普通贴图（非持久窗体）到 keep 个；钉图/历史图（持久）不受影响。"""
        while sum(1 for o in self._overlays
                  if not getattr(o, "_persistent", False)) > keep:
            victim = None
            for old in self._overlays:
                if not getattr(old, "_persistent", False):
                    victim = old
                    break
            if victim is None:
                break
            self._overlays.remove(victim)
            victim.close()
            victim.deleteLater()
            if victim._status_strip is not None:
                victim._status_strip.close()
                victim._status_strip.deleteLater()
            if victim._toolbar is not None:
                victim._toolbar.close()
                victim._toolbar.deleteLater()

    def _trim_pins(self):
        """钉图上限（[ui] max_pin_windows，默认 3，上限 20）：超出恢复最旧。"""
        maxp = max(1, min(20, self.config.getint("ui", "max_pin_windows", 3)))
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
