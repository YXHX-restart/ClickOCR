"""表格检测纯算法模块（无 Qt 依赖，可 headless 测试）。

从 ui/result_overlay.py 剥离：像素线检测 / 坐标启发式聚类 / 格文本映射，
均为确定性纯函数（输入 → 输出），不依赖任何视图/场景状态。

职责边界：
- 本模块：判断"是不是表格" + 计算行列条带 + 格文本映射（算法）。
- result_overlay.py：持有 OCR boxes/原图，调用本模块，渲染网格场景条目与交互。
"""
from __future__ import annotations

import cv2


def _bbox(polygon):
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_center(polygon):
    x0, y0, x1, y1 = _bbox(polygon)
    return (x0 + x1) / 2.0, (y0 + y1) / 2.0


def build_blobs(boxes, box_order):
    """OCR 结果 → 文字块列表（仅保留非空文本，按 box_order 阅读顺序）。

    boxes: {orig: OcrBox}；box_order: 阅读顺序下的原始索引列表。
    返回 [(orig, x0, y0, x1, y1, txt), ...]
    """
    blobs = []
    for orig in box_order:
        b = boxes[orig]
        txt = (b.text or "").strip()
        if not txt:
            continue
        x0, y0, x1, y1 = _bbox(b.polygon)
        blobs.append((orig, x0, y0, x1, y1, txt))
    return blobs


