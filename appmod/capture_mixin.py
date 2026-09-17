"""CaptureMixin —— 截图链路（框选 / 全屏 / 滚动截图）。

从 app.py 拆出（2026-09-11 重构 S3-S5），纯搬移不改行为。
本 mixin 只被 App 混入使用，方法体里的 self.* 由混入后的 App 提供，
本模块不单独实例化、也不 import app。
"""
from __future__ import annotations

from functools import partial

from PySide6.QtCore import QRect, QTimer
from PySide6.QtWidgets import QSystemTrayIcon

from core_engine.logging_setup import get_logger

from platform_compat import scroll_capture_supported
from ui.base.texts import T
from ui.windows.selection import SelectionOverlay

log = get_logger("app")


class CaptureMixin:
    # 抓屏前「等一帧」的延迟（ms）：Qt 的 hide()/close() 只是把窗口标记为待处理，
    # 真正从屏幕消失要等合成器处理完这次重绘；立刻 grab 会截到残影
    # （全屏截图把自己的面板拍进去、框选把选区遮罩拍进去）。
    GRAB_DELAY_MS = 60

    # ---- 坐标约定（raw_bbox 的唯一说明处）----
    # raw_bbox 一律是 **mss 原始（物理）像素** (left, top, right, bottom)：
    # - ScreenshotService.grab_region 直接把它交给 mss（mss 要的就是物理像素）；
    # - ResultOverlay 用它定位窗口时再除以 dpr 换回逻辑坐标。
    # 由此推出两条唯一写法：
    # 1) 框选给的 raw 由 SelectionOverlay._to_raw() 换算好（它才知道本屏 dpr
    #    与**本屏物理原点**），这里只做形状转换（_raw_bbox_of），**不得再乘 dpr**；
    # 2) **整幅图**的 raw 就是图像自身的像素尺寸 (0, 0, w, h)（_full_img_raw_bbox）
    #    ——grab_full() 返回的已是物理像素，重乘一次会让 dpr≠1 时「重新抓屏」
    #    的区域越界（截到图外/错内容）；历史恢复同样按 (0, 0, w, h) 取
    #    （见 history_mixin.restore_record）。
    # dpr==1 时两式数值相同，所以乘错只会在 125%/150% 缩放（dpr=1.25/1.5）
    # 下暴露：表现为重识别抓到的区域偏移/越界。

    @staticmethod
    def _raw_bbox_of(rect: QRect) -> tuple:
        """SelectionOverlay 发来的 raw QRect → (left, top, right, bottom) 元组。

        rect 已是虚拟桌面物理像素，这里只做形状转换；width/height 是右/下边界的
        **排他**值，故 right = left + width。
        """
        return (rect.left(), rect.top(),
                rect.left() + rect.width(), rect.top() + rect.height())

    @staticmethod
    def _full_img_raw_bbox(img) -> tuple:
        """整幅图的 raw_bbox = 图像自身像素尺寸（物理像素，见上方坐标约定）。"""
        h, w = img.shape[:2]
        return (0, 0, int(w), int(h))

    def _hide_ui_windows(self):
        """隐藏截图不该入镜的操作 UI（状态栏/标注栏/设置与表格面板）。

        图片窗体（贴图/钉图/历史图）与钉子保留，不打断用户；被隐藏的窗口记入
        _hidden_windows，由 _restore_hidden() 配对恢复。
        收敛点：「截图前隐藏浮窗」原先在 start_capture 与 capture_fullscreen
        各写一遍同样的遍历+记录+hide，本方法是唯一实现。
        """
        self._hidden_windows = []
        for w in self._all_float_windows():
            if w is not None and w.isVisible():
                self._hidden_windows.append(w)
                w.hide()

    def _grab_delayed(self, fn, *args):
        """延迟 GRAB_DELAY_MS 后抓屏 —— 所有抓屏入口的唯一排程点。

        收敛点：「隐藏 UI/遮罩 → 等一帧 → 抓屏」原先散在各处——框选与滚动框选
        在 on_region/on_scroll_region 里各写一次 QTimer.singleShot(60, ...)，
        而全屏截图（capture_fullscreen）**没有延迟**：hide() 之后立即
        grab_full()，窗口动画/合成器延迟下会把残影（自己的面板）截进去。
        现统一走本方法，延迟常量与排程方式只此一份。
        """
        QTimer.singleShot(self.GRAB_DELAY_MS, partial(fn, *args))

    def start_capture(self):
        # 已有框选遮罩在显示则忽略重复触发，避免叠加多个全屏遮罩
        if self._sel is not None and self._sel.isVisible():
            return
        # 截图前隐藏『操作 UI』（状态栏/标注栏/面板），避免入镜
        self._hide_ui_windows()
        sel = SelectionOverlay(self.config)
        self._sel = sel
        # 记下**遮罩所在那块屏**的 dpr：raw_bbox 是物理像素，下游把图换算回
        # 逻辑尺寸（ResultOverlay / ModeMask）时必须用同一块屏的 dpr，
        # 否则副屏（尤其混合 DPI）上窗口尺寸会算错。
        self._capture_dpr = float(getattr(sel, "_dpr", self.dpr) or self.dpr)
        sel.region_selected.connect(self.on_region)
        sel.cancelled.connect(self._on_capture_cancelled)
        sel.scroll_requested.connect(self.on_scroll_region)
        sel.show()

    def capture_fullscreen(self):
        """全屏截图热键回调：不框选，直接抓取主屏全屏并识别。"""
        self._hide_ui_windows()
        # 等一帧再抓：否则 hide() 后的浮窗残影会入镜（原先此处没有延迟）
        self._grab_delayed(self._fullscreen_grab)

    def _fullscreen_grab(self):
        """全屏抓屏体（capture_fullscreen 延迟一帧后执行）。"""
        try:
            img = self.svc.grab_full()
        except Exception as e:  # noqa: BLE001
            self._restore_hidden()
            self.tray.showMessage(
                "ClickOCR",
                T("app.fullscreen_fail", "全屏截图失败：") + str(e),
                QSystemTrayIcon.Warning, 3000)
            return
        self._restore_hidden()
        ov = self._make_overlay(img, self._full_img_raw_bbox(img))
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
        # rect 已是 raw 物理像素（SelectionOverlay._to_raw 换算），此处只转形状
        # 等选择遮罩彻底隐藏再抓屏，避免把它拍进去
        self._grab_delayed(self._grab_and_ocr, self._raw_bbox_of(rect))

    def on_scroll_region(self, rect: QRect):
        """滚动截图：框选完成 → 等遮罩隐藏后启动滚屏拼接。"""
        self._grab_delayed(self._scroll_capture, self._raw_bbox_of(rect))

    def _teardown_scroll_capture(self):
        """滚动截图收尾的唯一入口（实现见 screenshot.scroll_capture.teardown_scroll）。

        把「置 stop → 停 pynput 监听 → 等线程结束（带超时）→ 清引用」收敛到
        一个函数，进度牌「完成/退出」、picker 取消、done/failed、下一轮开始
        这五处都走它（原先各写一份，Esc 后监听线程不退出、残留 QThread 被覆盖
        引用后 GC 崩溃）。

        同时在这里清掉重入标志：收尾是**唯一**把滚动截图从「进行中」变为「已结束」
        的地方（正常完成 / 失败 / Esc 取消 / 下一轮开始前 都经过它），
        标志的置位-清除因此成对且只有一处清除点，不会漏清导致后续再也起不来。
        """
        from screenshot.scroll_capture import teardown_scroll
        teardown_scroll(self)
        self._scroll_active = False

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
            self.tray.showMessage(
                "ClickOCR",
                T("app.scroll_unsupported", "滚动截图仅支持 Windows"),
                QSystemTrayIcon.Warning, 3000)
            return
        # ---- 重入保护 ----
        # 滚动截图是「多个窗口 + 多条 pynput 监听线程 + 一个 stop Event」的有状态
        # 流程。连按热键会再走一遍 on_scroll_region → _scroll_capture，叠出第二套
        # ModeMask / picker / worker / 监听线程；更糟的是旧闭包持有的 stop 引用被
        # 新的覆盖，导致**旧的永远停不下来**（Esc 只作用于新那套）→ 状态机错乱，
        # 并最终以「Destroyed while thread is still running」收场。
        # 注意不能靠「遮罩是否可见」拦：选择遮罩发完 region_selected 就 close() 了
        # （那是 start_capture 用的办法，对滚动截图这条路径无效）。
        if getattr(self, "_scroll_active", False):
            log.warning("滚动截图已在进行中，忽略重复触发")
            return
        self._scroll_active = True
        # 开始新一轮前先收尾上一轮（正常情况无事可做）：防止上一轮的
        # worker/监听线程仍在跑到被本次赋值覆盖引用 → GC 回收运行中的 QThread。
        self._teardown_scroll_capture()
        self._restore_hidden()      # 恢复程序浮窗（滚动目标内容必须可见）
        import threading
        stop = threading.Event()
        flags = {"cancel": False}
        mask = ModeMask(raw, getattr(self, "_capture_dpr", self.dpr))
        mask.show()
        picker = ModePicker()
        self._scroll_picker = picker

        def on_cancel_requested():
            """进度牌 Esc = 退出滚动截图（不贴图）。

            先标记 cancel（随后到达的 done/failed 处理器据此丢弃结果），
            再走统一收尾——原先只 stop.set()：pynput 监听线程要「再点一次
            鼠标」才退出，手动模式下 ManualCapture 还会继续抓帧往**无人消费**
            的队列里推。
            """
            flags["cancel"] = True
            self._teardown_scroll_capture()

        def start_worker(mode: str):
            try:
                mask.close()
            except RuntimeError:
                pass
            badge = ScrollBadge()
            # Enter/单击进度牌 = 完成：同样走统一收尾（teardown 内置 stop.set），
            # 收尾放行后 worker 已 emit 的 done 会把长图贴上。
            badge.stop_requested.connect(
                lambda: self._teardown_scroll_capture())
            badge.cancel_requested.connect(on_cancel_requested)
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
            self._teardown_scroll_capture()   # 收尾（停止选择监听线程并清引用）
            self.tray.showMessage(
                "ClickOCR", T("app.scroll_exited", "已退出滚动截图"),
                QSystemTrayIcon.Information, 2000)

        picker.chosen.connect(start_worker)
        picker.cancelled.connect(on_cancelled)
        picker.start()

    def _on_scroll_done(self, badge, raw, flags, img):
        # 收尾：worker 已自行结束（到底/点击停止），此处与进度牌两条路径共用
        # 同一收尾函数——停掉 pynput 监听线程、清引用（见 screenshot.scroll_capture）
        self._teardown_scroll_capture()
        try:
            badge.close()
        except RuntimeError:
            pass
        # Esc 退出：不贴图
        if flags.get("cancel"):
            self.tray.showMessage(
                "ClickOCR", T("app.scroll_exited", "已退出滚动截图"),
                QSystemTrayIcon.Information, 2000)
            return
        if img is None or img.size == 0:
            self.tray.showMessage(
                "ClickOCR", T("app.scroll_no_frame", "滚动截图未获得画面"),
                QSystemTrayIcon.Warning, 3000)
            return
        h, w = img.shape[:2]
        ov = self._make_overlay(img, raw)
        # 长图比框选区域高：整图等比缩放入屏（否则只能看到顶部一屏）
        ov.fit_to_screen()
        self.tray.showMessage(
            "ClickOCR",
            T("app.scroll_done", "滚动截图完成：{w}×{h}").format(w=w, h=h),
            QSystemTrayIcon.Information, 2500)
        if not self.ocr_auto:
            ov._set_status("滚动截图完成 · 点「文字框」或「表格」开始识别")
            return
        self._ocr_overlay(ov, img)

    def _on_scroll_failed(self, badge, flags, msg):
        self._teardown_scroll_capture()   # 失败路径也要收尾（原先漏了）
        try:
            badge.close()
        except RuntimeError:
            pass
        if flags.get("cancel"):
            return                       # Esc 退出：静默，不弹失败提示
        self.tray.showMessage(
            "ClickOCR", T("app.scroll_fail", "滚动截图失败：") + str(msg),
            QSystemTrayIcon.Warning, 3000)


