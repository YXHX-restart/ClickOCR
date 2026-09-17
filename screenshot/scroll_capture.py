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
import threading
import time
from functools import partial

import cv2
import numpy as np
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QApplication

from core_engine.logging_setup import get_logger
from platform_compat import IS_WINDOWS
from platform_compat.fonts import ui_font_family
from ui.base.texts import T

log = get_logger("scroll")

WIN = IS_WINDOWS   # 光标移动/滚轮模拟走 user32，仅 Windows 有实现

_WHEEL_DELTA = 120      # 一格滚轮
_MAX_FRAMES = 300       # 最大拼接次数兜底
_MIN_VOTES = 4          # 位移众数最少票数（低于视为不可靠）

# ---- SIFT 匹配判据（唯一阈值来源，两处筛选共用 _accept）----
# 距离为 0（两帧像素逐字节一致）→ 直接可信，保持原判据不变（零回归）。
# 否则要求距离 ≤ _MAX_MATCH_DIST **且**通过 Lowe ratio 测试。
#
# 为什么必须放宽「只能零距离」：DPI 缩放 125%/150% 下平滑滚动的位移是
# 逻辑像素 × dpr，会落在物理像素之间（亚像素），加上字体抗锯齿，两帧同一
# 特征点的描述子距离不再为 0 → 一帧都匹配不上 → 被当成「已到底」→ 长图提前截断
# （只截到第一屏）。
#
# 阈值依据（实测，cv2 5.x SIFT，128 维描述子，唯一内容的截图，
# 内容上移 44px 且叠加 0.2~0.75px 亚像素平移）：
#   平移整数      → 最小距离 0.0（原判据即可用，458 票）
#   0.2px 亚像素  → 最小距离 5.7，原判据 0 票；T=32 + ratio → 正确位移 44，6 票
#   0.375px 亚像素→ 最小距离 5.7 → 正确位移 4，6 票
#   0.75px 亚像素 → 最小距离 13.2 → 正确位移 14，2 票（低于 _MIN_VOTES，仍判为不可靠）
# 即：亚像素重采样会把最小距离抬到 5~15，取 32 是 2~3 倍余量；再往上放
# （64+）在重复内容上会引入更多噪声票，得不偿失。
# 保守取向：**宁可漏配也不要错配**——漏配只是少拼一帧（还有「连续无位移」
# 兜底结束），错配会把内容拼错、整张长图不可用。因此：
# - ratio 门限取标准 0.75（比次优明显更优才采纳），不放宽；
# - 零距离匹配不做 ratio 门控（保持原判据），避免在重复内容上反而把票数
#   压到 _MIN_VOTES 以下、重新制造「提前截断」；
# - 新放开的只有「距离 ≤32 且过 ratio」这一档，风险仅落在原本 0 票、
#   必然截断的帧上。
_MAX_MATCH_DIST = 32
_MIN_RATIO = 0.75


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

    同列（x 差 ≤1.5px）且向下移动（p1.y > p2.y）的**可信**匹配对，
    投票出纵向位移众数；返回 (位移, 图1众数匹配最大 y, 图2众数匹配最大 y)。
    """

    def __init__(self, nfeatures: int = 500):
        self.sift = cv2.SIFT_create(nfeatures=nfeatures)
        self.bf = cv2.BFMatcher()

    @staticmethod
    def _accept(m: "cv2.DMatch", n: "cv2.DMatch") -> bool:
        """knn 最优/次优对是否可信（唯一判据，阈值依据见模块顶部注释）。"""
        if m.distance == 0:              # 像素完全一致：直接可信（原判据）
            return True
        return (m.distance <= _MAX_MATCH_DIST
                and m.distance < _MIN_RATIO * n.distance)

    def match(self, g1: np.ndarray, g2: np.ndarray):
        kp1, des1 = self.sift.detectAndCompute(g1, None)
        kp2, des2 = self.sift.detectAndCompute(g2, None)
        if des1 is None or des2 is None or len(des1) < 2 or len(des2) < 2:
            return None, 0, 0
        # 筛选只做一次：投票与 m1/m2 统计共用同一批匹配对。
        # （原先两处各写一遍 `m.distance != 0` + 同列判据，改一处漏一处就会
        #   让「投票用的匹配」与「裁剪基准用的匹配」不是同一批。）
        pairs: list[tuple[float, float]] = []
        for m, n in self.bf.knnMatch(des1, des2, k=2):
            if not self._accept(m, n):
                continue
            p1 = kp1[m.queryIdx].pt
            p2 = kp2[m.trainIdx].pt
            if abs(p1[0] - p2[0]) > 1.5:      # 同列（允许亚像素偏差）
                continue
            if p1[1] <= p2[1]:                # 仅向下滚动（内容上移）
                continue
            pairs.append((p1[1], p2[1]))
        votes: dict[int, int] = {}
        for y1, y2 in pairs:
            d = int(round(y1 - y2))
            votes[d] = votes.get(d, 0) + 1
        if not votes:
            return None, 0, 0
        best_d, best_n = max(votes.items(), key=lambda kv: kv[1])
        if best_n < _MIN_VOTES:
            return None, 0, 0
        same = [p for p in pairs if int(round(p[0] - p[1])) == best_d]
        m1 = max(y1 for y1, _y2 in same)
        m2 = max(y2 for _y1, y2 in same)
        return best_d, int(m1), int(m2)


class SiftStitcher:
    """SIFT 拼接状态：预分配缓冲 + 行游标追加（摊销 O(N)，不再每帧全量拷贝）。

    原实现每帧 np.vstack([canvas[:cut], bottom]) 分配新数组并复制整个旧画布，
    长图（数百帧 × 上万行）为 O(N²) 累计拷贝，越拼越慢；改为缓冲内原地写：
    前缀不动、bottom 写入 cut 之后，容量不足时几何扩容（总拷贝量摊销 O(N)）。
    """

    def __init__(self):
        self.matcher = SiftMatcher()
        self._buf: np.ndarray | None = None   # 预分配画布缓冲
        self._h = 0                           # 画布有效行数（行游标）
        self.last_gray: np.ndarray | None = None

    @property
    def canvas(self) -> np.ndarray | None:
        """当前画布 = 缓冲的有效前缀视图（首轴切片，内存连续）。"""
        return None if self._buf is None else self._buf[:self._h]

    def first(self, frame: np.ndarray):
        cap = max(2 * frame.shape[0], 256)
        self._buf = np.zeros((cap,) + frame.shape[1:], dtype=frame.dtype)
        self._buf[:frame.shape[0]] = frame
        self._h = frame.shape[0]
        self.last_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    def _reserve(self, total_rows: int):
        """确保缓冲容量 ≥ total_rows；不足则几何扩容并搬移有效前缀。

        仅在 add() 内调用（_buf 必已由 first() 分配）。
        """
        if total_rows <= self._buf.shape[0]:
            return
        cap = self._buf.shape[0]
        while cap < total_rows:
            cap *= 2
        grown = np.zeros((cap,) + self._buf.shape[1:], dtype=self._buf.dtype)
        grown[:self._h] = self._buf[:self._h]
        self._buf = grown

    def result(self) -> np.ndarray | None:
        """最终结果：精确尺寸的独立副本（释放几何扩容预留的富余缓冲）。"""
        return None if self._buf is None else self._buf[:self._h].copy()

    def add(self, frame: np.ndarray) -> bool:
        """合并新帧；无位移/不可靠/空白帧返回 False。"""
        if self._buf is None:
            self.first(frame)
            return True
        gray2 = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        d, m1, m2 = self.matcher.match(self.last_gray, gray2)
        if d is None or d < 3:
            return False                      # 无位移 / 微小抖动不算移动
        h1, h2 = self._h, frame.shape[0]
        # roll_screenshot 裁剪公式：画布裁到 m1 对应的内容点，新帧从 m2 接续
        cut = h1 - (h2 - m1)
        if not (4 < cut < h1) or not (2 < m2 < h2):
            return False
        bottom = frame[m2:]
        if bottom.shape[0] < 2:
            return False
        # 原地拼接：前缀 buf[:cut] 不动，bottom 写入 cut 之后（无全量拷贝）
        new_h = cut + bottom.shape[0]
        self._reserve(new_h)
        self._buf[cut:new_h] = bottom
        self._h = new_h
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
            # result()：精确尺寸副本，避免信号接收方长期持有扩容预留的富余缓冲
            self.done.emit(self.stitcher.result())
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
        f = QFont(ui_font_family())
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


class _MouseListenerThread(QThread):
    """pynput 全局鼠标监听线程基类（ModePicker / ManualCapture / StopListener 共用）。

    收敛点：三个监听线程原先各自 `with mouse.Listener(...) as lis: lis.join()`，
    都**没有对外停止入口**——监听只能靠回调 `return False` 自杀，于是 Esc 之后
    必须再点一次鼠标才退出、残留 QThread 被下一次会话赋值覆盖后失去引用
    → GC 回收正在运行的 QThread → Qt 抛 "Destroyed while thread is still running"。
    本基类统一：run() 持有 Listener 引用，stop() 可被收尾函数从任意线程调用
    （pynput 明确支持从外部 stop），并消除「stop 先于 Listener 创建」的竞态。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lis = None
        self._lis_lock = threading.Lock()    # 与 _stopped 配合消除创建/停止竞态
        self._stopped = threading.Event()

    def stop(self) -> None:
        """外部停止监听（幂等，可从任意线程调用）。"""
        self._stopped.set()
        with self._lis_lock:
            lis = self._lis
        if lis is not None:
            try:
                lis.stop()
            except Exception:       # noqa: BLE001  已停/句柄失效：忽略
                pass

    def _callbacks(self) -> dict:
        """子类返回 mouse.Listener 的回调字典。"""
        raise NotImplementedError

    def run(self):
        from pynput import mouse
        # 建 + 判 + start 都在锁内完成：stop() 要拿同一把锁读 _lis，因此 stop()
        # 不可能看到「已创建但未 start」的 Listener（那种实例 stop() 是空操作，
        # 会让监听线程永远活着）。
        with self._lis_lock:
            if self._stopped.is_set():
                return
            self._lis = mouse.Listener(**self._callbacks())
            self._lis.start()
        try:
            self._lis.join()
        finally:
            with self._lis_lock:
                self._lis = None


