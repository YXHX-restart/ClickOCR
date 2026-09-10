"""ui.base.theme — 明/暗两套 UI 主题（工具面板全局 QSS）。

设计：
- 主题只作用于工具面板（SettingsPanel / HistoryPanel / ShortcutPanel），
  用 QSS **ID 选择器**（objectName）限定作用域，**不影响截图浮窗**
  （ResultOverlay / SelectionOverlay 保持自绘外观）。
- 注意：QSS 逗号分隔的选择器不会把后缀选择器分发给每一项，所以
  后代规则必须逐面板展开（见 `_panels()`），否则只有最后一个面板匹配。
- 暗色主题 = 历史面板同款：无边框半透明深色卡片 rgba(28,28,30,238) 圆角 10px。
- 亮色主题 = 浅色卡片风格。
- `resolve_theme(config)` 把配置（dark|light|system）解析成实际主题；
  system 模式通过 QStyleHints.colorScheme 跟随系统深浅色（Qt 6.5+）。
"""
from __future__ import annotations

import os

_PANEL_NAMES = ("HistoryPanel", "SettingsPanel", "ShortcutPanel",
                "ColorPickerDialog", "StatusStrip", "ToolBar",
                "TablePreviewPanel",
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


# 下拉箭头 PNG（Qt QSS 的 ::down-arrow 只支持 image，不支持 CSS 三角；
# 程序启动时用 QPainter 生成主题色三角，避免引入外部图片资源）。
_ARROW_CACHE: dict = {}


def _arrow_path(color: str) -> str:
    """生成指定颜色的向下三角 PNG（临时目录），返回绝对路径（正斜杠）。"""
    key = color
    if key not in _ARROW_CACHE:
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtGui import QPixmap, QPainter, QPolygon, QColor
        import tempfile
        path = os.path.join(tempfile.gettempdir(),
                            f"clickocr_arrow_{abs(hash(key)) % 100000}.png")
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
                            f"clickocr_check_{abs(hash(key)) % 100000}.png")
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


