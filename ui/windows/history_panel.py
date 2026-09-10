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
                               QWidget, QTabWidget, QApplication, QMenu,
                               QGraphicsScene, QGraphicsPixmapItem, QToolTip)

from ui.base.card_window import CardWindow
from ui.base.texts import T
from ui.components.annotation import anno_from_json, render_annotated_image


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


# 拖放仅接受的图片扩展名。
# 前半段由 cv2.imdecode 直接解码；后半段（avif/heic/heif/hif）走 Pillow 回退：
#   - AVIF：Pillow >= 11.3 内置（本项目 Pillow 12.3 已确认可用），零额外依赖
#   - HEIC/HEIF：需 pillow_heif 注册 opener（自带 libheif）；未安装时解码失败会跳过
# 扩展名放开不代表一定能解——解码失败统一在 app 侧跳过并记日志。
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp",
               ".gif", ".tif", ".tiff",
               ".avif", ".heic", ".heif", ".hif"}


class _ResizeHandle(QWidget):
    """八方向缩放手柄之一：无边框窗体在指定边/角拖动改变尺寸。

    每个手柄是独立置顶子控件（raise_），必然收到自己的鼠标事件，
    不依赖父窗体事件过滤（CardWindow 子控件布满整窗，父过滤器收不到子事件）。
    边缘用位掩码组合：L=左 R=右 T=上 B=下（角=两边 OR）。
    """

    L, R, T, B = 1, 2, 4, 8
    MARGIN = 6          # 边热区宽度
    CORNER = 14         # 角热区边长

    def __init__(self, parent, edges):
        super().__init__(parent)
        self._edges = edges
        self._start = None
        self._start_geo = None
        self._set_cursor()
        self.setToolTip("拖动调整窗体大小")

    def _set_cursor(self):
        e = self._edges
        if e in (self.L, self.R):
            self.setCursor(Qt.SizeHorCursor)
        elif e in (self.T, self.B):
            self.setCursor(Qt.SizeVerCursor)
        elif e in (self.L | self.T, self.R | self.B):
            self.setCursor(Qt.SizeFDiagCursor)
        elif e in (self.R | self.T, self.L | self.B):
            self.setCursor(Qt.SizeBDiagCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._start = e.globalPosition().toPoint()
            self._start_geo = self.window().geometry()

    def mouseMoveEvent(self, e):
        if self._start is None or not (e.buttons() & Qt.LeftButton):
            return
        w = self.window()
        delta = e.globalPosition().toPoint() - self._start
        g = self._start_geo
        x0, y0, w0, h0 = g.x(), g.y(), g.width(), g.height()
        min_w, min_h = w.minimumWidth(), w.minimumHeight()
        x, y, nw, nh = x0, y0, w0, h0
        if self._edges & self.L:
            nw = max(min_w, w0 - delta.x()); x = x0 + (w0 - nw)
        elif self._edges & self.R:
            nw = max(min_w, w0 + delta.x())
        if self._edges & self.T:
            nh = max(min_h, h0 - delta.y()); y = y0 + (h0 - nh)
        elif self._edges & self.B:
            nh = max(min_h, h0 + delta.y())
        # clamp 屏幕可用区（不拖出屏）
        scr = QApplication.primaryScreen().availableGeometry()
        nw = min(nw, scr.right() - x + 1)
        nh = min(nh, scr.bottom() - y + 1)
        w.setGeometry(x, y, nw, nh)

    def mouseReleaseEvent(self, _e):
        self._start = None


class _DragImageLabel(QLabel):
    """缩略图标签：左键拖拽把**原图**作为文件拖出（桌面 / 微信 / QQ / WorkBuddy 等）。

    拖出的是完整分辨率截图文件（非缩略图），通过 file:// URL + 图片数据，
    桌面（复制文件）/ 聊天软件输入窗（发送图片）均可接收。
    多选时：若本项在选区内且选区 >1，拖出**全部选中项**的原图（多文件拖放）；
    否则仅拖出本张。
    """

    def __init__(self, img_path: str, rid=None, panel=None, lst=None, parent=None):
        super().__init__(parent)
        self._img_path = img_path
        self._rid = rid
        self._panel = panel
        self._lst = lst
        self._start = None
        # 鼠标按下瞬间快照的选区：press 阶段 Qt 尚未执行「点击已选 item →
        # 收敛选区」（收敛发生在 release），拖拽用快照带出整批。
        self._press_selection = []
        # 本次 press→release 序列是否发生过拖拽：发生过 → release 吞掉（不冒泡），
        # 防止列表把多选区收敛成单张；纯点击（无拖拽）→ release 正常冒泡收敛。
        self._drag_started = False

    def _drag_paths(self):
        """本次拖拽要带出的原图绝对路径列表（1 张或多张）。

        优先用「鼠标按下瞬间」的选区快照：拖拽启动时列表尚未执行「点击已选
        item → 收敛选区」（收敛发生在 release），快照保住多选批次，一次拖出
        带出全部选中项；纯点击（无拖拽）则由 Qt 在 release 正常收敛成单张。
        """
        panel = self._panel
        # 1) 按下瞬间的快照选区（仍含本项且 >1 张 → 整批带出）
        snap = getattr(self, "_press_selection", None)
        if (snap and self._rid is not None
                and self._rid in snap and len(snap) > 1):
            rids = snap
        # 2) 否则用当前实时选区（单张 / 无快照兜底）
        elif (panel is not None and self._lst is not None
              and self._rid is not None):
            sel = panel._selected_rids(self._lst)
            rids = sel if (self._rid in sel and len(sel) > 1) else [self._rid]
        else:
            rids = [self._rid] if self._rid is not None else []
        paths = []
        for r in rids:
            rec = panel._history.get(r) if panel is not None else None
            if rec is None:
                continue
            p = panel._orig_path(rec) if panel is not None else None
            if p and Path(p).exists():
                paths.append(str(p))
        return paths

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._start = e.pos()
            self._drag_started = False
            # 按下瞬间先快照当前选区（press 阶段 Qt 尚未收敛，选区仍完整），
            # 拖拽时用快照带出整批；不拦截 press：纯点击由 Qt 在 release 收敛。
            if self._panel is not None and self._lst is not None:
                self._press_selection = self._panel._selected_rids(self._lst)
            else:
                self._press_selection = [self._rid] if self._rid is not None else []
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        # 本次序列发生过拖拽 → release 吞掉（不冒泡到列表）：否则列表会在
        # release 执行「点击已选 item → 收敛选区」，把多选拖成单张，
        # 导致第二次拖拽只剩一张。
        if getattr(self, "_drag_started", False):
            self._drag_started = False
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseMoveEvent(self, e):
        if self._start is None or not (e.buttons() & Qt.LeftButton):
            return super().mouseMoveEvent(e)
        if (e.pos() - self._start).manhattanLength() < \
                QApplication.startDragDistance():
            return super().mouseMoveEvent(e)
        self._start = None
        # 本次序列发生拖拽 → release 需吞掉，防止列表把多选区收敛成单张
        self._drag_started = True
        paths = self._drag_paths()
        if not paths:
            return
        mime = QMimeData()
        # 主格式：本地文件 URL 列表（桌面 / 微信 / 网页 WorkBuddy 按文件接收；
        # 多选时一次带出多张）
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        pm = self.pixmap()
        preview = pm if (pm is not None and not pm.isNull()) else None
        # 图片数据：用首张（全分辨率）作为位图载荷（粘贴类目标单图）
        src = QPixmap(paths[0])
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
        # 拖拽结束：把选区恢复成按下瞬间的多选区。
        # QDrag 期间/之后列表可能把选区收敛成单张（「点击已选 item → release 收敛」），
        # 直接按快照重选，保证第二次拖拽仍是整批。
        self._restore_selection_after_drag()

    def _restore_selection_after_drag(self):
        """拖拽结束后恢复按下时的多选区（若已被收敛成单张）。"""
        snap = getattr(self, "_press_selection", None)
        if (not snap or len(snap) <= 1 or self._rid is None
                or self._rid not in snap):
            return
        lst = self._lst
        panel = self._panel
        if lst is None or panel is None:
            return
        try:
            cur = panel._selected_rids(lst)
        except Exception:  # noqa: BLE001
            return
        if sorted(cur) == sorted(snap):
            return  # 选区未被收敛，无需恢复
        lst.clearSelection()
        for i in range(lst.count()):
            it = lst.item(i)
            rid = it.data(Qt.UserRole)
            if rid is not None and rid in snap:
                it.setSelected(True)


class HistoryPanel(CardWindow):
    def __init__(self, history, on_restore, screenshots_dir=None,
                 thumb_width: int = 90, width: int = 620, cards: int = 5,
                 on_favorite_changed=None, on_files_dropped=None, config=None):
        super().__init__(title=T("hist.title", "历史记录"), with_close=True)
        self._cards = max(1, min(30, cards))
        self._cfg = config
        # 尺寸：优先记忆（config），否则按卡片数回退
        #   width：config 有 history_panel_width 则覆盖传入值
        #   height：config 有 history_panel_height(且>0) 用之；否则按卡片数算
        if config is not None:
            width = config.getint("ui", "history_panel_width", width)
        h_cfg = config.getint("ui", "history_panel_height", None) if config else None
        h = h_cfg if h_cfg else min(1080, max(300, 150 + self._cards * 84))
        self.resize(width, h)

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
        top.addWidget(self.search, 1)
        top.addWidget(b_search)
        self.card_lay.addLayout(top)

        # ---- 列表（全部 / 无文字 / 有文字 / 条码 / 有标注 / 收藏 六个 Tab，单列卡片）----
        self.tabs = QTabWidget()
        self.list_all = QListWidget()
        self.list_all.itemDoubleClicked.connect(self._on_double)
        self.list_no_text = QListWidget()          # 无文字
        self.list_no_text.itemDoubleClicked.connect(self._on_double)
        self.list_has_text = QListWidget()         # 有文字
        self.list_has_text.itemDoubleClicked.connect(self._on_double)
        self.list_has_qr = QListWidget()           # 条码（ocr_boxes 含 kind=="qr"）
        self.list_has_qr.itemDoubleClicked.connect(self._on_double)
        self.list_has_anno = QListWidget()         # 有标注
        self.list_has_anno.itemDoubleClicked.connect(self._on_double)
        self.list_fav = QListWidget()
        self.list_fav.itemDoubleClicked.connect(self._on_double)
        self.tabs.addTab(self.list_all, T("hist.tab_all", "全部"))
        self.tabs.addTab(self.list_no_text, T("hist.tab_no_text", "无文字"))
        self.tabs.addTab(self.list_has_text, T("hist.tab_has_text", "有文字"))
        self.tabs.addTab(self.list_has_qr, T("hist.tab_has_qr", "条码"))
        self.tabs.addTab(self.list_has_anno, T("hist.tab_has_anno", "有标注"))
        self.tabs.addTab(self.list_fav, T("hist.tab_fav", "收藏"))
        self.card_lay.addWidget(self.tabs, 1)

        # 右键菜单 + 多选（Ctrl 跨选 / Shift 连选，原生 ExtendedSelection）
        for _lst in (self.list_all, self.list_no_text, self.list_has_text,
                     self.list_has_qr, self.list_has_anno, self.list_fav):
            _lst.setContextMenuPolicy(Qt.CustomContextMenu)
            _lst.customContextMenuRequested.connect(
                lambda pos, l=_lst: self._on_context_menu(l, pos))
            _lst.setSelectionMode(QListWidget.ExtendedSelection)
            _lst.itemSelectionChanged.connect(
                lambda l=_lst: self._sync_selection_style(l))

        # 公告：提示可将图片拖出到桌面 / 微信 / QQ / WorkBuddy 等
        hint = QLabel(T("hist.drag_hint",
                        "提示：拖动缩略图即可把图片发送到桌面 / 微信 / QQ / WorkBuddy 等"))
        hint.setProperty("kind", "info")
        hint.setWordWrap(True)
        self.card_lay.addWidget(hint)

        # ---- 最小尺寸：确保 6 个 Tab 全可见 + 至少两卡片不截断 ----
        # 最小宽：tabBar 实际宽度 + card_lay 左右边距(16*2)；保底 460
        min_w = self.tabs.tabBar().sizeHint().width() + 32
        self.setMinimumWidth(max(460, min_w))
        # 最小高：标题栏~28 + 搜索行~40 + 提示~32 + 2*卡片高(84) + 分组头(28) + 边距(26)
        self.setMinimumHeight(28 + 40 + 32 + 2 * 84 + 28 + 26)   # ≈ 326

        # ---- 八方向缩放手柄（类原生：上/下/左/右 四条边 + 四个角）----
        H = _ResizeHandle
        self._handles = [
            H(self, H.L), H(self, H.R), H(self, H.T), H(self, H.B),
            H(self, H.L | H.T), H(self, H.R | H.T),
            H(self, H.L | H.B), H(self, H.R | H.B),
        ]
        for _h in self._handles:
            _h.raise_()
        self._layout_handles()

        self.reload()

    # ---- 窗口行为：八方向手柄重定位 + 关闭记忆尺寸 ----
    def _layout_handles(self):
        m, c = _ResizeHandle.MARGIN, _ResizeHandle.CORNER
        w, h = self.width(), self.height()
        H = _ResizeHandle
        # 四条边（避开角，避免方向歧义）
        self._handles[0].setGeometry(0, c, m, h - 2 * c)          # 左
        self._handles[1].setGeometry(w - m, c, m, h - 2 * c)      # 右
        self._handles[2].setGeometry(c, 0, w - 2 * c, m)          # 上
        self._handles[3].setGeometry(c, h - m, w - 2 * c, m)      # 下
        # 四个角（盖在边之上，置顶）
        self._handles[4].setGeometry(0, 0, c, c)                  # 左上
        self._handles[5].setGeometry(w - c, 0, c, c)              # 右上
        self._handles[6].setGeometry(0, h - c, c, c)              # 左下
        self._handles[7].setGeometry(w - c, h - c, c, c)          # 右下
        for _h in self._handles:
            _h.raise_()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if getattr(self, "_handles", None):
            self._layout_handles()

    def closeEvent(self, e):
        # 记忆最终尺寸到 config.ini（仅隐藏也会触发 closeEvent）
        if self._cfg is not None:
            self._cfg.cp.set("ui", "history_panel_width", str(self.width()))
            self._cfg.cp.set("ui", "history_panel_height", str(self.height()))
            self._cfg.save()
        super().closeEvent(e)

    # ---- 数据 ----
    @staticmethod
    def _has_text(r) -> bool:
        return bool((r.get("ocr_text") or "").strip())

    @staticmethod
    def _has_anno(r) -> bool:
        ann = (r.get("annotations") or "").strip()
        return ann not in ("", "[]", "{}", "null", "None")

    @staticmethod
    def _has_qr(r) -> bool:
        """是否含二维码/条码：ocr_boxes 中任一元素 kind=="qr"（M2+++ 双轨精确通道）。"""
        boxes = r.get("ocr_boxes")
        if not boxes:
            return False
        try:
            import json
            data = json.loads(boxes)
        except Exception:  # noqa: BLE001 旧记录格式异常按无码处理
            return False
        return any(isinstance(b, dict) and b.get("kind") == "qr" for b in data)

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
        self._build([r for r in records_all if self._has_qr(r)],
                    self.list_has_qr, T("hist.empty_has_qr", "暂无条码记录"))
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
            row = self._make_row(r, lst)
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

    def _make_row(self, r, lst) -> QWidget:
        """单列卡片：缩略图 + 文字信息 + 收藏/删除按钮。"""
        row = QFrame()
        row.setObjectName("row")   # 卡片样式由全局主题提供
        row.setAttribute(Qt.WA_StyledBackground)   # QFrame 需要此属性才绘制 QSS 背景
        lay = QHBoxLayout(row)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)

        thumb = _DragImageLabel(
            str(self._thumb_dir.parent / Path(r["screenshot_path"]).name),
            rid=r["id"], panel=self, lst=lst)
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
        raw_text = r.get("ocr_text")
        raw_boxes = r.get("ocr_boxes")
        if (raw_text or "").strip():
            text = raw_text
        elif raw_boxes is None or not str(raw_boxes).strip():
            # ocr_boxes IS NULL = 入库后从未识别（如关闭「拖入图片后自动 OCR」导入的图）
            text = T("hist.not_ocr", "(未识别)")
        else:
            # ocr_boxes='[]' = 已识别、图中确实没有文字
            text = T("hist.no_text", "(无文字)")
        text = text.replace("\n", " ⏎ ")
        if len(text) > 60:
            text = text[:60] + "…"
        lab = QLabel(text)
        lab.setProperty("kind", "text")
        lab.setWordWrap(True)
        # 档位是「识别执行记录」：未识别的记录没有值，不显示 " · " 空尾巴
        _tier = (r.get("model_tier") or "").strip()
        info = QLabel(_fmt_time(r["created_at"])
                      + (f" · {_tier}" if _tier else "")
                      + (" · ★ 收藏" if r["is_favorite"] else ""))
        info.setProperty("kind", "info")
        mid.addWidget(lab)
        mid.addWidget(info)
        lay.addLayout(mid, 1)

        from ui.base.icon import icon as svg_icon
        from ui.base.theme import accent_color, icon_color
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

    # ---- 多选工具 ----
    @staticmethod
    def _selected_rids(lst: QListWidget):
        """当前选中的 rid 列表（不含不可选分组头；双保险过滤 None）。"""
        out = []
        for it in lst.selectedItems():
            rid = it.data(Qt.UserRole)
            if rid is not None:
                out.append(rid)
        return out

    def _sync_selection_style(self, lst: QListWidget):
        """选中变化后，按 item.isSelected() 同步行 widget 的 selected 属性，
        驱动 #row[selected="true"] 高亮（暗/亮主题均已加规则）。"""
        for i in range(lst.count()):
            it = lst.item(i)
            w = lst.itemWidget(it)
            if w is not None and hasattr(w, "setProperty"):
                w.setProperty("selected", bool(it is not None and it.isSelected()))
                w.style().unpolish(w)
                w.style().polish(w)

    # ---- 右键菜单：单条 / 批量 派发 ----
    def _on_context_menu(self, lst: QListWidget, pos: QPoint):
        rids = self._selected_rids(lst)
        if not rids:
            # 无选区：回退到点中的单条
            item = lst.itemAt(pos)
            if item is None:
                return
            rid = item.data(Qt.UserRole)
            if rid is None:
                return
            rids = [rid]
        if len(rids) > 1:
            self._bulk_menu(lst, pos, rids)
        else:
            self._single_menu(lst, pos, rids[0])

    def _single_menu(self, lst: QListWidget, pos: str, rid):
        rec = self._history.get(rid)
        if rec is None:
            return
        fav = bool(rec.get("is_favorite"))
        menu = QMenu(self)
        menu.setMinimumWidth(180)
        # 图标：复制项用 copy 图标，收藏/删除沿用面板已有图（star/close）
        from ui.base.icon import icon as svg_icon
        from ui.base.theme import icon_color, accent_color
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

    def _bulk_menu(self, lst: QListWidget, pos: QPoint, rids):
        n = len(rids)
        menu = QMenu(self)
        menu.setMinimumWidth(200)
        from ui.base.icon import icon as svg_icon
        from ui.base.theme import icon_color, accent_color
        ico = icon_color()
        a_del = QAction(T("hist.bulk_delete", "批量删除(%d)") % n, self)
        a_del.setIcon(svg_icon("close", color=ico, size=16))
        a_del.setShortcut("Del")
        a_fav = QAction(T("hist.bulk_fav", "批量收藏/取消收藏(%d)") % n, self)
        a_fav.setIcon(svg_icon("star-filled", color=accent_color(), size=16))
        a_fav.setShortcut("Ctrl+D")
        a_copy_orig = QAction(T("hist.bulk_copy_orig", "批量复制原图(%d)") % n, self)
        a_copy_orig.setIcon(svg_icon("copy", color=ico, size=16))
        a_copy_anno = QAction(T("hist.bulk_copy_anno", "批量复制带标注(%d)") % n, self)
        a_copy_anno.setIcon(svg_icon("copy", color=ico, size=16))
        menu.addAction(a_del)
        menu.addAction(a_fav)
        menu.addSeparator()
        menu.addAction(a_copy_orig)
        menu.addAction(a_copy_anno)
        a_del.triggered.connect(
            lambda _=False, rs=rids: QTimer.singleShot(0, lambda: self._bulk_delete(rs)))
        a_fav.triggered.connect(
            lambda _=False, rs=rids: QTimer.singleShot(0, lambda: self._bulk_fav_toggle(rs)))
        a_copy_orig.triggered.connect(
            lambda _=False, rs=rids: QTimer.singleShot(0, lambda: self._bulk_copy_orig(rs)))
        a_copy_anno.triggered.connect(
            lambda _=False, rs=rids: QTimer.singleShot(0, lambda: self._bulk_copy_anno(rs)))
        menu.exec(lst.viewport().mapToGlobal(pos))

    def _orig_path(self, rec) -> Path:
        """历史原图（全分辨率截图）的绝对路径。

        库里 screenshot_path 只存文件名，原图在 screenshots/ 下
        （thumb 目录的父目录），与 _make_row 取图逻辑一致。
        """
        return self._thumb_dir.parent / Path(rec["screenshot_path"]).name

    def _copy_original(self, rid) -> bool:
        rec = self._history.get(rid)
        if rec is None:
            return False
        p = self._orig_path(rec)
        if not p or not p.exists():
            self._toast(T("hist.copy_no_img", "原图文件不存在"))
            return False
        pm = QPixmap(str(p))
        if pm.isNull():
            self._toast(T("hist.copy_no_img", "原图文件不存在"))
            return False
        QApplication.clipboard().setImage(pm.toImage())
        self._toast(T("hist.copied_orig", "已复制原图到剪贴板"))
        return True

    def _copy_annotated(self, rid) -> bool:
        rec = self._history.get(rid)
        if rec is None:
            return False
        # 该记录本身没有标注：明确告知并退化为复制原图（避免“ silently 给了原图”的困惑）
        items = anno_from_json(rec.get("annotations"))
        if not items:
            self._copy_original(rid)
            self._toast(T("hist.copy_no_anno", "该记录没有标注，已复制原图"))
            return True
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
            return True
        QApplication.clipboard().setImage(pm.toImage())
        self._toast(T("hist.copied_img", "已复制带标注的图片到剪贴板"))
        return True

    def _render_with_annotations(self, rid):
        """把原图 + 标注渲染成一张 QPixmap（全分辨率，坐标与标注一致）。

        复用 ui.components.annotation.render_annotated_image —— 与工具栏「完成/复制整图」
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
                    from ui.base.icon import icon as svg_icon
                    from ui.base.theme import accent_color
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
        # 各列表（全部/无文字/有文字/条码/有标注）：**不重新排序**，只更新星号与提示
        for lst in (self.list_all, self.list_no_text,
                    self.list_has_text, self.list_has_qr, self.list_has_anno):
            self._update_fav_in_list(lst, rid, fav)
        # 「收藏」列表：随状态刷新（收藏→加入，取消→移除）；关键词生效时用同源搜索
        records_fav = self._history.search_favorites(self.search.text().strip())
        self._build(records_fav, self.list_fav, T("hist.empty_fav", "暂无收藏"))

    def _delete(self, rid):
        self._history.delete(rid)
        self.reload()

    # ---- 批量操作（选区 ≥ 2 时由 _bulk_menu / 快捷键调用）----
    def _bulk_delete(self, rids):
        for rid in rids:
            self._history.delete(rid)
        self.reload()                       # 重建列表，选区自然清空
        self._toast(T("hist.bulk_deleted", "已删除 %d 项") % len(rids))

    def _bulk_fav_toggle(self, rids):
        recs = [self._history.get(r) for r in rids]
        all_fav = all(bool(x.get("is_favorite")) for x in recs if x)
        target = not all_fav               # 有未收藏则统一收藏，否则统一取消
        for rid in rids:
            self._history.set_favorite(rid, target)
            if self._on_favorite_changed is not None:
                self._on_favorite_changed(rid, target)
        self.reload()
        self._toast(T("hist.bulk_faved", "已%s %d 项")
                    % ("收藏" if target else "取消收藏", len(rids)))

    def _bulk_copy_orig(self, rids):
        ok = self._copy_original(rids[0])
        if ok and len(rids) > 1:
            self._toast(T("hist.bulk_copy_hint",
                           "已复制原图（共 %d 张，仅首张入剪贴板）") % len(rids))

    def _bulk_copy_anno(self, rids):
        ok = self._copy_annotated(rids[0])
        if ok and len(rids) > 1:
            self._toast(T("hist.bulk_copy_anno_hint",
                           "已复制带标注图片（共 %d 张，仅首张入剪贴板）") % len(rids))

    def keyPressEvent(self, e):
        # 焦点在搜索框时不拦截（Del 用于删字、Ctrl+D 无意义）
        if isinstance(self.focusWidget(), QLineEdit):
            super().keyPressEvent(e)
            return
        if e.key() == Qt.Key_Delete and not e.modifiers():
            lst = self.tabs.currentWidget()
            if isinstance(lst, QListWidget):
                rids = self._selected_rids(lst)
                if rids:
                    self._bulk_delete(rids)
            return
        if e.key() == Qt.Key_D and e.modifiers() & Qt.ControlModifier:
            lst = self.tabs.currentWidget()
            if isinstance(lst, QListWidget):
                rids = self._selected_rids(lst)
                if rids:
                    self._bulk_fav_toggle(rids)
            return
        super().keyPressEvent(e)
