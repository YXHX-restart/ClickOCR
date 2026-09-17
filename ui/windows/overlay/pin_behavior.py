"""钉图行为：钉点、钉图卡片、附属浮窗收起 —— 从 result_overlay.py 按职责搬移（纯搬移，行为零变化）。

归属断言见 scripts/_verify_review_fixes.py 的 5f 区块：方法一旦被别的 mixin 或
ResultOverlay 同名覆盖，MRO 会**静默遮蔽**（不报错、只是行为变了），故必须断言。
"""
from __future__ import annotations


class PinMixin:
    """钉图行为（钉点、卡片、贴图/钉图状态切换）"""
    def _ensure_pin_dot(self):
        """懒创建钉图钉子（PinDot 独立小窗）；信号接回本窗口。"""
        if self._pin_dot is not None:
            return self._pin_dot
        from ui.components.pin_dot import PinDot
        dot = PinDot()
        dot.clicked.connect(self._on_pin_clicked)
        dot.double_clicked.connect(self._on_pin_double)
        dot.drag.connect(self._on_pin_drag)
        dot.hover_in.connect(self._on_pin_hover_in)
        dot.hover_out.connect(self._on_pin_hover_out)
        self._pin_dot = dot
        self._fgroup.add(dot)             # 参与 z 序（钉图态提级）
        return dot

    def set_pinned(self, pinned: bool, hide_img: bool = False):
        """钉图：pinned=True 隐藏图片（可选）/状态栏/工具栏，只留 PinDot 钉子；
        False 恢复全部浮层。钉子定位由 app._pin_overlay 统一处理。"""
        self._pinned = pinned
        if pinned:
            # getattr 防御：closeEvent 会把 btn_close 置 None，而 PinDot 是独立
            # 顶层窗，其信号（点钉子）可能晚于关窗送达 —— 直接属性访问会
            # AttributeError（关图瞬间点钉子概率性崩溃）。此处与同函数其它
            # 浮层访问保持一致。
            if getattr(self, "btn_close", None) is not None:
                self.btn_close.hide()      # 关闭按钮消失
            if self._navigator is not None:
                self._navigator.hide()     # 导航图一起隐藏
            if self._status_strip is not None:
                self._status_strip.hide()  # 状态栏一起隐藏
            if self._toolbar is not None:
                self._toolbar.close_picker()
                self._toolbar.hide()       # 工具栏一起隐藏
        else:
            self._layout_close_btn()       # 立即重定位关闭按钮（钉图期间图片可能移动过）
            self._hide_pin_card()
            if self._status_strip is not None:
                self._status_strip.show()
            if self._toolbar is not None:
                self._toolbar.show()
            if self._navigator is not None and self._nav_on:
                self._navigator.show()
                self._layout_navigator()
        if hide_img:
            self.hide()                    # 图片收进钉子（PinDot 留在桌面）
        else:
            self.show()
            self._fgroup.raise_all()
            if not pinned:
                self._fgroup.follow()      # 取消钉图：恢复正常位置/尺寸

    def _on_pin_clicked(self):
        """单击钉子：取消钉图（恢复图片 + 状态栏 + 工具栏）。"""
        self._hide_pin_card()
        if self._toolbar is not None:
            self._toolbar.btn_pin.setChecked(False)   # 信号链 → _unpin → 恢复
        else:
            self.set_pinned(False)

    def _on_pin_double(self):
        """双击钉子：收起图片（钉子留在桌面）/ 图片收起时双击恢复。"""
        if self.isVisible():
            # 收起前记住图片位置：恢复时回原位（不依赖钉子当前位置反推，
            # 避免钉子位置漂移时图片飞到屏幕外 = "图片不保持"）
            self._pin_restore = (self.x(), self.y())
            self.hide()                    # 收起图片，钉子浮桌面
        else:
            if getattr(self, "_pin_restore", None) is not None:
                self.move(*self._pin_restore)      # 回原位
            else:
                dot = self._pin_dot                # 兜底：右下角对准钉子
                if dot is not None:
                    self.move(dot.x() + 38 - self.width(),
                              dot.y() + 38 - self.height())
            self.show()                    # 恢复图片显示
            self._fgroup.raise_all()
            self._fgroup.follow()

    def _on_pin_drag(self, delta):
        """拖动钉子 → 图片窗口跟着移动（隐藏态也同步 _pin_restore，
        确保双击恢复时落到拖动后的位置，不回弹）。"""
        self.move(self.x() + delta.x(), self.y() + delta.y())
        self._pin_restore = (self.x(), self.y())

    def _on_pin_hover_in(self):
        if self.isVisible():               # 图片显示时不显示卡片
            return
        self._show_pin_card()

    def _on_pin_hover_out(self):
        self._hide_pin_card()

    def _show_pin_card(self):
        """悬停钉子：显示缩略图 + OCR 文字/尺寸信息卡片。"""
        from ui.components.pin_card import PinCard
        if self._pin_card is None:
            self._pin_card = PinCard()
        self._pin_card.set_image(self.img)
        text = "\n".join(b.text for b in self.boxes) if self.boxes else ""
        info = f"{self.img.shape[1]}×{self.img.shape[0]}"
        if self.history_id:
            info += f" · #{self.history_id}"
        self._pin_card.set_info(text, info)
        # 定位：钉子左上方
        dot = self._pin_dot
        g = dot.mapToGlobal(dot.rect().topLeft()) if dot is not None \
            else self.mapToGlobal(self.rect().topLeft())
        self._pin_card.show_near(g)
        self._pin_card.show()
        self._pin_card.raise_()

    def _hide_pin_card(self):
        if self._pin_card is not None:
            self._pin_card.hide()

    def _close_sub_windows(self):
        """关闭与图片关联的次级浮窗（标注工具段子浮层、快捷键面板等），
        无动画钉图时使用，避免浮窗残留。"""
        if self._toolbar is not None:
            try:
                self._toolbar.close_picker()   # 关闭标注子浮层 + 调色板
            except Exception:            # noqa: BLE001
                pass
        if self._shortcut_panel is not None:
            self._shortcut_panel.close()
            self._shortcut_panel = None