DARK_QSS = f"""
{_panels()} {{ color: #e8e8e8; font-size: 13px; }}

/* 无边框半透明深色卡片（三个工具面板共用，历史面板同款） */
{_panels('#card')} {{
    background: rgba(28, 28, 30, 238);
    border-radius: 10px;
}}

/* ---- 标签 ---- */
{_panels('QLabel')} {{ color: #e8e8e8; background: transparent; }}
{_panels("QLabel[kind='title']")} {{ font-size: 15px; font-weight: bold; color: #ffffff; }}
{_panels("QLabel[kind='group']")} {{ font-size: 12px; font-weight: bold; color: #8ab4ff; }}
{_sel('SettingsPanel', "QLabel[kind='info']")} {{ font-size: 11px; color: #9a9a9a; }}
{_sel('HistoryPanel', "QLabel[kind='text']")} {{ color: #ececec; }}
{_sel('HistoryPanel', "QLabel[kind='info']")} {{ font-size: 11px; color: #9a9a9a; }}
{_sel('HistoryPanel', "QLabel[kind='empty']")} {{ color: #777; }}
{_sel('HistoryPanel', "QLabel[kind='thumb']")} {{
    background: rgba(255,255,255,20); border: 1px solid rgba(255,255,255,40);
    border-radius: 4px; color: #777;
}}
{_sel('ShortcutPanel', "QLabel[kind='key']")} {{ color: #ffd54f; font-family: Consolas; }}
{_sel('ShortcutPanel', "QLabel[kind='desc']")} {{ color: #c8c8c8; }}

/* ---- 历史列表 ---- */
{_sel('HistoryPanel', '#grouphead')} {{ background: rgba(138,180,255,26); border-radius: 5px; }}
{_sel('HistoryPanel', '#row')} {{ background: rgba(255,255,255,16); border-radius: 6px; }}
{_sel('HistoryPanel', '#row:hover')} {{ background: rgba(255,255,255,30); }}
{_sel('HistoryPanel', '#row[selected="true"]')} {{ border: 1px solid #8ab4ff; background: rgba(138,180,255,45); }}

/* ---- 输入框 ---- */
{_panels('QLineEdit')} {{
    background: rgba(255,255,255,26); border: 1px solid rgba(255,255,255,50);
    border-radius: 6px; padding: 4px 8px; color: #eee;
}}
{_panels('QLineEdit:focus')} {{ border-color: #8ab4ff; }}

/* ---- 按钮 ---- */
{_panels('QPushButton')} {{
    background: rgba(255,255,255,30); border: none; border-radius: 6px;
    padding: 5px 12px; color: #eee;
}}
{_panels('QPushButton:hover')} {{ background: rgba(255,255,255,55); }}
{_panels("QPushButton[kind='fav']")} {{
    padding: 0; font-size: 20px; color: #ffd54f;
    background: transparent; border-radius: 0;
}}
{_panels("QPushButton[kind='fav']:hover")} {{ color: #ffe082; background: transparent; }}
#ToolBar QPushButton[kind='pin'] {{
    padding: 0; font-size: 20px; color: #ffd54f;
    background: transparent; border: none; border-radius: 0;
}}
#ToolBar QPushButton[kind='pin']:hover,
#ToolBar QPushButton[kind='pin']:checked {{ color: #ffe082; background: transparent; }}
#StatusStrip QPushButton[kind='lock'] {{
    padding: 0; font-size: 14px; color: #ddd;      /* 解锁 ○ 灰白 */
    background: transparent; border: none; border-radius: 0;
}}
#StatusStrip QPushButton[kind='lock']:hover {{ color: #fff; background: transparent; }}
#StatusStrip QPushButton[kind='lock']:checked {{ color: #ffffff; }}  /* 锁定 ● 白 */
{_panels("QPushButton[kind='del']")} {{
    padding: 0; font-size: 13px; color: #ff9e9e; background: transparent;
}}
{_panels("QPushButton[kind='del']:hover")} {{ background: rgba(255,158,158,40); }}
{_panels("QPushButton[kind='close']")} {{
    padding: 0 8px; font-size: 14px; color: #bbb; background: transparent;
}}
{_panels("QPushButton[kind='close']:hover")} {{
    color: #fff; background: rgba(255,255,255,40);
}}
{_panels("QPushButton[kind='spinner']")} {{
    padding: 0; font-size: 9px; border-radius: 3px; color: #ccc;
    background: rgba(255,255,255,30);
}}
{_panels("QPushButton[kind='spinner']:hover")} {{
    background: rgba(255,255,255,55); color: #fff;
}}

/* ---- 复选框（软件/UI 设置）---- */
{_panels('QCheckBox')} {{
    color: #e8e8e8; background: transparent; spacing: 6px;
}}
{_panels('QCheckBox::indicator')} {{
    width: 14px; height: 14px; border-radius: 3px;
    border: 1px solid rgba(255,255,255,90);
    background: rgba(255,255,255,26);
}}
{_panels('QCheckBox::indicator:hover')} {{ border-color: #8ab4ff; }}
{_panels('QCheckBox::indicator:checked')} {{
    background: #3d6de0; border-color: #5b8cff;
    image: url(__CHECK__);
}}

/* ---- 滑块（设置面板：图片质量等，样式同线宽浮层）---- */
#SettingsPanel QSlider::groove:horizontal {{
    height: 4px; background: rgba(255,255,255,60); border-radius: 2px;
}}
#SettingsPanel QSlider::sub-page:horizontal {{
    background: #3d6de0; border-radius: 2px;
}}
#SettingsPanel QSlider::handle:horizontal {{
    width: 14px; margin: -5px 0; border-radius: 7px; background: #fff;
}}

/* ---- 标注工具区（统一工具栏标注态展开，跟随主题：暗色）---- */
#ToolBar #anno_tools QPushButton {{
    background: rgba(255,255,255,26); border: none; border-radius: 5px;
    padding: 4px 9px; color: #ddd; font-size: 14px;
}}
#ToolBar #anno_tools QPushButton[tool] {{
    font-size: 14px; padding: 0; min-width: 24px; min-height: 24px;
}}
#ToolBar #anno_tools QPushButton:hover {{ background: rgba(255,255,255,45); }}
#ToolBar #anno_tools QPushButton:checked {{ background: #3d6de0; color: #fff; }}
#ToolBar #anno_tools QLabel {{ color: #9aa; font-size: 14px; }}
#ToolBar #anno_tools QSpinBox {{
    background: rgba(255,255,255,26); border: 1px solid rgba(255,255,255,45);
    border-radius: 4px; color: #eee; padding: 1px 4px;
}}
#ToolBar #anno_tools QSpinBox::up-button,
#ToolBar #anno_tools QSpinBox::down-button {{
    width: 14px; background: rgba(255,255,255,20);
    border: none; border-radius: 2px;
}}
#ToolBar #anno_tools QFontComboBox {{
    background: rgba(255,255,255,26); border: 1px solid rgba(255,255,255,45);
    border-radius: 4px; color: #eee; padding: 1px 4px;
}}
#ToolBar #anno_tools QFontComboBox QAbstractItemView {{
    background: #2e2e31; color: #eee; border: 1px solid #555;
    selection-background-color: #3d3d40;
}}
/* 统一工具栏右下角拉伸手柄（自绘斜线，透明底） */
#StatusStrip #resize_grip {{ background: transparent; }}

/* ---- 截图浮窗角标按钮（画布工具：固定深色，白图上也可读，不随明暗主题）---- */
#ResultOverlay QPushButton[kind='corner'],
#ToolBar QPushButton[kind='corner'],
#SettingsPanel QPushButton[kind='corner'] {{
    background: rgba(30,30,32,205); color: #eee;
    border: 1px solid rgba(255,255,255,70); border-radius: 5px;
    padding: 3px 10px; font-size: 12px;
}}
#ResultOverlay QPushButton[kind='corner']:hover,
#ToolBar QPushButton[kind='corner']:hover,
#SettingsPanel QPushButton[kind='corner']:hover {{ background: rgba(55,55,60,220); }}
#ResultOverlay QPushButton[kind='corner']:checked,
#ToolBar QPushButton[kind='corner']:checked,
#SettingsPanel QPushButton[kind='corner']:checked {{
    background: #3d6de0; color: #fff; border-color: #5b8cff;
}}

/* ---- 快捷键修饰键（紧凑：无勾选框、纯文字，选中高亮）---- */
#SettingsPanel QCheckBox[kind='keymod'] {{
    spacing: 0; padding: 2px 4px; border-radius: 4px; color: #ccc;
    background: transparent;
}}
#SettingsPanel QCheckBox[kind='keymod']::indicator {{
    width: 0; height: 0; border: none;
}}
#SettingsPanel QCheckBox[kind='keymod']:checked {{
    background: rgba(61,109,224,90); color: #fff; border-radius: 4px;
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
    background: rgba(255,255,255,26); border: 1px solid rgba(255,255,255,50);
    border-radius: 6px; padding: 4px 8px; color: #eee;
}}
{_sel('SettingsPanel', 'QComboBox QAbstractItemView')} {{
    background: #2e2e31; color: #eee; border: 1px solid #555;
    selection-background-color: #3d3d40;
}}
{_sel('SettingsPanel', 'QSpinBox')} {{
    background: rgba(255,255,255,26); border: 1px solid rgba(255,255,255,50);
    border-radius: 6px; padding: 2px 6px; color: #eee;
}}

/* ---- 下拉箭头（主题化，替代系统原生）---- */
#SettingsPanel QComboBox::drop-down,
#ToolBar #anno_tools QFontComboBox::drop-down {{
    subcontrol-origin: padding; subcontrol-position: top right;
    width: 18px; border: none;
    border-left: 1px solid rgba(255,255,255,45);
    background: rgba(255,255,255,16);
    border-top-right-radius: 5px; border-bottom-right-radius: 5px;
}}
#SettingsPanel QComboBox::down-arrow,
#ToolBar #anno_tools QFontComboBox::down-arrow {{
    image: url(__COMBO_ARROW__);
}}

/* ---- 线宽滑块浮层（音量条样式）---- */
#WidthSliderPopup #width_card {{ background: rgba(28,28,30,245); border-radius: 6px; }}
#WidthSliderPopup QSlider::groove:horizontal {{
    height: 4px; background: rgba(255,255,255,60); border-radius: 2px;
}}
#WidthSliderPopup QSlider::sub-page:horizontal {{
    background: #3d6de0; border-radius: 2px;
}}
#WidthSliderPopup QSlider::handle:horizontal {{
    width: 14px; margin: -5px 0; border-radius: 7px; background: #fff;
}}
#WidthSliderPopup #width_val {{ color: #ccc; font-size: 11px; }}

/* ---- 字体选择浮层（点「文字」工具弹出）---- */
#FontPopup #font_card {{ background: rgba(28,28,30,245); border-radius: 6px; }}
#FontPopup QLabel {{ color: #ccc; font-size: 11px; }}
#FontPopup QFontComboBox {{
    background: rgba(255,255,255,26); border: 1px solid rgba(255,255,255,45);
    border-radius: 4px; color: #eee; padding: 1px 4px;
}}
#FontPopup QFontComboBox QAbstractItemView {{
    background: #2e2e31; color: #eee; border: 1px solid #555;
    selection-background-color: #3d3d40;
}}
#FontPopup QComboBox {{
    background: rgba(255,255,255,26); border: 1px solid rgba(255,255,255,45);
    border-radius: 4px; color: #eee; padding: 1px 4px;
}}
#FontPopup QComboBox QAbstractItemView {{
    background: #2e2e31; color: #eee; border: 1px solid #555;
    selection-background-color: #3d3d40;
}}
#FontPopup QPushButton {{
    background: rgba(255,255,255,26); border: 1px solid rgba(255,255,255,45);
    border-radius: 4px; color: #eee; font-size: 12px;
}}
#FontPopup QPushButton:hover {{ background: rgba(255,255,255,45); }}
#FontPopup QPushButton:checked {{ background: #3d6de0; color: #fff; }}

/* ---- 马赛克模式选择浮层 ---- */
#MosaicModePopup #mosaic_card {{ background: rgba(28,28,30,245); border-radius: 6px; }}
#ToolOptionsPopup #tool_options_card {{ background: rgba(28,28,30,245); border-radius: 6px; }}
#ToolOptionsPopup QLabel, #FontPopup #font_card QLabel, #MosaicModePopup #mosaic_card QLabel {{ color: #ccc; font-size: 12px; }}
#ToolOptionsPopup QPushButton {{
    background: rgba(255,255,255,26); border: none; border-radius: 5px;
    padding: 0;
}}
#ToolOptionsPopup QPushButton:hover {{ background: rgba(255,255,255,45); }}
#ToolOptionsPopup QPushButton:checked {{ background: #3d6de0; }}
#MosaicModePopup QPushButton {{
    background: rgba(255,255,255,26); border: none; border-radius: 5px;
    padding: 5px 14px; color: #ddd; font-size: 12px;
}}
#MosaicModePopup QPushButton:hover {{ background: rgba(255,255,255,45); }}
#MosaicModePopup QPushButton:checked {{ background: #3d6de0; color: #fff; }}

/* ---- 序号选项浮层（起始序号 + 颜色/大小）---- */
#NumPopup #num_card {{ background: rgba(28,28,30,245); border-radius: 6px; }}
#NumPopup QLabel {{ color: #ccc; font-size: 12px; }}
#NumPopup QPushButton {{
    background: rgba(255,255,255,26); border: none; border-radius: 5px;
    padding: 0;
}}
#NumPopup QPushButton:hover {{ background: rgba(255,255,255,45); }}
#NumPopup QPushButton:checked {{ background: #3d6de0; }}
#NumPopup QSpinBox {{
    background: rgba(255,255,255,26); border: 1px solid rgba(255,255,255,45);
    border-radius: 4px; color: #eee; padding: 1px 4px;
}}
#NumPopup QSpinBox::up-button, #NumPopup QSpinBox::down-button {{
    width: 14px; background: rgba(255,255,255,20);
    border: none; border-radius: 2px;
}}

/* ---- Tab ---- */
{_sel('SettingsPanel', 'QTabWidget::pane')} {{ border: none; background: transparent; }}
{_sel('HistoryPanel', 'QTabWidget::pane')} {{ border: none; background: transparent; }}
{_sel('SettingsPanel', 'QTabBar::tab')} {{
    background: transparent; color: #aaa; padding: 7px 18px;
    border-bottom: 2px solid transparent;
}}
{_sel('SettingsPanel', 'QTabBar::tab:selected')} {{ color: #fff; border-bottom: 2px solid #8ab4ff; }}
{_sel('SettingsPanel', 'QTabBar::tab:hover')} {{ color: #eee; }}
{_sel('HistoryPanel', 'QTabBar::tab')} {{
    background: transparent; color: #aaa; padding: 7px 18px;
    border-bottom: 2px solid transparent;
}}
{_sel('HistoryPanel', 'QTabBar::tab:selected')} {{ color: #fff; border-bottom: 2px solid #8ab4ff; }}
{_sel('HistoryPanel', 'QTabBar::tab:hover')} {{ color: #eee; }}

/* ---- 列表滚动条 ---- */
{_sel('HistoryPanel', 'QListWidget')} {{ background: transparent; border: none; }}
{_sel('HistoryPanel', 'QListWidget::item')} {{ border: none; background: transparent; }}
{_sel('HistoryPanel', 'QListWidget::item:selected')} {{
    background: rgba(138,180,255,35); border-radius: 6px;
}}
{_sel('HistoryPanel', 'QScrollBar:vertical')} {{
    background: transparent; width: 8px; margin: 2px;
}}
{_sel('HistoryPanel', 'QScrollBar::handle:vertical')} {{
    background: rgba(255,255,255,45); border-radius: 4px; min-height: 24px;
}}
{_sel('HistoryPanel', 'QScrollBar::add-line:vertical')},
{_sel('HistoryPanel', 'QScrollBar::sub-line:vertical')} {{ height: 0; }}
{_sel('HistoryPanel', 'QScrollBar::add-page:vertical')},
{_sel('HistoryPanel', 'QScrollBar::sub-page:vertical')} {{ background: transparent; }}

/* ---- 表格（TablePreviewPanel）暗色 ---- */
{_sel('TablePreviewPanel', 'QTableWidget')} {{
    background: rgba(0,0,0,60); border: 1px solid rgba(255,255,255,40);
    border-radius: 6px; color: #e8e8e8; gridline-color: rgba(255,255,255,28);
    selection-background-color: #3d6de0; selection-color: #fff;
}}
{_sel('TablePreviewPanel', 'QTableWidget::item')} {{ padding: 4px 8px; border: none; }}
{_sel('TablePreviewPanel', 'QTableWidget::item:selected')} {{
    background: rgba(61,109,224,160); color: #fff; border-radius: 3px;
}}
{_sel('TablePreviewPanel', 'QHeaderView::section')} {{
    background: rgba(255,255,255,22); color: #ccc;
    border: none; border-right: 1px solid rgba(255,255,255,24);
    border-bottom: 1px solid rgba(255,255,255,24); padding: 5px 8px;
}}
{_sel('TablePreviewPanel', 'QTableCornerButton::section')} {{
    background: rgba(255,255,255,22); border: none;
}}
{_sel('TablePreviewPanel', 'QScrollBar:vertical')} {{
    background: transparent; width: 8px; margin: 2px;
}}
{_sel('TablePreviewPanel', 'QScrollBar::handle:vertical')} {{
    background: rgba(255,255,255,45); border-radius: 4px; min-height: 24px;
}}
{_sel('TablePreviewPanel', 'QScrollBar::add-line:vertical')},
{_sel('TablePreviewPanel', 'QScrollBar::sub-line:vertical')} {{ height: 0; }}
{_sel('TablePreviewPanel', 'QScrollBar::add-page:vertical')},
{_sel('TablePreviewPanel', 'QScrollBar::sub-page:vertical')} {{ background: transparent; }}
/* 统一工具栏：常驻按钮文字/标志统一字号（覆盖共享 corner/pin/fav 规则） */
#ToolBar QPushButton[kind='corner'] {{ font-size: 14px; }}
#ToolBar QPushButton[kind='pin'] {{ font-size: 14px; }}
#ToolBar QPushButton[kind='fav'] {{ font-size: 14px; }}

/* ---- 设置面板滚动区（快捷键 tab）---- */
#SettingsPanel QScrollArea {{ background: transparent; border: none; }}
#SettingsPanel QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
#SettingsPanel QScrollBar::handle:vertical {{ background: rgba(255,255,255,60); border-radius: 4px; min-height: 30px; }}
#SettingsPanel QScrollBar::handle:vertical:hover {{ background: rgba(255,255,255,100); }}
#SettingsPanel QScrollBar::add-line:vertical, #SettingsPanel QScrollBar::sub-line:vertical {{ height: 0; }}
#SettingsPanel QScrollBar::add-page:vertical, #SettingsPanel QScrollBar::sub-page:vertical {{ background: transparent; }}

"""

