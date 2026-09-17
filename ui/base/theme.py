"""ui.base.theme — UI 主题（工具面板全局 QSS）+ 配色数据加载。

架构（三层，只有第一层需要人写）：
  1. 语义槽   ui/base/themes.toml 的 [themes.<名>]——**唯一真值源**。
              组件不写色值，一律引用槽名。
  2. 派生槽   [themes.<名>.derived]——由语义槽算出（低 alpha 叠加等）。
              写成规则而不是写死值，防止 hover/选中底将来再次各自漂移。
  3. 模板     本文件的 _build_qss()——127 条规则只写一份，各主题仅槽取值不同。
  → 新增主题 = 在 themes.toml 里加一段，本文件不用改。

设计：
- 主题只作用于工具面板（SettingsPanel / HistoryPanel / ShortcutPanel 等），
  用 QSS **ID 选择器**（objectName）限定作用域，**不影响截图浮窗**
  （ResultOverlay / SelectionOverlay 保持自绘外观）。
- 注意：QSS 逗号分隔的选择器不会把后缀选择器分发给每一项，所以
  后代规则必须逐面板展开（见 `_panels()`），否则只有最后一个面板匹配。
- 截图浮窗上的控件（角标按钮 / 关闭钮 / 钉图徽标 / 标注工具栏）**刻意固定深色**，
  因为要压在任意截图之上、白底图也要可读——它们在模板里保留字面量色值，
  白名单见 scripts/_verify_theme_tokens.py 的 TEMPLATE_LITERALS。
- `resolve_theme(config)` 把配置值解析成主题名（主题名本身或 system）；
  system 模式通过 QStyleHints.colorScheme 跟随系统深浅色（Qt 6.5+），
  深浅色分别映射到哪个主题见 themes.toml 的 [meta]。
- 回归守卫：scripts/_verify_theme_tokens.py（结构冻结 + 色值快照 + 槽完整性）。
  用法诊断：scripts/_p0_usage_audit.py（按实际 CSS 属性核对槽归属）。
"""
from __future__ import annotations

import os
import tomllib

from core_engine.logging_setup import get_logger

log = get_logger("theme")

# 「跟随系统」的配置取值（判据见 is_system_mode）
SYSTEM_MODE = "system"

_PANEL_NAMES = ("HistoryPanel", "SettingsPanel", "ShortcutPanel",
                "ColorPickerDialog", "StatusStrip", "ToolBar",
                "TablePreviewPanel",
                # 钉图悬停信息卡片：同为「无边框半透明卡片 + kind 标签」结构，
                # 原先没登记 → 内层 QFrame#card 拿不到背景、kind 标签读不到
                # 主题色（浅色主题下文字几乎不可读）。结构一致就该同一份规则。
                "PinCard",
                "ThemeDialog")


def _panels(sel: str = "") -> str:
    """生成 ID 选择器列表：sel 为空 = 面板自身；否则为每个面板的后代选择器。

    例：_panels("QLabel") -> "#HistoryPanel QLabel, #SettingsPanel QLabel,
    #ShortcutPanel QLabel"（逐面板展开，逗号不会把后缀分发给每项）。
    """
    if sel:
        return ", ".join(f"#{n} {sel}" for n in _PANEL_NAMES)
    return ", ".join(f"#{n}" for n in _PANEL_NAMES)


def _sel(name: str, sel: str = "") -> str:
    """单个面板选择器：#<name> 或 #<name> <sel>。"""
    return f"#{name} {sel}" if sel else f"#{name}"


# 标注子选项浮层（各工具点击后弹出，内含颜色 + 大小滑块）。
# 这些浮层挂在截图浮窗（ResultOverlay）的工具工具栏下，不属 _PANEL_NAMES，
# 但同样需要主题化的滑块外观。
_POPUP_NAMES = ("FontPopup", "MosaicModePopup", "ToolOptionsPopup", "NumPopup")


def _popups(sel: str = "") -> str:
    """生成标注子选项浮层的 ID 选择器列表（逐项展开，同 _panels）。"""
    if sel:
        return ", ".join(f"#{n} {sel}" for n in _POPUP_NAMES)
    return ", ".join(f"#{n}" for n in _POPUP_NAMES)


# 下拉箭头 PNG（Qt QSS 的 ::down-arrow 只支持 image，不支持 CSS 三角；
# 程序启动时用 QPainter 生成主题色三角，避免引入外部图片资源）。
_ARROW_CACHE: dict = {}


def _color_tag(color: str) -> str:
    """颜色 → 安全的文件名片段（如 "#bdbdbd" → "bdbdbd"）。

    用于临时 PNG 的确定性命名：同一颜色始终映射到同一文件，
    既不重复生成、也不会因哈希碰撞复用错误图标。
    """
    return "".join(c for c in str(color) if c.isalnum()) or "default"


