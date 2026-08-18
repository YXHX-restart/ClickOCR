# ScreenPinKit 对比 ClickOCR 与借鉴计划

> 调研日期：2026-08-16
> 调研对象：https://github.com/InterwovenCode/ScreenPinKit （PyQt5 / MIT）
> 本文档为**规划文档**，不含业务代码改动；具体移植/重构由代码开发会话执行。

---

## 0. 结论速览

| 结论 | 说明 |
|---|---|
| 能否直接引入 | ❌ 不能。ScreenPinKit 是 **PyQt5**，ClickOCR 是 **PySide6**，同进程不可混用。 |
| 许可证 | ✅ MIT，可自由参考/改编/移植（复制代码需保留版权声明）。 |
| 价值定位 | ✅ 优质**设计参考库**，非依赖。两者功能高度重合（截图/标注/钉图/OCR），架构同构（都是 QGraphicsView 画布 + 浮层）。 |
| 最值得借鉴 | ① 钉图 OS 级点击穿透 ② 画布命令模式撤销/重做 ③ 图元工厂化 + 每图元独立类 ④ 浮动窗口能力基类拆分。 |

---

## 1. 两者定位对比

| 维度 | ScreenPinKit | ClickOCR |
|---|---|---|
| 一句话 | 迷你截图 + 标注 + 钉图白板工具 | 截图 OCR + 标注 + 历史 + 钉图的生产力工具 |
| GUI 绑定 | **PyQt5** | **PySide6** |
| 形态 | 完整应用（`pip install` / `main.py`） | 完整应用（PyInstaller 打包） |
| 范围 | 大而全：截图/屏幕标注/钉图/OCR/插件/取色/白板 | 聚焦：截图→OCR→标注→历史/收藏→贴图 |
| 参考 | Snipaste / Excalidraw / ShareX / ppInk | 自研 + RapidOCR |

---

## 2. 设计思路对比（重点）

### 2.1 浮动窗口职责切分

- **ScreenPinKit**：`base/` 把窗口能力拆成单一职责基类——`pin_window`（置顶）、`mouse_through_window`（穿透）、`drag_window`（拖拽）、`shadow_window`（阴影）、`app_dpi_helper`（DPI）。**组合式复用**，能力可任意叠加。
- **ClickOCR**：单一 `CardWindow` 基类 + 三层浮窗（`ResultOverlay`/`StatusStrip`/`AnnotationToolBar`）。但**钉图（PinDot）逻辑硬塞在 `status_strip.py` 里（约 150 行）**，置顶/拖拽/穿透没有独立抽象。
- **差距**：ClickOCR 的"窗口能力"没有横向拆解，PinDot 是状态栏的附属形态而非独立组件（与上次讨论的"PinDot 抽出"结论一致）。

### 2.2 画布架构（View / Scene / Item 分层 + 图元工厂）

- **ScreenPinKit**：`canvas_editor/` = `canvas_view`(视图 2.5KB) + `canvas_scene`(场景核心 26.8KB) + `canvas_commands`(命令 3.9KB)；`canvas_item/` **每个图元一个类**（arrow / rect / pen / line_strip / text / bubble / eraser / mask / svg / star / number… 约 18 类），职责单一，每个都配 demo。
- **ClickOCR**：`ResultOverlay(QGraphicsView)` + `annotation.py` 的 `AnnotationSession` 集中处理，**图元构建用 if/elif 判断 type** 映射到 `QGraphicsRectItem/LineItem/PathItem/…`，图元逻辑与会话逻辑耦合在一个 809 行文件里。
- **差距**：ClickOCR 缺"图元类"这一层抽象，新增图元（箭头/星形/马赛克遮罩等）要改会话代码，扩展性弱。

### 2.3 撤销 / 重做

- **ScreenPinKit**：**命令模式**（`canvas_commands.py`，Command 对象 + 栈）。
- **ClickOCR**：**快照模式**——`_undo/_redo` 存 `copy.deepcopy(self.data)` 全量快照（≤50 步），恢复历史时按 items 顺序"逐步重放"。
- **差距**：功能已有，但每步深拷贝全量数据（O(n) 内存×步数），语义是"整状态回滚"而非"操作逆操作"。命令模式更省内存、可合并连续笔迹、可扩展。

### 2.4 钉图点击穿透

- **ScreenPinKit**：`mouse_through_window.py` 专门基类 + `Alt+F` 切换穿透。
- **ClickOCR**：**无**（仅有 `pin_label` 内部 `WA_TransparentForMouseEvents`，是"事件落到本窗口"，不是"鼠标穿透到桌面"）。
- **差距**：**明确缺失**，最直接的借鉴点。

### 2.5 主题 / 样式 / 图标

- **ScreenPinKit**：`qss/dark+light` 两套独立文件 + `resource.qrc` + 70+ SVG 图标。
- **ClickOCR**：`theme.py` 集中 QSS 字符串 + `kind` 属性选择器（集中式统一主题）；图标用 Unicode 字符（⭐★⊛☆）。
- **评价**：ClickOCR 集中式更契合既有"统一主题"规范；**SVG 图标可借鉴**——Unicode 符号跨平台字体有回退风险（历史上 ⭐→★ 踩过坑），SPK 用 SVG 更稳。

