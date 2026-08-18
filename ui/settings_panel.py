"""M2-4 设置面板：软件设置 / UI设置 / 快捷键设置 三个 Tab（统一浮层基类）。

- 「软件设置」：历史保留条数（不设上限）、历史图片位置、复制模式（本期仅存配置）。
- 「UI设置」：界面主题（跟随系统/亮/暗）、截图框选边框颜色/宽度、结果图片边框颜色/宽度。
- 「快捷键设置」：全局截图热键、历史记录快捷键、复制快捷键（Shift+C）、快捷键一览。
- 无边框半透明卡片（明/暗主题与历史面板一致）；标题栏可拖动；Esc 关闭。
- 热键基于 HotkeyHost.set_combo 即时生效；主题保存后即时全局应用。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (QFormLayout, QHBoxLayout, QVBoxLayout,
                               QTabWidget, QWidget, QCheckBox, QComboBox,
                               QLineEdit, QSpinBox, QLabel, QPushButton,
                               QApplication, QFileDialog, QScrollArea, QFrame,
                               QSlider)

from config import ConfigManager
from ui.card_window import CardWindow
from ui.texts import T
from ui.result_overlay import _parse_qt_combo
from ui.theme import apply_theme, resolve_theme
from ui.widgets import ThemeSpinBox, ColorButton as _ColorButton
from ui.theme_dialog import ThemeDialog


class _ModBox(QCheckBox):
    """紧凑修饰键：keymod 样式把勾选框渲染为 0 宽，但 sizeHint 仍按默认
    勾选框（18px）计算会把快捷键行撑宽；覆写 sizeHint 按文字宽度。"""

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self.text()) + 14,
                     super().sizeHint().height())


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
        if not key:
            self.setText("")
        elif key == "`":
            self.setText("`")
        else:
            self.setText(key.upper())

    # ---- 交互 ----
    def mousePressEvent(self, e):
        self._capturing = True
        self.setText("…")
        self.setFocus(Qt.MouseFocusReason)
        e.accept()

    def keyPressEvent(self, e: QKeyEvent):
        if not self._capturing:
            return
        key = e.key()
        # 只接受字母键（A-Z）或反引号（`）；修饰键/功能键忽略
        if ord("A") <= key <= ord("Z"):
            ch = chr(key).lower()
        elif key == Qt.Key_QuoteLeft:
            ch = "`"
        else:
            return
        self._key = ch
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
                 history_storage=None, parent=None, tray=None):
        super().__init__(title=T("settings.title", "ClickOCR 设置"), with_close=True)
        self.setMinimumWidth(400)
        self._cfg = config
        self._hotkey = hotkey_host
        self._history = history_storage
        self._tray = tray            # 托盘（保存成功等用气泡通知）

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
                                              "db", "screenshot_max_count", 50))
        form.addRow(T("history.count", "历史数量"), self.spin_capacity)
        self._attach_hint(
            form, self.spin_capacity,
            T("ui.hist_count_hint", "默认 50 条；条数越多占用磁盘越大（超出时自动清理最旧记录）。"))

        # 历史图片位置
        self.edit_shot_dir = QLineEdit(
            self._cfg.get("paths", "screenshots_dir", ""))
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

        # 历史窗口最大数量（默认 1，打开下一个销毁最旧）
        self.spin_max_histwin = ThemeSpinBox(
            1, 10, self._cfg.getint("ui", "max_history_windows", 1), width=56)
        form.addRow(T("history.popup_count", "弹窗数量"), self.spin_max_histwin)

        # 钉图数量上限（默认 3，上限 20；钉图不占历史窗口数量）
        self.spin_max_pin = ThemeSpinBox(
            1, 20, self._cfg.getint("ui", "max_pin_windows", 3), width=56)
        form.addRow(T("history.pin_count", "钉图数量"), self.spin_max_pin)
        self._attach_hint(
            form, self.spin_max_pin,
            T("ui.pin_hint", "钉图把截图/历史图片置顶显示，不占用历史窗口数量；超出上限时自动移除最旧的钉图。"))

        # 复制模式（本期仅存配置，行为不变）
        self.combo_selection = QComboBox()
        self.combo_selection.addItem(T("ui.copy_mode_auto", "auto_commit（选中即复制，现行为）"), "auto_commit")
        self.combo_selection.addItem(T("ui.copy_mode_manual", "manual_copy（手动 Ctrl+C，本期仅预留）"), "manual_copy")
        cur2 = self._cfg.get("screenshot", "selection_mode", "auto_commit")
        i2 = self.combo_selection.findData(cur2)
        self.combo_selection.setCurrentIndex(i2 if i2 >= 0 else 0)
        form.addRow(T("history.copy_mode", "复制模式"), self.combo_selection)
        self._attach_hint(
            form, self.combo_selection,
            T("ui.copy_mode_hint", "提示：复制模式本期仅保存配置，行为暂不变（M2 计划 §5.2）。"))

        # ---- 图片保存格式（PNG/JPG/BMP/WebP）+ 质量（有损格式 1-100）----
        self.combo_save_fmt = QComboBox()
        for label, data in ((T("ui.save_fmt_png", "PNG（无损）"), "png"),
                            (T("ui.save_fmt_jpg", "JPG（有损·小体积）"), "jpg"),
                            (T("ui.save_fmt_bmp", "BMP（无损）"), "bmp"),
                            (T("ui.save_fmt_webp", "WebP（有损·小体积）"), "webp")):
            self.combo_save_fmt.addItem(label, data)
        cur_fmt = self._cfg.get("screenshot", "save_format", "png").lower()
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
            "screenshot", "save_quality", 90))))
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
            self._cfg.get("screenshot", "name_pattern", "{datetime}_{ms}"))
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

        # GPU 加速（[ocr] device = cpu | cuda | dml，需重启生效）
        self.combo_accel = QComboBox()
        self.combo_accel.addItem(T("ui.accel_auto", "自动（CPU）"), "cpu")
        self.combo_accel.addItem(T("ui.accel_cuda", "CUDA（NVIDIA 显卡）"), "cuda")
        self.combo_accel.addItem(T("ui.accel_dml", "DirectML（AMD / Intel / 通用）"), "dml")
        dev = self._cfg.get("ocr", "device", "cpu").lower()
        i = self.combo_accel.findData(dev if dev in ("cuda", "dml") else "cpu")
        self.combo_accel.setCurrentIndex(i if i >= 0 else 0)
        self.lbl_accel_status = QLabel("")
        self.lbl_accel_status.setProperty("kind", "info")
        self.lbl_accel_status.setWordWrap(True)
        accel_row = QHBoxLayout()
        accel_row.addWidget(self.combo_accel, 1)
        self.btn_accel_install = QPushButton(T("btn.detect", "检测"))
        self.btn_accel_install.setToolTip(T("ui.accel_install_tip", "检测所选加速档位的运行时就绪状态；未安装时可一键安装（需联网）"))
        self.btn_accel_install.clicked.connect(self._check_install_accel)
        accel_row.addWidget(self.btn_accel_install)
        form.addRow(T("gpu.accel", "GPU 加速"), accel_row)
        self._attach_hint(
            form, accel_row,
            T("ui.accel_hint",
              "CUDA 需 onnxruntime-gpu；DirectML 需 onnxruntime-directml"
              "（AMD/Intel 核显/NVIDIA 通用）。无对应环境自动回退 CPU。重启程序生效。"))
        self._refresh_accel_status()

        # ---- OCR 引擎常驻（[ocr] ocr_keep_alive，默认勾选）----
        self.chk_ocr_keep_alive = QCheckBox(T("ui.ocr_keep_alive", "OCR 引擎常驻"))
        self.chk_ocr_keep_alive.setChecked(
            self._cfg.getbool("ocr", "ocr_keep_alive", True))
        self.chk_ocr_keep_alive.setToolTip(T(
            "ui.ocr_keep_alive_hint",
            "勾选：启动时加载 OCR 模型（识别更快，占用内存）；\n"
            "不勾选：启动不加载模型，首次识别时才加载（省内存、首次稍慢）。"))
        form.addRow("", self.chk_ocr_keep_alive)

        # ---- 截图后自动 OCR（[ocr] ocr_auto，默认勾选）----
        self.chk_ocr_auto = QCheckBox(T("ocr.auto", "截图后自动 OCR"))
        self.chk_ocr_auto.setChecked(
            self._cfg.getbool("ocr", "ocr_auto", True))
        self.chk_ocr_auto.setToolTip(
            T("ui.ocr_auto_hint",
              "勾选：截图后自动识别文字并显示文字框；\n"
              "不勾选：截图仅贴图，点「文字框」或「表格」按钮时才识别。"))
        form.addRow("", self.chk_ocr_auto)

        # 开机自启动（M2-8：注册表 HKCU Run）
        from core_engine.autostart import is_enabled
        self.chk_autostart = QCheckBox(T("autostart", "开机自动启动 ClickOCR"))
        self.chk_autostart.setChecked(is_enabled())
        form.addRow("", self.chk_autostart)
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

    def _on_save_fmt_changed(self):
        """质量滑块仅对有损格式（JPG/WebP）生效。"""
        fmt = self.combo_save_fmt.currentData()
        lossy = fmt in ("jpg", "webp")
        self.slider_quality.setEnabled(lossy)
        self.lbl_quality.setEnabled(lossy)

    def _check_install_accel(self):
        """检测并（开发模式）自动安装所选 GPU 运行时；下载前弹确认。"""
        from core_engine.ort_runtime import (device_ready, install_runtime,
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
            ThemeDialog.warning(None, T("gpu.accel", "GPU 加速"),
                                T("ui.gpu_pkg_manual", "打包版无法自动安装，请手动执行：\n\npip install {}").format(pkg))
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

        # 界面主题
        self.combo_theme = QComboBox()
        self.combo_theme.addItem(T("ui.theme_system", "跟随系统"), "system")
        self.combo_theme.addItem(T("ui.theme_light", "亮色"), "light")
        self.combo_theme.addItem(T("ui.theme_dark", "暗色"), "dark")
        cur = self._cfg.get("ui", "theme", "system").strip().lower()
        idx = self.combo_theme.findData(cur)
        self.combo_theme.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow(T("ui.theme", "界面主题"), self.combo_theme)
        self._attach_hint(
            form, self.combo_theme,
            T("ui.theme_hint", "暗色 = 无边框半透明深色卡片；跟随系统 = 随 Windows 深浅色自动切换。"))

        # 历史面板：宽度 / 显示卡片数（高度）
        self.spin_hist_width = ThemeSpinBox(
            400, 1600, self._cfg.getint("ui", "history_panel_width", 620),
            width=56)
        self.spin_hist_cards = ThemeSpinBox(
            1, 30, self._cfg.getint("ui", "history_visible_cards", 5),
            width=48)
        hist_row = QHBoxLayout()
        hist_row.setSpacing(4)
        hist_row.addWidget(QLabel(T("ui.hist_w", "宽")))
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
            self._cfg.getcolor("selection", "border_color", (100, 190, 255)))
        self.spin_sel_width = ThemeSpinBox(
            1, 10, self._cfg.getint("selection", "border_width", 2), width=48)
        border_row = QHBoxLayout()
        border_row.addWidget(self.btn_sel_color)
        border_row.addWidget(self.spin_sel_width)
        border_row.addStretch(1)
        form.addRow(T("ui.sel_border", "截图边框"), border_row)

        # 截图取色器：放大镜像素框大小 / 放大倍率（最大 15×15）
        self.spin_picker_size = ThemeSpinBox(
            3, 15, self._cfg.getint("selection", "picker_size", 7), width=48)
        self.spin_picker_zoom = ThemeSpinBox(
            2, 20, self._cfg.getint("selection", "picker_zoom", 9), width=48)
        picker_row = QHBoxLayout()
        picker_row.addWidget(QLabel(T("ui.picker_px", "像素")))
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
            self._cfg.getcolor("result", "overlay_border_color", (100, 190, 255)))
        self.spin_overlay_width = ThemeSpinBox(
            0, 10, self._cfg.getint("result", "overlay_border_width", 1), width=48)
        overlay_row = QHBoxLayout()
        overlay_row.addWidget(self.btn_overlay_color)
        overlay_row.addWidget(self.spin_overlay_width)
        overlay_row.addStretch(1)
        form.addRow(T("ui.overlay_border", "窗口边框"), overlay_row)
        self._attach_hint(
            form, overlay_row,
            T("ui.overlay_border_hint", "宽度 0 = 不显示边框（[selection]/[result] 配置段）。"))

        # 统一工具栏（操作按钮 + 标注工具区）全局开关
        self.chk_toolbar = QCheckBox(T("ui.toolbar", "统一工具栏"))
        self.chk_toolbar.setChecked(
            self._cfg.getbool("result", "toolbar_enabled", True))
        self.chk_toolbar.setToolTip(
            T("ui.toolbar_hint", "关闭后图片窗口不显示操作按钮/标注工具（仅保留状态栏、关闭按钮与快捷键）"))
        form.addRow("", self.chk_toolbar)

        # 纯信息状态栏全局开关
        self.chk_strip = QCheckBox(T("ui.strip", "状态栏（纯信息）"))
        self.chk_strip.setChecked(
            self._cfg.getbool("result", "strip_enabled", True))
        self.chk_strip.setToolTip(
            T("ui.strip_hint", "状态栏只显示状态文字与选择预览；关闭后不显示（Esc 关闭后也可在此重新开启）"))
        form.addRow("", self.chk_strip)

        # 识别文字蓝色框默认显示
        self.chk_text_boxes = QCheckBox(T("ui.text_boxes", "图片文字边框"))
        self.chk_text_boxes.setChecked(
            self._cfg.getbool("result", "show_text_boxes", True))
        form.addRow("", self.chk_text_boxes)

        # 标注栏默认开启
        self.chk_anno_default = QCheckBox(T("ui.anno_bar", "截图标注栏"))
        self.chk_anno_default.setChecked(
            self._cfg.getbool("result", "anno_toolbar_default", False))
        form.addRow("", self.chk_anno_default)

        # 标注工具参数永久化记录（颜色/线宽/字号/加粗/斜体/马赛克模式）
        self.chk_anno_persist = QCheckBox(T("ui.anno_persist", "永久化记录标注工具参数"))
        self.chk_anno_persist.setChecked(
            self._cfg.getbool("annotation", "persist_params", True))
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

        def make_mod(name: str) -> QCheckBox:
            cb = _ModBox(name)
            cb.setProperty("kind", "keymod")   # 紧凑修饰键：无方块、纯文字
            return cb

        def make_enable_btn() -> QPushButton:
            """「启用」按钮：checkable，按下变蓝（同状态栏文字框按钮）。"""
            b = QPushButton(T("hk.enable", "启用"))
            b.setProperty("kind", "corner")
            b.setCheckable(True)
            b.setFixedHeight(24)
            return b

        # ---- 分段 & 通用快捷键行（截图选择/贴图窗口/标注，可配置）----
        self._extra_hk_rows = []   # (mods, capture, chk, sec, key)，_save 循环保存

        def add_hk_group(text_key: str, text: str):
            g = QLabel(T(text_key, text))
            g.setProperty("kind", "group")
            # 整行独占（无空 label）：分组标题贴最左，不被 label 列推挤
            form.addRow(g)

        def add_hk_row(label_key: str, label: str, sec: str, key: str,
                       default: str):
            mods = tuple(make_mod(m) for m in
                         (T("hk.mod_ctrl", "Ctrl"), T("hk.mod_alt", "Alt"),
                          T("hk.mod_shift", "Shift"), T("hk.mod_win", "Win")))
            cap = KeyCaptureEdit(
                self._key_from_combo(self._cfg.get(sec, key, default),
                                     default.replace("+", " ").split()[-1]))
            chk = make_enable_btn()
            row = QHBoxLayout()
            row.setSpacing(4)
            for w in (*mods, cap, chk):
                row.addWidget(w)
            row.addStretch(1)
            form.addRow(T(label_key, label), row)
            self._load_key_row(mods, cap, chk, self._cfg.get(sec, key, default))
            cap.keyCaptured.connect(lambda _k, c=chk: c.setChecked(True))
            self._extra_hk_rows.append((mods, cap, chk, sec, key))

        add_hk_group("hk.group_global", "全局热键")

        # 全局热键（截图）：修饰键勾选 + 键位点击拾取 + 使用（蓝色按下）
        self.cb_ctrl = make_mod(T("hk.mod_ctrl", "Ctrl"))
        self.cb_alt = make_mod(T("hk.mod_alt", "Alt"))
        self.cb_shift = make_mod(T("hk.mod_shift", "Shift"))
        self.cb_win = make_mod(T("hk.mod_win", "Win"))
        self.key_capture = KeyCaptureEdit(
            self._key_from_combo(
                self._cfg.get("hotkey", "combo", "ctrl+shift+r"), "r"))
        self.chk_enable = make_enable_btn()
        hk_row = QHBoxLayout()
        hk_row.setSpacing(4)
        for wd in (self.cb_ctrl, self.cb_alt, self.cb_shift, self.cb_win,
                   self.key_capture, self.chk_enable):
            hk_row.addWidget(wd)
        hk_row.addStretch(1)
        form.addRow(T("hk.screenshot", "截图"), hk_row)
        self._load_key_row(
            (self.cb_ctrl, self.cb_alt, self.cb_shift, self.cb_win),
            self.key_capture, self.chk_enable,
            self._cfg.get("hotkey", "combo", "ctrl+shift+r"))
        self.key_capture.keyCaptured.connect(
            lambda _k: self.chk_enable.setChecked(True))

        # 全屏截图热键（独立组合：直接截整屏不框选，默认 off）
        self.cb_full_ctrl = make_mod(T("hk.mod_ctrl", "Ctrl"))
        self.cb_full_alt = make_mod(T("hk.mod_alt", "Alt"))
        self.cb_full_shift = make_mod(T("hk.mod_shift", "Shift"))
        self.cb_full_win = make_mod(T("hk.mod_win", "Win"))
        self.key_full_capture = KeyCaptureEdit(
            self._key_from_combo(
                self._cfg.get("hotkey_full", "combo", "off"), "f"))
        self.chk_full_enable = make_enable_btn()
        full_row = QHBoxLayout()
        full_row.setSpacing(4)
        for wd in (self.cb_full_ctrl, self.cb_full_alt, self.cb_full_shift,
                   self.cb_full_win, self.key_full_capture, self.chk_full_enable):
            full_row.addWidget(wd)
        full_row.addStretch(1)
        form.addRow(T("hk.fullscreen", "全屏截图"), full_row)
        self._load_key_row(
            (self.cb_full_ctrl, self.cb_full_alt, self.cb_full_shift,
             self.cb_full_win),
            self.key_full_capture, self.chk_full_enable,
            self._cfg.get("hotkey_full", "combo", "off"))
        self.key_full_capture.keyCaptured.connect(
            lambda _k: self.chk_full_enable.setChecked(True))

        # 历史记录快捷键（全局热键，默认 Ctrl+`）
        self.cb_hist_ctrl = make_mod(T("hk.mod_ctrl", "Ctrl"))
        self.cb_hist_alt = make_mod(T("hk.mod_alt", "Alt"))
        self.cb_hist_shift = make_mod(T("hk.mod_shift", "Shift"))
        self.cb_hist_win = make_mod(T("hk.mod_win", "Win"))
        self.key_hist_capture = KeyCaptureEdit(
            self._key_from_combo(
                self._cfg.get("hotkey_ext", "history_hotkey", "ctrl+`"), "`"))
        self.chk_hist_enable = make_enable_btn()
        hist_row = QHBoxLayout()
        hist_row.setSpacing(4)
        for wd in (self.cb_hist_ctrl, self.cb_hist_alt, self.cb_hist_shift,
                   self.cb_hist_win, self.key_hist_capture, self.chk_hist_enable):
            hist_row.addWidget(wd)
        hist_row.addStretch(1)
        form.addRow(T("hk.history", "历史记录"), hist_row)
        self._load_key_row(
            (self.cb_hist_ctrl, self.cb_hist_alt, self.cb_hist_shift,
             self.cb_hist_win),
            self.key_hist_capture, self.chk_hist_enable,
            self._cfg.get("hotkey_ext", "history_hotkey", "ctrl+`"))
        self.key_hist_capture.keyCaptured.connect(
            lambda _k: self.chk_hist_enable.setChecked(True))







        # ---- 截图选择（截图遮罩中）----
        add_hk_group("hk.group_select", "截图选择")
        add_hk_row("hk.full_sel", "全屏框选", "hotkey_ext", "full_sel", "a")
        add_hk_row("hk.scroll_sel", "滚动截图（截长图）", "hotkey_ext",
                   "scroll_sel", "r")

        # 取色复制色值快捷键（截图时光标取色器，默认 C）
        self.cb_pick_ctrl = make_mod(T("hk.mod_ctrl", "Ctrl"))
        self.cb_pick_alt = make_mod(T("hk.mod_alt", "Alt"))
        self.cb_pick_shift = make_mod(T("hk.mod_shift", "Shift"))
        self.cb_pick_win = make_mod(T("hk.mod_win", "Win"))
        pick_key = "c"
        _pp = self._cfg.get("hotkey_ext", "color_pick", "c").lower()
        for _p in _pp.replace("+", " ").split():
            if _p.isalpha() and _p not in ("ctrl", "alt", "shift", "win"):
                pick_key = _p
        self.pick_capture = KeyCaptureEdit(pick_key)
        self.chk_pick_enable = make_enable_btn()
        pick_row = QHBoxLayout()
        pick_row.setSpacing(4)
        for wd in (self.cb_pick_ctrl, self.cb_pick_alt, self.cb_pick_shift,
                   self.cb_pick_win, self.pick_capture, self.chk_pick_enable):
            pick_row.addWidget(wd)
        pick_row.addStretch(1)
        form.addRow(T("hk.pick_color", "复制色值"), pick_row)
        self._attach_hint(
            form, pick_row,
            T("hk.pick_hint",
              "截图时光标带取色器；按此键复制当前像素色值（如 #FF8800）。"))
        self._load_key_row(
            (self.cb_pick_ctrl, self.cb_pick_alt, self.cb_pick_shift,
             self.cb_pick_win),
            self.pick_capture, self.chk_pick_enable,
            self._cfg.get("hotkey_ext", "color_pick", "c"))
        self.pick_capture.keyCaptured.connect(
            lambda _k: self.chk_pick_enable.setChecked(True))
        # 切换色值格式快捷键（截图取色器按此键循环 RGB→HSV→HSL，默认 H）
        self.cb_fmt_ctrl = make_mod(T("hk.mod_ctrl", "Ctrl"))
        self.cb_fmt_alt = make_mod(T("hk.mod_alt", "Alt"))
        self.cb_fmt_shift = make_mod(T("hk.mod_shift", "Shift"))
        self.cb_fmt_win = make_mod(T("hk.mod_win", "Win"))
        fmt_key = "h"
        _fp = self._cfg.get("hotkey_ext", "color_fmt", "h").lower()
        for _p in _fp.replace("+", " ").split():
            if _p.isalpha() and _p not in ("ctrl", "alt", "shift", "win"):
                fmt_key = _p
        self.fmt_capture = KeyCaptureEdit(fmt_key)
        self.chk_fmt_enable = make_enable_btn()
        fmt_row = QHBoxLayout()
        fmt_row.setSpacing(4)
        for wd in (self.cb_fmt_ctrl, self.cb_fmt_alt, self.cb_fmt_shift,
                   self.cb_fmt_win, self.fmt_capture, self.chk_fmt_enable):
            fmt_row.addWidget(wd)
        fmt_row.addStretch(1)
        form.addRow(T("hk.fmt_color", "切换色值格式"), fmt_row)
        self._attach_hint(
            form, fmt_row,
            T("hk.fmt_hint",
              "截图取色时按此键循环切换色值显示：RGB → HSV → HSL。"))
        self._load_key_row(
            (self.cb_fmt_ctrl, self.cb_fmt_alt, self.cb_fmt_shift,
             self.cb_fmt_win),
            self.fmt_capture, self.chk_fmt_enable,
            self._cfg.get("hotkey_ext", "color_fmt", "h"))
        self.fmt_capture.keyCaptured.connect(
            lambda _k: self.chk_fmt_enable.setChecked(True))
        # ---- 贴图窗口（截图结果 / 历史图）----
        add_hk_group("hk.group_overlay", "贴图窗口")
        add_hk_row("hk.reocr", "重新 OCR", "hotkey_ext", "reocr", "ctrl+r")
        add_hk_row("hk.table_ocr", "表格识别", "hotkey_ext",
                   "table_ocr", "ctrl+shift+t")
        add_hk_row("hk.toggle_anno", "标注模式", "hotkey_ext",
                   "toggle_anno", "ctrl+shift+d")
        add_hk_row("hk.nav", "导航图", "hotkey_ext", "nav_hotkey", "m")
        add_hk_row("hk.ratio", "等比例调节窗口", "hotkey_ext",
                   "ratio_hotkey", "e")

        # 复制快捷键（默认 Shift+C；修饰键勾选 + 键位点击拾取）
        self.cb_copy_ctrl = make_mod(T("hk.mod_ctrl", "Ctrl"))
        self.cb_copy_alt = make_mod(T("hk.mod_alt", "Alt"))
        self.cb_copy_shift = make_mod(T("hk.mod_shift", "Shift"))
        self.cb_copy_win = make_mod(T("hk.mod_win", "Win"))
        copy_key = "c"
        _cp = self._cfg.get("hotkey_ext", "copy_shortcut", "shift+c").lower()
        for _p in _cp.replace("+", " ").split():
            if _p.isalpha() and _p not in ("ctrl", "alt", "shift", "win"):
                copy_key = _p
        self.copy_capture = KeyCaptureEdit(copy_key)
        self.chk_copy_enable = make_enable_btn()
        copy_row = QHBoxLayout()
        copy_row.setSpacing(4)
        for wd in (self.cb_copy_ctrl, self.cb_copy_alt, self.cb_copy_shift,
                   self.cb_copy_win, self.copy_capture, self.chk_copy_enable):
            copy_row.addWidget(wd)
        copy_row.addStretch(1)
        form.addRow(T("hk.copy", "全复制到剪贴板"), copy_row)
        self._load_key_row(
            (self.cb_copy_ctrl, self.cb_copy_alt, self.cb_copy_shift,
             self.cb_copy_win),
            self.copy_capture, self.chk_copy_enable,
            self._cfg.get("hotkey_ext", "copy_shortcut", "shift+c"))
        self.copy_capture.keyCaptured.connect(
            lambda _k: self.chk_copy_enable.setChecked(True))
        self._update_copy_enabled()
        self.chk_copy_enable.toggled.connect(self._update_copy_enabled)
        add_hk_row("hk.select_all", "全选文字", "hotkey_ext",
                   "select_all", "ctrl+a")
        add_hk_row("hk.copy_selected", "复制选中文字", "hotkey_ext",
                   "copy_selected", "ctrl+c")
        add_hk_row("hk.copy_image", "复制图片", "hotkey_ext",
                   "copy_image", "ctrl+shift+c")
        add_hk_row("hk.copy_raw_image", "复制原图", "hotkey_ext",
                   "copy_raw_image", "ctrl+alt+c")
        add_hk_row("hk.through", "点击穿透（全局）", "hotkey_ext",
                   "through_hotkey", "alt+f")

        # 图片缩放修饰键（[hotkey_ext] zoom_modifier，非按键行，用下拉）
        self.combo_zoom_mod = QComboBox()
        for label, data in ((T("hk.zoom_ctrl", "Ctrl + 滚轮"), "ctrl"),
                            (T("hk.zoom_alt", "Alt + 滚轮"), "alt"),
                            (T("hk.zoom_shift", "Shift + 滚轮"), "shift"),
                            (T("hk.zoom_off", "关"), "off")):
            self.combo_zoom_mod.addItem(label, data)
        cur_zm = self._cfg.get("hotkey_ext", "zoom_modifier", "ctrl").lower()
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
        # 窗口几何缩放修饰键（[hotkey_ext] geom_zoom_modifier，默认 Shift）
        self.combo_geom_zoom_mod = QComboBox()
        for label, data in ((T("hk.zoom_ctrl", "Ctrl + 滚轮"), "ctrl"),
                            (T("hk.zoom_alt", "Alt + 滚轮"), "alt"),
                            (T("hk.zoom_shift", "Shift + 滚轮"), "shift"),
                            (T("hk.zoom_off", "关"), "off")):
            self.combo_geom_zoom_mod.addItem(label, data)
        cur_gz = self._cfg.get("hotkey_ext", "geom_zoom_modifier", "shift").lower()
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
        add_hk_row("hk.undo_anno", "撤销", "hotkey_ext", "undo_anno", "ctrl+z")
        add_hk_row("hk.redo_anno", "重做", "hotkey_ext",
                   "redo_anno", "ctrl+shift+z")

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
        from ui.shortcut_panel import ShortcutPanel
        self._shortcut_panel = ShortcutPanel(cfg=self._cfg)
        self._shortcut_panel.show()
        self._shortcut_panel.raise_()

    def _on_tab_changed(self, idx: int):
        """「操作一览」按钮仅「快捷键设置」tab（index 2）显示。"""
        self.btn_shortcut.setVisible(idx == 2)

    def _update_copy_enabled(self):
        """复制快捷键「启用」勾选 → 控制拾取框可点性。"""
        on = self.chk_copy_enable.isChecked()
        self.copy_capture.setEnabled(on)
        if not on:
            self.copy_capture.clearFocus()

    # ---- 内部 ----
    @staticmethod
    def _key_from_combo(combo: str, default: str = "a") -> str:
        """从组合字符串提取末尾键位（如 "ctrl+shift+r" -> "r"；off -> default）。"""
        if not combo or combo.lower() in ("off", "none", "false"):
            return default
        for p in combo.lower().replace("+", " ").split():
            if p not in ("ctrl", "alt", "shift", "win") and len(p) == 1:
                return p
        return default

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
        """从控件生成组合字符串；未启用返回 "off"。"""
        if not chk_enable.isChecked():
            return "off"
        parts = []
        for cb, name in ((cbs[0], "ctrl"), (cbs[1], "alt"),
                         (cbs[2], "shift"), (cbs[3], "win")):
            if cb.isChecked():
                parts.append(name)
        parts.append(key_widget.key())
        return "+".join(parts)

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
        self._cfg.cp.set("db", "screenshot_max_count", str(cap))
        if self._history is not None:
            self._history.max_records = cap
        msgs.append(f"历史保留条数：{cap}")

        # ---- 历史图片位置（即时生效）----
        new_dir = self.edit_shot_dir.text().strip()
        self._cfg.cp.set("paths", "screenshots_dir", new_dir)
        if self._history is not None:
            target = Path(new_dir) if new_dir else \
                (self._cfg.app_root / "screenshots")
            target = target if target.is_absolute() \
                else (self._cfg.app_root / target)
            self._history.screenshots_dir = target
        msgs.append(f"历史图片位置：{new_dir or '默认 screenshots/'}")

        # ---- 复制模式 ----
        self._cfg.cp.set("screenshot", "selection_mode",
                         self.combo_selection.currentData())
        msgs.append(f"复制模式：{self.combo_selection.currentData()}")

        # ---- 图片保存格式 / 质量 / 文件名规则（即时生效）----
        fmt = self.combo_save_fmt.currentData()
        self._cfg.cp.set("screenshot", "save_format", fmt)
        self._cfg.cp.set("screenshot", "save_quality",
                         str(self.slider_quality.value()))
        pattern = self.edit_name_pattern.text().strip()
        self._cfg.cp.set("screenshot", "name_pattern",
                         pattern if pattern else "{datetime}_{ms}")
        msgs.append(f"保存格式：{fmt.upper()} · 质量："
                    f"{self.slider_quality.value()} · 文件名规则："
                    f"{pattern or '{datetime}_{ms}'}")

        # ---- OCR 引擎常驻 / 截图后自动 OCR ----
        keep = self.chk_ocr_keep_alive.isChecked()
        self._cfg.cp.set("ocr", "ocr_keep_alive", "true" if keep else "false")
        msgs.append(T("ui.msg_ocr_keep", "OCR 常驻：") + ("开" if keep else "关"))
        auto = self.chk_ocr_auto.isChecked()
        self._cfg.cp.set("ocr", "ocr_auto", "true" if auto else "false")
        msgs.append(T("ui.msg_ocr_auto", "自动 OCR：") + ("开" if auto else "关"))

        # ---- 开机自启动（M2-8）----
        from core_engine.autostart import set_enabled as _set_auto
        if _set_auto(self.chk_autostart.isChecked()):
            msgs.append(T("ui.msg_autostart", "开机自启：") + ("开" if self.chk_autostart.isChecked() else "关"))
        else:
            msgs.append(T("ui.msg_autostart_fail", "开机自启设置失败"))

        # ---- 历史窗口最大数量 ----
        self._cfg.cp.set("ui", "max_history_windows",
                         str(self.spin_max_histwin.value()))
        msgs.append(f"历史窗口上限：{self.spin_max_histwin.value()}")

        # ---- 历史面板：宽度 / 显示卡片数 ----
        self._cfg.cp.set("ui", "history_panel_width",
                         str(self.spin_hist_width.value()))
        self._cfg.cp.set("ui", "history_visible_cards",
                         str(self.spin_hist_cards.value()))
        msgs.append(f"历史面板：{self.spin_hist_width.value()}px / "
                    f"显示 {self.spin_hist_cards.value()} 张")

        # ---- 钉图数量上限 ----
        self._cfg.cp.set("ui", "max_pin_windows",
                         str(self.spin_max_pin.value()))
        msgs.append(f"钉图上限：{self.spin_max_pin.value()}")

        # ---- GPU 加速（[ocr] device，重启生效）----
        dev = self.combo_accel.currentData()
        self._cfg.cp.set("ocr", "device", dev)
        msgs.append(f"GPU 加速：{self.combo_accel.currentText()}")

        # ---- 截图框选边框 ----
        self._cfg.cp.set("selection", "border_color",
                         ",".join(map(str, self.btn_sel_color.color)))
        self._cfg.cp.set("selection", "border_width",
                         str(self.spin_sel_width.value()))
        msgs.append(f"框选边框：{self.btn_sel_color.color} "
                    f"{self.spin_sel_width.value()}px")

        # ---- 结果图片边框 ----
        self._cfg.cp.set("result", "overlay_border_color",
                         ",".join(map(str, self.btn_overlay_color.color)))
        self._cfg.cp.set("result", "overlay_border_width",
                         str(self.spin_overlay_width.value()))
        msgs.append(f"窗口边框：{self.btn_overlay_color.color} "
                    f"{self.spin_overlay_width.value()}px")

        # ---- 识别文字蓝色框默认显示 ----
        self._cfg.cp.set("result", "show_text_boxes",
                         "true" if self.chk_text_boxes.isChecked() else "false")
        msgs.append(T("ui.msg_text_boxes", "文字框：") + ("显示" if self.chk_text_boxes.isChecked() else "隐藏"))

        # ---- 截图取色器参数（像素框/倍率）----
        self._cfg.cp.set("selection", "picker_size",
                         str(self.spin_picker_size.value()))
        self._cfg.cp.set("selection", "picker_zoom",
                         str(self.spin_picker_zoom.value()))
        msgs.append(f"取色器：{self.spin_picker_size.value()}×"
                    f"{self.spin_picker_zoom.value()}")

        # ---- 统一工具栏全局开关 ----
        self._cfg.cp.set("result", "toolbar_enabled",
                         "true" if self.chk_toolbar.isChecked() else "false")
        msgs.append(T("ui.msg_toolbar", "统一工具栏：") + ("开" if self.chk_toolbar.isChecked() else "关"))

        # ---- 纯信息状态栏全局开关 ----
        self._cfg.cp.set("result", "strip_enabled",
                         "true" if self.chk_strip.isChecked() else "false")
        msgs.append(T("ui.msg_strip", "状态栏：") + ("开" if self.chk_strip.isChecked() else "关"))

        # ---- 标注栏默认开启 ----
        self._cfg.cp.set("result", "anno_toolbar_default",
                         "true" if self.chk_anno_default.isChecked() else "false")
        msgs.append(T("ui.msg_anno_default", "标注栏默认：") + ("开" if self.chk_anno_default.isChecked() else "关"))

        # ---- 标注工具参数永久化记录 ----
        self._cfg.cp.set("annotation", "persist_params",
                         "true" if self.chk_anno_persist.isChecked() else "false")
        msgs.append(T("ui.msg_anno_persist", "标注参数持久化：") +
                    ("开" if self.chk_anno_persist.isChecked() else "关"))

        # ---- 热键（截图，含启用勾选：不启用 = off）----
        new_combo = self._current_key_row(
            (self.cb_ctrl, self.cb_alt, self.cb_shift, self.cb_win),
            self.key_capture, self.chk_enable)
        if self._hotkey is not None:
            if new_combo == "off":
                self._hotkey.unregister_primary()
                self._cfg.cp.set("hotkey", "combo", "off")
                msgs.append("截图热键：off")
            elif new_combo != self._hotkey.active_combo:
                if self._hotkey.check_conflict(new_combo):
                    msgs.append(f"热键 {new_combo.upper()} 已被其它程序占用")
                elif self._hotkey.set_combo(new_combo):
                    self._cfg.cp.set("hotkey", "combo", new_combo)
                    msgs.append(f"热键已切换：{new_combo.upper()}")
                else:
                    msgs.append("热键切换失败（已回滚）")
            else:
                msgs.append(f"热键不变：{self._hotkey.active_combo.upper()}")
        elif new_combo != "off":
            self._cfg.cp.set("hotkey", "combo", new_combo)
            msgs.append(f"截图热键：{new_combo}")

        # ---- 历史记录快捷键（全局热键）----
        from hotkey import _parse_combo as _parse_global
        hh = self._current_key_row(
            (self.cb_hist_ctrl, self.cb_hist_alt, self.cb_hist_shift,
             self.cb_hist_win),
            self.key_hist_capture, self.chk_hist_enable)
        if hh in ("off", "none", "false"):
            if self._hotkey is not None:
                self._hotkey.unregister_secondary(2)
            self._cfg.cp.set("hotkey_ext", "history_hotkey", "off")
            msgs.append("历史记录快捷键：off")
        else:
            _m, _v = _parse_global(hh)
            if _v is None:
                msgs.append("历史记录快捷键格式无效（示例：ctrl+`）")
            else:
                if self._hotkey is not None:
                    ok = self._hotkey.set_secondary_combo(2, hh)
                    if not ok:
                        msgs.append("历史记录快捷键注册失败（被占用，已回滚）")
                self._cfg.cp.set("hotkey_ext", "history_hotkey", hh)
                msgs.append(f"历史记录快捷键：{hh}")

        # ---- 全屏截图热键 ----
        full_combo = self._current_key_row(
            (self.cb_full_ctrl, self.cb_full_alt, self.cb_full_shift,
             self.cb_full_win),
            self.key_full_capture, self.chk_full_enable)
        if self._hotkey is not None:
            if full_combo == "off":
                self._hotkey.unregister_secondary(3)
                self._cfg.cp.set("hotkey_full", "combo", "off")
                msgs.append("全屏截图热键：off")
            else:
                _fm, _fv = _parse_global(full_combo)
                if _fv is None:
                    msgs.append("全屏截图热键格式无效（示例：ctrl+shift+a）")
                else:
                    if self._hotkey.set_secondary_combo(3, full_combo):
                        self._cfg.cp.set("hotkey_full", "combo", full_combo)
                        msgs.append(f"全屏截图热键：{full_combo}")
                    else:
                        msgs.append("全屏截图热键注册失败（被占用，已回滚）")
        elif full_combo != "off":
            self._cfg.cp.set("hotkey_full", "combo", full_combo)
            msgs.append(f"全屏截图热键：{full_combo}")

        # ---- 复制快捷键（修饰键勾选 + 键位拾取）----
        sa = self._current_key_row(
            (self.cb_copy_ctrl, self.cb_copy_alt, self.cb_copy_shift,
             self.cb_copy_win),
            self.copy_capture, self.chk_copy_enable)
        if sa not in ("off", "none", "false") and _parse_qt_combo(sa) is None:
            msgs.append("复制快捷键格式无效（示例：shift+c）")
        else:
            self._cfg.cp.set("hotkey_ext", "copy_shortcut", sa)
            msgs.append(f"复制快捷键：{sa}")

        # ---- 取色复制色值快捷键 ----
        pk = self._current_key_row(
            (self.cb_pick_ctrl, self.cb_pick_alt, self.cb_pick_shift,
             self.cb_pick_win),
            self.pick_capture, self.chk_pick_enable)
        if pk not in ("off", "none", "false") and _parse_qt_combo(pk) is None:
            msgs.append("复制色值快捷键格式无效（示例：c）")
        else:
            self._cfg.cp.set("hotkey_ext", "color_pick", pk)
            msgs.append(f"复制色值：{pk}")

        # ---- 切换色值格式快捷键 ----
        fk = self._current_key_row(
            (self.cb_fmt_ctrl, self.cb_fmt_alt, self.cb_fmt_shift,
             self.cb_fmt_win),
            self.fmt_capture, self.chk_fmt_enable)
        if fk not in ("off", "none", "false") and _parse_qt_combo(fk) is None:
            msgs.append("切换色值格式快捷键格式无效（示例：h）")
        else:
            self._cfg.cp.set("hotkey_ext", "color_fmt", fk)
            msgs.append(f"切换色值格式：{fk}")

        # ---- 滚轮缩放修饰键 ----
        self._cfg.cp.set("hotkey_ext", "zoom_modifier",
                         self.combo_zoom_mod.currentData())
        msgs.append(f"滚轮缩放：{self.combo_zoom_mod.currentText()}")

        # ---- 分段快捷键（截图选择/贴图窗口/标注，可配置）----
        for _mods, _cap, _chk, _sec, _key in getattr(
                self, "_extra_hk_rows", []):
            self._cfg.cp.set(_sec, _key,
                             self._current_key_row(_mods, _cap, _chk))

        try:
            self._cfg.save()
            # 保存结果用托盘气泡通知（窗口内不再显示状态文字）
            self._notify(T("ui.msg_saved", "已保存"))
        except OSError as e:
            self._notify(T("ui.msg_save_fail", "保存失败：{}").format(e))