def _arrow_path(color: str) -> str:
    """生成指定颜色的向下三角 PNG（临时目录），返回绝对路径（正斜杠）。"""
    key = color
    if key not in _ARROW_CACHE:
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtGui import QPixmap, QPainter, QPolygon, QColor
        import tempfile
        # 文件名 = 颜色本身（确定性）。原用 abs(hash(color)) % 100000：str 的
        # hash() 按进程随机化（PYTHONHASHSEED），同一颜色每次启动都算出不同
        # 文件名 → os.path.exists 永不命中，%TEMP% 每次启动累积新 PNG；
        # 且取模碰撞时会让另一种颜色直接复用错误的既有图标。
        path = os.path.join(tempfile.gettempdir(),
                            f"clickocr_arrow_{_color_tag(color)}.png")
        if not os.path.exists(path):
            pm = QPixmap(14, 14)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            p.drawPolygon(QPolygon([QPoint(2, 5), QPoint(12, 5), QPoint(7, 11)]))
            p.end()
            pm.save(path)
        _ARROW_CACHE[key] = path.replace("\\", "/")
    return _ARROW_CACHE[key]


# 勾选标记 PNG（QSS QCheckBox::indicator:checked 的 image；白勾，两套主题共用）
_CHECK_CACHE: dict = {}


def _check_path(color: str) -> str:
    """生成勾选标记 PNG（临时目录），返回绝对路径（正斜杠）。"""
    key = color
    if key not in _CHECK_CACHE:
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QPixmap, QPainter, QPen, QColor, QPolygonF
        import tempfile
        path = os.path.join(tempfile.gettempdir(),
                            f"clickocr_check_{_color_tag(color)}.png")
        if not os.path.exists(path):
            pm = QPixmap(14, 14)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            pen = QPen(QColor(color), 2, Qt.SolidLine, Qt.RoundCap,
                       Qt.RoundJoin)
            p.setPen(pen)
            p.drawPolyline(QPolygonF([QPointF(2.5, 7.5), QPointF(5.5, 10.5),
                                      QPointF(11, 3.5)]))
            p.end()
            pm.save(path)
        _CHECK_CACHE[key] = path.replace("\\", "/")
    return _CHECK_CACHE[key]


# ---------------------------------------------------------------------------
# QSS 模板：结构与 127 条规则只写一份，各主题仅槽取值不同。
#
# 原先 DARK_QSS / LIGHT_QSS 是两段约 365 行的平行手写字符串，规则一一对应
# 但需人工保持同步——审查发现历史上已出现「某条规则只补进一套」的漂移
# （如标注浮层滑块规则），并催生了大量「本应复用却各自取值」的组件色。
# 现收敛为：唯一 f-string 模板 _build_qss() + themes.toml 里的语义槽。
#
# - 模板里的 {P.槽名} = 语义角色（不是色值）：多个组件引用同一槽，就自动同色。
# - 两套主题的差异只体现在 themes.toml 的槽取值里。
# - 截图浮窗上的固定深色控件保留字面量色值（见文件头说明）。
# - 注释统一为一份（Qt 忽略 QSS 注释，不影响渲染）。
# - 回归守卫：scripts/_verify_theme_tokens.py——去注释+去色值后与结构基线
#   （scripts/_p0_baseline_*.qss）逐字节一致；任何结构漂移都会被拦截。
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 配色数据（唯一真值源）：同目录的 themes.toml
#
# 三层分工，只有第 1 层需要人写：
#   1. 语义槽   —— toml 的 [themes.<名>]；组件不写色值，一律引用槽名
#   2. 派生槽   —— toml 的 [themes.<名>.derived]；由语义槽算出（低 alpha 叠加等），
#                  写成规则而不是写死值，防止 hover/选中底将来各自漂移
#   3. QSS 模板 —— _build_qss()，127 条规则只写一份，两套主题仅槽取值不同
#
# 新增主题 = 在 themes.toml 里加一段 [themes.<名字>]，本文件不用改。
# ---------------------------------------------------------------------------
_TOML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "themes.toml")


def _rgba(color: str) -> tuple:
    """解析 #rgb / #rrggbb / rgb() / rgba() → (r, g, b, a)。

    注意 QSS 的 alpha 是 **0~255 整数**（不是 CSS 的 0~1 小数）。同时兼容
    CSS 写法（小数或百分比），按 token 形态判断，避免 60 被当成 60.0 再乘 255。
    """
    c = str(color).strip()
    if c.startswith("#"):
        h = c[1:]
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
    # 显式校验形态：原先直接 c.index("(") —— themes.toml 写错一个槽（比如漏了
    # 括号或写成裸色名）会抛 `ValueError: substring not found`，既看不出是哪个
    # 槽、也看不出是什么值，且在启动路径上直接崩。
    if "(" not in c or ")" not in c:
        raise ValueError(
            f"主题色值格式非法：{color!r}（应为 #rgb / #rrggbb / rgb() / rgba()）")
    inner = c[c.index("(") + 1:c.rindex(")")]
    parts = [p.strip() for p in inner.split(",")]
    r, g, b = (int(float(p)) for p in parts[:3])
    if len(parts) < 4:
        a = 255
    else:
        tok = parts[3]
        if tok.endswith("%"):
            a = round(float(tok[:-1]) * 255 / 100)
        elif "." in tok:
            a = round(float(tok) * 255)
        else:
            a = int(float(tok))
    return (r, g, b, max(0, min(255, a)))


