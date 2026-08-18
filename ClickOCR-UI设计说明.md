# ClickOCR UI 设计说明：截图后状态栏与标注栏

> 范围：截图完成后屏幕上出现的「贴图窗口 + 状态栏 + 标注栏」三层浮窗的 UI 设计
> 适用版本：M1（全链路）+ M2（状态栏/标注栏独立浮层重构）+ M2 后续迭代（08-13 精修）
> 配套文档：`ClickOCR-M2开发日志.md`、`ClickOCR-2026-08-13开发日志.md`、蓝图工作台「交互细节规格」章节
> 说明：本文为**设计说明文档**，不涉及代码改写；类名仅用于指代设计对象

---

## 1. 总体拓扑：三层互不嵌套的浮窗

截图完成后，屏幕上出现三层**互不嵌套**的顶层浮窗。三者全部是 `Qt.Tool` 无边框置顶窗口，统一走 `CardWindow`（浮层基类，带 `DragBar` 拖动条）+ `ui/theme.py` 主题体系，**零内嵌 QSS**。

| 层 | 组件 | 锚定关系 | 主要组成 |
|---|---|---|---|
| 主图 | `ResultOverlay`（贴图窗口） | 居中 | 顶部拖动把手 · 右上角圆形关闭按钮 · 四周蓝色边框（cosmetic + NoFrame + 4 条填充矩形） |
| 下方 | `StatusStrip`（状态栏） | 跟随图片**正下方 y+4**，屏幕 clamp | 提示行 + 选择预览行 + 角标按钮 |
| 再下方 | `AnnotationToolBar`（标注栏） | **锚定状态栏下方**；放不下翻到状态栏上方 | 马赛克 / 箭头 / 线宽 / 文字 / 撤销 |

> **为何分离**：早期版本状态栏是图片 scene 的一部分（图片下方多了一块 band），导致两个结构性 bug —— ① 截图会把状态栏截进去；② 打开标注后把状态栏当画布。M2 把状态栏拆成独立浮窗后，标注画布天然只含图片，问题根除。

---

## 2. 状态栏（StatusStrip）设计

### 2.1 形态
`StatusStrip(CardWindow)` 浮层，包含三块：
- **提示行**：如「已识别 12 行文字 · 拖空白区移动图片」
- **选择预览行**：当前选区/表格选中态的预览
- **角标按钮行**：表格 / 标注 / 收藏 / 关闭

### 2.2 宽度公式
```
宽度 = max(340, min(图片宽, 标注栏宽 × 1.5))
且 setMaximumWidth(1400)   # 双保险，防止超宽屏拉爆
```

### 2.3 按钮顺序与语义
| 顺序 | 按钮 | 快捷键 | 说明 |
|---|---|---|---|
| 1 | `表格` | Ctrl+Shift+T | 打开表格预览面板（`TablePreviewPanel`）；TSV 复制去弹窗直接写剪贴板 |
| 2 | `标注` | Ctrl+Shift+D | 进入标注模式（`AnnotationToolBar` 显形） |
| 3 | `收藏 ☆/★` | — | 可取消收藏；★/☆ 切换；清空历史时保留收藏（`clear(keep_favorites=True)`） |
| 4 | `关闭 ✕` | — | 关闭当前贴图窗口 |

> 收藏星级历经 ⭐✩（Linux 可能方框）→ ★☆（最保险，DejaVu/Noto 都支持）；去圆角背景（通用 `QPushButton` 规则有 `border-radius:6px`，fav 规则需显式 `border-radius:0` + hover 透明）；历史点收藏不重排（只更新对应行星号，不 reload 重建列表）。

### 2.4 拖动联动（移动图片窗口）
在状态栏**空白区**按住拖动，会联动移动图片窗口。坐标基准是关键：

```
按下时：
  _drag_offset = 鼠标全局位置 - 目标图片窗口.frameGeometry().topLeft()
  # 必须以「目标图片窗口」的 frameGeometry 为基准，
  # 不能用状态栏自身的 frameGeometry（否则图片会被 move 到状态栏左上角 → 闪现 bug）

moveEvent（持续）：
  target_img.move(鼠标全局位置 - _drag_offset)
```

08-13 修复：`_drag_offset` 原先用状态栏 `frameGeometry` 记录，与 move 的目标（图片窗口）基准不一致 → 图片被甩到状态栏左上角。改为按下即用**目标图片窗口** `frameGeometry` 基准。

### 2.5 自动跟随
图片窗口的 `moveEvent / showEvent / resizeEvent` 触发 `_sync_status_strip_pos`：
- 状态栏始终贴在图片**正下方 y+4**
- 超出屏幕则 clamp 回可视区
- **关闭图片** → 状态栏 `closeEvent` 同步关闭（closeEvent 补 close）

---

## 3. 标注栏（AnnotationToolBar）设计

### 3.1 形态
独立顶层浮层，无边框 / 置顶 / `Tool`，可拖动且屏幕 clamp（与状态栏同款 `#anno_card` 子卡片样式，解决早期 QWidget 无 `WA_StyledBackground` 不绘制背景的问题）。

### 3.2 工具清单
| 工具 | 交互细节 |
|---|---|
| **马赛克** | 三模式：`画笔` / `框选` / `文字框`（点 OCR 文字框 bbox 一次打码）；记忆上次选择（config `[annotation] mosaic_mode`，默认 rect）；模式浮层选完保持打开，**切工具才关** |
| **箭头** | 尖端在**鼠标放开处**、箭尾在点击处；三角几何修正为「线只画到翅膀根 + 三角顶点突出线外」（粗线 16 时三角仍明显张开）；拖动中 `QGraphicsItemGroup` 实时预览完整箭头 |
| **线宽** | 单击弹滑块 ↔ 按住拖动调节**两者共存**（`_WidthButton`：右大左小 / 上大下小，~10px=1 步）；浮层锚定按钮中心下方不重叠 |
| **文字** | 点「文字」工具时在按钮下方弹 `_FontPopup`（替代内嵌 `QFontComboBox`） |
| **撤销 / 重做** | 标注 JSON 持久化，支持撤销重做 |

