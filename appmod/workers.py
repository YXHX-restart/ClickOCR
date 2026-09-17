"""常驻/一次性工作线程：OcrWorker / TableWorker / EngineLoader / DropImportWorker。

前三个是常驻线程：onnxruntime CUDA EP 每线程分配 ~188MiB thread-local 资源
且线程退出不回收，所以必须单线程常驻复用、只认最新任务，禁止每次新建。
DropImportWorker 是一次性批处理线程（导入完成后即结束，不涉及 ORT 会话）。
HistorySaveWorker 是常驻 FIFO 线程（见其 docstring 里「为什么不能每次新建」）。
"""
from __future__ import annotations

import threading

import numpy as np
from PySide6.QtCore import QThread, Signal

from core_engine.logging_setup import get_logger
from ocr import RapidOcrEngine

log = get_logger("app")


def _release_thread_db(history) -> None:
    """QThread 收尾时释放本线程的 DB 连接。

    **不调就是泄漏**：`threading.local` 的按线程数据由 `threading` 的线程收尾
    钩子清理，而 QThread 是原生线程、不走那套钩子 → 线程跑完后 `_tls` 里那条
    sqlite 连接一直留到进程退出，文件（含 -wal/-shm）被持续占用。
    详见 DBManager.close_thread_conn 的说明。
    """
    db = getattr(history, "db", None)
    if db is None:
        return
    try:
        db.close_thread_conn()
    except Exception:            # noqa: BLE001 收尾失败不该影响线程退出
        log.exception("释放线程 DB 连接失败")


class HistorySaveWorker(QThread):
    """常驻「编码 + 落库」线程：把截图/收藏/标注路径的保存移出 UI 线程。

    **为什么必须常驻**（而不是每存一张 new 一个 QThread）：
    `DBManager` 的连接是 `threading.local` 的 —— 线程结束、对象销毁才关闭
    那条 sqlite 连接。每张图新建线程 = 每次都开一条新连接再丢弃，同时反复
    创建/销毁 OS 线程；常驻单线程只维持一条稳定连接。

    **任务不合并**（与 OcrWorker「只认最新一张」相反）：每张截图都是一条独立
    历史记录，必须逐条按提交顺序落库，否则顺序与 id 递增都会乱。

    线程内只碰 `HistoryStorage`（自带进程内锁，见 DBManager 类文档）与
    numpy/OpenCV，**不触碰任何 Qt 对象**；需要在提交前由主线程抽成纯数据。
    """

    saved = Signal(int, int)      # (task_id, rid)
    failed = Signal(int, str)     # (task_id, message)

    def __init__(self, history):
        super().__init__()
        self.history = history
        self._cond = threading.Condition()
        self._queue: list = []
        self._stopping = False

    def submit(self, task_id: int, image, payload: dict):
        """入队一次落库任务（FIFO）。image 为 numpy 数组，线程内只读。"""
        with self._cond:
            self._queue.append((task_id, image, payload))
            self._cond.notify()

    @property
    def pending(self) -> int:
        """尚未取走的任务数（诊断用）。"""
        with self._cond:
            return len(self._queue)

    def stop(self):
        """请求结束：已入队的任务**不再处理**由调用方先行 flush（见 SaveQueue.stop）。"""
        with self._cond:
            self._stopping = True
            self._cond.notify()

    def run(self):
        try:
            while True:
                with self._cond:
                    while not self._queue and not self._stopping:
                        self._cond.wait()
                    if self._stopping:
                        return
                    task_id, image, payload = self._queue.pop(0)
                try:
                    rid = self._run_one(image, payload)
                except Exception as e:  # noqa: BLE001 单条失败不拖垮常驻线程
                    log.exception("后台保存失败（task=%s）", task_id)
                    self.failed.emit(task_id, str(e) or type(e).__name__)
                    continue
                self.saved.emit(task_id, rid)
        finally:
            # 线程退出前放掉本线程的 sqlite 连接（QThread 不会自动释放，见
            # _release_thread_db）——常驻线程只在退出时做一次
            _release_thread_db(self.history)

    def _run_one(self, image, payload: dict) -> int:
        """编码主图 + 缩略图 → 落库。返回新记录 id。"""
        ok, ext, image_bytes, thumb_bytes = payload["encode_pair"](image)
        if not ok or image_bytes is None:
            raise RuntimeError("图片编码失败")
        return self.history.save(
            image_bytes=image_bytes, thumb_bytes=thumb_bytes,
            ocr_text=payload["ocr_text"], ocr_boxes=payload["ocr_boxes"],
            model_tier=payload["model_tier"], device=payload["device"],
            ext=ext, name_pattern=payload["name_pattern"])


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


