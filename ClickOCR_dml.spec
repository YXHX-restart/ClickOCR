# -*- mode: python ; coding: utf-8 -*-
"""DML 通用版打包配置（DirectML，通吃 AMD / Intel / NVIDIA 的 DX12 显卡）。

与 ClickOCR_cpu.spec 的关系：
- 二者收集/瘦身逻辑基本一致（rapidocr 未用后端、ffmpeg、重复 v6 模型缓存等），
  差别只在**打包环境**：本 spec 必须由 onnxruntime-directml 环境（.venv-dml）执行。
- CUDA 剔除规则保留为防呆：directml 与 gpu 包互斥，正常不会命中；
  若误在含 CUDA 的环境执行，可避免把 ~884MB CUDA 库打进来（此时产物无 DML 能力）。

注意：DmlExecutionProvider 依赖 capi/DirectML.dll（17.6MB），
下方有兜底逻辑确保它进入产物——这是"通用版能否开 GPU 加速"的关键文件。

另一处关键：打包会把 resources/config.ini 的 `ocr_device` **覆写为 dml** 再打进包
（见下方"默认加速档位覆写"块）。源码里的该文件是 CUDA / DML 共用的一份（值为 cuda），
若不覆写，DML 产物会尝试 CUDA 失败后回落 CPU，DirectML 永远不会被启用。
"""
import os
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_all, collect_submodules


def _bn(path):
    return os.path.basename(path.replace('\\', '/'))


# ---- DML 版默认加速档位覆写（关键，勿删）----
# resources/config.ini 是 CUDA / DML 两份构建**共用**的同一个文件，内容是开发机上的
# 真实配置（ocr_device = cuda）。但 DML 产物里只有 DmlExecutionProvider，沿用 cuda 时
# rapidocr 会告警 "CUDAExecutionProvider is not in available providers" 后回落 CPU——
# 表现就是"装了 DirectML 版却一直跑 CPU，GPU 加速形同虚设"。
# 故在打包时把 ocr_device 覆写成 dml，只作用于本 spec 的产物：
#   · 源码 resources/config.ini 一个字不改 → CUDA 版照旧、开发环境照旧
#   · 不引入任何运行期分支 → .py 业务代码零改动
_SRC_CFG = 'resources/config.ini'
_cfg_lines = open(_SRC_CFG, encoding='utf-8').read().splitlines(keepends=True)
_patched = 0
for _i, _ln in enumerate(_cfg_lines):
    _s = _ln.lstrip()
    if _s.startswith('ocr_device') and '=' in _s:
        _head, _, _tail = _ln.partition('=')
        _comment = ''
        for _mark in ('#', ';'):                 # 保留行尾注释
            _pos = _tail.find(_mark)
            if _pos != -1:
                _comment = _tail[_pos:].rstrip('\r\n')
                _tail = _tail[:_pos]
                break
        _eol = '\r\n' if _ln.endswith('\r\n') else ('\n' if _ln.endswith('\n') else '')
        _cfg_lines[_i] = _head + '= dml' + ((' ' + _comment) if _comment else '') + _eol
        _patched += 1
        break
if _patched != 1:
    raise SystemExit('ClickOCR_dml.spec: resources/config.ini 里没找到唯一的 ocr_device 键，'
                     '拒绝产出"加速档位不确定"的 DML 包')
# 产物内的文件名必须叫 config.ini，故在临时目录里放成 config.ini 再交给 datas
_DML_CFG_DIR = os.path.join(SPECPATH, 'build', '_dml_default_cfg')
os.makedirs(_DML_CFG_DIR, exist_ok=True)
_DML_CFG = os.path.join(_DML_CFG_DIR, 'config.ini')
with open(_DML_CFG, 'w', encoding='utf-8') as _f:
    _f.writelines(_cfg_lines)
print('[DML spec] 默认配置 ocr_device 已覆写为 dml -> %s' % _DML_CFG)

# 只打包用到的 resources 子项（排除 resources/models 的 v3 回退模型）
# PyInstaller datas 语义：目录项 dest 必须带目录名（否则内容被展开到 dest 平铺）；
# 单文件项 dest 指向目录（否则被当目录创建，产出 config.ini/config.ini 嵌套）
datas = [
    ('resources/icons', 'resources/icons'),
    ('resources/models_v6', 'resources/models_v6'),
    (_DML_CFG, 'resources'),                 # ← 覆写版（ocr_device = dml），非源码原件
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
# ---- 现代图片格式解码（拖放导入 avif / heic / heif）----
# AVIF 由 Pillow(>=11.3) 内置；HEIC/HEIF 走 pillow_heif（自带 libheif）。
# pillow_heif 的二进制（_pillow_heif.pyd + libheif-*.dll）位于 site-packages
# 根目录而非包内 → 显式声明 pyd，由 pyd 依赖分析把 libheif DLL 一并带走。
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
