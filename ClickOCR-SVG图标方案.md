# ClickOCR SVG 图标支持方案

> 规划文档（2026-08-16）。本会话不改业务代码，具体实现交代码开发会话。
> 配套：《ClickOCR-统一工具栏与状态栏方案.md》。

## 0. 目标

用 SVG 单色图标替换所有 Unicode 字符图标（★☆⮺⊛↶↷✕⌖□↗✎▦ 等），根除生僻字符在字体回退时显示成方框/错位的问题，并支持随主题变色。

## 1. 现状

- 项目当前 **0 个 SVG**，图标全是 Unicode 字符；唯一 `QIcon` 是托盘图标（`app.py:_make_icon` 用 QPainter 现画）。
- 新落地的 `ui/toolbar.py`（⮺ ☆ ↶ ↷）、`ui/pin_dot.py`（⊛）也在用 Unicode。
- 生僻字风险最高：`⮺`（钉图）、`⊛`（钉子）、`⌖`（选择）、`▦`（马赛克）、`✎`（画笔）。

## 2. 怎么支持 SVG（技术选型）

### 方案 A：QIcon 直接加载 .svg
`QIcon("resources/icons/star.svg")` —— 依赖 Qt 的 `imageformats/qsvg` 插件。
- 优点：代码最少。缺点：打包必须带插件；运行时无法按主题重新着色（`currentColor` 支持有限）。

### 方案 B：QSvgRenderer 渲染成 QPixmap 再 QIcon（推荐）
`QSvgRenderer` 加载 → 渲染到 `QPixmap`（可指定尺寸）→ 用 `QPainter` 叠加主题色 tint → `QIcon.addPixmap(pm)`。
- 优点：**不依赖 imageformat 插件**；可运行时按主题着色；可缓存、可出多 DPI 尺寸。
- 缺点：需写一个约几十行的 `ui/icon.py` 工具。

**结论：选 B。**

### ui/icon.py 接口示意（供代码开发会话参考，非本会话实现）

```python
# ui/icon.py —— 单色 SVG 加载 + 主题着色 + 缓存
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QPixmap, QPainter
from PySide6.QtSvg import QSvgRenderer

_cache = {}  # (name, color, size) -> QIcon

def icon(name, color=None, size=20) -> QIcon:
    key = (name, color or "", size)
    if key in _cache:
        return _cache[key]
    pm = QPixmap(size, size); pm.fill(Qt.transparent)
    r = QSvgRenderer(str(ICON_DIR / f"{name}.svg"))
    p = QPainter(pm); r.render(p); p.end()
    if color:
        tint(pm, color)          # QPainter.CompositionMode_SourceIn 叠色
    ic = QIcon(); ic.addPixmap(pm)
    _cache[key] = ic
    return ic
```

主题色来源：`theme.py` 暴露一个 `icon_color()`（浅色主题=深色图标，深色主题=浅色图标），切换主题时清 `_cache`。

## 3. 图标怎么画（绘制方案）

12 个图标都是简单几何图形，**直接手写 SVG path**（单色、体积小、可控），不必用 Figma/Inkscape 导入。

**SVG 统一规范**：`viewBox="0 0 24 24"`；单色用 `fill="currentColor"`（实心）或 `stroke="currentColor" stroke-width="2"`（描边）；圆头 `stroke-linecap="round"`。

### 图标清单与几何描述

| name | 几何 | 类型 |
|---|---|---|
| `star.svg` / `star-filled.svg` | 五角星（描边 / 实心） | 收藏 ☆/★ |
| `pin.svg` | 图钉（圆头 + 针 + 尖端） | 钉图 ⮺ |
| `pin-dot.svg` | 实心圆点（可带一圈描边） | 钉子 ⊛ |
| `undo.svg` | 左向弧形箭头 | 撤销 ↶ |
| `redo.svg` | 右向弧形箭头 | 重做 ↷ |
| `close.svg` | 两条交叉对角线 | 关闭 ✕ |
| `tool-select.svg` | 鼠标光标箭头 | 选择 ⌖ |
| `tool-rect.svg` | 矩形外框 | 矩形 □ |
| `tool-arrow.svg` | 对角直线 + 箭头 | 箭头 ↗ |
| `tool-pen.svg` | 铅笔（笔杆 + 笔尖） | 画笔 ✎ |
| `tool-mosaic.svg` | 2×2 / 3×3 网格 | 马赛克 ▦ |
| `tool-text.svg` | 大写 T（或 I 光标） | 文字 T |