LIGHT_QSS = f"""
{_panels()} {{ color: #222; font-size: 13px; }}

/* 亮色卡片（三个工具面板共用） */
{_panels('#card')} {{
    background: rgba(236, 236, 238, 244);
    border-radius: 10px;
}}

/* ---- 标签 ---- */
{_panels('QLabel')} {{ color: #222; background: transparent; }}
{_panels("QLabel[kind='title']")} {{ font-size: 15px; font-weight: bold; color: #111; }}
{_panels("QLabel[kind='group']")} {{ font-size: 12px; font-weight: bold; color: #1a56db; }}
{_sel('SettingsPanel', "QLabel[kind='info']")} {{ font-size: 11px; color: #8a8a8a; }}
{_sel('HistoryPanel', "QLabel[kind='text']")} {{ color: #2a2a2a; }}
{_sel('HistoryPanel', "QLabel[kind='info']")} {{ font-size: 11px; color: #8a8a8a; }}
{_sel('HistoryPanel', "QLabel[kind='empty']")} {{ color: #aaa; }}
{_sel('HistoryPanel', "QLabel[kind='thumb']")} {{
    background: rgba(0,0,0,8); border: 1px solid rgba(0,0,0,30);
    border-radius: 4px; color: #aaa;
}}
{_sel('ShortcutPanel', "QLabel[kind='key']")} {{ color: #8a5a00; font-family: Consolas; font-weight: bold; }}
{_sel('ShortcutPanel', "QLabel[kind='desc']")} {{ color: #555; }}

/* ---- 历史列表 ---- */
{_sel('HistoryPanel', '#grouphead')} {{ background: rgba(26,86,219,26); border-radius: 5px; }}
{_sel('HistoryPanel', '#row')} {{ background: rgba(0,0,0,8); border-radius: 6px; }}
{_sel('HistoryPanel', '#row:hover')} {{ background: rgba(0,0,0,16); }}
{_sel('HistoryPanel', '#row[selected="true"]')} {{ border: 1px solid #1a56db; background: rgba(26,86,219,35); }}

/* ---- 输入框 ---- */
{_panels('QLineEdit')} {{
    background: #f2f2f2; border: 1px solid rgba(0,0,0,120);
    border-radius: 6px; padding: 4px 8px; color: #222;
}}
{_panels('QLineEdit:focus')} {{ border-color: #1a56db; }}

/* ---- 按钮 ---- */
{_panels('QPushButton')} {{
    background: rgba(0,0,0,24); border: none; border-radius: 6px;
    padding: 5px 12px; color: #222;
}}
{_panels('QPushButton:hover')} {{ background: rgba(0,0,0,40); }}
{_panels("QPushButton[kind='fav']")} {{
    padding: 0; font-size: 20px; color: #b8860b;
    background: transparent; border-radius: 0;
}}
{_panels("QPushButton[kind='fav']:hover")} {{ color: #8a5a00; background: transparent; }}
#ToolBar QPushButton[kind='pin'] {{
    padding: 0; font-size: 20px; color: #b8860b;
    background: transparent; border: none; border-radius: 0;
}}
#ToolBar QPushButton[kind='pin']:hover,
#ToolBar QPushButton[kind='pin']:checked {{ color: #8a5a00; background: transparent; }}
#StatusStrip QPushButton[kind='lock'] {{
    padding: 0; font-size: 14px; color: #888;      /* 解锁 ○ 灰 */
    background: transparent; border: none; border-radius: 0;
}}
#StatusStrip QPushButton[kind='lock']:hover {{ color: #555; background: transparent; }}
#StatusStrip QPushButton[kind='lock']:checked {{ color: #000000; }}  /* 锁定 ● 黑 */
{_panels("QPushButton[kind='del']")} {{
    padding: 0; font-size: 13px; color: #c0392b; background: transparent;
}}
{_panels("QPushButton[kind='del']:hover")} {{ background: rgba(192,57,43,32); }}
{_panels("QPushButton[kind='close']")} {{
    padding: 0 8px; font-size: 14px; color: #888; background: transparent;
}}
{_panels("QPushButton[kind='close']:hover")} {{
    color: #111; background: rgba(0,0,0,36);
}}
{_panels("QPushButton[kind='spinner']")} {{
    padding: 0; font-size: 9px; border-radius: 3px; color: #666;
    background: rgba(0,0,0,24);
}}
{_panels("QPushButton[kind='spinner']:hover")} {{
    background: rgba(0,0,0,40); color: #111;
}}

/* ---- 复选框（软件/UI 设置）---- */
{_panels('QCheckBox')} {{
    color: #222; background: transparent; spacing: 6px;
}}
{_panels('QCheckBox::indicator')} {{
    width: 14px; height: 14px; border-radius: 3px;
    border: 1px solid rgba(0,0,0,120); background: #fff;
}}
{_panels('QCheckBox::indicator:hover')} {{ border-color: #1a56db; }}
{_panels('QCheckBox::indicator:checked')} {{
    background: #1a56db; border-color: #1a56db;
    image: url(__CHECK__);
}}

/* ---- 滑块（设置面板：图片质量等，样式同线宽浮层）---- */
#SettingsPanel QSlider::groove:horizontal {{
    height: 4px; background: rgba(0,0,0,45); border-radius: 2px;
}}
#SettingsPanel QSlider::sub-page:horizontal {{
    background: #1a56db; border-radius: 2px;
}}
#SettingsPanel QSlider::handle:horizontal {{
    width: 14px; margin: -5px 0; border-radius: 7px; background: #fff;
    border: 1px solid rgba(0,0,0,90);
}}

/* ---- 标注工具区（统一工具栏标注态展开，跟随主题：亮色浅灰）---- */
#ToolBar #anno_tools QPushButton {{
    background: rgba(0,0,0,26); border: none; border-radius: 5px;
    padding: 4px 9px; color: #333; font-size: 14px;
}}
#ToolBar #anno_tools QPushButton[tool] {{
    font-size: 14px; padding: 0; min-width: 24px; min-height: 24px;
}}
#ToolBar #anno_tools QPushButton:hover {{ background: rgba(0,0,0,45); }}
#ToolBar #anno_tools QPushButton:checked {{ background: #1a56db; color: #fff; }}
#ToolBar #anno_tools QLabel {{ color: #888; font-size: 14px; }}
#ToolBar #anno_tools QSpinBox {{
    background: #f2f2f2; border: 1px solid rgba(0,0,0,120);
    border-radius: 4px; color: #222; padding: 1px 4px;
}}
#ToolBar #anno_tools QSpinBox::up-button,
#ToolBar #anno_tools QSpinBox::down-button {{
    width: 14px; background: rgba(0,0,0,20);
    border: none; border-radius: 2px;
}}
#ToolBar #anno_tools QFontComboBox {{
    background: #f2f2f2; border: 1px solid rgba(0,0,0,120);
    border-radius: 4px; color: #222; padding: 1px 4px;
}}
#ToolBar #anno_tools QFontComboBox QAbstractItemView {{
    background: #fff; color: #222; border: 1px solid #bbb;
    selection-background-color: #d6e2ff;
}}
/* 统一工具栏右下角拉伸手柄（自绘斜线，透明底） */
#StatusStrip #resize_grip {{ background: transparent; }}

/* ---- 截图浮窗角标按钮（固定深色，两套主题一致）---- */
#ResultOverlay QPushButton[kind='corner'],
#ToolBar QPushButton[kind='corner'],
#SettingsPanel QPushButton[kind='corner'] {{
    background: rgba(30,30,32,205); color: #eee;
    border: 1px solid rgba(255,255,255,70); border-radius: 5px;
    padding: 3px 10px; font-size: 12px;
}}
#ResultOverlay QPushButton[kind='corner']:hover,
#ToolBar QPushButton[kind='corner']:hover,
#SettingsPanel QPushButton[kind='corner']:hover {{ background: rgba(55,55,60,220); }}
#ResultOverlay QPushButton[kind='corner']:checked,
#ToolBar QPushButton[kind='corner']:checked,
#SettingsPanel QPushButton[kind='corner']:checked {{
    background: #3d6de0; color: #fff; border-color: #5b8cff;
}}

/* ---- 快捷键修饰键（紧凑：无勾选框、纯文字，选中高亮）---- */
#SettingsPanel QCheckBox[kind='keymod'] {{
    spacing: 0; padding: 2px 4px; border-radius: 4px; color: #555;
    background: transparent;
}}
#SettingsPanel QCheckBox[kind='keymod']::indicator {{
    width: 0; height: 0; border: none;
}}
#SettingsPanel QCheckBox[kind='keymod']:checked {{
    background: rgba(26,86,219,110); color: #fff; border-radius: 4px;
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
    background: #f2f2f2; border: 1px solid rgba(0,0,0,120);
    border-radius: 6px; padding: 4px 8px; color: #222;
}}
{_sel('SettingsPanel', 'QComboBox QAbstractItemView')} {{
    background: #f2f2f2; color: #222; border: 1px solid #bbb;
    selection-background-color: #d6e2ff;
}}
{_sel('SettingsPanel', 'QSpinBox')} {{
    background: #f2f2f2; border: 1px solid rgba(0,0,0,120);
    border-radius: 6px; padding: 2px 6px; color: #222;
}}

/* ---- 下拉箭头（主题化）---- */
#SettingsPanel QComboBox::drop-down,
#ToolBar #anno_tools QFontComboBox::drop-down {{
    subcontrol-origin: padding; subcontrol-position: top right;
    width: 18px; border: none;
    border-left: 1px solid rgba(0,0,0,60);
    background: rgba(0,0,0,16);
    border-top-right-radius: 5px; border-bottom-right-radius: 5px;
}}
#SettingsPanel QComboBox::down-arrow,
#ToolBar #anno_tools QFontComboBox::down-arrow {{
    image: url(__COMBO_ARROW__);
}}

/* ---- 线宽滑块浮层（音量条样式）---- */
#WidthSliderPopup #width_card {{ background: rgba(236,236,238,248); border-radius: 6px; }}
#WidthSliderPopup QSlider::groove:horizontal {{
    height: 4px; background: rgba(0,0,0,45); border-radius: 2px;
}}
#WidthSliderPopup QSlider::sub-page:horizontal {{
    background: #1a56db; border-radius: 2px;
}}
#WidthSliderPopup QSlider::handle:horizontal {{
    width: 14px; margin: -5px 0; border-radius: 7px; background: #fff;
    border: 1px solid rgba(0,0,0,90);
}}
#WidthSliderPopup #width_val {{ color: #666; font-size: 11px; }}

/* ---- 字体选择浮层（点「文字」工具弹出）---- */
#FontPopup #font_card {{ background: rgba(236,236,238,248); border-radius: 6px; }}
#FontPopup QLabel {{ color: #666; font-size: 11px; }}
#FontPopup QFontComboBox {{
    background: #fff; border: 1px solid rgba(0,0,0,120);
    border-radius: 4px; color: #333; padding: 1px 4px;
}}
#FontPopup QFontComboBox QAbstractItemView {{
    background: #fff; color: #333; border: 1px solid #bbb;
    selection-background-color: #d6e4ff;
}}
#FontPopup QComboBox {{
    background: #fff; border: 1px solid rgba(0,0,0,120);
    border-radius: 4px; color: #333; padding: 1px 4px;
}}
#FontPopup QComboBox QAbstractItemView {{
    background: #fff; color: #333; border: 1px solid #bbb;
    selection-background-color: #d6e4ff;
}}
#FontPopup QPushButton {{
    background: #fff; border: 1px solid rgba(0,0,0,120);
    border-radius: 4px; color: #333; font-size: 12px;
}}
#FontPopup QPushButton:hover {{ background: #ececee; }}
#FontPopup QPushButton:checked {{ background: #3d6de0; color: #fff; }}

/* ---- 马赛克模式选择浮层 ---- */
#MosaicModePopup #mosaic_card {{ background: rgba(236,236,238,248); border-radius: 6px; }}
#ToolOptionsPopup #tool_options_card {{ background: rgba(236,236,238,248); border-radius: 6px; }}
#ToolOptionsPopup QLabel, #FontPopup #font_card QLabel, #MosaicModePopup #mosaic_card QLabel {{ color: #666; font-size: 12px; }}
#ToolOptionsPopup QPushButton {{
    background: rgba(0,0,0,26); border: none; border-radius: 5px;
    padding: 0;
}}
#ToolOptionsPopup QPushButton:hover {{ background: rgba(0,0,0,45); }}
#ToolOptionsPopup QPushButton:checked {{ background: #1a56db; }}
#MosaicModePopup QPushButton {{
    background: rgba(0,0,0,26); border: none; border-radius: 5px;
    padding: 5px 14px; color: #333; font-size: 12px;
}}
#MosaicModePopup QPushButton:hover {{ background: rgba(0,0,0,45); }}
#MosaicModePopup QPushButton:checked {{ background: #1a56db; color: #fff; }}

/* ---- 序号选项浮层（起始序号 + 颜色/大小）---- */
#NumPopup #num_card {{ background: rgba(236,236,238,248); border-radius: 6px; }}
#NumPopup QLabel {{ color: #666; font-size: 12px; }}
#NumPopup QPushButton {{
    background: rgba(0,0,0,26); border: none; border-radius: 5px;
    padding: 0;
}}
#NumPopup QPushButton:hover {{ background: rgba(0,0,0,45); }}
#NumPopup QPushButton:checked {{ background: #1a56db; }}
#NumPopup QSpinBox {{
    background: #fff; border: 1px solid rgba(0,0,0,120);
    border-radius: 4px; color: #333; padding: 1px 4px;
}}
#NumPopup QSpinBox::up-button, #NumPopup QSpinBox::down-button {{
    width: 14px; background: rgba(0,0,0,20);
    border: none; border-radius: 2px;
}}

/* ---- Tab ---- */
{_sel('SettingsPanel', 'QTabWidget::pane')} {{ border: none; background: transparent; }}
{_sel('HistoryPanel', 'QTabWidget::pane')} {{ border: none; background: transparent; }}
{_sel('SettingsPanel', 'QTabBar::tab')} {{
    background: transparent; color: #777; padding: 7px 18px;
    border-bottom: 2px solid transparent;
}}
{_sel('SettingsPanel', 'QTabBar::tab:selected')} {{ color: #111; border-bottom: 2px solid #1a56db; }}
{_sel('SettingsPanel', 'QTabBar::tab:hover')} {{ color: #333; }}
{_sel('HistoryPanel', 'QTabBar::tab')} {{
    background: transparent; color: #777; padding: 7px 18px;
    border-bottom: 2px solid transparent;
}}
{_sel('HistoryPanel', 'QTabBar::tab:selected')} {{ color: #111; border-bottom: 2px solid #1a56db; }}
{_sel('HistoryPanel', 'QTabBar::tab:hover')} {{ color: #333; }}

/* ---- 列表滚动条 ---- */
{_sel('HistoryPanel', 'QListWidget')} {{ background: transparent; border: none; }}
{_sel('HistoryPanel', 'QListWidget::item')} {{ border: none; background: transparent; }}
{_sel('HistoryPanel', 'QListWidget::item:selected')} {{
    background: rgba(26,86,219,35); border-radius: 6px;
}}
{_sel('HistoryPanel', 'QScrollBar:vertical')} {{
    background: transparent; width: 8px; margin: 2px;
}}
{_sel('HistoryPanel', 'QScrollBar::handle:vertical')} {{
    background: rgba(0,0,0,80); border-radius: 4px; min-height: 24px;
}}
{_sel('HistoryPanel', 'QScrollBar::add-line:vertical')},
{_sel('HistoryPanel', 'QScrollBar::sub-line:vertical')} {{ height: 0; }}
{_sel('HistoryPanel', 'QScrollBar::add-page:vertical')},
{_sel('HistoryPanel', 'QScrollBar::sub-page:vertical')} {{ background: transparent; }}

/* ---- 表格（TablePreviewPanel）亮色 ---- */
{_sel('TablePreviewPanel', 'QTableWidget')} {{
    background: #f2f2f2; border: 1px solid rgba(0,0,0,120);
    border-radius: 6px; color: #222; gridline-color: rgba(0,0,0,40);
    selection-background-color: #3d6de0; selection-color: #fff;
}}
{_sel('TablePreviewPanel', 'QTableWidget::item')} {{ padding: 4px 8px; border: none; }}
{_sel('TablePreviewPanel', 'QTableWidget::item:selected')} {{
    background: rgba(26,86,219,120); color: #fff; border-radius: 3px;
}}
{_sel('TablePreviewPanel', 'QHeaderView::section')} {{
    background: rgba(0,0,0,22); color: #555;
    border: none; border-right: 1px solid rgba(0,0,0,30);
    border-bottom: 1px solid rgba(0,0,0,30); padding: 5px 8px;
}}
{_sel('TablePreviewPanel', 'QTableCornerButton::section')} {{
    background: rgba(0,0,0,22); border: none;
}}
{_sel('TablePreviewPanel', 'QScrollBar:vertical')} {{
    background: transparent; width: 8px; margin: 2px;
}}
{_sel('TablePreviewPanel', 'QScrollBar::handle:vertical')} {{
    background: rgba(0,0,0,80); border-radius: 4px; min-height: 24px;
}}
{_sel('TablePreviewPanel', 'QScrollBar::add-line:vertical')},
{_sel('TablePreviewPanel', 'QScrollBar::sub-line:vertical')} {{ height: 0; }}
{_sel('TablePreviewPanel', 'QScrollBar::add-page:vertical')},
{_sel('TablePreviewPanel', 'QScrollBar::sub-page:vertical')} {{ background: transparent; }}
/* 统一工具栏：常驻按钮文字/标志统一字号（覆盖共享 corner/pin/fav 规则） */
#ToolBar QPushButton[kind='corner'] {{ font-size: 14px; }}
#ToolBar QPushButton[kind='pin'] {{ font-size: 14px; }}
#ToolBar QPushButton[kind='fav'] {{ font-size: 14px; }}

/* ---- 设置面板滚动区（快捷键 tab）---- */
#SettingsPanel QScrollArea {{ background: transparent; border: none; }}
#SettingsPanel QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
#SettingsPanel QScrollBar::handle:vertical {{ background: rgba(0,0,0,60); border-radius: 4px; min-height: 30px; }}
#SettingsPanel QScrollBar::handle:vertical:hover {{ background: rgba(0,0,0,100); }}
#SettingsPanel QScrollBar::add-line:vertical, #SettingsPanel QScrollBar::sub-line:vertical {{ height: 0; }}
#SettingsPanel QScrollBar::add-page:vertical, #SettingsPanel QScrollBar::sub-page:vertical {{ background: transparent; }}

"""


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


