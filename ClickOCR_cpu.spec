# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_all

datas = [('resources', 'resources')]
binaries = []
hiddenimports = []
binaries += collect_dynamic_libs('sqlite_vec')
tmp_ret = collect_all('rapidocr')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
    pathex=['E:/WorkSpace'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# 普通版（CPU）：剥离 onnxruntime-gpu 自带的 CUDA 运行库（cublas/cudnn/nvrtc/cuda EP 等，约 884MB）。
# 运行时 onnxruntime 无 CUDA 原生库，自动回退 CPU ExecutionProvider（device=cpu）。
_CUDA_BIN_PATTERNS = (
    'onnxruntime_providers_cuda', 'onnxruntime_providers_tensorrt',
    'cublas', 'cudnn', 'cufft', 'nvrtc',
    'nvonnxparser', 'nvinfer', 'nvparsers',
)
a.binaries = [
    (name, path, typ) for (name, path, typ) in a.binaries
    if not any(pat in name.lower() for pat in _CUDA_BIN_PATTERNS)
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
