# ClickOCR M2 开发日志

> 版本范围：M2（设置面板重组 · 历史记录增强 · 标注工具打磨 · 贴图交互重构 · **表格识别 + GPU 加速（由 M4 提前）**）
> 引擎：RapidOCR（ONNX Runtime），PP-OCRv6-tiny（离线 bundle，同 M1）；表格识别 RapidTable（SLANet）；GPU onnxruntime-gpu + CUDA 13
> 状态：M2 各项功能完成，含多轮真机/实机验收修复；`_test_m2b` 回归持续全绿；M2 扩展（表格识别+GPU）已交付并通过 headless 验证
> 主要日期：2026-08-12（当日完成大部分 UI/交互迭代与验收修复 + M2 扩展开发）

---

## 1. 概述

M2 围绕「截图的后续生产力」展开：历史记录可管理可收藏、设置面板体系化、标注工具可商用化（就地编辑/字体/主题化/独立浮层）、贴图窗口与状态栏彻底分离（修复"截图截到状态栏/标注把状态当画布"两大结构性问题），并完成 PyInstaller 打包闭环（`dist/ClickOCR/`）。

**最终结论：M2 各功能完成并通过实机验收；期间修复了多个深水坑——顶层 QWidget QSS 背景不渲染（WA_StyledBackground）、真机顶层窗 width() 返回 2px 怪癖、QTabWidget pane 纯白盖卡片、测试污染真实配置文件等；UI 样式最终全部收敛到 `ui/theme.py` 单一来源。**

---

## 2. M2 完成情况

| 编号 | 任务 | 状态 | 说明 |
|---|---|---|---|
| M2-1 | 设置面板三 Tab 重组 | ✅ | 软件设置 / UI设置 / 快捷键设置；历史保留条数不设上限（1~20亿）；历史图片目录可配置（相对/绝对路径，保存即时生效） |
| M2-2 | 截图遮罩 Esc 取消 | ✅ | `showEvent` 抢焦点 + QShortcut(ApplicationShortcut) 兜底（Qt.Tool 窗口默认不抢键盘焦点） |
| M2-3 | 历史记录「收藏」Tab | ✅ | 全部/收藏两页；收藏置顶、搜索联动、空态提示；`ConfigManager.save()` 修复显式 path 写回 |
| M2-4 | 标注工具（M2·γ） | ✅ | 矩形/箭头/画笔/马赛克/文字五工具 + 撤销重做栈 + 就地文字编辑 + 字体选择；Ctrl+Shift+D 入口；标注持久化到历史 |
| M2-5 | Ctrl+R 重新 OCR | ✅ | 重抓原区域重识别，覆盖更新历史记录（不新增） |
| M2-6 | PyInstaller 打包 | ✅ | onedir + config 资源/数据分离（`_MEIPASS` 只读默认值 vs exe 旁用户数据）+ 开机自启；产物 `dist/ClickOCR/`（~360MB，体积待瘦身） |
| M2-7 | 贴图交互重构 | ✅ | 状态栏独立浮层（StatusStrip）；顶部拖动把手；关闭联动；宽度上限 1.5×标注栏；标注栏锚定状态栏下方/上方 |
| M2-8 | 贴图窗口上限 | ✅ | `[ui] max_history_windows`（默认 1，可配 1-10）；新开销毁最旧 |
| M2-9 | UI 样式统一主题类 | ✅ | 三面板 + 标注工具栏 + 角标按钮 + StatusStrip + ColorPickerDialog 全部收敛到 `ui/theme.py`；组件零内嵌 QSS |
| M2-10 | 表格识别（由 M4 提前） | ✅ | `core_engine/table_engine.py`：RapidTable 3.0.2 + SLANet（slanet-plus 6.8MB 随包）；识别 4×4 表格 13 单元格；惰性加载 + 卸载 |
| M2-11 | 表格导出（由 M4 提前） | ✅ | `core_engine/table_export.py`：Excel（合并单元格+边框）/ Markdown / TSV / HTML 四格式全通过 |
| M2-12 | GPU 加速（由 M4 提前） | ✅ | onnxruntime-gpu 1.28.0 + CUDA 13.6 + cuDNN 9.24；`CUDAExecutionProvider` 激活（RTX 4060）；无 GPU 自动回退 CPU |
| M2-13 | 表格识别 UI 入口 | ✅ | Ctrl+Shift+T + 贴图信号；`ui/table_panel.py` 预览面板（QTableWidget + 导出/复制）；异步 TableWorker 不卡 UI |

### 额外交付（验收迭代中新增）

