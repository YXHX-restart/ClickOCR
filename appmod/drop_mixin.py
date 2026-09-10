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
from ui.windows.selection import SelectionOverlay
from ui.windows.result_overlay import ResultOverlay
from appmod.workers import OcrWorker, TableWorker, EngineLoader

log = get_logger("app")
try:  # 统一解码实现下沉在 core_engine（桌面端/服务端共用，含 HEIC/AVIF + EXIF 纠正）
    from core_engine.image_decode import decode_image_bytes as _decode_image_bytes
except Exception:  # noqa: BLE001 core_engine 缺失时退化：仅支持 cv2 原生格式
    def _decode_image_bytes(raw) -> "np.ndarray | None":
        return cv2.imdecode(raw, cv2.IMREAD_COLOR)




class DropMixin:
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



