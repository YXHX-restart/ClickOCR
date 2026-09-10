"""ui.components.annotation — M2-7 基础标注工具（矩形/箭头/画笔/马赛克/文字）。

- 标注能力由两部分组成：
  - 标注数据/图层/撤销重做：`AnnotationSession`（items + 场景图层 + Command 栈，
    由 ResultOverlay 在标注模式下转发鼠标事件驱动）。
  - 工具栏交互组件（供统一工具栏 StatusStrip 复用）：
    `_WidthButton`/`_WidthSliderPopup`（线宽）、`_FontPopup`（文字属性）、
    `_MosaicModePopup`（马赛克模式）、`TOOLS`/`TOOL_LABELS`/`TOOL_TIPS`。
- 独立顶层标注工具栏已移除（并入 StatusStrip 统一工具栏，标注态展开）。
- 数据规范（§8.2 简化）：{"version": 1, "items": [...]}，item 类型：
  rect/arrow/pen/mosaic/text/num（num=序号标注，编号随起始序号 +1 递增）。
- 马赛克：从原图 QPixmap 裁区域 → 缩小再放大（块状化）→ 作为图层项。
"""
from __future__ import annotations

import copy
import math

from PySide6.QtCore import Qt, QPointF, QRectF, Signal
from PySide6.QtGui import (QColor, QPen, QBrush, QFont, QPainterPath,
                           QPolygonF, QPixmap, QImage, QPainter)
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
                               QLabel, QFrame, QSlider, QSpinBox,
                               QButtonGroup, QFontComboBox, QComboBox,
                               QGraphicsRectItem, QGraphicsLineItem,
                               QGraphicsPathItem, QGraphicsPolygonItem,
                               QGraphicsPixmapItem, QGraphicsEllipseItem,
                               QGraphicsTextItem, QGraphicsItemGroup,
                               QGraphicsScene, QStyleOptionGraphicsItem)

# 标注核心纯逻辑（命令撤销体系 + 序列化，无 Qt 依赖，见 ui/anno_core.py）
from ui.core.anno_core import (ANNO_VERSION, UNDO_LIMIT, SERIALIZABLE_TYPES,
                          _Cmd, _AddCmd, _DeleteCmd, _MoveCmd, _EditCmd,
                          _ClearCmd, _RenumberCmd,
                          anno_to_json, anno_from_json)


# ---- 命令模式撤销/重做（Command 栈）：命令类见 ui/anno_core.py ----
# 快照式 undo（deepcopy 整个 data 列表）改为「命令 + 数据变更」，省内存：
# 每条命令只存被操作的 item/坐标，undo/redo 走 apply/unapply；
# 单笔绘制（含全部轨迹点）= 一条 AddCmd，连续笔迹天然合并为一步撤回。


# ---- 图元类 + 工厂（每图元独立类，新增图元注册即可扩展）----
class _BaseElement:
    """图元基类：create(d, base) -> item / (items) / None。

    d=标注数据块；base=原图 QPixmap（马赛克图元取像素用）。
    返回单个 QGraphicsItem、元组（如箭头 line+head）或 None（不可渲染）。
    """

    @staticmethod
    def create(d: dict, base):
        raise NotImplementedError


# ---- 多段路径辅助（画笔双线/波浪、线段波浪/锯齿共用） ----

def _norm_offsets(pts):
    """逐点单位法线 (nx, ny)：首尾用相邻段方向，中间用前后点连线方向。"""
    n = len(pts)
    out = []
    for i in range(n):
        if i == 0:
            dx = pts[1][0] - pts[0][0]
            dy = pts[1][1] - pts[0][1]
        elif i == n - 1:
            dx = pts[i][0] - pts[i - 1][0]
            dy = pts[i][1] - pts[i - 1][1]
        else:
            dx = pts[i + 1][0] - pts[i - 1][0]
            dy = pts[i + 1][1] - pts[i - 1][1]
        L = math.hypot(dx, dy)
        if L < 1e-6:
            nx, ny = 0.0, 1.0
        else:
            nx, ny = -dy / L, dx / L
        out.append((nx, ny))
    return out


def _text_item_ink_rect(ti: QGraphicsTextItem) -> QRectF:
    """光栅化文字图元实测墨迹框（ti 本地坐标）。

    序号徽章用：Microsoft YaHei 的 QFontMetricsF.tightBoundingRect 对
    数字谎报墨迹（18px 字号返回 18×18 的 em 方块，实际墨迹仅约
    10×13，且数字为全角设计），按它算缩放/居中都会偏；把图元直接
    画进 QImage 扫 alpha 包络，任何字体都准。徽章逐个创建，开销可忽略。
    """
    br = ti.boundingRect()
    w = max(4, int(math.ceil(br.width())) + 2)
    h = max(4, int(math.ceil(br.height())) + 2)
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.translate(-br.x() + 1.0, -br.y() + 1.0)
    ti.paint(p, QStyleOptionGraphicsItem(), None)
    p.end()
    x0 = y0 = x1 = y1 = None
    for y in range(h):
        for x in range(w):
            if img.pixelColor(x, y).alpha() > 10:
                if x0 is None or x < x0:
                    x0 = x
                if x1 is None or x > x1:
                    x1 = x
                if y0 is None or y < y0:
                    y0 = y
                if y1 is None or y > y1:
                    y1 = y
    if x0 is None:
        return QRectF()
    return QRectF(br.x() - 1.0 + x0, br.y() - 1.0 + y0,
                  x1 - x0 + 1, y1 - y0 + 1)


def _dedupe_points(pts, eps=0.5):
    """去掉相邻近重复点（鼠标密集采样会产生，偏移前必须清理）。"""
    out = []
    for p in pts:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > eps:
            out.append((p[0], p[1]))
    return out


def _offset_polyline(pts, off):
    """折线单侧法线偏移，返回偏移点列（双线笔刷用）。

    拐角顶点取相邻两段偏移线的交点（miter）：转角内侧交点天然截断
    重叠（旧逐点平均法线在这里会交叉），外侧自然延伸出尖角；交点距
    顶点超过 3*off（急弯长刺）时退化为归一化平均法线偏移（bevel）。
    首尾点按所在段法线偏移。
    """
    n = len(pts)
    if n < 2:
        return list(pts)
    dirs = []
    for i in range(n - 1):
        dx, dy = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
        L = math.hypot(dx, dy)
        dirs.append((dx / L, dy / L) if L > 1e-9 else None)
    for i in range(n - 1):                     # 零长段借相邻段方向
        if dirs[i] is None:
            dirs[i] = (dirs[i - 1] if i > 0 and dirs[i - 1] else
                       dirs[i + 1] if i + 1 < n - 1 and dirs[i + 1] else
                       (1.0, 0.0))

    def _edge(i, d):
        return (pts[i][0] - d[1] * off, pts[i][1] + d[0] * off)

    out = [_edge(0, dirs[0])]
    for i in range(1, n - 1):
        d1, d2 = dirs[i - 1], dirs[i]
        if abs(d1[0] * d2[1] - d1[1] * d2[0]) < 1e-9:   # 共线段
            out.append(_edge(i, d1))
            continue
        ax, ay = _edge(i, d1)                  # 段1偏移线过此点
        bx, by = _edge(i, d2)                  # 段2偏移线过此点
        ex, ey = bx - ax, by - ay
        det = d2[0] * d1[1] - d1[0] * d2[1]
        t = (-ex * d2[1] + d2[0] * ey) / det
        mx, my = ax + t * d1[0], ay + t * d1[1]
        if math.hypot(mx - pts[i][0], my - pts[i][1]) > 3.0 * off:
            adx, ady = d1[0] + d2[0], d1[1] + d2[1]      # bevel 兜底
            al = math.hypot(adx, ady)
            adx, ady = (adx / al, ady / al) if al > 1e-9 else d1
            out.append(_edge(i, (adx, ady)))
        else:
            out.append((mx, my))
    out.append(_edge(n - 1, dirs[n - 2]))
    return out


def _resample_points(pts, step=5.0):
    """把折线按弧长等距重采样（首尾保留）。

    波浪笔刷用：鼠标采样点疏密不均（手速快慢），直接按点序做正弦相位
    会让波形忽大忽小；先等距重采样再按索引取相位，波形均匀稳定。
    """
    if len(pts) < 2:
        return [tuple(p) for p in pts]
    acc = [0.0]
    for i in range(1, len(pts)):
        acc.append(acc[-1] + math.hypot(pts[i][0] - pts[i - 1][0],
                                        pts[i][1] - pts[i - 1][1]))
    total = acc[-1]
    if total < step:
        return [tuple(p) for p in pts]
    n = max(2, int(total / step))
    out = []
    j = 0
    for k in range(n + 1):
        d = total * k / n
        while j < len(acc) - 2 and acc[j + 1] < d:
            j += 1
        seg = acc[j + 1] - acc[j]
        t = 0.0 if seg < 1e-9 else (d - acc[j]) / seg
        out.append((pts[j][0] + (pts[j + 1][0] - pts[j][0]) * t,
                    pts[j][1] + (pts[j + 1][1] - pts[j][1]) * t))
    return out


def _polyline_item(pts, pen):
    """把点列连成折线 QGraphicsPathItem。"""
    path = QPainterPath()
    path.moveTo(pts[0][0], pts[0][1])
    for px, py in pts[1:]:
        path.lineTo(px, py)
    it = QGraphicsPathItem(path)
    it.setPen(pen)
    it.setBrush(Qt.NoBrush)
    return it


def _line_sine_item(x0, y0, x1, y1, pen, w):
    """沿直线画正弦波浪线（波浪线段）。

    波数按长度自适应（约 26px 一个完整波），首尾都落在基线上；
    振幅随线宽走但不超波距的 1/4，避免相邻波自交。
    """
    dx, dy = x1 - x0, y1 - y0
    L = math.hypot(dx, dy)
    if L < 1e-6:
        it = QGraphicsLineItem(x0, y0, x1, y1)
        it.setPen(pen)
        return it
    waves = max(1, round(L / 26.0))
    segs = waves * 12                       # 每波 12 段，放大后仍平滑
    amp = max(2.5, min(w * 1.2, L / (waves * 4)))
    ux, uy = dx / L, dy / L
    nx, ny = -uy, ux
    path = QPainterPath(QPointF(x0, y0))
    for i in range(1, segs + 1):
        t = i / segs
        s = math.sin(t * math.tau * waves) * amp
        path.lineTo(x0 + ux * L * t + nx * s, y0 + uy * L * t + ny * s)
    it = QGraphicsPathItem(path)
    it.setPen(pen)
    it.setBrush(Qt.NoBrush)
    return it


