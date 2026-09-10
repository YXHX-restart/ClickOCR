# ClickOCR

截图 → OCR → 复制 / 标注 / 表格识别 / 钉图 的 Windows 桌面工具。OCR 引擎：**RapidOCR（ONNX Runtime，PP-OCRv6）**。

## 当前状态

| 里程碑                         | 状态    | 内容                                                     |
| --------------------------- | ----- | ------------------------------------------------------ |
| [M1](docs/milestones/M1.md) | ✅ 完成  | 截图→OCR→复制 最小骨架                                         |
| [M2](docs/milestones/M2.md) | ✅ 完成  | 截图模式完善 + core_engine 抽取 + 标注 + 表格识别 + GPU 加速 + QR 存储定稿 |
| [M3](docs/milestones/M3.md) | ⏳ 进行中 | 贴图窗口数量限制、历史窗口改进、二维码识别 UI、边框配色                          |
| M5 跨平台                      | ⏳ 待启动 | 麒麟 V10 / ARM Linux                                     |

## 核心能力

- **OCR**：PP-OCRv6 三档（tiny/small/medium）热切换；CPU / CUDA / DirectML provider 自动选择；引擎常驻或懒加载（设置可切）
- **截图**：全局热键触发、框选、滚轮微调选区、滚动截图（pynput 滚轮模拟）
- **贴图浮窗**：点击文字即复制、滚轮缩放（鼠标为锚点 0.2~5.0）、Ctrl+R 重新识别
- **钉图（Pin）**：截图/历史图置顶桌面、上限可调（默认 3 / 上限 20）、长按收起、双击切换、可拖动带图
- **标注工具**：矩形（5 种）/ 箭头（5 种）/ 画笔（5 种）/ 线段（5 种）/ 马赛克 / 文字 / 序号；撤销重做栈 ≤500；JSON 持久化（旧版本打开新标注优雅降级）
- **表格识别**：SLANet 模型（~6.8MB 内置）→ 表格面板 + Excel 导出（openpyxl）
- **历史记录**：sqlite-vec + FTS5、日期分组、收藏（不占 FIFO）、关键词搜索、可拉伸记忆尺寸、多选拖拽
- **二维码识别**：zxing-cpp（M3 实现，存储结构已定稿见 [docs/archive/M2+++最终修改版-QR存储定稿.md](docs/archive/M2+++最终修改版-QR存储定稿.md)）
- **UI**：明暗双主题跟随系统、全局微软雅黑、快捷键 27 项全可配置（全局/截图/贴图/标注四组）、UI 文字 JSON 表（274 条）
- **打包**：PyInstaller onedir、开机自启（winreg HKCU Run）、资源/数据分离

## 环境

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

依赖：PySide6 / rapidocr(>=3.9) / onnxruntime / mss / numpy / opencv-python / Pillow(>=11.3) / pillow-heif / sqlite-vec / rapid_table / openpyxl / pynput / zxing-cpp（详见 `requirements.txt`）

> 注：`core_engine` 已 vendor 进本工程（`ClickOCR/core_engine/`），`main.py` 优先加载内置副本，
> 仅在缺失时才回退到上级目录 `E:\WorkSpace\core_engine`——因此整个 ClickOCR 文件夹移动即用。
> （旧布局的上级目录副本已于 2026-09-10 删除，该回退分支现为死路径，待后续清理。）

## 运行

```bash
.venv\Scripts\python main.py
```

或双击 `run.bat`。启动后常驻托盘：

