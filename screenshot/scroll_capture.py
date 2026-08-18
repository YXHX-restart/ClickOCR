"""screenshot.scroll_capture — 滚动截图（截长图，借鉴 roll_screenshot 思路）。

流程（R 键滚动模式下框选区域后）：
- 全屏三选遮罩：**左键 = 自动滚动**（程序移光标到选区中心模拟滚轮）；
  **右键 = 退出**；**直接滚轮 = 手动滚动**（用户滚一下抓一帧）；
- 拼接：**SIFT 特征点匹配**——同列特征点对投票出纵向位移众数，
  按众数位移裁剪拼接；空白区域无特征点天然跳过，重复内容靠众数投票抗错；
  连续 3 帧无位移 = 到底自动结束；
- 进度牌：Enter/单击 = 完成贴图，Esc = 退出不贴图；手动模式下任意点击结束。
"""
from __future__ import annotations

import ctypes
import queue
import sys
import threading
import time

import cv2
import numpy as np
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QApplication

WIN = sys.platform.startswith("win")

_WHEEL_DELTA = 120      # 一格滚轮
_MAX_FRAMES = 300       # 最大拼接次数兜底
_MIN_VOTES = 4          # 位移众数最少票数（低于视为不可靠）


def _set_cursor(x: int, y: int):
    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))


def _get_cursor():
    user32 = ctypes.windll.user32

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _wheel_down(clicks: int = 1):
    """向光标下的窗口发送滚轮下滚（MOUSEEVENTF_WHEEL，负 delta = 向下）。"""
    user32 = ctypes.windll.user32
    for _ in range(max(1, clicks)):
        user32.mouse_event(0x0800, 0, 0, -_WHEEL_DELTA, 0)
        time.sleep(0.04)


def _frame_diff(a: np.ndarray, b: np.ndarray) -> float:
    """两帧平均绝对差（内容完全没变 → 0，用于判定画面静止/到底）。"""
    if a is None or a.shape != b.shape:
        return 1e9
    return float(np.mean(cv2.absdiff(a, b)))


class SiftMatcher:
    """SIFT 特征匹配（roll_screenshot 同款思路，OpenCV 5 内置 SIFT）。

    同列（x 差 ≤1.5px）且向下移动（p1.y > p2.y）的零距离匹配对，
    投票出纵向位移众数；返回 (位移, 图1众数匹配最大 y, 图2众数匹配最大 y)。
    """

    def __init__(self, nfeatures: int = 500):
        self.sift = cv2.SIFT_create(nfeatures=nfeatures)
        self.bf = cv2.BFMatcher()

    def match(self, g1: np.ndarray, g2: np.ndarray):
        kp1, des1 = self.sift.detectAndCompute(g1, None)
        kp2, des2 = self.sift.detectAndCompute(g2, None)
        if des1 is None or des2 is None or len(des1) < 2 or len(des2) < 2:
            return None, 0, 0
        matches = self.bf.knnMatch(des1, des2, k=2)
        votes: dict[int, int] = {}
        for m, _n in matches:
            if m.distance != 0:               # 原始抓帧像素完全一致，零距离才可信
                continue
            p1 = kp1[m.queryIdx].pt
            p2 = kp2[m.trainIdx].pt
            if abs(p1[0] - p2[0]) > 1.5:      # 同列（允许亚像素偏差）
                continue
            if p1[1] <= p2[1]:                # 仅向下滚动（内容上移）
                continue
            d = int(round(p1[1] - p2[1]))
            votes[d] = votes.get(d, 0) + 1
        if not votes:
            return None, 0, 0
        best_d, best_n = max(votes.items(), key=lambda kv: kv[1])
        if best_n < _MIN_VOTES:
            return None, 0, 0
        m1 = m2 = 0.0
        for m, _n in matches:
            if m.distance != 0:
                continue
            p1 = kp1[m.queryIdx].pt
            p2 = kp2[m.trainIdx].pt
            if abs(p1[0] - p2[0]) <= 1.5 \
                    and int(round(p1[1] - p2[1])) == best_d:
                m1 = max(m1, p1[1])
                m2 = max(m2, p2[1])
        return best_d, int(m1), int(m2)


