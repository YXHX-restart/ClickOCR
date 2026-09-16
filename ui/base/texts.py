"""UI 文字表：集中管理界面文字（resources/ui_text.json）。

改界面文字只需编辑 ui_text.json，无需动代码。T(key, default) 在 JSON
缺失 key 时回退默认值（原中文），保证安全。
"""
from __future__ import annotations

import json
from pathlib import Path

from core_engine.logging_setup import get_logger

log = get_logger("texts")

_TEXTS: dict = {}
_LOADED = False
_LOAD_OK = False          # 文字表是否成功加载（加载失败属整体回退，不逐 key 告警）
_WARNED: set = set()      # 已告警过的缺失 key（避免渲染循环里刷屏）


def load_ui_text(path=None) -> dict:
    """加载文字表；path 缺省时用 resources/ui_text.json。失败则空表（回退默认值）。"""
    global _TEXTS, _LOADED, _LOAD_OK
    # 本文件位于 ui/base/，向上三级 = 项目根
    p = Path(path) if path else (
        Path(__file__).resolve().parent.parent.parent / "resources" / "ui_text.json")
    try:
        _TEXTS = json.loads(p.read_text(encoding="utf-8"))
        _LOAD_OK = True
    except Exception as e:               # noqa: BLE001 文件缺失/损坏 → 全部回退默认
        _TEXTS = {}
        _LOAD_OK = False
        log.warning("界面文字表加载失败（全部回退代码默认值）：%s", e)
    _LOADED = True
    _WARNED.clear()
    return _TEXTS


def T(key: str, default: str = "") -> str:
    """取界面文字；JSON 缺失时返回 default（未给则返回 key 本身）。

    缺 key **记一次 warning**：T(key, default) 的设计是「JSON 缺失也能跑」，于是
    「key 写错了 / 新文案忘了同步 JSON」和「本来就这样」长得完全一样 —— 不留痕就
    只能靠肉眼比对。同一 key 只报一次，避免在渲染循环里刷屏；文字表整体加载失败
    时不报（那是全局回退，不是单个 key 的问题，load_ui_text 已单独告警）。
    """
    if not _LOADED:
        load_ui_text()
    if key in _TEXTS:
        return _TEXTS[key]
    if _LOAD_OK and key not in _WARNED:
        _WARNED.add(key)
        log.warning("界面文字缺少 key %r（已回退代码默认值 %r）", key, default or key)
    return default if default else key
