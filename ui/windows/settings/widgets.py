"""设置面板通用小控件（_ModBox / _ShortcutRowEdit / KeyCaptureEdit）。

从 settings_panel.py 拆出（2026-09-11 重构 S2），纯搬移不改行为。
对外名字由 `ui.windows.settings_panel` 再导出，调用方无需改动。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QCheckBox, QWidget, QHBoxLayout, QPushButton, QLineEdit

from ui.base.texts import T

class _ModBox(QCheckBox):
    """紧凑修饰键：keymod 样式把勾选框渲染为 0 宽，但 sizeHint 仍按默认
    勾选框（18px）计算会把快捷键行撑宽；覆写 sizeHint 按文字宽度。"""

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self.text()) + 14,
                     super().sizeHint().height())


class _ShortcutRowEdit(QWidget):
    """统一快捷键编辑控件：修饰键勾选 + 单键拾取框 + 「启用」开关。

    收敛历史上每个快捷键功能都手写「4 个修饰键 checkbox + 拾取框 + 启用
    按钮」的重复代码 —— 所有快捷键行（全局 / 全屏 / 历史 / 复制 / 局部）
    都复用它；交互统一：未启用 = off、拾取到键自动置启用。

    信号 changed：内部任一子控件被改时发出（保存循环直接读 combo()，
    无需逐个控件登记）。
    """

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mods = tuple(_ModBox(T(m, d)) for m, d in
                           (("hk.mod_ctrl", "Ctrl"), ("hk.mod_alt", "Alt"),
                            ("hk.mod_shift", "Shift"), ("hk.mod_win", "Win")))
        for cb in self._mods:
            cb.setProperty("kind", "keymod")
        self._cap = KeyCaptureEdit()
        self._chk = QPushButton(T("hk.enable", "启用"))
        self._chk.setProperty("kind", "corner")
        self._chk.setCheckable(True)
        self._chk.setFixedHeight(24)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        for w in (*self._mods, self._cap, self._chk):
            lay.addWidget(w)
        lay.addStretch(1)
        # 交互联动：拾取到键自动启用；启用开关控制拾取框可点性。
        # toggled(keyCaptured) 会带参数，用 lambda 丢弃并转成无参 changed
        self._cap.keyCaptured.connect(self._on_capture)
        self._chk.toggled.connect(self._sync_enabled)
        self._chk.toggled.connect(lambda _=False: self.changed.emit())
        self._cap.keyCaptured.connect(lambda _k="": self.changed.emit())

    def _on_capture(self, _k):
        self._chk.setChecked(True)

    def _sync_enabled(self):
        self._cap.setEnabled(self._chk.isChecked())
        if not self._chk.isChecked():
            self._cap.clearFocus()

    def set_combo(self, combo: str):
        """从组合字符串加载：ctrl+alt+shift+r / off / 空。"""
        from ui.windows.settings_panel import SettingsPanel  # 延迟导入：_key_from_combo 在 SettingsPanel 上，运行期才需要
        enabled = bool(combo) and combo.lower() not in ("off", "none", "false")
        self._chk.setChecked(enabled)
        low = combo.lower().replace("+", " ")
        names = ("ctrl", "alt", "shift", "win")
        for cb, name in zip(self._mods, names):
            cb.setChecked(enabled and name in low)
        if enabled:
            self._cap.set_key(SettingsPanel._key_from_combo(combo, "a"))
        self._sync_enabled()

    def combo(self) -> str:
        """生成组合字符串；未启用返回 "off"。"""
        if not self._chk.isChecked():
            return "off"
        parts = []
        for cb, name in zip(self._mods, ("ctrl", "alt", "shift", "win")):
            if cb.isChecked():
                parts.append(name)
        parts.append(self._cap.key())
        return "+".join(parts)


class KeyCaptureEdit(QLineEdit):
    """键位拾取框：点击后捕获用户按下的**单个键**（如 C）并显示。

    替代键位下拉框：修饰键（Ctrl/Alt/Shift/Win）由行内勾选负责，
    这里只拾取末尾的字母键；显示大写（C），保存用小写（c）。
    """

    keyCaptured = Signal(str)    # 捕获到新键（小写，如 "c"）

    def __init__(self, key: str = "", parent=None):
        super().__init__(parent)
        self._capturing = False
        self.setReadOnly(True)
        self.setFixedWidth(36)             # 单字母宽度（一个键位足够）
        self.setPlaceholderText(T("hk.capture_ph", "键"))
        self.setToolTip(T("ui.key_capture_tip", "点击输入框，然后按下要用的字母键（如 C）"))
        self.setCursor(Qt.PointingHandCursor)
        self.set_key(key)

    # ---- 显示 ----
    def sizeHint(self):
        # setFixedWidth 不改变 QLineEdit.sizeHint()（父布局仍按 placeholder 计算，
        # 会把快捷键 tab 撑宽）；显式返回固定宽度
        return QSize(36, super().sizeHint().height())

    def _show_key(self, key: str):
        from ui.base.hotkey_keys import token_display
        self.setText(token_display(key))

    def set_key(self, key: str):
        """同时写回内部状态与显示——唯一入口，避免「显示对、状态错」。

        `_show_key` 只改显示文本，`combo()` 读的却是 `self._key`（内部状态）。
        若只调 `_show_key`，会出现「界面上有字母、保存时丢字母」。故加载/捕获
        一律走本方法，保证两处同步。
        """
        self._key = key
        self._show_key(key)

    # ---- 交互 ----
    def mousePressEvent(self, e):
        self._capturing = True
        self.setText("…")
        self.setFocus(Qt.MouseFocusReason)
        e.accept()

    def keyPressEvent(self, e: QKeyEvent):
        if not self._capturing:
            return
        # 支持的键：字母 / 数字 / F1-F12 / 方向键 / 空格 / 反引号 / 常用标点；
        # 修饰键与功能键（Esc/Tab 等）忽略
        from ui.base.hotkey_keys import qt_to_token
        tok = qt_to_token(e.key())
        if tok is None:
            return
        self._capturing = False
        self.set_key(tok)
        self.keyCaptured.emit(self._key)
        e.accept()

    def focusOutEvent(self, e):
        if self._capturing:
            self._capturing = False
            self._show_key(self._key)
        super().focusOutEvent(e)

    def key(self) -> str:
        return self._key