- 亮色主题白色统一降 5% 亮度（卡片 `rgba(236,236,238,244)`、输入框 `#f2f2f2`），白背景可区分
- 下拉框箭头主题化：QPainter 生成主题色三角 PNG + `::down-arrow image`（Qt QSS 只支持 image）
- 自定义颜色选择器 `ColorPickerDialog`（替代系统 QColorDialog）：24 色板 + hex 输入，CardWindow 浮层主题化；再点击 toggle 关闭、锚定按钮上方
- `ThemeSpinBox`：QSpinBox 去原生箭头 + 主题 ▲▼ 按钮
- 标注工具栏：独立顶层浮层（无边框/置顶/Tool）、可拖动（屏幕 clamp）、画图让位（绘制中光标碰到自动隐藏、这笔画完恢复）
- 截图时自动隐藏**所有**程序浮窗（贴图/状态栏/标注栏/历史/设置），取消恢复——任何 ClickOCR UI 不进截图
- 架构蓝图工作台（资料库在线版）：8 章蓝图 + 里程碑 + 待办 + 更新日志四模块，`https://workbuddy.link/p/KEEknI7biRbv8R6z9kUQ04`

---

## 3. 关键变更

### 3.1 状态栏与图片彻底分离（修复两大结构性问题）

用户反馈两个严重 bug：① 截图会截到状态栏；② 打开标注后把状态栏当画布。根因同源——**状态带是图片窗口 scene 的一部分**（图片下方多了一块 `band`），标注画布 = 整个 scene。

重构（`ui/status_strip.py` 新建 + `ui/result_overlay.py` 瘦身）：

- `StatusStrip(CardWindow)` 独立浮层：提示行 + 选择预览行 + 文字框/标注角标按钮；空白区拖动联动移动图片窗口
- ResultOverlay **scene/窗口高 = 纯图片**；`_set_status/_set_preview` 转发；`set_status_strip` 关联按钮信号
- moveEvent/showEvent/resizeEvent → `_sync_status_strip_pos`（状态栏跟随图片正下方 y+4，屏幕 clamp）
- 标注画布天然只含图片；截图时贴图+状态栏+标注栏+历史+设置全量隐藏，取消/完成恢复

### 3.2 标注工具栏独立浮层 + 画图让位

- 独立顶层窗体（无 parent + Frameless + 置顶 + Tool + 半透明），拖动 clamp 屏幕可用区，位置记忆复用
- **画图让位**（三轮迭代收敛）：全局光标轮询（80ms，仅标注模式）→ 最终规则 = **仅在按住左键绘制中，光标碰到标注栏/状态栏才隐藏，并保持到这一笔画完（松手恢复）**；未绘制时移到工具栏上保持可见可点按钮

### 3.3 贴图交互细节

- 顶部 8px 拖动把手（无需 Alt，悬停抓手光标）；Alt+拖动任意位置保留；中部仍选字
- 关闭图片 → 状态栏同步关闭（closeEvent 补 close）
- 状态栏宽度 = `max(340, min(图片宽, 标注栏宽 × 1.5))`；标注栏锚定**状态栏**下方（放不下翻上方，用 strip 顶做锚防重叠）
- 标注按钮（同文字框）checkable 状态显示；`[result] anno_toolbar_default` 配置截图后默认进入标注

### 3.4 UI 样式统一主题类（`ui/theme.py` 单一来源）

- 顶层 QWidget 自身 QSS background 在全局样式表下**不渲染**（`style()` 是 QCommonStyle）→ 工具栏内部套 `_BarCard(QFrame#anno_card, WA_StyledBackground)`，与三面板同构
- 角标按钮 `kind='corner'`、色板 `ColorPickerDialog`、StatusStrip 均纳入主题 `_PANEL_NAMES`/选择器；组件零内嵌 QSS
- 下拉箭头：`_arrow_path(color)` QPainter 生成三角 PNG（明暗两色），`apply_theme` 替换 `__COMBO_ARROW__` 占位

### 3.5 配置与打包

- 新增 `[paths] screenshots_dir`（历史图片目录可配）、`[ui] max_history_windows`、`[result] show_text_boxes` / `anno_toolbar_default`
- `ConfigManager.save()` 三分支：显式 path → 写回该 path（测试不污染真实配置）；开发 → 写 `resources/config.ini`；打包 → 写 exe 旁
- 打包 config 资源/数据分离：`res_root=_MEIPASS`（只读默认）vs `data_root=exe 目录`（config.ini/db/screenshots/logs）

---

## 4. 遇到的问题与解决（M2 深水坑）

