"""ui.base.icon — 单色 SVG 图标加载 + 主题着色 + 缓存（方案 B：QSvgRenderer 渲染）。

- 不依赖 Qt imageformats/qsvg 图片插件（QtSvg 模块直接渲染到 QPixmap）。
- 图标源：resources/icons/*.svg（统一规范：viewBox="0 0 24 24"，
  fill/stroke 用 currentColor）；ICON_PATHS 集中注册，业务只调 icon("name")。
- 着色：icon(name, color=...) 把渲染结果 tint 成指定颜色（SourceIn 保 alpha）；
  color=None 时按 SVG 默认（currentColor=黑）。
- 缓存：(name, color, size) → QIcon，含 1x / 2x（devicePixelRatio=2）两档，
  高 DPI 下不模糊。主题切换时由 ui.base.theme 调 clear_cache() 重建。
- 图标缺失时返回空 QIcon（不抛异常，按钮表现为无图标，不崩溃）。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor

# 图标目录：开发 = 项目根/resources/icons；打包 = _internal/resources/icons
# （本文件位于 ui/base/，向上三级 = 项目根）
_ICON_DIR = Path(__file__).resolve().parent.parent.parent / "resources" / "icons"
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    _mei = Path(sys._MEIPASS) / "resources" / "icons"
    if _mei.is_dir():
        _ICON_DIR = _mei

# name → 文件名 注册表（业务代码只写 name，不写路径字符串）
ICON_PATHS = {
    "star": "star.svg",
    "star-filled": "star-filled.svg",
    "copy": "copy.svg",
    "pin": "pin.svg",
    "pin-dot": "pin-dot.svg",
    "undo": "undo.svg",
    "redo": "redo.svg",
    "close": "close.svg",
    "link": "link.svg",
    "link-off": "link-off.svg",
    "qr": "qr.svg",
    "scroll": "scroll.svg",
    "nav": "nav.svg",
    "ratio": "ratio.svg",
    "ratio-off": "ratio-off.svg",
    "tool-select": "tool-select.svg",
    "tool-rect": "tool-rect.svg",
    "tool-arrow": "tool-arrow.svg",
    "tool-pen": "tool-pen.svg",
    "tool-line": "tool-line.svg",
    "tool-mosaic": "tool-mosaic.svg",
    "tool-text": "tool-text.svg",
    "tool-num": "tool-num.svg",
    # 直线样式
    "line-solid": "line-solid.svg",
    "line-dash": "line-dash.svg",
    "line-dot": "line-dot.svg",
    "line-dashdot": "line-dashdot.svg",
    "line-double": "line-double.svg",
    "line-wavy": "line-wavy.svg",
    "line-zigzag": "line-zigzag.svg",
    # 序号样式
    "num-circle": "num-circle.svg",
    "num-circle-o": "num-circle-o.svg",
    "num-square": "num-square.svg",
    "num-paren": "num-paren.svg",
    "num-plain": "num-plain.svg",
    # 矩形样式
    "rect-outline": "tool-rect.svg",
    "rect-filled": "rect-filled.svg",
    "rect-round": "rect-round.svg",
    "rect-ellipse": "rect-ellipse.svg",
    "rect-dialog": "rect-dialog.svg",
    "rect-highlight": "rect-highlight.svg",
    "rect-dash": "rect-dash.svg",
    # 箭头样式
    "arrow-single": "arrow-single.svg",
    "arrow-double": "arrow-double.svg",
    "arrow-dash": "arrow-dash.svg",
    "arrow-elbow": "arrow-elbow.svg",
    "arrow-curve": "arrow-curve.svg",
    "arrow-dot": "arrow-dot.svg",
    "arrow-circle": "arrow-circle.svg",
    # 画笔样式
    "pen-solid": "pen-solid.svg",
    "pen-highlight": "pen-highlight.svg",
    "pen-dash": "pen-dash.svg",
    "pen-dot": "pen-dot.svg",
    "pen-double": "pen-double.svg",
    "pen-wavy": "pen-wavy.svg",
    "pen-glow": "pen-glow.svg",
}

_cache: dict = {}


def clear_cache():
    """清空图标缓存（主题切换时调用，下次取图标按新颜色重建）。"""
    _cache.clear()


def _renderer(name: str):
    """加载 SVG 渲染器；文件缺失 / 解析失败返回 None。"""
    fname = ICON_PATHS.get(name, f"{name}.svg")
    path = _ICON_DIR / fname
    if not path.exists():
        return None
    try:
        from PySide6.QtSvg import QSvgRenderer
        return QSvgRenderer(str(path))
    except Exception:            # noqa: BLE001
        return None


def _render(r, size: int, color: str | None) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    r.render(p)
    p.end()
    if color:
        _tint(pm, color)
    return pm


def _tint(pm: QPixmap, color: str):
    """SourceIn 叠色：像素 alpha 保留，RGB 全部替换为 color。"""
    c = QColor(color)
    p = QPainter(pm)
    p.setCompositionMode(QPainter.CompositionMode_SourceIn)
    p.fillRect(pm.rect(), c)
    p.end()


def icon(name: str, color: str | None = None, size: int = 20) -> QIcon:
    """获取单色 SVG 图标（带缓存，含 1x/2x 两档）。

    - name：ICON_PATHS 中的注册名；
    - color：目标颜色（如 "#ffd54f"）；None = 保持 SVG 默认（currentColor 黑）；
    - size：逻辑像素尺寸（2x 档自动生成）。
    """
    key = (name, color or "", size)
    if key in _cache:
        return _cache[key]
    r = _renderer(name)
    ic = QIcon()
    if r is not None:
        pm = _render(r, size, color)
        ic.addPixmap(pm)
        pm2 = _render(r, size * 2, color)
        pm2.setDevicePixelRatio(2.0)
        ic.addPixmap(pm2)
    _cache[key] = ic
    return ic


def icon_pixmap(name: str, color: str | None = None, size: int = 20) -> QPixmap:
    """直接取 QPixmap（QLabel.setPixmap 等场景），不复用 QIcon 缓存。"""
    r = _renderer(name)
    if r is None:
        return QPixmap()
    return _render(r, size, color)