class ModelSwitchWorker(QThread):
    """模型档位热切换线程（一次性）。

    switch_model 会丢弃并重建整个 RapidOCR 引擎（首次约 1-2s），在 UI 线程
    同步执行会冻结界面。引擎内部已用双检锁把「构建/切换」与「识别」串行化，
    故此处只需把耗时操作移出主线程。
    """
    done = Signal(str)      # 切换完成（新档位）
    failed = Signal(str)

    def __init__(self, engine_ref, model_set: str):
        super().__init__()
        self.engine_ref = engine_ref
        self.model_set = model_set

    def run(self):
        try:
            self.engine_ref.switch_model(self.model_set)
            self.done.emit(self.model_set)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e) or type(e).__name__)


class GpuProbeWorker(QThread):
    """GPU 探测线程（一次性）。

    `probe_dml_devices` / `probe_cuda_devices` 要建 CPU 基准 session 再逐设备
    建 session 预热计时，实测 1~4 秒。原先在「检测」按钮回调里同步跑，靠
    `QApplication.processEvents()` 假装不卡 —— 那只是把卡顿摊成一串重入点，
    期间用户能再点按钮、能关窗口，重入路径没有任何保护。现整体下沉。

    探测函数是纯计算（只碰 onnxruntime 与本地模型文件），不触碰 Qt。
    """
    done = Signal(object)      # list[dict]
    failed = Signal(str)

    def __init__(self, probe_fn, det_path=None, parent=None):
        super().__init__(parent)
        self._probe_fn = probe_fn
        self._det_path = det_path

    def run(self):
        try:
            self.done.emit(self._probe_fn(self._det_path))
        except Exception as e:  # noqa: BLE001 单次探测失败不该结束进程
            log.exception("GPU 探测失败")
            self.failed.emit(str(e) or type(e).__name__)


class HistoryClearWorker(QThread):
    """清空历史线程（一次性）：删除 DB 记录 + 磁盘截图文件。

    条数上限默认 50、实测可达数千 —— 「3000 条记录 + 逐个删文件」同步跑在
    UI 线程会让设置页整个冻住（用户以为程序死了）。这里只碰 HistoryStorage
    与文件系统，不触碰 Qt。
    """
    done = Signal(int)         # 清掉的条数
    failed = Signal(str)

    def __init__(self, history, keep_favorites: bool = True, parent=None):
        super().__init__(parent)
        self._history = history
        self._keep = keep_favorites

    def run(self):
        try:
            self.done.emit(int(self._history.clear(keep_favorites=self._keep)))
        except Exception as e:  # noqa: BLE001
            log.exception("清空历史失败")
            self.failed.emit(str(e) or type(e).__name__)
        finally:
            _release_thread_db(self._history)   # QThread 不会自动释放（见该函数）