| 问题 | 根因 | 解决 |
|---|---|---|
| 标注工具白背景看不清 | AnnotationToolBar 是 QWidget，QSS 背景缺 `WA_StyledBackground` 不绘制 | 加属性；后统一为 `#anno_card` 子卡片 |
| 工具栏"位置不对"偏右 346px | **真机顶层窗 `width()` 返回 2px**（布局未激活），`sizeHint()/geometry()` 正常 | 独立浮窗尺寸一律用 sizeHint() |
| 历史面板亮色下纯白 255 看不清 | QTabWidget `::pane` 默认背景 = palette window（浅色纯白），盖住卡片 236 | 两套主题加 `#HistoryPanel QTabWidget::pane{background:transparent}` |
| Shift+C 复制失效 | `resources/config.ini` 被早期测试污染（copy_shortcut 被改） | 恢复默认；**测试必须用临时 cfg（显式 path）**，绝不让测试碰真实配置 |
| 截图截到状态栏 | 旧贴图浮窗（含状态带）留在屏幕上被再次框选截入 | 截图开始全量隐藏程序浮窗，取消恢复 |
| 标注把状态当画布 | 状态带在 scene 内 = 标注画布一部分 | 状态/图片分离重构（§3.1） |
| 标注模式 Ctrl+Shift+D 无效 | ResultOverlay 是 Qt.Tool 窗口不抢焦点 | showEvent 抢焦点 + QShortcut(WindowShortcut) 兜底（与 SelectionOverlay Esc 同款） |
| 多次点击累积多个颜色窗体 | 每次 _pick 都新建 dialog | 单实例复用 + 再点击 toggle 关闭 |
| Esc 无法取消截图 | Qt.Tool 全屏遮罩不抢键盘焦点 | showEvent 抢焦点 + QShortcut(ApplicationShortcut) 兜底 |
| 测试写入真实配置文件 | 测试用 `ConfigManager()` 默认 app_root | `ConfigManager(path=tmp)` 显式 path（`_path_explicit` 标志） |
| 顶层窗 QSS 背景不渲染 | 全局样式表对顶层 QWidget background 不绘制 | 内部子卡片承载背景（三面板同构模式） |
| QMouseEvent.mapToScene 报 TypeError | mapToScene 只接受 QPoint(int)，传 QPointF 报错 | 用 `e.pos()`（QPoint） |

**通用教训**：所有 `Qt.Tool` 自绘浮窗（SelectionOverlay/ResultOverlay）都要 showEvent 抢焦点 + QShortcut 兜底，否则键盘交互真机失效；独立浮窗尺寸用 sizeHint() 不用 width()/height()。

---

## 4.5 M2 扩展：表格识别（SLANet）+ GPU 加速（由 M4 提前）

> 用户决策（08-12 晚）：将原 M4 的「表格识别 + GPU 加速」提前到 M2 立即开发。工作台里程碑 M2 改「进行中」，M4 改占位。

### 4.5.1 技术选型

- **表格识别**：RapidTable 3.0.2（RapidAI）——SLANet 表格结构序列预测，模型 `slanet-plus.onnx`（~6.8MB）**已打包进 whl**，无需联网下载；单元格文字复用主引擎 RapidOCR（v6-tiny），与项目零新增模型下载。
- **GPU**：`onnxruntime-gpu==1.28.0`（与 CPU 版同版本对齐）+ NVIDIA 运行时 DLL 走 pip 包 `nvidia-cublas`（cu13）+ `nvidia-cudnn-cu13`，免系统级 CUDA Toolkit 安装。

### 4.5.2 实现要点

| 模块 | 文件 | 说明 |
|---|---|---|
| 表格引擎 | `core_engine/table_engine.py`（新） | `TableEngine`：`load_table_model`（惰性）/ `unload_table_model` / `recognize_table` → `TableResult`；解析 RapidTable `pred_htmls` → `TableCell` 网格（含 row_span/col_span/坐标）；外部注入 OCR 回调识别单元格文字 |
| 表格导出 | `core_engine/table_export.py`（新） | `TableExportService`：`export_excel`（openpyxl，保留合并单元格+边框+首行加粗+列宽自适应）/ `export_markdown` / `to_tsv` / `to_html` |
| GPU 加速 | `ocr/rapid_engine.py`（改） | 构造参数 `use_cuda`；v6 构建时注入 `CUDAExecutionProvider`；`_probe_gpu()` 用现成 v6 模型建 session 探测真实 provider；`get_info()` 返回真实 `gpu_available` |
| UI 入口 | `ui/table_panel.py`（新） | `TablePreviewPanel`：QTableWidget 渲染（双击可编辑）+ 导出 Excel/MD + 复制 TSV；纳入 `theme.py` 主题 |
| 快捷键 | `ui/result_overlay.py`（改） | 新增 `table_requested` 信号 + `Ctrl+Shift+T` + QShortcut 兜底（与 Ctrl+Shift+D 同款） |
| 编排 | `app.py`（改） | `TableWorker(QThread)` 异步识别；`_table_recognize` / `_on_table_done` / `_on_table_failed`；初始化 `TableEngine`（注入主引擎 recognize） |
| 配置 | `core_engine/config.py` + `resources/config.ini` | `[ocr] device=cpu\|cuda`、`[ocr] table_model=slanet_plus` |

