"""M2-4 设置面板：软件设置 / UI设置 / 快捷键设置 三个 Tab（统一浮层基类）。

2026-09-11 重构 S2：本文件原 1,438 行，按页签拆为四个 mixin/模块 ——

  - `settings/widgets.py`    通用小控件（_ModBox / _ShortcutRowEdit / KeyCaptureEdit）
  - `settings/software_tab.py`  SoftwareTabMixin（软件设置页 + GPU 探测）
  - `settings/ui_tab.py`        UiTabMixin（UI 设置页）
  - `settings/hotkey_tab.py`    HotkeyTabMixin（快捷键设置页）

拆分方式是**纯搬移**：行为零变化，`SettingsPanel` 对外接口不变，
`app.py` / 测试脚本一行都不用改。
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

from config import ConfigManager
from ui.base.card_window import CardWindow
from ui.base.texts import T
from ui.base.theme import apply_theme, resolve_theme
from ui.base.theme_dialog import ThemeDialog
from ui.base.widgets import (ThemeSpinBox, ColorButton as _ColorButton,
                             FitComboBox)
from ui.windows.settings.widgets import _ModBox, _ShortcutRowEdit, KeyCaptureEdit
from ui.windows.settings.software_tab import SoftwareTabMixin
from ui.windows.settings.ui_tab import UiTabMixin
from ui.windows.settings.hotkey_tab import HotkeyTabMixin


class SettingsPanel(SoftwareTabMixin, UiTabMixin, HotkeyTabMixin, CardWindow):
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


# 对外再导出（保持 `from ui.windows.settings_panel import KeyCaptureEdit` 等旧用法）
from ui.windows.settings.widgets import KeyCaptureEdit, _ModBox, _ShortcutRowEdit  # noqa: F401,E402
