"""core_engine.db_manager — SQLite 统一读写（无 Qt 依赖）。

- 建库：M2 一次性建齐 §16.1 的 10 张表 + 1 张 vec0 虚拟表（image_embedding_vec，
  需 sqlite-vec 扩展；扩展不可用时降级跳过并记日志，不阻塞建库）。
- 并发：WAL + 每操作短连接 + 进程内锁（截图 OCR 在工作线程，历史保存需线程安全）。
- HistoryStorage：截图模式历史记录（screenshot_history）CRUD / 搜索 / 收藏 / FIFO。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, List, Optional

logger = logging.getLogger("clickocr.db")

# ---------------------------------------------------------------------------
# 建库 DDL（§16.1 + §19.6，M2 一次性建齐；M6 起启用预置表）
# ---------------------------------------------------------------------------

_SCHEMA = """
-- 截图模式历史记录
CREATE TABLE IF NOT EXISTS screenshot_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    screenshot_path TEXT NOT NULL,          -- 截图 PNG 文件路径（相对软件目录）
    ocr_text    TEXT,                       -- OCR 识别文本（纯文本，供搜索）
    ocr_boxes   TEXT,                       -- OcrBox 列表 JSON（含坐标，供选中复制）
    model_tier  TEXT NOT NULL,             -- 识别执行记录：实际档位 tiny/small/medium；'' = 未识别
    device      TEXT NOT NULL,             -- 识别执行记录：实际生效设备 cpu/cuda/dml；'' = 未识别
    created_at  REAL NOT NULL,             -- 创建时间戳（Unix timestamp）
    file_size   INTEGER,                   -- 截图文件大小（字节，用于校验）
    is_favorite INTEGER DEFAULT 0,         -- 收藏标记: 0=普通, 1=收藏（收藏不受条数限制）
    annotations TEXT,                      -- 标注数据 JSON（M2-7 写入）
    has_table   INTEGER DEFAULT 0,         -- 是否包含表格: 0=无, 1=有
    status      INTEGER DEFAULT 0          -- 向量化状态: 0=未向量化, 1=已向量化
);
CREATE INDEX IF NOT EXISTS idx_screenshot_created ON screenshot_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_screenshot_text ON screenshot_history(ocr_text);
CREATE INDEX IF NOT EXISTS idx_screenshot_favorite ON screenshot_history(is_favorite, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_screenshot_status ON screenshot_history(status);

-- 照片/图片主表（M3 看图模式使用；M2 建好结构）
CREATE TABLE IF NOT EXISTS images (
    id TEXT PRIMARY KEY,                    -- file_path + size + mtime 哈希
    file_path TEXT UNIQUE NOT NULL,
    file_size INTEGER NOT NULL,
    file_mtime REAL NOT NULL,
    width INTEGER,
    height INTEGER,
    exif_make TEXT, exif_model TEXT, exif_datetime TEXT,
    exif_fnumber REAL, exif_exposure_time TEXT, exif_iso INTEGER,
    exif_focal_length REAL, exif_orientation INTEGER,
    gps_latitude REAL, gps_longitude REAL, gps_altitude REAL,
    gps_country TEXT, gps_city TEXT,
    caption TEXT,
    annotations TEXT,
    indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_images_file_path ON images(file_path);
CREATE INDEX IF NOT EXISTS idx_images_exif_datetime ON images(exif_datetime);
CREATE INDEX IF NOT EXISTS idx_images_gps ON images(gps_latitude, gps_longitude);

-- 照片 OCR 文字行（每行一条，RAG 天然 chunk）
CREATE TABLE IF NOT EXISTS image_ocr (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id TEXT NOT NULL,
    model_tier TEXT NOT NULL DEFAULT 'tiny',
    text_content TEXT NOT NULL,
    polygon_json TEXT,
    confidence REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_image_ocr_image ON image_ocr(image_id, model_tier);

-- 照片标签（三源：manual / ocr / ai）
CREATE TABLE IF NOT EXISTS image_tags (
    image_id TEXT NOT NULL,
    tag TEXT NOT NULL COLLATE NOCASE,
    source TEXT DEFAULT 'manual',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (image_id, tag),
    FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_image_tags_tag ON image_tags(tag);

-- OCR 扩展结果（区域框选重识别，M4）
CREATE TABLE IF NOT EXISTS image_ocr_extend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_ocr_id INTEGER NOT NULL,
    region_x REAL NOT NULL, region_y REAL NOT NULL,
    region_w REAL NOT NULL, region_h REAL NOT NULL,
    ocr_texts TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (image_ocr_id) REFERENCES image_ocr(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ocr_extend_ocr ON image_ocr_extend(image_ocr_id);

-- FTS5 全局搜索（OCR + caption + tag 聚合，trigram 分词）
CREATE VIRTUAL TABLE IF NOT EXISTS image_search USING fts5(
    image_id UNINDEXED,
    content,
    source,                                 -- ocr / caption / tag
    tokenize = 'trigram'
);
CREATE TRIGGER IF NOT EXISTS trg_image_ocr_insert AFTER INSERT ON image_ocr
BEGIN
    INSERT INTO image_search(image_id, content, source)
    VALUES (NEW.image_id, NEW.text_content, 'ocr');
END;
CREATE TRIGGER IF NOT EXISTS trg_image_ocr_delete AFTER DELETE ON image_ocr
BEGIN
    DELETE FROM image_search
    WHERE image_id = OLD.image_id AND content = OLD.text_content;
END;
CREATE TRIGGER IF NOT EXISTS trg_images_caption_update AFTER UPDATE OF caption ON images
BEGIN
    DELETE FROM image_search WHERE image_id = NEW.id AND source = 'caption';
    INSERT INTO image_search(image_id, content, source)
    VALUES (NEW.id, NEW.caption, 'caption');
END;
CREATE TRIGGER IF NOT EXISTS trg_tags_insert AFTER INSERT ON image_tags
BEGIN
    INSERT INTO image_search(image_id, content, source)
    VALUES (NEW.image_id, NEW.tag, 'tag');
END;

-- 表格识别结果缓存（M4）
CREATE TABLE IF NOT EXISTS table_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'image',
    model_tier TEXT NOT NULL DEFAULT 'slanet',
    table_result TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_table_cache_image ON table_cache(image_id, model_tier);

-- ===== 预置表（§19.6，M6 启用；建库即建好） =====

-- 语义检索向量元数据（每图两条：image + text）
CREATE TABLE IF NOT EXISTS image_embedding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id TEXT NOT NULL,
    entry_type TEXT NOT NULL,               -- 'image': CLIP 图像向量 / 'text': BGE 文本向量
    chunk_text TEXT,
    vector_key TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE,
    UNIQUE (image_id, entry_type, model)
);
CREATE INDEX IF NOT EXISTS idx_image_embedding_image ON image_embedding(image_id);
CREATE INDEX IF NOT EXISTS idx_image_embedding_model ON image_embedding(model);

-- 轻型档向量本体（sqlite-vec vec0；在 Python 层按扩展可用性创建，见 init_db）
-- CREATE VIRTUAL TABLE image_embedding_vec USING vec0(embedding float[512]);

-- 图片去重（imagehash 感知哈希，M4）
CREATE TABLE IF NOT EXISTS image_hash (
    image_id TEXT NOT NULL,
    hash_type TEXT NOT NULL,                -- 'phash' / 'dhash' / 'ahash'
    hash_value TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (image_id, hash_type),
    FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_image_hash_value ON image_hash(hash_type, hash_value);

-- 布局分析结果（DocLayout-YOLO，可选）
CREATE TABLE IF NOT EXISTS image_layout (
    image_id TEXT PRIMARY KEY,
    layout_json TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT 'doclayout-yolo',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
);
"""


class DBManager:
    """SQLite 统一读写。线程安全：每操作短连接 + 进程内锁。"""

    def __init__(self, db_path: str | Path, enable_vec0: bool = True):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self.vec0_available = False
        self._vec0_enabled = enable_vec0

    # ---- 连接与建库 ----
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 8000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> bool:
        """建库（幂等）。返回 vec0 是否可用。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_SCHEMA)
                if self._vec0_enabled:
                    self._try_create_vec0(conn)
                conn.commit()
            finally:
                conn.close()
        return self.vec0_available

    def _try_create_vec0(self, conn: sqlite3.Connection):
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            conn.load_extension(sqlite_vec.loadable_path())
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS image_embedding_vec "
                "USING vec0(embedding float[512])")
            self.vec0_available = True
            logger.info("sqlite-vec 可用，image_embedding_vec 已建")
        except Exception as e:  # noqa: BLE001
            self.vec0_available = False
            logger.warning("sqlite-vec 不可用，跳过 vec0 表（%s）；M6 启用前需安装", e)

    # ---- 通用执行 ----
    def execute(self, sql: str, params: tuple = ()) -> int:
        """执行写语句，返回 rowcount。"""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    def executemany(self, sql: str, seq) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.executemany(sql, seq)
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    def query(self, sql: str, params: tuple = ()) -> List[dict]:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def query_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def table_names(self) -> List[str]:
        rows = self.query(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view','trigger')")
        return [r["name"] for r in rows]


# ---------------------------------------------------------------------------
# HistoryStorage — 截图模式历史记录
# ---------------------------------------------------------------------------

_MODEL_TIERS = {"tiny", "small", "medium"}


def _parse_time_token(tk: str):
    """把时间类关键词解析为 (start_ts, end_ts|None)；非时间词返回 None。

    支持（不区分大小写）：today/今天、yesterday/昨天、week/本周、
    dN / lastNd / N天（最近 N 天）、YYYY-MM-DD 或 YYYY/MM/DD（当天）。
    时间戳与库内 created_at（time.time() 的 POSIX 秒）一致。
    """
    now = datetime.now()
    low = tk.lower()
    if low in ("today", "今天"):
        start = datetime(now.year, now.month, now.day)
        return (start.timestamp(), None)
    if low in ("yesterday", "昨天"):
        start = datetime(now.year, now.month, now.day) - timedelta(days=1)
        end = datetime(now.year, now.month, now.day)
        return (start.timestamp(), end.timestamp())
    if low in ("week", "本周"):
        monday = datetime(now.year, now.month, now.day) - timedelta(days=now.weekday())
        return (monday.timestamp(), None)
    m = re.match(r"^d(\d+)$", low) or re.match(r"^(\d+)天$", low) or \
        re.match(r"^last(\d+)d$", low)
    if m:
        start = datetime(now.year, now.month, now.day) - timedelta(days=int(m.group(1)))
        return (start.timestamp(), None)
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", tk)
    if m:
        try:
            start = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        return (start.timestamp(), (start + timedelta(days=1)).timestamp())
    return None


def _build_search_clause(keyword: str, favorite_only: bool = False):
    """把搜索串解析为 (where_sql, params)。

    - 空格分词，多个文本词之间 AND；
    - 文本词匹配 ocr_text 或 annotations（标注里的 text 也参与检索）；
    - tiny/small/medium 视为模型档位筛选（model_tier = ?）；
    - 时间词（今天/昨天/本周/日期/dN）视为时间范围筛选（created_at 有索引）。
    返回 ("", []) 表示无约束（调用方应回退到 list / list_favorites）。
    """
    raw = (keyword or "").strip()
    if not raw:
        return ("", [])
    text_tokens, models, times = [], [], []
    for tk in raw.split():
        low = tk.lower()
        if low in _MODEL_TIERS:
            models.append(low)
            continue
        tr = _parse_time_token(tk)
        if tr is not None:
            times.append(tr)
            continue
        text_tokens.append(tk)

    wheres, params = [], []
    for tk in text_tokens:
        kw = tk.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{kw}%"
        # 标注文字参与：annotations 是 JSON，LIKE 命中 "text":"..." 等；
        # 无法用索引，但 3000 级记录下全表扫描仍是亚毫秒~毫秒级。
        wheres.append("(ocr_text LIKE ? ESCAPE '\\' OR annotations LIKE ? ESCAPE '\\')")
        params += [like, like]
    for mv in models:
        wheres.append("model_tier = ?")
        params.append(mv)
    for (ts, te) in times:
        wheres.append("created_at >= ?")
        params.append(ts)
        if te is not None:
            wheres.append("created_at < ?")
            params.append(te)
    if favorite_only:
        wheres.append("is_favorite = 1")
    if not wheres:
        return ("", [])
    return (" WHERE " + " AND ".join(wheres), params)


class HistoryStorage:
    """screenshot_history 存储服务（§13.4 HistoryStorage Protocol 实现）。"""

    def __init__(self, db: DBManager, screenshots_dir: str | Path,
                 max_records: int = 50):
        self.db = db
        self.screenshots_dir = Path(screenshots_dir)
        self.max_records = max(1, int(max_records))

    # ---- 内部 ----
    def _next_seq(self) -> int:
        """序号占位符 {seq}：screenshots 目录内已有文件数 + 1（跨重启稳定）。"""
        try:
            n = sum(1 for p in self.screenshots_dir.iterdir()
                    if p.is_file())
            return n + 1
        except OSError:
            return 1

    def _build_name(self, pattern: str, ext: str) -> str:
        """按占位符模板生成文件名（不含目录），自动补扩展名与重名避让。

        占位符：{datetime} 日期时间、{date} 日期、{time} 时间、{ms} 毫秒(3位)、
        {ts} 时间戳(ms)、{seq} 序号、{ext} 扩展名。
        空模板/模板产出为空 → 回退默认 {datetime}_{ms}。
        """
        now = time.localtime()
        ms3 = int(time.time() * 1000) % 1000
        pat = (pattern or "").strip() or "{datetime}_{ms}"
        name = (pat.replace("{datetime}", time.strftime("%Y%m%d_%H%M%S", now))
                   .replace("{date}", time.strftime("%Y%m%d", now))
                   .replace("{time}", time.strftime("%H%M%S", now))
                   .replace("{ms}", f"{ms3:03d}")
                   .replace("{ts}", str(int(time.time() * 1000)))
                   .replace("{seq}", str(self._next_seq()))
                   .replace("{ext}", ext))
        # 非法文件名字符替换 + 空名回退
        name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip()
        if not name:
            name = (time.strftime("%Y%m%d_%H%M%S", now) + f"_{ms3:03d}")
        # 确保带扩展名（模板已带 {ext} 且以 .ext 结尾时跳过）
        if not name.lower().endswith("." + ext.lower()):
            name += "." + ext
        # 重名避让：追加 _2/_3…
        stem, suffix = Path(name).stem, Path(name).suffix
        candidate = self.screenshots_dir / name
        i = 2
        while candidate.exists():
            name = f"{stem}_{i}{suffix}"
            candidate = self.screenshots_dir / name
            i += 1
        return name

    def _write_files(self, image_bytes: bytes, thumb_bytes: Optional[bytes],
                     ext: str = "png", name_pattern: str = "") -> str:
        """写截图 + 缩略图，返回相对软件目录的截图路径（如 screenshots/xxx.png）。"""
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        thumb_dir = self.screenshots_dir / "thumb"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        name = self._build_name(name_pattern, ext or "png")
        (self.screenshots_dir / name).write_bytes(image_bytes)
        if thumb_bytes:
            (thumb_dir / name).write_bytes(thumb_bytes)
        return f"screenshots/{name}"

    def _delete_files(self, screenshot_path: str):
        """删除记录关联的截图与缩略图文件（尽力而为）。"""
        for rel in (screenshot_path, str(Path(screenshot_path).with_name(
                "thumb") / Path(screenshot_path).name)):
            p = self.screenshots_dir.parent / rel
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    # ---- 写入 ----
    def save(self, image_bytes: bytes, thumb_bytes: Optional[bytes],
             ocr_text: str, ocr_boxes: Any, model_tier: str = "",
             device: str = "", annotations: Optional[str] = None,
             has_table: int = 0, ext: str = "png",
             name_pattern: str = "") -> int:
        """保存一条历史记录；返回新记录 id。超过条数上限自动 FIFO 清理（收藏除外）。

        ext = 图片扩展名（png/jpg/bmp/webp）；name_pattern = 文件名占位符模板
        （空 = 默认 {datetime}_{ms}）。

        model_tier / device 是**识别执行记录**（这条被哪个档位、在哪个设备上识别的），
        **仅在识别已完成时才有值**；入库时还没识别的图（拖入不识别 / 关了自动 OCR 就
        收藏、钉图）留空 ``""``。运行中补识别的结果由 update_ocr 补写这两个字段。
        列本身是 NOT NULL，所以"没有执行记录"用空串表示，与 ``ocr_boxes IS NULL``
        （未识别）一一对应。
        """
        rel = self._write_files(image_bytes, thumb_bytes, ext or "png",
                                name_pattern or "")
        now = time.time()
        rid = self.db.execute(
            """INSERT INTO screenshot_history
               (screenshot_path, ocr_text, ocr_boxes, model_tier, device,
                created_at, file_size, is_favorite, annotations, has_table, status)
               VALUES (?,?,?,?,?,?,?,0,?,?,0)""",
            (rel, ocr_text or "",
             json.dumps(ocr_boxes, ensure_ascii=False) if ocr_boxes is not None else None,
             model_tier, device, now, len(image_bytes), annotations, has_table),
        )
        # 返回自增 id：SQLite 无 RETURNING 时用 last_insert_rowid 需同一连接，
        # 这里用最大 id 近似（单写锁下安全）
        row = self.db.query_one(
            "SELECT id FROM screenshot_history WHERE created_at=? AND screenshot_path=?",
            (now, rel))
        rid = row["id"] if row else 0
        self.purge()
        return rid

    def update_ocr(self, rid: int, ocr_text: str, ocr_boxes: Any,
                   model_tier: str = "", device: str = "") -> bool:
        """识别结果写回记录（D-M2-3：更新 ocr_text/ocr_boxes + 执行记录 + created_at）。

        不新增记录、不动截图文件；FTS image_search 由 UPDATE 触发器同步。

        model_tier / device 是**识别执行记录**：本次识别实际用的档位与生效设备
        （取自 RapidOcrEngine.get_info()，设备为 cpu/cuda/dml；GPU 探测失败自动回退时
        如实记 cpu）。只有真跑过识别才会走到这里，所以在此写入即代表"识别过"。
        """
        try:
            self.db.execute(
                """UPDATE screenshot_history
                   SET ocr_text=?, ocr_boxes=?, model_tier=?, device=?, created_at=?
                   WHERE id=?""",
                (ocr_text or "",
                 json.dumps(ocr_boxes, ensure_ascii=False) if ocr_boxes is not None else None,
                 model_tier or "", device or "", time.time(), rid))
            return True
        except Exception:  # noqa: BLE001
            return False

    def update_annotations(self, rid: int, annotations: str) -> bool:
        """保存标注 JSON 到记录（M2-7）；同时刷新 created_at 使该记录置顶（操作时间）。"""
        try:
            self.db.execute(
                "UPDATE screenshot_history SET annotations=?, created_at=? WHERE id=?",
                (annotations, time.time(), rid))
            return True
        except Exception:  # noqa: BLE001
            return False

    def touch(self, record_id: int) -> bool:
        """仅刷新 created_at（操作时间）使记录置顶；不动文字/标注/截图文件。

        用于拖放去重命中「同名同大小」时：不新增记录，把已有那条移到最前。
        """
        try:
            self.db.execute(
                "UPDATE screenshot_history SET created_at=? WHERE id=?",
                (time.time(), record_id))
            return True
        except Exception:  # noqa: BLE001
            return False

    def purge(self) -> int:
        """FIFO：普通记录超过上限时删除最旧（收藏不受限）。返回删除条数。"""
        excess = self.db.query_one(
            """SELECT COUNT(*) AS c FROM screenshot_history WHERE is_favorite=0""")
        n_excess = excess["c"] - self.max_records if excess else 0
        if n_excess <= 0:
            return 0
        doomed = self.db.query(
            """SELECT id, screenshot_path FROM screenshot_history
               WHERE is_favorite=0
               ORDER BY created_at ASC, id ASC LIMIT ?""", (n_excess,))
        ids = [d["id"] for d in doomed]
        if ids:
            marks = ",".join("?" * len(ids))
            self.db.execute(
                f"DELETE FROM screenshot_history WHERE id IN ({marks})", tuple(ids))
        for d in doomed:
            self._delete_files(d["screenshot_path"])
        return len(ids)

    # ---- 读取 ----
    def list(self, limit: int = 50) -> List[dict]:
        """时间倒序（最新在最上）；收藏不影响顺序。"""
        return self.db.query(
            """SELECT * FROM screenshot_history
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (limit,))

    def get(self, record_id: int) -> Optional[dict]:
        return self.db.query_one(
            "SELECT * FROM screenshot_history WHERE id=?", (record_id,))

    def list_favorites(self) -> List[dict]:
        return self.db.query(
            """SELECT * FROM screenshot_history WHERE is_favorite=1
               ORDER BY created_at DESC, id DESC""")

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS c FROM screenshot_history")
        return row["c"] if row else 0

    # ---- 修改 / 删除 ----
    def set_favorite(self, record_id: int, is_favorite: bool) -> None:
        self.db.execute("UPDATE screenshot_history SET is_favorite=? WHERE id=?",
                        (1 if is_favorite else 0, record_id))

    def delete(self, record_id: int) -> None:
        row = self.get(record_id)
        if row:
            self._delete_files(row["screenshot_path"])
        self.db.execute("DELETE FROM screenshot_history WHERE id=?", (record_id,))

    def clear(self, keep_favorites: bool = True) -> int:
        """清空历史记录（并删除截图文件）。

        keep_favorites=True（默认）：只清非收藏记录，收藏的图片保留。
        """
        if keep_favorites:
            rows = self.db.query(
                "SELECT id, screenshot_path FROM screenshot_history"
                " WHERE is_favorite=0")
            for r in rows:
                self._delete_files(r["screenshot_path"])
            return self.db.execute(
                "DELETE FROM screenshot_history WHERE is_favorite=0")
        rows = self.db.query("SELECT id, screenshot_path FROM screenshot_history")
        for r in rows:
            self._delete_files(r["screenshot_path"])
        return self.db.execute("DELETE FROM screenshot_history")

    def search(self, keyword: str, limit: int = 200) -> List[dict]:
        """关键词搜索：支持空格分词 AND、标注文字、模型(tiny/small/medium)、
        时间(今天/昨天/本周/日期/dN)。详见 _build_search_clause。"""
        where, params = _build_search_clause(keyword, favorite_only=False)
        if not where:
            return self.list(limit)
        sql = ("SELECT * FROM screenshot_history" + where +
               " ORDER BY created_at DESC, id DESC LIMIT ?")
        return self.db.query(sql, tuple(params) + (limit,))

    def search_favorites(self, keyword: str, limit: int = 200) -> List[dict]:
        """收藏 Tab 的搜索：与 search 同源（文字/标注/模型/时间一致），
        但限定 is_favorite=1。"""
        where, params = _build_search_clause(keyword, favorite_only=True)
        if not where:
            return self.list_favorites()
        sql = ("SELECT * FROM screenshot_history" + where +
               " ORDER BY created_at DESC, id DESC LIMIT ?")
        return self.db.query(sql, tuple(params) + (limit,))
