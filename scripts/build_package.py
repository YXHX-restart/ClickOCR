# -*- coding: utf-8 -*-
"""scripts/build_package.py — ClickOCR 可重复打包入口（PyInstaller onedir）。

为什么要有这个脚本，而不是继续手敲 PyInstaller 命令
--------------------------------------------------
手敲命令在实测里踩过三个坑，共同点是「命令跑通了，结果却是错的」：

① **产物落到双层嵌套目录**。COLLECT 的输出恒为 `<distpath>/<COLLECT.name>`，而 name
   写在 spec 里（dml/cpu 是 `ClickOCR`、gpu 是 `ClickOCR_gpu`）。于是
   `--distpath dist\\ClickOCR_dml` 实际得到 `dist\\ClickOCR_dml\\ClickOCR\\ClickOCR.exe`，
   照着 README 去 `dist\\ClickOCR_dml\\ClickOCR.exe` 找 exe 会找不到。
   本脚本统一先建到临时 distpath，再整目录搬到 `dist/<档位>`，与 spec 里的名字解耦。

② **档位与解释器不匹配时打包不报错**。用 `.venv` 去跑 `ClickOCR_dml.spec`，产物只剩
   CPU EP，PyInstaller 全程退出码 0，要等用户运行期才发现「GPU 加速没生效」。
   本脚本在预检阶段就断言当前解释器的 ExecutionProvider 与档位匹配。

③ **加速档位只在 spec 里覆写、出包后无人复核**。spec 会把产物内出厂 `config.ini` 的
   `ocr_device` 覆写成档位值，但没有任何东西在「打完之后」验证它真的生效了。
   本脚本把 `ocr_device`、DirectML.dll 等作为**出包后硬校验**，不通过则非零退出。

另外补一条「不该出现在发布包里」的校验：`clickocr.db` / `screenshots/` / `logs/` /
exe 同级的 `config.ini` 都是**运行期在 exe 旁**生成的用户数据。它们若出现在刚打好的
产物里，说明打包前那个目录被运行过 —— 等于把本机状态当发布包发出去。

用法
----
    .venv-dml\\Scripts\\python.exe scripts\\build_package.py dml
    .venv-dml\\Scripts\\python.exe scripts\\build_package.py dml --zip --smoke
    .venv\\Scripts\\python.exe     scripts\\build_package.py gpu      # CUDA 版（~1.9GB）

参数
----
    --zip        额外产出免安装分发包 dist/ClickOCR_<档位>-<版本>-<日期>.zip
    --smoke      出包校验通过后，调用 scripts/_smoke_packaged.py 做启动冒烟
    --no-clean   保留上一份产物（默认先删除，保证「可重复执行」语义）
    --keep-work  保留 PyInstaller 的 build 工作目录（排查用）

退出码
------
    0 全部通过；1 预检或打包失败；2 出包后校验不通过（产物已生成但不可信）。
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
WORK_ROOT = ROOT / "build"
#: 临时 distpath：先建在这里，校验后再整目录搬到 dist/，避免 COLLECT 名字带来的嵌套
TMP_DIST = WORK_ROOT / "_distout"

#: 档位表。
#:   spec          —— PyInstaller 配置文件名
#:   collect_name  —— spec 里 `COLLECT(name=...)` 的值，决定 PyInstaller 产出的目录名
#:   out_name      —— 最终产物目录名（dist/<out_name>）
#:   ep            —— 该档位必备的 ExecutionProvider（预检断言当前解释器）
#:   ep_dlls       —— 必须出现在产物 `_internal/onnxruntime/capi/` 的原生库
#:   accel         —— 产物内出厂 config.ini 期望的 ocr_device；None = spec 不覆写，沿用模板
#:   venv_hint     —— 报错时提示用户该用哪个虚拟环境
FLAVORS: dict[str, dict] = {
    "dml": {
        "spec": "ClickOCR_dml.spec",
        "collect_name": "ClickOCR",
        "out_name": "ClickOCR_dml",
        "ep": "DmlExecutionProvider",
        "ep_dlls": ["onnxruntime.dll", "DirectML.dll"],
        "accel": "dml",
        "venv_hint": ".venv-dml",
    },
    "gpu": {
        "spec": "ClickOCR.spec",
        "collect_name": "ClickOCR_gpu",
        "out_name": "ClickOCR_gpu",
        "ep": "CUDAExecutionProvider",
        "ep_dlls": ["onnxruntime.dll", "onnxruntime_providers_cuda.dll"],
        "accel": None,
        "venv_hint": ".venv",
    },
    "cpu": {
        "spec": "ClickOCR_cpu.spec",
        "collect_name": "ClickOCR",
        "out_name": "ClickOCR_cpu",
        "ep": "CPUExecutionProvider",
        "ep_dlls": ["onnxruntime.dll"],
        "accel": None,          # spec 不覆写：沿用模板值
        "venv_hint": ".venv-dml",
    },
}

#: 发布包里**不应该**出现的东西：全部由运行期在 exe 旁生成（属本机/个人状态）
FORBIDDEN_IN_RELEASE = ("clickocr.db", "config.ini", "logs", "screenshots", "thumb")

#: 必须齐备的 resources 子项（缺一则 OCR/二维码/多语言必坏）
REQUIRED_RESOURCES = ("icons", "models_v6", "config.ini", "ui_text.json")

#: 产出目录里库/数据的分家目录（PyInstaller 6 的 onedir 布局）
INTERNAL = "_internal"

_failures: list[str] = []


def say(msg: str = "") -> None:
    print(msg, flush=True)


def check(name: str, ok: bool, extra: str = "") -> bool:
    """记录一条校验结果。任一 FAIL 都让脚本非零退出（避免「日志里红一下就过了」）。"""
    say("  %s %s%s" % ("OK  " if ok else "FAIL", name, (" | " + extra) if extra else ""))
    if not ok:
        _failures.append(name)
    return ok


def rel(path: Path) -> str:
    """相对仓库根显示，日志更短且可复制。"""
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            blk = f.read(chunk)
            if not blk:
                break
            h.update(blk)
    return h.hexdigest()


def dir_size_mb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total / 1048576.0


def project_version() -> str:
    """从 core_engine/__init__.py 读 __version__（单真源，避免脚本里再写一份）。"""
    init = ROOT / "core_engine" / "__init__.py"
    try:
        for line in init.read_text(encoding="utf-8").splitlines():
            if line.startswith("__version__"):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return "0.0.0"


# ---------------------------------------------------------------- 预检

def preflight(flavor: dict) -> bool:
    """打包前体检：档位与解释器是否匹配、源物料是否齐备。"""
    say("[1/6] 预检")
    ok = True
    spec = ROOT / flavor["spec"]
    ok &= check("spec 存在", spec.is_file(), rel(spec))

    models_dir = ROOT / "resources" / "models_v6"
    onnx = sorted(models_dir.glob("*.onnx")) if models_dir.is_dir() else []
    ok &= check("v6 模型齐备（6 个 onnx）", len(onnx) == 6,
                "实际 %d 个：%s" % (len(onnx), ", ".join(p.name for p in onnx)) if onnx
                else "%s 缺失或为空" % rel(models_dir))
    tmpl = ROOT / "resources" / "config.ini"
    ok &= check("出厂模板 resources/config.ini 存在", tmpl.is_file(), rel(tmpl))

    # 解释器能力：档位要求的 EP 必须真实可用，否则打出来的是「假加速包」
    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        ok &= check("当前解释器有 %s" % flavor["ep"], flavor["ep"] in providers,
                    "可用：%s（该档位请用 %s 运行）" % (providers, flavor["venv_hint"]))
        say("     解释器: %s" % sys.executable)
        say("     onnxruntime %s / device=%s" % (ort.__version__, ort.get_device()))
    except Exception as e:                                  # noqa: BLE001
        ok &= check("可 import onnxruntime", False, repr(e))

    # PyInstaller 可用
    try:
        import PyInstaller
        say("     PyInstaller %s" % PyInstaller.__version__)
    except Exception as e:                                  # noqa: BLE001
        ok &= check("可 import PyInstaller", False, repr(e))
    return bool(ok)


# ---------------------------------------------------------------- 清理 / 打包

def clean(flavor: dict, keep_work: bool) -> None:
    """删除上一份同名产物与工作目录 —— 这是「可重复执行」的前提。"""
    say("[2/6] 清理旧产物")
    out = DIST / flavor["out_name"]
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
        say("     已删除 %s" % rel(out))
    else:
        say("     无需删除（%s 不存在）" % rel(out))
    if TMP_DIST.exists():
        shutil.rmtree(TMP_DIST, ignore_errors=True)
    work = WORK_ROOT / flavor["out_name"]
    if keep_work:
        say("     --keep-work：保留 %s" % rel(work))
    elif work.exists():
        shutil.rmtree(work, ignore_errors=True)
        say("     已删除工作目录 %s" % rel(work))


def build(flavor: dict) -> bool:
    """执行 PyInstaller。产物先落在临时 distpath，稍后搬运。"""
    say("[3/6] PyInstaller 打包（onedir，可能数分钟）")
    TMP_DIST.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "PyInstaller", flavor["spec"],
        "--noconfirm",
        "--distpath", str(TMP_DIST),
        "--workpath", str(WORK_ROOT / flavor["out_name"]),
    ]
    say("     $ %s" % " ".join(cmd))
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(ROOT))
    say("     退出码 %d，耗时 %.1fs" % (proc.returncode, time.time() - t0))
    if proc.returncode != 0:
        _failures.append("PyInstaller 退出码 %d" % proc.returncode)
        return False
    return True


def relocate(flavor: dict) -> Path:
    """把 `<TMP_DIST>/<collect_name>` 搬到 `dist/<out_name>`。

    这一步是刻意的：spec 里的 COLLECT.name 是给 PyInstaller 看的，产物目录名不该
    由它决定（否则 `--distpath` 一变就嵌套）。搬运后 `dist/<档位>/ClickOCR*.exe`
    的层级与 README 记载一致。
    """
    say("[4/6] 归位产物")
    src = TMP_DIST / flavor["collect_name"]
    dst = DIST / flavor["out_name"]
    if not src.is_dir():
        _failures.append("未找到 PyInstaller 产出目录 %s" % rel(src))
        return dst
    DIST.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    shutil.rmtree(TMP_DIST, ignore_errors=True)
    say("     %s -> %s" % (rel(src), rel(dst)))
    return dst


# ---------------------------------------------------------------- 出包后校验

def verify(flavor: dict, out: Path) -> bool:
    """出包后硬校验：缺失加速库 / 档位不符 / 混入本机状态，都要挡在这里。"""
    say("[5/6] 校验产物")
    ok = True
    exe = out / (flavor["collect_name"] + ".exe")
    ok &= check("主程序存在", exe.is_file(), rel(exe) if exe.is_file() else "缺失")
    internal = out / INTERNAL
    ok &= check("_internal 存在（onedir 布局）", internal.is_dir())

    # ① 加速原生库：与**当前解释器**逐个比对 sha256 —— 打包漏收或收错版本都会露馅
    capi = internal / "onnxruntime" / "capi"
    for dll in flavor["ep_dlls"]:
        got = capi / dll
        if not check("capi 含 %s" % dll, got.is_file(),
                     "%.1f MB" % (got.stat().st_size / 1048576) if got.is_file() else "缺失"):
            ok = False
            continue
        try:
            import onnxruntime as ort
            src = Path(ort.__file__).parent / "capi" / dll
            if src.is_file():
                same = sha256_file(src) == sha256_file(got)
                ok &= check("  %s 与解释器一致" % dll, same,
                            "逐字节一致" if same else "内容不一致（可能漏收/版本错配）")
        except Exception as e:                              # noqa: BLE001
            say("     跳过 %s 比对：%r" % (dll, e))

    # ② 逐出项：CUDA/TRT（dml/cpu 不该有）、ffmpeg（无视频 I/O）
    if flavor["ep"] != "CUDAExecutionProvider":
        bad = [p.name for p in capi.glob("*.dll")
               if any(k in p.name.lower() for k in
                      ("cuda", "cublas", "cudnn", "cufft", "nvrtc", "nvinfer", "nvparsers"))]
        ok &= check("无 CUDA/TensorRT 残留", not bad, ", ".join(bad[:5]))
    ffmpeg = [p.name for p in internal.rglob("opencv_videoio_ffmpeg*.dll")]
    ok &= check("无 opencv ffmpeg 库（无视频 I/O）", not ffmpeg, ", ".join(ffmpeg[:3]))

    # ③ 数据库：vec0 扩展必须随包（否则 vec0 建表失败 → 语义检索不可用）
    vec = list(internal.rglob("vec0*.dll"))
    ok &= check("sqlite-vec 扩展 vec0*.dll 已随包", bool(vec),
                ", ".join(rel(p) for p in vec[:2]) or "缺失")

    # ④ 资源与出厂配置
    res = internal / "resources"
    for name in REQUIRED_RESOURCES:
        ok &= check("resources/%s 存在" % name, (res / name).exists())
    n_models = len(list((res / "models_v6").glob("*.onnx"))) if (res / "models_v6").is_dir() else 0
    ok &= check("产物内模型 6 个", n_models == 6, "实际 %d 个" % n_models)

    seed = res / "config.ini"
    accel = None
    if seed.is_file():
        for line in seed.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("ocr_device") and "=" in s:
                accel = s.split("=", 1)[1].split("#")[0].strip()
                break
    want = flavor["accel"]
    if want is None:
        want = read_template_ocr_device()
        say("     该档位 spec 不覆写 ocr_device，期望沿用模板值：%s" % want)
    ok &= check("产物内 ocr_device == %s" % want, accel == want, "实际 %s" % accel)

    # ⑤ 发布包里不该有本机状态
    leaked = [n for n in FORBIDDEN_IN_RELEASE if (out / n).exists()]
    ok &= check("未混入本机运行数据", not leaked, ", ".join(leaked) or "干净")

    say("     产物体积 %.0f MB，%s" % (dir_size_mb(out), rel(out)))
    if exe.is_file():
        say("     主程序 %.1f MB" % (exe.stat().st_size / 1048576))
    return bool(ok)


def read_template_ocr_device() -> str:
    tmpl = ROOT / "resources" / "config.ini"
    try:
        for line in tmpl.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("ocr_device") and "=" in s:
                return s.split("=", 1)[1].split("#")[0].strip()
    except OSError:
        pass
    return "?"


# ---------------------------------------------------------------- 归档 / 冒烟

def make_zip(flavor: dict, out: Path) -> Path | None:
    ver = project_version()
    stamp = time.strftime("%Y%m%d")
    base = DIST / ("%s-%s-%s" % (flavor["out_name"], ver, stamp))
    say("     生成分发包 %s.zip" % base.name)
    try:
        archive = shutil.make_archive(str(base), "zip", root_dir=str(out.parent),
                                      base_dir=out.name)
    except OSError as e:
        _failures.append("打包 zip 失败：%s" % e)
        return None
    path = Path(archive)
    say("     %.0f MB -> %s" % (path.stat().st_size / 1048576, rel(path)))
    say("     解压后直接运行 %s/ClickOCR.exe（绿色免安装；数据写在 exe 旁）"
        % flavor["out_name"])
    return path


def run_smoke(flavor: dict, out: Path) -> bool:
    say("[6/6] 启动冒烟（scripts/_smoke_packaged.py）")
    smoke = ROOT / "scripts" / "_smoke_packaged.py"
    if not smoke.is_file():
        say("     跳过：未找到 %s" % rel(smoke))
        return True
    accel = flavor["accel"] or read_template_ocr_device()
    proc = subprocess.run([sys.executable, str(smoke), str(out), accel], cwd=str(ROOT))
    if proc.returncode != 0:
        _failures.append("冒烟失败（退出码 %d）" % proc.returncode)
        return False
    return True


# ---------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="ClickOCR 可重复打包入口（PyInstaller onedir）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("flavor", choices=sorted(FLAVORS), help="打包档位")
    ap.add_argument("--zip", action="store_true", help="额外产出免安装分发包 zip")
    ap.add_argument("--smoke", action="store_true", help="出包后跑启动冒烟")
    ap.add_argument("--no-clean", action="store_true", help="保留上一份产物（默认先删）")
    ap.add_argument("--keep-work", action="store_true", help="保留 build 工作目录")
    args = ap.parse_args(argv)

    flavor = FLAVORS[args.flavor]
    say("=" * 66)
    say("ClickOCR 打包 · 档位 %s · 版本 %s" % (args.flavor, project_version()))
    say("  仓库 %s" % ROOT)
    say("=" * 66)

    if not preflight(flavor):
        say("\n预检未通过，未开始打包。")
        return 1

    if not args.no_clean:
        clean(flavor, args.keep_work)
    else:
        say("[2/6] 清理旧产物  --no-clean：跳过")

    if not build(flavor):
        say("\n打包失败（详见上方 PyInstaller 输出）。")
        return 1

    out = relocate(flavor)
    verified = verify(flavor, out)

    if args.zip and verified:
        make_zip(flavor, out)
    elif args.zip:
        say("     跳过 --zip：出包校验未通过，不产出分发包。")

    if args.smoke and verified:
        run_smoke(flavor, out)

    say("=" * 66)
    if _failures:
        say("构建完成但存在 %d 项问题：" % len(_failures))
        for f in _failures:
            say("  - %s" % f)
        return 2
    say("构建成功：%s" % rel(out))
    say("运行方式：%s\\ClickOCR.exe" % rel(out).replace("/", "\\"))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:                                    # 控制台可能是 GBK，中文日志不该炸
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                       # noqa: BLE001
            pass
    raise SystemExit(main())
