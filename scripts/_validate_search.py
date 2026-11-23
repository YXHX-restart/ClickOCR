"""Validation: enhanced search in core_engine.db_manager.

Builds a synthetic 3000-row screenshot_history, then exercises:
  - multi-keyword space-separated AND (text on ocr_text + annotations)
  - annotation text participating (OCR text lacks the word, only annotations has it)
  - model tier filter (tiny / small / medium)
  - time range filter (today / dN / YYYY-MM-DD)
  - favorites-only search
and prints row counts + wall-clock timing for each.
"""
import os
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core_engine.db_manager import DBManager, HistoryStorage  # noqa: E402

WORDS = ["发票", "合同", "金额", "日期", "客户", "备注", "合计", "单价",
         "数量", "税率", "地址", "电话", "邮箱", "银行", "账号", "商品"]
ANNOT_TEXTS = ["红色圆圈标注", "箭头指向金额", "手写批注重要", "马赛克遮挡隐私",
               "序号1", "序号2", "重点标记", "待确认项"]


def main():
    tmp = tempfile.mkdtemp(prefix="clickocr_test_")
    db_path = os.path.join(tmp, "test.db")
    db = DBManager(db_path)
    db.init_db()  # vec0 may be unavailable; schema still built
    assert "screenshot_history" in db.table_names(), "schema not built"
    store = HistoryStorage(db, os.path.join(tmp, "screenshots"), max_records=100000)

    N = 3000
    rows = []
    for i in range(N):
        word = WORDS[i % len(WORDS)]
        ann_word = ANNOT_TEXTS[i % len(ANNOT_TEXTS)]
        # OCR text sometimes contains the word; annotations always JSON with a text field
        ocr = f"第{i}条 {word} 内容" if i % 3 == 0 else f"第{i}条 普通内容"
        ajson = (f'{{"version":1,"items":['
                 f'{{"type":"text","text":"{ann_word}"}},'
                 f'{{"type":"num","text":"{i}"}}]}}')
        tier = ["tiny", "small", "medium"][i % 3]
        fav = 1 if i % 7 == 0 else 0
        # spread created_at across last ~60 days deterministically (~30 min apart)
        created = time.time() - i * 1800
        rows.append((f"screenshots/{i}.png", ocr, "[]", tier, "cpu",
                     created, 1234, fav, ajson))
    t0 = time.time()
    store.db.executemany(
        """INSERT INTO screenshot_history
           (screenshot_path, ocr_text, ocr_boxes, model_tier, device,
            created_at, file_size, is_favorite, annotations, has_table, status)
           VALUES (?,?,?,?,?,?,?,?,?,0,0)""",
        rows)
    print(f"[seed] inserted {N} rows in {time.time()-t0:.3f}s", flush=True)

    # ---- Test 1: multi-keyword AND on ocr_text ----
    t0 = time.time()
    r1 = store.search("发票 合同")
    print(f"[t1] 多关键词AND(发票 合同) -> {len(r1)} rows  {time.time()-t0:.4f}s")

    # ---- Test 2: annotation text only (NOT in ocr_text) ----
    t0 = time.time()
    r2 = store.search("红色圆圈标注")
    # every 3rd record (i%3==0) is the matching annot index; i%8==0 -> ann_word[0]
    print(f"[t2] 标注文字(红色圆圈标注) -> {len(r2)} rows  {time.time()-t0:.4f}s")

    # ---- Test 3: model tier ----
    t0 = time.time()
    r3 = store.search("medium")
    print(f"[t3] 模型档位(medium) -> {len(r3)} rows  {time.time()-t0:.4f}s")

    # ---- Test 4: time range (d30 = last 30 days) ----
    t0 = time.time()
    r4 = store.search("d30")
    print(f"[t4] 时间范围(d30) -> {len(r4)} rows  {time.time()-t0:.4f}s")

    # ---- Test 5: combine text AND model AND time ----
    t0 = time.time()
    r5 = store.search("发票 medium d30")
    print(f"[t5] 组合(发票 medium d30) -> {len(r5)} rows  {time.time()-t0:.4f}s")

    # ---- Test 6: favorites-only search ----
    t0 = time.time()
    rf = store.search_favorites("medium")
    print(f"[t6] 收藏搜索(medium) -> {len(rf)} rows  {time.time()-t0:.4f}s")

    # ---- Sanity assertions ----
    assert all("medium" == x["model_tier"] for x in r3), "t3 model filter leaked"
    assert all("发票" in (x["ocr_text"] or "") or "发票" in (x["annotations"] or "")
               for x in r5), "t5 text filter leaked"
    print("\nALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
