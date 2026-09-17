"""标注会话接线：进入/退出标注、工具与样式回调、标注键盘分发 —— 从 result_overlay.py 按职责搬移（纯搬移，行为零变化）。

归属断言见 scripts/_verify_review_fixes.py 的 5f 区块：方法一旦被别的 mixin 或
ResultOverlay 同名覆盖，MRO 会**静默遮蔽**（不报错、只是行为变了），故必须断言。
"""
from __future__ import annotations

from PySide6.QtCore import Qt



class AnnoMixin:
    """标注会话接线（AnnotationSession 的宿主回调）"""
    def _toggle_annotation(self):
        """Ctrl+Shift+D / 快捷键：切换标注模式（QShortcut 与 keyPressEvent 共用）。"""
        if self._anno_mode:
            self._exit_annotation()
        else:
            self._enter_annotation()

    def _on_anno_btn_toggled(self, on: bool):
        """「标注」按钮状态变化 → 同步标注模式（与文字框按钮同款状态显示）。"""
        if on:
            self._enter_annotation()
        else:
            self._exit_annotation()

    def _enter_annotation(self):
        if self._anno_mode:
            return
        from ui.components.annotation import AnnotationSession
        if self._anno is None:
            self._anno = AnnotationSession(self, self.scene, self._pix)
        self._anno_mode = True
        # 统一工具栏标注态：同一行内联展开工具段（无独立标注工具栏顶层窗）
        if self._toolbar is not None:
            self._toolbar.set_anno_mode(True)
            self._toolbar.btn_anno.setChecked(True)  # 标注按钮按下高亮
            self._anno.set_num_start(self._toolbar.num_start)
            self._anno.set_num_size(self._toolbar.num_size)
            self._anno.set_num_style(self._toolbar.num_style)
            self._anno.set_tool_style("rect", self._toolbar.rect_style)
            self._anno.set_tool_style("arrow", self._toolbar.arrow_style)
            self._anno.set_tool_style("line", self._toolbar.line_style)
            self._anno.set_tool_style("pen", self._toolbar.pen_style)
        self._fgroup.set_yield_enabled(True)   # 开启绘制让位（事件驱动，替代 80ms 轮询）
        self._fgroup.raise_all()
        self._sync_toolbar_pos()           # 工具段展开：工具栏变长重排
        self._set_status("标注模式：拖拽绘制 · Ctrl+Z 撤销 · Esc 退出")

    def _save_mosaic_mode(self, mode: str):
        """马赛克模式：更新会话 + 按开关持久化（下次打开标注保持上次选择）。"""
        if self._anno is not None:
            self._anno.set_mosaic_mode(mode)
        self._save_anno_param("mosaic_mode", mode)

    def _on_anno_color(self, c: tuple):
        """颜色变更：即时更新会话 + 按开关持久化。"""
        if self._anno is not None:
            self._anno.set_color(tuple(c))
        self._save_anno_param("tool_color", ",".join(str(int(v)) for v in c))

    def _on_anno_width(self, w: int):
        """线宽变更：即时更新会话 + 按开关持久化。"""
        if self._anno is not None:
            self._anno.set_width(w)
        self._save_anno_param("tool_width", str(w))

    def _on_num_start(self, v: int):
        """序号起始序号变更：更新会话起始值并重排已有序号 + 按开关持久化。"""
        self._save_anno_param("num_start", str(int(v)))
        if self._anno is not None:
            self._anno.set_num_start(int(v), renumber=True)

    def _on_num_size(self, v: int):
        """序号圆圈尺寸变更：即时更新会话 + 按开关持久化。"""
        self._save_anno_param("num_size", str(int(v)))
        if self._anno is not None:
            self._anno.set_num_size(v)

    def _on_num_style(self, v: str):
        """序号样式变更：即时更新会话（后续放置用新样式）+ 按开关持久化。"""
        self._save_anno_param("num_style", str(v))
        if self._anno is not None:
            self._anno.set_num_style(v)

    def _on_tool_style(self, tool: str, style: str):
        """矩形/箭头/画笔样式变更：即时更新会话 + 按开关持久化。"""
        self._save_anno_param(f"{tool}_style", str(style))
        if self._anno is not None:
            self._anno.set_tool_style(tool, style)

    def _switch_tool_by_key(self, key) -> bool:
        """按 QWERTYU 位置映射切换标注工具；命中返回 True。"""
        from ui.components.annotation import TOOLS
        try:
            idx = [getattr(Qt, f"Key_{c.upper()}") for c in self._TOOL_KEYS] \
                .index(key)
        except ValueError:
            return False
        if idx >= len(TOOLS):
            return False
        tool = TOOLS[idx]
        tb = self._toolbar
        if tb is not None:
            tb.select_tool(tool)          # 切工具 + 高亮按钮 + 弹子选项浮层
        self._set_status(
            f"标注工具：{self._TOOL_KEY_NAMES.get(tool, tool)} ("
            f"{self._TOOL_KEYS[idx].upper()})")
        return True

    def handle_anno_key(self, e) -> bool:
        """标注态通用键盘分发（贴图窗口 / 工具栏 / 子选项浮层共用）。

        焦点可能落在工具栏按钮或子浮层上（独立顶层窗），Q/W/E/R/T/Y/U 与
        Esc 都转发到这里统一处理；文字就地编辑时保护输入不拦截。
        返回 True = 已处理。
        """
        if not self._anno_mode or self._anno is None:
            return False
        editing = self._anno._text_edit is not None
        if e.key() == Qt.Key_Escape:
            if editing:
                self._anno.cancel_text_edit()
            else:
                self._on_esc()
            return True
        if e.modifiers() == Qt.NoModifier and not editing:
            if self._switch_tool_by_key(e.key()):
                return True
        return False

    def _save_anno_param(self, key: str, value: str):
        """标注工具参数永久化记录（[ui] persist_params 开关控制）。"""
        if not self.config.getbool("ui", "persist_params", True):
            return
        try:
            self.config.cp.set("annotation", key, str(value))
            self.config.save()
        except OSError:
            pass

    def _exit_annotation(self):
        if not self._anno_mode:
            return
        self._anno_mode = False
        if getattr(self, "_fgroup", None) is not None:
            self._fgroup.set_yield_enabled(False)   # 关闭让位并恢复未恢复的浮窗
        if self._toolbar is not None:
            self._toolbar.set_anno_mode(False)   # 收起工具段（含关闭子浮层）
            self._toolbar.btn_anno.setChecked(False)  # 取消高亮
        if self._anno is not None:
            # 空会话不发信号：annotations_changed → _save_annotations → _save_record
            # 会**无条件落库**，于是「误触进入标注模式再按 Esc」也会凭空造出一条
            # 空标注历史记录。没有任何标注时退出 = 无操作，「无操作是否落库」
            # 归 [software] save_no_op_close 管，不在这里绕过去。
            if self._anno.data:
                self.annotations_changed.emit(self._anno.json())
        self._set_status("已退出标注模式")

    def _anno_undo(self):
        if self._anno is not None:
            self._anno.undo()

    def _anno_redo(self):
        if self._anno is not None:
            self._anno.redo()

    def _anno_clear(self):
        if self._anno is not None:
            self._anno.clear()

    def load_annotations(self, json_text: str | None):
        """恢复历史时载入标注（M2-7 持久化）。"""
        from ui.components.annotation import AnnotationSession, anno_from_json
        items = anno_from_json(json_text)
        if not items:
            return
        if self._anno is None:
            self._anno = AnnotationSession(self, self.scene, self._pix)
        self._anno.load(items)
