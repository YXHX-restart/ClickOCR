"""ui.components.float_group — 浮窗组管理器：主窗 + 附属浮窗的 z 序 / 绘制让位 / 位置跟随收口。

把散落在各处的手动 raise_() 调用、位置跟随同步、80ms 全局光标轮询
（绘制让位）收口到这一个管理器，减少同步复杂度（不减窗口数）。

职责：
- z 序：raise_all()——主窗交互（按下/显示/绘制结束）统一提级整组；
  钉图态支持延迟 raise（对抗 Windows 异步激活把图片提到最前压过钉子）。
- 绘制让位：maybe_yield()——宿主绘制中（左键按住画布，鼠标事件被 capture
  到画布，浮窗收不到 Enter/Leave），由宿主在 mouseMove 时调用本方法，检查
  QCursor 是否进入成员浮窗，进入即隐藏并记录位置；绘制结束 restore_all()
  恢复并提级。事件驱动（光标移动才检查），替代固定 80ms 轮询。
- 位置跟随：follow()——主窗 move/resize 后调用，各成员注册的 follow 回调统一执行。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QCursor


class FloatGroup(QObject):
    """浮窗组管理器：主窗 + 附属浮窗的 z 序 / 绘制让位 / 位置跟随收口。"""

    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner
        self._members: list = []        # [(widget, follow_cb|None)]
        self._hidden: dict = {}         # widget -> geometry（让位隐藏时记录）
        self._yield = False             # 是否允许让位（标注模式下开启）
        self._follow_warned: set = set()  # 已报过跟随失败的成员（id()），避免每帧刷屏

    # ---- 注册 ----
    def add(self, widget, follow=None):
        """注册附属浮窗；follow 为可选的位置跟随回调（主窗移动/缩放时调用）。"""
        if widget is None or any(w is widget for w, _ in self._members):
            return
        self._members.append((widget, follow))

    # ---- z 序 ----
    def raise_all(self, deferred: bool = False):
        """整组提级：主窗 + 所有可见附属窗。

        deferred=True 时延迟到事件循环下一轮（钉图态图片被点击/激活后
        Windows 异步提层会压过钉子，延迟 raise 对抗之）。
        """
        def _raise(w):
            if w is not None:
                if deferred:
                    QTimer.singleShot(0, w.raise_)
                else:
                    w.raise_()
        _raise(self._owner)
        for w, _ in self._members:
            if w is not None and w.isVisible():
                _raise(w)

    # ---- 位置跟随 ----
    def follow(self):
        """主窗 move/resize 后重排附属窗（执行各成员的 follow 回调）。

        回调异常不外抛（一次布局失败不该打断 move/resize 事件链），但**必须留痕**：
        静默 `pass` 会让「附属浮窗不跟随」这类故障完全无声，只能靠肉眼发现。
        每个成员只报一次 —— follow 在拖动窗口时每帧都会跑，不加限制会刷屏。
        """
        for w, cb in self._members:
            if w is not None and cb is not None:
                try:
                    cb()
                except Exception as e:        # noqa: BLE001
                    key = id(w)
                    if key not in self._follow_warned:
                        self._follow_warned.add(key)
                        from core_engine.logging_setup import get_logger
                        get_logger("float_group").warning(
                            "附属浮窗跟随回调失败（同一成员后续不再重复报告）：%s", e)

    # ---- 绘制让位（替代 80ms 轮询）----
    def set_yield_enabled(self, on: bool):
        """开启/关闭让位（进入/退出标注模式时切换）。关闭时立即恢复。"""
        self._yield = bool(on)
        if not on:
            self.restore_all()

    def maybe_yield(self):
        """绘制中：光标进入成员浮窗 → 隐藏该成员（记录位置，release 恢复）。

        由宿主在绘制中的 mouseMoveEvent 里调用（事件驱动，光标移动才检查）。
        """
        if not self._yield or self._owner is None:
            return
        if not getattr(self._owner, "is_drawing", False):
            return
        pos = QCursor.pos()
        for w, _ in self._members:
            if (w is not None and w.isVisible()
                    and w not in self._hidden
                    and w.frameGeometry().contains(pos)):
                self._hidden[w] = w.geometry()
                w.hide()

    def restore_all(self):
        """绘制结束：恢复让位隐藏的浮窗并提级。"""
        for w in list(self._hidden):
            if w is not None:
                w.show()
                w.raise_()
        self._hidden.clear()

    # ---- 清理 ----
    def shutdown(self):
        """窗口关闭时清理引用（浮窗本体由宿主各自管理）。"""
        self._hidden.clear()
        self._members = []
