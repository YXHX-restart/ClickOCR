"""FloatMixin —— 浮窗生命周期（创建 / 定位 / 配额裁剪 / 钉图 / 穿透），即 FloatGroup 方案 B 的实体。

从 app.py 拆出（2026-09-11 重构 S3-S5），纯搬移不改行为。
本 mixin 只被 App 混入使用，方法体里的 self.* 由混入后的 App 提供，
本模块不单独实例化、也不 import app。
"""
from __future__ import annotations

from functools import partial


from core_engine.logging_setup import get_logger

from ui.base.widgets import screen_available_geometry

log = get_logger("app")


class FloatMixin:
    # 关窗后全量 GC 的最小间隔（秒）。见 _dispose_overlay 的说明。
    _GC_MIN_INTERVAL = 5.0

    @staticmethod
    def _panel_alive(o):
        """判断 QObject 是否仍存活（未被 C++ 侧销毁）。"""
        try:
            from shiboken6 import isDeleted
            return o is not None and not isDeleted(o)
        except Exception:
            return o is not None

    def _live_overlay(self):
        """当前贴图窗口；若已被销毁则返回 None（并顺手清掉悬空引用）。

        **这是 self._overlay 的唯一读取入口。** 收敛原因：
        窗口关闭走 `deleteLater()`，`self._overlay` 不会自动清空，于是它可能指向
        一个已销毁的 C++ 对象。对这种对象取属性会抛
        `RuntimeError: Internal C++ object already deleted`，而
        `getattr(o, name, default)` **吞不掉**这个异常——它会从槽函数一路抛出去，
        部分 PySide6 版本直接终止进程。

        历史上这个校验是「报出一个入口就补一处」（engine_mixin 补了 3 处、
        history_mixin._reocr 漏了），所以改为统一入口：新代码不要再直接读
        `self._overlay`。
        """
        ov = self._overlay
        if ov is None:
            return None
        if not self._panel_alive(ov):
            self._overlay = None          # 悬空引用就地清理
            return None
        return ov


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
        # 取**面板所在屏**的可用区域，不能用 primaryScreen()：多显示器下按主屏
        # availableGeometry 夹取会把副屏上的窗口强行拉回主屏边界
        # （ui/base/widgets.py 的 screen_available_geometry 文档已点名这个坑）。
        # 面板不存在时退回主屏。
        scr = screen_available_geometry(panel)
        pg = panel.frameGeometry()
        x = pg.left() - ov.width() - 12          # 默认：面板左侧
        if x < scr.left() + 4:                   # 左边放不下 → 放面板右侧（紧邻，不飞角落）
            x = pg.right() + 12
        y = pg.top() + max(0, (pg.height() - ov.height()) // 2)
        x = max(scr.left() + 4, min(x, scr.right() - ov.width()))
        y = max(scr.top() + 4, min(y, scr.bottom() - ov.height()))
        ov.move(x, y)

    # ---- 启动 ----

    def _all_float_windows(self) -> list:
        """截图前需要隐藏的『操作 UI』：状态栏 / 统一工具栏 / 面板。

        图片窗体（贴图 / 钉图 / 历史图）与钉图钉子作为内容**保留**，不隐藏——
        钉图与历史图是独立窗体，截图不应打断它们。
        """
        wins = []
        for ov in self._overlays:      # 钉图已从 _overlays 移出，这里只含普通贴图 + 历史图
            if not self._panel_alive(ov):
                continue               # 防御：跳过可能残留的死引用
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


    @staticmethod
    def _restore_overlay_geometry(ov, intend):
        """把窗口放回**截图时的位置**（压掉 Qt 首次显示时的兜底居中）。

        背景：`QPlatformWindow::initialGeometry()` 在顶层窗口**首次显示**时会判断
        「窗口中心是否落在某块屏上」，不在就**居中到主屏**。而跨屏框选的结果窗口
        中心，恰好落在 Qt 逻辑空间的**屏间缝隙**里（实测主屏逻辑宽 2560、副屏逻辑
        原点 3840，中间 2560~3840 无人认领 → `screenAt` 返回 None），于是被拽到
        主屏正中。

        这条调整发生在 `setGeometry` **之后**（首次 show 时），所以显示之后必须
        原样再摆一次，窗口才会停在截图的位置。

        **这里只还原，绝不做夹取/居中对齐**：用户要的就是「窗口就在我截的那个
        位置」。任何「夹进某块屏」「居中到某块屏」都会把窗口从选区位置搬走，
        反而变成飞来飞去。
        """
        try:
            cur = ov.geometry()
            if (cur.x(), cur.y()) != (intend.x(), intend.y()):
                # 只在 Qt 真的动过窗口时记录，且降为 debug：正常截图不打日志
                log.debug("贴图窗口落位修正：Qt 兜底居中 %s → 截图位置 %s",
                          cur.getRect(), intend.getRect())
                ov.setGeometry(intend)
        except Exception:            # noqa: BLE001  位置修正失败不该阻断贴图
            pass

    def _make_overlay(self, img, raw, boxes=None, persistent=False):
        """创建贴图窗口 + 纯信息状态栏 + 统一工具栏（均有全局开关）。

        数量上限分两类独立管理（见 _trim_overlays）：
        - 截图贴图（persistent=False）→ [software] max_shot_windows，默认 1
        - 历史贴图（persistent=True，从历史恢复的图）→ [software] max_restore_windows，默认 3
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
        ov = ResultOverlay(img, raw, getattr(self, "_capture_dpr", self.dpr),
                           self.config)
        ov._persistent = persistent
        # 信号连接统一走 _bind：同时登记到 ov._bound_signals，_dispose_overlay
        # 遍历该列表注销（原先「连接清单」与「注销清单」是两处手写列表，新增
        # 信号忘记同步就会复现「关窗后内存不释放、RSS 持续上涨」的信号环）
        self._bind(ov, ov.reocr_requested, self._reocr)      # Ctrl+R 重新 OCR
        self._bind(ov, ov.table_requested,                   # Ctrl+Shift+T 开表格
                   self._open_table_window)
        self._bind(ov, ov.table_window_requested,            # 双击/长按开表格
                   self._open_table_window)
        self._bind(ov, ov.annotations_changed,               # 标注持久化
                   partial(self._save_annotations, ov))
        self._bind(ov, ov.fav_requested,                     # 收藏当前截图
                   partial(self._favorite_overlay, ov))
        self._bind(ov, ov.pin_toggled,                       # 钉图切换
                   partial(self._pin_overlay, ov))
        self._bind(ov, ov.ocr_needed,                        # 点文字框/表格补识别
                   partial(self._ocr_on_demand, ov))
        self._bind(ov, ov.done_requested, self._on_overlay_done)   # 完成
        self._bind(ov, ov.copied, partial(self._on_copied, ov))    # 复制文字
        self._bind(ov, ov.closed, partial(self._on_overlay_closed, ov))
        # 纯信息状态栏（[ui] strip_enabled，默认开）
        if self.config.getbool("ui", "strip_enabled", True):
            strip = StatusStrip()
            ov.set_status_strip(strip)
        # 统一工具栏（[ui] toolbar_enabled，默认开）
        if self.config.getbool("ui", "toolbar_enabled", True):
            toolbar = ToolBar()
            ov.set_toolbar(toolbar)
        # ResultOverlay 构造期已按 raw/dpr 摆到截图位置 —— 先记下来
        _intend = ov.geometry()
        ov.show()
        # 显示后必须原样摆回：Qt 的 initialGeometry 会在首次显示时把「中心不在任何
        # 屏上」的顶层窗口居中到主屏（跨屏时中心正落在屏间缝隙里），它发生在
        # setGeometry 之后。这里**只还原，不做夹取/对齐**。
        self._restore_overlay_geometry(ov, _intend)
        if ov._status_strip is not None:
            ov._status_strip.show()
            ov._status_strip.raise_()
        if ov._toolbar is not None:
            ov._toolbar.show()
            ov._toolbar.raise_()
        ov._sync_status_strip_pos()
        ov._sync_toolbar_pos()
        # 关闭按钮是**独立顶层窗体**，且它在 __init__ 里比 setGeometry 更晚创建 ——
        # 定位只挂在 moveEvent/resizeEvent 上时，构造期的 moveEvent 会因「btn_close
        # 还没建」提前返回，之后若窗口不再移动就**永远不定位**，按钮停在默认位置
        # （表现为截图后关闭按钮跑到别的角落）。
        if getattr(ov, "btn_close", None) is not None:
            ov._layout_close_btn()
        if boxes is not None:
            ov.set_boxes(boxes)
        self._overlays.append(ov)
        self._trim_overlays()            # 新窗口计入后按两类配额裁剪（最旧优先）
        self._overlay = ov
        return ov


    def _on_overlay_done(self, ov):
        """「完成」按钮：保存当前截图到历史（操作过的都应留存），并刷新历史面板。"""
        if ov is None or self.history is None:
            return
        # 刷新必须等落库回来：保存已下沉后台线程，直接 reload 会刷在记录入库
        # **之前**——面板上看不到刚点「完成」的那张图（像是丢了）。
        self._save_record(ov, on_saved=lambda _rid: self._reload_history_panel())


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
        # 钉图态直接关窗（✕/Esc）同样要从 _pins 移除：钉图窗口不在 _overlays
        # 里，原先只清 _overlays 会在 _pins 留下死引用，后续 _trim_pins/
        # _toggle_through_top/_sync_favorite 遍历时对已销毁的 C++ 对象调用
        # 方法抛 RuntimeError
        if ov in self._pins:
            self._pins.remove(ov)
        # 悬空引用就地清空：self._overlay 是「最新一个贴图窗口」，关闭后
        # 若不清空，后续 _live_overlay()/Ctrl+R 等路径会读到已销毁对象
        if ov is self._overlay:
            self._overlay = None
        # 「截图无操作也保存」开启时：截图窗口未做任何操作就被关闭 → 仍落库
        # （_save_record 已判 history_id 为空才写，历史恢复的窗口自动跳过）
        if self.config.getbool("software", "save_no_op_close", False):
            self._save_record(ov)
        self._dispose_overlay(ov)


    @staticmethod
    def _bind(ov, sig, slot):
        """连接信号并登记到 ov._bound_signals，供 _dispose_overlay 统一注销。

        用 Signal 对象而非信号名登记：新增连接时无需再维护一份注销名单。
        """
        sig.connect(slot)
        try:
            ov._bound_signals.append(sig)
        except AttributeError:
            ov._bound_signals = [sig]

    def _dispose_overlay(self, ov):
        """断开窗口的全部信号连接、销毁窗口、回收内存（幂等，可安全多次调用）。

        GC 那段是**限频**的：截图 ndarray 动辄几十 MB，关窗后等 Python 按阈值
        自然回收会多占一会儿内存，所以这里主动收一次；但全量 gen-2 回收要遍历
        整个堆，**每次关窗都跑一遍**会让「连开关几个贴图窗口」出现周期性卡顿
        （配额裁剪还会批量走这里）。折中：距上次不足 _GC_MIN_INTERVAL 就跳过 ——
        上面已经把自引用环断开，引用计数会即时释放，GC 只是补网。
        """
        # 断开 _bind 登记过的全部信号：消除 partial(.,ov) ↔ ov 持有信号 的
        # 自引用环，否则 Python 引用计数无法归零、C++ 对象不析构、
        # 图像/场景内存不释放。
        for sig in list(getattr(ov, "_bound_signals", [])):
            try:
                sig.disconnect()
            except Exception:  # noqa: BLE001
                pass
        try:
            ov._bound_signals = []
        except Exception:  # noqa: BLE001
            pass
        try:
            ov.deleteLater()
        except Exception:  # noqa: BLE001
            pass
        import gc
        import time as _time
        now = _time.monotonic()
        if now - getattr(self, "_last_gc_at", 0.0) >= self._GC_MIN_INTERVAL:
            self._last_gc_at = now
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
        - 截图贴图（非持久）：[software] max_shot_windows，默认 1
        - 历史贴图（持久，从历史恢复的图）：[software] max_restore_windows，默认 3
        钉图在 _pins 独立管理，不占贴图配额（取消钉图回列表时按配额裁剪）。
        """
        self._trim_group(
            False, max(1, self.config.getint(
                "software", "max_shot_windows", 1)))
        self._trim_group(
            True, max(1, self.config.getint(
                "software", "max_restore_windows", 3)))


    def _trim_group(self, persistent: bool, limit: int):
        """裁剪 _overlays 中 persistent 状态 == persistent 的窗口到 limit 个。"""
        while sum(1 for o in self._overlays
                  if self._panel_alive(o)
                  and bool(getattr(o, "_persistent", False)) == persistent) > limit:
            victim = None
            for old in self._overlays:      # 列表头部 = 最旧
                if self._panel_alive(old) \
                        and bool(getattr(old, "_persistent", False)) == persistent:
                    victim = old
                    break
            if victim is None:
                break
            self._overlays.remove(victim)
            # close() → ResultOverlay.closeEvent 逐个清理状态栏/工具栏等子窗口
            # → closed 信号 → _on_overlay_closed 完整清理（断信号 + deleteLater）。
            # 此处不再手动 close 子窗口：与上述流程重复，且子窗口可能已被销毁，
            # 对残留的 Python 引用调 close() 会抛 RuntimeError。
            victim.close()


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
        # 统一验活过滤：列表中可能残留已销毁引用（防御），isVisible 前必须
        # 先过 _panel_alive（对死对象调 isVisible 会抛 RuntimeError）
        wins = [w for w in list(self._overlays) + list(self._pins)
                if self._panel_alive(w) and w.isVisible()]
        if not wins:
            return
        wins[-1].toggle_through()        # 列表尾部 = 最近打开的，视为最上层