class SiftStitcher:
    """SIFT 拼接状态：画布 + 上一帧灰度；新帧按众数位移裁剪拼接。"""

    def __init__(self):
        self.matcher = SiftMatcher()
        self.canvas: np.ndarray | None = None
        self.last_gray: np.ndarray | None = None

    def first(self, frame: np.ndarray):
        self.canvas = frame.copy()
        self.last_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    def add(self, frame: np.ndarray) -> bool:
        """合并新帧；无位移/不可靠/空白帧返回 False。"""
        if self.canvas is None:
            self.first(frame)
            return True
        gray2 = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        d, m1, m2 = self.matcher.match(self.last_gray, gray2)
        if d is None or d < 3:
            return False                      # 无位移 / 微小抖动不算移动
        h1, h2 = self.canvas.shape[0], frame.shape[0]
        # roll_screenshot 裁剪公式：画布裁到 m1 对应的内容点，新帧从 m2 接续
        cut = h1 - (h2 - m1)
        if not (4 < cut < h1) or not (2 < m2 < h2):
            return False
        bottom = frame[m2:]
        if bottom.shape[0] < 2:
            return False
        self.canvas = np.vstack([self.canvas[:cut], bottom])
        self.last_gray = gray2
        return True


class ScrollWorker(QThread):
    """滚动截图工作线程。

    mode="auto"：自动滚轮抓帧合并；mode="manual"：外部（滚轮监听）推帧，
    本线程只消费合并。停止由 finish()（事件）或连续 3 帧无位移触发。
    """

    progress = Signal(int, int)        # (帧数, 总高)
    done = Signal(object)              # 拼接完成图（BGR ndarray）
    failed = Signal(str)

    def __init__(self, svc, raw, mode: str = "manual", speed: int = 2,
                 max_height: int = 12000, stop=None, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.raw = raw
        self.mode = mode if mode in ("auto", "manual") else "manual"
        self.speed = max(1, min(10, int(speed)))    # 自动模式每秒帧数
        self.max_height = max(800, int(max_height))
        self.stop = stop or threading.Event()
        self.stitcher = SiftStitcher()
        self.queue: queue.Queue = queue.Queue()

    def push_frame(self, frame):
        """手动模式：滚轮监听线程抓帧推入（线程安全）。"""
        self.queue.put(frame)

    def finish(self):
        self.stop.set()

    def _emit(self, frames: int):
        self.progress.emit(frames, self.stitcher.canvas.shape[0])
        return frames

    def _run_auto(self):
        cx = (self.raw[0] + self.raw[2]) // 2
        cy = (self.raw[1] + self.raw[3]) // 2
        old_cur = _get_cursor()
        frames = 0
        same = 0
        prev = None
        still = 0
        no_grow = 0
        last_h = 0
        try:
            _set_cursor(cx, cy)
            time.sleep(0.2)
            while not self.stop.is_set():
                try:
                    frame = self.svc.grab_region(self.raw)
                except Exception as e:  # noqa: BLE001
                    self.failed.emit(f"抓帧失败：{e}")
                    return
                # 连续两帧画面完全一致 = 滚不动了（聊天等到底后元素仍轻微
                # 动画时 SIFT 可能给出微小位移，用帧一致判定兜底）
                if prev is not None and _frame_diff(prev, frame) < 1.0:
                    still += 1
                    if still >= 2 and frames > 0:
                        break
                else:
                    still = 0
                prev = frame
                before = self.stitcher.canvas.shape[0] \
                    if self.stitcher.canvas is not None else 0
                if self.stitcher.add(frame):
                    frames += 1
                    self._emit(frames)
                    same = 0
                else:
                    same += 1
                    if same >= 3 and frames > 0:
                        break                  # 到底（连续无位移）
                # 合并成功但长图几乎不增长（抖动/重复内容）连续 4 次 → 结束
                grown = self.stitcher.canvas.shape[0] - last_h
                last_h = self.stitcher.canvas.shape[0]
                if grown < 4:
                    no_grow += 1
                    if no_grow >= 4 and frames > 0:
                        break
                else:
                    no_grow = 0
                if self.stitcher.canvas.shape[0] >= self.max_height \
                        or frames >= _MAX_FRAMES:
                    break
                _wheel_down(1)
                time.sleep(1.0 / self.speed)
        finally:
            _set_cursor(*old_cur)

    def _run_manual(self):
        frames = 0
        while not self.stop.is_set():
            try:
                frame = self.queue.get(timeout=0.3)
            except queue.Empty:
                continue
            if self.stitcher.add(frame):
                frames += 1
                self._emit(frames)
            if self.stitcher.canvas.shape[0] >= self.max_height \
                    or frames >= _MAX_FRAMES:
                break
        # 停止后清空已抓未拼的剩余帧（点击结束的瞬间滚轮事件可能还在路上）
        while True:
            try:
                frame = self.queue.get_nowait()
            except queue.Empty:
                break
            if self.stitcher.add(frame):
                frames += 1
                self._emit(frames)

    def run(self):
        if not WIN:
            self.failed.emit("滚动截图仅支持 Windows")
            return
        try:
            if self.mode == "auto":
                self._run_auto()
            else:
                self._run_manual()
            if self.stitcher.canvas is None:
                self.failed.emit("未抓到任何画面")
                return
            self.done.emit(self.stitcher.canvas)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class ModeMask(QWidget):
    """三选遮罩（照搬 roll_screenshot roller_mask）：全屏透明，
    选区外压暗 + 红框 + 操作提示；鼠标事件全部穿透（点击/滚轮落到目标窗口）。"""

    def __init__(self, raw, dpr: float = 1.0):
        super().__init__()
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool)
        scr = QApplication.primaryScreen().geometry()
        self.setGeometry(scr)
        self._rect = QtCore_rect(raw, dpr)
        self.setStyleSheet("background: transparent;")

    def paintEvent(self, e):
        from PySide6.QtGui import QPainter, QColor, QPen, QFont
        p = QPainter(self)
        r = self._rect
        p.fillRect(0, 0, self.width(), r.top(),
                   QColor(0, 0, 0, 120))
        p.fillRect(0, r.top(), r.left(), r.height(),
                   QColor(0, 0, 0, 120))
        p.fillRect(r.right() + 1, r.top(),
                   self.width() - r.right() - 1, r.height(),
                   QColor(0, 0, 0, 120))
        p.fillRect(0, r.bottom() + 1, self.width(),
                   self.height() - r.bottom() - 1, QColor(0, 0, 0, 120))
        p.setPen(QPen(QColor(255, 80, 80), 2, Qt.SolidLine))
        p.drawRect(r)
        tip = "左键：自动滚动    右键：退出    滚轮：手动滚动"
        f = QFont("Microsoft YaHei")
        f.setPixelSize(14)
        p.setFont(f)
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(tip) + 20
        th = fm.height() + 10
        lx = max(2, r.left() + (r.width() - tw) // 2)
        ly = max(2, r.top() - th - 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(28, 28, 30, 235))
        p.drawRoundedRect(lx, ly, tw, th, 8, 8)
        p.setPen(QColor(138, 180, 255))
        p.drawText(lx + 10, ly + 5, tw - 20, th - 10,
                   Qt.AlignCenter, tip)
        p.end()


def QtCore_rect(raw, dpr):
    from PySide6.QtCore import QRect
    d = dpr or 1.0
    return QRect(int(raw[0] / d), int(raw[1] / d),
                 int((raw[2] - raw[0]) / d), int((raw[3] - raw[1]) / d))


class ModePicker(QThread):
    """三选监听（pynput 全局鼠标）：左键 = 自动；右键 = 退出；滚轮 = 手动。

    刻意**不校验点击是否在选区内**——pynput 报告物理坐标，多显示器/DPI
    缩放下与业务坐标体系可能不一致，按区域判断会导致模式永远选不上
    （遮罩一直挂着）；遮罩仅作视觉参考，按任意位置即可。
    """

    chosen = Signal(str)       # "auto" / "manual"
    cancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self):
        from pynput import mouse

        def on_click(x, y, button, pressed):
            if not pressed:
                return
            if button == mouse.Button.right:
                self.cancelled.emit()
                return False                 # 停止监听
            if button == mouse.Button.left:
                self.chosen.emit("auto")
                return False

        def on_scroll(x, y, _dx, _dy):
            self.chosen.emit("manual")
            return False

        with mouse.Listener(on_click=on_click,
                            on_scroll=on_scroll) as lis:
            lis.join()


