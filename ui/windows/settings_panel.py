"""M2-4 设置面板：软件设置 / UI设置 / 快捷键设置 三个 Tab（统一浮层基类）。

- 「软件设置」：历史保留条数（不设上限）、历史图片位置、复制模式（本期仅存配置）。
- 「UI设置」：界面主题（跟随系统/亮/暗）、截图框选边框颜色/宽度、结果图片边框颜色/宽度。
- 「快捷键设置」：全局截图热键、历史记录快捷键、复制快捷键（Shift+C）、快捷键一览。
- 无边框半透明卡片（明/暗主题与历史面板一致）；标题栏可拖动；Esc 关闭。
- 热键基于 HotkeyHost.set_combo 即时生效；主题保存后即时全局应用。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from core_engine.logging_setup import get_logger

log = get_logger("settings")

from PySide6.QtCore import Qt, Signal, QSize, QUrl
from PySide6.QtGui import QKeyEvent, QDesktopServices
from PySide6.QtWidgets import (QFormLayout, QHBoxLayout, QVBoxLayout,
                               QTabWidget, QWidget, QCheckBox, QComboBox,
                               QLineEdit, QSpinBox, QLabel, QPushButton,
                               QApplication, QFileDialog, QScrollArea, QFrame,
                               QSlider)

from ui.base.theme_dialog import ThemeDialog

from config import ConfigManager
from ui.base.card_window import CardWindow
from ui.base.texts import T
from ui.windows.result_overlay import _parse_qt_combo
from ui.base.theme import apply_theme, resolve_theme
from ui.base.widgets import (ThemeSpinBox, ColorButton as _ColorButton,
                             FitComboBox)
from ui.base.theme_dialog import ThemeDialog


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
        enabled = bool(combo) and combo.lower() not in ("off", "none", "false")
        self._chk.setChecked(enabled)
        low = combo.lower().replace("+", " ")
        names = ("ctrl", "alt", "shift", "win")
        for cb, name in zip(self._mods, names):
            cb.setChecked(enabled and name in low)
        if enabled:
            self._cap._show_key(SettingsPanel._key_from_combo(combo, "a"))
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
        self._key = key
        self.setReadOnly(True)
        self.setFixedWidth(36)             # 单字母宽度（一个键位足够）
        self.setPlaceholderText(T("hk.capture_ph", "键"))
        self.setToolTip(T("ui.key_capture_tip", "点击输入框，然后按下要用的字母键（如 C）"))
        self.setCursor(Qt.PointingHandCursor)
        self._show_key(key)

    # ---- 显示 ----
    def sizeHint(self):
        # setFixedWidth 不改变 QLineEdit.sizeHint()（父布局仍按 placeholder 计算，
        # 会把快捷键 tab 撑宽）；显式返回固定宽度
        return QSize(36, super().sizeHint().height())

    def _show_key(self, key: str):
        from ui.base.hotkey_keys import token_display
        self.setText(token_display(key))

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
        self._key = tok
        self._capturing = False
        self._show_key(self._key)
        self.keyCaptured.emit(self._key)
        e.accept()

    def focusOutEvent(self, e):
        if self._capturing:
            self._capturing = False
            self._show_key(self._key)
        super().focusOutEvent(e)

    def key(self) -> str:
        return self._key


class SettingsPanel(CardWindow):
    def __init__(self, config: ConfigManager, hotkey_host=None,
                 history_storage=None, parent=None, tray=None,
                 on_clear_history=None, on_model_changed=None):
        super().__init__(title=T("settings.title", "ClickOCR 设置"), with_close=True)
        self.setMinimumWidth(400)
        self._cfg = config
        self._hotkey = hotkey_host
        self._history = history_storage
        self._tray = tray            # 托盘（保存成功等用气泡通知）
        self._on_clear_history = on_clear_history  # App 回调：清空后刷新历史面板
        # 模型档位旧值：_save 检测变化后调 App 回调热切换引擎（仅重建内部引擎）
        self._model_set_old = self._cfg.get("software", "ocr_model", "v6-tiny")
        self._on_model_changed = on_model_changed  # App 回调：模型档位切换

        self.card_lay.setSpacing(10)
        # 注意：QTabWidget 必须保存为 self 属性（局部变量会被 PySide6 GC 连带
        # 销毁页面控件，HistoryPanel 同款写法），否则 tab 页内控件 C++ 对象失效
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_software_tab(), T("tab.software", "软件设置"))
        self.tabs.addTab(self._build_ui_tab(), T("tab.ui", "UI设置"))
        self.tabs.addTab(self._build_hotkey_tab(), T("tab.hotkey", "快捷键设置"))
        self.card_lay.addWidget(self.tabs, 1)
        # 「操作一览」仅在快捷键设置 tab 显示（index 2）
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # 窗口尺寸：逻辑 600×720 会随系统 DPI 放大（150% 屏显示 900×1080）。
        # 按 DPR 折算逻辑尺寸，保证**屏幕上物理尺寸**为 600×720。
        try:
            _dpr = QApplication.primaryScreen().devicePixelRatio() or 1.0
        except Exception:  # noqa: BLE001
            _dpr = 1.0
        self.setFixedSize(max(400, int(round(600 / _dpr))),
                          max(480, int(round(720 / _dpr))))

        # ---- 按钮 ----
        btns = QHBoxLayout()
        btns.setSpacing(8)
        # 快捷键一览（操作一览）：仅「快捷键设置」tab 显示，样式同保存/关闭
        self.btn_shortcut = QPushButton(T("btn.shortcut_list", "操作一览"))
        self.btn_shortcut.clicked.connect(self._open_shortcut_panel)
        self.btn_shortcut.hide()
        btns.addWidget(self.btn_shortcut)
        btns.addStretch(1)
        b_save = QPushButton(T("btn.save", "保存"))
        b_close = QPushButton(T("btn.close", "关闭"))
        b_save.clicked.connect(self._save)
        b_close.clicked.connect(self.close)
        btns.addWidget(b_save)
        btns.addWidget(b_close)
        self.card_lay.addLayout(btns)

    def _scroll_page(self) -> tuple:
        """带滚动区的 tab 页面：返回 (page, scroll, inner)。

        页面内容超高时滚动（固定窗口高度下不被裁剪，避免 wordWrap 标签与
        布局高度协商死循环）；关闭 viewport 与内容控件 autoFillBackground
        （暗色主题下不露白底）。
        """
        page = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.viewport().setAutoFillBackground(False)
        inner = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scroll)
        return page, scroll, inner

    def _notify(self, msg: str):
        """托盘气泡通知（保存结果等）；托盘不可用时静默兜底。"""
        try:
            from PySide6.QtWidgets import QSystemTrayIcon
            tray = self._tray
            if tray is not None and QSystemTrayIcon.isSystemTrayAvailable():
                tray.showMessage(T("settings.title", "ClickOCR 设置"), msg,
                                 QSystemTrayIcon.Information, 2500)
        except Exception:            # noqa: BLE001
            pass

    def _form(self, parent: QWidget) -> QFormLayout:
        form = QFormLayout(parent)
        form.setSpacing(10)
        form.setContentsMargins(8, 14, 8, 8)
        return form

    @staticmethod
    def _attach_hint(form: QFormLayout, field, text: str):
        """说明文字不占行：挂到功能行 label 与行内所有控件 tooltip，悬停浮现。

        field 可为 QWidget（单个控件）或 QLayout（一行布局）；按钮除外
        （保留各自原有 tooltip）。用 Qt 默认延迟（稳定），不设 setDelay——
        过短延迟会在鼠标微动时重置计时，导致时灵时不灵。"""
        lbl = form.labelForField(field)
        if lbl is not None:
            lbl.setToolTip(text)
        widgets = [field] if isinstance(field, QWidget) else []
        if isinstance(field, QHBoxLayout):
            for i in range(field.count()):
                it = field.itemAt(i)
                if it.widget() is not None:
                    widgets.append(it.widget())
        for wd in widgets:
            if isinstance(wd, QPushButton):
                continue                # 按钮保留自己的 tooltip（浏览/检测等）
            cur = wd.toolTip()
            wd.setToolTip(f"{cur}\n\n{text}" if cur else text)
            # ThemeSpinBox 等组合控件：悬停实际落在内部 .spin 上，需一并挂
            inner = getattr(wd, "spin", None)
            if isinstance(inner, QWidget):
                cur = inner.toolTip()
                inner.setToolTip(f"{cur}\n\n{text}" if cur else text)

    # ---- Tab 1：软件设置 ----
    def _build_software_tab(self) -> QWidget:
        page, scroll, inner = self._scroll_page()
        w = inner
        form = self._form(w)

        # 历史保留条数（默认 50，不设上限）
        self.spin_capacity = ThemeSpinBox(1, 3000,   # 上限 3000 张
                                          self._cfg.getint(
                                              "software", "history_max_count", 50),
                                          width=56)
        form.addRow(T("history.count", "历史数量"), self.spin_capacity)
        self._attach_hint(
            form, self.spin_capacity,
            T("ui.hist_count_hint", "默认 50 条；条数越多占用磁盘越大（超出时自动清理最旧记录）。"))

        # 历史图片位置
        self.edit_shot_dir = QLineEdit(
            self._cfg.get("software", "screenshots_dir", ""))
        self.edit_shot_dir.setPlaceholderText(T("ui.shot_dir_ph", "留空 = 软件目录下 screenshots/"))
        b_browse = QPushButton(T("btn.browse", "浏览…"))
        b_browse.clicked.connect(self._pick_shot_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.edit_shot_dir, 1)
        dir_row.addWidget(b_browse)
        form.addRow(T("history.file_loc", "文件位置"), dir_row)
        self._attach_hint(
            form, dir_row,
            T("ui.shot_dir_hint", "截图与缩略图保存目录；可填绝对路径或相对软件根的路径。"))

        # 截图贴图最大数量（默认 1，打开下一个销毁最旧）
        self.spin_max_shot = ThemeSpinBox(
            1, 10, self._cfg.getint("software", "max_history_windows", 1), width=56)
        form.addRow(T("history.shot_count", "截图贴图"), self.spin_max_shot)

        # 历史贴图最大数量（从历史记录恢复的图片窗口，默认 3）
        self.spin_max_hist = ThemeSpinBox(
            1, 10, self._cfg.getint("software", "max_hist_windows", 3), width=56)
        form.addRow(T("history.hist_count", "历史贴图"), self.spin_max_hist)
        self._attach_hint(
            form, self.spin_max_hist,
            T("ui.hist_restore_hint",
              "历史贴图 = 从历史记录恢复打开的图片窗口；超出上限时自动关闭最旧的历史贴图。"))

        # 钉图数量上限（默认 3，上限 20；钉图不占贴图数量）
        self.spin_max_pin = ThemeSpinBox(
            1, 20, self._cfg.getint("software", "max_pin_windows", 3), width=56)
        form.addRow(T("history.pin_count", "钉图数量"), self.spin_max_pin)
        self._attach_hint(
            form, self.spin_max_pin,
            T("ui.pin_hint", "钉图把截图/历史图片置顶显示，不占用贴图数量；超出上限时自动移除最旧的钉图。"))

        # 复制模式（本期仅存配置，行为不变）
        self.combo_selection = FitComboBox()
        self.combo_selection.addItem(T("ui.copy_mode_auto", "auto_commit（选中即复制，现行为）"), "auto_commit")
        self.combo_selection.addItem(T("ui.copy_mode_manual", "manual_copy（手动 Ctrl+C，本期仅预留）"), "manual_copy")
        cur2 = self._cfg.get("software", "selection_mode", "auto_commit")
        i2 = self.combo_selection.findData(cur2)
        self.combo_selection.setCurrentIndex(i2 if i2 >= 0 else 0)
        form.addRow(T("history.copy_mode", "复制模式"), self.combo_selection)
        self._attach_hint(
            form, self.combo_selection,
            T("ui.copy_mode_hint", "提示：复制模式本期仅保存配置，行为暂不变（M2 计划 §5.2）。"))

        # ---- 图片保存格式（PNG/JPG/BMP/WebP）+ 质量（有损格式 1-100）----
        self.combo_save_fmt = FitComboBox()
        for label, data in ((T("ui.save_fmt_png", "PNG（无损）"), "png"),
                            (T("ui.save_fmt_jpg", "JPG（有损·小体积）"), "jpg"),
                            (T("ui.save_fmt_bmp", "BMP（无损）"), "bmp"),
                            (T("ui.save_fmt_webp", "WebP（有损·小体积）"), "webp")):
            self.combo_save_fmt.addItem(label, data)
        cur_fmt = self._cfg.get("software", "save_format", "png").lower()
        if cur_fmt not in ("png", "jpg", "bmp", "webp"):
            cur_fmt = "png"
        self.combo_save_fmt.setCurrentIndex(
            self.combo_save_fmt.findData(cur_fmt))
        form.addRow(T("ui.save_fmt", "保存格式"), self.combo_save_fmt)
        self._attach_hint(
            form, self.combo_save_fmt,
            T("ui.save_fmt_hint",
              "历史记录图片的保存格式；JPG/WebP 体积更小，PNG/BMP 无损。"))

        self.slider_quality = QSlider(Qt.Horizontal)
        self.slider_quality.setRange(1, 100)
        self.slider_quality.setFixedWidth(140)
        self.slider_quality.setValue(max(1, min(100, self._cfg.getint(
            "software", "save_quality", 90))))
        self.lbl_quality = QLabel(str(self.slider_quality.value()))
        self.slider_quality.valueChanged.connect(
            lambda v: self.lbl_quality.setText(str(v)))
        quality_row = QHBoxLayout()
        quality_row.addWidget(self.slider_quality)
        quality_row.addWidget(self.lbl_quality)
        quality_row.addStretch(1)
        form.addRow(T("ui.save_quality", "图片质量"), quality_row)
        self._attach_hint(
            form, quality_row,
            T("ui.save_quality_hint",
              "JPG/WebP 有损压缩质量（1-100，越高越清晰体积越大）；"
              "PNG/BMP 无损，质量不生效。"))
        # 质量仅对 JPG/WebP 生效
        self.combo_save_fmt.currentIndexChanged.connect(
            self._on_save_fmt_changed)
        self._on_save_fmt_changed()

        # ---- 文件名规则（占位符模板，默认时间排序）----
        self.edit_name_pattern = QLineEdit(
            self._cfg.get("software", "name_pattern", "{datetime}_{ms}"))
        self.edit_name_pattern.setPlaceholderText(
            T("ui.name_pattern_ph", "{datetime}_{ms}"))
        form.addRow(T("ui.name_pattern", "文件名规则"),
                    self.edit_name_pattern)
        self._attach_hint(
            form, self.edit_name_pattern,
            T("ui.name_pattern_hint",
              "文件名占位符模板，默认 {datetime}_{ms}（时间排序）。\n"
              "{datetime}=日期时间 {date}=日期 {time}=时间 {ms}=毫秒 "
              "{ts}=时间戳 {seq}=序号 {ext}=扩展名；"
              "留空 = 默认模板，重名自动加 _2 后缀。"))

        # GPU 加速（[software] ocr_device，按平台动态：Win=cpu|cuda|dml；Linux=cpu|cuda|migraphx|openvino）
        from core_engine.ort_runtime import available_devices
        # 各加速方式的内存开销说明（悬停 tooltip）
        self._MEM_HINT = {
            "cpu": T("ui.mem_cpu", "无额外显存占用。"),
            "cuda": T("ui.mem_cuda", "额外占用显存约 0.5~1.5GB（模型权重 + CUDA 上下文）；需 NVIDIA 显卡 + 驱动。"),
            "dml": T("ui.mem_dml", "额外占用显存/共享内存约 0.3~1GB（核显用系统内存）；任意 DX12 显卡（AMD/Intel/NVIDIA）。"),
            "migraphx": T("ui.mem_migraphx", "额外占用显存约 0.5~1.5GB；需 AMD Radeon 7000/9000 + ROCm。"),
            "openvino": T("ui.mem_openvino", "额外占用显存约 0.3~1GB；需 Intel 核显 / Arc / NPU。"),
        }
        self.combo_accel = FitComboBox()
        _dev_labels = {
            "cpu": ("ui.accel_auto", "自动（CPU）"),
            "cuda": ("ui.accel_cuda", "CUDA（NVIDIA 显卡）"),
            "dml": ("ui.accel_dml", "DirectML（AMD / Intel / 通用）"),
            "migraphx": ("ui.accel_migraphx", "MIGraphX（AMD 显卡，Linux）"),
            "openvino": ("ui.accel_openvino", "OpenVINO（Intel 核显 / Arc / NPU，Linux）"),
        }
        for _d in available_devices():
            _k, _df = _dev_labels[_d]
            _text = T(_k, _df)
            # DML / CUDA 选项：把最近一次检测的倍率拼进选项文字，
            # 这样「检测后」与「下次重开」显示一致；同时附在 tooltip（含显存说明）。
            # 选项文字较长由 FitComboBox 宽度上限兜底，不会把整行撑宽触发横滚动条。
            _tip = self._MEM_HINT.get(_d, "")
            if _d in ("dml", "cuda"):
                _ok = [x for x in self._cfg_get_probe(_d).get("devices", [])
                       if x.get("ok")]
                if _ok:
                    _best = max(_ok, key=lambda x: x.get("speedup", 0))
                    _text += f"（≈{_best['speedup']:.1f}×）"
                    _tip += f"\n（实测推理 ≈{_best['speedup']:.1f}×）"
            self.combo_accel.addItem(_text, _d)
            _idx = self.combo_accel.count() - 1
            self.combo_accel.setItemData(_idx, _tip, Qt.ToolTipRole)
        dev = self._cfg.get("software", "ocr_device", "cpu").lower()
        i = self.combo_accel.findData(dev if dev in available_devices() else "cpu")
        self.combo_accel.setCurrentIndex(i if i >= 0 else 0)
        self.lbl_accel_status = QLabel("")
        self.lbl_accel_status.setProperty("kind", "info")
        self.lbl_accel_status.setWordWrap(True)
        accel_row = QHBoxLayout()
        accel_row.addWidget(self.combo_accel, 1)
        self.btn_accel_install = QPushButton(T("btn.detect", "检测"))
        self.btn_accel_install.setToolTip(T(
            "ui.accel_install_tip",
            "检测 DirectML 各设备的实际推理倍率（记录到配置，下拉框显示）；"
            "CUDA 显示就绪状态。检测约 1~4 秒。"))
        self.btn_accel_install.clicked.connect(self._run_gpu_probe)
        accel_row.addWidget(self.btn_accel_install)
        form.addRow(T("gpu.accel", "GPU 加速"), accel_row)
        self._attach_hint(
            form, accel_row,
            T("ui.accel_hint",
              "CUDA 需 onnxruntime-gpu；DirectML 需 onnxruntime-directml"
              "（AMD/Intel 核显/NVIDIA 通用）。无对应环境自动回退 CPU。重启程序生效。"))
        self._refresh_accel_status()

        # ---- OCR 模型档位（[software] ocr_model = v6-tiny | v6-small | v6-medium）----
        self.combo_model = FitComboBox()
        self.combo_model.addItem(T("ui.model_tiny", "轻量（v6-tiny）"), "v6-tiny")
        self.combo_model.addItem(T("ui.model_small", "标准（v6-small）"), "v6-small")
        self.combo_model.addItem(T("ui.model_medium", "高精度（v6-medium）"), "v6-medium")
        cur_model = self._cfg.get("software", "ocr_model", "v6-tiny")
        i_m = self.combo_model.findData(cur_model)
        self.combo_model.setCurrentIndex(i_m if i_m >= 0 else 0)
        form.addRow(T("ui.model_set", "OCR 模型"), self.combo_model)
        self._attach_hint(
            form, self.combo_model,
            T("ui.model_set_hint",
              "轻量=识别最快；标准=速度与精度均衡；高精度=识别更准但更慢。\n"
              "切到高精度模型首次加载约 1–2 秒（保存后立即生效）。"))

        # ---- OCR 引擎常驻（[software] ocr_keep_alive，默认勾选）----
        self.chk_ocr_keep_alive = QCheckBox(T("ui.ocr_keep_alive", "OCR 引擎常驻"))
        self.chk_ocr_keep_alive.setChecked(
            self._cfg.getbool("software", "ocr_keep_alive", True))
        self.chk_ocr_keep_alive.setToolTip(T(
            "ui.ocr_keep_alive_hint",
            "勾选：启动时加载 OCR 模型（识别更快，占用内存）；\n"
            "不勾选：启动不加载模型，首次识别时才加载（省内存、首次稍慢）。"))
        form.addRow("", self.chk_ocr_keep_alive)

        # ---- 截图后自动 OCR（[software] ocr_auto，默认勾选）----
        self.chk_ocr_auto = QCheckBox(T("ocr.auto", "截图后自动 OCR"))
        self.chk_ocr_auto.setChecked(
            self._cfg.getbool("software", "ocr_auto", True))
        self.chk_ocr_auto.setToolTip(
            T("ui.ocr_auto_hint",
              "勾选：截图后自动识别文字并显示文字框；\n"
              "不勾选：截图仅贴图，点「文字框」或「表格」按钮时才识别。"))
        form.addRow("", self.chk_ocr_auto)

        # ---- 拖入图片后自动 OCR（[software] drop_ocr_auto，默认勾选）----
        # 独立于「截图后自动 OCR」：拖进历史面板的图片是否自动识别
        self.chk_drop_ocr = QCheckBox(
            T("ocr.drop_auto", "拖入图片后自动 OCR"))
        self.chk_drop_ocr.setChecked(
            self._cfg.getbool("software", "drop_ocr_auto", True))
        self.chk_drop_ocr.setToolTip(
            T("ocr.drop_auto_hint",
              "勾选：把图片拖进历史面板后自动识别文字（原行为）；\n"
              "不勾选：仅入库为「待识别」，需要时在历史面板点开该图，"
              "再点「文字框」识别。"))
        form.addRow("", self.chk_drop_ocr)

        # ---- 二维码识别（[ui] enable_qr，默认关）----
        self.chk_qr = QCheckBox(T("ui.enable_qr", "二维码识别"))
        self.chk_qr.setChecked(
            self._cfg.getbool("software", "enable_qr", False))
        self.chk_qr.setToolTip(T(
            "ui.enable_qr_hint",
            "勾选：截图 OCR 时顺带识别二维码/条码（需 zxing-cpp）。\n"
            "当前贴图窗口用工具栏 QR 按钮 / Q 键即时切换；此处为默认值，保存后对后续截图生效。"))
        form.addRow("", self.chk_qr)

        # ---- 截图无操作也保存（[software] save_no_op_close，默认关）----
        self.chk_save_no_op = QCheckBox(T("ui.save_no_op", "截图无操作也保存"))
        self.chk_save_no_op.setChecked(
            self._cfg.getbool("software", "save_no_op_close", False))
        self.chk_save_no_op.setToolTip(T("ui.save_no_op_hint",
            "勾选：截图窗口未做任何操作（不复制 / 不标注 / 不识别）就关闭时，仍保存截图到历史记录；\n"
            "不勾选：未做任何操作就关闭的截图不保存。"))
        form.addRow("", self.chk_save_no_op)

        # 开机自启动（M2-8：注册表 HKCU Run）
        from core_engine.autostart import is_enabled
        self.chk_autostart = QCheckBox(T("autostart", "开机自动启动 ClickOCR"))
        self.chk_autostart.setChecked(is_enabled())
        form.addRow("", self.chk_autostart)

        # ---- 历史管理：打开历史文件夹（左） + 清空全部历史（右，危险操作）----
        self.btn_open_history = QPushButton(
            T("settings.open_history", "打开历史文件夹"))
        self.btn_open_history.clicked.connect(self._open_history_folder)
        self._attach_hint(
            form, self.btn_open_history,
            T("settings.open_history_hint",
              "在系统文件管理器中打开历史记录图片的保存目录。"))
        self.btn_clear_history = QPushButton(
            T("settings.clear_all_hist", "清空全部历史"))
        self.btn_clear_history.clicked.connect(self._clear_all_history)
        self._attach_hint(
            form, self.btn_clear_history,
            T("settings.clear_all_hist_hint",
              "清空全部历史记录（收藏的图片会保留）；不可恢复，请谨慎操作。"))
        _hist_row = QHBoxLayout()
        _hist_row.setSpacing(8)
        _hist_row.addStretch(1)                 # 按钮组靠右，与底部保存/关闭区呼应
        _hist_row.addWidget(self.btn_open_history)
        _hist_row.addWidget(self.btn_clear_history)
        form.addRow("", _hist_row)
        # 滚动区接管内容：setWidget 会强制开启 autoFill，需在其后关闭
        scroll.setWidget(inner)
        inner.setAutoFillBackground(False)
        return page   # 必须返回页面 widget：返回 None 会导致 tab 页丢失、
                      # 页内控件（spin/combo）失去 Qt 持有后被 PySide6 GC 销毁

    def _refresh_accel_status(self):
        """显示当前所选档位的运行时状态。"""
        from core_engine.ort_runtime import runtime_status
        dev = self.combo_accel.currentData()
        self.lbl_accel_status.setText(runtime_status(dev))

    def _cfg_get_probe(self, ep: str = "dml") -> dict:
        """读配置里最近一次 GPU 检测结果（JSON）；缺失/损坏返回空 dict。"""
        key = "cuda_probe" if ep == "cuda" else "dml_probe"
        try:
            raw = self._cfg.get("software", key, "")
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _run_gpu_probe(self):
        """检测按钮：探测所选 EP（DirectML / CUDA）各设备实际推理倍率，
        记录到配置（[software] dml_probe / cuda_probe）并刷新显示。

        用当前模型档位的 det 模型计时（640x640）；探测约 1~4 秒同步执行。
        """
        dev = self.combo_accel.currentData()
        if dev == "cpu":
            ThemeDialog.info(None, T("gpu.accel", "GPU 加速"),
                             T("ui.probe_cpu", "CPU 模式无需 GPU 检测。"))
            self._refresh_accel_status()
            return
        if dev == "dml":
            from ocr.rapid_engine import probe_dml_devices, _DIR_V6
            probe_fn = probe_dml_devices
        elif dev == "cuda":
            from ocr.rapid_engine import probe_cuda_devices, _DIR_V6
            probe_fn = probe_cuda_devices
        else:
            ThemeDialog.info(None, T("gpu.accel", "GPU 加速"),
                             T("ui.probe_unsupported",
                               "{} 暂不支持性能检测。").format(dev))
            return
        det_path = None
        try:
            _names = {"v6-tiny": "PP-OCRv6_det_tiny.onnx",
                      "v6-small": "PP-OCRv6_det_small.onnx",
                      "v6-medium": "PP-OCRv6_det_medium.onnx"}
            _n = _names.get(self.combo_model.currentData())
            if _n:
                det_path = _DIR_V6 / _n
        except Exception:  # noqa: BLE001
            det_path = None
        self.btn_accel_install.setEnabled(False)
        self.btn_accel_install.setText(T("btn.detect_running", "检测中…"))
        self.lbl_accel_status.setText(T("ui.probe_running", "正在探测设备…"))
        QApplication.processEvents()
        try:
            devices = probe_fn(det_path)
        except Exception as e:  # noqa: BLE001
            log.exception("GPU 检测失败")
            devices = []
            self.lbl_accel_status.setText(
                T("ui.probe_fail", "检测失败：{}").format(e))
        finally:
            self.btn_accel_install.setEnabled(True)
            self.btn_accel_install.setText(T("btn.detect", "检测"))
        if not devices:
            self.lbl_accel_status.setText(T(
                "ui.probe_no_dev", "未检测到可用的 {} 设备（当前环境无支持）。"
                ).format(dev.upper()))
            return
        # 记录到配置（下次启动直接显示倍率）
        _key = "cuda_probe" if dev == "cuda" else "dml_probe"
        try:
            self._cfg.cp.set("software", _key, json.dumps(
                {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "det": self.combo_model.currentData(),
                 "devices": devices}, ensure_ascii=False))
            self._cfg.save()
        except Exception:  # noqa: BLE001
            pass
        # 刷新下拉框：当前 EP 项附加最快倍率
        _ok = [d for d in devices if d.get("ok")]
        for _i in range(self.combo_accel.count()):
            if self.combo_accel.itemData(_i) == dev:
                _base_text = {
                    "dml": "DirectML（AMD / Intel / 通用）",
                    "cuda": "CUDA（NVIDIA 显卡）",
                }.get(dev, dev)
                _text = _base_text
                if _ok:
                    _best = max(_ok, key=lambda d: d.get("speedup", 0))
                    _text += f"（≈{_best['speedup']:.1f}×）"
                self.combo_accel.setItemText(_i, _text)
                break
        # 状态显示各设备倍率
        lines = [f"[{time.strftime('%H:%M:%S')}] CPU 基准 "
                 f"{_ok[0]['cpu_ms']:.0f}ms/张" if _ok else "无可用设备"]
        for d in devices:
            if d.get("ok"):
                lines.append(f"设备{d['id']}：≈{d['speedup']:.2f}× "
                             f"({d['dml_ms']:.0f}ms vs CPU {d['cpu_ms']:.0f}ms)")
            else:
                lines.append(f"设备{d['id']}：不可用"
                             + (f"（{d['err']}）" if d.get("err") else ""))
        self.lbl_accel_status.setText("\n".join(lines))

    def _open_history_folder(self):
        """打开历史记录图片的保存目录（系统文件管理器，跨平台走 QDesktopServices）。"""
        d = None
        if self._history is not None:
            d = getattr(self._history, "screenshots_dir", None)
        if not d:
            self._notify(T("ui.msg_history_none", "历史记录不可用"))
            return
        try:
            p = Path(d)
            p.mkdir(parents=True, exist_ok=True)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))):
                self._notify(T("settings.open_history_fail",
                               "打开历史文件夹失败：{}").format(p))
        except Exception as e:      # noqa: BLE001
            log.exception("打开历史文件夹失败: %s", e)
            self._notify(T("settings.open_history_fail",
                           "打开历史文件夹失败：{}").format(e))

    def _clear_all_history(self):
        """清空全部历史（原历史面板按钮移入）：确认后只清非收藏，收藏保留；
        完成后经 App 回调刷新已打开的历史面板。"""
        if self._history is None:
            self._notify(T("ui.msg_history_none", "历史记录不可用"))
            return
        if not ThemeDialog.question(
                None,
                T("settings.title", "ClickOCR 设置"),
                T("settings.clear_all_hist_confirm",
                  "确定清空全部历史记录？\n（收藏的图片会保留，此操作不可恢复）")):
            return
        try:
            self._history.clear(keep_favorites=True)
        except Exception as e:      # noqa: BLE001
            log.exception("清空历史失败: %s", e)
            self._notify(T("ui.msg_save_fail", "清空失败：{}").format(e))
            return
        if self._on_clear_history is not None:
            try:
                self._on_clear_history()
            except Exception:       # noqa: BLE001
                log.exception("清空后刷新历史面板失败")
        self._notify(T("ui.msg_cleared", "已清空全部历史（收藏保留）"))

    def _on_save_fmt_changed(self):
        """质量滑块仅对有损格式（JPG/WebP）生效。"""
        fmt = self.combo_save_fmt.currentData()
        lossy = fmt in ("jpg", "webp")
        self.slider_quality.setEnabled(lossy)
        self.lbl_quality.setEnabled(lossy)

    def _check_install_accel(self):
        """检测并（开发模式）自动安装所选 GPU 运行时；下载前弹确认。"""
        from core_engine.ort_runtime import (device_ready, install_runtime,
                                             install_command, runtime_status,
                                             DEVICE_PKG, PKG_SIZE)
        dev = self.combo_accel.currentData()
        if dev not in DEVICE_PKG:
            ThemeDialog.info(None, T("gpu.accel", "GPU 加速"), T("ui.gpu_cpu_ok", "CPU 模式无需安装 GPU 运行时。"))
            return
        pkg, ep = DEVICE_PKG[dev]
        if device_ready(dev):
            ThemeDialog.info(None, T("gpu.accel", "GPU 加速"), T("ui.gpu_ready", "{} 已就绪，无需安装。").format(ep))
            self._refresh_accel_status()
            return
        import sys as _sys
        if getattr(_sys, "frozen", False):
            # 打包版已捆绑运行时：直接显示实际状态（已就绪 / 设备不支持自动回退 CPU）
            ThemeDialog.info(None, T("gpu.accel", "GPU 加速"), runtime_status(dev))
            self._refresh_accel_status()
            return
        if not ThemeDialog.question(None, T("gpu.accel", "GPU 加速"),
                                     T("ui.gpu_need_install",
                                       "需要下载安装 {}（{}，需联网）\n"
                                       "才能使用 {}。安装后请重启程序生效。\n\n是否继续安装？")
                                     .format(pkg, PKG_SIZE[pkg], ep)):
            return
        self.btn_accel_install.setEnabled(False)
        self.btn_accel_install.setText(T("ui.gpu_installing", "安装中…"))
        try:
            ok, msg = install_runtime(dev)
            if ok:
                ThemeDialog.info(None, T("gpu.accel", "GPU 加速"), msg)
            else:
                ThemeDialog.warning(None, T("gpu.accel", "GPU 加速"), T("ui.gpu_install_fail", "安装失败：\n{}").format(msg))
        finally:
            self.btn_accel_install.setEnabled(True)
            self.btn_accel_install.setText(T("btn.detect", "检测"))
            self._refresh_accel_status()

    def _pick_shot_dir(self):
        cur = self.edit_shot_dir.text().strip()
        d = QFileDialog.getExistingDirectory(
            self, T("ui.pick_shot_dir_title", "选择历史图片保存目录"), cur or str(self._cfg.app_root))
        if d:
            self.edit_shot_dir.setText(d)

    # ---- Tab 2：UI 设置 ----
    def _build_ui_tab(self) -> QWidget:
        page, scroll, inner = self._scroll_page()
        w = inner
        form = self._form(w)

        # UI 页 6 个数值栏统一宽度（px）
        _W5 = 75

        # 界面主题
        self.combo_theme = QComboBox()
        self.combo_theme.addItem(T("ui.theme_system", "跟随系统"), "system")
        self.combo_theme.addItem(T("ui.theme_light", "亮色"), "light")
        self.combo_theme.addItem(T("ui.theme_dark", "暗色"), "dark")
        cur = self._cfg.get("ui", "theme", "system").strip().lower()
        idx = self.combo_theme.findData(cur)
        self.combo_theme.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow(T("ui.base.theme", "界面主题"), self.combo_theme)
        self._attach_hint(
            form, self.combo_theme,
            T("ui.theme_hint", "暗色 = 无边框半透明深色卡片；跟随系统 = 随 Windows 深浅色自动切换。"))

        # 历史面板：宽度 / 显示卡片数（高度）
        self.spin_hist_width = ThemeSpinBox(
            400, 1600, self._cfg.getint("ui", "history_panel_width", 600),
            width=_W5)
        self.spin_hist_cards = ThemeSpinBox(
            1, 30, self._cfg.getint("ui", "history_visible_cards", 5),
            width=_W5)
        hist_row = QHBoxLayout()
        hist_row.setSpacing(4)
        lbl_w = QLabel(T("ui.hist_w", "宽"))
        lbl_w.setMinimumWidth(30)
        hist_row.addWidget(lbl_w)
        hist_row.addWidget(self.spin_hist_width)
        hist_row.addWidget(QLabel(T("ui.hist_cards", "卡片")))
        hist_row.addWidget(self.spin_hist_cards)
        hist_row.addStretch(1)
        form.addRow(T("ui.hist_panel", "历史面板"), hist_row)
        self._attach_hint(
            form, hist_row,
            T("ui.hist_panel_hint",
              "历史记录窗口宽度（400~1600）与显示卡片数（决定面板高度，"
              "每张卡片约 84px）；保存后下次打开生效。"))

        # 截图框选边框
        self.btn_sel_color = _ColorButton(
            self._cfg.getcolor("ui", "border_color", (100, 190, 255)))
        self.spin_sel_width = ThemeSpinBox(
            1, 10, self._cfg.getint("ui", "border_width", 2),
            width=_W5)
        border_row = QHBoxLayout()
        border_row.setSpacing(4)
        border_row.addWidget(self.btn_sel_color)
        border_row.addWidget(self.spin_sel_width)
        border_row.addStretch(1)
        form.addRow(T("ui.sel_border", "截图边框"), border_row)

        # 截图取色器：放大镜像素框大小 / 放大倍率（最大 15×15）
        self.spin_picker_size = ThemeSpinBox(
            3, 15, self._cfg.getint("ui", "picker_size", 7),
            width=_W5)
        self.spin_picker_zoom = ThemeSpinBox(
            2, 20, self._cfg.getint("ui", "picker_zoom", 9),
            width=_W5)
        picker_row = QHBoxLayout()
        picker_row.setSpacing(4)
        lbl_px = QLabel(T("ui.picker_px", "像素"))
        lbl_px.setMinimumWidth(30)
        picker_row.addWidget(lbl_px)
        picker_row.addWidget(self.spin_picker_size)
        picker_row.addWidget(QLabel(T("ui.picker_zoom", "倍率")))
        picker_row.addWidget(self.spin_picker_zoom)
        picker_row.addStretch(1)
        form.addRow(T("ui.picker", "取色器"), picker_row)
        self._attach_hint(
            form, picker_row,
            T("ui.picker_hint", "像素框 = 放大镜采样的像素边长（3~15，最大 15×15）；放大倍率 = 每像素放大倍数。"))

        # 贴图窗口边框（overlay）
        self.btn_overlay_color = _ColorButton(
            self._cfg.getcolor("ui", "overlay_border_color", (100, 190, 255)))
        self.spin_overlay_width = ThemeSpinBox(
            0, 10, self._cfg.getint("ui", "overlay_border_width", 1),
            width=_W5)
        overlay_row = QHBoxLayout()
        overlay_row.setSpacing(4)
        overlay_row.addWidget(self.btn_overlay_color)
        overlay_row.addWidget(self.spin_overlay_width)
        overlay_row.addStretch(1)
        form.addRow(T("ui.overlay_border", "窗口边框"), overlay_row)
        self._attach_hint(
            form, overlay_row,
            T("ui.overlay_border_hint", "宽度 0 = 不显示边框（[ui] 配置段）。"))

        # 二维码识别边框（[ui] qr_border_color，默认紫）
        self.btn_qr_color = _ColorButton(
            self._cfg.getcolor("ui", "qr_border_color", (156, 39, 176)))
        qr_row = QHBoxLayout()
        qr_row.addWidget(self.btn_qr_color)
        qr_row.addStretch(1)
        form.addRow(T("ui.qr_border", "二维码边框"), qr_row)

        # 表格模式网格边框（[ui] table_border_color，默认绿）
        self.btn_table_color = _ColorButton(
            self._cfg.getcolor("ui", "table_border_color", (90, 220, 120)))
        table_row = QHBoxLayout()
        table_row.addWidget(self.btn_table_color)
        table_row.addStretch(1)
        form.addRow(T("ui.table_border", "表格边框"), table_row)

        # 统一工具栏（操作按钮 + 标注工具区）全局开关
        self.chk_toolbar = QCheckBox(T("ui.components.toolbar", "统一工具栏"))
        self.chk_toolbar.setChecked(
            self._cfg.getbool("ui", "toolbar_enabled", True))
        self.chk_toolbar.setToolTip(
            T("ui.toolbar_hint", "关闭后图片窗口不显示操作按钮/标注工具（仅保留状态栏、关闭按钮与快捷键）"))
        form.addRow("", self.chk_toolbar)

        # 纯信息状态栏全局开关
        self.chk_strip = QCheckBox(T("ui.strip", "状态栏（纯信息）"))
        self.chk_strip.setChecked(
            self._cfg.getbool("ui", "strip_enabled", True))
        self.chk_strip.setToolTip(
            T("ui.strip_hint", "状态栏只显示状态文字与选择预览；关闭后不显示（Esc 关闭后也可在此重新开启）"))
        form.addRow("", self.chk_strip)

        # 识别文字蓝色框默认显示
        self.chk_text_boxes = QCheckBox(T("ui.text_boxes", "图片文字边框"))
        self.chk_text_boxes.setChecked(
            self._cfg.getbool("ui", "show_text_boxes", True))
        form.addRow("", self.chk_text_boxes)

        # 标注栏默认开启
        self.chk_anno_default = QCheckBox(T("ui.anno_bar", "截图标注栏"))
        self.chk_anno_default.setChecked(
            self._cfg.getbool("ui", "anno_toolbar_default", False))
        form.addRow("", self.chk_anno_default)

        # 标注工具参数永久化记录（颜色/线宽/字号/加粗/斜体/马赛克模式）
        self.chk_anno_persist = QCheckBox(T("ui.anno_persist", "永久化记录标注工具参数"))
        self.chk_anno_persist.setChecked(
            self._cfg.getbool("ui", "persist_params", True))
        self.chk_anno_persist.setToolTip(
            T("ui.anno_persist_hint",
              "开启：颜色/线宽/字号/加粗/斜体/马赛克模式写入配置，重启软件后保持；"
              "关闭：仅本次运行内记住"))
        form.addRow("", self.chk_anno_persist)
        # 滚动区接管内容：setWidget 会强制开启 autoFill，需在其后关闭
        scroll.setWidget(inner)
        inner.setAutoFillBackground(False)
        return page

    # ---- Tab 3：快捷键设置 ----
    def _build_hotkey_tab(self) -> QWidget:
        w = QWidget()
        # 快捷键行较多：内容包进滚动区（窗口固定 600×720，超高可滚动）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        # QSS 的 background:transparent 只作用于 QScrollArea 本体，不传播到
        # viewport（其默认 autoFillBackground 会刷系统浅色底，暗色主题下变白）；
        # 关闭 viewport 自动填充 → 透出卡片暗底
        scroll.viewport().setAutoFillBackground(False)
        inner = QWidget()
        form = self._form(inner)

        # 统一快捷键控件：每行一个 _ShortcutRowEdit（4 修饰键 + 拾取框 + 启用开关）。
        # 历史上每个功能都手写「修饰键勾选 + 拾取框 + 启用」的重复代码，现收敛到
        # _row() 一个声明点；_save 统一遍历 _hk_rows 读取 combo() 即可。
        self._hk_rows = []   # (row, label, sec, key, parser, invalid, apply)

        def add_hk_group(text_key: str, text: str):
            g = QLabel(T(text_key, text))
            g.setProperty("kind", "group")
            # 整行独占（无空 label）：分组标题贴最左，不被 label 列推挤
            form.addRow(g)

        def _row(label_key: str, label: str, sec: str, key: str, default: str,
                 parser=None, invalid: str | None = None, apply=None):
            """声明一个快捷键行并登记到 _hk_rows（_save 统一读取 combo()）。

            parser：本地快捷键用 _parse_qt_combo 校验；再次验证格式。
            apply：全局热键行用（冲突检测 + 注册/注销到 HotkeyHost），返回 msgs 列表。
            返回已构造的 _ShortcutRowEdit（可再拿其子控件做旧属性兼容）。
            """
            row = _ShortcutRowEdit()
            row.set_combo(self._cfg.get(sec, key, default))
            form.addRow(T(label_key, label), row)
            self._hk_rows.append((row, T(label_key, label), sec, key,
                                  parser, invalid, apply))
            return row

        def add_hk_row(label_key: str, label: str, sec: str, key: str,
                       default: str):
            """局部快捷键行（截图选择/贴图窗口/标注）：Qt 解析校验后写 config。"""
            return _row(label_key, label, sec, key, default,
                        parser=_parse_qt_combo,
                        invalid=f"{label} 快捷键格式无效")

        add_hk_group("hk.group_global", "全局热键")

        # 全局截图热键（主热键）：冲突检测 + 注册到 HotkeyHost
        def apply_shot(combo):
            _msgs = []
            if self._hotkey is not None:
                if combo == "off":
                    self._hotkey.unregister_primary()
                    self._cfg.cp.set("hotkey_global", "screenshot_combo", "off")
                    _msgs.append("截图热键：off")
                elif combo != self._hotkey.active_combo:
                    if self._hotkey.check_conflict(combo):
                        _msgs.append(f"热键 {combo.upper()} 已被其它程序占用")
                    elif self._hotkey.set_combo(combo):
                        self._cfg.cp.set(
                            "hotkey_global", "screenshot_combo", combo)
                        _msgs.append(f"热键已切换：{combo.upper()}")
                    else:
                        _msgs.append("热键切换失败（已回滚）")
                else:
                    _msgs.append(f"热键不变：{self._hotkey.active_combo.upper()}")
            elif combo != "off":
                self._cfg.cp.set("hotkey_global", "screenshot_combo", combo)
                _msgs.append(f"截图热键：{combo}")
            return _msgs

        self._row_shot = _row("hk.screenshot", "截图", "hotkey_global",
                              "screenshot_combo", "ctrl+shift+r",
                              apply=apply_shot)
        self.cb_ctrl, self.cb_alt, self.cb_shift, self.cb_win = \
            self._row_shot._mods            # 兼容旧属性名（脚本/外部引用）
        self.key_capture = self._row_shot._cap
        self.chk_enable = self._row_shot._chk

        # 全屏截图热键（独立组合：直接截整屏不框选，默认 off）
        apply_full = self._secondary_apply(
            3, "hotkey_global", "fullscreen_combo", "全屏截图")
        self._row_full = _row("hk.fullscreen", "全屏截图", "hotkey_global",
                              "fullscreen_combo", "off", apply=apply_full)
        self.cb_full_ctrl, self.cb_full_alt, self.cb_full_shift, self.cb_full_win = \
            self._row_full._mods
        self.key_full_capture = self._row_full._cap
        self.chk_full_enable = self._row_full._chk

        # 历史记录快捷键（全局热键，默认 Ctrl+`）
        apply_hist = self._secondary_apply(2, "hotkey_global", "history",
                                           "历史记录", off_val="off", hint="ctrl+`")
        self._row_hist = _row("hk.history", "历史记录", "hotkey_global",
                              "history", "ctrl+`", apply=apply_hist)
        self.cb_hist_ctrl, self.cb_hist_alt, self.cb_hist_shift, self.cb_hist_win = \
            self._row_hist._mods
        self.key_hist_capture = self._row_hist._cap
        self.chk_hist_enable = self._row_hist._chk







        # ---- 截图选择（截图遮罩中）----
        add_hk_group("hk.group_select", "截图选择")
        add_hk_row("hk.full_sel", "全屏框选", "hotkey_select", "full_sel", "a")
        add_hk_row("hk.scroll_sel", "滚动截图（截长图）", "hotkey_select",
                   "scroll_sel", "r")

        # 取色复制色值快捷键（截图时光标取色器，默认 C）
        self._row_pick = add_hk_row("hk.pick_color", "复制色值", "hotkey_select",
                                    "color_pick", "c")
        self._attach_hint(form, self._row_pick, T(
            "hk.pick_hint",
            "截图时光标带取色器；按此键复制当前像素色值（如 #FF8800）。"))
        self.cb_pick_ctrl, self.cb_pick_alt, self.cb_pick_shift, self.cb_pick_win = \
            self._row_pick._mods
        self.pick_capture = self._row_pick._cap
        self.chk_pick_enable = self._row_pick._chk
        # 切换色值格式快捷键（截图取色器按此键循环 RGB→HSV→HSL，默认 H）
        self._row_fmt = add_hk_row("hk.fmt_color", "切换色值格式", "hotkey_select",
                                   "color_fmt", "h")
        self._attach_hint(form, self._row_fmt, T(
            "hk.fmt_hint",
            "截图取色时按此键循环切换色值显示：RGB → HSV → HSL。"))
        self.cb_fmt_ctrl, self.cb_fmt_alt, self.cb_fmt_shift, self.cb_fmt_win = \
            self._row_fmt._mods
        self.fmt_capture = self._row_fmt._cap
        self.chk_fmt_enable = self._row_fmt._chk
        # ---- 贴图窗口（截图结果 / 历史图）----
        add_hk_group("hk.group_overlay", "贴图窗口")
        add_hk_row("hk.reocr", "重新 OCR", "hotkey_overlay", "reocr", "ctrl+r")
        add_hk_row("hk.table_ocr", "表格识别", "hotkey_overlay",
                   "table_ocr", "ctrl+shift+t")
        add_hk_row("hk.toggle_anno", "标注模式", "hotkey_overlay",
                   "toggle_anno", "ctrl+shift+d")
        add_hk_row("hk.nav", "导航图", "hotkey_overlay", "nav", "m")
        add_hk_row("hk.ratio", "等比例调节窗口", "hotkey_overlay",
                   "ratio", "b")
        add_hk_row("hk.toggle_strip", "状态栏开关", "hotkey_overlay",
                   "toggle_strip", "s")
        add_hk_row("hk.toggle_qr", "二维码开关", "hotkey_overlay",
                   "toggle_qr", "q")
        add_hk_row("hk.toggle_boxes", "文字框开关", "hotkey_overlay",
                   "toggle_boxes", "w")
        add_hk_row("hk.toggle_table", "表格", "hotkey_overlay",
                   "toggle_table", "e")
        add_hk_row("hk.fav", "收藏", "hotkey_overlay", "fav", "f")

        # 复制快捷键（默认 Shift+C）
        self._row_copy = add_hk_row("hk.copy", "全复制到剪贴板", "hotkey_overlay",
                                    "copy_shortcut", "shift+c")
        self.cb_copy_ctrl, self.cb_copy_alt, self.cb_copy_shift, self.cb_copy_win = \
            self._row_copy._mods
        self.copy_capture = self._row_copy._cap
        self.chk_copy_enable = self._row_copy._chk
        add_hk_row("hk.select_all", "全选文字", "hotkey_overlay",
                   "select_all", "ctrl+a")
        add_hk_row("hk.copy_selected", "复制选中文字", "hotkey_overlay",
                   "copy_selected", "ctrl+c")
        add_hk_row("hk.copy_image", "复制图片", "hotkey_overlay",
                   "copy_image", "ctrl+shift+c")
        add_hk_row("hk.copy_raw_image", "复制原图", "hotkey_overlay",
                   "copy_raw_image", "ctrl+alt+c")
        add_hk_row("hk.through", "点击穿透（全局）", "hotkey_global",
                   "through", "alt+f")

        # 图片缩放修饰键（[hotkey_overlay] zoom_modifier，非按键行，用下拉）
        self.combo_zoom_mod = QComboBox()
        for label, data in ((T("hk.zoom_ctrl", "Ctrl + 滚轮"), "ctrl"),
                            (T("hk.zoom_alt", "Alt + 滚轮"), "alt"),
                            (T("hk.zoom_shift", "Shift + 滚轮"), "shift"),
                            (T("hk.zoom_off", "关"), "off")):
            self.combo_zoom_mod.addItem(label, data)
        cur_zm = self._cfg.get("hotkey_overlay", "zoom_modifier", "ctrl").lower()
        if cur_zm not in ("ctrl", "alt", "shift", "off"):
            cur_zm = "ctrl"
        self.combo_zoom_mod.setCurrentIndex(
            self.combo_zoom_mod.findData(cur_zm))
        form.addRow(T("hk.zoom_mod", "图片缩放"), self.combo_zoom_mod)
        self._attach_hint(
            form, self.combo_zoom_mod,
            T("hk.zoom_mod_hint",
              "贴图窗口按住修饰键 + 滚轮 = 图片在窗口内缩放（窗口尺寸不变；"
              "图片超出窗口时自动出现滚动条，也可中键拖动平移查看）。"))
        # 窗口几何缩放修饰键（[hotkey_overlay] geom_zoom_modifier，默认 Shift）
        self.combo_geom_zoom_mod = QComboBox()
        for label, data in ((T("hk.zoom_ctrl", "Ctrl + 滚轮"), "ctrl"),
                            (T("hk.zoom_alt", "Alt + 滚轮"), "alt"),
                            (T("hk.zoom_shift", "Shift + 滚轮"), "shift"),
                            (T("hk.zoom_off", "关"), "off")):
            self.combo_geom_zoom_mod.addItem(label, data)
        cur_gz = self._cfg.get(
            "hotkey_overlay", "geom_zoom_modifier", "shift").lower()
        if cur_gz not in ("ctrl", "alt", "shift", "off"):
            cur_gz = "shift"
        self.combo_geom_zoom_mod.setCurrentIndex(
            self.combo_geom_zoom_mod.findData(cur_gz))
        form.addRow(T("hk.geom_zoom_mod", "窗口缩放"), self.combo_geom_zoom_mod)
        self._attach_hint(
            form, self.combo_geom_zoom_mod,
            T("hk.geom_zoom_mod_hint",
              "按住修饰键 + 滚轮 = 整个贴图窗口缩放（图片随窗口一起放大缩小）。"))

        add_hk_group("hk.group_anno", "标注")
        add_hk_row("hk.undo_anno", "撤销", "hotkey_anno", "undo", "ctrl+z")
        add_hk_row("hk.redo_anno", "重做", "hotkey_anno",
                   "redo", "ctrl+shift+z")

        # 快捷键一览按钮已移到底部按钮行（操作一览，最左）——滚动区直通到底
        scroll.setWidget(inner)
        # QScrollArea::setWidget 会强制开启内容控件 autoFillBackground（防滚动
        # 残影），其调色板是系统浅色 → 暗色主题下整片白底；此处关闭自动填充，
        # 让内容透出卡片暗底（viewport 已在上面关闭 autoFill）
        inner.setAutoFillBackground(False)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scroll)
        return w

    def _open_shortcut_panel(self):
        """UI 入口：弹出快捷键速查面板（不占用全局快捷键），读实际配置键位。"""
        from ui.components.shortcut_panel import ShortcutPanel
        self._shortcut_panel = ShortcutPanel(cfg=self._cfg)
        self._shortcut_panel.show()
        self._shortcut_panel.raise_()

    def _on_tab_changed(self, idx: int):
        """「操作一览」按钮仅「快捷键设置」tab（index 2）显示。"""
        self.btn_shortcut.setVisible(idx == 2)

    # ---- 内部 ----
    @staticmethod
    def _key_from_combo(combo: str, default: str = "a") -> str:
        """从组合字符串提取末尾键位（如 "ctrl+shift+r" -> "r"；"shift+f1" -> "f1"；
        off -> default）。token 取最后一个非修饰项。"""
        if not combo or combo.lower() in ("off", "none", "false"):
            return default
        tok = default
        for p in combo.lower().replace("+", " ").split():
            if p not in ("ctrl", "alt", "shift", "win"):
                tok = p
        return tok

    # ---- 通用：修饰键 + 键位拾取框 + 启用勾选（四行快捷键统一样式）----
    @staticmethod
    def _load_key_row(cbs, key_widget, chk_enable, combo: str):
        """从组合字符串加载修饰键 checkbox / 键位拾取框 / 启用勾选。"""
        enabled = bool(combo) and combo.lower() not in ("off", "none", "false")
        chk_enable.setChecked(enabled)
        low = combo.lower().replace("+", " ")
        for cb, name in ((cbs[0], "ctrl"), (cbs[1], "alt"),
                         (cbs[2], "shift"), (cbs[3], "win")):
            cb.setChecked(enabled and name in low)
        if enabled:
            key_widget._show_key(SettingsPanel._key_from_combo(combo, "a"))
        else:
            key_widget._show_key(key_widget.key())

    @staticmethod
    def _current_key_row(cbs, key_widget, chk_enable) -> str:
        """从控件生成组合字符串；未启用返回 "off"。（兼容旧调用，新代码用 _ShortcutRowEdit.combo()）"""
        if not chk_enable.isChecked():
            return "off"
        parts = []
        for cb, name in ((cbs[0], "ctrl"), (cbs[1], "alt"),
                         (cbs[2], "shift"), (cbs[3], "win")):
            if cb.isChecked():
                parts.append(name)
        parts.append(key_widget.key())
        return "+".join(parts)

    def _secondary_apply(self, hid: int, sec: str, key: str, label: str,
                         off_val: str = "off", hint: str = "ctrl+shift+a"):
        """构造全局附加热键（历史记录 / 全屏截图）的 apply 回调。

        保存时逐行读取 combo() 后调用；只负责「格式校验 + 注册/注销到 HotkeyHost
        + 写 config + 生成提示消息」，把原先散落写在 _save 里的重复代码收敛一处。
        """
        from hotkey import _parse_combo as _parse_global

        def apply(combo: str):
            msgs = []
            if combo in ("off", "none", "false"):
                if self._hotkey is not None:
                    self._hotkey.unregister_secondary(hid)
                self._cfg.cp.set(sec, key, off_val)
                msgs.append(f"{label}快捷键：off")
            else:
                _m, _v = _parse_global(combo)
                if _v is None:
                    msgs.append(f"{label}快捷键格式无效（示例：{hint}）")
                else:
                    if self._hotkey is not None:
                        if not self._hotkey.set_secondary_combo(hid, combo):
                            msgs.append(f"{label}快捷键注册失败（被占用，已回滚）")
                    self._cfg.cp.set(sec, key, combo)
                    msgs.append(f"{label}快捷键：{combo}")
            return msgs

        return apply

    def _save(self):
        msgs = []

        # ---- 主题（即时生效）----
        theme = self.combo_theme.currentData()
        self._cfg.cp.set("ui", "theme", theme)
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, resolve_theme(self._cfg))
        msgs.append(f"主题：{theme}")

        # ---- 历史保留条数（不设上限）----
        cap = self.spin_capacity.value()
        self._cfg.cp.set("software", "history_max_count", str(cap))
        if self._history is not None:
            self._history.max_records = cap
        msgs.append(f"历史保留条数：{cap}")

        # ---- 历史图片位置（即时生效）----
        new_dir = self.edit_shot_dir.text().strip()
        self._cfg.cp.set("software", "screenshots_dir", new_dir)
        if self._history is not None:
            target = Path(new_dir) if new_dir else \
                (self._cfg.app_root / "screenshots")
            target = target if target.is_absolute() \
                else (self._cfg.app_root / target)
            self._history.screenshots_dir = target
        msgs.append(f"历史图片位置：{new_dir or '默认 screenshots/'}")

        # ---- 复制模式 ----
        self._cfg.cp.set("software", "selection_mode",
                         self.combo_selection.currentData())
        msgs.append(f"复制模式：{self.combo_selection.currentData()}")

        # ---- 图片保存格式 / 质量 / 文件名规则（即时生效）----
        fmt = self.combo_save_fmt.currentData()
        self._cfg.cp.set("software", "save_format", fmt)
        self._cfg.cp.set("software", "save_quality",
                         str(self.slider_quality.value()))
        pattern = self.edit_name_pattern.text().strip()
        self._cfg.cp.set("software", "name_pattern",
                         pattern if pattern else "{datetime}_{ms}")
        msgs.append(f"保存格式：{fmt.upper()} · 质量："
                    f"{self.slider_quality.value()} · 文件名规则："
                    f"{pattern or '{datetime}_{ms}'}")

        # ---- OCR 引擎常驻 / 截图后自动 OCR ----
        keep = self.chk_ocr_keep_alive.isChecked()
        self._cfg.cp.set("software", "ocr_keep_alive",
                         "true" if keep else "false")
        msgs.append(T("ui.msg_ocr_keep", "OCR 常驻：") + ("开" if keep else "关"))
        auto = self.chk_ocr_auto.isChecked()
        self._cfg.cp.set("software", "ocr_auto", "true" if auto else "false")
        msgs.append(T("ui.msg_ocr_auto", "自动 OCR：") + ("开" if auto else "关"))
        drop = self.chk_drop_ocr.isChecked()
        self._cfg.cp.set("software", "drop_ocr_auto", "true" if drop else "false")
        msgs.append(T("ui.msg_drop_ocr", "拖入自动 OCR：")
                    + ("开" if drop else "关"))
        # ---- 二维码识别（[software] enable_qr）----
        qr_on = self.chk_qr.isChecked()
        self._cfg.cp.set("software", "enable_qr", "true" if qr_on else "false")
        msgs.append("二维码识别：" + ("开" if qr_on else "关"))
        # ---- 截图无操作也保存（[software] save_no_op_close）----
        noop = self.chk_save_no_op.isChecked()
        self._cfg.cp.set("software", "save_no_op_close",
                         "true" if noop else "false")
        msgs.append(T("ui.msg_save_no_op", "无操作保存：")
                    + ("开" if noop else "关"))

        # ---- 开机自启动（M2-8）----
        from core_engine.autostart import set_enabled as _set_auto
        if _set_auto(self.chk_autostart.isChecked()):
            msgs.append(T("ui.msg_autostart", "开机自启：") + ("开" if self.chk_autostart.isChecked() else "关"))
        else:
            msgs.append(T("ui.msg_autostart_fail", "开机自启设置失败"))

        # ---- 截图贴图最大数量 ----
        self._cfg.cp.set("software", "max_history_windows",
                         str(self.spin_max_shot.value()))
        msgs.append(f"截图贴图：{self.spin_max_shot.value()}")
        # ---- 历史贴图最大数量（从历史恢复的图）----
        self._cfg.cp.set("software", "max_hist_windows",
                         str(self.spin_max_hist.value()))
        msgs.append(f"历史贴图：{self.spin_max_hist.value()}")

        # ---- 历史面板：宽度 / 显示卡片数 ----
        self._cfg.cp.set("ui", "history_panel_width",
                         str(self.spin_hist_width.value()))
        self._cfg.cp.set("ui", "history_visible_cards",
                         str(self.spin_hist_cards.value()))
        msgs.append(f"历史面板：{self.spin_hist_width.value()}px / "
                    f"显示 {self.spin_hist_cards.value()} 张")

        # ---- 钉图数量上限 ----
        self._cfg.cp.set("software", "max_pin_windows",
                         str(self.spin_max_pin.value()))
        msgs.append(f"钉图上限：{self.spin_max_pin.value()}")

        # ---- GPU 加速（[software] ocr_device，重启生效）----
        dev = self.combo_accel.currentData()
        self._cfg.cp.set("software", "ocr_device", dev)
        msgs.append(f"GPU 加速：{self.combo_accel.currentText()}")

        # ---- OCR 模型档位（[software] ocr_model，保存即热切换）----
        new_model = self.combo_model.currentData()
        self._cfg.cp.set("software", "ocr_model", new_model)
        msgs.append(f"OCR 模型：{self.combo_model.currentText()}")
        if new_model != self._model_set_old:
            self._model_set_old = new_model   # 先更新旧值，避免重复触发
            if self._on_model_changed is not None:
                try:
                    self._on_model_changed(new_model)
                except Exception:       # noqa: BLE001 切换失败已在 App 内提示
                    log.exception("模型档位切换回调失败: %s", new_model)

        # ---- 截图框选边框 ----
        self._cfg.cp.set("ui", "border_color",
                         ",".join(map(str, self.btn_sel_color.color)))
        self._cfg.cp.set("ui", "border_width",
                         str(self.spin_sel_width.value()))
        msgs.append(f"框选边框：{self.btn_sel_color.color} "
                    f"{self.spin_sel_width.value()}px")

        # ---- 结果图片边框 ----
        self._cfg.cp.set("ui", "overlay_border_color",
                         ",".join(map(str, self.btn_overlay_color.color)))
        self._cfg.cp.set("ui", "overlay_border_width",
                         str(self.spin_overlay_width.value()))
        msgs.append(f"窗口边框：{self.btn_overlay_color.color} "
                    f"{self.spin_overlay_width.value()}px")

        # ---- 二维码边框 / 表格边框颜色 ----
        self._cfg.cp.set("ui", "qr_border_color",
                         ",".join(map(str, self.btn_qr_color.color)))
        msgs.append(f"二维码边框：{self.btn_qr_color.color} ")
        self._cfg.cp.set("ui", "table_border_color",
                         ",".join(map(str, self.btn_table_color.color)))
        msgs.append(f"表格边框：{self.btn_table_color.color} ")

        # ---- 识别文字蓝色框默认显示 ----
        self._cfg.cp.set("ui", "show_text_boxes",
                         "true" if self.chk_text_boxes.isChecked() else "false")
        msgs.append(T("ui.msg_text_boxes", "文字框：") + ("显示" if self.chk_text_boxes.isChecked() else "隐藏"))

        # ---- 截图取色器参数（像素框/倍率）----
        self._cfg.cp.set("ui", "picker_size",
                         str(self.spin_picker_size.value()))
        self._cfg.cp.set("ui", "picker_zoom",
                         str(self.spin_picker_zoom.value()))
        msgs.append(f"取色器：{self.spin_picker_size.value()}×"
                    f"{self.spin_picker_zoom.value()}")

        # ---- 统一工具栏全局开关 ----
        self._cfg.cp.set("ui", "toolbar_enabled",
                         "true" if self.chk_toolbar.isChecked() else "false")
        msgs.append(T("ui.msg_toolbar", "统一工具栏：") + ("开" if self.chk_toolbar.isChecked() else "关"))

        # ---- 纯信息状态栏全局开关 ----
        self._cfg.cp.set("ui", "strip_enabled",
                         "true" if self.chk_strip.isChecked() else "false")
        msgs.append(T("ui.msg_strip", "状态栏：") + ("开" if self.chk_strip.isChecked() else "关"))

        # ---- 标注栏默认开启 ----
        self._cfg.cp.set("ui", "anno_toolbar_default",
                         "true" if self.chk_anno_default.isChecked() else "false")
        msgs.append(T("ui.msg_anno_default", "标注栏默认：") + ("开" if self.chk_anno_default.isChecked() else "关"))

        # ---- 标注工具参数永久化记录 ----
        self._cfg.cp.set("ui", "persist_params",
                         "true" if self.chk_anno_persist.isChecked() else "false")
        msgs.append(T("ui.msg_anno_persist", "标注参数持久化：") +
                    ("开" if self.chk_anno_persist.isChecked() else "关"))

        # ---- 所有快捷键行（统一控件，逐行读取 combo()）----
        # 全局热键行走 apply（冲突检测 + 注册/注销到 HotkeyHost + 写 config）；
        # 局部行走 parser 校验后写 config。原先散落在这里的一大段热键处理
        # 代码（截图/全屏/历史/复制/取色/格式 各自来一遍）全部收敛于此循环。
        for (row, _lbl, _sec, _key, parser, invalid, apply) in getattr(
                self, "_hk_rows", []):
            combo = row.combo()
            if apply is not None:
                msgs += apply(combo) or []
            elif parser is not None:
                if combo not in ("off", "none", "false") \
                        and parser(combo) is None:
                    msgs.append(invalid or f"{_lbl} 快捷键格式无效")
                else:
                    self._cfg.cp.set(_sec, _key, combo)
                    msgs.append(f"{_lbl}：{combo}")
            else:
                self._cfg.cp.set(_sec, _key, combo)
                msgs.append(f"{_lbl}：{combo}")

        # ---- 滚轮缩放修饰键 ----
        self._cfg.cp.set("hotkey_overlay", "zoom_modifier",
                         self.combo_zoom_mod.currentData())
        msgs.append(f"滚轮缩放：{self.combo_zoom_mod.currentText()}")
        self._cfg.cp.set("hotkey_overlay", "geom_zoom_modifier",
                         self.combo_geom_zoom_mod.currentData())
        msgs.append(f"窗口缩放：{self.combo_geom_zoom_mod.currentText()}")

        try:
            self._cfg.save()
            # 保存结果用托盘气泡通知（窗口内不再显示状态文字）
            self._notify(T("ui.msg_saved", "已保存"))
        except OSError as e:
            self._notify(T("ui.msg_save_fail", "保存失败：{}").format(e))
