# -*- coding: utf-8 -*-
"""scripts/_smoke_packaged.py — PyInstaller 产物冒烟（启动 + 初始化 + 加速档位）。

为什么值得有一个固定脚本：打包后的 exe 是 console=False（无控制台），
出问题时看不到任何报错，只能靠"日志 + stderr 重定向 + 命名管道状态"三路取信。
本脚本把踩过的四个坑全部固化成判定项——只看"进程还活着"会假阳性：

① 进程存活但 App.__init__ 已抛异常（Qt 事件循环吊着进程）→ 必须看日志有没有真的走到
   "accel=" 与"数据库就绪"。
② 崩溃信息只从 stderr 看得见（console=False）→ 必须抓 stderr 找 Traceback。
③ 被单实例机制顶掉时会 rc=0 静静退出，且**提示只走 stdout**：
   "[ClickOCR] 已在运行中，已聚焦到已有窗口…"。占用者常常不是 ClickOCR.exe 而是
   上一步的 python 测试进程（QLocalServer 建了没释放），按镜像名查残留会漏掉，
   所以这里直接查命名管道是否空闲。
④ 打包默认档位可能与产物能力不匹配（如 DML 包内默认 ocr_device=cuda）→ 断言 accel。

用法：
    python scripts/_smoke_packaged.py [产物目录] [期望的 accel: dml|cuda|cpu]

    # CUDA 版
    python scripts/_smoke_packaged.py dist/ClickOCR cuda
    # DML 通用版
    python scripts/_smoke_packaged.py dist/ClickOCR_dml dml

注意：DML 首次加载模型约 15s+，等待时间不要调短，否则会把正常加载误判成崩溃；
      0xC0000005 = 访问冲突，rc=0/1 多为正常退出或单实例退出。
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
APP = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else _ROOT / "dist" / "ClickOCR_dml"
EXE = APP / "ClickOCR.exe"
LOG = APP / "logs" / "clickocr.log"
# 存到系统临时目录，别落在 scripts/ 里污染仓库（这两个文件只用于本次判定后即弃）
_OUT_DIR = Path(tempfile.gettempdir())
OUT = _OUT_DIR / "clickocr_smoke_stdout.txt"
ERR = _OUT_DIR / "clickocr_smoke_stderr.txt"
WANT_ACCEL = sys.argv[2] if len(sys.argv) > 2 else "dml"
SINGLE_NAME = "ClickOCR-SingleInstance"
WAIT = 35

fails = []


def check(name, cond, extra=""):
    print(("  OK  " if cond else "  FAIL") + " " + name
          + ((" | " + extra) if extra else ""))
    if not cond:
        fails.append(name)


def clickocr_procs():
    out = subprocess.run(["tasklist"], capture_output=True, text=True,
                         encoding="gbk", errors="replace").stdout or ""
    return [ln.strip() for ln in out.splitlines() if ln.upper().startswith("CLICKOCR")]


def pipe_busy():
    """命名管道 ClickOCR-SingleInstance 是否被占用（不依赖 PySide6）。"""
    try:
        return SINGLE_NAME.lower() in (n.lower() for n in os.listdir("\\\\.\\pipe\\"))
    except OSError:
        return False


print("[0] 环境体检")
print("     产物:", APP)
print("     exe :", "存在" if EXE.is_file() else "缺失！")
pre = clickocr_procs()
print("     既有 ClickOCR 进程:", pre or "无")
check("产物存在", EXE.is_file())
check("启动前无残留 ClickOCR 实例", not pre, "%d 个" % len(pre))

busy = pipe_busy()
check("命名管道 %s 空闲" % SINGLE_NAME, not busy,
      "被占用→新 exe 会被当第二实例直接 exit(0)" if busy else "无人占用")
if busy:
    print("\n中止：先关掉占用该管道的实例（含遗留的 python 测试进程）再冒烟。")
    sys.exit(1)
if not EXE.is_file():
    sys.exit(1)

if LOG.exists():
    LOG.unlink()                 # 清掉上次日志，确保读到的是本次的
OUT.parent.mkdir(parents=True, exist_ok=True)

with open(OUT, "wb") as fo, open(ERR, "wb") as fe:
    t0 = time.time()
    proc = subprocess.Popen([str(EXE)], cwd=str(APP), stdout=fo, stderr=fe)
    print("[1] PID=%d 启动，等待 %ds..." % (proc.pid, WAIT))
    time.sleep(WAIT)
    alive = proc.poll() is None

print("[2] 判定")
check("进程存活 ≥%ds" % WAIT, alive,
      "存活 %.1fs" % (time.time() - t0) if alive else "rc=%s" % proc.poll())

out_txt = OUT.read_text(encoding="utf-8", errors="replace") if OUT.exists() else ""
err_txt = ERR.read_text(encoding="utf-8", errors="replace") if ERR.exists() else ""
tb = [ln for ln in err_txt.splitlines() if "Traceback" in ln or "Error" in ln]
check("stderr 无 Traceback/Error", not tb, "; ".join(tb[:3]))
if err_txt.strip():
    print("     stderr 全文:")
    for ln in err_txt.strip().splitlines()[:12]:
        print("       |", ln[:150])
check("stdout 无单实例提示", "已在运行" not in out_txt,
      out_txt.strip()[:120] if "已在运行" in out_txt else "")

log_txt = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
lines = log_txt.rstrip().splitlines()
check("日志非空（证明走完初始化）", bool(lines), "%d 行" % len(lines))
check("日志含引擎初始化行", any("accel=" in l for l in lines))
check("日志含数据库就绪行", any("数据库就绪" in l for l in lines))
check("日志含后台加载完成行", any("后台加载完成" in l for l in lines))

accel = next((l.split("accel=")[-1].split()[0] for l in lines if "accel=" in l), "?")
dev = next((l.split("device=")[-1].strip().rstrip("）)").strip()
            for l in lines if "后台加载完成" in l), "?")
print("     引擎: accel=%s / 实际 device=%s（期望 accel=%s）" % (accel, dev, WANT_ACCEL))
check("引擎 accel == %s" % WANT_ACCEL, accel == WANT_ACCEL, "实际 %s" % accel)

print("     日志末 6 行:")
for ln in lines[-6:]:
    print("       |", ln.strip()[:150])

if alive:
    # 与上面 tasklist 同理：中文 Windows 的命令行输出是 GBK，按默认(UTF-8)解码会
    # 抛 UnicodeDecodeError —— 它在 reader 线程里，表现为刷一段 traceback 却仍判
    # PASS，容易把真故障淹掉。
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                   capture_output=True, text=True, encoding="gbk", errors="replace")
    time.sleep(2)
print()
if fails:
    print("SMOKE FAILED: %d -> %s" % (len(fails), fails))
    sys.exit(1)
print("SMOKE OK: 全部通过")
