"""标注核心纯逻辑（无 Qt 依赖，可 headless 测试）。

从 ui/annotation.py 剥离：
- 撤销命令体系（命令 = 对标注数据 list[dict] 的 apply/unapply 增量变更，
  非快照式；单笔绘制（含全部轨迹点）= 一条 AddCmd，连续笔迹天然合并为一步撤回）；
- 标注数据序列化（anno_to_json / anno_from_json）。

职责边界：
- 本模块：命令语义 + 数据变更 + 序列化（纯逻辑）。
- annotation.py：AnnotationSession 持有数据/场景，调用命令并负责渲染重建，
  以及全部鼠标交互、图元工厂（QGraphicsItem）等视图职责。
"""
from __future__ import annotations

import copy
import json

ANNO_VERSION = 1
UNDO_LIMIT = 500
# 可反序列化的 item 类型：标注工具 + 马赛克·画笔（mosaic 的 pen 模式，非独立工具）
SERIALIZABLE_TYPES = ("rect", "arrow", "pen", "line", "mosaic", "mosaic_pen",
                      "text", "num")

# ---- 各工具的合法样式（**唯一来源**）----
# 每项 = (样式 id, 图标名, 中文标签)。原先这张表散落三处（本表 + 浮层展示 +
# 工具栏恢复白名单），且工具栏那份是缩水版（各 3 种），导致 ellipse/elbow/
# wavy/glow 等 16 种样式在重启后被静默回退默认值。收敛到此，其余各处只读。
TOOL_STYLES = {
    "rect": (("rect", "rect-outline", "空心矩形"),
             ("filled", "rect-filled", "实心矩形"),
             ("round", "rect-round", "圆角矩形"),
             ("ellipse", "rect-ellipse", "椭圆"),
             ("dialog", "rect-dialog", "对话框气泡"),
             ("highlight", "rect-highlight", "荧光高亮框"),
             ("dash", "rect-dash", "虚线框")),
    "arrow": (("single", "arrow-single", "单箭头"),
              ("double", "arrow-double", "双箭头"),
              ("dash", "arrow-dash", "虚线箭头"),
              ("elbow", "arrow-elbow", "直角箭头"),
              ("curve", "arrow-curve", "曲线箭头"),
              ("dot", "arrow-dot", "点线箭头"),
              ("circle", "arrow-circle", "圆点箭头")),
    "line": (("solid", "line-solid", "实线"),
             ("dash", "line-dash", "虚线"),
             ("dot", "line-dot", "点线"),
             ("dashdot", "line-dashdot", "点划线"),
             ("double", "line-double", "双线"),
             ("wavy", "line-wavy", "波浪线"),
             ("zigzag", "line-zigzag", "锯齿线")),
    "pen": (("solid", "pen-solid", "实线画笔"),
            ("highlight", "pen-highlight", "荧光笔"),
            ("dash", "pen-dash", "虚线画笔"),
            ("dot", "pen-dot", "点线笔刷"),
            ("double", "pen-double", "双线笔刷"),
            ("wavy", "pen-wavy", "波浪笔刷"),
            ("glow", "pen-glow", "发光笔刷")),
}

# 序号样式（id, 图标名, 中文标签）
NUM_STYLES = (("circle", "num-circle", "实心圆"),
              ("circle_o", "num-circle-o", "空心圆"),
              ("square", "num-square", "圆角方"),
              ("paren", "num-paren", "(1) 括号"),
              ("plain", "num-plain", "纯数字"))

# 马赛克模式
MOSAIC_MODES = ("pen", "rect", "textbox")


def tool_style_ids(tool: str) -> tuple:
    """某工具的全部合法样式 id（校验/回读用；未知工具返回空元组）。"""
    return tuple(row[0] for row in TOOL_STYLES.get(tool, ()))


def num_style_ids() -> tuple:
    """序号的全部合法样式 id。"""
    return tuple(row[0] for row in NUM_STYLES)


class _Cmd:
    """撤销命令基类：apply=正向执行（redo），unapply=反向撤销（undo）。"""

    def apply(self, data: list):
        raise NotImplementedError

    def unapply(self, data: list):
        raise NotImplementedError


class _AddCmd(_Cmd):
    """新增一个标注块（画矩形/箭头/画笔/马赛克/文字）。"""

    def __init__(self, index: int, item: dict):
        self.index = index
        self.item = item

    def apply(self, data):
        data.insert(self.index, copy.deepcopy(self.item))

    def unapply(self, data):
        data.pop(self.index)


class _DeleteCmd(_Cmd):
    """删除一个标注块（选择工具 Delete）。"""

    def __init__(self, index: int, item: dict):
        self.index = index
        self.item = item

    def apply(self, data):
        data.pop(self.index)

    def unapply(self, data):
        data.insert(self.index, copy.deepcopy(self.item))


class _MoveCmd(_Cmd):
    """移动一个标注块（选择工具拖动）。"""

    def __init__(self, index: int, dx: float, dy: float):
        self.index = index
        self.dx = dx
        self.dy = dy

    def apply(self, data):
        self._shift(data, self.dx, self.dy)

    def unapply(self, data):
        self._shift(data, -self.dx, -self.dy)

    def _shift(self, data, dx: float, dy: float):
        d = data[self.index]
        t = d["type"]
        if t in ("rect", "arrow", "line", "mosaic"):
            d["x0"] += dx
            d["x1"] += dx
            d["y0"] += dy
            d["y1"] += dy
        elif t in ("pen", "mosaic_pen"):
            d["points"] = [[x + dx, y + dy] for x, y in d["points"]]
        elif t in ("text", "num"):
            d["x"] += dx
            d["y"] += dy


class _EditCmd(_Cmd):
    """替换一个标注块（双击编辑文字原地替换）。"""

    def __init__(self, index: int, old: dict, new: dict):
        self.index = index
        self.old = old
        self.new = new

    def apply(self, data):
        data[self.index] = copy.deepcopy(self.new)

    def unapply(self, data):
        data[self.index] = copy.deepcopy(self.old)


class _ClearCmd(_Cmd):
    """清空全部标注（恢复 = 还原整个旧列表）。"""

    def __init__(self, old_data: list):
        self.old_data = old_data

    def apply(self, data):
        data.clear()

    def unapply(self, data):
        data[:] = copy.deepcopy(self.old_data)


class _RenumberCmd(_Cmd):
    """重排所有序号标注的编号（起始序号变更时批量更新）。"""

    def __init__(self, changes: list):
        self.changes = changes     # [(index, old_num, new_num), ...]

    def apply(self, data):
        for idx, _old, new in self.changes:
            data[idx]["num"] = new

    def unapply(self, data):
        for idx, old, _new in self.changes:
            data[idx]["num"] = old


# ---- 序列化 ----
def anno_to_json(data: list) -> str:
    return json.dumps({"version": ANNO_VERSION, "items": data},
                      ensure_ascii=False)


def anno_from_json(text: str | None) -> list:
    if not text:
        return []
    try:
        d = json.loads(text)
        items = d.get("items", []) if isinstance(d, dict) else []
        return [it for it in items if isinstance(it, dict)
                and it.get("type") in SERIALIZABLE_TYPES]
    except (ValueError, AttributeError):
        return []
