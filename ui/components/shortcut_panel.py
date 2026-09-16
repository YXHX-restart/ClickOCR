"""快捷键速查面板（统一浮层基类 CardWindow，明/暗主题由全局 QSS 提供）。

按「状态」分段展示全部快捷键：全局热键 / 截图选择 / 贴图窗口 / 标注模式。
可配置的键位（截图、全屏、历史、全复制、复制色值、切换格式）读取 config 实际值。
点击任意处或 Esc 关闭。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QHBoxLayout, QApplication

from ui.base.card_window import CardWindow
from ui.base.texts import T


def _fmt_combo(c: str) -> str:
    """'ctrl+shift+r' -> 'Ctrl+Shift+R'；off/空 -> 「未启用」。"""
    if not c or c.lower() in ("off", "none", "false"):
        return T("shortcut.off", "未启用")
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}
    parts = [p.strip().lower() for p in c.replace("+", " ").split() if p.strip()]
    return "+".join(names.get(p, "`" if p == "`" else p.upper()) for p in parts)


def build_shortcut_rows(cfg=None) -> list:
    """全部快捷键行：(分组, 键位, 说明)。可配置的取 config 实际值。"""

    def get(sec, key, default):
        try:
            return cfg.get(sec, key, default) if cfg is not None else default
        except Exception:                     # noqa: BLE001
            return default

    rows = []
    # ---- 全局热键（任意时刻有效，设置里可配置）----
    rows += [
        ("global", _fmt_combo(get("hotkey_global", "screenshot_combo",
                                  "ctrl+shift+r")),
         T("shortcut.desc_screenshot", "触发截图")),
        ("global", _fmt_combo(get("hotkey_global", "fullscreen_combo", "off")),
         T("shortcut.desc_fullscreen", "全屏截图")),
        ("global", _fmt_combo(get("hotkey_global", "history", "ctrl+`")),
         T("shortcut.desc_history", "打开历史记录")),
        ("global", _fmt_combo(get("hotkey_global", "through", "alt+f")),
         T("shortcut.desc_through", "切换贴图/钉图窗口点击穿透（鼠标落到桌面，再按取消）")),
    ]
    # ---- 截图选择（截图遮罩中）----
    rows += [
        ("select", "按下拖动", T("shortcut.desc_drag", "框选区域")),
        ("select", _fmt_combo(get("hotkey_select", "full_sel", "a")),
         T("shortcut.desc_full_sel", "框选整个屏幕")),
        ("select", _fmt_combo(get("hotkey_select", "scroll_sel", "r")),
         T("shortcut.desc_scroll_sel",
           "开启/退出滚动截图（框选后：左键自动滚动 / 右键退出 / 滚轮手动）")),
        ("select", _fmt_combo(get("hotkey_select", "color_pick", "c")),
         T("shortcut.desc_pick_color", "复制光标处像素色值")),
        ("select", _fmt_combo(get("hotkey_select", "color_fmt", "h")),
         T("shortcut.desc_fmt_color", "切换色值显示格式")),
        ("select", "Shift（按住）", T("shortcut.desc_slow", "放慢鼠标精细框选")),
        ("select", "Alt（按住）", T("shortcut.desc_ratio", "等比例调节窗口（锁定/解锁）")),
        ("select", "Esc / 右键", T("shortcut.desc_cancel_sel", "取消截图")),
    ]
    # ---- 贴图窗口（截图结果 / 历史图窗口）----
    rows += [
        ("overlay", "双击", T("shortcut.desc_dbl", "选中整行")),
        ("overlay", "Ctrl+单击/拖拽", T("shortcut.desc_append", "追加多区域选区")),
        ("overlay", _fmt_combo(get("hotkey_overlay", "copy_selected", "ctrl+c")),
         T("shortcut.desc_copy_sel", "复制选中文字")),
        ("overlay", _fmt_combo(get("hotkey_overlay", "copy_shortcut", "shift+c")),
         T("shortcut.desc_copy_all", "复制全部文字")),
        ("overlay", _fmt_combo(get("hotkey_overlay", "select_all", "ctrl+a")),
         T("shortcut.desc_select_all", "全选文字（仅选中）")),
        ("overlay", _fmt_combo(get("hotkey_overlay", "copy_image", "ctrl+shift+c")),
         T("shortcut.desc_copy_image", "复制整张图片到剪贴板")),
        ("overlay", _fmt_combo(get("hotkey_overlay", "copy_raw_image", "ctrl+alt+c")),
         T("shortcut.desc_copy_raw_image", "复制原图（不含标注）")),
        ("overlay", _fmt_combo(get("hotkey_overlay", "reocr", "ctrl+r")),
         T("shortcut.desc_reocr", "重新 OCR")),
        ("overlay", _fmt_combo(get("hotkey_overlay", "toggle_qr", "q")),
         T("shortcut.desc_qr", "二维码识别开关")),
        ("overlay", _fmt_combo(get("hotkey_overlay", "table_ocr", "ctrl+shift+t")),
         T("shortcut.desc_table", "表格识别")),
        ("overlay", _fmt_combo(get("hotkey_overlay", "toggle_anno", "ctrl+shift+d")),
         T("shortcut.desc_anno", "进入/退出标注模式")),
        ("overlay", (_zr := str(get("hotkey_overlay", "zoom_modifier",
                                    "ctrl")).lower()) == "off" and "未启用"
         or f"{_zr.upper()}+滚轮",
         T("shortcut.desc_zoom_img", "图片缩放（窗口内，直接滚轮 = 平移）")),
        ("overlay", (_gz := str(get("hotkey_overlay", "geom_zoom_modifier",
                                    "shift")).lower()) == "off" and "未启用"
         or f"{_gz.upper()}+滚轮",
         T("shortcut.desc_zoom_win", "窗口几何缩放")),
        ("overlay", "Alt+拖动", T("shortcut.desc_move", "移动贴图（跳过选取）")),
        ("overlay", "中键拖动", T("shortcut.desc_middle_pan", "平移查看图片")),
        ("overlay", _fmt_combo(get("hotkey_overlay", "nav", "m")),
         T("shortcut.desc_navigator", "导航图开关（缩略小地图）")),
        ("overlay", _fmt_combo(get("hotkey_overlay", "ratio", "b")),
         T("shortcut.desc_ratio", "等比例调节窗口（锁定/解锁）")),
        ("overlay", "Esc", T("shortcut.desc_clear_sel", "清除选区")),
        ("overlay", "Esc / ✕", T("shortcut.desc_close", "关闭贴图")),
        ("overlay", "⊛", T("shortcut.desc_pin", "钉图（收起/恢复）")),
        ("overlay", "☆", T("shortcut.desc_fav", "收藏截图")),
    ]
    # ---- 标注模式 ----
    rows += [
        ("anno", _fmt_combo(get("hotkey_anno", "undo", "ctrl+z")),
         T("shortcut.desc_undo", "撤销")),
        ("anno", _fmt_combo(get("hotkey_anno", "redo", "ctrl+shift+z")),
         T("shortcut.desc_redo", "重做")),
        ("anno", "Q W E R T Y U I O",
         T("shortcut.desc_tool_keys", "切换标注工具（按位置，第1~9个，O 预留）")),
        ("anno", "Esc", T("shortcut.desc_exit_anno", "退出标注模式")),
        ("anno", "工具栏", T("shortcut.desc_tools",
                            "矩形 / 直线 / 箭头 / 文字 / 马赛克")),
    ]
    return rows


# 分组名 → 文字表 key
_GROUP_TEXT = {
    "global": ("shortcut.group_global", "全局热键"),
    "select": ("shortcut.group_select", "截图选择"),
    "overlay": ("shortcut.group_overlay", "贴图窗口"),
    "anno": ("shortcut.group_anno", "标注模式"),
}


class ShortcutPanel(CardWindow):
    """半透明浮层：按状态分段展示全部快捷键。"""

    def __init__(self, rows: list | None = None,
                 title: str | None = None, cfg=None):
        super().__init__(title=title or T("shortcut.title", "操作一览"),
                         with_close=False)
        self.setFixedWidth(560)
        rows = rows if rows is not None else build_shortcut_rows(cfg)

        self.card_lay.setSpacing(4)
        last = None
        for group, key, desc in rows:
            if group != last:
                k, dflt = _GROUP_TEXT.get(group, (group, group))
                g = QLabel(T(k, dflt))
                g.setProperty("kind", "group")
                self.card_lay.addWidget(g)
                last = group
            row = QHBoxLayout()
            k = QLabel(key)
            k.setProperty("kind", "key")
            k.setMinimumWidth(140)
            d = QLabel(desc)
            d.setProperty("kind", "desc")
            row.addWidget(k)
            row.addWidget(d, 1)
            self.card_lay.addLayout(row)

        self.adjustSize()
        # 分段较多：超过屏幕高度时限制高度（可滚动由外层处理）
        scr = QApplication.primaryScreen()
        scr_h = scr.availableGeometry().height() if scr else 800
        if self.height() > scr_h - 80:
            self.setFixedHeight(scr_h - 80)

    def mousePressEvent(self, _e):
        self.close()
