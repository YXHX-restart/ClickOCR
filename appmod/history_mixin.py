"""HistoryMixin —— 历史落库语义（ocr_boxes 三态 / 执行记录 / 编码 / 收藏 / 恢复 / 标注持久化）。

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
from core_engine.image_decode import decode_image_bytes as _decode_image_bytes

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
from appmod.image_encode import PILLOW_FORMATS, pillow_encode

log = get_logger("app")


class HistoryMixin:
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
            # 统一解码（cv2 + Pillow 回退）：save_format 可能是 heic/heif/avif，
            # cv2.imdecode 读不了这三种（原实现直接 imdecode，存 HEIC 后打不开历史）。
            # np.fromfile 保留「兼容非 ASCII 路径」的特性。
            raw = np.fromfile(str(full), dtype=np.uint8)
            img = _decode_image_bytes(raw)
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


    def _encode_image(self, img):
        """按 [software] save_format/save_quality 编码图片。

        返回 (ok, ext, buf)；有损格式（jpg/webp/heic/heif/avif）带质量参数，
        png/bmp 无损忽略质量。cv2 覆盖原生格式（png/jpg/bmp/webp）；
        heic/heif/avif cv2 编不了，走 Pillow（见 appmod/image_encode），
        依赖缺失时 ok=False，调用方跳过该条。
        """
        fmt = str(self.config.get("software", "save_format", "png")).lower()
        if fmt not in ("png", "jpg", "jpeg", "bmp", "webp",
                       "heic", "heif", "avif"):
            fmt = "png"
        ext = "jpg" if fmt == "jpeg" else fmt
        q = max(1, min(100, self.config.getint(
            "software", "save_quality", 90)))
        if fmt in PILLOW_FORMATS:
            data = pillow_encode(img, fmt, q)
            if data is None:
                log.warning("图片编码失败（%s）：缺少 Pillow/pillow_heif 或格式不支持", fmt)
                return False, ext, None
            # 包成 ndarray 以保持与 cv2.imencode 一致的返回契约（调用方用 .tobytes()）
            return True, ext, np.frombuffer(data, dtype=np.uint8)
        params = []
        if fmt in ("jpg", "jpeg", "webp"):
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
        其余为 None。缩略图编码失败忽略（写空字节）——不向上抛异常：
        拖放循环等调用方对单张失败只跳过，主图已成功时不应整条丢弃。
        """
        ok, ext, buf = self._encode_image(img)
        if not ok:
            return False, ext, None, None
        h, w = img.shape[:2]
        tw = self.THUMB_WIDTH
        th = max(1, int(round(h * tw / w)))
        thumb = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
        tok, _text, tbuf = self._encode_image(thumb)
        if not tok or tbuf is None:
            log.warning("缩略图编码失败（忽略，落库写空字节）")
            return True, ext, buf.tobytes(), b""
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
        ov = self._live_overlay()   # 统一存活校验（见 FloatMixin._live_overlay）
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
            # 未保存过 → 复用 _save_record 落库再收藏。
            # 原先这里手写了一份保存逻辑（不走 _ocr_payload_for_save），
            # 是三处落库实现中唯一漏掉 kind 字段的——二维码截图直接点收藏
            # 时 QR 标记丢失，恢复后退化为普通文字。
            self._save_record(ov)
            rid = getattr(ov, "history_id", None)
            if rid is None:
                return                    # 落库失败（_save_record 内已记日志）
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
        # 2) 贴图/钉图窗口：匹配 rid 的工具栏星标同步（先验活：钉图态关窗后
        #    _pins 可能短暂残留死引用，对其访问 _toolbar 会抛 RuntimeError）
        for ov in list(self._overlays) + list(self._pins):
            if not self._panel_alive(ov):
                continue
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


