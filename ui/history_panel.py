"""M2-2 历史记录面板（统一浮层基类 CardWindow，样式由全局主题提供）。

日期分组（今天/昨天/本周/更早）+ 缩略图 + 搜索 + 收藏 + 删除 + 恢复贴图。
无边框半透明卡片（明/暗主题），标题栏可拖动，Esc 关闭。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import (Qt, QSize, QUrl, QPoint, QMimeData,
                             QByteArray, QBuffer, QIODevice, QTimer, QRectF)
from PySide6.QtGui import (QPixmap, QDrag, QImage, QPainter, QCursor, QAction)
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLineEdit, QListWidget,
                               QListWidgetItem, QLabel, QPushButton, QFrame,
                               QTabWidget, QApplication, QMenu,
                               QGraphicsScene, QGraphicsPixmapItem, QToolTip)

from ui.card_window import CardWindow
from ui.texts import T
from ui.annotation import anno_from_json, render_annotated_image


def _group_name(ts: float) -> str:
    """按时间戳分到 今天/昨天/本周/更早。"""
    now = datetime.now()
    d = datetime.fromtimestamp(ts)
    today = now.date()
    day = d.date()
    if day == today:
        return "今天"
    if day == today - timedelta(days=1):
        return "昨天"
    monday = today - timedelta(days=today.weekday())
    if day >= monday:
        return "本周"
    return "更早"


def _fmt_time(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    if d.date() == datetime.now().date():
        return d.strftime("%H:%M:%S")
    return d.strftime("%m-%d %H:%M")


# 拖放仅接受的图片扩展名（与 cv2 可解码格式对齐）
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp",
               ".gif", ".tif", ".tiff"}


class _DragImageLabel(QLabel):
    """缩略图标签：左键拖拽把**原图**作为文件拖出（桌面 / 微信 / QQ / WorkBuddy 等）。

    拖出的是完整分辨率截图文件（非缩略图），通过 file:// URL + 图片数据，
    桌面（复制文件）/ 聊天软件输入窗（发送图片）均可接收。
    """

    def __init__(self, img_path: str, parent=None):
        super().__init__(parent)
        self._img_path = img_path
        self._start = None

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._start = e.pos()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._start is None or not (e.buttons() & Qt.LeftButton):
            return super().mouseMoveEvent(e)
        if (e.pos() - self._start).manhattanLength() < \
                QApplication.startDragDistance():
            return super().mouseMoveEvent(e)
        self._start = None
        path = self._img_path
        if not path or not Path(path).exists():
            return
        mime = QMimeData()
        # 主格式：本地文件 URL（桌面 / 微信 / 网页 WorkBuddy 按文件接收）
        mime.setUrls([QUrl.fromLocalFile(path)])
        pm = self.pixmap()
        preview = pm if (pm is not None and not pm.isNull()) else None
        # 图片数据：优先用原图（全分辨率），否则退回缩略图
        src = QPixmap(path)
        if src.isNull() and preview is not None:
            src = preview
        if not src.isNull():
            qimg = src.toImage()
            mime.setImageData(qimg)                       # → Windows CF_DIB（位图）
            # 显式 image/png（网页 / 部分聊天软件）
            ba = QByteArray()
            buf = QBuffer(ba)
            if buf.open(QIODevice.WriteOnly) and qimg.save(buf, "PNG"):
                buf.close()
                if ba.size() > 0:
                    mime.setData("image/png", ba)
            # 显式 CF_DIB 原始字节：QQ 9.7.25 拖拽收图需要 Windows 位图格式，
            # Qt6 的 QDrag 不一定把 application/x-qt-image 暴露成 QQ 可读的 CF_DIB，
            # 故手动生成（BMP 去 14 字节文件头 = CF_DIB）直接写入。
            bmp = QByteArray()
            bbuf = QBuffer(bmp)
            if bbuf.open(QIODevice.WriteOnly) and qimg.save(bbuf, "BMP"):
                bbuf.close()
                dib = bmp.mid(14)
                if dib.size() > 0:
                    mime.setData('application/x-qt-windows-mime;value="CF_DIB"', dib)
        drag = QDrag(self)
        drag.setMimeData(mime)
        if preview is not None:
            drag.setPixmap(preview)
            drag.setHotSpot(QPoint(preview.width() // 2, preview.height() // 2))
        drag.exec(Qt.CopyAction)


class HistoryPanel(CardWindow):
    def __init__(self, history, on_restore, screenshots_dir=None,
                 thumb_width: int = 90, width: int = 620, cards: int = 5,
                 on_favorite_changed=None, on_files_dropped=None):
        super().__init__(title=T("hist.title", "历史记录"), with_close=True)
        self._cards = max(1, min(30, cards))
        # 高度 = 显示卡片数（单列卡片，每行约 84px）+ 标题栏/搜索行/边距
        h = 150 + self._cards * 84
        self.resize(width, min(1080, max(300, h)))

        self._history = history
        self._on_restore = on_restore
        self._on_favorite_changed = on_favorite_changed
        self._on_files_dropped = on_files_dropped
        # 启用拖放：拖图片进历史窗口 → 复制入库 + 后台 OCR
        self.setAcceptDrops(True)
        self._thumb_dir = Path(screenshots_dir) / "thumb" if screenshots_dir \
            else Path.cwd() / "screenshots" / "thumb"
        self._thumb_width = thumb_width

        self.card_lay.setSpacing(10)

        # ---- 搜索行 ----
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(T("hist.search_ph", "搜索历史文字…（回车）"))
        self.search.returnPressed.connect(self.reload)
        b_search = QPushButton(T("hist.btn_search", "搜索"))
        b_search.clicked.connect(self.reload)
        b_clear = QPushButton(T("hist.btn_clear", "清空全部"))
        b_clear.clicked.connect(self._clear_all)
        top.addWidget(self.search, 1)
        top.addWidget(b_search)
        top.addWidget(b_clear)
        self.card_lay.addLayout(top)

        # ---- 列表（全部 / 无文字 / 有文字 / 有标注 / 收藏 五个 Tab，单列卡片）----
        self.tabs = QTabWidget()
        self.list_all = QListWidget()
        self.list_all.itemDoubleClicked.connect(self._on_double)
        self.list_no_text = QListWidget()          # 无文字
        self.list_no_text.itemDoubleClicked.connect(self._on_double)
        self.list_has_text = QListWidget()         # 有文字
        self.list_has_text.itemDoubleClicked.connect(self._on_double)
        self.list_has_anno = QListWidget()         # 有标注
        self.list_has_anno.itemDoubleClicked.connect(self._on_double)
        self.list_fav = QListWidget()
        self.list_fav.itemDoubleClicked.connect(self._on_double)
        self.tabs.addTab(self.list_all, T("hist.tab_all", "全部"))
        self.tabs.addTab(self.list_no_text, T("hist.tab_no_text", "无文字"))
        self.tabs.addTab(self.list_has_text, T("hist.tab_has_text", "有文字"))
        self.tabs.addTab(self.list_has_anno, T("hist.tab_has_anno", "有标注"))
        self.tabs.addTab(self.list_fav, T("hist.tab_fav", "收藏"))
        self.card_lay.addWidget(self.tabs, 1)

        # 右键菜单：复制原图 / 复制图片(带标注) / 收藏(取消收藏) / 删除
        for _lst in (self.list_all, self.list_no_text, self.list_has_text,
                     self.list_has_anno, self.list_fav):
            _lst.setContextMenuPolicy(Qt.CustomContextMenu)
            _lst.customContextMenuRequested.connect(
                lambda pos, l=_lst: self._on_context_menu(l, pos))

        # 公告：提示可将图片拖出到桌面 / 微信 / QQ / WorkBuddy 等
        hint = QLabel(T("hist.drag_hint",
                        "提示：拖动缩略图即可把图片发送到桌面 / 微信 / QQ / WorkBuddy 等"))
        hint.setProperty("kind", "info")
        hint.setWordWrap(True)
        self.card_lay.addWidget(hint)

        self.reload()

    # ---- 数据 ----
    @staticmethod
    def _has_text(r) -> bool:
        return bool((r.get("ocr_text") or "").strip())

    @staticmethod
    def _has_anno(r) -> bool:
        ann = (r.get("annotations") or "").strip()
        return ann not in ("", "[]", "{}", "null", "None")

    def reload(self):
        keyword = self.search.text().strip()
        # 全部 Tab
        if keyword:
            records_all = self._history.search(keyword)
        else:
            records_all = self._history.list(200)
        self._build(records_all, self.list_all, T("hist.empty_all", "暂无历史记录"))
        # 筛选 Tab（在 records_all 基础上按 文字/标注 过滤，与搜索关键词叠加）
        self._build([r for r in records_all if not self._has_text(r)],
                    self.list_no_text, T("hist.empty_no_text", "暂无无文字记录"))
        self._build([r for r in records_all if self._has_text(r)],
                    self.list_has_text, T("hist.empty_has_text", "暂无有文字记录"))
        self._build([r for r in records_all if self._has_anno(r)],
                    self.list_has_anno, T("hist.empty_has_anno", "暂无有标注记录"))
        # 收藏 Tab（关键词走 search_favorites：文字/标注/模型/时间 与全部 Tab 同源）
        records_fav = self._history.search_favorites(keyword)
        self._build(records_fav, self.list_fav, T("hist.empty_fav", "暂无收藏"))

    # ---- 拖放：拖图片进窗口 → 复制入库 + 后台 OCR ----
    @staticmethod
    def _accept_drag(event) -> bool:
        md = event.mimeData()
        if not md.hasUrls():
            return False
        for u in md.urls():
            if u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() \
                    in _IMAGE_EXTS:
                return True
        return False

    def dragEnterEvent(self, event):
        if self._accept_drag(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._accept_drag(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = []
        for u in event.mimeData().urls():
            if u.isLocalFile():
                p = u.toLocalFile()
                if Path(p).suffix.lower() in _IMAGE_EXTS:
                    paths.append(p)
        # 去重保序
        seen = set()
        uniq = []
        for p in paths:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        if uniq and self._on_files_dropped is not None:
            try:
                self._on_files_dropped(uniq)
            except Exception as e:  # noqa: BLE001 拖放失败不阻断 UI
                log = None
                try:
                    from core_engine.logging_setup import get_logger
                    get_logger("history").exception("拖放图片处理失败: %s", e)
                except Exception:
                    pass
        if uniq:
            event.acceptProposedAction()
        else:
            event.ignore()

    def _build(self, records, lst: QListWidget, empty_text: str):
        lst.clear()
        if not records:
            item = QListWidgetItem()
            lab = QLabel(empty_text)
            lab.setProperty("kind", "empty")
            lab.setAlignment(Qt.AlignCenter)
            item.setSizeHint(QSize(0, 80))
            lst.addItem(item)
            lst.setItemWidget(item, lab)
            return

        last_group = None
        for r in records:
            g = _group_name(r["created_at"])
            if g != last_group:
                self._add_group(g, lst)
                last_group = g
            row = self._make_row(r)
            item = QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            item.setData(Qt.UserRole, r["id"])
            lst.addItem(item)
            lst.setItemWidget(item, row)

    def _add_group(self, name: str, lst: QListWidget):
        """分组头：浅色小标题胶囊条，样式由全局主题提供。"""
        head = QFrame()
        head.setObjectName("grouphead")
        head.setAttribute(Qt.WA_StyledBackground)   # QFrame 需要此属性才绘制 QSS 背景
        hl = QHBoxLayout(head)
        hl.setContentsMargins(10, 3, 10, 3)
        lab = QLabel(name)
        lab.setProperty("kind", "group")
        hl.addWidget(lab)
        hl.addStretch(1)
        item = QListWidgetItem()
        item.setFlags(Qt.ItemIsEnabled)
        item.setSizeHint(QSize(0, head.sizeHint().height() + 4))
        lst.addItem(item)
        lst.setItemWidget(item, head)

    def _make_row(self, r) -> QWidget:
        """单列卡片：缩略图 + 文字信息 + 收藏/删除按钮。"""
        row = QFrame()
        row.setObjectName("row")   # 卡片样式由全局主题提供
        row.setAttribute(Qt.WA_StyledBackground)   # QFrame 需要此属性才绘制 QSS 背景
        lay = QHBoxLayout(row)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)

        thumb = _DragImageLabel(
            str(self._thumb_dir.parent / Path(r["screenshot_path"]).name))
        thumb.setProperty("kind", "thumb")
        thumb.setFixedSize(self._thumb_width, int(self._thumb_width * 0.7))
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setToolTip(T("hist.drag_out_tip",
                           "拖拽图片到桌面 / 微信 / QQ / WorkBuddy 等发送"))
        p = self._thumb_dir / Path(r["screenshot_path"]).name
        pm = QPixmap(str(p)) if p.exists() else QPixmap()
        if not pm.isNull():
            pm = pm.scaled(thumb.size(), Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
            thumb.setPixmap(pm)
        else:
            thumb.setText(T("hist.no_img", "无图"))
        lay.addWidget(thumb)

        mid = QVBoxLayout()
        mid.setSpacing(2)
        text = (r["ocr_text"] or T("hist.no_text", "(无文字)")).replace("\n", " ⏎ ")
        if len(text) > 60:
            text = text[:60] + "…"
        lab = QLabel(text)
        lab.setProperty("kind", "text")
        lab.setWordWrap(True)
        info = QLabel(f"{_fmt_time(r['created_at'])} · {r['model_tier']}"
                      + (" · ★ 收藏" if r["is_favorite"] else ""))
        info.setProperty("kind", "info")
        mid.addWidget(lab)
        mid.addWidget(info)
        lay.addLayout(mid, 1)

        from ui.icon import icon as svg_icon
        from ui.theme import accent_color, icon_color
        fav = QPushButton()
        fav.setProperty("kind", "fav")
        fav.setFixedSize(38, 36)   # 收藏按钮（加大一号）
        fav.setIcon(svg_icon("star-filled" if r["is_favorite"] else "star",
                             color=accent_color(), size=20))
        fav.setToolTip(T("hist.fav_tip", "收藏/取消收藏"))
        fav.clicked.connect(lambda _=False, rid=r["id"],
                            fav_=r["is_favorite"]:
                            self._toggle_fav(rid, not fav_))
        # 收藏不重排：保存按钮/提示引用，切换时只更新本行
        row._fav_btn = fav
        row._info_label = info
        dele = QPushButton()
        dele.setProperty("kind", "del")
        dele.setFixedSize(28, 26)
        dele.setIcon(svg_icon("close", color=icon_color(), size=12))
        dele.setToolTip(T("hist.delete_tip", "删除"))
        dele.clicked.connect(lambda _=False, rid=r["id"]: self._delete(rid))
        lay.addWidget(fav)
        lay.addWidget(dele)
        return row

    # ---- 动作 ----
    def _on_double(self, item):
        rid = item.data(Qt.UserRole)
        if rid is None:
            return
        rec = self._history.get(rid)
        if rec and self._on_restore:
            self._on_restore(rec)

    # ---- 右键菜单 ----
    def _on_context_menu(self, lst: QListWidget, pos: QPoint):
        item = lst.itemAt(pos)
        if item is None:
            return
        rid = item.data(Qt.UserRole)
        if rid is None:
            return
        rec = self._history.get(rid)
        if rec is None:
            return
        fav = bool(rec.get("is_favorite"))
        menu = QMenu(self)
        menu.setMinimumWidth(180)
        # 图标：复制项用 copy 图标，收藏/删除沿用面板已有图（star/close）
        from ui.icon import icon as svg_icon
        from ui.theme import icon_color, accent_color
        ico = icon_color()
        a_copy_orig = QAction(T("hist.copy_orig", "复制原图"), self)
        a_copy_orig.setIcon(svg_icon("copy", color=ico, size=16))
        a_copy_img = QAction(T("hist.copy_img", "复制图片（带标注）"), self)
        a_copy_img.setIcon(svg_icon("copy", color=ico, size=16))
        a_fav = QAction(
            T("hist.unfav" if fav else "hist.fav",
              "取消收藏" if fav else "收藏"), self)
        a_fav.setIcon(svg_icon("star-filled" if fav else "star",
                               color=accent_color(), size=16))
        a_fav.setShortcut("Ctrl+D")
        a_del = QAction(T("hist.delete", "删除"), self)
        a_del.setIcon(svg_icon("close", color=icon_color(), size=16))
        a_del.setShortcut("Del")
        menu.addAction(a_copy_orig)
        menu.addAction(a_copy_img)
        menu.addSeparator()
        menu.addAction(a_fav)
        menu.addAction(a_del)
        a_copy_orig.triggered.connect(
            lambda _=False, r=rid: QTimer.singleShot(0, lambda: self._copy_original(r)))
        a_copy_img.triggered.connect(
            lambda _=False, r=rid: QTimer.singleShot(0, lambda: self._copy_annotated(r)))
        a_fav.triggered.connect(
            lambda _=False, r=rid, f=not fav: QTimer.singleShot(0, lambda: self._toggle_fav(r, f)))
        a_del.triggered.connect(
            lambda _=False, r=rid: QTimer.singleShot(0, lambda: self._delete(r)))
        menu.exec(lst.viewport().mapToGlobal(pos))

    def _orig_path(self, rec) -> Path:
        """历史原图（全分辨率截图）的绝对路径。

        库里 screenshot_path 只存文件名，原图在 screenshots/ 下
        （thumb 目录的父目录），与 _make_row 取图逻辑一致。
        """
        return self._thumb_dir.parent / Path(rec["screenshot_path"]).name

    def _copy_original(self, rid):
        rec = self._history.get(rid)
        if rec is None:
            return
        p = self._orig_path(rec)
        if not p or not p.exists():
            self._toast(T("hist.copy_no_img", "原图文件不存在"))
            return
        pm = QPixmap(str(p))
        if pm.isNull():
            self._toast(T("hist.copy_no_img", "原图文件不存在"))
            return
        QApplication.clipboard().setImage(pm.toImage())
        self._toast(T("hist.copied_orig", "已复制原图到剪贴板"))

    def _copy_annotated(self, rid):
        rec = self._history.get(rid)
        if rec is None:
            return
        # 该记录本身没有标注：明确告知并退化为复制原图（避免“ silently 给了原图”的困惑）
        items = anno_from_json(rec.get("annotations"))
        if not items:
            self._copy_original(rid)
            self._toast(T("hist.copy_no_anno", "该记录没有标注，已复制原图"))
            return
        try:
            pm = self._render_with_annotations(rid)
        except Exception as e:            # noqa: BLE001 任何异常都不应让程序崩溃
            pm = None
            try:
                from core_engine.logging_setup import get_logger
                get_logger("history").exception("标注渲染失败: %s", e)
            except Exception:
                pass
        if pm is None or pm.isNull():
            # 渲染失败时退化为复制原图，并提示
            self._copy_original(rid)
            self._toast(T("hist.copy_fail", "标注渲染失败，已复制原图"))
            return
        QApplication.clipboard().setImage(pm.toImage())
        self._toast(T("hist.copied_img", "已复制带标注的图片到剪贴板"))

    def _render_with_annotations(self, rid):
        """把原图 + 标注渲染成一张 QPixmap（全分辨率，坐标与标注一致）。

        复用 ui.annotation.render_annotated_image —— 与工具栏「完成/复制整图」
        同源，保证两种导出路径渲染结果完全一致（含序号 num 等所有类型）。
        全程异常保护：任何失败都回退返回原图文件（base）。
        """
        try:
            rec = self._history.get(rid)
            if rec is None:
                return None
            p = self._orig_path(rec)
            if not p or not p.exists():
                return None
            base = QPixmap(str(p))
            if base.isNull():
                return None
            return render_annotated_image(base.toImage(),
                                         rec.get("annotations"), opaque=True)
        except Exception as e:            # noqa: BLE001 任何失败都回退原图
            try:
                from core_engine.logging_setup import get_logger
                get_logger("history").exception("标注渲染异常，回退原图: %s", e)
            except Exception:
                pass
            try:
                rec = self._history.get(rid)
                if rec is not None:
                    pp = self._orig_path(rec)
                    if pp and pp.exists():
                        b = QPixmap(str(pp))
                        if not b.isNull():
                            return b
            except Exception:
                pass
            return None

    def _toast(self, msg: str):
        """右键操作后的轻量反馈（跟随光标，1.5s 自动消失）。"""
        try:
            QToolTip.showText(QCursor.pos(), msg)
            QTimer.singleShot(1500, QToolTip.hideText)
        except Exception:                # noqa: BLE001
            pass

    def _update_fav_in_list(self, lst: QListWidget, rid, fav: bool):
        """在单个列表中同步该行的星标图标与收藏提示（不重排）。"""
        for i in range(lst.count()):
            it = lst.item(i)
            if it is not None and it.data(Qt.UserRole) == rid:
                w = lst.itemWidget(it)
                fb = getattr(w, "_fav_btn", None)
                if fb is not None:
                    from ui.icon import icon as svg_icon
                    from ui.theme import accent_color
                    fb.setIcon(svg_icon("star-filled" if fav else "star",
                                        color=accent_color(), size=20))
                info = getattr(w, "_info_label", None)
                if info is not None:
                    info.setText(info.text().replace(" · ★ 收藏", "")
                                 + (" · ★ 收藏" if fav else ""))
                break

    def _toggle_fav(self, rid, fav: bool):
        self._history.set_favorite(rid, fav)
        # 通知外部（App）：同步贴图窗口状态栏星标等
        if self._on_favorite_changed is not None:
            self._on_favorite_changed(rid, fav)
        # 各列表（全部/无文字/有文字/有标注）：**不重新排序**，只更新星号与提示
        for lst in (self.list_all, self.list_no_text,
                    self.list_has_text, self.list_has_anno):
            self._update_fav_in_list(lst, rid, fav)
        # 「收藏」列表：随状态刷新（收藏→加入，取消→移除）；关键词生效时用同源搜索
        records_fav = self._history.search_favorites(self.search.text().strip())
        self._build(records_fav, self.list_fav, T("hist.empty_fav", "暂无收藏"))

    def _delete(self, rid):
        self._history.delete(rid)
        self.reload()

    def _clear_all(self):
        # 清空全部 = 只清非收藏，收藏的图片保留
        self._history.clear(keep_favorites=True)
        self.reload()