def _fmt(rgba: tuple) -> str:
    """(r,g,b,a) → QSS 色值；alpha=255 输出 #rrggbb，否则 rgba(...)。"""
    r, g, b, a = rgba
    if a >= 255:
        return "#%02x%02x%02x" % (r, g, b)
    return "rgba(%d,%d,%d,%d)" % (r, g, b, a)


def _derive(rule: dict, slots: dict) -> str:
    """按派生规则算出一个槽的色值（两种都是精确换算，无取整误差）。

    alpha = N      取源色的 RGB，换成指定 alpha
    alpha_add = N  在源色的 alpha 上加减 N
    """
    r, g, b, a = _rgba(slots[rule["from"]])
    if "alpha" in rule:
        return _fmt((r, g, b, int(rule["alpha"])))
    if "alpha_add" in rule:
        return _fmt((r, g, b, max(0, min(255, a + int(rule["alpha_add"])))))
    raise ValueError("未知派生规则：%r" % (rule,))


def _load_toml() -> dict:
    with open(_TOML_PATH, "rb") as f:
        return tomllib.load(f)


_RAW = _load_toml()
_META = _RAW["meta"]


class _Slots(dict):
    """让 QSS 模板能用 {P.<槽名>} 点号取值。"""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def _resolve(theme: str) -> _Slots:
    """把某主题的 toml 段落解析成 {槽名: 值}（含派生槽与非颜色片段）。"""
    conf = _RAW["themes"][theme]
    slots = {k: v for k, v in conf.items() if isinstance(v, str)}
    for name, rule in conf.get("derived", {}).items():
        slots[name] = _derive(rule, slots)
    slots.update(conf.get("fragments", {}))
    return _Slots(slots)


def theme_names() -> tuple:
    """themes.toml 里定义的全部主题名。"""
    return tuple(_RAW["themes"])


def theme_label(name: str) -> str:
    """主题的显示名（[themes.<名>.meta] label；缺省回退为主题名）。

    供设置页的主题下拉框使用——新增主题会自动出现在下拉里，无需改代码。
    """
    return str(_RAW["themes"].get(name, {}).get("meta", {}).get("label", name))


def system_label() -> str:
    """「跟随系统」在下拉框里的显示名。"""
    return str(_META.get("system_label", "system"))


_SLOT_CACHE: dict = {}
_QSS_CACHE: dict = {}


def slots_of(theme: str) -> _Slots:
    """取某主题的槽字典（按需解析 + 缓存）。"""
    if theme not in _SLOT_CACHE:
        _SLOT_CACHE[theme] = _resolve(theme)
    return _SLOT_CACHE[theme]


def qss_of(theme: str) -> str:
    """取某主题的 QSS（按需构建 + 缓存）。

    刻意不写成「导入时一次性构建全部主题」——那样代码里就得硬编码主题名，
    在 themes.toml 里改了主题名反而会让模块导入直接崩。
    """
    if theme not in _QSS_CACHE:
        _QSS_CACHE[theme] = _build_qss(slots_of(theme))
    return _QSS_CACHE[theme]