def _line_zigzag_item(x0, y0, x1, y1, pen, w):
    """沿直线画锯齿折线（锯齿线段）。

    尖峰（V 形）数按长度自适应（约 24px 一个 V），首尾都落在基线上
    （用户拖的起止点上）；峰谷交替 +amp/-amp，最后一笔回到基线终点。
    """
    dx, dy = x1 - x0, y1 - y0
    L = math.hypot(dx, dy)
    if L < 1e-6:
        it = QGraphicsLineItem(x0, y0, x1, y1)
        it.setPen(pen)
        return it
    peaks = max(2, round(L / 24.0))
    amp = max(2.5, min(w * 1.3, L / (peaks * 4)))
    ux, uy = dx / L, dy / L
    nx, ny = -uy, ux
    n = peaks * 2
    path = QPainterPath(QPointF(x0, y0))
    for i in range(1, n):
        t = i / n
        side = 1.0 if (i % 2) else -1.0
        path.lineTo(x0 + ux * L * t + nx * amp * side,
                    y0 + uy * L * t + ny * amp * side)
    path.lineTo(x1, y1)                     # 终点回基线（用户拖的点上）
    it = QGraphicsPathItem(path)
    it.setPen(pen)
    it.setBrush(Qt.NoBrush)
    return it


def _bubble_path(rect: QRectF, w: float) -> QPainterPath:
    """对话框气泡轮廓（单子路径）：圆角矩形 + 底边左下 V 形尾巴。

    圆角 / 尾巴尺寸随框大小自适应；尾巴与主体是同一条连续轮廓
    （旧的 addRoundedRect 会 closeSubpath，尾巴 moveTo 另起子路径，
    渲染成「矩形 + 悬空三角」两块不相连）。
    """
    fw, fh = rect.width(), rect.height()
    r = max(3.0, min(10.0, fw / 6.0, fh / 4.0))    # 自适应圆角
    tw = max(7.0, min(w * 3.0, fh * 0.45))         # 尾巴下伸长度
    bw = max(10.0, min(20.0, fw * 0.24))           # 尾巴根宽
    tx = rect.left() + max(r + 2.0, fw * 0.16)     # 尾巴左根（左下角附近）
    left, top = rect.left(), rect.top()
    right, bottom = rect.right(), rect.bottom()

    path = QPainterPath(QPointF(tx, bottom))
    path.lineTo(tx + bw * 0.30, bottom + tw)       # 尾巴尖（略向左下）
    path.lineTo(tx + bw, bottom)                   # 尾巴右根
    path.lineTo(right - r, bottom)                 # 底边
    path.quadTo(right, bottom, right, bottom - r)  # 右下圆角
    path.lineTo(right, top + r)                    # 右边
    path.quadTo(right, top, right - r, top)        # 右上圆角
    path.lineTo(left + r, top)                     # 顶边
    path.quadTo(left, top, left, top + r)          # 左上圆角
    path.lineTo(left, bottom - r)                  # 左边
    path.quadTo(left, bottom, left + r, bottom)    # 左下圆角
    path.closeSubpath()
    return path


