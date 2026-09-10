"""常驻工作线程：OcrWorker / TableWorker / EngineLoader（从 app.py 拆出，纯搬移）。

三者都是 QThread 子类：onnxruntime CUDA EP 每线程分配 ~188MiB thread-local 资源
且线程退出不回收，所以必须单线程常驻复用、只认最新任务，禁止每次新建。
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from ocr import RapidOcrEngine


class OcrWorker(QThread):
    """常驻 OCR 工作线程：单线程复用，只认最新一张图。

    修复「截图几次显存耗尽」：onnxruntime CUDA EP 会为每个新建线程分配
    独立 CUDA stream/workspace（实测约 ~188 MiB/线程），且线程结束后不回收。
    旧实现每次截图 new OcrWorker → 每次新线程 → 显存逐步累积直至 8G 耗尽。
    改为单线程常驻 + 任务队列：CUDA workspace 只分配一次，显存稳定；同时
    省去线程反复创建/销毁的开销，识别速度不受影响（只处理最新一张，天然
    丢弃来不及开始就绪的过期任务）。
    """
    done = Signal(int, object, list)   # (task_id, img, boxes)
    failed = Signal(int, str)          # (task_id, message)

    def __init__(self, engine: RapidOcrEngine):
        super().__init__()
        self.engine = engine
        self._cond = threading.Condition()
        self._img = None
        self._task_id = 0
        self._stopping = False

    def submit(self, task_id: int, img):
        """提交新任务；若上一任务尚未被取走则直接覆盖（只认最新一张）。"""
        with self._cond:
            self._task_id = task_id
            self._img = img
            self._cond.notify()

    def stop(self):
        with self._cond:
            self._stopping = True
            self._img = None
            self._task_id = 0
            self._cond.notify()

    def run(self):
        while True:
            with self._cond:
                while self._img is None and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
                img = self._img
                tid = self._task_id
                self._img = None
                self._task_id = 0
            try:
                boxes = self.engine.recognize(img)
                self.done.emit(tid, img, boxes)
            except Exception as e:  # noqa: BLE001
                self.failed.emit(tid, str(e) or type(e).__name__)


class TableWorker(QThread):
    """常驻表格识别线程：单线程复用（SLANet 惰性加载 + 结构识别）。

    与 OcrWorker 同理：onnxruntime CUDA EP 为每个新建线程分配 thread_local 的
    cublas/cudnn handle + CUDA stream + workspace，线程结束不回收 → 每次 new
    QThread 跑表格识别都会泄漏一批句柄/显存。改为单线程常驻复用。
    """
    done = Signal(object)          # TableResult
    failed = Signal(str)

    def __init__(self, table_engine):
        super().__init__()
        self.table_engine = table_engine
        self._cond = threading.Condition()
        self._img = None
        self._stopping = False

    def submit(self, img):
        with self._cond:
            self._img = img
            self._cond.notify()

    def stop(self):
        with self._cond:
            self._stopping = True
            self._img = None
            self._cond.notify()

    def run(self):
        while True:
            with self._cond:
                while self._img is None and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
                img = self._img
                self._img = None
            try:
                result = self.table_engine.recognize_table(img)
                self.done.emit(result)
            except Exception as e:  # noqa: BLE001
                self.failed.emit(str(e) or type(e).__name__)


class EngineLoader(QThread):
    """后台加载 OCR 模型（keep_alive=true 时启用）。

    直接在传入的引擎包装对象内构建模型（eng._ensure_engine()），
    而非新建对象——避免替换 self.engine 后，table_engine 等已绑定的
    ocr_recognize 回调指向失效的旧对象。模型构建在原生层会释放 GIL，
    故 UI 线程全程不阻塞。
    """
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, engine_ref):
        super().__init__()
        self.engine_ref = engine_ref

    def run(self):
        try:
            self.engine_ref._ensure_engine()      # 在包装对象内构建模型
            self.ready.emit(self.engine_ref)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e) or type(e).__name__)


