"""字体族决策（FontResolver）—— 平台适配层的字体部分。

只做一件事：告诉上层「当前平台该用哪个字体族」。**不 import Qt、不持有状态**，
返回值直接喂给 `QFont(...)`，因此 `app.py` / `ui/` / `screenshot/` 都能无顾虑引用。

为什么需要这一层：原先 `"Microsoft YaHei"` 硬编码在 8 处
（`app.py`、`ui/components/annotation.py` ×3、`ui/components/toolbar.py`、
`ui/windows/result_overlay.py`、`screenshot/scroll_capture.py`），
在 Linux / macOS 上要么落到没有中文字形的字体、要么各控件表现不一致。

平台取值：

  - **Windows**：`Microsoft YaHei` —— 与历史行为**逐字一致**，改这里不会改变
    Windows 观感（若要改 Windows 默认字体，只动这一处即可全局生效）。
  - **macOS**：`PingFang SC`（系统中文首选）。
  - **Linux**：从 `Noto Sans CJK SC` / `Source Han Sans SC` / `WenQuanYi Micro Hei`
    / `Sarasa Gothic SC` 里挑一个**实际装了**的（用 `fc-list` 探测一次并缓存）；
    探测不到（无 fontconfig、无任何候选）就用静态首选 —— fontconfig 会自行做
    字形替换，中文仍能渲染，只是字形未必最理想。
"""
from __future__ import annotations

import subprocess

from . import IS_LINUX, IS_MACOS, IS_WINDOWS

#: 各平台候选字体链（按优先级排列；末位是系统级兜底）
CANDIDATES: dict[str, tuple[str, ...]] = {
    "windows": ("Microsoft YaHei", "Microsoft YaHei UI", "SimHei", "Segoe UI"),
    "macos": ("PingFang SC", "Hiragino Sans GB", "Heiti SC", "Helvetica Neue"),
    "linux": ("Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei",
              "Sarasa Gothic SC", "DejaVu Sans"),
}

_CACHE: dict[str, str] = {}


def pick_installed(candidates: tuple[str, ...],
                   installed: set[str]) -> str | None:
    """从候选链里挑第一个已安装的族名。

    纯函数（便于单测）：`installed` 传字体系统里实际存在的族名集合。
    比较时忽略大小写与空格差异 —— `fc-list` 报 `Noto Sans CJK SC`，
    而配置里可能写成 `noto sans cjk sc`。
    """
    norm = {" ".join(n.split()).lower() for n in installed}
    for cand in candidates:
        if " ".join(cand.split()).lower() in norm:
            return cand
    return None


def installed_families() -> set[str]:
    """当前系统已安装的字体族（`fc-list`；取不到就返回空集）。

    只在 Linux 上调用；结果缓存，避免反复起子进程。
    """
    if "installed" in _CACHE:
        return set(_CACHE["installed"].split("\n")) if _CACHE["installed"] else set()
    fams: set[str] = set()
    try:
        out = subprocess.run(
            ["fc-list", "--format=%{family[0]}\n"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            fams = {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}
    except (OSError, subprocess.SubprocessError):
        fams = set()
    _CACHE["installed"] = "\n".join(sorted(fams))
    return fams


def ui_font_candidates() -> tuple[str, ...]:
    """当前平台的候选字体链（诊断/设置面板展示用）。"""
    if IS_WINDOWS:
        return CANDIDATES["windows"]
    if IS_MACOS:
        return CANDIDATES["macos"]
    return CANDIDATES["linux"]


def ui_font_family() -> str:
    """UI 默认字体族（中文字形优先），直接用于 `QFont(...)`。"""
    if "family" in _CACHE:
        return _CACHE["family"]

    if IS_WINDOWS:
        family = CANDIDATES["windows"][0]          # 与历史行为一致
    elif IS_MACOS:
        family = CANDIDATES["macos"][0]
    elif IS_LINUX:
        family = pick_installed(CANDIDATES["linux"], installed_families()) \
            or CANDIDATES["linux"][0]
    else:
        family = CANDIDATES["linux"][-1]

    _CACHE["family"] = family
    return family
