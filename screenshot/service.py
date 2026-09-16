"""截图服务（M1-2）—— 基于 mss 的跨平台抓屏。

- 输出 numpy BGR（与 RapidOCR 输入约定一致，无需 cv2 依赖，用 numpy 翻转通道）。
- 区域选择（Qt 橡皮筋框选）的 UI 交互在 M1-3 接入；本模块只负责"给定区域 → 像素"。
- mss 需要显示服务（Windows 桌面 / X11 / XWayland），无显示环境无法 grab。
- **线程安全**：抓屏入口自身保证线程安全（见类文档），调用方无需自律。
"""
from __future__ import annotations

import threading
import weakref

import numpy as np
import mss


class ScreenshotService:
    """抓屏服务（线程安全）。

    mss 的 Windows 后端在 `__init__` 里就 `CreateCompatibleDC` 并分配一块可复用
    的 DIB 缓冲，`grab()` 每次都往这块缓冲里写结果——**跨线程并发 grab 会互相
    踩缓冲**（表现为画面错位/花屏）。而本服务会被 `ScrollWorker`、`ManualCapture`
    这些 QThread 调用（滚动截图），因此线程安全必须由服务自身提供，而不是要求
    调用方自律。

    收敛做法：**每线程一个 mss 实例**（实例只被 threading.local 强引用，
    线程结束即由 GC 回收，mss.MSSBase.__del__ 会 close），实例创建用锁串行化
    （分配 DIB / 建 DC 的初始化过程同样不可并发）。

    实例是惰性创建的：谁先抓屏谁创建，因此主线程不抓屏时不会白占一块 DIB 缓冲。
    """

    def __init__(self):
        self._tls = threading.local()          # 每线程一个 mss 实例
        self._lock = threading.Lock()          # 串行化实例创建 / _instances 读写
        # 弱引用登记已创建的实例：close() 可统一释放，但不阻止线程结束后 GC
        # （否则每轮滚动截图的 worker 线程都会留下一块几十 MB 的 DIB 缓冲）
        self._instances = weakref.WeakSet()

    def _sct(self) -> "mss.base.MSSBase":
        """取当前线程的 mss 实例（不存在则创建）。"""
        sct = getattr(self._tls, "sct", None)
        if sct is None:
            with self._lock:
                sct = mss.mss()
                self._instances.add(sct)
            self._tls.sct = sct
        return sct

    @property
    def monitors(self):
        return self._sct().monitors

    def grab_full(self) -> np.ndarray:
        """抓取主屏全屏，返回 BGR numpy。"""
        sct = self._sct()
        mon = sct.monitors[1]
        return self._to_bgr(sct.grab(mon))

    def grab_region(self, bbox: tuple) -> np.ndarray:
        """抓取指定区域，bbox: (left, top, right, bottom)。返回 BGR numpy。

        mss.grab 要求 left/top/width/height，需把 right/bottom 换算为宽高。
        bbox 坐标为 mss 原始像素（逻辑像素 × dpr）；grab_full 的返回图也是原始
        像素，故整幅图的 bbox 就是图像尺寸本身，不要再乘 dpr。
        """
        left, top, right, bottom = bbox
        region = {
            "left": int(left),
            "top": int(top),
            "width": int(right - left),
            "height": int(bottom - top),
        }
        return self._to_bgr(self._sct().grab(region))

    def close(self):
        """释放本服务创建过的 mss 实例（幂等）。

        **不要求 App 退出时调用**：实例只被各线程的 threading.local 强引用，
        线程结束（或进程退出）即被 GC 回收，mss.MSSBase.__del__ 会自动 close，
        所以不调用也自洽。本方法只是给需要确定性释放的场景留个入口
        （例如长跑进程里手动回收 GDI 资源）。close() 之后再抓屏会按需重建实例。
        """
        with self._lock:
            instances = list(self._instances)
            self._instances = weakref.WeakSet()
        for sct in instances:
            try:
                sct.close()
            except Exception:   # noqa: BLE001  已关闭/句柄失效：忽略即可
                pass
        # 丢弃本线程的缓存引用（其余线程的实例已随上面的 close 失效，
        # 下次抓屏会为它们各自重建）
        self._tls = threading.local()

    @staticmethod
    def _to_bgr(shot) -> np.ndarray:
        # mss 的 .bgra 前 3 字节即 BGR，一次切片 + 一次拷贝完成（原实现走
        # shot.rgb 的纯 Python 通道重组 + 负步长翻转 + copy，4K 全屏 ~33MB
        # 数据被遍历约 4 遍，滚动截图每帧都走这条路）
        img = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(
            shot.height, shot.width, 4)
        return np.ascontiguousarray(img[..., :3])