def _build_qss(P) -> str:
    """唯一 QSS 模板：127 条规则写一次，色值经语义槽 P 注入。

    P 为 slots_of(主题名) 返回的槽字典（含派生槽与非颜色片段）；
    {_panels(...)}/{_popups(...)}/{_sel(...)} 为选择器展开助手（见文件头）。
    """
    return f"""
{_panels()} {{ color: {P.text}; font-size: 13px; }}

/* 无边框半透明卡片（三个工具面板共用，历史面板同款；底色随主题，见 panel_card_bg） */
{_panels('#card')} {{
    background: {P.bg};
    border-radius: 10px;
}}

/* ---- 标签 ---- */
{_panels('QLabel')} {{ color: {P.text}; background: transparent; }}
{_panels("QLabel[kind='title']")} {{ font-size: 15px; font-weight: bold; color: {P.text}; }}
{_panels("QLabel[kind='group']")} {{ font-size: 12px; font-weight: bold; color: {P.accent_text}; }}
{_sel('SettingsPanel', "QLabel[kind='info']")} {{ font-size: 11px; color: {P.text_muted}; }}
{_sel('HistoryPanel', "QLabel[kind='text']")} {{ color: {P.text}; }}
{_sel('HistoryPanel', "QLabel[kind='info']")} {{ font-size: 11px; color: {P.text_muted}; }}
{_sel('HistoryPanel', "QLabel[kind='empty']")} {{ color: {P.text_faint}; }}
{_sel('HistoryPanel', "QLabel[kind='thumb']")} {{
    background: {P.surface}; border: 1px solid {P.border};
    border-radius: 4px; color: {P.text_faint};
}}
{_sel('ShortcutPanel', "QLabel[kind='key']")} {{ color: {P.warning}; font-family: Consolas; {P.diff_shortcut_key_weight}}}
{_sel('ShortcutPanel', "QLabel[kind='desc']")} {{ color: {P.text_muted}; }}

/* ---- 历史列表 ---- */
{_sel('HistoryPanel', '#grouphead')} {{ background: {P.accent_soft}; border-radius: 5px; }}
{_sel('HistoryPanel', '#row')} {{ background: {P.surface}; border-radius: 6px; }}
{_sel('HistoryPanel', '#row:hover')} {{ background: {P.control_hover}; }}
{_sel('HistoryPanel', '#row[selected="true"]')} {{ border: 1px solid {P.accent_text}; background: {P.selected}; }}

/* ---- 输入框 ---- */
{_panels('QLineEdit')} {{
    background: {P.control}; border: 1px solid {P.border};
    border-radius: 6px; padding: 4px 8px; color: {P.text};
}}
{_panels('QLineEdit:focus')} {{ border-color: {P.accent_text}; }}

/* ---- 按钮 ---- */
{_panels('QPushButton')} {{
    background: {P.control}; border: none; border-radius: 6px;
    padding: 5px 12px; color: {P.text};
}}
{_panels('QPushButton:hover')} {{ background: {P.control_hover}; }}
{_panels("QPushButton[kind='fav']")} {{
    padding: 0; font-size: 20px; color: {P.warning};
    background: transparent; border-radius: 0;
}}
{_panels("QPushButton[kind='fav']:hover")} {{ color: {P.warning_hover}; background: transparent; }}
{_sel('ToolBar', "QPushButton[kind='pin']")} {{
    padding: 0; font-size: 20px; color: {P.warning};
    background: transparent; border: none; border-radius: 0;
}}
{_sel('ToolBar', "QPushButton[kind='pin']")}:hover,
{_sel('ToolBar', "QPushButton[kind='pin']:checked")} {{ color: {P.warning_hover}; background: transparent; }}
{_sel('StatusStrip', "QPushButton[kind='lock']")} {{
    padding: 0; font-size: 14px; color: {P.text_muted};      
    background: transparent; border: none; border-radius: 0;
}}
{_sel('StatusStrip', "QPushButton[kind='lock']:hover")} {{ color: {P.text}; background: transparent; }}
{_sel('StatusStrip', "QPushButton[kind='lock']:checked")} {{ color: {P.text}; }}  
/* 锁定 ● 白 */
{_panels("QPushButton[kind='del']")} {{
    padding: 0; font-size: 13px; color: {P.danger}; background: transparent;
}}
{_panels("QPushButton[kind='del']:hover")} {{ background: {P.danger_soft}; }}
{_panels("QPushButton[kind='close']")} {{
    padding: 0 8px; font-size: 14px; color: {P.text_muted}; background: transparent;
}}
{_panels("QPushButton[kind='close']:hover")} {{
    color: {P.text}; background: {P.control_hover};
}}
{_panels("QPushButton[kind='spinner']")} {{
    padding: 0; font-size: 9px; border-radius: 3px; color: {P.text_muted};
    background: {P.control};
}}
{_panels("QPushButton[kind='spinner']:hover")} {{
    background: {P.control_hover}; color: {P.text};
}}

/* ---- 复选框（软件/UI 设置）---- */
{_panels('QCheckBox')} {{
    color: {P.text}; background: transparent; spacing: 6px;
}}
{_panels('QCheckBox::indicator')} {{
    width: 14px; height: 14px; border-radius: 3px;
    border: 1px solid {P.border_strong};{P.diff_qcheckbox_newline}background: {P.control};
}}
{_panels('QCheckBox::indicator:hover')} {{ border-color: {P.accent_text}; }}
{_panels('QCheckBox::indicator:checked')} {{
    background: {P.accent}; border-color: {P.accent};
    image: url("__CHECK__");
}}

/* ---- 滑块（设置面板：图片质量等，样式同线宽浮层）---- */
{_sel('SettingsPanel', 'QSlider::groove:horizontal')} {{
    height: 4px; background: {P.scrollbar}; border-radius: 2px;
}}
{_sel('SettingsPanel', 'QSlider::sub-page:horizontal')} {{
    background: {P.accent}; border-radius: 2px;
}}
{_sel('SettingsPanel', 'QSlider::handle:horizontal')} {{
    width: 14px; margin: -5px 0; border-radius: 7px; background: #fff;{P.diff_slider_handle_border}
}}

/* ---- 标注工具区（统一工具栏标注态展开，跟随主题：暗色）---- */
{_sel('ToolBar', '#anno_tools QPushButton')} {{
    background: {P.control}; border: none; border-radius: 5px;
    padding: 4px 9px; color: {P.text_muted}; font-size: 14px;
}}
{_sel('ToolBar', '#anno_tools QPushButton[tool]')} {{
    font-size: 14px; padding: 0; min-width: 24px; min-height: 24px;
}}
{_sel('ToolBar', '#anno_tools QPushButton:hover')} {{ background: {P.control_hover}; }}
{_sel('ToolBar', '#anno_tools QPushButton:checked')} {{ background: {P.accent}; color: #fff; }}
{_sel('ToolBar', '#anno_tools QLabel')} {{ color: {P.text_muted}; font-size: 14px; }}
{_sel('ToolBar', '#anno_tools QSpinBox')} {{
    background: {P.control}; border: 1px solid {P.border_strong};
    border-radius: 4px; color: {P.text}; padding: 1px 4px;
}}
{_sel('ToolBar', '#anno_tools QSpinBox')}::up-button,
{_sel('ToolBar', '#anno_tools QSpinBox::down-button')} {{
    width: 14px; background: {P.surface};
    border: none; border-radius: 2px;
}}
{_sel('ToolBar', '#anno_tools QFontComboBox')} {{
    background: {P.control}; border: 1px solid {P.border_strong};
    border-radius: 4px; color: {P.text}; padding: 1px 4px;
}}
{_sel('ToolBar', '#anno_tools QFontComboBox QAbstractItemView')} {{
    background: {P.surface}; color: {P.text}; border: 1px solid {P.border};
    selection-background-color: {P.selected};
}}
/* 统一工具栏右下角拉伸手柄（自绘斜线，透明底） */
{_sel('StatusStrip', '#resize_grip')} {{ background: transparent; }}

/* ---- 截图浮窗角标按钮（画布工具：固定深色，白图上也可读，不随明暗主题）---- */
#ResultOverlay QPushButton[kind='corner'],
{_sel('ToolBar', "QPushButton[kind='corner']")},
#SettingsPanel QPushButton[kind='corner'],
{_sel('HistoryPanel', "QPushButton[kind='corner']")} {{
    background: rgba(30,30,32,205); color: #eee;
    border: 1px solid rgba(255,255,255,70); border-radius: 5px;
    padding: 3px 10px; font-size: 12px;
}}
#ResultOverlay QPushButton[kind='corner']:hover,
{_sel('ToolBar', "QPushButton[kind='corner']")}:hover,
#SettingsPanel QPushButton[kind='corner']:hover,
{_sel('HistoryPanel', "QPushButton[kind='corner']:hover")} {{ background: rgba(55,55,60,220); }}
#ResultOverlay QPushButton[kind='corner']:checked,
{_sel('ToolBar', "QPushButton[kind='corner']")}:checked,
#SettingsPanel QPushButton[kind='corner']:checked,
{_sel('HistoryPanel', "QPushButton[kind='corner']:checked")} {{
    background: #3d6de0; color: #fff; border-color: #5b8cff;
}}

/* ---- 快捷键修饰键（紧凑：无勾选框、纯文字，选中高亮）---- */
{_sel('SettingsPanel', "QCheckBox[kind='keymod']")} {{
    spacing: 0; padding: 2px 4px; border-radius: 4px; color: {P.text_muted};
    background: transparent;
}}
{_sel('SettingsPanel', "QCheckBox[kind='keymod']::indicator")} {{
    width: 0; height: 0; border: none;
}}
{_sel('SettingsPanel', "QCheckBox[kind='keymod']:checked")} {{
    background: {P.selected}; color: {P.text}; border-radius: 4px;
}}

/* ---- 图片浮窗右上角圆形关闭按钮（固定深色半透明，白图上也可见）---- */
#overlay_close {{
    background: rgba(30,30,32,190); color: #fff;
    border: none; border-radius: 16px;
    font-size: 15px; padding: 0;
}}
#overlay_close:hover {{
    background: #e04444; color: #000;
}}
#overlay_close:pressed {{
    background: #b93a3a; color: #000;
}}

/* ---- 钉图圆形钉子（固定深色半透明，白图上也可见）---- */
#pin_badge_btn {{
    background: rgba(30,30,32,190); color: #ffd54f;
    border: 1px solid rgba(255,255,255,60); border-radius: 16px;
    font-size: 22px; padding: 0;
}}
#pin_badge_btn:hover {{
    background: rgba(55,55,60,220); color: #ffe082;
}}

/* ---- 下拉 / 数值 ---- */
{_sel('SettingsPanel', 'QComboBox')} {{
    background: {P.control}; border: 1px solid {P.border};
    border-radius: 6px; padding: 4px 8px; color: {P.text};
}}
{_sel('SettingsPanel', 'QComboBox QAbstractItemView')} {{
    background: {P.surface}; color: {P.text}; border: 1px solid {P.border};
    selection-background-color: {P.selected};
}}
{_sel('SettingsPanel', 'QSpinBox')} {{
    background: {P.control}; border: 1px solid {P.border};
    border-radius: 6px; padding: 2px 6px; color: {P.text};
}}

/* ---- 下拉箭头（主题化，替代系统原生）---- */
{_sel('SettingsPanel', 'QComboBox')}::drop-down,
{_sel('ToolBar', '#anno_tools QFontComboBox::drop-down')} {{
    subcontrol-origin: padding; subcontrol-position: top right;
    width: 18px; border: none;
    border-left: 1px solid {P.border};
    background: {P.surface};
    border-top-right-radius: 5px; border-bottom-right-radius: 5px;
}}
{_sel('SettingsPanel', 'QComboBox')}::down-arrow,
{_sel('ToolBar', '#anno_tools QFontComboBox::down-arrow')} {{
    image: url("__COMBO_ARROW__");
}}

/* ---- 标注子选项浮层内的滑块（音量条样式）----    滑块位于四个在用的标注浮层内；此前这些浮层没有 QSlider 规则，    滑块呈系统原生浅色外观、不随明暗主题切换 */
{_popups('QSlider::groove:horizontal')} {{
    height: 4px; background: {P.scrollbar}; border-radius: 2px;
}}
{_popups('QSlider::sub-page:horizontal')} {{
    background: {P.accent}; border-radius: 2px;
}}
{_popups('QSlider::handle:horizontal')} {{
    width: 14px; margin: -5px 0; border-radius: 7px; background: #fff;{P.diff_slider_handle_border}
}}

/* ---- 字体选择浮层（点「文字」工具弹出）---- */
#FontPopup #font_card {{ background: {P.bg}; border-radius: 6px; }}
#FontPopup QLabel {{ color: {P.text_muted}; font-size: 11px; }}
#FontPopup QFontComboBox {{
    background: {P.control}; border: 1px solid {P.border_strong};
    border-radius: 4px; color: {P.text}; padding: 1px 4px;
}}
#FontPopup QFontComboBox QAbstractItemView {{
    background: {P.surface}; color: {P.text}; border: 1px solid {P.border};
    selection-background-color: {P.selected};
}}
#FontPopup QComboBox {{
    background: {P.control}; border: 1px solid {P.border_strong};
    border-radius: 4px; color: {P.text}; padding: 1px 4px;
}}
#FontPopup QComboBox QAbstractItemView {{
    background: {P.surface}; color: {P.text}; border: 1px solid {P.border};
    selection-background-color: {P.selected};
}}
#FontPopup QPushButton {{
    background: {P.control}; border: 1px solid {P.border_strong};
    border-radius: 4px; color: {P.text}; font-size: 12px;
}}
#FontPopup QPushButton:hover {{ background: {P.control_hover}; }}
#FontPopup QPushButton:checked {{ background: #3d6de0; color: #fff; }}

/* ---- 马赛克模式选择浮层 ---- */
#MosaicModePopup #mosaic_card {{ background: {P.bg}; border-radius: 6px; }}
#ToolOptionsPopup #tool_options_card {{ background: {P.bg}; border-radius: 6px; }}
#ToolOptionsPopup QLabel, #FontPopup #font_card QLabel, #MosaicModePopup #mosaic_card QLabel {{ color: {P.text_muted}; font-size: 12px; }}
#ToolOptionsPopup QPushButton {{
    background: {P.control}; border: none; border-radius: 5px;
    padding: 0;
}}
#ToolOptionsPopup QPushButton:hover {{ background: {P.control_hover}; }}
#ToolOptionsPopup QPushButton:checked {{ background: {P.accent}; }}
#MosaicModePopup QPushButton {{
    background: {P.control}; border: none; border-radius: 5px;
    padding: 5px 14px; color: {P.text_muted}; font-size: 12px;
}}
#MosaicModePopup QPushButton:hover {{ background: {P.control_hover}; }}
#MosaicModePopup QPushButton:checked {{ background: {P.accent}; color: #fff; }}

/* ---- 序号选项浮层（起始序号 + 颜色/大小）---- */
#NumPopup #num_card {{ background: {P.bg}; border-radius: 6px; }}
#NumPopup QLabel {{ color: {P.text_muted}; font-size: 12px; }}
#NumPopup QPushButton {{
    background: {P.control}; border: none; border-radius: 5px;
    padding: 0;
}}
#NumPopup QPushButton:hover {{ background: {P.control_hover}; }}
#NumPopup QPushButton:checked {{ background: {P.accent}; }}
#NumPopup QSpinBox {{
    background: {P.control}; border: 1px solid {P.border_strong};
    border-radius: 4px; color: {P.text}; padding: 1px 4px;
}}
#NumPopup QSpinBox::up-button, #NumPopup QSpinBox::down-button {{
    width: 14px; background: {P.surface};
    border: none; border-radius: 2px;
}}

/* ---- Tab ---- */
{_sel('SettingsPanel', 'QTabWidget::pane')} {{ border: none; background: transparent; }}
{_sel('HistoryPanel', 'QTabWidget::pane')} {{ border: none; background: transparent; }}
{_sel('SettingsPanel', 'QTabBar::tab')} {{
    background: transparent; color: {P.text_muted}; padding: 7px 18px;
    border-bottom: 2px solid transparent;
}}
{_sel('SettingsPanel', 'QTabBar::tab:selected')} {{ color: {P.text}; border-bottom: 2px solid {P.accent_text}; }}
{_sel('SettingsPanel', 'QTabBar::tab:hover')} {{ color: {P.text}; }}
{_sel('HistoryPanel', 'QTabBar::tab')} {{
    background: transparent; color: {P.text_muted}; padding: 7px 18px;
    border-bottom: 2px solid transparent;
}}
{_sel('HistoryPanel', 'QTabBar::tab:selected')} {{ color: {P.text}; border-bottom: 2px solid {P.accent_text}; }}
{_sel('HistoryPanel', 'QTabBar::tab:hover')} {{ color: {P.text}; }}

/* ---- 列表滚动条 ---- */
{_sel('HistoryPanel', 'QListWidget')} {{ background: transparent; border: none; }}
{_sel('HistoryPanel', 'QListWidget::item')} {{ border: none; background: transparent; }}
{_sel('HistoryPanel', 'QListWidget::item:selected')} {{
    background: {P.selected}; border-radius: 6px;
}}
{_sel('HistoryPanel', 'QScrollBar:vertical')} {{
    background: transparent; width: 8px; margin: 2px;
}}
{_sel('HistoryPanel', 'QScrollBar::handle:vertical')} {{
    background: {P.scrollbar}; border-radius: 4px; min-height: 24px;
}}
#HistoryPanel QScrollBar::add-line:vertical,
{_sel('HistoryPanel', 'QScrollBar::sub-line:vertical')} {{ height: 0; }}
#HistoryPanel QScrollBar::add-page:vertical,
{_sel('HistoryPanel', 'QScrollBar::sub-page:vertical')} {{ background: transparent; }}

/* ---- 表格（TablePreviewPanel）暗色 ---- */
{_sel('TablePreviewPanel', 'QTableWidget')} {{
    background: {P.inset}; border: 1px solid {P.border};
    border-radius: 6px; color: {P.text}; gridline-color: {P.divider};
    selection-background-color: {P.accent}; selection-color: {P.on_accent};
}}
{_sel('TablePreviewPanel', 'QTableWidget::item')} {{ padding: 4px 8px; border: none; }}
{_sel('TablePreviewPanel', 'QTableWidget::item:selected')} {{
    background: {P.selected}; color: {P.text}; border-radius: 3px;
}}
{_sel('TablePreviewPanel', 'QHeaderView::section')} {{
    background: {P.surface}; color: {P.text_muted};
    border: none; border-right: 1px solid {P.border};
    border-bottom: 1px solid {P.border}; padding: 5px 8px;
}}
{_sel('TablePreviewPanel', 'QTableCornerButton::section')} {{
    background: {P.surface}; border: none;
}}
{_sel('TablePreviewPanel', 'QScrollBar:vertical')} {{
    background: transparent; width: 8px; margin: 2px;
}}
{_sel('TablePreviewPanel', 'QScrollBar::handle:vertical')} {{
    background: {P.scrollbar}; border-radius: 4px; min-height: 24px;
}}
#TablePreviewPanel QScrollBar::add-line:vertical,
{_sel('TablePreviewPanel', 'QScrollBar::sub-line:vertical')} {{ height: 0; }}
#TablePreviewPanel QScrollBar::add-page:vertical,
{_sel('TablePreviewPanel', 'QScrollBar::sub-page:vertical')} {{ background: transparent; }}
/* 统一工具栏：常驻按钮文字/标志统一字号（覆盖共享 corner/pin/fav 规则） */
{_sel('ToolBar', "QPushButton[kind='corner']")} {{ font-size: 14px; }}
{_sel('ToolBar', "QPushButton[kind='pin']")} {{ font-size: 14px; }}
{_sel('ToolBar', "QPushButton[kind='fav']")} {{ font-size: 14px; }}

/* ---- 设置面板滚动区（快捷键 tab）---- */
{_sel('SettingsPanel', 'QScrollArea')} {{ background: transparent; border: none; }}
{_sel('SettingsPanel', 'QScrollBar:vertical')} {{ background: transparent; width: 8px; margin: 0; }}
{_sel('SettingsPanel', 'QScrollBar::handle:vertical')} {{ background: {P.scrollbar}; border-radius: 4px; min-height: 30px; }}
{_sel('SettingsPanel', 'QScrollBar::handle:vertical:hover')} {{ background: {P.scrollbar_hover}; }}
#SettingsPanel QScrollBar::add-line:vertical, {_sel('SettingsPanel', 'QScrollBar::sub-line:vertical')} {{ height: 0; }}
#SettingsPanel QScrollBar::add-page:vertical, {_sel('SettingsPanel', 'QScrollBar::sub-page:vertical')} {{ background: transparent; }}


/* ---- 禁用态 ----
   QSS 一旦给控件设了 color，QPalette 的 Disabled 组就被覆盖（Qt 官方行为），
   因此禁用态必须显式声明，否则禁用控件看起来和可用的一模一样。 */
{_panels('QPushButton:disabled')} {{ color: {P.text_faint}; }}
{_panels('QToolButton:disabled')} {{ color: {P.text_faint}; }}
{_panels('QLabel:disabled')} {{ color: {P.text_faint}; }}
{_panels('QLineEdit:disabled')} {{ color: {P.text_faint}; }}
{_panels('QComboBox:disabled')} {{ color: {P.text_faint}; }}
{_panels('QCheckBox:disabled')} {{ color: {P.text_faint}; }}
{_panels('QSpinBox:disabled')} {{ color: {P.text_faint}; }}
"""


