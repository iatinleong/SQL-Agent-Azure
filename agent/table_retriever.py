"""表格語意檢索：用 BGE-M3 對 schema 欄位做 embedding，找出與 query 最相關的表格。

索引結構：每張表產生
  - 1 個表格層 chunk：表格名 + 表格中文名 + table_summary
  - N 個欄位層 chunk：表格名 + 欄位英文名 + 欄位中文名 + 欄位定義（短句）

每張表的分數 = 其所有 chunk 中 top-K 個 cosine score 的平均（max-K pooling）。
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Optional

import numpy as np

from .config import BASE_DIR
from .embedding import encode

_SCHEMA_PATH = BASE_DIR / "schema.csv"
_SUMMARIES_DIR = BASE_DIR / "table_summaries"
_CACHE_PATH = BASE_DIR / "table_chunks_embeddings.npz"
_USED_TABLES_PATH = BASE_DIR / "used_tables.txt"

_EXTRA_TABLES = {
    "S_ARIELSHAO.CUSTOMER_GROUP_2026Q1",
    "S_MELODYJJJIAN.CUSTOMER_GROUP_2026",
}

TOP_K_CHUNKS = 3  # 每張表取 top-K chunk 做平均

_chunk_index: Optional[tuple[list[tuple[str, str]], np.ndarray]] = None


def _load_target_tables() -> set[str]:
    tables: set[str] = set(_EXTRA_TABLES)
    with open(_SCHEMA_PATH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            t = row.get("表格名稱", "").strip()
            if t:
                tables.add(t)
    return tables


def _load_summaries() -> dict[str, str]:
    return {
        f.stem: f.read_text(encoding="utf-8").strip()
        for f in _SUMMARIES_DIR.glob("*.txt")
    }


def _build_chunks(target: set[str]) -> list[tuple[str, str]]:
    """回傳 [(table_name, chunk_text), ...]"""
    schema: dict[str, list[dict]] = defaultdict(list)
    table_cn: dict[str, str] = {}

    with open(_SCHEMA_PATH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            tname = row.get("表格名稱", "").strip()
            if not tname or tname not in target:
                continue
            if tname not in table_cn:
                table_cn[tname] = row.get("表格中文名稱", "").strip()
            schema[tname].append({
                "col":  row.get("欄位名稱", "").strip(),
                "cn":   row.get("欄位中文名稱", "").strip(),
                "defn": row.get("欄位定義說明", "").strip(),
            })

    summaries = _load_summaries()
    chunks: list[tuple[str, str]] = []

    for tname, cols in schema.items():
        summary = summaries.get(tname, "")
        cn_name = table_cn.get(tname, "")
        table_text = f"[表格] {tname} {cn_name} {summary}".strip()
        chunks.append((tname, table_text))

        for c in cols:
            parts = [f"[欄位] {tname}", c["col"]]
            if c["cn"]:
                parts.append(c["cn"])
            if c["defn"]:
                parts.append(c["defn"])
            chunks.append((tname, " ".join(parts)))

    return chunks


def _get_index() -> tuple[list[tuple[str, str]], np.ndarray]:
    """載入或建立 chunk embedding index（module-level cache）。"""
    global _chunk_index
    if _chunk_index is not None:
        return _chunk_index

    target = _load_target_tables()
    chunks = _build_chunks(target)
    chunk_ids = [f"{c[0]}||{i}" for i, c in enumerate(chunks)]

    if _CACHE_PATH.exists():
        cached = np.load(_CACHE_PATH, allow_pickle=False)
        if list(cached["ids"].tolist()) == chunk_ids:
            _chunk_index = (chunks, cached["vecs"])
            print(f"  [TableRetriever] 載入快取：{len(chunks)} chunks")
            return _chunk_index

    print(f"  [TableRetriever] 建立 index（{len(chunks)} chunks）...")
    vecs = encode(
        [c[1] for c in chunks],
        normalize_embeddings=True,
        batch_size=32,
        show_progress_bar=True,
    )
    np.savez(_CACHE_PATH, ids=np.array(chunk_ids), vecs=vecs)
    print(f"  [TableRetriever] 已存至 {_CACHE_PATH.name}")
    _chunk_index = (chunks, vecs)
    return _chunk_index


def retrieve_tables(
    query: str,
    top_n: int = 5,
    with_scores: bool = False,
) -> list[str] | list[tuple[str, float]]:
    """
    回傳與 query 最相關的前 top_n 張表格。
    with_scores=True 時回傳 [(table_name, score), ...]，否則只回傳 [table_name, ...]。
    """
    chunks, vecs = _get_index()
    query_vec = encode([query], normalize_embeddings=True)[0]
    scores = vecs @ query_vec  # cosine（已正規化）

    table_chunk_scores: dict[str, list[float]] = defaultdict(list)
    for i, (tname, _) in enumerate(chunks):
        table_chunk_scores[tname].append(float(scores[i]))

    table_scores = {
        tname: sum(sorted(sc, reverse=True)[:TOP_K_CHUNKS]) / min(len(sc), TOP_K_CHUNKS)
        for tname, sc in table_chunk_scores.items()
    }

    ranked = sorted(table_scores.items(), key=lambda x: x[1], reverse=True)[:top_n]
    if with_scores:
        return list(ranked)
    return [tname for tname, _ in ranked]