def _detect_table_lines(img, line_ratio: float = 0.5, merge_gap: int = 4):
    """**像素级表格线检测**（纯图像，不依赖 OCR 结果，毫秒级）：

    - 灰度 → Otsu 二值化（表格线 = 贯穿整行/整列的暗像素长条）；
    - 行投影：暗像素占比 ≥ line_ratio 的连续行 → 一条水平线（取均值行）；
    - 列投影：同理得到竖线；相邻过近的线合并（粗线/抗锯齿多行）；
    - 横线 ≥ 2 且 竖线 ≥ 2 → 返回 `(rows, cols)` 条带（相邻线之间），
      否则返回 `None`（调用方回退文字坐标法）。

    rows = [(y0,y1), ...] 逐行条带；cols = [(x0,x1), ...] 逐列条带。
    文字行暗像素占比通常 < 0.35（本末列长文场景），0.5 阈值可稳定区分。
    """
    if img is None or img.size == 0:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, bw = cv2.threshold(gray, 0, 255,
                          cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    h, w = bw.shape
    row_ratio = bw.sum(axis=1) / 255.0 / max(1, w)
    col_ratio = bw.sum(axis=0) / 255.0 / max(1, h)

    def find_lines(ratio):
        lines = []
        run = []
        for i, r in enumerate(ratio):
            if r >= line_ratio:
                run.append(i)
            elif run:
                lines.append(int(round(sum(run) / len(run))))
                run = []
        if run:
            lines.append(int(round(sum(run) / len(run))))
        merged = []
        for ln in lines:
            if merged and ln - merged[-1] < merge_gap:
                merged[-1] = (merged[-1] + ln) // 2
            else:
                merged.append(ln)
        return merged

    hlines = find_lines(row_ratio)
    vlines = find_lines(col_ratio)
    if len(hlines) < 2 or len(vlines) < 2:
        return None
    rows = [(hlines[i], hlines[i + 1]) for i in range(len(hlines) - 1)]
    cols = [(vlines[i], vlines[i + 1]) for i in range(len(vlines) - 1)]
    return rows, cols


def _coords_table_grid(blobs):
    """**纯坐标启发式**（无网格线表格的回退通道）：

    - 行聚类：中心 y 排序，间隙 > 行高 0.6 为新行；
    - 列聚类：x 区间重叠合并（同列文字左边界对齐，x 区间重叠 → 同列；
      比"中心 x 差阈值"鲁棒——同列内宽窄不一的块不会因中心偏被误拆）；
    - 表格判定：行≥2 且 列≥2 且 ≥2 列有 ≥2 块文本对齐，且行距均匀
      （列宽不均已放宽，避免误杀窄列+宽末列的真实表格）。

    返回 `(rows, cols)` 条带（扩展到相邻组中点 ≈ 表格线位置）；非表格返回 None。
    blobs: [(orig, x0, y0, x1, y1, txt), ...]
    """
    from statistics import median
    h_med = median([t[4] - t[2] for t in blobs])
    w_med = median([t[3] - t[1] for t in blobs])
    # ---- 行聚类（纵向多行）：中心 y 排序，间隙 > 行高 0.6 为新行 ----
    row_groups: list[list] = []
    for blob in sorted(blobs, key=lambda t: (t[2] + t[4]) / 2.0):
        cy = (blob[2] + blob[4]) / 2.0
        if row_groups:
            gy = (row_groups[-1][0] + row_groups[-1][1]) / 2.0
            if cy - gy < h_med * 0.6:
                g = row_groups[-1]
                g[0] = min(g[0], blob[2])
                g[1] = max(g[1], blob[4])
                g[2].append(blob)
                continue
        row_groups.append([blob[2], blob[4], [blob]])
    # ---- 列聚类：x 区间重叠合并 ----
    col_groups: list[list] = []
    # 列合并阈值：合并同列内 x 接近的块，保留跨列间隙。
    # 原 max(4, w_med*0.2)≈24 在常见中文表格（列间隙 10~20px）下会把相邻
    # 列误并为同一列；改为 0.05 倍 w_med 后，列间隙 > ~9px 即可正确分隔。
    gap_min = max(4.0, w_med * 0.05)
    for blob in sorted(blobs, key=lambda t: t[1]):   # 按 x0 排序
        x0, x1 = blob[1], blob[3]
        if col_groups and x0 <= col_groups[-1][1] + gap_min:
            g = col_groups[-1]
            g[0] = min(g[0], x0)
            g[1] = max(g[1], x1)
            g[2].append(blob)
            continue
        col_groups.append([x0, x1, [blob]])
    # ---- 表格判定（纯坐标规则）----
    n_row, n_col = len(row_groups), len(col_groups)
    row_cy = [(g[0] + g[1]) / 2.0 for g in row_groups]
    col_counts = [len(g[2]) for g in col_groups]

    def gaps_ok(centers) -> bool:
        g = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
        if not g:
            return False
        m = sum(g) / len(g)
        return m > 0 and (max(g) - min(g)) / m < 0.4   # 间距稳定（行高均匀）

    aligned_cols = sum(1 for c in col_counts if c >= 2)  # 大量文本 x 对齐
    # 列宽不均时 col_cx 间距差异大，gaps_ok(col_cx) 会误杀真实表格
    # （典型如「前两列窄、末列装长文」的设备/接口表），故不再要求。
    is_table = (n_row >= 2 and n_col >= 2 and aligned_cols >= 2
                and gaps_ok(row_cy))
    if not is_table:
        return None                                      # 非表格
    rows_raw = [(g[0], g[1]) for g in row_groups]
    cols_raw = [(g[0], g[1]) for g in col_groups]
    # 条带扩展到相邻组中点（网格线落在文字间隙中间 ≈ 表格线位置）
    rows = []
    for i, (y0, y1) in enumerate(rows_raw):
        top, bottom = y0, y1
        if i > 0:
            top = (rows_raw[i - 1][1] + y0) / 2.0
        if i < len(rows_raw) - 1:
            bottom = (y1 + rows_raw[i + 1][0]) / 2.0
        rows.append((top, bottom))
    cols = []
    for i, (x0, x1) in enumerate(cols_raw):
        left, right = x0, x1
        if i > 0:
            left = (cols_raw[i - 1][1] + x0) / 2.0
        if i < len(cols_raw) - 1:
            right = (x1 + cols_raw[i + 1][0]) / 2.0
        cols.append((left, right))
    return rows, cols


def map_cell_texts(blobs, rows, cols):
    """文字块 → (行, 列) 格文本映射：块中心落入的行/列条带 → 拼接。

    rows: [(y0,y1), ...]；cols: [(x0,x1), ...]。
    返回 {(ri, ci): "格内文本"}（先按行 cy 再按列 cx 排序，换行格保持阅读顺序）。
    """
    cell_parts: dict[tuple[int, int], list[tuple[float, float, str]]] = {}
    for blob in blobs:
        cx = (blob[1] + blob[3]) / 2.0
        cy = (blob[2] + blob[4]) / 2.0
        ri = next((i for i, (y0, y1) in enumerate(rows)
                   if y0 <= cy <= y1), None)
        ci = next((i for i, (x0, x1) in enumerate(cols)
                   if x0 <= cx <= x1), None)
        if ri is None or ci is None:
            continue
        cell_parts.setdefault((ri, ci), []).append((cy, cx, blob[5]))
    return {key: "".join(t for _, _, t in sorted(parts))
            for key, parts in cell_parts.items()}
