# ClickOCR

**端侧离线的「截图即识别、点字即复制」Windows 桌面工具。**

截图 → OCR → 贴图浮窗浮现文字框 → 点击/拖选文字写入剪贴板 → 历史留存（截图 + 缩略图 + 文字 + 标注 + 表格）。
OCR 引擎：**RapidOCR（ONNX Runtime，PP-OCRv6）**，全程零网络请求，数据不出本机。

> 本文档是项目的**唯一权威文档**（产品定义 / 功能 / 架构 / 数据模型 / 配置 / 开发构建 / 里程碑）。
> 历史设计稿与开发日志已归档至 [docs/archive/](docs/archive/)，仅供溯源，不再维护。

---

## 目录

1. [产品定义](#1-产品定义)
2. [核心能力](#2-核心能力)
3. [使用说明](#3-使用说明)
4. [架构](#4-架构)
5. [数据模型](#5-数据模型)
6. [配置体系](#6-配置体系)
7. [环境与运行](#7-环境与运行)
8. [测试](#8-测试)
9. [打包发行](#9-打包发行)
10. [模型](#10-模型)
11. [里程碑现状](#11-里程碑现状)
12. [规划中功能（未实现）](#12-规划中功能未实现)
13. [目录结构](#13-目录结构)
14. [文档与资产](#14-文档与资产)

---

## 1. 产品定义

### 1.1 一句话定义

**ClickOCR**：截图 → OCR → 贴图浮窗点字即复制 → 历史留存的桌面工具。

- **形态**：单一应用（托盘常驻、单进程、单实例）。
- **平台**：Windows 10/11（当前主线）；Linux（麒麟 V10 SP3 / ARM）为 M5 跨平台目标，待启动。
- **共享层**：`core_engine/`（纯逻辑，无 Qt）为内置 vendor 副本，自包含分发——整个目录移动即用。
- **隐私**：模型随包 bundle，无遥测、无更新检查、无云端同步，**运行期零网络调用**。截图、缩略图、OCR 文本、标注均为**明文本地存储**，安全性依赖操作系统账户隔离与磁盘加密。

### 1.2 核心交互闭环

> 全局热键 **Ctrl+Alt+R**（默认，可在设置面板改）/ 托盘「截图」→ 框选屏幕区域 → 后台 OCR → 贴图浮窗浮现文字框 → 点击文字块写入剪贴板 → 用户操作后留存历史。

### 1.3 目标场景

| 场景 | 机器配置 | 模型选择 | 体验目标 |
| --- | --- | --- | --- |
| 新机器办公 | 64GB | PP-OCRv6 Medium | 最快速度 + 最高准确率 |
| 低配机日常 | 8GB | PP-OCRv6 Tiny/Small | 可用、不卡顿 |
| 截图速取 | 任意 | 当前配置 | 截图后直接选中文字复制 |
| 历史回溯 | 任意 | — | 调取最近截图和文字，重新复制 |
| 表格提取 | 任意 | + SLANet 表格模型 | 表格一键识别并导出 |

### 1.4 核心约束

- 高精度 + 轻量双档可配：模型档位 tiny/small/medium 运行期**热切换**。
- 几百 MB 内存可接受，8GB 低配机可运行（引擎懒加载 + 表格模型用后即卸）。
- 表格识别为高频需求（P1）：导出 Excel / Markdown / TSV。
- 失败不崩溃：引擎 / 数据库初始化失败均降级，截图 / 贴图 / 标注照常可用，仅 OCR 停用。

---

## 2. 核心能力

- **OCR**：PP-OCRv6 三档（tiny/small/medium）热切换；CPU / CUDA / DirectML provider 自动选择，GPU 失败自动回退 CPU（设置面板显示**实际落位**）；引擎常驻或懒加载（设置可切）
- **截图**：全局热键触发、框选、滚轮微调选区、放大镜取色、滚动截图（pynput 滚轮模拟 + SIFT 拼接，仅 Windows）
- **贴图浮窗**：点击文字即复制、滚轮缩放（鼠标为锚点 0.2~5.0）、Ctrl+R 用原始截图重新识别
- **选中复制（核心交互）**：编辑器式选择——按下设锚点、拖动按字符扩展（任意方向/跨行）、双击整行、Ctrl 追加多区域；写入即复制（`auto_commit`）；单行纯文本 / 多行 `\n` / 表格 TSV
- **钉图（Pin）**：截图/历史图置顶桌面、上限可调（默认 3 / 硬上限 20）、长按收起、双击切换、可拖动带图；点击穿透（应用级 `Alt+F` + 全局 `Ctrl+Alt+F` 双入口）
- **标注工具**：8 种（选择 / 序号 / 箭头 / 画笔 / 直线 / 矩形 / 马赛克 / 文字），每工具多样式（如矩形 7 种）；参数浮层（样式/颜色/线宽/字号）；撤销重做栈 ≤500（命令模式）；JSON 持久化（旧版本打开新标注优雅降级）
- **表格识别**：SLANet 模型（~6.8MB 内置）→ 表格面板 + Excel / Markdown / TSV 导出（openpyxl）
- **二维码识别**：zxing-cpp，`kind="qr"` 并入 OcrBox，`[QR]` 前缀行入库（存储定稿见 [docs/archive/M2+++最终修改版-QR存储定稿.md](docs/archive/M2+++最终修改版-QR存储定稿.md)）
- **历史记录**：sqlite-vec + FTS5 预置、6 Tab（全部/图/字/码/标/收藏）、日期分组、收藏（不占 FIFO）、关键词搜索、可拉伸记忆尺寸、多选拖拽、拖入导入去重 + 串行补 OCR
- **UI**：明暗双主题跟随系统、全局微软雅黑、快捷键 27 项全可配置（全局/截图/贴图/标注四组）、统一工具栏（常态/标注态两态内联展开）+ 纯信息状态条、UI 文字 JSON 表（346 条）
- **打包**：PyInstaller onedir、DML 通用版 / CUDA 版双线、开机自启（winreg HKCU Run）、资源/数据分离

---

## 3. 使用说明

启动后常驻托盘：

- 右键托盘 → 截图 / 历史记录 / 设置 / 退出
- 全局热键 **Ctrl+Alt+R** 触发框选（可改键、可关闭）
- 全局热键 **Ctrl+`** 打开历史记录（可改键、可关闭）
- 贴图窗口 **Ctrl+R** 重新识别当前图（用保存的原始截图，不重新抓屏）

### 3.1 截图 → 复制

框选（滚轮微调、放大镜取色）→ 贴图浮窗 → 悬停高亮文字框 → 按下拖动选字 / 双击整行 / Ctrl 追加多区域 → 写入剪贴板。框选后按 `R` 进入滚动截图。

### 3.2 贴图浮窗

- **工具栏**（单行）：钉 / 收藏 / 二维码 / 文字框 / 表格 / 标注 / 完成；点「标注」同一行内联展开 8 工具 + 撤销/重做/清空
- **状态条**：状态文字 + 选择预览 + 链锁 / 导航图 / 等比例 3 个辅助开关；Esc 关闭
- **表格按钮**：单击 = 表格模式（框选复制 TSV）；双击 / 长按 = 打开表格窗口
- **钉图**：点钉图收起图片只留钉子（32×32），双击恢复；**穿透**：鼠标点击落到桌面，窗口仍可见

### 3.3 历史记录

- 落库时机：**仅用户操作过才落库**（复制 / 标注 / 收藏 / 钉图 / Ctrl+R / 完成）；`save_no_op_close` 可改为无操作关闭也存
- 双击卡片恢复贴图（关联原记录，后续操作直接更新原行）；多选批量删除 / 收藏 / 复制（Del / Ctrl+D）
- 容量：`history_max_count` 默认 50（可调 1~500），FIFO 清理同步删文件，**收藏豁免**

---

## 4. 架构

### 4.1 分层结构（依赖严格单向向下）

```
L1 入口引导层   main.py
      ↓
L2 应用编排层   app.py（编排中枢）+ appmod/（9 个能力模块）   ← 全局唯一的流程编排者
      ↓
L3 表现层       ui/  →  windows · components · core · base
      ↓
L4 领域服务层   ocr/ · screenshot/ · config.py · hotkey.py
      ↓
L5 共享引擎层   core_engine/（无 Qt · vendor 副本）
      ↓
外部依赖        PySide6 · rapidocr · onnxruntime · OpenCV · mss · rapid_table · zxing-cpp · sqlite-vec · Pillow
```

**依赖规则（硬性）**：L5 不 import 任何 Qt；`ui/core/` 同样无 Qt（可独立单测）；L2 只做编排不实现算法；平台相关代码集中在 `hotkey.py` 与 `scroll_capture.py`；`config.py` 是薄兼容层（转发 `core_engine.config`）。

### 4.2 关键模块

| 模块 | 职责 |
|---|---|
| `app.py` + `appmod/` | 单实例守护（QLocalServer）、托盘与热键、引擎生命周期、窗口配额、落库编排、代次失效（防串图）、拖放导入 |
| `appmod/workers.py` | 常驻 worker：`OcrWorker` / `TableWorker` / `EngineLoader` / `GpuProbeWorker` / `HistorySaveWorker` |
| `ui/windows/` | `selection` 框选、`result_overlay` 贴图浮窗（overlay/ 5 mixin：表格/钉图/缩放导航/文字选择/标注会话）、`history_panel`、`settings`（4 tab） |
| `ui/components/` | 统一工具栏、状态条、标注（`anno/` 图元工厂 + 参数浮层）、`float_group` 浮窗组（z 序/让位/位置跟随）、钉子、导航器 |
| `ui/core/` | 无 Qt 纯逻辑：`anno_core` 命令模式撤销栈、`table_detect` 表格几何推断 |
| `ocr/rapid_engine.py` | RapidOCR 封装（懒加载、热切换、GPU 探测） |
| `core_engine/` | `ai_pipeline`（OcrBox/TableResult 契约）、`db_manager`、`config`、`logging_setup`、`image_decode`（HEIC/AVIF + EXIF 纠正）、`table_engine`、`qr_engine`、`ort_runtime` |

### 4.3 接口契约

```python
# core_engine/ai_pipeline.py —— 唯一定义处
@dataclass
class OcrBox:
    text: str                        # 识别文本
    polygon: List[List[int]]         # 4 顶点坐标
    score: float                     # 置信度 0~1
    kind: str = "ocr"                # ocr=文字 | qr=二维码/条码

class OcrEngine(Protocol):
    def recognize(self, image) -> List[OcrBox]: ...
    def get_info(self) -> dict: ...
    def switch_model(self, model_name: str) -> None: ...
```

换 OCR 引擎只需新增实现类 + 改 `app.py` 一处构造调用。`switch_model` 只重建**内部**引擎、包装对象不变（保住 `table_engine` 绑定的 `recognize` 引用）。

### 4.4 线程模型

| 线程 | 归属 | 用途 |
|---|---|---|
| UI 主线程 | Qt | 渲染、抓屏；落库仅「取快照 + 提交」 |
| `OcrWorker` / `TableWorker` | App（**常驻单线程**） | OCR / SLANet 推理，**只认最新任务** |
| `HistorySaveWorker` | App（常驻） | 编码 + 落库后台队列 |
| `EngineLoader` | App（一次性） | 后台加载模型（GIL 释放，UI 不卡） |
| `GpuProbeWorker` / `ScrollWorker` | 按需 | GPU 探测 / 滚屏抓取拼接 |

> **为什么常驻**：CUDA EP 每新建线程分配 ~188MiB 显存且不回收，按需建线程会耗尽显存。常驻 + 覆盖式任务槽后显存稳定。

### 4.5 关键数据流要点

- **主链路**：热键 → 隐藏操作 UI → 框选 → mss 抓屏 → 贴图 + 浮窗 → OcrWorker → 文字框渲染 → 点击复制 → 操作后落库
- **落库**：主图 + 160px 缩略图（`cv2.INTER_AREA`）+ DB 行 + FIFO 清理；OCR 未完成时先标记，回调补写
- **历史恢复**：`history_id` 关联原记录，标注恢复，后续更新原行；仅 `ocr_boxes IS NULL` 且 `ocr_auto=true` 才补识别
- **表格**：代次校验防串图，识别完成即 `unload_table_model()`（省 150–200MB）
- **滚动截图**：滚屏逐帧抓取 → SIFT 拼接去重 → 长图等比入屏（仅 Windows）

---

## 5. 数据模型

### 5.1 存储布局

```
<app_root>/
├── screenshots/          # 主图（格式/质量由 save_format/save_quality 控制）
│   └── thumb/            # 160px 缩略图（同格式、同文件名）
├── clickocr.db           # SQLite
├── logs/                 # 结构化日志（5MB×3 轮转）
└── resources/            # 随包资源（config.ini / models_v6 / icons / ui_text.json）
```

dev 环境 `<app_root>` = 仓库根；打包后 = exe 同目录。

### 5.2 在用表：`screenshot_history`

| 字段 | 类型 | 说明 |
|---|---|---|
| `screenshot_path` | TEXT NOT NULL | 相对软件目录的路径 |
| `ocr_text` | TEXT | 识别文本（供搜索）；QR 内容以 `[QR] xxx` 前缀行并入 |
| `ocr_boxes` | TEXT | OcrBox 列表 JSON（含 polygon / score / kind） |
| `model_tier` | TEXT NOT NULL | **执行记录**：实际档位；`''` = 未识别 |
| `device` | TEXT NOT NULL | **执行记录**：实际生效设备 cpu/cuda/dml；`''` = 未识别 |
| `created_at` | REAL NOT NULL | 时间戳（落库/置顶时刷新） |
| `file_size` | INTEGER | 截图字节数 |
| `is_favorite` | INTEGER DEFAULT 0 | 收藏（不受 FIFO 限制） |
| `annotations` | TEXT | 标注 JSON |
| `has_table` | INTEGER DEFAULT 0 | 是否含表格 |
| `status` | INTEGER DEFAULT 0 | 向量化状态（预留） |

**两个关键语义**：

1. **`ocr_boxes` 三态**：`NULL` = 从未识别（打开历史图才补识别）；`'[]'` = 已识别无文字；`'[...]'` = 已识别有文字。
2. **`model_tier` / `device` 是执行记录**（回答「这条被哪个档位/设备识别的」），非配置快照；GPU 回退时如实记 `cpu`。

### 5.3 预置表（建库一次性建齐 11 张 + 1 张 vec0 虚表，当前仅 `screenshot_history` 在用）

| 表 | 用途 |
|---|---|
| `images` / `image_ocr` / `image_tags` / `image_search`(FTS5) | 图库主表（主键 = path+size+mtime 哈希）、每行文字一条、三源标签、全文索引（4 触发器） |
| `image_ocr_extend` / `table_cache` / `image_hash` | 区域框选重识别、表格缓存、感知哈希去重 |
| `image_embedding` / `image_embedding_vec` | 向量元数据 + sqlite-vec `vec0`（按扩展可用性动态创建，缺失降级） |
| `image_layout` | DocLayout-YOLO 版面分析（可选） |

### 5.4 文件名与安全

`_build_name(pattern, ext)`：占位符 `{datetime}` `{date}` `{time}` `{ms}` `{ts}` `{seq}` `{ext}`，空模板回退 `{datetime}_{ms}`；非法字符 `[\\/:*?"<>|\r\n\t]` → `_`（同时阻断路径穿越）；扩展名白名单 `png/jpg/jpeg/bmp/webp`；质量钳制 1..100。

---

## 6. 配置体系

`resources/config.ini`（出厂模板，首次运行复制到可写位置），**8 个 section**：

| Section | 关键项 |
|---|---|
| `[software]` | `ocr_model`(v6-tiny/small/medium) · `ocr_device`(cpu/cuda/dml) · `ocr_keep_alive` · `ocr_auto` · `drop_ocr_auto` · `enable_qr` · `history_max_count` · `save_format`/`save_quality`/`name_pattern` · `max_shot_windows`/`max_restore_windows`/`max_pin_windows` · `save_no_op_close` · `cuda_probe` |
| `[ui]` | `theme`（明/暗/跟随系统）· 历史面板宽高/列数/可见卡片 · 边框色/透明度 · `toolbar_enabled` · `strip_enabled` |
| `[hotkey_global]` | `screenshot_combo`(默认 ctrl+alt+r) · `fullscreen_combo` · `history`(ctrl+\`) · `through`(ctrl+alt+f) |
| `[hotkey_select]` | `full_sel` · `scroll_sel` · `color_pick` · `color_fmt` |
| `[hotkey_overlay]` | `reocr` · `table_ocr` · `toggle_anno` · `nav` · `ratio` · `toggle_strip` · `toggle_qr` · `toggle_boxes` · `toggle_table` · `fav` · `copy_shortcut` · `select_all` · `copy_selected` · `copy_image` · `copy_raw_image` · `zoom_modifier` · `geom_zoom_modifier` · `toggle_through`(alt+f) |
| `[hotkey_anno]` | `undo` · `redo` |
| `[engine]` | `ocr_engine` · `use_cls` · `table_model` · `preprocess_preset`（预置未实现） |
| `[annotation]` | 标注默认参数 |

**设计要点**：旧键由 `_LEGACY_MAP` 自动迁移（如 `max_history_windows` → `max_shot_windows`）；关键开关实时读配置（`@property`），设置保存后同会话生效；UI 文案外置 `ui_text.json`（346 条），`T()` 读取缺失回退默认值。

---

## 7. 环境与运行

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

依赖：PySide6 / rapidocr(>=3.9) / onnxruntime / mss / numpy / opencv-python / Pillow(>=11.3) / pillow-heif / sqlite-vec / rapid_table / openpyxl / pynput / zxing-cpp（详见 `requirements.txt`）

> 注：`core_engine` 已 vendor 进本工程（`ClickOCR/core_engine/`），`main.py` 只认内置副本（自包含，目录移动即用）；开发态缺失时 `SystemExit` 明确报错。

```bash
.venv\Scripts\python main.py
```

或双击 `run.bat`。

---

## 8. 测试

```bash
.venv\Scripts\python -m unittest discover -s tests -t . -v   # 全套离线回归（推荐）
.venv\Scripts\python tests\test_db_manager.py                # 单个模块
```

`tests/` 是**离线**套件：不加载推理大模型、不依赖真实显示器（Qt 走 `offscreen`）、不写用户配置与历史库；需要模型/显示环境的用例自己 `skipUnless`。`tests/test_guards.py` 还会把 `scripts/` 下 14 个断言型守卫当子进程跑一遍——**一条命令 = 全套回归**（CI 见 [.github/workflows/tests.yml](.github/workflows/tests.yml)）。

回归首跑建议先跑 `scripts\_test_ocr_chain.py`（配置实时读取 / `@property` 冲突静态检测 / `restore_record` 门控 / 落库三态 / 执行记录语义等 6 组断言）。

> 注：`scripts/` 下有一批需要**真桌面 + 人眼判断**或外部输入的脚本，断言不了「看起来对不对」，仍需手跑：`_test_pin_*.py`、`_test_restore*.py`、`_test_popup_close.py`、`_test_qr_btn_visibility.py`、`_test_num_badge.py`、`_test_select_tool.py`、`_test_text_tool.py`、`_test_anno_text_edit.py`、`_test_table_lines.py <图片>`、`_smoke_packaged.py`、`_bench_*.py`、`_spec_probe.py`（需先跑一次基线 `build/_spec_before.json`）。

---

## 9. 打包发行

onnxruntime 的 gpu 包与 directml 包在 PyPI 上互斥，必须分 venv、分构建线：

| 构建线 | 环境 | spec | 产物 |
|---|---|---|---|
| **DML 通用版**（推荐分发） | `.venv-dml`（onnxruntime-directml） | `ClickOCR_dml.spec` | 490MB，通吃 AMD/Intel/NVIDIA |
| **CUDA 版** | `.venv`（onnxruntime-gpu） | `ClickOCR.spec` | ~1.9GB，NVIDIA 最优 |
| ~~纯 CPU 版~~ | — | ~~`ClickOCR_cpu.spec`~~ | 已弃用（保留备查） |

**推荐入口是 `scripts\build_package.py`**（可重复执行：预检 → 清理 → 打包 → 归位 → 校验 → 冒烟 → 归档）：

```bash
.venv-dml\Scripts\python scripts\build_package.py dml --zip --smoke   # DirectML 通用版（推荐）
.venv\Scripts\python     scripts\build_package.py gpu --zip --smoke   # CUDA 版
```

它把「命令跑通了、结果却是错的」固化成硬校验（任一项不过即非零退出）：档位与解释器的 ExecutionProvider 匹配、加速原生库逐字节一致、产物 `ocr_device` 等于档位值、spec datas 逐项落位、发布包无运行期数据。冒烟产物的 `config.ini` / `clickocr.db` / `logs\` 会被移出，不进分发包。

> **DML 配置覆写（关键）**：`resources/config.ini` 两线共用（开发机为 cuda），DML 版打包时把 `ocr_device` 覆写为 `dml` 打进包（源码不动），否则产物静默回退 CPU、DirectML 形同虚设。
>
> 手敲 `python -m PyInstaller` 的坑：COLLECT 输出恒为 `<distpath>\<COLLECT.name>`，`--distpath dist\ClickOCR_dml` 会双层嵌套；确需手敲用 `--distpath dist` 再改名。
>
> 其它坑位：冒烟必须等 ≥35 秒（DML 首载约 15s+）；单实例命名管道会干扰冒烟判定；CUDA 打包需预加载 nvidia DLL（否则真机静默回退 CPU）。

产物：`dist\<档位>\ClickOCR.exe`（onedir）；`--zip` 另出 `dist\ClickOCR_dml-<版本>-<日期>.zip`，解压即用（绿色免安装）。资源读 `_internal\`，数据写 exe 旁 `config.ini` / `clickocr.db` / `screenshots\` / `logs\`。

单独跑产物冒烟：

```bash
.venv\Scripts\python scripts\_smoke_packaged.py dist\ClickOCR_dml dml
```

### 9.1 本地瘦身

以下内容**对打包新版本不必要**，可安全删除（打包/运行会自动重建）：

| 内容 | 说明 |
|---|---|
| `dist/` `build/` `wheels/` | 旧打包产物 / PyInstaller 中间产物 / pip 离线缓存 |
| `logs/` `screenshots/` `clickocr.db` `config.ini` | 运行期数据（注意：截图历史在 `screenshots/` + `clickocr.db` 里，删了即清空历史） |
| `__pycache__/` `.ruff_cache/` | 缓存 |
| 不用的 venv | 打哪条版本线留哪个（DML → `.venv-dml`；CUDA → `.venv`） |

**打包必需**：源码 + spec + `scripts/build_package.py` + `resources/`（含全部模型）+ 对应 venv。
从 GitHub 克隆后编译前，须先按 [`resources/PP-OCRv6_models_manifest.txt`](resources/PP-OCRv6_models_manifest.txt) 下载 medium/small 四个 onnx（162MB），否则产物只有 tiny 档。

---

## 10. 模型

`resources/models_v6/`：

- `PP-OCRv6_det_{tiny,small,medium}.onnx`
- `PP-OCRv6_rec_{tiny,small,medium}.onnx`
- `ppocrv6_dict.txt` / `ppocrv6_tiny_dict.txt`

三档按 `[software] ocr_model`（`v6-tiny` / `v6-small` / `v6-medium`）选择，运行期可 `switch_model` 热切换。

**获取方式：手动下载。** medium/small 四个文件因体积（合计 162MB）不入库，从 [`resources/PP-OCRv6_models_manifest.txt`](resources/PP-OCRv6_models_manifest.txt) 里按名称取 ModelScope 链接下载，校验清单中的 sha256 后放入本目录即可（tiny 两个已随仓库提供）。

**禁止依赖运行时下载**——打包产物必须自带模型（bundle 离线）。

---

## 11. 里程碑现状

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 截图 → OCR → 复制 最小骨架（2026-08-09 ~ 08-10）：引擎定型 RapidOCR(ONNX)、mss 框选、贴图浮窗、编辑器式选区、RegisterHotKey 热键、托盘入口 | ✅ 完成 |
| M2 | 截图模式完善 + core_engine 抽取（2026-08-11 ~ 08-19）：数据与存储层、热键改键/冲突检测、三面板、双主题、Ctrl+R 重识别、标注工具、打包、表格识别（SLANet）、GPU 加速、档位热切换、QR 存储定稿 | ✅ 完成 |
| M3 | 迭代改进 T1~T6（2026-08）：贴图配额修复（`max_shot_windows`/`max_restore_windows`）、历史窗口可拉伸/记忆/多选拖拽、状态栏关闭联动、二维码识别 UI（zxing-cpp）、边框配色 | ✅ 完成 |
| M4 | GPU 加速矩阵 / 图片去重 | 🟡 EP 矩阵已完成（CUDA 7.54× / DML 6.58×）；去重表已建未启用 |
| M5 | 跨平台（麒麟 V10 / ARM Linux） | ⏳ 待启动 |

**M5 演进方向**：引擎层与 PySide6 本就跨平台，平台相关只在外壳。改造点：`HotkeyManager`（X11 `XGrabKey`，Wayland GlobalShortcuts portal）· `ScreenshotCaptor` · `TrayAdapter` · `FontResolver`；NUMA 探测与 Linux EP（migraphx/openvino）已就绪；建议先攻 X11。

---

## 12. 规划中功能（未实现）

| 功能 | 状态 | 说明 |
|---|---|---|
| 看图模式（多图浏览 + OCR 持久化 + 区域框选重识别） | 📋 远期 | VS Code 风格三栏布局，防抖自动 OCR，与截图模式共享引擎与选中复制交互 |
| 图像预处理（几何矫正 / OpenCV 增强 / 超分） | 🟡 仅配置预置 | `preprocess_preset` 键已预置，算法未实现 |
| 首次启动智能推荐 + 新手引导 | 📋 远期 | 硬件检测推荐模型档位 + 3 步引导 |
| 剪贴板图片粘贴（Ctrl+V） | 📋 远期 | — |

完整需求原文见 [docs/archive/需求说明-v0.2.md](docs/archive/需求说明-v0.2.md) §4。

---

## 13. 目录结构

```
ClickOCR/
├── README.md                # 本文件（唯一权威文档）
├── main.py / app.py         # 入口（main→App）+ 编排中枢
├── appmod/                  # 编排能力模块（workers/engine/history/float/capture/drop/save_queue/image_encode）
├── config.py                # core_engine.config 的薄兼容层
├── hotkey.py                # Windows 全局热键（RegisterHotKey + nativeEvent）
├── requirements.txt
├── run.bat
├── ClickOCR.spec            # PyInstaller（CUDA 版）
├── ClickOCR_dml.spec        # PyInstaller（DirectML 通用版，推荐）
├── ClickOCR_cpu.spec        # PyInstaller（CPU 版，已弃用，保留备查）
├── spec_common.py           # 三份 spec 共用的收集/瘦身/收尾逻辑
├── ocr/                     # 引擎抽象 + RapidOcrEngine
├── screenshot/              # mss 抓屏 + 滚动截图
├── core_engine/             # 共享引擎层（无 Qt，vendor 副本）
├── ui/                      # 四层分包：base / components / core / windows
├── resources/
│   ├── icons/               # SVG 图标（currentColor 单色规范）
│   ├── models_v6/           # PP-OCRv6 ONNX bundle
│   ├── config.ini           # 出厂配置模板（只读；首次运行复制到可写位置）
│   └── ui_text.json         # UI 文字表（346 条）
├── config.ini               # 实际生效的用户配置（dev 在仓库根；打包在 exe 同目录）
├── scripts/                 # 开发期验证脚本（断言守卫 / UI 冒烟 / 基准）
├── tests/                   # 离线回归测试
└── docs/
    ├── assets/              # 引用资产（系统架构图等）
    ├── architecture/        # 架构图 HTML/JSON
    └── archive/             # 历史文档归档（仅供溯源）
```

---

## 14. 文档与资产

本文档即全部权威文档。其余文件仅供溯源 / 可视化：

- [docs/assets/系统架构图.html](docs/assets/系统架构图.html) — 交互式系统架构图
- [docs/architecture/clickocr-architecture.html](docs/architecture/clickocr-architecture.html) — 架构总览（配套 JSON）
- [docs/archive/](docs/archive/) — 历史归档：需求说明、系统架构设计 v3.1、统一工具栏方案、M1–M3 里程碑与开发日志/计划、QR 存储定稿、代码评审记录等（内容已吸收进本 README，不再维护）