# 各主题的 QSS：按需构建（见 qss_of）。切换主题只是换字符串，不重新渲染。
# 这里刻意不硬编码主题名——主题清单完全由 themes.toml 决定。
DARK_QSS = qss_of(_META["system_dark"])
LIGHT_QSS = qss_of(_META["system_light"])



def is_system_dark() -> bool:
    """跟随系统：QStyleHints.colorScheme（Qt 6.5+，Windows 有效）。"""
    try:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtCore import Qt
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:  # noqa: BLE001
        return False


# 当前生效主题（apply_theme 时更新；供 icon_color()/accent_color() 取色）
_THEME = "dark"

# 主题变更回调注册表（弱引用）：apply_theme 只清 SVG 图标缓存，**已挂在
# 按钮上的 QIcon 不会被替换**——ToolBar/StatusStrip 等持有图标的组件在此
# 订阅刷新，否则主题切换后旧色图标残留（浅色主题下几乎不可见）
import weakref as _weakref

_theme_changed_callbacks: list = []


def on_theme_changed(cb):
    """注册主题变更回调（cb(theme)）。弱引用持有：组件销毁后自动失效。"""
    _theme_changed_callbacks.append(_weakref.WeakMethod(cb))


def _notify_theme_changed(theme: str):
    for w in list(_theme_changed_callbacks):
        cb = w()
        if cb is None:
            continue
        try:
            cb(theme)
        except Exception:  # noqa: BLE001 单个订阅者异常不影响其它
            pass
    # 顺手清理已失效弱引用
    _theme_changed_callbacks[:] = [
        w for w in _theme_changed_callbacks if w() is not None]


