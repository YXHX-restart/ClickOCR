"""appmod.save_queue — 「编码 + 落库」后台队列的**主线程侧门面**。

为什么单独一层：把「后台线程 + 任务表 + 完成回填 + 收尾等待」四件事收在一处，
而不是在 App / history_mixin 上各写一半。调用方只表达一件事：
「把这台窗口存下来，存好了接着做 X」。

线程安全边界（本模块存在的意义）：
- 提交前，需要的字段由**主线程**抽成纯数据（numpy 图 + str/JSON），
  绝不把 QObject 传进线程；
- 线程内只碰 HistoryStorage 与 numpy/OpenCV（见 workers.HistorySaveWorker）；
- 完成信号回到主线程后，才去动 `ov.history_id` 与续作。

为什么异步而不是「只把编码挪走」：编码（4K PNG/webp 约 100–300ms）与落库
（写文件 + 插库 + FIFO 清理）都在 UI 线程**串行**发生，只挪一半仍要主线程
等另一半。整条链路下沉后，UI 线程只花「取快照 + 提交」的微秒级开销。
"""
from __future__ import annotations

import time

from PySide6.QtCore import QCoreApplication, QObject

from core_engine.logging_setup import get_logger
from appmod.workers import HistorySaveWorker

log = get_logger("app")


class SaveQueue(QObject):
    """后台落库队列：submit 提交、flush 收干、stop 退出收尾。"""

    def __init__(self, history, encode_pair, parent=None):
        super().__init__(parent)
        self._history = history
        self._encode_pair = encode_pair      # callable(img) -> (ok, ext, main, thumb)
        self._worker = None
        self._seq = 0
        # task_id -> (on_done, on_failed)：完成回填要回到主线程执行，
        # 故续作只在 `_on_saved` / `_on_failed` 里跑
        self._ctx: dict = {}

    # ---- 状态 ----

    @property
    def busy(self) -> bool:
        """是否还有在途任务（未回填）。"""
        return bool(self._ctx)

    def _ensure_worker(self) -> HistorySaveWorker:
        """惰性建线程：从不保存的进程（如纯只读脚本）不必起这条线程。"""
        w = self._worker
        if w is None or not w.isRunning():
            w = HistorySaveWorker(self._history)
            w.saved.connect(self._on_saved)
            w.failed.connect(self._on_failed)
            self._worker = w
            w.start()
        return w

    # ---- 提交 ----

    def submit(self, image, *, ocr_text, ocr_boxes, model_tier, device,
               name_pattern, on_done=None, on_failed=None) -> int:
        """提交一次落库，返回 task_id。

        image 是 numpy 数组（线程内只读）；其余必须都是**纯数据**。
        on_done(rid) / on_failed(msg) 在主线程、结果就绪后回调。
        """
        self._seq += 1
        tid = self._seq
        self._ctx[tid] = (on_done, on_failed)
        self._ensure_worker().submit(tid, image, {
            "encode_pair": self._encode_pair,
            "ocr_text": ocr_text,
            "ocr_boxes": ocr_boxes,
            "model_tier": model_tier,
            "device": device,
            "name_pattern": name_pattern,
        })
        return tid

    # ---- 回填（主线程）----

    def _on_saved(self, tid: int, rid: int):
        on_done, _ = self._ctx.pop(tid, (None, None))
        if on_done is None:
            return
        try:
            on_done(rid)
        except Exception:      # noqa: BLE001 单条续作失败不影响其它任务
            log.exception("保存后续动作失败（rid=%s）", rid)

    def _on_failed(self, tid: int, msg: str):
        _, on_failed = self._ctx.pop(tid, (None, None))
        log.error("后台保存失败：%s", msg)
        if on_failed is None:
            return
        try:
            on_failed(msg)
        except Exception:      # noqa: BLE001
            log.exception("保存失败回调异常")

    # ---- 收尾 ----

    def flush(self, timeout_ms: int = 5000) -> bool:
        """等待全部在途任务**完成并跑完续作**；返回是否已收干。

        退出前收尾用（不 flush 会丢掉刚截的图），测试里也用它把异步保存
        变回可断言的同步点。
        """
        if not self._ctx:
            return True
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        while self._ctx:
            if time.monotonic() > deadline:
                log.warning("保存队列收尾超时：仍有 %d 项在途", len(self._ctx))
                return False
            # 跨线程信号要经事件循环投递到主线程：只 sleep 不 processEvents
            # 会永远等不到 `_on_saved`（这就是「flush 卡死」的经典写法）
            QCoreApplication.processEvents()
            time.sleep(0.005)
        return True

    def stop(self, timeout_ms: int = 3000):
        """先收干再停线程（顺序不能反：停了线队列里的任务就没人处理了）。"""
        self.flush(timeout_ms)
        w = self._worker
        if w is not None and w.isRunning():
            w.stop()
            w.wait(timeout_ms)
        self._worker = None
