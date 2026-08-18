# ClickOCR (M1 · alpha)

截图 → OCR → 复制的最小可用骨架。**OCR 引擎：RapidOCR（ONNX Runtime）**。

## 环境

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows
pip install -r requirements.txt
```

## 运行（M1·α）

```bash
python main.py                # 监听 Ctrl+Shift+R → 截图 → 控制台打印识别文字
```

> M1·α 仅打通端到端链路（控制台输出），贴图窗口 / 点击复制 / 托盘在 M1·β / M1·γ 实现。

## 冒烟测试

```bash
python tests/test_engine.py   # 合成图 → RapidOcrEngine.recognize → 校验输出
```

## 模型

首次运行会由 RapidOCR 自动下载 ONNX 模型（~30MB）到站点包目录。
下载后执行 `python scripts/sync_models.py` 将其同步到 `resources/models/`，
即可离线 / CI 使用（引擎会优先读取 `resources/models/config.yaml`）。

## 目录

```
ocr/            引擎抽象 (types) + RapidOcrEngine 实现
screenshot/     mss 抓屏服务
ui/             贴图窗口（M1·β 实现）
resources/models/  ONNX 模型 bundle
tests/          冒烟测试
scripts/        模型同步脚本
```
