# -*- mode: python ; coding: utf-8 -*-
"""DML 通用版打包配置（DirectML，通吃 AMD / Intel / NVIDIA 的 DX12 显卡）。

产物：dist/ClickOCR/（含 DirectML.dll 17.6MB —— 这是「通用版能否开 GPU 加速」的关键）。
本机实测 v6-medium 设备0 6.58×，与 CUDA 的 6.65× 持平。

必须由 onnxruntime-directml 环境（.venv-dml）执行。
收集 / 瘦身 / 收尾逻辑统一在 spec_common.py，本文件只保留 DML 专属的两件事：
产物内默认加速档位、DirectML.dll 兜底。
"""
import sys

sys.path.insert(0, SPECPATH)     # spec 执行时 PyInstaller 注入 SPECPATH = 本目录
import spec_common as C          # noqa: E402

# ---- DML 版默认加速档位覆写（关键，勿删）----
# resources/config.ini 是**出厂模板**（只读），三份产物共用同一个源文件，其
# ocr_device 是通用兜底值 cpu。但本产物里只有 DmlExecutionProvider：沿用 cpu 时
# DirectML 永远不会被启用 —— 表现就是「装了 DirectML 版却一直跑 CPU，GPU 加速
# 形同虚设」。故在打包时把 ocr_device 覆写成 dml，只作用于本 spec 的产物。
_SEED_CFG = C.seed_config_entry('dml', SPECPATH)

datas = C.resource_datas(config_entry=_SEED_CFG)
binaries = []
hiddenimports = []

# ---- 依赖收集（slim：剔除 rapidocr 未用后端与重复的 v6 模型缓存）----
binaries += C.collect_sqlite_vec()
_d, _b, _h, _excludes = C.collect_rapidocr()
datas += _d; binaries += _b; hiddenimports += _h
_d, _b, _h = C.collect_zxingcpp()
datas += _d; binaries += _b; hiddenimports += _h
hiddenimports += C.core_engine_submodules()
hiddenimports += C.PILLOW_HEIF_HIDDENIMPORTS
binaries += C.pillow_heif_dlls()
binaries = C.drop_ffmpeg(binaries)
# DML 专属：DirectML.dll 若被 hook 漏收，产物只剩 CPU EP（设置里检测不到设备）
C.append_directml_fallback(binaries)


a = Analysis(
    ['main.py'],
    pathex=[SPECPATH],  # 由 spec 自身位置推导；写绝对路径换机/CI 必挂
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_excludes,
    noarchive=False,
    optimize=0,
)
# 防呆：剔除 CUDA / TensorRT 运行库（directml 与 gpu 包互斥，正常不会命中；
# 若误在含 CUDA 的环境执行，可避免把 ~884MB CUDA 库打进来）
C.prune_cuda_and_ffmpeg(a)

exe = C.build_exe(EXE, PYZ, a, 'ClickOCR')
coll = C.build_collect(COLLECT, exe, a, 'ClickOCR')
