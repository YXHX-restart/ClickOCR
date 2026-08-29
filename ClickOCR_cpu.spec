# -*- mode: python ; coding: utf-8 -*-
"""【已废弃 2026-08-29，勿再用于出包】CPU 版打包配置（瘦身版）。

废弃原因：本 spec 从 onnxruntime-gpu 环境（.venv）打包后再剔除 CUDA 库，
产物只剩 CPUExecutionProvider —— 设置里选 DirectML 检测必然失败
（probe_dml_devices 首行判 DmlExecutionProvider 不在 available providers），
既无 GPU 加速也无通用性，价值低。

通用版改由 .venv-dml + ClickOCR_dml.spec 产出（含 DirectML.dll）：
本机实测 v6-medium 设备0 6.58×，与 CUDA 的 6.65× 持平，且通吃 AMD/Intel/NVIDIA。
保留本文件仅供对照其中的依赖收集 / 瘦身逻辑。

排除项（均经实测/源码核对，安全）：
1. rapidocr 未用推理后端 paddle/openvino/mnn/pytorch/tensorrt
   —— ClickOCR 显式 Det/Rec.engine_type=ONNXRUNTIME，仅加载 onnxruntime 后端。
2. opencv_videoio_ffmpeg*.dll（~29MB）—— 视频编解码，ClickOCR 仅截图(mss)+图像数组运算。
3. resources/models（PP-OCRv3 回退模型）—— 回退依赖 rapidocr_onnxruntime 未随包，路径已失效。
4. rapidocr/models/ 内 v6 small det/rec（~30MB）—— 与 resources/models_v6 重复的缓存；
   ClickOCR 用显式 Det/Rec.model_path（tiny/small 均指向 resources/models_v6），从不引用。
   保留 ch_ppocr_mobile_v2.0_cls_mobile.onnx：rapidocr 构造期会加载 cls（未传 Cls.model_path 时走默认缓存）。
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
# CPU 版：剥离 onnxruntime-gpu 自带的 CUDA 运行库（cublas/cudnn/nvrtc/cuda EP 等，约 884MB）。
# 运行时 onnxruntime 无 CUDA 原生库，自动回退 CPU ExecutionProvider（device=cpu）。
# 同时剔除 cv2 标准钩子注入的 opencv_videoio_ffmpeg*.dll（视频编解码，ClickOCR 无视频 I/O）。
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