### 参考示例（可直接用）

```svg
<!-- close.svg -->
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
  <path d="M6 6 L18 18 M18 6 L6 18"/>
</svg>

<!-- star-filled.svg -->
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
  <path d="M12 2l2.9 6.2 6.6.9-4.8 4.6 1.1 6.5L12 17.8 6.2 20.2l1.1-6.5L2.5 9.1l6.6-.9L12 2z"/>
</svg>

<!-- undo.svg -->
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M9 14l-4-4 4-4"/>
  <path d="M5 10h11a5 5 0 0 1 0 10h-3"/>
</svg>

<!-- pin.svg -->
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
  <path d="M16 3l5 5-7 1-3 3-4 1 1 1 4 4 1 1 1-4 3-3 1-7z"/>
</svg>
```

## 4. SVG 图标怎么管理

- **目录**：`resources/icons/*.svg`（与 `resources/ui_text.json` 同级）。
- **命名**：kebab-case，语义名 + 状态后缀（`-filled`/`-outline`），工具类加 `tool-` 前缀。
- **注册表**：`ui/icon.py` 里一个 `ICON_PATHS` 常量表（name → 文件名）集中管理，业务代码只调 `icon("star")`，不写路径字符串。
- **主题联动**：图标色从 `theme.py` 取；主题切换时清 `_cache` 重建。
- **版本/归属**：图标是资源，随 `--add-data` 打进包，不散落代码里。

## 5. 怎么替换（清单 + 顺序）

### 替换映射

| 文件 | 当前符号 | 换成 |
|---|---|---|
| `ui/toolbar.py` | ⮺ / ☆ / ↶ / ↷ | `icon("pin")` / `icon("star")` / `icon("undo")` / `icon("redo")` |
| `ui/pin_dot.py` | ⊛ | `icon("pin-dot")`（或保留圆点样式，只换图标源） |
| `ui/history_panel.py` | ★ / ☆ / ✕ | `icon("star-filled")`/`icon("star")`/`icon("close")` |
| `ui/result_overlay.py` | ✕（btn_close） | `icon("close")` |
| `ui/card_window.py` | ✕（_close） | `icon("close")` |
| `ui/annotation.py` `TOOL_LABELS` | ⌖ □ ↗ ✎ ▦ T | `icon("tool-select")`…`icon("tool-text")` |
| `ui/shortcut_panel.py` | ⊛ ☆（纯展示字符串） | 可后续再换，优先级低 |

### 实施顺序

1. 建 `resources/icons/` + 12 个 SVG（按第 3 节规范）。
2. 写 `ui/icon.py`（加载/着色/缓存）+ `theme.py` 暴露 `icon_color()`。
3. 逐文件替换（toolbar → pin_dot → history_panel → overlay/card_window → annotation）。
4. **PyInstaller spec**：`--add-data "resources/icons:resources/icons"`（方案 B 不需要 imageformat 插件，但 QtSvg 是 PySide6 常规依赖，确认在打包 include 内）。
5. 验证：Windows 下逐按钮看渲染 + 深浅主题切换后图标颜色正确。

## 6. 影响与风险

- **改动量**：小~中。新增 1 个工具模块 + 12 个 SVG + 替换约 20 处按钮面；**不动 OCR/标注/历史核心逻辑**。
- **风险点**：打包 `--add-data` 漏配（最易踩）；方案 B 已规避 imageformat 插件依赖。
- **收益**：根除 ⮺/⊛/⌖/✎/▦ 字体回退方框；图标视觉统一、可随主题变色。
- **边界**：`⏎`（历史记录里的换行分隔）是正文内联符号，不是按钮图标，不在此范围。