class ModePicker(_MouseListenerThread):
    """三选监听（pynput 全局鼠标）：左键 = 自动；右键 = 退出；滚轮 = 手动。

    刻意**不校验点击是否在选区内**——pynput 报告物理坐标，多显示器/DPI
    缩放下与业务坐标体系可能不一致，按区域判断会导致模式永远选不上
    （遮罩一直挂着）；遮罩仅作视觉参考，按任意位置即可。
    """

    chosen = Signal(str)       # "auto" / "manual"
    cancelled = Signal()

    def _callbacks(self) -> dict:
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

        return {"on_click": on_click, "on_scroll": on_scroll}


class ManualCapture(_MouseListenerThread):
    """手动模式抓帧：pynput 监听滚轮（下滚，节流 0.35s）抓帧推给 worker；
    任意点击结束（roll_screenshot 同款）。"""

    def __init__(self, svc, raw, worker: ScrollWorker, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.raw = raw
        self.worker = worker
        self._last = 0.0

    def _callbacks(self) -> dict:
        def on_scroll(x, y, _dx, dy):
            if self.worker.stop.is_set():
                return False                 # 已收尾：停监听，不再抓帧
            if dy >= 0:                      # 只响应下滚
                return
            now = time.time()
            if now - self._last < 0.35:
                return
            self._last = now
            try:
                frame = self.svc.grab_region(self.raw)
            except Exception:                # noqa: BLE001
                return
            if not self.worker.stop.is_set():
                # 抓帧期间可能已收尾（Esc/完成）→ 不再往**无人消费**的队列推帧
                # （原实现收尾后仍继续 push，白抓 + 队列无限堆积）
                self.worker.push_frame(frame)

        def on_click(x, y, button, pressed):
            if pressed:
                self.worker.finish()          # 任意点击结束
                return False

        return {"on_click": on_click, "on_scroll": on_scroll}


class StopListener(_MouseListenerThread):
    """自动模式：任意点击停止（roll_screenshot 同款）。"""

    def __init__(self, worker: ScrollWorker, parent=None):
        super().__init__(parent)
        self.worker = worker

    def _callbacks(self) -> dict:
        def on_click(x, y, button, pressed):
            if pressed:
                self.worker.finish()
                return False

        return {"on_click": on_click}


# ---- 滚动截图收尾（自动/手动两条路径共用的唯一实现）----

# 收尾时要处理的线程属性（CaptureMixin 上的引用名）
_SCROLL_THREAD_ATTRS = ("_manual_cap", "_stop_listener", "_scroll_picker",
                        "_scroll_worker")

# 超时仍未结束的线程：转由模块级强引用表持有，直到它真正结束。
# （清引用不能以丢掉唯一引用为代价：QThread 被 GC 回收时若仍在运行，Qt 直接崩）
_lingering: set = set()


def _retain_until_finished(t) -> None:
    if t is None or not t.isRunning():
        return
    _lingering.add(t)
    t.finished.connect(partial(_lingering.discard, t))


def teardown_scroll(host, timeout_s: float = 1.0) -> None:
    """滚动截图统一收尾：置 stop → 停 pynput 监听 → 等线程结束 → 清引用。

    **唯一收尾入口**，自动/手动两条路径共用（调用点见 CaptureMixin）。
    原先收尾散在五处各写一半，于是：
    - Esc 只 `stop.set()`：pynput 全局监听要「再点一次鼠标」才退出；
    - 手动模式 Esc 后 ManualCapture 继续监听滚轮，把帧推进**无人消费**的队列；
    - host._scroll_worker / _manual_cap / _stop_listener 被下次会话赋值覆盖后，
      残留 QThread 失去引用 → GC 回收运行中的线程 → Qt 崩
      （"Destroyed while thread is still running"）。

    timeout_s 是**总**等待预算：收尾跑在 UI 线程上，不能长时间卡界面；
    先 stop() 再 wait()，正常情况下线程立即返回，超时只是兜底。
    """
    worker = getattr(host, "_scroll_worker", None)
    if worker is not None:
        try:
            worker.finish()                  # 置 stop：主循环下一轮退出
        except Exception:                    # noqa: BLE001
            pass
    threads = [(attr, getattr(host, attr, None)) for attr in _SCROLL_THREAD_ATTRS]
    for _attr, t in threads:
        stop_fn = getattr(t, "stop", None) if t is not None else None
        if callable(stop_fn):
            try:
                stop_fn()                    # 主动停 pynput 监听（原先只能等回调自杀）
            except Exception:                # noqa: BLE001
                log.warning("停止滚动截图监听线程失败（继续收尾）", exc_info=True)
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    for attr, t in threads:
        if t is None:
            continue
        try:
            if t.isRunning():
                t.wait(max(0, int((deadline - time.monotonic()) * 1000)))
                if t.isRunning():
                    log.warning("滚动截图线程 %s 未在收尾超时内结束，"
                                "暂由强引用表持有直至其结束", attr)
        except RuntimeError:                 # 底层对象已销毁
            pass
        _retain_until_finished(t)            # 仍在跑的：先保住引用再清属性
        try:
            setattr(host, attr, None)        # 清引用，避免下次会话覆盖后 GC 崩溃
        except Exception:                    # noqa: BLE001
            pass


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
        self.lbl = QLabel(T("strip.scroll_running", "滚动截图中…"))
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
            T("strip.scroll_progress",
              "滚动截图中… 已拼 {n} 帧 / 高 {h}px · "
              "Enter = 完成 · Esc = 退出 · 点击停止").format(
                  n=frames, h=height))

    def show_center_bottom(self):
        self.adjustSize()
        # 取**光标所在屏**，不能固定主屏：多显示器下进度条会被丢到主屏，
        # 而用户正在副屏上看滚动拼接结果。取不到再回退主屏。
        from PySide6.QtGui import QCursor
        scr_obj = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if scr_obj is not None:                 # 无屏环境（离屏/异常）不移动
            scr = scr_obj.availableGeometry()
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