### 4.5.3 深水坑

| 问题 | 根因 | 解决 |
|---|---|---|
| RapidTable 输出 pred_htmls 为空 | `use_ocr=False` 时 `__call__` 直接 `continue`，**不生成 HTML** | 必须 `use_ocr=True`（外部传 `ocr_results` 会优先覆盖内部引擎，不会双跑 OCR） |
| CUDA provider 回退 CPU | `cublasLt64_13.dll` 找不到——nvidia pip 包 DLL 在独立目录，onnxruntime 延迟加载不认 `add_dll_directory` | 把 DLL 复制到 `site-packages/onnxruntime/capi/`（cublasLt64_13 / cublas64_13 / nvrtc64_130_0 / nvrtc-builtins64_133 / cudnn64_9） |
| `nvidia-cublas-cu13` 包名废弃 | pip 构建报 DEPRECATED | 改用新版统一包 `nvidia-cublas`（13.6.0.2）+ `nvidia-cudnn-cu13`（9.24） |

### 4.5.4 验证结果

- `_test_m2e.py` headless 全绿：导出四格式 / TableEngine 端到端（4×4、13 cells、md 含 Alice）/ GPU 激活（`gpu_available=True`）/ 面板构造关闭
- ResultOverlay 回归：Ctrl+Shift+T 新信号触发；Ctrl+R（reocr）、Ctrl+Shift+D（标注）不受影响
- GPU 实测：tiny 模型 CPU 0.18s vs GPU 0.165s（1.09x——tiny 模型过小收益有限，small/medium 与表格模型收益更大）；表格识别 GPU 0.64s（SLANet 加载 0.39s）
- `_test_m2b` 热键段失败为 offscreen 环境无法注册真实全局热键（hotkey.py 未改动，非本次引入）

### 4.5.5 遗留

- 打包需 `--collect` rapid_table / openpyxl / CUDA DLL（onnxruntime capi 内）
- GPU 对 tiny 提速有限；后续大模型档位（M3 智能推荐）收益明显
- 表格预览面板编辑功能已就位，`table_cache` 缓存命中逻辑可随 M3 联调

---

## 5. 交付文件清单（M2 新增/重构）

```
E:\WorkSpace\ClickOCR\
├── ui/
│   ├── widgets.py            # ThemeSpinBox / ColorButton / ColorPickerDialog（共用组件）
│   ├── status_strip.py       # StatusStrip 独立状态栏浮层
│   ├── annotation.py         # 标注工具：AnnotationToolBar（独立浮层）+ AnnotationSession
│   ├── theme.py              # UI 类：明暗两套 QSS 统一管理全部面板/工具栏/按钮
│   ├── history_panel.py      # QTabWidget 全部/收藏
│   ├── settings_panel.py     # 三 Tab 重组 + 新配置项
│   ├── card_window.py        # 浮层基类（DragBar/CardWindow）
│   ├── result_overlay.py     # 贴图：纯图片 scene / 拖动把手 / 标注联动 / 让位轮询 / 表格快捷键
│   └── table_panel.py        # 表格识别预览面板（QTableWidget + 导出 Excel/MD + 复制 TSV）
├── core_engine/
│   ├── config.py             # save 三分支 / get_screenshots_dir / [ocr] device+table_model
│   ├── db_manager.py         # list_favorites / set_favorite / update_ocr / update_annotations
│   ├── autostart.py          # 开机自启（HKCU Run）
│   ├── table_engine.py       # TableEngine（SLANet 惰性加载 / recognize_table / 卸载）
│   └── table_export.py       # TableExportService（Excel/Markdown/TSV/HTML）
├── app.py                    # _make_overlay / 贴图窗口上限 / 截图全量隐藏 / TableWorker 表格识别
├── scripts/_test_m2b.py      # 回归（持续全绿）
├── scripts/_test_m2e.py      # M2 扩展 headless 验证（表格+GPU+面板）
└── dist/ClickOCR/            # PyInstaller onedir 产物
```

---

## 6. 遗留事项与后续建议（M3 方向）

1. **打包瘦身**：`--collect-all rapidocr` 导致体积 ~360MB，M3 需裁剪（精确收集 onnxruntime/sqlite-vec 依赖）
2. **多显示器**：框选遮罩仅覆盖主屏，虚拟桌面多屏需扩展（M1 遗留）
3. **GPU 加速**：onnxruntime-gpu / DirectML EP 未启用（`get_info()` 已预留 gpu_available）
4. **模型档位**：`[ocr] model_set` 可加 v6-small/medium
5. **选区模型**：caret 定位基于 QFontMetrics 近似，极端字体仍有偏差（可引入逐字符框）
6. **标注字体持久化**：JSON 已含 font 字段，恢复贴图已还原；标注工具栏字体选择建议后续记忆用户偏好
