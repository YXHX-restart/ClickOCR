"""ui.components.anno.items — 标注图元：数据类 + 工厂 + 离屏渲染 + 几何辅助。

从 ui/components/annotation.py 拆出（2026-09-11 重构 S1），纯搬移不改行为。
对外名字由 ui.components.annotation 再导出，调用方无需改动。
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

from platform_compat.fonts import ui_font_family

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
        f = QFont(d.get("font") or ui_font_family())
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
        f = QFont(d.get("font") or ui_font_family())
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


