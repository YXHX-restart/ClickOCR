# -*- mode: python ; coding: utf-8 -*-
"""CUDA 版打包配置（完整 onnxruntime-gpu，含 CUDA 运行库）。

产物：dist/ClickOCR_gpu/（约 1.2GB，给有 NVIDIA GPU + CUDA 环境用户）。
通用版（DirectML，含 DirectML.dll）走 ClickOCR_dml.spec → dist/ClickOCR/。

收集 / 瘦身 / 收尾的公共逻辑在 spec_common.py，本文件只留 GPU 专属部分。
"""
import sys

sys.path.insert(0, SPECPATH)     # spec 执行时 PyInstaller 注入 SPECPATH = 本目录
import spec_common as C          # noqa: E402

# GPU 版整个 resources/ 打进去（通用版只打用到的子项，见另两个 spec）
datas = C.resource_datas(whole_dir=True)
binaries = []
hiddenimports = []

# ---- 依赖收集 ----
binaries += C.collect_sqlite_vec()
_d, _b, _h, _ = C.collect_rapidocr(slim=False)      # GPU 版不做瘦身
datas += _d; binaries += _b; hiddenimports += _h
_d, _b, _h = C.collect_zxingcpp()
datas += _d; binaries += _b; hiddenimports += _h
hiddenimports += C.core_engine_submodules()
hiddenimports += C.PILLOW_HEIF_HIDDENIMPORTS
binaries += C.pillow_heif_dlls()
# GPU 专属：cudnn 9 拆分 DLL 重定向到 onnxruntime/capi/（否则 CUDA provider 静默回退 CPU）
C.append_cudnn_split(binaries)


a = Analysis(
    ['main.py'],
    pathex=[SPECPATH],  # 由 spec 自身位置推导；写绝对路径换机/CI 必挂
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
# GPU 构建：默认保留 onnxruntime-gpu 的 CUDA 运行库（cublas/cudnn/nvrtc/cuda EP 等，约 884MB）。
# 如需出纯 CPU 版（约 361MB），把上面一行换成 C.prune_cuda_and_ffmpeg(a) 即可
# （缺 CUDA 库时 onnxruntime 自动回退 CPU provider）。
C.drop_nvidia_tree(a)       # 剔除与 onnxruntime/capi/ 重复的 nvidia/ 副本（约 141MB）

exe = C.build_exe(EXE, PYZ, a, 'ClickOCR_gpu')
coll = C.build_collect(COLLECT, exe, a, 'ClickOCR_gpu')
