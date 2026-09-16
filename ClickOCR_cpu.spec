# -*- mode: python ; coding: utf-8 -*-
"""【已废弃 2026-08-29，勿再用于出包】CPU 版打包配置（瘦身版）。

废弃原因：本 spec 从 onnxruntime-gpu 环境（.venv）打包后再剔除 CUDA 库，
产物只剩 CPUExecutionProvider —— 设置里选 DirectML 检测必然失败
（probe_dml_devices 首行判 DmlExecutionProvider 不在 available providers），
既无 GPU 加速也无通用性，价值低。

通用版改由 .venv-dml + ClickOCR_dml.spec 产出（含 DirectML.dll）：
本机实测 v6-medium 设备0 6.58×，与 CUDA 的 6.65× 持平，且通吃 AMD/Intel/NVIDIA。

**本文件现已完全冗余**：出纯 CPU 包只需把 ClickOCR.spec 里的
`C.drop_nvidia_tree(a)` 换成 `C.prune_cuda_and_ffmpeg(a)`（该文件内已注明）。
依赖收集 / 瘦身逻辑已全部收敛到 spec_common.py —— 原先「保留本文件仅供对照
其中的依赖收集 / 瘦身逻辑」的理由已消失，可随时删除。

排除项（均经实测/源码核对，安全）：
1. rapidocr 未用推理后端 paddle/openvino/mnn/pytorch/tensorrt
   —— ClickOCR 显式 Det/Rec.engine_type=ONNXRUNTIME，仅加载 onnxruntime 后端。
2. opencv_videoio_ffmpeg*.dll（~29MB）—— 视频编解码，ClickOCR 仅截图(mss)+图像数组运算。
3. resources/models（PP-OCRv3 回退模型）—— 回退依赖 rapidocr_onnxruntime 未随包，路径已失效。
4. rapidocr/models/ 内 v6 small det/rec（~30MB）—— 与 resources/models_v6 重复的缓存。
   保留 ch_ppocr_mobile_v2.0_cls_mobile.onnx：rapidocr 构造期会加载 cls。
"""
import sys

sys.path.insert(0, SPECPATH)     # spec 执行时 PyInstaller 注入 SPECPATH = 本目录
import spec_common as C          # noqa: E402

# 出厂模板本身即 cpu，无需覆写默认档位
datas = C.resource_datas()
binaries = []
hiddenimports = []

binaries += C.collect_sqlite_vec()
_d, _b, _h, _excludes = C.collect_rapidocr()
datas += _d; binaries += _b; hiddenimports += _h
_d, _b, _h = C.collect_zxingcpp()
datas += _d; binaries += _b; hiddenimports += _h
hiddenimports += C.core_engine_submodules()
binaries = C.drop_ffmpeg(binaries)


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
# 剥离 CUDA / TensorRT 运行库（约 884MB）与 ffmpeg：运行时 onnxruntime 无 CUDA 原生库，
# 自动回退 CPU ExecutionProvider（device=cpu）
C.prune_cuda_and_ffmpeg(a)

exe = C.build_exe(EXE, PYZ, a, 'ClickOCR')
coll = C.build_collect(COLLECT, exe, a, 'ClickOCR')
