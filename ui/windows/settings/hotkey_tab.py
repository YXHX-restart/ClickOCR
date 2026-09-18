"""HotkeyTabMixin —— 「快捷键设置」页（全局/全屏/历史/复制/局部热键 + 操作一览）。

从 settings_panel.py 拆出（2026-09-11 重构 S2），纯搬移不改行为。
方法体里用到的 self.*（_cfg/_hotkey/_scroll_page/_form/_notify 等）都
由混入后的 SettingsPanel 提供，本 mixin 不单独实例化。
"""

from __future__ import annotations


from core_engine.logging_setup import get_logger

log = get_logger("settings")

from PySide6.QtWidgets import (QVBoxLayout,
                               QWidget, QComboBox,
                               QLabel, QScrollArea, QFrame)

from ui.base.texts import T
from ui.windows.settings.widgets import Problem   # 问题项消息标记（见 settings_panel._save）
from ui.base.hotkey_keys import parse_qt_combo
from ui.windows.settings.widgets import _ShortcutRowEdit


class HotkeyTabMixin:
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

            parser：本地快捷键用 parse_qt_combo 校验；再次验证格式。
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
                        parser=parse_qt_combo,
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
                        _msgs.append(Problem(f"热键 {combo.upper()} 已被其它程序占用"))
                    elif self._hotkey.set_combo(combo):
                        self._cfg.cp.set(
                            "hotkey_global", "screenshot_combo", combo)
                        _msgs.append(f"热键已切换：{combo.upper()}")
                    else:
                        _msgs.append(Problem("热键切换失败（已回滚）"))
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
        add_hk_row("hk.toggle_through", "点击穿透", "hotkey_overlay",
                   "toggle_through", "alt+f")

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
        # 点击穿透的**全局键**（默认 Ctrl+Alt+F）：必须接 apply——原先只写 config，
        # 改键保存后新键不注册（重启才生效）、改 off 后旧键仍活着。
        # 应用级快捷键（上方「贴图窗口」组的 toggle_through）只覆盖本应用窗口，
        # 焦点在**其它软件**时只有这个全局键能收到；刻意不用 Alt+F（Windows 多数
        # 程序的「文件」菜单助记键）。
        apply_through = self._secondary_apply(
            4, "hotkey_global", "through", "点击穿透（全局键）",
            hint="ctrl+alt+f")
        _row("hk.through", "点击穿透（全局键）", "hotkey_global",
             "through", "ctrl+alt+f", apply=apply_through)

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
        """UI 入口：弹出快捷键速查面板（不占用全局快捷键），读实际配置键位。

        面板是**独立顶层窗**（无父），每次点「操作一览」都新建一个并覆盖
        `self._shortcut_panel`，旧实例失去 Python 引用后处于「还显示着但已经管不到」
        的状态；同时读的是构造时的配置快照，也不能简单复用旧实例
        （用户刚改完键位再打开会看到旧值）。故先显式关掉旧的再建新的。
        """
        from ui.components.shortcut_panel import ShortcutPanel
        old = getattr(self, "_shortcut_panel", None)
        if old is not None:
            try:
                old.close()
                old.deleteLater()
            except Exception as e:       # noqa: BLE001 关不掉不该挡住新面板
                log.warning("关闭旧的快捷键速查面板失败：%s", e)
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
                    msgs.append(Problem(f"{label}快捷键格式无效（示例：{hint}）"))
                else:
                    # 注册失败（已回滚到旧键）时**不写 config**：否则记录的是
                    # 未生效的新键，重启后再注册再失败 → 热键静默丢失，
                    # 且 UI 显示新键、实际生效旧键（对照主热键 apply_shot 的写法）
                    ok = True
                    if self._hotkey is not None:
                        if not self._hotkey.set_secondary_combo(hid, combo):
                            ok = False
                            msgs.append(Problem(f"{label}快捷键注册失败（被占用，已回滚）"))
                    if ok:
                        self._cfg.cp.set(sec, key, combo)
                        msgs.append(f"{label}快捷键：{combo}")
            return msgs

        return apply