> 标注工具栏经 08-13 精修：删「标注」标题 QLabel；工具图标放大至 19px（普通按钮仍 12px）。

### 3.3 画图让位（yield-to-cursor）
标注栏/状态栏是浮在画布上方的，绘制时会挡住光标路径，需要「让位」。判定经历三轮迭代，最终规则：

```
全局光标轮询（80ms，仅标注模式开启）：
  if 光标位于 [标注栏 ∪ 状态栏] 矩形内:
      if AnnotationSession.is_drawing:   # 仅「按住左键绘制中」才让位
          临时隐藏 标注栏/状态栏
      else:
          保持可见（未绘制时点按钮不受影响）
  else:
      恢复可见

「本笔画完」= 松开鼠标（is_drawing 变为 False）→ 立即恢复
```

**关键修正**：早期用 `QApplication.mouseButtons() & Qt.LeftButton`（全局左键）判断"绘制中"，点按钮时左键按下的间隙被误判为绘制 → 栏隐藏"点不到"。改为判 `AnnotationSession.is_drawing`（`_drawing is not None`），仅画布拖拽绘制中才让位。

### 3.4 锚定关系
- 标注栏**锚定状态栏下方**（用 strip 顶做锚防重叠）
- 空间不足（靠近屏幕底部）时翻到状态栏**上方**
- 关闭图片 → 状态栏 + 标注栏同步关闭

---

## 4. 全局约束（两条贯穿设计的原则）

### 4.1 截图不截入任何 ClickOCR UI
截图开始瞬间，**贴图 + 状态栏 + 标注栏 + 历史 + 设置全量隐藏**；取消 / 完成再恢复。这是从根上杜绝「截图截到状态栏 / 标注栏」的唯一可靠手段（单纯把栏移出选区不够，因为下一次框选可能覆盖）。

### 4.2 主题统一（CardWindow + theme.py）
下列组件全部收敛到 `ui/theme.py` 主题体系，新增窗口一律 `CardWindow + theme`，**禁止原生 / 内嵌样式**（用户 08-13 明确的 UI 规范）：
- 状态栏、标注栏、角标按钮（kind='corner' / 'fav'）
- 颜色选择器 `ColorPickerDialog`（替代系统 `QColorDialog`）
- 表格预览面板 `TablePreviewPanel`
- 主题化弹窗 `ThemeDialog`（替代原生 `QMessageBox` 的 info/question/warning）
- 贴图关闭按钮 `#overlay_close`（ID 选择器，避免父级无选择器 QSS 覆盖子控件致透明）

> 例外：表格导出 / 提示用的系统 `QFileDialog` 仍原生（保存类系统对话框，可后续做主题化轻提示）。

---

## 5. 关键踩坑回顾（设计决策的来由）

| 问题 | 根因 | 设计解 |
|---|---|---|
| 截图截到状态栏 / 标注把状态栏当画布 | 状态栏是图片 scene 的一部分 | 拆成独立 `CardWindow` 浮窗；截图时全量隐藏 |
| 状态栏拖动图片「闪现」甩到角落 | 拖动偏移基准用了状态栏自身 frameGeometry | 按下即以目标图片窗口 frameGeometry 为基准 |
| 点标注栏按钮「点不到」（栏消失） | 让位守卫用全局左键误判绘制中 | 改判 `AnnotationSession.is_drawing` |
| 标注需点两次才进入 | `ResultOverlay.__init__` 未存 `self.config` | `__init__` 补 `self.config = cfg` |
| 顶层 QWidget 白背景看不清 | 缺 `WA_StyledBackground` / 无选择器 QSS | 统一 `#anno_card` 子卡片 + theme 规则 |
| 结果图边框高 DPI 消失 | QPen 非 cosmetic + QFrame 偏移 + 浮点舍入 | `setCosmetic(True)` + `NoFrame` + 4 条填充矩形 |
| 关闭按钮透明 | 父级无选择器 `background:transparent` 覆盖子控件 | 改 `#ResultOverlay{}` / `#overlay_close` ID 选择器 |

---

## 6. 附：状态栏 / 标注栏与贴图窗口的锚定坐标示意

```
y = img_top
┌──────────────────────────────┐  ← ResultOverlay（图片 scene）
│   [拖动把手]                  │
│   图片内容（OCR 块 / 表格网格）│
│                          (●) │  ← 右上角圆形关闭按钮（部分悬出窗外 5px）
└──────────────────────────────┘
                │ +4px (y+4, 屏幕 clamp)
┌──────────────────────────────┐  ← StatusStrip（状态栏）
│ 已识别 N 行 · 拖空白区移动图片 │ [表格][标注][☆][✕]
└──────────────────────────────┘
                │ 锚定（放不下翻上方）
┌──────────────────────────────┐  ← AnnotationToolBar（标注栏）
│ [马赛克▾][箭头][线宽][文字][撤销] │
└──────────────────────────────┘
```

> 三者独立浮窗，互不嵌套；任一窗口 move/resize 都会触发 `_sync_status_strip_pos` 级联跟随，保持「贴图在下、状态栏居中、标注栏居下」的相对布局。
