"""DBManager / HistoryStorage 的离线单测。

覆盖本轮审查修复里**曾经真出过错**的点，以及连接模型的行为契约：
  * 截图路径契约（存纯文件名、按自定义目录还原）
  * 删除顺序：先删库记录再删文件（避免「文件已没、记录还在」的破图）
  * `created_at` / `updated_at` 语义拆分（分组·搜索·FIFO 用前者，排序用后者）
  * 搜索不再命中标注 JSON 的**键名**
  * 连接模型：每线程长连接、`close()` 可重建、读不被写锁阻塞、写失败会回滚
"""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from core_engine.db_manager import DBManager, HistoryStorage
from ui.core.anno_core import anno_to_json      # 标注串一律用真实序列化器造

_IMG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"


class _TmpDb(unittest.TestCase):
    """每个用例一个临时库；tearDown 先 close（长连接会占住文件句柄，Windows 删不掉）。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self._dbs: list[DBManager] = []

    def tearDown(self):
        for d in self._dbs:
            d.close()
        self._td.cleanup()

    def db(self, name="t.db") -> DBManager:
        d = DBManager(self.tmp / name)
        d.init_db()
        self._dbs.append(d)
        return d

    def store(self, cap=50, shots=None) -> HistoryStorage:
        """未识别入库：ocr_text=""、ocr_boxes=None、档位留空（与拖入不识别一致）。"""
        return HistoryStorage(self.db(), shots or (self.tmp / "shots"),
                              max_records=cap)

    @staticmethod
    def save(hs, text="", boxes=None, tier="", device=""):
        return hs.save(_IMG, None, text, boxes, tier, device)


class TestPathContract(_TmpDb):
    def test_path_stored_as_bare_name_and_resolves_in_custom_dir(self):
        shots = self.tmp / "custom_shots"        # 自定义目录（原 bug 触发条件）
        hs = self.store(shots=shots)
        rid = self.save(hs)
        row = hs.get(rid)
        stored = row["screenshot_path"]
        self.assertNotIn("/", stored)
        self.assertNotIn("\\", stored)
        self.assertTrue((shots / stored).exists(), "按 screenshots_dir 应能还原到文件")

    def test_delete_from_custom_dir(self):
        shots = self.tmp / "custom_shots"
        hs = self.store(shots=shots)
        rid = self.save(hs)
        path = shots / hs.get(rid)["screenshot_path"]
        self.assertTrue(path.exists())
        hs.delete(rid)
        self.assertIsNone(hs.get(rid))
        self.assertFalse(path.exists())

    def test_list_paths_is_light(self):
        hs = self.store()
        self.save(hs)
        rows = hs.list_paths()
        self.assertEqual(len(rows[0]), 3, "去重索引只需 id / 文件名 / 大小 三列")


class TestDeleteOrder(_TmpDb):
    def test_delete_tolerates_missing_file(self):
        """文件被外部删掉时，删除记录仍应成功（不能因为文件没了就删不掉记录）。"""
        hs = self.store()
        rid = self.save(hs)
        (hs.screenshots_dir / hs.get(rid)["screenshot_path"]).unlink()
        hs.delete(rid)                            # 不抛
        self.assertIsNone(hs.get(rid))

    def test_delete_runs_after_clear(self):
        hs = self.store()
        for _ in range(3):
            self.save(hs)
        hs.clear()
        self.assertEqual(hs.count(), 0)
        self.assertEqual(len(list(hs.screenshots_dir.glob("*.png"))), 0)


class TestTimeSemantics(_TmpDb):
    def test_created_at_frozen_updated_at_moves(self):
        hs = self.store()
        old = self.save(hs, text="第一条")
        self.save(hs, text="第二条")
        first = hs.get(old)
        hs.touch(old)                             # 拖放去重命中 / 标注保存都会走它
        after = hs.get(old)
        self.assertEqual(first["created_at"], after["created_at"],
                         "created_at 落库后不得再被改写（否则分组/搜索/FIFO 全错位）")
        self.assertGreaterEqual(after["updated_at"], first["updated_at"])
        self.assertEqual(hs.list()[0]["id"], old, "最近操作过的应排在最前（按 updated_at）")

    def test_purge_uses_created_at_not_updated_at(self):
        """FIFO 淘汰按创建时间：刚标注过的**老图**不该因此豁免于清理。"""
        hs = self.store(cap=3)
        ids = [self.save(hs, text=f"第{i}条") for i in range(3)]
        hs.touch(ids[0])                          # 把最老的「变新」
        self.assertEqual(hs.list()[0]["id"], ids[0], "touch 后它确实排到了最前")
        self.save(hs, text="第四条")               # 触发 purge
        left = [r["id"] for r in hs.list()]
        self.assertNotIn(ids[0], left, "最老的仍应被淘汰（purge 按 created_at）")
        self.assertLessEqual(len(left), 3)


class TestSearch(_TmpDb):
    """搜索语义。**标注串一律用真实序列化器造**（`anno_to_json`）——

    手写 JSON 曾把真 bug 挡住：`db_manager` 里的路径写成 `$.marks`，而实际入库格式是
    `{"version":N,"items":[...]}`。对不存在的路径 `json_each` **静默返回 0 行**，
    于是「搜标注里的文字」彻底失效却毫无迹象；假数据恰好长得跟错路径一致，测试照过。
    """

    def test_search_annotation_text_value(self):
        hs = self.store()
        rid = self.save(hs, text="")
        hs.update_annotations(rid, anno_to_json(
            [{"type": "text", "x": 1, "y": 2, "text": "发票号码"}]))
        self.assertEqual(len(hs.search("发票号码")), 1, "标注 text 值应可检索")
        self.assertEqual(len(hs.search("不存在")), 0)

    def test_search_annotation_num_value(self):
        hs = self.store()
        rid = self.save(hs, text="")
        hs.update_annotations(rid, anno_to_json(
            [{"type": "num", "x": 1, "y": 2, "num": 7}]))
        self.assertEqual(len(hs.search("7")), 1, "标注 num 值应可检索")

    def test_search_ignores_annotation_json_keys(self):
        """JSON 的**键名**与 type/color 等值都不参与检索（只有 text/num 是用户可见文本）。"""
        hs = self.store()
        rid = self.save(hs, text="普通文字")
        hs.update_annotations(rid, anno_to_json(
            [{"type": "rect", "x0": 1, "y0": 1, "x1": 2, "y1": 2,
              "color": "#ff0000", "width": 2}]))
        for kw in ("items", "version", "marks", "rect", "color", "type", "points"):
            self.assertEqual(len(hs.search(kw)), 0,
                             f"搜 {kw!r} 不该命中（键名/非文本值）")

    def test_search_survives_invalid_json(self):
        """历史里的脏数据（非法 JSON）不得把整条搜索带崩。"""
        hs = self.store()
        rid = self.save(hs, text="")
        hs.update_annotations(rid, "这不是 JSON")
        self.assertEqual(len(hs.search("随便搜")), 0)   # 不抛异常

    def test_search_multi_keyword_is_and(self):
        hs = self.store()
        self.save(hs, text="东莞易锋钢材有限公司")
        self.assertEqual(len(hs.search("东莞 钢材")), 1)
        self.assertEqual(len(hs.search("东莞 不存在的词")), 0)


class TestConnectionModel(_TmpDb):
    def test_thread_local_reuse_and_close(self):
        d = self.db("conn.db")
        d.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        c1 = d._conn()
        self.assertIs(d._conn(), c1, "同线程应复用同一连接（不再每操作新建）")
        d.close()
        self.assertIsNot(d._conn(), c1, "close() 后应按需重建")
        d.close()                                 # 幂等：再调不抛

    def test_read_not_blocked_by_write_lock(self):
        """WAL 读写分离：写锁被占时另一线程的读仍应立即返回（原实现全串行）。"""
        d = self.db("con2.db")
        d.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        d.execute("INSERT INTO t (v) VALUES ('a')")
        got: dict = {}

        def _reader():
            try:
                got["rows"] = d.query("SELECT COUNT(*) AS c FROM t")
            finally:
                d.close()

        d._write_lock.acquire()
        try:
            th = threading.Thread(target=_reader)
            th.start()
            th.join(timeout=3)
            self.assertFalse(th.is_alive(), "读被写锁阻塞了（读写未分离）")
        finally:
            d._write_lock.release()
        self.assertEqual(got["rows"][0]["c"], 1)
        d.close()

    def test_worker_thread_has_own_connection(self):
        d = self.db("con3.db")
        d.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        main_id = id(d._conn())
        seen: dict = {}

        def _worker():
            try:
                seen["id"] = id(d._conn())
            finally:
                d.close()

        th = threading.Thread(target=_worker)
        th.start()
        th.join(timeout=3)
        self.assertNotEqual(seen["id"], main_id)

    def test_failed_write_rolls_back(self):
        """长连接下失败必须回滚：否则同线程后续读会看到未提交数据。"""
        d = self.db("con4.db")
        d.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        d.execute("INSERT INTO t (v) VALUES ('a')")
        with self.assertRaises(sqlite3.Error):
            d.execute("INSERT INTO nope (x) VALUES (1)")
        self.assertFalse(d._conn().in_transaction, "连接卡在事务里了（未回滚）")
        self.assertEqual(d.query_one("SELECT COUNT(*) AS c FROM t")["c"], 1)
        self.assertEqual(d.execute("UPDATE t SET v='b'"), 1)

    def test_concurrent_writes_all_land(self):
        """多线程并发写：靠写锁 + busy_timeout 全部落库，不丢不串。"""
        d = self.db("con5.db")
        d.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")

        def _writer(n):
            try:
                for i in range(10):
                    d.execute("INSERT INTO t (v) VALUES (?)", (f"{n}-{i}",))
            finally:
                d.close()

        ths = [threading.Thread(target=_writer, args=(n,)) for n in range(4)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(timeout=10)
        d.close()
        self.assertEqual(d.query_one("SELECT COUNT(*) AS c FROM t")["c"], 40)


class TestSchemaMigration(_TmpDb):
    def test_old_db_gets_updated_at_backfilled(self):
        """旧库（无 updated_at 列）→ init_db 应就地补列并回填，幂等。"""
        p = self.tmp / "old.db"
        conn = sqlite3.connect(str(p))
        conn.execute(
            "CREATE TABLE screenshot_history (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " screenshot_path TEXT NOT NULL, ocr_text TEXT, ocr_boxes TEXT,"
            " model_tier TEXT NOT NULL, device TEXT NOT NULL, created_at REAL NOT NULL,"
            " file_size INTEGER, is_favorite INTEGER DEFAULT 0, annotations TEXT,"
            " has_table INTEGER DEFAULT 0, status INTEGER DEFAULT 0)")
        for ts in (1000.0, 2000.0):
            conn.execute(
                "INSERT INTO screenshot_history (screenshot_path, ocr_text,"
                " ocr_boxes, model_tier, device, created_at, file_size,"
                " is_favorite, annotations, has_table, status)"
                " VALUES (?,?,?,?,?,?,?,0,NULL,0,0)",
                (f"old{ts}.png", "", None, "", "", ts, 0))
        conn.commit()
        conn.close()

        d = DBManager(p)
        d.init_db()
        self._dbs.append(d)
        rows = d.query("SELECT created_at, updated_at FROM screenshot_history")
        self.assertTrue(all("updated_at" in r.keys() for r in rows))
        self.assertTrue(all(r["updated_at"] == r["created_at"] for r in rows),
                        "旧库的 updated_at 应由 created_at 回填（既有排序行为不变）")
        d.init_db()                               # 幂等：再跑一次不报错、不改数据
        rows2 = d.query("SELECT created_at, updated_at FROM screenshot_history")
        self.assertEqual([r["updated_at"] for r in rows], [r["updated_at"] for r in rows2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
