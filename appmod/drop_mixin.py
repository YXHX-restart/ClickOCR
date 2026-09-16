"""DropMixin —— 拖放图片批量导入（去重 + 后台序列 OCR）。

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
from ui.base.texts import T
from ui.windows.selection import SelectionOverlay
from ui.windows.result_overlay import ResultOverlay
from appmod.workers import (OcrWorker, TableWorker, EngineLoader,
                            DropImportWorker)

log = get_logger("app")
try:  # 统一解码实现下沉在 core_engine（桌面端/服务端共用，含 HEIC/AVIF + EXIF 纠正）
    from core_engine.image_decode import decode_image_bytes as _decode_image_bytes
except Exception:  # noqa: BLE001 core_engine 缺失时退化：仅支持 cv2 原生格式
    def _decode_image_bytes(raw) -> "np.ndarray | None":
        return cv2.imdecode(raw, cv2.IMREAD_COLOR)




class DropMixin:
    def _on_history_files_dropped(self, paths: list):
        """历史面板拖放回调：**后台线程**逐张读解码 → 落库（空 ocr_text = 待识别），
        完成后按 [software] drop_ocr_auto 决定是否串行 OCR（与截图的 ocr_auto
        相互独立；FIFO 由 save 内部处理）。

        paths: 经面板过滤、去重后的本地图片绝对路径列表。

        原实现整条链路（读 → 解码 → 双编码 → 落库）在 UI 线程同步执行，
        50 张图会让界面冻结 10-25s 且无任何反馈；现下沉到 DropImportWorker。
        """
        if self.history is None:
            return
        if not paths:
            return
        # 去重索引：库内已存在「同名且同大小」的文件时，不新增记录，
        # 而是把对应历史记录刷新 created_at 置顶（允许「重复拖入放前边」）。
        dup_index = self._build_history_dup_index()
        old = getattr(self, "_drop_import_worker", None)
        if old is not None and old.isRunning():
            # 上一批仍在导入：请求其收尾（已导入的记录保持有效），
            # 覆盖引用会让线程失控，故先等它结束再启新批。
            old.stop()
            old.wait(3000)
        w = DropImportWorker(
            self.history, paths, dup_index,
            encode_pair=self._encode_image_pair,
            find_dup=self._find_dup_rid,
            decode=_decode_image_bytes,
            name_pattern=self.config.get("software", "name_pattern", ""),
        )
        w.progressed.connect(self._on_drop_import_progress)
        w.finished_ok.connect(self._on_drop_import_done)
        w.failed.connect(self._on_drop_import_failed)
        self._drop_import_worker = w
        w.start()

    def _on_drop_import_progress(self, done: int, total: int):
        """导入进度：在托盘 tooltip 反映（不打断用户，也不弹窗）。"""
        self._set_engine_status(f"导入中 {done}/{total}…")

    def _on_drop_import_failed(self, msg: str):
        self._drop_import_worker = None
        self._set_engine_status("就绪")
        log.error("拖放导入失败：%s", msg)
        try:
            self.tray.showMessage(
                "ClickOCR", T("app.drop_import_fail", "拖放导入失败：") + str(msg),
                QSystemTrayIcon.Warning, 3000)
        except Exception:  # noqa: BLE001
            pass

    def _on_drop_import_done(self, added, touched: int):
        """导入完成（主线程）：刷新面板 → 提示 → 按开关入队后台 OCR。

        added: [(rid, 原图绝对路径)] —— 队列只存路径，不驻留解码后的全尺寸图。
        """
        self._drop_import_worker = None
        self._set_engine_status("就绪")
        if not added and not touched:
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
            if added:
                self._notify_dropped_imported(len(added))
            return
        # 追加到现有队列而非整体覆盖：识别进行中再次拖放时，前一批剩余记录
        # 仍会被串行识别（覆盖会使其永久停留「待识别」态且无任何提示）。
        self._drop_ocr_queue = list(self._drop_ocr_queue) + added
        # 有任务在途（已提交、回调未回）时不抢提交——worker 单槽，抢提交会
        # 覆盖尚未取走的任务导致其永久丢失；回调收尾时自会取下一张。
        if not self._drop_ocr_busy:
            self._drop_ocr_start_next()

    def _decode_drop_image(self, path: str):
        """读原图并解码（供拖放 OCR 使用）；失败返回 None。

        从「原图路径」而非落库副本解码：落库副本可能是有损格式（jpg/webp/
        heic），会降低识别率。代价是每张多一次解码，但内存占用从 N 张降为 1 张。
        """
        try:
            raw = DropImportWorker._read(path)
        except Exception as e:  # noqa: BLE001
            log.warning("拖放 OCR 读取原图失败: %s (%s)", path, e)
            return None
        img = _decode_image_bytes(raw)
        if img is None:
            log.warning("拖放 OCR 解码原图失败: %s", path)
        return img

    # ---- M2·δ 拖放去重：同名同大小 → 置顶已有记录（不新增、不重复 OCR） ----

    def _build_history_dup_index(self) -> dict:
        """构建库内现有截图的检索索引：{文件名: [(rid, 绝对路径, 字节大小)]}。

        匹配标准 = 文件名 + 文件大小 双匹配，**不比对内容**（零文件内容读取）。

        收敛点（性能）：数据源改用 `HistoryStorage.list_paths()`——只 SELECT
        id/screenshot_path/file_size 三列。原实现 `history.list(limit=100000)`
        是 SELECT *，把每条记录的 ocr_boxes JSON（每块文字的 4 顶点多边形）
        也一起拉回来，3000 条即几十 MB 的瞬时分配 + 反序列化，而这些字段
        一个都没用到；SQL 也收在 HistoryStorage/DBManager 里，不在 mixin 内手写。

        文件大小同样取自 DB 的 `file_size` 列（save() 写库时 = 落盘字节数），
        不再逐条 stat()：N 次系统调用（机械盘/网络盘上明显）直接归零。
        台账口径不变——仍是「同名 + 同大小」，只是数据来源从磁盘元数据换成
        同一份写入记录；DB 里 file_size 缺失/为 0 的少数记录（外部脚本直插等）
        退回 stat() 兜底，行为与改前一致。

        命中 → 调用方 touch 该记录置顶（不新增、不重复 OCR）；改名 / 新副本的
        同图不匹配 → 正常新增入库（同样满足「重复拖入放前边」的用法）。
        库内文件绝对路径 = screenshots_dir / <screenshot_path 的文件名>。
        """
        index: dict = {}
        if self.history is None:
            return index
        try:
            rows = self.history.list_paths()   # (id, screenshot_path, file_size)
        except Exception:  # noqa: BLE001
            return index
        shot_dir = self.config.get_screenshots_dir()
        for rid, sp, fsize in rows:
            name = Path(sp or "").name
            if not name:
                continue
            ap = shot_dir / name
            if not fsize:
                # 极少数记录 file_size 未落值（外部直插 / 旧工具写入）：兜底 stat
                try:
                    fsize = ap.stat().st_size
                except OSError:
                    continue
            index.setdefault(name, []).append((rid, ap, fsize))
        return index


    def _find_dup_rid(self, p: str, size: int, dup_index: dict):
        """拖入文件与库内某记录「同名且同大小」→ 返回该记录 id，否则 None。

        命中候选额外确认文件仍在（一次 stat，仅在已同名同大小时发生）：索引里的
        大小来自 DB，不校验存在性的话，「记录还在、截图文件被外部删掉」的破图
        记录会被当成重复而置顶（不再重新入库/识别）。改前的实现是逐条 stat()，
        这类记录会因 OSError 被跳过 → 走新增；这里保持同一结果。
        """
        for rid, ap, fsize in dup_index.get(Path(p).name, []):
            if fsize == size and Path(ap).exists():
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
        task_id 直接传 rid：回调按任务自身携带的 rid 回写，消除原先用单一
        self._drop_ocr_rid 变量关联结果时的串台（二次拖放会覆盖该变量，
        导致 A 图的识别结果写进 B 图的历史记录）。

        队列元素为 (rid, 原图路径)：本方法按需解码单张（原实现预先把全部
        解码结果常驻内存，50 张 4K 图约 1.2GB）。读图/解码失败则跳过该张
        继续下一张，不阻断整批。
        """
        queue = self._drop_ocr_queue
        if not queue:
            return
        engine = self._ensure_engine()
        if engine is None:
            # 引擎不可用：剩余记录保持待识别态，仅提示一次
            self._warn_ocr_failed()
            self._drop_ocr_queue = []
            return
        w = getattr(self, "_drop_worker", None)
        while queue:
            rid, path = queue.pop(0)
            img = self._decode_drop_image(path)
            if img is None:
                continue                    # 单张失败：跳过，继续下一张
            if w is None:
                w = OcrWorker(engine)
                w.done.connect(self._on_drop_ocr_done)
                w.failed.connect(self._on_drop_ocr_failed)
                self._drop_worker = w
                w.start()
            self._drop_ocr_queue = queue
            self._drop_ocr_busy = True
            w.submit(rid, img)
            return
        self._drop_ocr_queue = []


    def _on_drop_ocr_done(self, seq, img, boxes):
        """单张 OCR 完成：覆盖记录文字 + 刷新面板，再启下一张。"""
        self._drop_ocr_busy = False
        self._update_history_ocr(seq, boxes)
        # 前图 done → 启下一张（队列空则自然结束）
        self._drop_ocr_start_next()


    def _on_drop_ocr_failed(self, seq, msg):
        """拖放 OCR 单张失败：记日志后继续下一张（不阻断整批）。"""
        log.error("拖放 OCR 失败（task %s）：%s", seq, msg)
        self._drop_ocr_busy = False
        self._drop_ocr_start_next()