class _RectElement(_BaseElement):
    """矩形图元：style = rect(空心) / filled(半透明实心) / round(圆角) /
    ellipse(椭圆) / dialog(对话框气泡) / highlight(荧光高亮框) / dash(虚线框)。"""

    @staticmethod
    def create(d, base):
        rect = QRectF(d["x0"], d["y0"],
                      d["x1"] - d["x0"], d["y1"] - d["y0"])
        style = d.get("style", "rect")
        color = QColor(d.get("color", "#ff5f5f"))
        w = d.get("width", 2)
        if style == "filled":
            it = QGraphicsRectItem(rect)
            it.setPen(QPen(color, w))
            it.setBrush(QBrush(QColor(color.red(), color.green(),
                                      color.blue(), 70)))
            return it
        if style == "round":
            path = QPainterPath()
            path.addRoundedRect(rect, 6, 6)
            it = QGraphicsPathItem(path)
            it.setPen(QPen(color, w))
            it.setBrush(Qt.NoBrush)
            return it
        if style == "ellipse":
            it = QGraphicsEllipseItem(rect)
            it.setPen(QPen(color, w))
            it.setBrush(Qt.NoBrush)
            return it
        if style == "dialog":
            # 对话框气泡：圆角矩形 + 底边左下 V 形尾巴（单一连续轮廓）
            it = QGraphicsPathItem(_bubble_path(rect, w))
            it.setPen(QPen(color, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            it.setBrush(Qt.NoBrush)
            return it
        if style == "highlight":
            # 荧光高亮框：粗半透明边框
            it = QGraphicsRectItem(rect)
            it.setPen(QPen(QColor(color.red(), color.green(),
                                  color.blue(), 110), max(6, w * 2)))
            it.setBrush(Qt.NoBrush)
            return it
        if style == "dash":
            it = QGraphicsRectItem(rect)
            it.setPen(QPen(color, w, Qt.DashLine))
            it.setBrush(Qt.NoBrush)
            return it
        it = QGraphicsRectItem(rect)
        it.setPen(QPen(color, w))
        it.setBrush(Qt.NoBrush)
        return it


class _ArrowElement(_BaseElement):
    """箭头图元：style = single(单箭头) / double(双箭头) / dash(虚线箭头) /
    dot(点线箭头) / elbow(直角箭头) / curve(曲线箭头) / circle(圆点箭头)。

    线 + 三角头（两/三个独立 item，tuple 返回）。
    """

    @staticmethod
    def create(d, base):
        color = QColor(d.get("color", "#ff5f5f"))
        w = d.get("width", 2)
        style = d.get("style", "single")
        tail = QPointF(d["x0"], d["y0"])
        tip = QPointF(d["x1"], d["y1"])
        v = tip - tail
        length = math.hypot(v.x(), v.y())
        if length < 4:
            return None
        ux, uy = v.x() / length, v.y() / length   # 指向箭头的单位向量
        L = max(12, w * 2)                        # 三角臂长（明显大于线宽）
        wing = L * 0.55                           # 翅膀横向半宽
        pen = QPen(color, w, Qt.SolidLine, Qt.FlatCap, Qt.RoundJoin)
        if style == "dash":
            pen.setStyle(Qt.DashLine)
        elif style == "dot":
            pen.setStyle(Qt.DotLine)

        def head(pt: QPointF, dirx: float, diry: float):
            """箭头三角：尖端在 pt，底边中心在 pt + dir*L（dir 指向线来向）。

            翅膀沿「头方向自身的法线」展开（-diry, dirx），保证三角头
            永远正对行进方向 —— elbow/curve 的末段方向 ≠ 整体连线方向，
            不能复用连线法线，否则三角头是歪的。
            """
            bx, by = pt.x() + dirx * L, pt.y() + diry * L
            hx, hy = -diry, dirx
            w1 = QPointF(bx + hx * wing, by + hy * wing)
            w2 = QPointF(bx - hx * wing, by - hy * wing)
            h = QGraphicsPolygonItem(QPolygonF([pt, w1, w2]))
            h.setBrush(QBrush(color))
            h.setPen(Qt.NoPen)
            return h

        if style == "double":
            # 双箭头：线两端各让出一个箭头长度，两端各画三角头
            line = QGraphicsLineItem(tail.x() + ux * L, tail.y() + uy * L,
                                     tip.x() - ux * L, tip.y() - uy * L)
            line.setPen(pen)
            return (line, head(tail, ux, uy), head(tip, -ux, -uy))
        if style == "elbow":
            # 直角箭头：先水平后垂直的 L 形折线 + 单头（头朝最后一段方向，
            # 翅膀沿末段法线展开）。线止于三角底边前，避免穿过三角头。
            mid = QPointF(tip.x(), tail.y())
            hx, hy = tip.x() - mid.x(), tip.y() - mid.y()
            hl = math.hypot(hx, hy)
            if hl < 1e-6:
                hx, hy, hl = ux, uy, 1.0
            dx, dy = hx / hl, hy / hl
            end = QPointF(tip.x() - dx * L, tip.y() - dy * L)
            path = QPainterPath(tail)
            if (mid - tail).manhattanLength() > 0.5:
                path.lineTo(mid)               # 纯垂直拖拽时 mid==tail，跳过
            path.lineTo(end)
            line = QGraphicsPathItem(path)
            line.setPen(pen)
            return (line, head(tip, -dx, -dy))
        if style == "curve":
            # 曲线箭头：对称二次贝塞尔 —— 控制点取连线中点沿法线偏移，
            # 弓向拖拽方向一侧（换方向拖即可换弯向）；头方向 = 末端切线
            # （quadTo 在终点处切线 = tip - ctrl），线止于三角底边前。
            mx, my = (tail.x() + tip.x()) / 2.0, (tail.y() + tip.y()) / 2.0
            bow = max(14.0, length * 0.22)
            ctrl = QPointF(mx - uy * bow, my + ux * bow)
            hx, hy = tip.x() - ctrl.x(), tip.y() - ctrl.y()
            hl = math.hypot(hx, hy)
            if hl < 1e-6:
                hx, hy, hl = ux, uy, 1.0
            dx, dy = hx / hl, hy / hl
            end = QPointF(tip.x() - dx * L, tip.y() - dy * L)
            path = QPainterPath(tail)
            path.quadTo(ctrl, end)
            line = QGraphicsPathItem(path)
            line.setPen(pen)
            return (line, head(tip, -dx, -dy))
        if style == "circle":
            # 圆点箭头：线 + 末端实心圆点（替代三角头）
            line = QGraphicsLineItem(tail.x(), tail.y(), tip.x(), tip.y())
            line.setPen(pen)
            r = max(3.0, w * 1.5)
            dot = QGraphicsEllipseItem(-r, -r, 2 * r, 2 * r)
            dot.setPos(tip)
            dot.setBrush(QBrush(color))
            dot.setPen(Qt.NoPen)
            return (line, dot)
        base_x = tip.x() - ux * L                 # 翅膀根 = 线的终点
        base_y = tip.y() - uy * L
        line = QGraphicsLineItem(tail.x(), tail.y(), base_x, base_y)
        line.setPen(pen)
        return (line, head(tip, -ux, -uy))


class _PenElement(_BaseElement):
    """画笔图元：多点折线路径。
    style = solid(实线) / highlight(荧光笔) / dash(虚线) / dot(点线) /
            double(双线·法线偏移平行线) / wavy(等距重采样正弦) /
            glow(三层辉光)。"""

    @staticmethod
    def create(d, base):
        pts = d.get("points", [])
        if len(pts) < 2:
            return None
        style = d.get("style", "solid")
        color = QColor(d.get("color", "#ff5f5f"))
        w = d.get("width", 2)
        pen = QPen(color, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        if style == "highlight":
            pen = QPen(QColor(color.red(), color.green(), color.blue(), 80),
                       max(6, w * 3), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            return _polyline_item(pts, pen)
        if style == "glow":
            # 发光笔刷：三层辉光（外晕 38% / 中晕 90% / 亮芯 100%），
            # 模拟真实发光的亮度衰减，比旧的两层过渡自然得多。
            path = QPainterPath()
            path.moveTo(pts[0][0], pts[0][1])
            for px, py in pts[1:]:
                path.lineTo(px, py)

            def _layer(width, alpha):
                p = QPen(QColor(color.red(), color.green(), color.blue(),
                                alpha),
                         width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
                it = QGraphicsPathItem(path)
                it.setPen(p)
                it.setBrush(Qt.NoBrush)
                return it

            return (_layer(max(w * 5.0, 12.0), 38),   # 外层光晕
                    _layer(w * 2.6, 90),              # 中层过渡
                    _layer(w, 255))                   # 亮芯
        if style == "double":
            # 双线笔刷：轨迹沿法线 ±off 各偏移出一条平行线，两条线
            # 独立、端头不相连（Stroker 轮廓是首尾封口的「跑道形」，
            # 不是双线，已废弃）。拐角 miter 求交见 _offset_polyline；
            # 间距 max(4, w*1.8) 保证两线边缘始终留缝不糊在一起。
            off = max(4.0, w * 1.8)
            ded = _dedupe_points(pts)
            if len(ded) < 2:
                return None
            return (_polyline_item(_offset_polyline(ded, off), pen),
                    _polyline_item(_offset_polyline(ded, -off), pen))
        if style == "wavy":
            # 波浪笔刷：先等距重采样（消除手速不均），再沿法线正弦摆动，
            # 波形均匀稳定；振幅比旧版明显（max(2.5, w)）。
            rs = _resample_points(pts, step=5.0)
            amp = max(2.5, w * 1.0)
            waves = max(1.0, round(len(rs) / 14.0))
            norms = _norm_offsets(rs)
            n = max(1, len(rs) - 1)
            out_pts = [(px + nx * amp * math.sin(math.tau * i / n * waves),
                        py + ny * amp * math.sin(math.tau * i / n * waves))
                       for i, ((px, py), (nx, ny))
                       in enumerate(zip(rs, norms))]
            return _polyline_item(out_pts, pen)
        if style == "dash":
            pen.setStyle(Qt.DashLine)
        elif style == "dot":
            pen.setStyle(Qt.DotLine)
        return _polyline_item(pts, pen)


class _LineElement(_BaseElement):
    """直线图元：style = solid(实线) / dash(虚线) / dot(点线) /
    dashdot(点划线) / double(双线) / wavy(波浪线) / zigzag(锯齿线)。"""

    @staticmethod
    def create(d, base):
        style = d.get("style", "solid")
        color = QColor(d.get("color", "#ff5f5f"))
        w = d.get("width", 2)
        x0, y0 = d["x0"], d["y0"]
        x1, y1 = d["x1"], d["y1"]
        pen = QPen(color, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        if style == "dash":
            pen.setStyle(Qt.DashLine)
        elif style == "dot":
            pen.setStyle(Qt.DotLine)
        elif style == "dashdot":
            pen.setStyle(Qt.DashDotLine)
        if style == "double":
            # 双线：沿法线 ±off 两条平行直线
            dx, dy = x1 - x0, y1 - y0
            L = math.hypot(dx, dy)
            off = max(1.0, w * 0.8)
            if L > 1e-6:
                nx, ny = -dy / L * off, dx / L * off
                l1 = QGraphicsLineItem(x0 + nx, y0 + ny, x1 + nx, y1 + ny)
                l2 = QGraphicsLineItem(x0 - nx, y0 - ny, x1 - nx, y1 - ny)
            else:
                l1 = QGraphicsLineItem(x0, y0, x1, y1)
                l2 = QGraphicsLineItem(x0, y0, x1, y1)
            l1.setPen(pen)
            l2.setPen(pen)
            return (l1, l2)
        if style == "wavy":
            return _line_sine_item(x0, y0, x1, y1, pen, w)
        if style == "zigzag":
            return _line_zigzag_item(x0, y0, x1, y1, pen, w)
        it = QGraphicsLineItem(x0, y0, x1, y1)
        it.setPen(pen)
        return it


class _MosaicElement(_BaseElement):
    """马赛克图元：从原图 base 抠出矩形区域打码（不改原图）。"""

    @staticmethod
    def create(d, base):
        rect = QRectF(d["x0"], d["y0"], d["x1"] - d["x0"], d["y1"] - d["y0"])
        pm = _make_mosaic_pixmap(base, rect, d.get("block", 8))
        it = QGraphicsPixmapItem(pm)
        it.setPos(rect.topLeft())
        it.setZValue(4)
        return it


class _MosaicPenElement(_BaseElement):
    """马赛克·画笔图元：路径上每个点一个 block 大小的马赛克块（group）。"""

    @staticmethod
    def create(d, base):
        block = d.get("block", 8)
        group = QGraphicsItemGroup()
        for px, py in d.get("points", []):
            r = QRectF(px - block / 2.0, py - block / 2.0, block, block)
            pm = _make_mosaic_pixmap(base, r, block)
            item = QGraphicsPixmapItem(pm)
            item.setPos(r.topLeft())
            group.addToGroup(item)
        return group


class _TextElement(_BaseElement):
    """文字图元：单行文字，或带虚线边框的文本框（group）。"""

    @staticmethod
    def create(d, base):
        it = QGraphicsTextItem(d.get("text", ""))
        f = QFont(d.get("font") or "Microsoft YaHei")
        f.setPixelSize(d.get("font_size", 18))
        f.setBold(bool(d.get("bold", False)))
        f.setItalic(bool(d.get("italic", False)))
        it.setFont(f)
        it.setDefaultTextColor(QColor(d.get("color", "#ff5f5f")))
        if d.get("text_width"):
            it.setTextWidth(d["text_width"])
        it.setPos(d["x"], d["y"])
        it.setZValue(5)
        if d.get("text_width"):
            # 文本框：包一层可见虚线边框（尺寸随文本），区别于单行文字
            group = QGraphicsItemGroup()
            group.addToGroup(it)
            # 用 sceneBoundingRect（含 text 的 pos）而非本地 boundingRect，
            # 否则边框 box 会落到 group 原点附近、包围盒异常覆盖其它元素。
            sb = it.sceneBoundingRect()
            box = QGraphicsRectItem(sb.adjusted(-3, -3, 3, 3))
            box.setPen(QPen(QColor(120, 120, 140, 180), 1, Qt.DashLine))
            box.setBrush(Qt.NoBrush)
            group.addToGroup(box)
            return group
        return it


class _NumElement(_BaseElement):
    """序号图元：按 style 渲染多种序号样式。

    - circle   ：彩色实心圆 + 白字（默认）
    - circle_o ：彩色描边空心圆 + 彩字
    - square   ：彩色圆角方底 + 白字
    - paren    ：纯文字 "(n)"
    - plain    ：纯数字 "n"
    底色类样式数字居中于 (x,y)；纯文字样式 (x,y) 为文字中心。
    纯文字样式的数字高度与底色类样式对齐（同 font_size 视觉一致）：
    按裸数字墨迹套用与 circle 相同的缩放，再按整段墨迹居中。
    """

    @staticmethod
    def create(d, base):
        num = str(d.get("num", 0)) or "0"
        style = d.get("style", "circle")
        color = QColor(d.get("color", "#ff5f5f"))
        size = max(8, int(d.get("font_size", 18) or 18))
        f = QFont(d.get("font") or "Microsoft YaHei")
        f.setPixelSize(size)
        f.setBold(True)
        cx, cy = d["x"], d["y"]
        if style in ("paren", "dot", "plain"):
            # 纯文字样式：无底色，数字用标注色。
            # 缩放与底色类样式一致：按裸数字墨迹高度算 s（与 circle
            # 同公式），保证同 font_size 下 5 种样式的数字一样大。
            txt = {"paren": f"({num})", "dot": f"{num}.",
                   "plain": num}.get(style, num)
            ti = QGraphicsTextItem(txt)
            ti.setFont(f)
            ti.setDefaultTextColor(color)
            tr = ti.boundingRect()
            pad = max(3.0, size * 0.25)
            rh = max(size * 0.6, tr.height() / 2.0 + pad)
            di = QGraphicsTextItem(num)          # 裸数字（高度基准）
            di.setFont(f)
            dvis = _text_item_ink_rect(di)
            s = (min(2.0 * rh * 0.62 / max(dvis.height(), 1e-6), 2.2)
                 if not dvis.isEmpty() else 1.0)
            ti.setScale(s)
            ink = _text_item_ink_rect(ti)        # 整段墨迹（含括号/点）
            if ink.isEmpty():
                ink = QRectF(tr)
            ti.setPos(cx - ink.center().x() * s, cy - ink.center().y() * s)
            return ti
        ti = QGraphicsTextItem(num)
        ti.setFont(f)
        tr = ti.boundingRect()
        pad = max(3.0, size * 0.25)
        # 高半径只由行高决定（多位数不撑高，宽度交给胶囊半宽 rw）
        rh = max(size * 0.6, tr.height() / 2.0 + pad)
        # 数字墨迹实测框（ti 坐标系）：文本行高含上伸/下沉/行距空白，
        # 数字实际墨迹仅行高的 ~45%，按行高撑圈会让数字显小；
        # 按实测墨迹放大数字到视觉高度占圈高 ~62%（更饱满）。
        # 雅黑数字为全角设计（每字宽≈字高），多位数塞不进正圆时
        # 徽章拉宽成胶囊（Google Maps 式），两个方向都撑满。
        vis = _text_item_ink_rect(ti)
        if vis.isEmpty():
            vis = QRectF(tr)
        s = min(2.0 * rh * 0.62 / max(vis.height(), 1e-6), 2.2)
        rw = max(rh, s * vis.width() / 2.0 + pad)   # 半宽（正圆时 = rh）
        ti.setScale(s)
        if style == "circle_o":
            # 空心圆：描边用标注色，数字也用标注色（白字在空心底上不可读）
            badge = QGraphicsEllipseItem(QRectF(cx - rw, cy - rh, 2 * rw, 2 * rh))
            badge.setPen(QPen(color, max(1.5, size * 0.09)))
            badge.setBrush(Qt.NoBrush)
            ti.setDefaultTextColor(color)
        elif style == "square":
            badge = QGraphicsRectItem(QRectF(cx - rw, cy - rh, 2 * rw, 2 * rh))
            badge.setBrush(QBrush(color))
            badge.setPen(Qt.NoPen)
            ti.setDefaultTextColor(QColor("white"))
        else:   # circle
            badge = QGraphicsEllipseItem(QRectF(cx - rw, cy - rh, 2 * rw, 2 * rh))
            badge.setBrush(QBrush(color))
            badge.setPen(Qt.NoPen)
            ti.setDefaultTextColor(QColor("white"))
        # 按紧致框居中（setScale 后原点缩放，中心需乘 s 补偿）
        ti.setPos(cx - vis.center().x() * s, cy - vis.center().y() * s)
        group = QGraphicsItemGroup()
        group.addToGroup(badge)
        group.addToGroup(ti)
        return group


class _ItemFactory:
    """图元工厂：type → 图元类。新增图元 = 写一个 _*Element + 注册一行。"""

    _registry: dict = {}

    @classmethod
    def register(cls, kind: str):
        def deco(klass):
            cls._registry[kind] = klass
            return klass
        return deco

    @classmethod
    def create(cls, kind: str, d: dict, base):
        klass = cls._registry.get(kind)
        if klass is None:
            return None
        try:
            return klass.create(d, base)
        except Exception:                # noqa: BLE001  数据异常时不渲染，不崩溃
            return None


# 注册内置图元
for _kind, _klass in (("rect", _RectElement), ("arrow", _ArrowElement),
                      ("pen", _PenElement), ("line", _LineElement),
                      ("mosaic", _MosaicElement),
                      ("mosaic_pen", _MosaicPenElement),
                      ("text", _TextElement), ("num", _NumElement)):
    _ItemFactory.register(_kind)(_klass)

# 工具标识（select 为选择工具，非标注 item 类型；其余与 items["type"] 一致）
# 顺序即工具栏展示顺序：选择 序号 箭头 画笔 直线 矩形 马赛克 文字
TOOLS = ("select", "num", "arrow", "pen", "line", "rect", "mosaic", "text")
# 可反序列化的 item 类型见 ui/anno_core.SERIALIZABLE_TYPES（序列化亦在 anno_core）
TOOL_LABELS = {"select": "⌖", "rect": "□", "arrow": "↗", "pen": "✎",
               "line": "╱", "mosaic": "▦", "text": "T", "num": "Ⓝ"}
TOOL_TIPS = {"select": "选择（拖动移动 / Del 删除 / 双击编辑文字）",
             "rect": "矩形", "arrow": "箭头", "pen": "画笔", "line": "直线",
             "mosaic": "马赛克", "text": "文字",
             "num": "序号（单击放置，按起始序号递增）"}


def render_annotated_image(base: "QImage", annotations_json,
                           opaque: bool = True) -> "QImage":
    """底图 + 标注 JSON → 带标注的图（全分辨率，坐标与标注一致）。

    统一渲染入口：工具栏「完成 / 复制整图」与历史记录「复制图片（带标注）」
    共用此函数，保证两种导出路径渲染结果完全一致（含序号 num 等所有类型）。

    - base: 原图 QImage（BGR→RGB 的截图，或从文件加载的截图）。
    - annotations_json: AnnotationSession 序列化后的 JSON 字符串。
    - 返回 QImage。opaque=True 时输出 Format_RGB32（不透明），规避剪贴板
      DIB 的预乘 alpha 兼容问题；False 时输出 Format_ARGB32_Premultiplied。
    - 任一标注解析/渲染异常均被隔离（单条跳过），整体失败回退 base 拷贝。
    """
    try:
        if base is None or base.isNull():
            return base if base is not None else QImage()
        w, h = base.width(), base.height()
        if w <= 0 or h <= 0:
            return base.copy()
        items = anno_from_json(annotations_json)
        if not items:
            return base.copy()
        base_px = QPixmap.fromImage(base)
        scene = QGraphicsScene()
        scene.setSceneRect(0, 0, w, h)
        scene.addItem(QGraphicsPixmapItem(base_px))
        for d in items:
            try:
                it = _ItemFactory.create(d["type"], d, base_px)
            except Exception:            # noqa: BLE001 单条异常不中断整图
                it = None
            if it is None:
                continue
            subs = it if isinstance(it, tuple) else (it,)
            for s in subs:
                scene.addItem(s)
        fmt = QImage.Format_RGB32 if opaque else QImage.Format_ARGB32_Premultiplied
        out = QImage(w, h, fmt)
        out.fill(Qt.black if opaque else Qt.transparent)
        painter = QPainter(out)
        painter.setRenderHint(QPainter.Antialiasing, True)
        scene.render(painter, QRectF(0, 0, w, h), QRectF(0, 0, w, h))
        painter.end()
        return out
    except Exception:                    # noqa: BLE001 整体兜底
        try:
            return base.copy()
        except Exception:
            return QImage()


def _hex(qc: QColor) -> str:
    return qc.name()


# ---- 马赛克纹理 ----
def _make_mosaic_pixmap(base: QPixmap, rect: QRectF, block: int = 8) -> QPixmap:
    """把 base 中 rect 区域块状化（缩小再放大），返回同尺寸马赛克图。"""
    r = rect.toAlignedRect() & base.rect()
    if r.isEmpty():
        r = QRectF(rect).toAlignedRect()
    sub = base.copy(r)
    if sub.isNull() or sub.width() < 1 or sub.height() < 1:
        return sub
    small = sub.scaled(max(1, sub.width() // block),
                       max(1, sub.height() // block),
                       Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return small.scaled(sub.size(), Qt.IgnoreAspectRatio,
                        Qt.FastTransformation)


# ---- 工具栏 ----
class _WidthSliderPopup(QWidget):
    """线宽滑块浮层（音量条样式）：单击线宽按钮弹出，拖动调节，点外自动关闭。"""

    valueChanged = Signal(int)

    def __init__(self, parent=None, value: int = 2):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("WidthSliderPopup")
        card = QFrame(self)
        card.setObjectName("width_card")
        card.setAttribute(Qt.WA_StyledBackground)
        lay = QHBoxLayout(card)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(1, 32)
        self.slider.setValue(value)
        self.slider.setFixedWidth(130)
        self.slider.valueChanged.connect(self.valueChanged.emit)
        lay.addWidget(self.slider)
        self.lbl = QLabel(str(value))
        self.lbl.setObjectName("width_val")
        self.lbl.setFixedWidth(18)
        self.lbl.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.lbl)
        self.slider.valueChanged.connect(lambda v: self.lbl.setText(str(v)))
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(card)


class _WidthButton(QPushButton):
    """线宽按钮：**单击**弹出滑块（音量条样式）；**按住**左右/上下滑动直接调节
    （右大左小、上大下小）。按下移动超过阈值判定为拖动，否则松开视为单击弹滑块。"""

    clicked_pop = Signal()         # 单击 → 弹滑块
    width_adjusted = Signal(int)   # 按住拖动 → 实时线宽

    def __init__(self, text: str = "2", parent=None):
        super().__init__(text, parent)
        self._press = None        # (起始 x, 起始 y, 起始线宽)
        self._dragging = False

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            try:
                w0 = int(self.text())
            except ValueError:
                w0 = 2
            self._press = (e.globalPosition().x(), e.globalPosition().y(), w0)
            self._dragging = False
            self.setDown(True)   # 按下高亮
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press is not None and (e.buttons() & Qt.LeftButton):
            sx, sy, w0 = self._press
            dx = e.globalPosition().x() - sx   # 右为正
            dy = e.globalPosition().y() - sy   # 下为正
            # 移动超过阈值 → 判定为拖动（否则是准备单击）
            if not self._dragging and (abs(dx) >= 4 or abs(dy) >= 4):
                self._dragging = True
            if self._dragging:
                # 右大左小（dx 增大）、上大下小（-dy 增大）；约 8px = 1 档
                new_w = max(1, min(32, w0 + round((dx - dy) / 8.0)))
                self.width_adjusted.emit(new_w)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._press is not None:
            was_drag = self._dragging
            self._press = None
            self._dragging = False
            self.setDown(False)
            if not was_drag:              # 未拖动 → 单击 → 弹滑块
                self.clicked_pop.emit()
            return
        super().mouseReleaseEvent(e)


class _ColorSizeRow(QWidget):
    """颜色 + 大小 配置行（各标注工具子选项浮层通用，替代工具栏主行格子）。

    颜色 = 色块按钮（点击弹取色器）；大小 = 水平滑块（1-32，拖动即时生效）。
    信号：color_changed / width_changed。
    """

    color_changed = Signal(tuple)
    width_changed = Signal(int)

    def __init__(self, parent=None, color: tuple = (255, 95, 95), width: int = 2,
                 show_size: bool = True, show_color: bool = True,
                 size_range: tuple = (1, 32)):
        super().__init__(parent)
        self._size_range = (int(size_range[0]), int(size_range[1]))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        if show_color:
            lay.addWidget(QLabel("颜色"))
            from ui.base.widgets import ColorButton
            self.color_btn = ColorButton(color)
            self.color_btn.colorChanged.connect(self.color_changed.emit)
            lay.addWidget(self.color_btn)
        else:
            self.color_btn = None
        if show_size:
            lay.addSpacing(4)
            lay.addWidget(QLabel("大小"))
            self.slider = QSlider(Qt.Horizontal)
            self.slider.setRange(*self._size_range)
            self.slider.setValue(max(self._size_range[0],
                                     min(self._size_range[1], int(width))))
            self.slider.setFixedWidth(110)
            self.slider.valueChanged.connect(self.width_changed.emit)
            lay.addWidget(self.slider)
            self.lbl = QLabel(str(width))
            self.lbl.setFixedWidth(18)
            self.lbl.setAlignment(Qt.AlignCenter)
            self.slider.valueChanged.connect(lambda v: self.lbl.setText(str(v)))
            lay.addWidget(self.lbl)
        else:
            self.slider = None
            self.lbl = None

    def set_state(self, color: tuple, width: int):
        """打开时回显当前颜色/大小（信号抑制：回显不改状态）。"""
        if self.color_btn is not None:
            self.color_btn.set_color(tuple(color))
        if self.slider is not None:
            self.slider.blockSignals(True)
            self.slider.setValue(max(self._size_range[0],
                                     min(self._size_range[1], int(width))))
            self.slider.blockSignals(False)
            self.lbl.setText(str(self.slider.value()))


class _AnnoPopupBase(QWidget):
    """标注子选项浮层基类（Qt.Tool 独立顶层窗）。

    焦点落在浮层上时（如刚点过样式按钮/序号输入框），Q/W/E/R/T/Y/U 与 Esc
    转发给宿主贴图窗口统一处理，保证标注快捷键在浮层焦点下也生效。
    """

    def _find_overlay(self):
        w = self.parent()
        while w is not None:
            ov = getattr(w, "_overlay", None)
            if ov is not None:
                return ov
            w = w.parent()
        return None

    def keyPressEvent(self, e):
        ov = self._find_overlay()
        if ov is not None and getattr(ov, "_anno_mode", False) \
                and ov.handle_anno_key(e):
            return
        super().keyPressEvent(e)


class _FontPopup(_AnnoPopupBase):
    """文字属性浮层：点击「文字」工具按钮时在按钮下方弹出，不遮挡工具栏。

    第 1 行：字体/字号/加粗/斜体；第 2 行：颜色/大小（_ColorSizeRow）。
    Qt.Tool（非 Popup）：点击外部保持打开，再次点击/切换功能才关闭。
    """

    size_changed = Signal(int)
    bold_toggled = Signal(bool)
    italic_toggled = Signal(bool)
    color_changed = Signal(tuple)
    width_changed = Signal(int)

    SIZES = ("8", "10", "12", "14", "16", "18", "20", "24", "28", "32", "40", "48", "64")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("FontPopup")
        card = QFrame(self)
        card.setObjectName("font_card")
        card.setAttribute(Qt.WA_StyledBackground)
        # 第 1 行：字体 | 色块(无标签) | 加粗 | 斜体
        lay1 = QHBoxLayout()
        lay1.setSpacing(6)
        lay1.addWidget(QLabel("字体"))
        self.font_combo = QFontComboBox()
        self.font_combo.setMaximumWidth(150)
        self.font_combo.setCurrentFont(QFont("Microsoft YaHei"))
        lay1.addWidget(self.font_combo)
        from ui.base.widgets import ColorButton
        self.color_btn = ColorButton((255, 95, 95))
        self.color_btn.colorChanged.connect(self.color_changed.emit)
        lay1.addWidget(self.color_btn)
        self.btn_bold = QPushButton("B")
        self.btn_bold.setCheckable(True)
        self.btn_bold.setFocusPolicy(Qt.NoFocus)
        self.btn_bold.setToolTip("加粗")
        f = self.btn_bold.font(); f.setBold(True); self.btn_bold.setFont(f)
        self.btn_bold.setFixedWidth(30)
        self.btn_bold.toggled.connect(self.bold_toggled.emit)
        lay1.addWidget(self.btn_bold)
        self.btn_italic = QPushButton("I")
        self.btn_italic.setCheckable(True)
        self.btn_italic.setFocusPolicy(Qt.NoFocus)
        self.btn_italic.setToolTip("斜体")
        fi = self.btn_italic.font(); fi.setItalic(True); self.btn_italic.setFont(fi)
        self.btn_italic.setFixedWidth(30)
        self.btn_italic.toggled.connect(self.italic_toggled.emit)
        lay1.addWidget(self.btn_italic)
        # 第 2 行：大小滑块（8-64，与其他工具滑块样式一致）
        lay2 = QHBoxLayout()
        lay2.setSpacing(6)
        lay2.addWidget(QLabel("大小"))
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(8, 64)
        self.size_slider.setValue(18)
        self.size_slider.setFixedWidth(140)
        self.size_slider.valueChanged.connect(self.size_changed.emit)
        lay2.addWidget(self.size_slider)
        self.size_lbl = QLabel("18")
        self.size_lbl.setFixedWidth(18)
        self.size_lbl.setAlignment(Qt.AlignCenter)
        self.size_slider.valueChanged.connect(
            lambda v: self.size_lbl.setText(str(v)))
        lay2.addWidget(self.size_lbl)
        lay2.addStretch(1)
        root = QVBoxLayout(card)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)
        root.addLayout(lay1)
        root.addLayout(lay2)
        root2 = QVBoxLayout(self)
        root2.setContentsMargins(0, 0, 0, 0)
        root2.addWidget(card)

    def set_state(self, size: int, bold: bool, italic: bool,
                  color: tuple = (255, 95, 95), _width: int = 2):
        """打开时回显当前文字属性 + 颜色（信号抑制：回显不改状态）。"""
        self.size_slider.blockSignals(True)
        self.size_slider.setValue(max(8, min(64, int(size))))
        self.size_slider.blockSignals(False)
        self.size_lbl.setText(str(self.size_slider.value()))
        self.btn_bold.setChecked(bool(bold))
        self.btn_italic.setChecked(bool(italic))
        self.color_btn.set_color(tuple(color))


class _MosaicModePopup(_AnnoPopupBase):
    """马赛克模式选择浮层：点击马赛克工具时在按钮下方弹出。

    第 1 行：模式（画笔/框选/文字框）；第 2 行：颜色/大小（_ColorSizeRow）。
    Qt.Tool（非 Popup）：点击外部保持打开，再次点击/切换功能才关闭。
    """

    MODES = (("pen", "画笔"), ("rect", "框选"), ("textbox", "文字框"))
    modeSelected = Signal(str)
    color_changed = Signal(tuple)
    width_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("MosaicModePopup")
        card = QFrame(self)
        card.setObjectName("mosaic_card")
        card.setAttribute(Qt.WA_StyledBackground)
        root = QVBoxLayout(card)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)
        # 第 1 行：模式（互斥单选：同一时刻只亮一个）
        lay = QHBoxLayout()
        lay.setSpacing(4)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._btns: dict[str, QPushButton] = {}
        for mode, label in self.MODES:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setFocusPolicy(Qt.NoFocus)   # 点击不抢焦点（快捷键仍归贴图窗口）
            b.clicked.connect(lambda _=False, m=mode: self.modeSelected.emit(m))
            self._btns[mode] = b
            self._group.addButton(b)
            lay.addWidget(b)
        root.addLayout(lay)
        # 第 2 行：大小（马赛克块大小；颜色取自原图像素，不提供颜色配置）
        self.cs = _ColorSizeRow(card, show_color=False)
        self.cs.color_changed.connect(self.color_changed.emit)
        self.cs.width_changed.connect(self.width_changed.emit)
        root.addWidget(self.cs)
        root2 = QVBoxLayout(self)
        root2.setContentsMargins(0, 0, 0, 0)
        root2.addWidget(card)

    def set_current(self, mode: str):
        """高亮当前模式（记忆上次选择）。"""
        for m, b in self._btns.items():
            b.setChecked(m == mode)

    def set_state(self, mode: str, color: tuple, width: int):
        """打开时回显当前模式 + 颜色/大小。"""
        self.set_current(mode)
        self.cs.set_state(color, width)


class _StyleIconRow(QWidget):
    """样式选择行：一组 SVG 图标按钮（互斥单选：选中 = 白图标；未选中 = 主题色）。

    序号/矩形/箭头/画笔的子选项浮层共用，替代文字按钮。
    """

    style_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)     # 互斥：同一时刻只亮一个样式
        self._btns: dict[str, QPushButton] = {}

    def set_styles(self, styles):
        """重建按钮组：styles = [(style_id, icon_name, tooltip), ...]。"""
        from ui.base.icon import icon as svg_icon
        from ui.base.theme import icon_color
        lay = self.layout()
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                self._group.removeButton(w)
                w.deleteLater()
        self._btns = {}

        def refresh(b: QPushButton):
            iname = b.property("style_icon")
            if not iname:
                return
            color = "#ffffff" if b.isChecked() else icon_color()
            b.setIcon(svg_icon(iname, color=color, size=18))

        for sid, iname, tip in styles:
            b = QPushButton()
            b.setCheckable(True)
            b.setFixedSize(28, 28)
            b.setFocusPolicy(Qt.NoFocus)   # 点击不抢焦点：焦点留在图片/工具栏窗口
            b.setProperty("style_icon", iname)
            b.setToolTip(tip)
            b.clicked.connect(
                lambda _=False, m=sid: self.style_selected.emit(m))
            b.toggled.connect(lambda _=False, bb=b: refresh(bb))
            self._btns[sid] = b
            self._group.addButton(b)
            lay.addWidget(b)
        for b in self._btns.values():
            refresh(b)

    def set_current(self, style: str):
        """高亮当前样式（打开时回显，记忆上次选择）。"""
        for m, b in self._btns.items():
            b.setChecked(m == style)


class _ToolOptionsPopup(_AnnoPopupBase):
    """通用工具选项浮层（矩形/箭头/画笔）：样式图标行 + 颜色/大小配置行。"""

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

    color_changed = Signal(tuple)
    width_changed = Signal(int)
    style_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("ToolOptionsPopup")
        card = QFrame(self)
        card.setObjectName("tool_options_card")
        card.setAttribute(Qt.WA_StyledBackground)
        root = QVBoxLayout(card)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)
        # 样式图标行（按当前工具重建；无样式的工具隐藏）
        self.style_row = _StyleIconRow(card)
        self.style_row.style_selected.connect(self.style_selected.emit)
        root.addWidget(self.style_row)
        self.cs = _ColorSizeRow(card)
        self.cs.color_changed.connect(self.color_changed.emit)
        self.cs.width_changed.connect(self.width_changed.emit)
        root.addWidget(self.cs)
        root2 = QVBoxLayout(self)
        root2.setContentsMargins(0, 0, 0, 0)
        root2.addWidget(card)

    def set_state(self, color: tuple, width: int, tool: str = "",
                  style: str | None = None):
        """打开时回显当前颜色/大小 + 按工具重建样式图标行。"""
        styles = self.TOOL_STYLES.get(tool)
        if styles:
            self.style_row.set_styles(styles)
            self.style_row.set_current(style or styles[0][0])
            self.style_row.show()
        else:
            self.style_row.hide()
        self.cs.set_state(color, width)


class _NumPopup(_AnnoPopupBase):
    """序号工具选项浮层：点击「序号」工具时在按钮下方弹出。

    第 1 行：序号（左上角 QSpinBox，两位数宽）+ 样式图标（同行）；
    第 2 行：颜色/大小（_ColorSizeRow）。
    Qt.Tool（非 Popup）：点击外部保持打开，再次点击/切换功能才关闭。
    """

    STYLES = (("circle", "num-circle", "实心圆"),
              ("circle_o", "num-circle-o", "空心圆"),
              ("square", "num-square", "圆角方"),
              ("paren", "num-paren", "(1) 括号"),
              ("plain", "num-plain", "纯数字"))

    start_changed = Signal(int)
    style_selected = Signal(str)
    size_changed = Signal(int)
    color_changed = Signal(tuple)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("NumPopup")
        card = QFrame(self)
        card.setObjectName("num_card")
        card.setAttribute(Qt.WA_StyledBackground)
        root = QVBoxLayout(card)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)
        # 第 1 行：序号（左上）+ 样式图标（同行）
        lay0 = QHBoxLayout()
        lay0.setSpacing(6)
        lay0.addWidget(QLabel("序号"))
        self.start_spin = QSpinBox()
        self.start_spin.setRange(0, 9999)
        self.start_spin.setValue(1)
        self.start_spin.setFixedWidth(52)      # 两位数宽
        self.start_spin.setToolTip("序号标注的最低起始序号（其余按 +1 递增）")
        self.start_spin.valueChanged.connect(self.start_changed.emit)
        lay0.addWidget(self.start_spin)
        lay0.addStretch(1)
        self.style_row = _StyleIconRow(card)
        self.style_row.style_selected.connect(self.style_selected.emit)
        self.style_row.set_styles(self.STYLES)
        lay0.addWidget(self.style_row)
        root.addLayout(lay0)
        # 第 2 行：颜色 + 大小（序号数字字号，像素；圆圈直径约为字号 2 倍）
        self.cs = _ColorSizeRow(card, show_size=True, show_color=True,
                                size_range=(8, 64))
        if self.cs.slider is not None:
            self.cs.slider.setToolTip(
                "数字字号（像素）；圆圈直径约为字号的 2 倍，"
                "多位数自动拉宽为胶囊形")
        self.cs.color_changed.connect(self.color_changed.emit)
        self.cs.width_changed.connect(self.size_changed.emit)
        root.addWidget(self.cs)
        root2 = QVBoxLayout(self)
        root2.setContentsMargins(0, 0, 0, 0)
        root2.addWidget(card)

    def set_state(self, start: int, size: int, color: tuple, style: str):
        """打开时回显当前起始序号 + 大小 + 颜色 + 样式（信号抑制：回显不改状态）。"""
        self.style_row.set_current(style)
        self.start_spin.blockSignals(True)
        self.start_spin.setValue(max(0, min(9999, int(start))))
        self.start_spin.blockSignals(False)
        self.cs.set_state(color, size)


# ---- 标注会话 ----
class AnnotationSession:
    """标注数据 + 场景图层 + 撤销重做。由 ResultOverlay 驱动。"""

    def __init__(self, view, scene, base_pixmap: QPixmap):
        self.view = view
        self.scene = scene
        self.base = base_pixmap
        self.tool = "rect"
        self.color = (255, 95, 95)
        self.width = 2
        self.font_family = "Microsoft YaHei"
        self.font_size = 18
        self.font_bold = False
        self.font_italic = False
        self.mosaic_mode = "rect"   # 马赛克模式：pen(画笔)/rect(框选)/textbox(文字框)
        self.num_start = 1          # 序号标注最低起始序号
        self.num_size = 18          # 序号圆圈/数字尺寸
        self.num_style = "circle"   # 序号样式：circle/circle_o/square/paren/plain
        # 矩形/箭头/直线/画笔的样式（各自记忆，切换工具不丢）
        self.tool_styles = {"rect": "rect", "arrow": "single", "line": "solid",
                            "pen": "solid"}
        self.data: list[dict] = []
        self._items: list = []          # QGraphicsItem
        self._undo: list[_Cmd] = []
        self._redo: list[_Cmd] = []
        self._drawing: dict | None = None
        self._draw_item = None
        self._last_pt = None
        self._press_pt = None           # text 工具：按下起点（区分单击/拖动）
        self._text_drag_rect = None     # text 工具：拖动中的预览矩形
        self._text_edit = None          # 就地编辑中的文字项（text 工具）
        self._text_edit_box = None      # 文本框编辑时的可见虚线边框
        self._editing_index = None      # 正在编辑的已有文字块 data 索引（双击编辑）
        self._hidden_edit_items: list = []  # 双击编辑时隐藏的旧渲染项（防编辑态与旧渲染重影）
        # 选择工具（select）：选中/拖动/删除标注
        self._selected_idx = None       # 选中的 data 索引
        self._select_box = None         # 选中高亮虚线框
        self._select_press = None       # 拖动基准点（场景坐标，用于增量 moveBy）
        self._select_start_pt = None    # 拖动起点（用于 release 计算总位移）
        self._select_moved = False
        self._select_items: list = []   # 选中项缓存（拖动时只 moveBy 这些，避免每次 move 全量遍历）
        # 就地文字编辑：编辑器失焦时提交
        self.scene.focusItemChanged.connect(self._on_focus_changed)

    # ---- 工具/外观 ----
    def set_tool(self, tool: str):
        self.tool = tool if tool in TOOLS else "rect"

    def set_color(self, color: tuple):
        self.color = tuple(int(c) for c in color)

    def set_width(self, w: int):
        self.width = max(1, int(w))

    def set_font(self, family: str):
        self.font_family = family or "Microsoft YaHei"

    def set_font_size(self, v: int):
        self.font_size = max(6, min(96, int(v)))

    def set_bold(self, b: bool):
        self.font_bold = bool(b)

    def set_italic(self, b: bool):
        self.font_italic = bool(b)

    def set_mosaic_mode(self, mode: str):
        self.mosaic_mode = mode if mode in ("pen", "rect", "textbox") else "rect"

    def set_num_start(self, v: int, renumber: bool = False):
        """设置序号起始序号；renumber=True 时同步重排已有序号标注。"""
        self.num_start = max(0, int(v))
        if renumber:
            self.renumber(self.num_start)

    def set_num_size(self, v: int):
        self.num_size = max(8, min(64, int(v)))

    def set_num_style(self, style: str):
        if style in ("circle", "circle_o", "square", "paren", "plain"):
            self.num_style = style

    def set_tool_style(self, tool: str, style: str):
        """设置矩形/箭头/直线/画笔的样式（合法值见 _ToolOptionsPopup.TOOL_STYLES）。"""
        if tool not in self.tool_styles:
            return
        styles = ("rect", "filled", "round", "ellipse", "dialog",
                  "highlight", "dash") if tool == "rect" else \
                 ("single", "double", "dash", "elbow", "curve",
                  "dot", "circle") if tool == "arrow" else \
                 ("solid", "dash", "dot", "dashdot", "double",
                  "wavy", "zigzag") if tool == "line" else \
                 ("solid", "highlight", "dash", "dot", "double",
                  "wavy", "glow")
        if style in styles:
            self.tool_styles[tool] = style

    def renumber(self, start: int):
        """按起始序号重排所有已放置的序号标注（顺序 = data 中的出现顺序）。"""
        changes = []
        seq = 0
        for i, d in enumerate(self.data):
            if d.get("type") != "num":
                continue
            old = int(d.get("num", 0))
            new = int(start) + seq
            seq += 1
            if old != new:
                changes.append((i, old, new))
                d["num"] = new
        if changes:
            self._push_cmd(_RenumberCmd(changes))
            self._render_all()

    @property
    def is_drawing(self) -> bool:
        """是否正在画布上拖拽绘制（区别于点击工具栏按钮等其它左键操作）。"""
        return self._drawing is not None

    # ---- 数据 ----
    def load(self, items: list):
        self.data = copy.deepcopy(items)
        # 重建撤销栈：按 items 顺序生成 AddCmd 序列（add0、add1…），
        # 使恢复历史后也能分步撤销（例如画 3 个框可逐框撤回，而非整体一步）。
        self._undo = [_AddCmd(i, it) for i, it in enumerate(items)]
        self._redo = []
        self._render_all()

    def json(self) -> str:
        return anno_to_json(self.data)

    # ---- 渲染 ----
    def _clear_layer(self):
        for it in self._items:
            self.scene.removeItem(it)
        self._items = []
        self._clear_select_box()

    def _make_item(self, d: dict):
        """按类型经图元工厂创建场景项（图元类见 _ItemFactory 注册表）。"""
        return _ItemFactory.create(d["type"], d, self.base)

    def _render_all(self):
        self._clear_layer()
        for i, d in enumerate(self.data):
            self._add_to_scene(d, i)

    def _add_to_scene(self, d: dict, idx: int | None = None):
        it = self._make_item(d)
        if it is None:
            return
        items = it if isinstance(it, tuple) else (it,)
        for i in items:
            i.setZValue(5)
            self.scene.addItem(i)
            self._tag_idx(i, idx)
        self._items.extend(items)

    def _tag_idx(self, item, idx: int | None):
        """给标注项（含 group 内子项）标记 data 索引，供选择/移动/命中。"""
        if idx is None:
            return
        item._anno_idx = idx
        for ch in item.childItems():
            self._tag_idx(ch, idx)

    # ---- 撤销 / 重做 / 清空（命令栈）----
    def _push_cmd(self, cmd: _Cmd):
        """压入一条撤销命令（清空重做栈，裁剪到上限）。"""
        self._undo.append(cmd)
        if len(self._undo) > UNDO_LIMIT:
            self._undo.pop(0)
        self._redo.clear()

    def undo(self):
        if not self._undo:
            return
        cmd = self._undo.pop()
        cmd.unapply(self.data)
        self._redo.append(cmd)
        self._render_all()

    def redo(self):
        if not self._redo:
            return
        cmd = self._redo.pop()
        cmd.apply(self.data)
        self._undo.append(cmd)
        self._render_all()

    def clear(self):
        if not self.data:
            return
        self._push_cmd(_ClearCmd(self.data))
        self.data = []
        self._render_all()

    # ---- 鼠标绘制 ----
    def mouse_press(self, pt: QPointF):
        self._last_pt = pt
        # 若仍在文字编辑态，先提交退出（点其它元素/切工具都应先退出当前编辑）。
        # 否则编辑态残留、旧项被隐藏，会导致后续命中错乱、选不中其它元素。
        if self._text_edit is not None:
            self._commit_text()
        if self.tool == "select":
            self._on_select_press(pt)
            return
        if self.tool == "text":
            # 单击 = 单行文字；按住拖动 = 文本框（release 判定）
            self._press_pt = pt
            self._text_drag_rect = None
            self._drawing = {"type": "text"}
            self._draw_item = None
            return
        # 序号工具：单击即放置一个序号（无需拖动），编号 = 起始序号 + 已有数量
        if self.tool == "num":
            self._place_num(pt)
            return
        # 马赛克·画笔模式：涂抹式，路径上连续打码
        if self.tool == "mosaic" and self.mosaic_mode == "pen":
            d = {"type": "mosaic_pen", "points": [[pt.x(), pt.y()]],
                 "color": _hex(QColor(*self.color)), "width": self.width,
                 "block": self.width}
            self._drawing = d
            self._draw_item = None
            return
        # 马赛克·文字框模式：一次点击对一个 OCR 文字框打码（无需拖动）
        if self.tool == "mosaic" and self.mosaic_mode == "textbox":
            box = self._hit_textbox(pt)
            if box is None:
                return
            x0, y0, x1, y1 = box
            d = {"type": "mosaic", "color": _hex(QColor(*self.color)),
                 "width": self.width, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                 "block": self.width}
            self._push_cmd(_AddCmd(len(self.data), d))
            self.data.append(d)
            self._render_all()
            return
        d = {"type": self.tool, "color": _hex(QColor(*self.color)),
             "width": self.width,
             "style": self.tool_styles.get(self.tool, "")}
        if self.tool == "pen":
            d["points"] = [[pt.x(), pt.y()]]
        else:
            d.update(x0=pt.x(), y0=pt.y(), x1=pt.x(), y1=pt.y())
            if self.tool == "mosaic":
                d["block"] = self.width
        self._drawing = d
        self._draw_item = None

    def _place_num(self, pt: QPointF):
        """序号工具：在点击处放置一个序号标注（编号 = 起始序号 + 已有序号数）。"""
        cnt = sum(1 for d in self.data if d.get("type") == "num")
        d = {"type": "num", "x": pt.x(), "y": pt.y(),
             "num": self.num_start + cnt,
             "style": self.num_style,
             "color": _hex(QColor(*self.color)),
             "font_size": self.num_size,
             "font": self.font_family, "bold": True}
        self._push_cmd(_AddCmd(len(self.data), d))
        self.data.append(d)
        self._render_all()

    def _hit_textbox(self, pt: QPointF):
        """文字框打码：返回 pt 命中的 OCR 文字框 bbox (x0,y0,x1,y1)；无则 None。"""
        boxes = getattr(self.view, "boxes", None) or []
        for b in boxes:
            poly = getattr(b, "polygon", None)
            if not poly:
                continue
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            if min(xs) <= pt.x() <= max(xs) and min(ys) <= pt.y() <= max(ys):
                return min(xs), min(ys), max(xs), max(ys)
        return None

    def mouse_move(self, pt: QPointF):
        if self.tool == "select":
            self._select_move(pt)
            return
        if self._drawing is None:
            return
        self._last_pt = pt
        if self.tool == "text":
            # 拖动：显示文本框虚线预览（单击不出现）
            r = QRectF(self._press_pt, pt).normalized()
            self._text_drag_rect = r
            if self._draw_item is not None:
                self.scene.removeItem(self._draw_item)
                self._draw_item = None
            if r.width() >= 5 and r.height() >= 5:
                self._draw_item = QGraphicsRectItem(r)
                self._draw_item.setPen(
                    QPen(QColor(120, 120, 140, 170), 1, Qt.DashLine))
                self._draw_item.setBrush(QBrush(QColor(0, 0, 0, 12)))
                self._draw_item.setZValue(6)
                self.scene.addItem(self._draw_item)
            return
        if self.tool == "pen" or self._drawing.get("type") == "mosaic_pen":
            self._drawing["points"].append([pt.x(), pt.y()])
        else:
            self._drawing["x1"] = pt.x()
            self._drawing["y1"] = pt.y()
        if self._draw_item is not None:
            self.scene.removeItem(self._draw_item)
            self._draw_item = None
        # 马赛克·框选模式：拖动时显示空心虚线框（画框式，同矩形工具），
        # 松手才生成马赛克；画笔模式走 _make_item 实时显示涂抹块。
        if (self.tool == "mosaic" and self._drawing is not None
                and self._drawing.get("type") == "mosaic"):
            r = QRectF(self._drawing["x0"], self._drawing["y0"],
                       self._drawing["x1"] - self._drawing["x0"],
                       self._drawing["y1"] - self._drawing["y0"]).normalized()
            self._draw_item = QGraphicsRectItem(r)
            self._draw_item.setPen(QPen(QColor(*self.color), 1, Qt.DashLine))
            self._draw_item.setBrush(QBrush(QColor(*self.color, 32)))
            self._draw_item.setZValue(6)
            self.scene.addItem(self._draw_item)
            return
        if self._drawing is not None:
            made = self._make_item(self._drawing)
            if isinstance(made, tuple):
                # 箭头：线 + 三角组合为 group，拖动时**实时显示完整箭头**
                self._draw_item = QGraphicsItemGroup()
                for it in made:
                    self._draw_item.addToGroup(it)
            else:
                self._draw_item = made
            if self._draw_item is not None:
                self._draw_item.setZValue(6)
                self.scene.addItem(self._draw_item)

    def mouse_release(self, _pt: QPointF):
        if self.tool == "select":
            self._select_release(_pt)
            return
        if self._drawing is None:
            return
        if self._draw_item is not None:
            self.scene.removeItem(self._draw_item)
            self._draw_item = None
        if self.tool == "text":
            d = self._drawing
            self._drawing = None
            self._editing_index = None     # 新建文字块（非编辑已有）
            r = self._text_drag_rect
            if r is not None and r.width() >= 5 and r.height() >= 5:
                # 按住拖动 → 文本框（固定宽度，支持换行/自动换行）
                self._start_text_edit(QPointF(r.x(), r.y()), width=r.width())
            else:
                # 单击 → 单行文字（现状：就地编辑）
                self._start_text_edit(QPointF(self._press_pt))
            return
        d = self._drawing
        self._drawing = None
        # 归一化坐标（反向拖动时 x1<x0 / y1<y0），保证矩形类工具方向正确。
        # 注意：arrow 不归一化——尖端在点击处（x0,y0）有方向语义，归一化会破坏方向；
        # line 同样不归一化——x/y 独立排序会把线段变成包围盒对角线（斜率翻转）。
        if d["type"] in ("rect", "mosaic"):
            d["x0"], d["x1"] = sorted((d["x0"], d["x1"]))
            d["y0"], d["y1"] = sorted((d["y0"], d["y1"]))
        # 过小过滤（矩形/箭头/直线/框选马赛克；pen/mosaic_pen 不过滤）
        if d["type"] in ("rect", "arrow", "line", "mosaic"):
            if abs(d["x1"] - d["x0"]) < 3 and abs(d["y1"] - d["y0"]) < 3:
                return
        self._push_cmd(_AddCmd(len(self.data), d))
        self.data.append(d)
        self._render_all()

    # ---- 选择工具（select）：选中/拖动移动/删除标注 ----
    def _on_select_press(self, pt: QPointF):
        idx = self._hit_anno_idx(pt)
        if idx is not None and not (0 <= idx < len(self.data)):
            idx = None                       # 防御：索引越界则视为未命中
        self._selected_idx = idx
        self._select_moved = False
        if idx is not None:
            self._select_press = pt
            self._select_start_pt = pt
            # 缓存选中项（含箭头 line+head、文本框 group 等所有顶层 item），
            # 拖动时只对这些 moveBy，O(选中项数) 而非 O(总标注数)，保证跟手。
            self._select_items = [it for it in self._items
                                  if getattr(it, "_anno_idx", None) == idx]
            self._show_select_box(idx)
        else:
            self._select_press = None
            self._select_start_pt = None
            self._select_items = []
            self._clear_select_box()

    def _select_move(self, pt: QPointF):
        # 拖动中实时 moveBy 选中项（仅改 transform，极轻量），元素跟手；
        # data 只在 release 时一次性写回，避免拖动中反复全量重建导致卡顿。
        if self._selected_idx is None or self._select_press is None:
            return
        delta = pt - self._select_press
        if delta.manhattanLength() == 0:
            return
        if not self._select_moved:
            self._clear_select_box()       # 首次真正拖动：隐藏选中框，避免拖尾
            self._select_moved = True
        for it in self._select_items:
            it.moveBy(delta.x(), delta.y())
        self._select_press = pt

    def _select_release(self, pt: QPointF):
        if self._selected_idx is not None and self._select_moved \
                and self._select_start_pt is not None:
            dx = pt.x() - self._select_start_pt.x()
            dy = pt.y() - self._select_start_pt.y()
            if dx or dy:
                self._push_cmd(_MoveCmd(self._selected_idx, dx, dy))
                self._move_data(self._selected_idx, dx, dy)
                # 不重建：item 已实时 moveBy 到目标位置，坐标与 data 一致。
        self._select_press = None
        self._select_start_pt = None
        self._select_moved = False
        self._select_items = []
        if self._selected_idx is not None:
            self._show_select_box(self._selected_idx)

    def _move_data(self, idx: int, dx: float, dy: float):
        d = self.data[idx]
        t = d["type"]
        if t in ("rect", "arrow", "line", "mosaic"):
            d["x0"] = d["x0"] + dx
            d["x1"] = d["x1"] + dx
            d["y0"] = d["y0"] + dy
            d["y1"] = d["y1"] + dy
        elif t in ("pen", "mosaic_pen"):
            d["points"] = [[x + dx, y + dy] for x, y in d["points"]]
        elif t in ("text", "num"):
            d["x"] = d["x"] + dx
            d["y"] = d["y"] + dy

    def _hit_anno_idx(self, pt: QPointF):
        """命中检测（仅标注层）：返回 pt 处标注项的 data 索引；无则 None。

        只遍历 self._items（标注层列表），不调用 scene.items() 跨层命中；
        倒序遍历（后画的视觉上层优先）。
        用 sceneBoundingRect + 4px 容差命中（而非精确 contains）——箭头/画笔等
        细线型 item 的精确命中区域极小，点偏一点就选不中；包围盒命中更宽松易选。
        """
        for it in reversed(self._items):
            idx = getattr(it, "_anno_idx", None)
            if idx is None:
                continue
            try:
                r = it.sceneBoundingRect()
                if r.isNull():
                    continue
                if r.adjusted(-4, -4, 4, 4).contains(pt):
                    return idx
            except Exception:            # noqa: BLE001
                continue
        return None

    def _show_select_box(self, idx: int):
        self._clear_select_box()
        rects = [it.sceneBoundingRect() for it in self._items
                 if getattr(it, "_anno_idx", None) == idx]
        if not rects:
            return
        r = rects[0]
        for rr in rects[1:]:
            r = r.united(rr)
        box = QGraphicsRectItem(r.adjusted(-4, -4, 4, 4))
        box.setPen(QPen(QColor(90, 150, 255), 1.4, Qt.DashLine))
        box.setBrush(Qt.NoBrush)
        box.setZValue(10)
        self.scene.addItem(box)
        self._select_box = box

    def _clear_select_box(self):
        if self._select_box is not None:
            self.scene.removeItem(self._select_box)
            self._select_box = None

    def delete_selected(self) -> bool:
        """删除当前选中的标注（Delete/Backspace）。返回是否删除。"""
        if self._selected_idx is None \
                or not (0 <= self._selected_idx < len(self.data)):
            self._selected_idx = None
            self._clear_select_box()
            return False
        self._push_cmd(_DeleteCmd(self._selected_idx,
                                  copy.deepcopy(self.data[self._selected_idx])))
        self.data.pop(self._selected_idx)
        self._selected_idx = None
        self._clear_select_box()
        self._render_all()
        return True

    # ---- 双击编辑已有文字（画其它标注后还能回来改文字）----
    def mouse_double_click(self, pt: QPointF):
        """双击已提交的文字（单行/文本框）→ 重新进入编辑，数据块原地替换。"""
        if self._text_edit is not None:
            # 双击序列中第一次 release 已创建了空编辑框 → 取消它再编辑已有项；
            # 若已有内容（用户正在打字时双击）则不打断。
            if self._text_edit.toPlainText() == "":
                self.cancel_text_edit()
            else:
                return
        it = self._hit_text_item(pt)
        if it is None:
            return
        idx = getattr(it, "_anno_idx", None)
        if idx is None or not (0 <= idx < len(self.data)):
            return
        d = self.data[idx]
        self._editing_index = idx
        self._selected_idx = None
        self._clear_select_box()
        # 隐藏旧渲染项（含文本框 group 及其边框），避免与新的编辑态 item 叠加产生重影；
        # 提交时 _render_all 会 removeItem 这些旧项；取消时 _cancel 恢复显示。
        self._hidden_edit_items = [i for i in self._items
                                   if getattr(i, "_anno_idx", None) == idx]
        for i in self._hidden_edit_items:
            i.setVisible(False)
        self._start_text_edit(QPointF(d["x"], d["y"]),
                              width=d.get("text_width"))
        self._text_edit.setPlainText(d["text"])
        try:
            from PySide6.QtGui import QTextCursor
            cur = self._text_edit.textCursor()
            cur.movePosition(QTextCursor.End)
            self._text_edit.setTextCursor(cur)
        except Exception:                    # noqa: BLE001
            pass

    def _hit_text_item(self, pt: QPointF):
        """命中检测（仅标注层）：返回 pt 处已提交的文字项（含文本框 group 内文本项）。
        倒序遍历（后画的视觉上层优先）；包围盒 + 容差命中（与 _hit_anno_idx 一致）。
        仅命中 type=="text" 的数据块——序号/其它含文本子项的图元不允许双击
        进入文字编辑（各自成类，防止误编辑转成 text）。"""
        for it in reversed(self._items):
            idx = getattr(it, "_anno_idx", None)
            if idx is not None and 0 <= idx < len(self.data) \
                    and self.data[idx].get("type") != "text":
                continue                      # 非文字块（如序号）：不可文字编辑
            try:
                r = it.sceneBoundingRect()
                if r.isNull() or not r.adjusted(-4, -4, 4, 4).contains(pt):
                    continue
                if hasattr(it, "toPlainText"):
                    return it
                for ch in it.childItems():
                    if hasattr(ch, "toPlainText"):
                        return ch
            except Exception:            # noqa: BLE001
                continue
        return None

    # ---- 文字就地编辑（text 工具：单击=单行 / 拖动=文本框，失焦提交）----
    def _start_text_edit(self, pt: QPointF, width: float | None = None):
        self.cancel_text_edit()          # 清理未提交的旧编辑
        item = QGraphicsTextItem()
        f = QFont(self.font_family)
        f.setPixelSize(self.font_size)
        f.setBold(self.font_bold)
        f.setItalic(self.font_italic)
        item.setFont(f)
        item.setDefaultTextColor(QColor(*self.color))
        item.setTextInteractionFlags(Qt.TextEditorInteraction)
        if width:
            item.setTextWidth(width)     # 文本框：固定宽度，Enter 换行/自动换行
        item.setPos(pt.x(), pt.y())
        item.setZValue(7)
        self.scene.addItem(item)
        self._text_edit = item
        self._text_edit_box = None
        if width:
            # 文本框编辑态：显示可见虚线边框（随内容增长），明确"这是文本框"。
            # 用 sceneBoundingRect（含 setPos 位置），否则边框画在场景原点=屏幕左上角。
            sb = item.sceneBoundingRect()
            box = QGraphicsRectItem(sb.adjusted(-3, -3, 3, 3))
            box.setPen(QPen(QColor(120, 120, 140, 190), 1, Qt.DashLine))
            box.setBrush(Qt.NoBrush)
            box.setZValue(7)
            self.scene.addItem(box)
            self._text_edit_box = box
            try:
                item.document().contentsChanged.connect(
                    lambda: self._sync_text_box(box, item))
            except Exception:            # noqa: BLE001
                pass
        item.setFocus()                  # 光标进入，可直接输入

    def _sync_text_box(self, box, item):
        """文本框边框跟随文本内容尺寸（场景坐标，跟随 text 实际位置）。"""
        try:
            sb = item.sceneBoundingRect()
            box.setRect(QRectF(sb.x() - 3, sb.y() - 3,
                               sb.width() + 6, sb.height() + 6))
        except Exception:                # noqa: BLE001
            pass

    def _on_focus_changed(self, new, old, reason):
        # 编辑器失焦（点击其它位置/切换到别处）→ 提交
        if self._text_edit is not None and new is not self._text_edit:
            self._commit_text()

    def _commit_text(self):
        it = self._text_edit
        self._text_edit = None
        if it is None:
            return
        if self._text_edit_box is not None:
            self.scene.removeItem(self._text_edit_box)
            self._text_edit_box = None
        txt = it.toPlainText().strip()
        pos = it.pos()
        width = it.textWidth() if it.textWidth() > 0 else None
        self.scene.removeItem(it)
        if not txt:
            self._editing_index = None
            # 空文本：不更新 data，但需恢复双击编辑时隐藏的旧项可见
            for i in self._hidden_edit_items:
                try:
                    i.setVisible(True)
                except Exception:        # noqa: BLE001
                    pass
            self._hidden_edit_items = []
            return
        f = it.font()
        d = {"type": "text", "x": pos.x(), "y": pos.y(), "text": txt,
             "font_size": f.pixelSize() if f.pixelSize() > 0 else self.font_size,
             "font": f.family() or self.font_family,
             "color": _hex(QColor(*self.color)),
             "bold": f.bold(), "italic": f.italic()}
        if width:
            d["text_width"] = width
        if self._editing_index is not None:
            idx = self._editing_index
            self._editing_index = None
            self._push_cmd(_EditCmd(idx, self.data[idx], d))
            self.data[idx] = d               # 原地替换：编辑已有文字块
        else:
            self._push_cmd(_AddCmd(len(self.data), d))
            self.data.append(d)              # 新增文字块
        self._hidden_edit_items = []          # 旧项已由 _render_all 移除，清空引用
        self._render_all()

    def cancel_text_edit(self) -> bool:
        """取消进行中的文字编辑。返回是否确实取消了。"""
        if self._text_edit is None:
            return False
        if self._text_edit_box is not None:
            self.scene.removeItem(self._text_edit_box)
            self._text_edit_box = None
        self.scene.removeItem(self._text_edit)
        self._text_edit = None
        self._editing_index = None
        # 恢复双击编辑时隐藏的旧渲染项（编辑取消 → 旧内容重新显示）
        for i in self._hidden_edit_items:
            try:
                i.setVisible(True)
            except Exception:            # noqa: BLE001
                pass
        self._hidden_edit_items = []
        return True