def current_theme() -> str:
    """当前生效主题名（themes.toml 里的键，如 dark|light）。"""
    return _THEME


def icon_color() -> str:
    """通用图标前景色（SVG 图标描边）。取自色板 icon 槽。"""
    return slots_of(_THEME).icon


def accent_color() -> str:
    """金色强调色（收藏星标/钉图钉子，与 QSS kind='fav'/'pin' 一致）。

    注意名字有历史包袱：它取的是**收藏金**（对应 warning 槽），不是蓝色强调色。
    蓝色强调色在模板里用 accent / accent_text 槽。
    """
    return slots_of(_THEME).warning


def theme_qss_selector_drift() -> tuple:
    """返回两套 QSS 的**选择器差异**（暗色有而亮色没有 / 反之）。

    两套 QSS 现在由**同一个模板**生成，理论上选择器必然一致；本函数作为
    回归守卫，防止将来有人给某个主题漏加规则。
    """
    def _sels(qss: str) -> set:
        out = set()
        for line in qss.splitlines():
            line = line.strip()
            # 只取「规则头」行：含 '{' 且不以注释/属性开头
            if "{" not in line or line.startswith("/*"):
                continue
            head = line.split("{", 1)[0].strip()
            if head:
                out.add(head)
        return out

    dark, light = _sels(DARK_QSS), _sels(LIGHT_QSS)
    return tuple(sorted(dark - light)), tuple(sorted(light - dark))