class ManualCapture(QThread):
    """手动模式抓帧：pynput 监听滚轮（下滚，节流 0.35s）抓帧推给 worker；
    任意点击结束（roll_screenshot 同款）。"""

    def __init__(self, svc, raw, worker: ScrollWorker, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.raw = raw
        self.worker = worker
        self._last = 0.0

    def run(self):
        from pynput import mouse

        def on_scroll(x, y, _dx, dy):
            if dy >= 0:                       # 只响应下滚
                return
            now = time.time()
            if now - self._last < 0.35:
                return
            self._last = now
            try:
                frame = self.svc.grab_region(self.raw)
            except Exception:                 # noqa: BLE001
                return
            self.worker.push_frame(frame)

        def on_click(x, y, button, pressed):
            if pressed:
                self.worker.finish()          # 任意点击结束
                return False

        with mouse.Listener(on_click=on_click,
                            on_scroll=on_scroll) as lis:
            lis.join()


class StopListener(QThread):
    """自动模式：任意点击停止（roll_screenshot 同款）。"""

    def __init__(self, worker: ScrollWorker, parent=None):
        super().__init__(parent)
        self.worker = worker

    def run(self):
        from pynput import mouse

        def on_click(x, y, button, pressed):
            if pressed:
                self.worker.finish()
                return False

        with mouse.Listener(on_click=on_click) as lis:
            lis.join()


class ScrollBadge(QWidget):
    """滚动截图进度牌：仅文字提示（帧数/高度）。

    Enter / 单击牌面 = 完成（贴图）；Esc = 退出（不贴图）。
    """

    stop_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)
        self.lbl = QLabel("滚动截图中…")
        self.lbl.setStyleSheet(
            "QLabel { background: rgba(28,28,30,235); color: #8ab4ff;"
            " border: 1px solid rgba(138,180,255,120); border-radius: 8px;"
            " padding: 8px 16px; font-size: 13px; }")
        root.addWidget(self.lbl)
        self._press = None

    def _stop(self):
        self.stop_requested.emit()
        self.close()

    def _cancel(self):
        self.cancel_requested.emit()
        self.close()

    def set_progress(self, frames: int, height: int):
        self.lbl.setText(
            f"滚动截图中… 已拼 {frames} 帧 / 高 {height}px · "
            f"Enter = 完成 · Esc = 退出 · 点击停止")

    def show_center_bottom(self):
        self.adjustSize()
        scr = QApplication.primaryScreen().availableGeometry()
        self.move(scr.center().x() - self.width() // 2,
                  scr.bottom() - self.height() - 40)
        self.show()
        self.raise_()
        self.activateWindow()        # Enter/Esc 需要焦点

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._cancel()           # Esc：退出滚动截图（不贴图）
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._stop()             # Enter：直接完成（立即贴图）
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):
        self._press = e.globalPosition().toPoint()

    def mouseReleaseEvent(self, e):
        if self._press is not None \
                and (e.globalPosition().toPoint() - self._press).manhattanLength() < 6:
            self._stop()             # 单击牌面 = 完成
        self._press = None