- 右键托盘 → 截图 / 历史记录 / 设置 / 退出
- 全局热键 **Ctrl+Shift+R** 触发框选（可改键、可关闭）
- 全局热键 **Ctrl+`** 打开历史记录（可改键、可关闭）
- 贴图窗口 **Ctrl+R** 重新识别当前图

## 测试

```bash
.venv\Scripts\python -m pytest tests\ -q          # 冒烟：合成图 → RapidOcrEngine.recognize
.venv\Scripts\python tests\test_engine.py
```

> 注：`scripts/` 下 `_test_*.py` / `_diag_*.py` / `_bench_*.py` / `_smoke_packaged.py` 为开发期验证脚本（offscreen QTest），非正式 pytest 套件，后续计划收编进 `tests/`。

## 打包

推荐 **DirectML 通用版**（通吃 AMD / Intel / NVIDIA 的 DX12 显卡），须用 `onnxruntime-directml` 环境：

```bash
.venv-dml\Scripts\python -m PyInstaller ClickOCR_dml.spec --noconfirm --distpath dist\ClickOCR_dml
.venv\Scripts\python      -m PyInstaller ClickOCR.spec                              # CUDA 版（~1.9GB）
```

> `ClickOCR_cpu.spec` 路线已弃用（在 gpu 包里剔 CUDA 库得不到 DML），保留仅供备查。
> DML spec 会在打包时把默认配置的 `ocr_device` 覆写为 `dml`（源码 config 不动），
> 否则产物只有 `DmlExecutionProvider`，会静默回落 CPU、DirectML 形同虚设。

打包产物：`dist\<名>\ClickOCR.exe`（onedir 模式），资源读 `_internal\`、数据写 exe 旁 `config.ini` / `clickocr.db` / `screenshots/` / `logs/`。

产物冒烟（判定进程存活 / stderr 无 Traceback / stdout 无单实例提示 / 日志关键行 / accel 断言）：

```bash
.venv\Scripts\python scripts\_smoke_packaged.py dist\ClickOCR_dml dml
```

## 模型

`resources/models_v6/`：

- `PP-OCRv6_det_{tiny,small,medium}.onnx`
- `PP-OCRv6_rec_{tiny,small,medium}.onnx`
- `ppocrv6_dict.txt` / `ppocrv6_tiny_dict.txt`

三档按 `config.ini` 的 `[software] ocr_model`（`v6-tiny` / `v6-small` / `v6-medium`）选择，运行期可 `switch_model` 热切换。

**获取方式：手动下载。** medium/small 四个文件因体积（合计 162MB）不入库，从
[`resources/PP-OCRv6_models_manifest.txt`](resources/PP-OCRv6_models_manifest.txt) 里按名称取
ModelScope 链接下载，校验清单中的 sha256 后放入本目录即可（tiny 两个已随仓库提供）。

**禁止依赖运行时下载**——打包产物必须自带模型（bundle 离线）。

## 目录结构

```
ClickOCR/
├── README.md                # 本文件
├── main.py / app.py         # 入口（main→App）
├── config.py                # core_engine.config 的薄兼容层
├── hotkey.py                # Windows 全局热键（RegisterHotKey + nativeEvent）
├── requirements.txt
├── run.bat
├── ClickOCR.spec            # PyInstaller（CUDA 版）
├── ClickOCR_dml.spec        # PyInstaller（DirectML 通用版，推荐）
├── ClickOCR_cpu.spec        # PyInstaller（CPU 版，路线已弃用，保留备查）
├── ocr/                     # 引擎抽象 + RapidOcrEngine
├── screenshot/              # mss 抓屏 + 滚动截图
├── ui/                      # 四层分包：base / components / core / windows
├── resources/
│   ├── icons/               # SVG 图标（currentColor 单色规范）
│   ├── models_v6/           # PP-OCRv6 ONNX bundle
│   ├── config.ini           # 默认配置
│   └── ui_text.json         # UI 文字表（~103 条）
├── scripts/                 # 临时验证脚本（_test_* / _diag_* / _bench_* / _smoke_packaged）
├── tests/                   # 冒烟测试
└── docs/                    # 文档（详见下方索引）
```

## 文档索引

> 旧的设计文档、开发日志、过期进展均归档到 [docs/archive/](docs/archive/)，**权威现状**只看本 README 和 `docs/milestones/`。

### 里程碑（现状）

- [docs/milestones/M1.md](docs/milestones/M1.md) — 截图→OCR→复制 最小骨架 ✅
- [docs/milestones/M2.md](docs/milestones/M2.md) — 截图完善 + core_engine + 标注 + 表格 + GPU + QR 存储 ✅
- [docs/milestones/M3.md](docs/milestones/M3.md) — 迭代改进（T1~T6）⏳

### 设计文档

- [docs/design/系统架构设计.md](docs/design/系统架构设计.md)
- [docs/design/统一工具栏与状态栏方案.md](docs/design/统一工具栏与状态栏方案.md)
- [docs/design/SVG图标方案.md](docs/design/SVG图标方案.md)

### 实现规格

- [docs/spec/需求说明-v0.2.md](docs/spec/需求说明-v0.2.md) — 原始需求（209KB，后续计划拆分专题）
- [docs/spec/后端检测面板规格.md](docs/spec/后端检测面板规格.md)
- [docs/spec/T2历史窗口改进-实现规格.md](docs/spec/T2历史窗口改进-实现规格.md)

### 归档（历史）

`docs/archive/` 下保存原始开发计划、开发日志、定稿文档、过期进展，供溯源：

- M1/M2/M3 各自的开发计划与开发日志（已被 `docs/milestones/` 摘要替代）
- `M2+++最终修改版-QR存储定稿.md` — QR 存储结构定稿（M3 T4 UI 依赖此结构）
- `ScreenPinKit对比与借鉴计划.md` — 钉图功能借鉴来源
- `2026-08-13开发日志.md` / `最近进展日志-2026-08-15.md` — 日期进展（已被里程碑摘要吸收）

### 资产

- [docs/assets/blueprint-workbench.html](docs/assets/blueprint-workbench.html) — 蓝图工作台
- [docs/assets/系统架构图.html](docs/assets/系统架构图.html)