def is_system_mode(config) -> bool:
    """配置是否处于「跟随系统」模式。

    `resolve_theme` 与 `install_system_follow` **必须共用这一个判据**：
    原先前者是「不是已知主题名 → 当跟随系统」，后者是「等于 'system' 或空 →
    才算跟随系统」。两者在「配置里写了个不存在的主题名」时结论相反 ——
    表现为解析出来跟了系统，但系统深浅色切换时又不重应用（静默错配）。
    """
    t = str(config.get("ui", "theme", SYSTEM_MODE)).strip().lower()
    return t in ("", SYSTEM_MODE) or t not in theme_names()


def resolve_theme(config) -> str:
    """把配置值解析成实际主题名（themes.toml 里定义的键）。

    取值可以是主题名本身（dark / light / 将来新增的任意名字），
    或 "system"（跟随系统深浅色，映射见 themes.toml 的 [meta]）。
    未知取值按跟随系统处理，但会记一条 warning —— 否则把主题名拼错会静默降级。
    """
    t = str(config.get("ui", "theme", SYSTEM_MODE)).strip().lower()
    if t in theme_names():
        return t
    if t not in ("", SYSTEM_MODE):
        log.warning("配置 [ui] theme=%r 不是有效主题名（可用：%s），按跟随系统处理",
                    t, " / ".join(theme_names()))
    return _META["system_dark"] if is_system_dark() else _META["system_light"]


