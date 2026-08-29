# -*- mode: python ; coding: utf-8 -*-
"""DML 通用版打包配置（DirectML，通吃 AMD / Intel / NVIDIA 的 DX12 显卡）。

与 ClickOCR_cpu.spec 的关系：
- 二者收集/瘦身逻辑基本一致（rapidocr 未用后端、ffmpeg、重复 v6 模型缓存等），
  差别只在**打包环境**：本 spec 必须由 onnxruntime-directml 环境（.venv-dml）执行。
- CUDA 剔除规则保留为防呆：directml 与 gpu 包互斥，正常不会命中；
  若误在含 CUDA 的环境执行，可避免把 ~884MB CUDA 库打进来（此时产物无 DML 能力）。

注意：DmlExecutionProvider 依赖 capi/DirectML.dll（17.6MB），
下方有兜底逻辑确保它进入产物——这是"通用版能否开 GPU 加速"的关键文件。
"""
import os
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_all, collect_submodules


def _bn(path):
    return os.path.basename(path.replace('\\', '/'))


# 只打包用到的 resources 子项（排除 resources/models 的 v3 回退模型）
# PyInstaller datas 语义：目录项 dest 必须带目录名（否则内容被展开到 dest 平铺）；
# 单文件项 dest 指向目录（否则被当目录创建，产出 config.ini/config.ini 嵌套）
datas = [
    ('resources/icons', 'resources/icons'),
    ('resources/models_v6', 'resources/models_v6'),
    ('resources/config.ini', 'resources'),
    ('resources/ui_text.json', 'resources'),
    ('resources/PP-OCRv6_models_manifest.txt', 'resources'),
]
binaries = []
hiddenimports = []

binaries += collect_dynamic_libs('sqlite_vec')

# rapidocr 未用推理后端（仅 onnxruntime 被加载）
_EXCLUDE_ENGINES = (
    'rapidocr.inference_engine.paddle',
    'rapidocr.inference_engine.openvino',
    'rapidocr.inference_engine.mnn',
    'rapidocr.inference_engine.pytorch',
    'rapidocr.inference_engine.tensorrt',
)
_EXCLUDE_ENGINE_DIRS = tuple('/inference_engine/%s/' % e.split('.')[-1] for e in _EXCLUDE_ENGINES)
# rapidocr/models/ 内仅保留 cls；v6 small det/rec 与 resources/models_v6 重复
_KEEP_RAPIDOCR_MODELS = {'ch_ppocr_mobile_v2.0_cls_mobile.onnx', '.gitkeep'}

tmp_ret = collect_all('rapidocr')
for d in tmp_ret[0]:
    src, dst = d[0], d[1]
    norm = src.replace('\\', '/')
    if any(seg in norm for seg in _EXCLUDE_ENGINE_DIRS):
        continue  # 未用后端的数据文件
    if '/rapidocr/models/' in norm and _bn(src) not in _KEEP_RAPIDOCR_MODELS:
        continue  # v6 small det/rec 重复缓存
    datas.append(d)
for b in tmp_ret[1]:
    if any(b[0].startswith(p + '.') or b[0] == p for p in _EXCLUDE_ENGINES):
        continue
    binaries.append(b)
for h in tmp_ret[2]:
    if any(h == p or h.startswith(p + '.') for p in _EXCLUDE_ENGINES):
        continue
    hiddenimports.append(h)

tmp_ret = collect_all('zxingcpp')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# core_engine 已内置到 ClickOCR/core_engine，确保所有子模块被打包
hiddenimports += collect_submodules('core_engine')

# 剔除 opencv ffmpeg 视频编解码库（保留 cv2.pyd 主体）
binaries = [b for b in binaries if 'opencv_videoio_ffmpeg' not in b[0].lower()]

# DirectML.dll 兜底：DML EP 的原生实现若被 hook 漏收，产物将只剩 CPU EP，
# 表现为设置里 DirectML 检测不到设备。此处仅在缺失时补入。
try:
    import onnxruntime as _ort
    _dml_dll = os.path.join(os.path.dirname(_ort.__file__), 'capi', 'DirectML.dll')
    if os.path.isfile(_dml_dll) and not any(_bn(b[0]) == 'DirectML.dll' for b in binaries):
        # 二元组语义：(源文件, 目标目录)；与 onnxruntime.dll 同目录 ORT 才能找到
        binaries.append((_dml_dll, os.path.join('onnxruntime', 'capi')))
except Exception:  # noqa: BLE001
    pass

a = Analysis(
    ['main.py'],
    pathex=['E:/WorkSpace/ClickOCR'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=list(_EXCLUDE_ENGINES),
    noarchive=False,
    optimize=0,
)
# 防呆：剔除 CUDA / TensorRT 运行库（directml 环境本就没有，正常为空操作）
_CUDA_BIN_PATTERNS = (
    'onnxruntime_providers_cuda', 'onnxruntime_providers_tensorrt',
    'cublas', 'cudnn', 'cufft', 'nvrtc',
    'nvonnxparser', 'nvinfer', 'nvparsers',
)
a.binaries = [
    (name, path, typ) for (name, path, typ) in a.binaries
    if not any(pat in name.lower() for pat in _CUDA_BIN_PATTERNS)
    and 'opencv_videoio_ffmpeg' not in name.lower()
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ClickOCR',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ClickOCR',
)
