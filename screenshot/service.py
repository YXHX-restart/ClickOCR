"""截图服务（M1-2）—— 基于 mss 的跨平台抓屏。

- 输出 numpy BGR（与 RapidOCR 输入约定一致，无需 cv2 依赖，用 numpy 翻转通道）。
- 区域选择（Qt 橡皮筋框选）的 UI 交互在 M1-3 接入；本模块只负责"给定区域 → 像素"。
- mss 需要显示服务（Windows 桌面 / X11 / XWayland），无显示环境无法 grab。
"""
from __future__ import annotations

import numpy as np
import mss


class ScreenshotService:
    def __init__(self):
        self._sct = mss.mss()

    @property
    def monitors(self):
        return self._sct.monitors

    def grab_full(self) -> np.ndarray:
        """抓取主屏全屏，返回 BGR numpy。"""
        mon = self._sct.monitors[1]
        return self._to_bgr(self._sct.grab(mon))

    def grab_region(self, bbox: tuple) -> np.ndarray:
        """抓取指定区域，bbox: (left, top, right, bottom)。返回 BGR numpy。

        mss.grab 要求 left/top/width/height，需把 right/bottom 换算为宽高。
        bbox 坐标为 mss 原始像素（逻辑像素 × dpr）。
        """
        left, top, right, bottom = bbox
        region = {
            "left": int(left),
            "top": int(top),
            "width": int(right - left),
            "height": int(bottom - top),
        }
        return self._to_bgr(self._sct.grab(region))

    @staticmethod
    def _to_bgr(shot) -> np.ndarray:
        img = np.frombuffer(shot.rgb, dtype=np.uint8).reshape(shot.height, shot.width, 3)
        return img[..., ::-1].copy()  # RGB -> BGR