def apply_theme(app, theme: str):
    """给 QApplication 应用全局主题 QSS。

    同步更新当前主题状态并清空 SVG 图标缓存（图标色随主题重建），
    最后通知订阅者（on_theme_changed）刷新已挂在按钮上的旧色图标。
    """
    global _THEME
    if theme not in theme_names():
        theme = _META["default_theme"]
    _THEME = theme
    try:
        from . import icon as _icon
        _icon.clear_cache()
    except Exception:            # noqa: BLE001
        pass
    s = slots_of(theme)
    # 下拉箭头 / 勾选标记是运行时用 QPainter 生成的 PNG（QSS 的 ::down-arrow
    # 只吃 image），颜色同样从色板取，不再硬编码
    arrow = _arrow_path(s.text_muted)
    check = _check_path(s.on_accent)
    app.setStyleSheet(qss_of(theme)
                      .replace("__COMBO_ARROW__", arrow)
                      .replace("__CHECK__", check))
    _notify_theme_changed(_THEME)


def install_system_follow(app, config, on_change=None):
    """theme=system 时监听系统深浅色变化自动重应用。

    返回是否成功安装监听（不支持的平台返回 False，无副作用）。
    """
    try:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtCore import Qt

        hints = QGuiApplication.styleHints()

        def _handler(scheme):
            # 判据与 resolve_theme 共用 is_system_mode，避免两处结论相反
            if not is_system_mode(config):
                return
            theme = (_META["system_dark"] if scheme == Qt.ColorScheme.Dark
                     else _META["system_light"])
            apply_theme(app, theme)
            if on_change is not None:
                on_change(theme)

        hints.colorSchemeChanged.connect(_handler)
        return True
    except Exception:  # noqa: BLE001
        return False
