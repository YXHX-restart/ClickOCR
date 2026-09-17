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

from pathlib import Path

from core_engine.logging_setup import get_logger

log = get_logger("settings")

from PySide6.QtWidgets import (QFormLayout, QHBoxLayout, QVBoxLayout,
                               QTabWidget, QWidget, QPushButton,
                               QApplication, QScrollArea, QFrame)

from config import ConfigManager
from ui.base.card_window import CardWindow
from ui.base.texts import T
from ui.windows.settings.widgets import Problem   # 问题项消息标记（见 _save）
from ui.base.theme import apply_theme, resolve_theme
from ui.base.theme_dialog import ThemeDialog
from ui.windows.settings.software_tab import SoftwareTabMixin
from ui.windows.settings.ui_tab import UiTabMixin
from ui.windows.settings.hotkey_tab import HotkeyTabMixin


class SettingsPanel(SoftwareTabMixin, UiTabMixin, HotkeyTabMixin, CardWindow):
    def __init__(self, config: ConfigManager, hotkey_host=None,
                 history_storage=None, parent=None, tray=None,
                 on_clear_history=None, on_model_changed=None,
                 on_drop_ocr_changed=None):
        super().__init__(title=T("settings.title", "ClickOCR 设置"), with_close=True)
        self.setMinimumWidth(400)
        self._cfg = config
        # 保存流程里的配置旧值快照 {(sec,key): 旧值}：_set 记录、落盘失败时回滚
        # （见 _save / _set），初始为空以免采集方法被单独调用时报 AttributeError
        self._cfg_old: dict = {}
        self._hotkey = hotkey_host
        self._history = history_storage
        self._tray = tray            # 托盘（保存成功等用气泡通知）
        self._on_clear_history = on_clear_history  # App 回调：清空后刷新历史面板
        # 模型档位旧值：_save 检测变化后调 App 回调热切换引擎（仅重建内部引擎）
        self._model_set_old = self._cfg.get("software", "ocr_model", "v6-tiny")
        self._on_model_changed = on_model_changed  # App 回调：模型档位切换
        # App 回调：本页改了「拖入图片后自动 OCR」→ 请同步历史面板角标
        # （反向由 HistoryPanel.on_drop_ocr_changed 触发，见 sync_drop_ocr_checkbox）
        self._on_drop_ocr_changed = on_drop_ocr_changed

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


    # ---- 保存：先落盘、后生效 ----

    def _save(self):
        """保存设置（唯一编排入口）：**先写配置并落盘成功，再执行运行时副作用**。

        1) 采集：_save_software / _save_ui / _save_hotkey 只读控件 → 写内存配置，
           各自返回 (消息列表, 待生效副作用)；此阶段不产生任何运行时变化；
        2) 落盘：self._cfg.save()；失败 → 回滚内存配置 + 提示后直接返回，运行时
           与磁盘保持一致（原顺序是「先做副作用、最后 save」：磁盘写失败——磁盘
           满/目录只读——时运行时已经变了而配置没落盘，重启后静默丢失）；
        3) 生效：依次执行待生效副作用（换主题、改历史上限/目录、写自启注册表、
           热切换模型、注册热键），把它们产生的消息并入 msgs。
        """
        self._cfg_old = {}          # {(sec,key): 保存前的值}，落盘失败时整块回滚
        msgs, pending = [], []
        for collect in (self._save_software, self._save_ui, self._save_hotkey):
            _msgs, _pending = collect()
            msgs += _msgs
            pending += _pending
        try:
            self._cfg.save()
        except OSError as e:
            self._rollback_cfg()
            self._notify(T("ui.msg_save_fail", "保存失败：{}").format(e))
            return
        for effect in pending:
            msgs += effect() or []

        # 校验/执行类消息反馈：热键被占用、格式无效、自启失败等原本全部
        # 被吞掉、用户一律只看到「已保存」——信任级缺陷。按 `Problem` 标记挑出
        # 问题项弹窗提示（正常项仍走气泡，避免 30+ 条刷屏）。
        # 标记由产生处打（见 ui/windows/settings/widgets.Problem）；**不再按文案
        # 关键词反推** —— 那既会被正常文案里的词误伤，也会在 i18n 改措辞后静默失效。
        problems = [m for m in msgs if isinstance(m, Problem)]
        if problems:
            try:
                ThemeDialog.warning(
                    self, T("settings.title", "ClickOCR 设置"),
                    T("ui.msg_saved_with_issues",
                      "已保存，但以下设置存在问题：\n\n{}").format(
                          "\n".join(f"· {p}" for p in problems)))
            except Exception:  # noqa: BLE001 弹窗失败退回气泡
                self._notify(T("ui.msg_saved_with_issues_brief",
                               "已保存（{} 项设置存在问题）").format(
                                   len(problems)))
        else:
            # 保存结果用托盘气泡通知（窗口内不再显示状态文字）
            self._notify(T("ui.msg_saved", "已保存"))

    def _set(self, sec: str, key: str, value: str):
        """保存流程内写内存配置的**唯一**入口；顺带记下旧值供落盘失败时回滚。

        回滚为什么必要：不少配置是「读时生效」的（App.ocr_auto property、
        resolve_theme、历史目录等），落盘失败后内存里若留着新值，运行时行为已经
        变了而磁盘没变——重启即回到旧值（用户眼中的「静默丢失」）。
        """
        self._cfg_old.setdefault((sec, key),
                                 self._cfg.cp.get(sec, key, fallback=""))
        self._cfg.cp.set(sec, key, value)

    def _rollback_cfg(self):
        """落盘失败：把内存配置整块还原为本次保存前的值（与 _set 成对使用）。"""
        for (sec, key), old in self._cfg_old.items():
            self._cfg.cp.set(sec, key, old)
        self._cfg_old.clear()

    # ---- 采集各 Tab（只写配置，不产生运行时副作用）----

    def _save_software(self) -> tuple:
        """「软件设置」页：读控件 → 写内存配置，返回 (msgs, pending)。

        pending 是必须等配置**落盘成功**才允许执行的副作用（改历史上限/目录、
        写自启注册表、热切换模型），统一由 _save 在落盘后按序调用。
        """
        msgs, pending = [], []

        # ---- 历史保留条数（不设上限）----
        cap = self.spin_capacity.value()
        self._set("software", "history_max_count", str(cap))
        msgs.append(f"历史保留条数：{cap}")

        def _eff_cap():
            if self._history is not None:
                self._history.max_records = cap
            return []
        pending.append(_eff_cap)

        # ---- 历史图片位置（即时生效）----
        new_dir = self.edit_shot_dir.text().strip()
        self._set("software", "screenshots_dir", new_dir)
        msgs.append(f"历史图片位置：{new_dir or '默认 screenshots/'}")

        def _eff_dir():
            if self._history is None:
                return []
            target = Path(new_dir) if new_dir else \
                (self._cfg.app_root / "screenshots")
            target = target if target.is_absolute() \
                else (self._cfg.app_root / target)
            self._history.screenshots_dir = target
            return []
        pending.append(_eff_dir)

        # ---- 复制模式 ----
        self._set("software", "selection_mode",
                  self.combo_selection.currentData())
        msgs.append(f"复制模式：{self.combo_selection.currentData()}")

        # ---- 图片保存格式 / 质量 / 文件名规则（即时生效）----
        fmt = self.combo_save_fmt.currentData()
        self._set("software", "save_format", fmt)
        self._set("software", "save_quality",
                  str(self.slider_quality.value()))
        pattern = self.edit_name_pattern.text().strip()
        self._set("software", "name_pattern",
                  pattern if pattern else "{datetime}_{ms}")
        msgs.append(f"保存格式：{fmt.upper()} · 质量："
                    f"{self.slider_quality.value()} · 文件名规则："
                    f"{pattern or '{datetime}_{ms}'}")

        # ---- OCR 引擎常驻 / 截图后自动 OCR ----
        keep = self.chk_ocr_keep_alive.isChecked()
        self._set("software", "ocr_keep_alive",
                  "true" if keep else "false")
        msgs.append(T("ui.msg_ocr_keep", "OCR 常驻：") + ("开" if keep else "关"))
        auto = self.chk_ocr_auto.isChecked()
        self._set("software", "ocr_auto", "true" if auto else "false")
        msgs.append(T("ui.msg_ocr_auto", "自动 OCR：") + ("开" if auto else "关"))
        drop = self.chk_drop_ocr.isChecked()
        self._set("software", "drop_ocr_auto", "true" if drop else "false")
        msgs.append(T("ui.msg_drop_ocr", "拖入自动 OCR：")
                    + ("开" if drop else "关"))
        # ---- 二维码识别（[software] enable_qr）----
        qr_on = self.chk_qr.isChecked()
        self._set("software", "enable_qr", "true" if qr_on else "false")
        msgs.append("二维码识别：" + ("开" if qr_on else "关"))
        # 「拖入图片后自动 OCR」有两个入口（本页勾选框 / 历史面板标签栏角标），
        # 唯一真值源是 [software] drop_ocr_auto。落盘成功后通知 App 把**另一处**
        # 的显示刷成一致 —— 否则在设置里改完，历史面板角标仍停在旧状态，用户
        # 以为没生效，再去点一下反而又改回去（last-writer-wins）。
        def _eff_drop_ocr():
            if self._on_drop_ocr_changed is not None:
                self._on_drop_ocr_changed()
            return []
        pending.append(_eff_drop_ocr)
        # ---- 截图无操作也保存（[software] save_no_op_close）----
        noop = self.chk_save_no_op.isChecked()
        self._set("software", "save_no_op_close",
                  "true" if noop else "false")
        msgs.append(T("ui.msg_save_no_op", "无操作保存：")
                    + ("开" if noop else "关"))

        # ---- 开机自启动（M2-8：写注册表 = 运行时副作用，落盘后再执行）----
        autostart = self.chk_autostart.isChecked()

        def _eff_autostart():
            from core_engine.autostart import set_enabled as _set_auto
            if _set_auto(autostart):
                return [T("ui.msg_autostart", "开机自启：")
                        + ("开" if autostart else "关")]
            return [Problem(T("ui.msg_autostart_fail", "开机自启设置失败"))]
        pending.append(_eff_autostart)

        # ---- 截图贴图最大数量 ----
        self._set("software", "max_shot_windows",
                  str(self.spin_max_shot.value()))
        msgs.append(f"截图贴图：{self.spin_max_shot.value()}")
        # ---- 历史贴图最大数量（从历史恢复的图）----
        self._set("software", "max_restore_windows",
                  str(self.spin_max_hist.value()))
        msgs.append(f"历史贴图：{self.spin_max_hist.value()}")
        # ---- 钉图数量上限 ----
        self._set("software", "max_pin_windows",
                  str(self.spin_max_pin.value()))
        msgs.append(f"钉图上限：{self.spin_max_pin.value()}")

        # ---- GPU 加速（[software] ocr_device，重启生效）----
        dev = self.combo_accel.currentData()
        self._set("software", "ocr_device", dev)
        msgs.append(f"GPU 加速：{self.combo_accel.currentText()}")

        # ---- OCR 模型档位（[software] ocr_model，保存即热切换）----
        new_model = self.combo_model.currentData()
        self._set("software", "ocr_model", new_model)
        msgs.append(f"OCR 模型：{self.combo_model.currentText()}")

        def _eff_model():
            if new_model == self._model_set_old:
                return []
            self._model_set_old = new_model   # 先更新旧值，避免重复触发
            if self._on_model_changed is not None:
                try:
                    self._on_model_changed(new_model)
                except Exception:       # noqa: BLE001 切换失败已在 App 内提示
                    log.exception("模型档位切换回调失败: %s", new_model)
            return []
        pending.append(_eff_model)

        return msgs, pending

    def _save_ui(self) -> tuple:
        """「UI设置」页：读控件 → 写内存配置，返回 (msgs, pending)。

        本页唯一的运行时副作用是换主题（apply_theme），同样延后到落盘之后。
        """
        msgs, pending = [], []

        # ---- 主题（即时生效）----
        theme = self.combo_theme.currentData()
        self._set("ui", "theme", theme)
        msgs.append(f"主题：{theme}")

        def _eff_theme():
            app = QApplication.instance()
            if app is not None:
                apply_theme(app, resolve_theme(self._cfg))
            return []
        pending.append(_eff_theme)

        # ---- 历史面板：宽度 / 显示卡片数 ----
        self._set("ui", "history_panel_width",
                  str(self.spin_hist_width.value()))
        self._set("ui", "history_visible_cards",
                  str(self.spin_hist_cards.value()))
        msgs.append(f"历史面板：{self.spin_hist_width.value()}px / "
                    f"显示 {self.spin_hist_cards.value()} 张")

        # ---- 截图框选边框 ----
        self._set("ui", "border_color",
                  ",".join(map(str, self.btn_sel_color.color)))
        self._set("ui", "border_width",
                  str(self.spin_sel_width.value()))
        msgs.append(f"框选边框：{self.btn_sel_color.color} "
                    f"{self.spin_sel_width.value()}px")

        # ---- 结果图片边框 ----
        self._set("ui", "overlay_border_color",
                  ",".join(map(str, self.btn_overlay_color.color)))
        self._set("ui", "overlay_border_width",
                  str(self.spin_overlay_width.value()))
        msgs.append(f"窗口边框：{self.btn_overlay_color.color} "
                    f"{self.spin_overlay_width.value()}px")

        # ---- 二维码边框 / 表格边框颜色 + 线宽 ----
        self._set("ui", "qr_border_color",
                  ",".join(map(str, self.btn_qr_color.color)))
        self._set("ui", "qr_border_width",
                  str(self.spin_qr_width.value()))
        msgs.append(f"二维码边框：{self.btn_qr_color.color} "
                    f"{self.spin_qr_width.value()}px")
        self._set("ui", "table_border_color",
                  ",".join(map(str, self.btn_table_color.color)))
        self._set("ui", "table_border_width",
                  str(self.spin_table_width.value()))
        msgs.append(f"表格边框：{self.btn_table_color.color} "
                    f"{self.spin_table_width.value()}px")

        # ---- 识别文字蓝色框默认显示 ----
        self._set("ui", "show_text_boxes",
                  "true" if self.chk_text_boxes.isChecked() else "false")
        msgs.append(T("ui.msg_text_boxes", "文字框：") + ("显示" if self.chk_text_boxes.isChecked() else "隐藏"))

        # ---- 截图取色器参数（像素框/倍率）----
        self._set("ui", "picker_size",
                  str(self.spin_picker_size.value()))
        self._set("ui", "picker_zoom",
                  str(self.spin_picker_zoom.value()))
        msgs.append(f"取色器：{self.spin_picker_size.value()}×"
                    f"{self.spin_picker_zoom.value()}")

        # ---- 统一工具栏全局开关 ----
        self._set("ui", "toolbar_enabled",
                  "true" if self.chk_toolbar.isChecked() else "false")
        msgs.append(T("ui.msg_toolbar", "工具栏：") + ("开" if self.chk_toolbar.isChecked() else "关"))

        # ---- 纯信息状态栏全局开关 ----
        self._set("ui", "strip_enabled",
                  "true" if self.chk_strip.isChecked() else "false")
        msgs.append(T("ui.msg_strip", "状态栏：") + ("开" if self.chk_strip.isChecked() else "关"))

        # ---- 标注栏默认开启 ----
        self._set("ui", "anno_toolbar_default",
                  "true" if self.chk_anno_default.isChecked() else "false")
        msgs.append(T("ui.msg_anno_default", "标注栏默认：") + ("开" if self.chk_anno_default.isChecked() else "关"))

        # ---- 标注工具参数永久化记录 ----
        self._set("ui", "persist_params",
                  "true" if self.chk_anno_persist.isChecked() else "false")
        msgs.append(T("ui.msg_anno_persist", "标注参数持久化：") +
                    ("开" if self.chk_anno_persist.isChecked() else "关"))

        return msgs, pending

    def _save_hotkey(self) -> tuple:
        """「快捷键设置」页：写键位配置，返回 (msgs, pending)。

        局部快捷键行（parser 行）只写配置，属采集阶段；全局热键行的 apply
        （hotkey_tab.py 的 apply_shot / _secondary_apply）把「注册/注销 +
        冲突检测 + 写 config」绑在一起、且只在注册成功时才写 config——本文件
        无法拆开，故整行延后：落盘成功后再执行它（见 _apply_hotkeys）。
        如需在别处复用，注意 apply 的返回值是消息列表。
        """
        msgs, pending = [], []
        global_rows = []          # [(apply, combo)]：注册动作延后到落盘之后

        # ---- 所有快捷键行（统一控件，逐行读取 combo()）----
        # 全局热键行走 apply（冲突检测 + 注册/注销到 HotkeyHost + 写 config）；
        # 局部行走 parser 校验后写 config。原先散落在这里的一大段热键处理
        # 代码（截图/全屏/历史/复制/取色/格式 各自来一遍）全部收敛于此循环。
        for (row, _lbl, _sec, _key, parser, invalid, apply) in getattr(
                self, "_hk_rows", []):
            combo = row.combo()
            if apply is not None:
                global_rows.append((apply, combo))
            elif parser is not None:
                if combo not in ("off", "none", "false") \
                        and parser(combo) is None:
                    msgs.append(Problem(invalid or f"{_lbl} 快捷键格式无效"))
                else:
                    self._set(_sec, _key, combo)
                    msgs.append(f"{_lbl}：{combo}")
            else:
                self._set(_sec, _key, combo)
                msgs.append(f"{_lbl}：{combo}")

        # ---- 滚轮缩放修饰键 ----
        self._set("hotkey_overlay", "zoom_modifier",
                  self.combo_zoom_mod.currentData())
        msgs.append(f"滚轮缩放：{self.combo_zoom_mod.currentText()}")
        self._set("hotkey_overlay", "geom_zoom_modifier",
                  self.combo_geom_zoom_mod.currentData())
        msgs.append(f"窗口缩放：{self.combo_geom_zoom_mod.currentText()}")

        pending.append(lambda rows=global_rows: self._apply_hotkeys(rows))
        return msgs, pending

    def _apply_hotkeys(self, rows) -> list:
        """落盘后执行全局热键的 apply（注册/注销），并让键位落盘。

        apply 内部在注册成功时才会写 config，而那次写入发生在首次 save() 之后，
        故这里执行完再补一次 save()：首次 save 已成功 ⇒ 磁盘可写，这次失败只
        影响键位本身（并在消息里报出来），不会波及其余设置。
        """
        msgs = []
        for apply_fn, combo in rows:
            msgs += apply_fn(combo) or []
        if rows:
            try:
                self._cfg.save()
            except OSError as e:
                msgs.append(Problem(T("ui.msg_save_fail", "保存失败：{}").format(e)))
        return msgs


# 对外再导出（保持 `from ui.windows.settings_panel import KeyCaptureEdit` 等旧用法）
from ui.windows.settings.widgets import KeyCaptureEdit, _ModBox, _ShortcutRowEdit  # noqa: F401,E402