### 2.6 国际化 / 热键 / OCR

| 项 | ScreenPinKit | ClickOCR | 结论 |
|---|---|---|---|
| 国际化 | i18n `.ts/.qm`（Qt Linguist） | `ui_text.json` + `texts.py T()` | 相近，ClickOCR 轻量方案够用 |
| 热键 | `common/hotkey.py` + `system_hotkey` fork | `hotkey.py`（327 行） | 相近 |
| OCR | PaddleOCR + **QWebEngineView** 文字层（重） | RapidOCR + `QGraphicsTextItem`（轻） | **不借鉴 SPK** |

---

## 3. 值得借鉴的设计点（按优先级）

### P0 — 强烈建议（补 ClickOCR 明确短板）

1. **钉图 OS 级点击穿透**：移植 `mouse_through_window` 思路到 PySide6，PinDot/贴图窗口加"穿透"开关（如 Alt+F），穿透时鼠标事件落到桌面、可随时唤起。收益直接、改动独立。
2. **命令模式撤销/重做**：把 `annotation.py` 的快照式 undo 升级为 `canvas_commands` 式 Command 栈，省内存、支持合并连续笔迹。
3. **图元工厂化 + 每图元独立类**：将 if/elif 构建改为"图元类 + Factory"，为后续新增箭头/星形/马赛克/遮罩等图元留扩展口。

### P1 — 可借鉴（收益中等）

4. **浮动窗口能力基类**：抽 `置顶/穿透/拖拽/阴影/DPI` 为可组合基类，并顺势把 **PinDot 从 `status_strip.py` 抽成独立组件**（呼应 08-15 讨论结论）。
5. **SVG 图标替换 Unicode 符号**：⭐★⊛☆ 等符号改 SVG，根除跨平台字体回退问题。
6. **标注后处理特效**：借鉴 `after_effect_util`/`mask_item`/`eraser_item`，补马赛克/模糊遮罩、橡皮擦。

### P2 — 长期 / 可选

7. **插件系统 + 插件市场 UI**（`internal_plugins` 思路）。
8. **取色放大镜 rgb/hex**（若未来做取色）。
9. **多语言运行时无缝切换**（Qt Linguist 流）。

---

## 4. 不建议借鉴的

- ❌ OCR 文字层用 `QWebEngineView`（资源重、选区体验差，ClickOCR 已有 RapidOCR 轻方案）。
- ❌ `canvas_item/canvas_util.py` 55.9KB 上帝文件（反例，避免 ClickOCR 重蹈）。
- ❌ PyQt5 绑定本身（不可混用）。
- ❌ 整体应用级引入（框架冲突 + 定位重叠，是"竞品"而非"库"）。

---

## 5. 行动计划（分阶段）

> 原则：优先"移植设计思路"而非"复制代码"；复制代码需保留 MIT 声明；每阶段在**代码开发会话**执行（本会话按约定不改业务代码）。

| 阶段 | 内容 | 涉及文件 | 依赖 | 风险 | 验收标准 |
|---|---|---|---|---|---|
| **S1** 钉图穿透 | PinDot/贴图窗口加 OS 级点击穿透开关 | `status_strip.py`（或新 `ui/pin_dot.py`） | 无，独立 | 低 | 穿透态鼠标可点穿到桌面、热键可唤起 |
| **S2** 命令模式撤销 | 快照 undo → Command 栈 | `ui/annotation.py` | 无 | 中（动核心） | 撤销/重做行为一致，内存占用下降 |
| **S3** 图元工厂化 | 图元 if/elif → 类 + Factory | `ui/annotation.py` 拆分 | 建议先 S2 | 中 | 图元行为不变，新增图元不再改会话代码 |
| **S4** 窗口能力基类 + PinDot 抽出 | 抽置顶/穿透/拖拽/阴影/DPI 基类，PinDot 独立成类 | `ui/card_window.py`、`ui/status_strip.py` | 建议先 S1 | 中 | 状态栏行数下降，PinDot 可单测 |
| **S5**（可选）SVG 图标 | Unicode 符号 → SVG 资源 | `ui/theme.py`、`resources/` | 无 | 低 | 跨平台符号显示一致 |
| **S6**（可选）后处理特效 | 马赛克/模糊遮罩、橡皮擦图元 | `ui/annotation.py` + 新图元类 | 依赖 S3 | 中 | 新增图元可用 |
| **S7**（长期）插件系统 | 插件机制 + 市场 UI | 新 `plugins/` | 大 | 高 | 可加载第三方插件 |

**建议顺序**：S1（独立、收益最直接）→ S2+S3（画布重构，一起做最省）→ S4 → 视需求 S5/S6/S7。

---

## 6. 约束与提醒

- 本会话为规划会话，**不改 ClickOCR 业务代码**；上述 S1–S7 交由代码开发会话执行。
- 复制 ScreenPinKit 代码需在文件头保留 MIT 版权与许可声明。
- 移植以"设计思路 + 自行实现"为主，避免照搬 PyQt5 绑定相关的写法。
- 已同步到工作区日志 `memory/2026-08-16.md`。
