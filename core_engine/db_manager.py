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
    created_at  REAL NOT NULL,             -- 创建时间戳（Unix timestamp），落库后**不再修改**
    updated_at  REAL NOT NULL DEFAULT 0,   -- 最后操作时间（识别写回/标注/拖放置顶时刷新）
    file_size   INTEGER,                   -- 截图文件大小（字节，用于校验）
    is_favorite INTEGER DEFAULT 0,         -- 收藏标记: 0=普通, 1=收藏（收藏不受条数限制）
    annotations TEXT,                      -- 标注数据 JSON（M2-7 写入）
    has_table   INTEGER DEFAULT 0,         -- 是否包含表格: 0=无, 1=有
    status      INTEGER DEFAULT 0          -- 向量化状态: 0=未向量化, 1=已向量化
);
CREATE INDEX IF NOT EXISTS idx_screenshot_created ON screenshot_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_screenshot_text ON screenshot_history(ocr_text);
CREATE INDEX IF NOT EXISTS idx_screenshot_status ON screenshot_history(status);
-- 注意：updated_at 相关的两个索引不在这里建。
-- 旧库的表已存在（CREATE TABLE IF NOT EXISTS 会跳过），此时 updated_at 列还没加上，
-- 在这里建索引会直接报 "no such column: updated_at"。它们由 _migrate() 在建列之后创建。

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
    """SQLite 统一读写。

    **连接**：每线程一个长连接（`threading.local` 缓存），不再每操作
    `sqlite3.connect()` + `close()`。短连接的代价不只是 open：`connect()` 每次都要
    解析路径、加文件锁、跑两遍 PRAGMA，而历史面板翻一页就是十几次读写。

    **锁**：读写分离。
      - 写走 `_write_lock` 串行化 —— SQLite 同一时刻只允许一个写者，显式串行比
        依赖 `busy_timeout` 重试更可预期；
      - 读**不加锁** —— WAL 下读不阻塞写、写不阻塞读，UI 线程读历史不再被工作
        线程的写卡住（原实现是一把全局锁把读写全部串行化）。

    每线程的连接由 `threading.local` 持有，线程结束即随对象销毁而关闭；
    `close()` 可显式关闭**当前线程**的连接。
    """

    def __init__(self, db_path: str | Path, enable_vec0: bool = True):
        self.db_path = Path(db_path)
        self._write_lock = threading.Lock()
        self._tls = threading.local()
        self.vec0_available = False
        self._vec0_enabled = enable_vec0

    # ---- 连接与建库 ----
    def _connect(self) -> sqlite3.Connection:
        """新建一个连接（**不**进入线程缓存）。"""
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 8000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _conn(self) -> sqlite3.Connection:
        """取本线程的连接（不存在则建并缓存）。"""
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = self._connect()
            self._tls.conn = conn
        return conn

    def close(self) -> None:
        """关闭**当前线程**的连接（幂等）。

        不提供跨线程关闭：对别的线程正在使用的连接调用 close() 会让它正在跑的
        语句崩掉。其它线程的连接在其线程结束后由 GC 关闭（`sqlite3.Connection`
        析构会 close）。
        """
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            return
        try:
            conn.close()
        except Exception:                # noqa: BLE001 已关闭/句柄失效：忽略
            pass
        self._tls.conn = None

    def _write(self, fn):
        """在写锁内执行 `fn(conn)` 并提交；异常回滚。

        长连接下**必须**显式回滚：留下半截事务会让同线程后续读看到未提交数据，
        并且一直占着写锁把其它写者挡到 busy_timeout 超时（短连接靠 close 兜住了
        这个问题）。
        """
        with self._write_lock:
            conn = self._conn()
            try:
                out = fn(conn)
                conn.commit()
                return out
            except Exception:
                try:
                    conn.rollback()
                except Exception:        # noqa: BLE001 回滚失败不掩盖原异常
                    pass
                raise

    def init_db(self) -> bool:
        """建库（幂等）。返回 vec0 是否可用。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        def _init(conn):
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            if self._vec0_enabled:
                self._try_create_vec0(conn)

        self._write(_init)
        return self.vec0_available

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """就地迁移旧库（幂等）。

        历史问题：`created_at` 一列同时被当作「创建时间」和「最后操作时间」用
        （`update_ocr` / `update_annotations` / `touch` 都会改写它），导致
        分组、时间搜索、FIFO 淘汰三条链路全部错位——最典型的是「被标注过的老
        记录会变新从而永远不被清理，刚截的新图反而先被删」。

        现拆为两列：`created_at` 落库后不再修改，`updated_at` 记录最后操作时间。
        旧库补列后回填 `updated_at = created_at`，因此既有排序行为保持不变。
        """
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(screenshot_history)")}
        if "updated_at" not in cols:
            conn.execute("ALTER TABLE screenshot_history "
                         "ADD COLUMN updated_at REAL NOT NULL DEFAULT 0")
            conn.execute("UPDATE screenshot_history SET updated_at = created_at")
            logger.info("已迁移 screenshot_history：新增 updated_at 并由 created_at 回填")
        # 兜底：极少数路径可能留下 0（如外部脚本直插），统一回填避免排到最后
        fixed = conn.execute(
            "UPDATE screenshot_history SET updated_at = created_at "
            "WHERE updated_at IS NULL OR updated_at = 0").rowcount
        if fixed:
            logger.info("已回填 %d 条 updated_at=0 的记录", fixed)
        # 建列之后再建索引（见 _SCHEMA 里的说明）
        conn.execute("CREATE INDEX IF NOT EXISTS idx_screenshot_updated "
                     "ON screenshot_history(updated_at DESC)")
        # 旧库的 idx_screenshot_favorite 建在 created_at 上，语义已变，需重建
        old = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_screenshot_favorite'").fetchone()
        if old and "updated_at" not in (old["sql"] or ""):
            conn.execute("DROP INDEX idx_screenshot_favorite")
            conn.execute("CREATE INDEX idx_screenshot_favorite "
                         "ON screenshot_history(is_favorite, updated_at DESC)")
        if not old:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_screenshot_favorite "
                         "ON screenshot_history(is_favorite, updated_at DESC)")

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

    # ---- 语句执行（写走写锁；读不加锁，见类文档）----

    def execute(self, sql: str, params: tuple = ()) -> int:
        """执行写语句，返回 rowcount（注意：INSERT 时恒为 1，不是新记录 id）。"""
        return self._write(lambda conn: conn.execute(sql, params).rowcount)

    def execute_returning_id(self, sql: str, params: tuple = ()) -> int:
        """执行 INSERT 并返回新记录自增 id。

        `last_insert_rowid` 必须与 INSERT 在**同一连接**内读取 —— 连接是线程内
        复用的，故 INSERT 与取 lastrowid 天然同连接。
        """
        def _run(conn):
            cur = conn.execute(sql, params)
            return int(cur.lastrowid or 0)
        return self._write(_run)

    def executemany(self, sql: str, seq) -> int:
        return self._write(lambda conn: conn.executemany(sql, seq).rowcount)

    def query(self, sql: str, params: tuple = ()) -> List[dict]:
        return [dict(r) for r in self._conn().execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        """只取首行。单独走 fetchone 路径，不经 query() 的全量 fetchall。

        调用点多用于 COUNT(*) / 按 id 取单条，结果集本就一行；复用 query()
        会把整个结果集先物化成 list 再取 [0]，属于无谓的内存与时间开销。
        """
        row = self._conn().execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def query_tuples(self, sql: str, params: tuple = ()) -> List[tuple]:
        """按列取值的轻量查询：返回「元组列表」，不构造每行 dict。

        与 query() 的区别：大结果集只取一两列时（如拖放去重索引只要
        id/文件名/文件大小），省掉 N 次 Row→dict 构造；配合调用只 SELECT
        必要列（不把大 JSON 列一起拉回来），瞬时分配与 GC 压力都随之下降。
        """
        return [tuple(r) for r in self._conn().execute(sql, params).fetchall()]

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
        # 已知代价（写清而不只是"自知"）：`LIKE '%kw%'` 的前导通配使 ocr_text 上的
        # idx_screenshot_text 彻底失效，每个文本词 = ocr_text + annotations 两次
        # 全表扫描（annotations 是 JSON，连前缀匹配的可能都没有）。
        # 当前规模下代价可接受：历史默认上限 50 条、实测数千条，万行级扫描在
        # 亚毫秒~毫秒级，搜索又是显式用户动作，无感。**故本轮不引入 FTS5**：
        # image_search（fts5 trigram）与写入触发器虽已建好，但历史搜索还要覆盖
        # annotations、并保持 \ % _ 的转义语义，切过去要重写本函数的整套语义 +
        # 旧库回填 + 转义规则对齐，改动面与回归风险远大于收益。
        # 迁移触发条件（满足任一即应改为 image_search MATCH + 回查本表）：
        #   a) 单表稳定过万行后搜索耗时 > 100ms；
        #   b) 搜索被挪到 UI 线程同步执行并出现可感知卡顿；
        #   c) 需要相关性排序 / 布尔查询 / 命中高亮等 FTS5 能力。
        # 届时：text_tokens 走 image_search MATCH（trigram 天然支持子串），
        # JOIN screenshot_history 取行，annotations 的 LIKE 降级为兜底分支。
        # 标注（JSON）只搜**值**、不搜键名：原先整串 `annotations LIKE '%kw%'`
        # 会把 JSON 的键名也算进命中，搜 "text"/"type"/"rect"/"mosaic"/"color"/"x"
        # 之类会捞出一堆根本不含该词的记录。改用 json_each 只看每个标注的 text/num
        # 两个用户可见字段；json_valid 兜住历史里的脏数据（非法 JSON 会让
        # json_each 直接抛错，把整条搜索带崩）。
        # 路径 `$.items` 必须与 ui/core/anno_core.anno_to_json 的序列化格式一致
        # （`{"version": N, "items": [...]}`）—— 写成别的键名时 json_each 对不存在的
        # 路径**静默返回 0 行**、不报错，于是「搜标注里的文字」会彻底失效而毫无迹象
        # （曾有 `$.marks` 与实际格式不符，靠这一行修掉；tests/test_db_manager.py
        # 用真实序列化器造数据守住它）。
        wheres.append(
            "(ocr_text LIKE ? ESCAPE '\\'"
            " OR (json_valid(annotations) AND EXISTS ("
            "   SELECT 1 FROM json_each(annotations, '$.items')"
            "   WHERE json_extract(value, '$.text') LIKE ? ESCAPE '\\'"
            "      OR CAST(json_extract(value, '$.num') AS TEXT)"
            "         LIKE ? ESCAPE '\\')))"
        )
        params += [like, like, like]
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
        # {seq} 只在模板真的用到时才求值：_next_seq() 要遍历整个截图目录
        # （N 条记录 = N 次 stat）且发生在调用线程（保存走 UI 线程），而默认模板
        # {datetime}_{ms} 不含 {seq} —— 无条件求值等于每次保存都白扫一遍目录。
        seq = str(self._next_seq()) if "{seq}" in pat else ""
        name = (pat.replace("{datetime}", time.strftime("%Y%m%d_%H%M%S", now))
                   .replace("{date}", time.strftime("%Y%m%d", now))
                   .replace("{time}", time.strftime("%H%M%S", now))
                   .replace("{ms}", f"{ms3:03d}")
                   .replace("{ts}", str(int(time.time() * 1000)))
                   .replace("{seq}", seq)
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
        """写截图 + 缩略图，返回截图文件名（不含目录，如 xxx.png）。

        DB 的 screenshot_path 列只存文件名：UI 层（历史面板/拖放去重）一律用
        ``Path(...).name`` + screenshots_dir 还原绝对路径，若这里存
        ``screenshots/<name>`` 前缀，自定义截图目录（screenshots_dir 可配置）
        时 `_delete_files` 按 `screenshots_dir.parent / rel` 还原会指向错误位置，
        文件永远删不掉（磁盘静默泄漏）。旧库中的带前缀值由 `_delete_files`
        兼容处理。
        """
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        thumb_dir = self.screenshots_dir / "thumb"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        name = self._build_name(name_pattern, ext or "png")
        # 独占创建（'xb'）兜底 _build_name 的 exists() 检查-写入竞态：
        # 拖放导入线程与主线程（截图/收藏落库）可能并发走到这里，
        # 仅靠 exists() 判断时两者可能选中同名文件互相覆盖。
        path = self.screenshots_dir / name
        stem, suffix = Path(name).stem, Path(name).suffix
        for i in range(2, 1002):
            try:
                with open(path, "xb") as fh:
                    fh.write(image_bytes)
                break
            except FileExistsError:
                name = f"{stem}_{i}{suffix}"
                path = self.screenshots_dir / name
        else:
            raise OSError(f"无法为截图分配唯一文件名（重名过多）: {stem}")
        if thumb_bytes:
            (thumb_dir / name).write_bytes(thumb_bytes)
        return name

    def _delete_files(self, screenshot_path: str):
        """删除记录关联的截图与缩略图文件（尽力而为）。

        以「文件名 + 当前 screenshots_dir」还原真实路径，兼容旧库中
        ``screenshots/<name>`` 前缀格式与纯文件名格式（统一取 Path.name）。
        """
        name = Path(screenshot_path).name
        if not name or name in (".", ".."):
            return
        for p in (self.screenshots_dir / name,
                  self.screenshots_dir / "thumb" / name):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                # 尽力而为但不静默：Windows 上缩略图预览 / 杀软扫描会短暂占用
                # 文件（极常见），删不掉只留孤儿文件而不影响记录删除，但必须
                # 带路径留痕，否则「磁盘只涨不降」无从排查。
                logger.warning("删除截图文件失败（文件可能被占用）: %s", p,
                               exc_info=True)

    def _write_update(self, sql: str, params, what: str) -> bool:
        """写回类操作的**唯一**失败出口：记 warning（含堆栈）后返回 False。

        update_ocr / update_annotations / touch 语义一致（写回 DB、返回 bool），
        原先各自裸 `except: return False` 吞错且无任何日志——最严重的是
        update_ocr：写回失败时 OCR 识别结果静默丢失，界面无提示、日志无线索，
        只能靠复现排查。收敛到这里，新增写回方法直接复用，避免「静默失败」
        再次散落多处。

        params 可传 tuple，也可传「返回 tuple 的可调用对象」：后者让参数构造
        （如 ocr_boxes 的 json 序列化）也落在同一保护区内，失败同样有日志，
        而不是在调用方再写一个静默的 except。
        """
        try:
            self.db.execute(sql, params() if callable(params) else params)
            return True
        except Exception:  # noqa: BLE001
            logger.warning("%s 写入失败，本次修改未落库", what, exc_info=True)
            return False

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
        name = self._write_files(image_bytes, thumb_bytes, ext or "png",
                                 name_pattern or "")
        now = time.time()
        try:
            rid = self.db.execute_returning_id(
                """INSERT INTO screenshot_history
                   (screenshot_path, ocr_text, ocr_boxes, model_tier, device,
                    created_at, updated_at, file_size, is_favorite, annotations,
                    has_table, status)
                   VALUES (?,?,?,?,?,?,?,?,0,?,?,0)""",
                (name, ocr_text or "",
                 json.dumps(ocr_boxes, ensure_ascii=False) if ocr_boxes is not None else None,
                 model_tier, device, now, now, len(image_bytes), annotations,
                 has_table),
            )
        except Exception:
            # 插库失败回滚删除已写文件：purge 只清 DB 记录对应的文件，
            # 孤儿文件一旦留下永远不会被清理（磁盘缓慢泄漏）
            try:
                self._delete_files(name)
            except Exception:  # noqa: BLE001
                logger.exception("插库失败后回滚删除截图文件失败: %s", name)
            raise
        self.purge()
        return rid

    def update_ocr(self, rid: int, ocr_text: str, ocr_boxes: Any,
                   model_tier: str = "", device: str = "") -> bool:
        """识别结果写回记录（D-M2-3：更新 ocr_text/ocr_boxes + 执行记录 + 操作时间）。

        不新增记录、不动截图文件；FTS image_search 由 UPDATE 触发器同步。
        **不动 created_at**（那是截图创建时间，供分组与时间搜索使用），
        只刷 updated_at 让该记录在列表里置顶。

        model_tier / device 是**识别执行记录**：本次识别实际用的档位与生效设备
        （取自 RapidOcrEngine.get_info()，设备为 cpu/cuda/dml；GPU 探测失败自动回退时
        如实记 cpu）。只有真跑过识别才会走到这里，所以在此写入即代表"识别过"。
        """
        # 参数用 lambda 延迟到 _write_update 的保护区内求值：ocr_boxes 的 json
        # 序列化（不可序列化对象会抛 TypeError）也算写回失败，走同一出口留痕，
        # 而不是在调用方另写一个静默的 except。
        def _params():
            return (ocr_text or "",
                    json.dumps(ocr_boxes, ensure_ascii=False) if ocr_boxes is not None else None,
                    model_tier or "", device or "", time.time(), rid)

        return self._write_update(
            """UPDATE screenshot_history
               SET ocr_text=?, ocr_boxes=?, model_tier=?, device=?, updated_at=?
               WHERE id=?""",
            _params, f"update_ocr(id={rid})，OCR 结果未落库")

    def update_annotations(self, rid: int, annotations: str) -> bool:
        """保存标注 JSON 到记录（M2-7）；同时刷新 updated_at 使该记录置顶。"""
        return self._write_update(
            "UPDATE screenshot_history SET annotations=?, updated_at=? WHERE id=?",
            (annotations, time.time(), rid),
            f"update_annotations(id={rid})")

    def touch(self, record_id: int) -> bool:
        """仅刷新 updated_at（操作时间）使记录置顶；不动文字/标注/截图文件。

        用于拖放去重命中「同名同大小」时：不新增记录，把已有那条移到最前。
        """
        return self._write_update(
            "UPDATE screenshot_history SET updated_at=? WHERE id=?",
            (time.time(), record_id),
            f"touch(id={record_id})")

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
    # 排序一律用 updated_at：这就是「最近操作的排前面」的置顶语义
    # （识别写回 / 标注 / 拖放去重都会刷新它）。而分组、时间搜索、FIFO 淘汰
    # 用 created_at（截图创建时间），三者互不干扰。
    def list(self, limit: int = 50) -> List[dict]:
        """按操作时间倒序（最新在最上）；收藏不影响顺序。"""
        return self.db.query(
            """SELECT * FROM screenshot_history
               ORDER BY updated_at DESC, id DESC LIMIT ?""",
            (limit,))

    def get(self, record_id: int) -> Optional[dict]:
        return self.db.query_one(
            "SELECT * FROM screenshot_history WHERE id=?", (record_id,))

    def list_paths(self, limit: int = 100000) -> List[tuple]:
        """列出全部记录的 (id, screenshot_path, file_size) —— 只查必要列。

        给「拖放去重索引」用：它只需要文件名 + 文件大小做「同名同大小」判定，
        原调用方用 `list(limit=100000)`（= SELECT *）把每条记录的 ocr_boxes
        JSON 也一起取回来——3000 条、每块文字 20 个多边形时是几十 MB 的
        瞬时分配 + 反序列化，而这些数据一个字段都没用到。

        file_size 直接取 DB 列而不去 stat() 文件：save() 写入时
        `file_size = len(image_bytes)`，而 _write_files 写盘的正是同一份
        image_bytes，故该列与「截图文件的实际字节数」同值（本文件的
        验证脚本可逐条比对）；省掉 UI 线程上 N 次 stat 系统调用。
        排序与 list() 一致（updated_at DESC）：去重命中时取到的是同一条记录。
        """
        return self.db.query_tuples(
            """SELECT id, screenshot_path, file_size FROM screenshot_history
               ORDER BY updated_at DESC, id DESC LIMIT ?""",
            (limit,))

    def list_favorites(self) -> List[dict]:
        return self.db.query(
            """SELECT * FROM screenshot_history WHERE is_favorite=1
               ORDER BY updated_at DESC, id DESC""")

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS c FROM screenshot_history")
        return row["c"] if row else 0

    # ---- 修改 / 删除 ----
    def set_favorite(self, record_id: int, is_favorite: bool) -> None:
        self.db.execute("UPDATE screenshot_history SET is_favorite=? WHERE id=?",
                        (1 if is_favorite else 0, record_id))

    def delete(self, record_id: int) -> None:
        """删除记录。**先删 DB 记录、再删文件**。

        顺序不能反：先删文件的话，一旦 DB 删除失败（锁/磁盘满），就会留下
        「文件已没、记录还在」的破图记录，历史面板永久显示红叉，用户无法自行恢复。
        反过来先删记录、文件删失败最多留个孤儿文件（磁盘缓慢泄漏，可后续清理）。
        """
        row = self.get(record_id)
        self.db.execute("DELETE FROM screenshot_history WHERE id=?", (record_id,))
        if row:
            self._delete_files(row["screenshot_path"])

    def clear(self, keep_favorites: bool = True) -> int:
        """清空历史记录（并删除截图文件）。返回删除条数。

        keep_favorites=True（默认）：只清非收藏记录，收藏的图片保留。
        顺序同 delete()：先删 DB 记录，再删文件（避免破图记录）。
        """
        if keep_favorites:
            rows = self.db.query(
                "SELECT id, screenshot_path FROM screenshot_history"
                " WHERE is_favorite=0")
            n = self.db.execute(
                "DELETE FROM screenshot_history WHERE is_favorite=0")
        else:
            rows = self.db.query(
                "SELECT id, screenshot_path FROM screenshot_history")
            n = self.db.execute("DELETE FROM screenshot_history")
        for r in rows:
            self._delete_files(r["screenshot_path"])
        return n

    def search(self, keyword: str, limit: int = 200) -> List[dict]:
        """关键词搜索：支持空格分词 AND、标注文字、模型(tiny/small/medium)、
        时间(今天/昨天/本周/日期/dN)。详见 _build_search_clause。"""
        where, params = _build_search_clause(keyword, favorite_only=False)
        if not where:
            return self.list(limit)
        sql = ("SELECT * FROM screenshot_history" + where +
               " ORDER BY updated_at DESC, id DESC LIMIT ?")
        return self.db.query(sql, tuple(params) + (limit,))

    def search_favorites(self, keyword: str, limit: int = 200) -> List[dict]:
        """收藏 Tab 的搜索：与 search 同源（文字/标注/模型/时间一致），
        但限定 is_favorite=1。"""
        where, params = _build_search_clause(keyword, favorite_only=True)
        if not where:
            return self.list_favorites()
        sql = ("SELECT * FROM screenshot_history" + where +
               " ORDER BY updated_at DESC, id DESC LIMIT ?")
        return self.db.query(sql, tuple(params) + (limit,))