class DropImportWorker(QThread):
    """拖放导入工作线程：读文件 → 解码 → 编码 → 落库（全程后台）。

    原实现整条链路在 UI 线程同步执行（单张 200-500ms，50 张即 10-25s 界面
    完全无响应、且无进度反馈）。本线程只访问 HistoryStorage（DBManager 自带
    进程内锁，线程安全）与文件系统，**不触碰任何 Qt 控件**。

    完成信号携带 [(rid, 原图绝对路径)]：队列只存路径而非解码后的全尺寸
    ndarray——原先 50 张 4K 图常驻内存约 1.2GB，现在按需单张解码。
    """
    progressed = Signal(int, int)       # (已处理张数, 总数)
    finished_ok = Signal(object, int)   # ([(rid, 原图路径)], 置顶条数)
    failed = Signal(str)

    def __init__(self, history, paths, dup_index, encode_pair, find_dup,
                 decode=None, name_pattern: str = "", parent=None):
        super().__init__(parent)
        self._history = history
        self._paths = list(paths)
        self._dup_index = dup_index
        self._encode_pair = encode_pair     # callable(img) -> (ok, ext, main, thumb)
        self._find_dup = find_dup           # callable(path, size, index) -> rid|None
        self._decode = decode               # callable(raw_bytes) -> ndarray|None
        self._name_pattern = name_pattern
        self._stopping = False

    def stop(self):
        """请求尽早结束（当前张处理完后退出，已导入的记录保持有效）。"""
        self._stopping = True

    def run(self):
        added, touched = [], 0
        total = len(self._paths)
        try:
            for i, p in enumerate(self._paths, start=1):
                if self._stopping:
                    break
                try:
                    rid, was_touch = self._import_one(p)
                except Exception as e:  # noqa: BLE001 单张失败不中断整批
                    log.exception("拖放导入单张失败，跳过: %s (%s)", p, e)
                    rid, was_touch = None, False
                if was_touch:
                    touched += 1
                elif rid is not None:
                    added.append((rid, p))
                self.progressed.emit(i, total)
        finally:
            _release_thread_db(self._history)   # QThread 不会自动释放（见该函数）
        self.finished_ok.emit(added, touched)

    @staticmethod
    def _read(p: str) -> np.ndarray:
        """读文件为 uint8 ndarray。np.fromfile 兼容含非 ASCII 字符的路径。"""
        try:
            return np.fromfile(p, np.uint8)
        except Exception:  # noqa: BLE001 退回普通 read_bytes
            with open(p, "rb") as fh:
                return np.frombuffer(fh.read(), np.uint8)

    def _import_one(self, p: str):
        """导入单张：返回 (rid, False) 新增 | (None, True) 置顶已有 | (None, False) 跳过。"""
        # 读图：调用方注入的读取器兼容含非 ASCII 字符的路径
        try:
            raw = self._read(p)
        except Exception as e:  # noqa: BLE001
            log.warning("拖放图片读取失败，跳过: %s (%s)", p, e)
            return None, False
        # 同名同大小 → 视为同一文件：置顶已有记录，不新增、不重复 OCR
        dup_rid = self._find_dup(p, len(raw), self._dup_index)
        if dup_rid is not None:
            if self._history.touch(dup_rid):
                log.info("拖放图片与库内记录同名同大小，已置顶记录 #%s: %s",
                         dup_rid, p)
                return None, True
            return None, False
        # 解码：cv2 优先，失败回退 Pillow（AVIF / HEIC / HEIF 等现代格式）
        img = self._decode(raw)
        if img is None:
            log.warning("拖放图片解码失败（非图像/损坏/缺少对应解码器），跳过: %s", p)
            return None, False
        # 编码主图 + 缩略图（遵循 [software] save_format / save_quality）
        ok, ext, image_bytes, thumb_bytes = self._encode_pair(img)
        if not ok:
            return None, False
        del img          # 交回内存后再落库（大图编码后不再需要）
        # 落库：ocr_boxes=None → NULL = 「从未识别」（待识别态，面板显示 "(未识别)"）。
        # 不能写 []：'[]' 的语义是「已识别、图中无文字」，会被当成已完成识别而不再补跑
        # （见 restore_record）。model_tier/device 同理留空：等识别完由 update_ocr
        # 补写执行记录（这是「执行记录」而非「配置快照」的关键）。
        rid = self._history.save(
            image_bytes=image_bytes, thumb_bytes=thumb_bytes,
            ocr_text="", ocr_boxes=None, model_tier="", device="",
            ext=ext, name_pattern=self._name_pattern)
        return rid, False