def current_theme() -> str:
    """当前生效主题（dark|light）。"""
    return _THEME


def icon_color() -> str:
    """通用图标前景色：深色主题=浅色图标，浅色主题=深色图标。"""
    return "#e4e4e4" if _THEME == "dark" else "#333333"


def accent_color() -> str:
    """金色强调色（收藏星标/钉图钉子等，与 QSS kind='fav'/'pin' 一致）。"""
    return "#ffd54f" if _THEME == "dark" else "#b8860b"


def resolve_theme(config) -> str:
    """把配置值（dark|light|system）解析成实际主题名（dark|light）。"""
    t = str(config.get("ui", "theme", "system")).strip().lower()
    if t == "dark":
        return "dark"
    if t == "light":
        return "light"
    return "dark" if is_system_dark() else "light"


def apply_theme(app, theme: str):
    """给 QApplication 应用全局主题 QSS（theme in {dark, light}）。

    同步更新当前主题状态并清空 SVG 图标缓存（图标色随主题重建）。
    """
    global _THEME
    _THEME = "dark" if theme == "dark" else "light"
    try:
        from . import icon as _icon
        _icon.clear_cache()
    except Exception:            # noqa: BLE001
        pass
    base = DARK_QSS if theme == "dark" else LIGHT_QSS
    arrow = _arrow_path("#bdbdbd" if theme == "dark" else "#666666")
    check = _check_path("#ffffff")
    app.setStyleSheet(base.replace("__COMBO_ARROW__", arrow)
                      .replace("__CHECK__", check))


def install_system_follow(app, config, on_change=None):
    """theme=system 时监听系统深浅色变化自动重应用。

    返回是否成功安装监听（不支持的平台返回 False，无副作用）。
    """
    try:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtCore import Qt

        hints = QGuiApplication.styleHints()

        def _handler(scheme):
            mode = str(config.get("ui", "theme", "system")).strip().lower()
            if mode not in ("system", ""):
                return
            theme = "dark" if scheme == Qt.ColorScheme.Dark else "light"
            apply_theme(app, theme)
            if on_change is not None:
                on_change(theme)

        hints.colorSchemeChanged.connect(_handler)
        return True
    except Exception:  # noqa: BLE001
        return False
