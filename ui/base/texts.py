"""UI 文字表：集中管理界面文字（resources/ui_text.json）。

改界面文字只需编辑 ui_text.json，无需动代码。T(key, default) 在 JSON
缺失 key 时回退默认值（原中文），保证安全。
"""
from __future__ import annotations

import json
from pathlib import Path

_TEXTS: dict = {}
_LOADED = False


def load_ui_text(path=None) -> dict:
    """加载文字表；path 缺省时用 resources/ui_text.json。失败则空表（回退默认值）。"""
    global _TEXTS, _LOADED
    # 本文件位于 ui/base/，向上三级 = 项目根
    p = Path(path) if path else (
        Path(__file__).resolve().parent.parent.parent / "resources" / "ui_text.json")
    try:
        _TEXTS = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                    # noqa: BLE001 文件缺失/损坏 → 全部回退默认
        _TEXTS = {}
    _LOADED = True
    return _TEXTS


def T(key: str, default: str = "") -> str:
    """取界面文字；JSON 缺失时返回 default（未给则返回 key 本身）。"""
    if not _LOADED:
        load_ui_text()
    return _TEXTS.get(key, default if default else key)
