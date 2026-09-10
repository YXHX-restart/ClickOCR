# -*- mode: python ; coding: utf-8 -*-
"""CUDA 版打包配置（完整 onnxruntime-gpu，含 CUDA 运行库）。

产物：dist/ClickOCR_gpu/（约 1.2GB，给有 NVIDIA GPU + CUDA 环境用户）。
通用版（CPU + DirectML 捆绑）走 ClickOCR_cpu.spec → dist/ClickOCR/。
"""
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import collect_submodules
import os

datas = [('resources', 'resources')]
binaries = []
hiddenimports = []
binaries += collect_dynamic_libs('sqlite_vec')
tmp_ret = collect_all('rapidocr')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('zxingcpp')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# core_engine 已内置到 ClickOCR/core_engine，确保所有子模块被打包
hiddenimports += collect_submodules('core_engine')
# ---- 现代图片格式解码（拖放导入 avif / heic / heif）----
# AVIF 由 Pillow(>=11.3) 内置；HEIC/HEIF 走 pillow_heif（自带 libheif）。
# pillow_heif 的二进制（_pillow_heif.pyd + libheif-*.dll）位于 site-packages
# 根目录而非包内，collect_dynamic_libs('pillow_heif') 收不到 → 显式声明 pyd，
# 由 PyInstaller 的 pyd 依赖分析把 libheif DLL 一并带走。
hiddenimports += ['_pillow_heif', 'pillow_heif.HeifImagePlugin']
hiddenimports += ['PIL.ImageOps']      # EXIF 方向纠正（exif_transpose / transpose）
hiddenimports += ['PIL.AvifImagePlugin']   # AVIF 插件（Pillow 动态注册，防漏收）
# pillow_heif 的依赖 DLL（libheif / libde265 / libx265 / libstdc++ / libgcc_s /
# libwinpthread）由 delvewheel 铺在 site-packages 根目录且为**延迟加载**，
# PyInstaller 的导入表分析易漏收 → 显式 glob 收集到 _internal 根目录，
# 与 _pillow_heif.pyd 同目录（delvewheel 加载器按同目录查找）。
try:
    import glob as _glob
    import sysconfig as _sysconfig
    _sp = _sysconfig.get_paths()['purelib']
    for _pat in ('libheif-*.dll', 'libde265-*.dll', 'libx265-*.dll',
                 'libstdc++-*.dll', 'libgcc_s_*-*.dll', 'libwinpthread-*.dll'):
        for _dll in _glob.glob(os.path.join(_sp, _pat)):
            binaries.append((_dll, '.'))
except Exception:                        # pragma: no cover
    pass
# 补收 cudnn 9 拆分 DLL（cudnn_adv64_9.dll 等 9 个）：onnxruntime hook 只收主 dll
# （cudnn64_9.dll，位于 onnxruntime/capi/），拆分 DLL 若不在同目录，
# 打包版真机加载 cudnn64_9.dll 时找不到依赖 → CUDA provider 静默回退 CPU。
# 重定向到 onnxruntime/capi/ 与主 dll 同目录；主 dll 本身已由 hook 收集，跳过。
# 注意：binaries 源列表项为二元组 (src, dest)（三元组是 Analysis 后 a.binaries 的格式）。
for _src, _dst in collect_dynamic_libs('nvidia.cudnn'):
    _fn = os.path.basename(_src)
    if _fn == 'cudnn64_9.dll':
        continue
    binaries.append((_src, os.path.join('onnxruntime', 'capi')))


a = Analysis(
    ['main.py'],
    pathex=['E:/WorkSpace/ClickOCR'],
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
# GPU 构建：默认包含 onnxruntime-gpu 的 CUDA 运行库（cublas/cudnn/nvrtc/cuda EP 等，约 884MB）。
# 如需出 CPU 版（约 361MB），取消下方注释块即可排除 CUDA 库（缺 CUDA 时自动回退 CPU provider）：
# _CUDA_BIN_PATTERNS = (
#     'onnxruntime_providers_cuda', 'onnxruntime_providers_tensorrt',
#     'cublas', 'cudnn', 'cufft', 'nvrtc',
#     'nvonnxparser', 'nvinfer', 'nvparsers',
# )
# a.binaries = [
#     (name, path, typ) for (name, path, typ) in a.binaries
#     if not any(pat in name.lower() for pat in _CUDA_BIN_PATTERNS)
# ]

# 去重：依赖分析会把 cudnn 拆分 DLL 按原始目录收到 nvidia/cudnn/bin/，
# 与上方重定向到 onnxruntime/capi/ 的同一批 DLL 重复（约 141MB）。
# 运行时 cudnn64_9.dll 只从同目录（onnxruntime/capi/）加载拆分 DLL，
# nvidia/ 下的副本无人引用，剔除。
a.binaries = [
    (name, path, typ) for (name, path, typ) in a.binaries
    if not name.replace('\\', '/').startswith('nvidia/')
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ClickOCR_gpu',
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
    name='ClickOCR_gpu',
)
