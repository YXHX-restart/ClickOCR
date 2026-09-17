"""UiTabMixin —— 「UI设置」页（主题/截图框选/结果图边框）。

从 settings_panel.py 拆出（2026-09-11 重构 S2），纯搬移不改行为。
方法体里用到的 self.*（_cfg/_hotkey/_scroll_page/_form/_notify 等）都
由混入后的 SettingsPanel 提供，本 mixin 不单独实例化。
"""

from __future__ import annotations


from core_engine.logging_setup import get_logger

log = get_logger("settings")

from PySide6.QtWidgets import (QHBoxLayout, QWidget, QCheckBox, QComboBox,
                               QLabel)

from ui.base.texts import T
from ui.base.theme import (theme_names, theme_label, system_label)
from ui.base.widgets import (ThemeSpinBox, ColorButton as _ColorButton)


class UiTabMixin:
    def _build_ui_tab(self) -> QWidget:
        page, scroll, inner = self._scroll_page()
        w = inner
        form = self._form(w)

        # UI 页 6 个数值栏统一宽度（px）
        _W5 = 75

        # 界面主题：主题清单读自 ui/base/themes.toml，
        # 新增主题只要在 toml 里加一段，这里会自动多出一项（无需改代码）。
        # 文案优先走 i18n（ui.theme_<名>），没有则用 toml 里的 label。
        self.combo_theme = QComboBox()
        self.combo_theme.addItem(T("ui.theme_system", system_label()),
                                 "system")
        for _name in theme_names():
            self.combo_theme.addItem(
                T(f"ui.theme_{_name}", theme_label(_name)), _name)
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
        lbl_w = QLabel(T("ui.hist_w", "宽度"))
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

        # 截图取色器：放大镜像素框大小 / 放大倍率（最大 15×15）
        # 位置紧跟「历史面板」之下（按使用频次排列：面板尺寸 → 取色器 → 各边框）
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
            T("ui.overlay_border_hint", "宽度 0 = 不显示边框（[selection]/[result] 配置段）。"))

        # 二维码识别边框（[ui] qr_border_color，默认紫）+ 线宽（同截图边框的做法）
        self.btn_qr_color = _ColorButton(
            self._cfg.getcolor("ui", "qr_border_color", (156, 39, 176)))
        self.spin_qr_width = ThemeSpinBox(
            1, 10, self._cfg.getint("ui", "qr_border_width", 1),
            width=_W5)
        qr_row = QHBoxLayout()
        qr_row.setSpacing(4)
        qr_row.addWidget(self.btn_qr_color)
        qr_row.addWidget(self.spin_qr_width)
        qr_row.addStretch(1)
        form.addRow(T("ui.qr_border", "二维码边框"), qr_row)

        # 表格模式网格边框（[ui] table_border_color，默认绿）+ 线宽（同截图边框）
        self.btn_table_color = _ColorButton(
            self._cfg.getcolor("ui", "table_border_color", (90, 220, 120)))
        self.spin_table_width = ThemeSpinBox(
            1, 10, self._cfg.getint("ui", "table_border_width", 1),
            width=_W5)
        table_row = QHBoxLayout()
        table_row.setSpacing(4)
        table_row.addWidget(self.btn_table_color)
        table_row.addWidget(self.spin_table_width)
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
        self.chk_strip = QCheckBox(T("ui.strip", "状态栏"))
        self.chk_strip.setChecked(
            self._cfg.getbool("ui", "strip_enabled", True))
        self.chk_strip.setToolTip(
            T("ui.strip_hint", "状态栏只显示状态文字与选择预览；关闭后不显示（Esc 关闭后也可在此重新开启）"))
        form.addRow("", self.chk_strip)

        # 识别文字蓝色框默认显示
        self.chk_text_boxes = QCheckBox(T("ui.text_boxes", "OCR文字边框"))
        self.chk_text_boxes.setChecked(
            self._cfg.getbool("ui", "show_text_boxes", True))
        form.addRow("", self.chk_text_boxes)

        # 标注栏默认开启
        # 默认文案与 resources/ui_text.json 保持一致（i18n 守卫会比对二者）
        self.chk_anno_default = QCheckBox(T("ui.anno_bar", "标注工具栏"))
        self.chk_anno_default.setChecked(
            self._cfg.getbool("ui", "anno_toolbar_default", False))
        form.addRow("", self.chk_anno_default)

        # 标注工具参数永久化记录（颜色/线宽/字号/加粗/斜体/马赛克模式）
        self.chk_anno_persist = QCheckBox(T("ui.anno_persist", "记录标注参数"))
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
