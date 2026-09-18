"""Bounded, order-preserving sampling helpers over ``dataset.parquet`` (shared by ingest and profile).

Samples are unions of contiguous row chunks spread over the file, so local dynamics (autocorrelation,
hold periods, counters) survive, unlike random row sampling. Every sample carries a description that
callers record as evidence ("sampling").
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

from .readers import quote_ident


def chunk_plan(total_rows: int, n_target: int, n_chunks: int = 20, min_chunk: int = 50) -> list[tuple[int, int]]:
    """Evenly spaced contiguous [start, end) chunks covering about n_target rows."""
    total_rows = int(total_rows)
    if total_rows <= 0:
        return []
    if total_rows <= n_target:
        return [(0, total_rows)]
    n_chunks = max(1, min(n_chunks, n_target // min_chunk))
    chunk_len = max(min_chunk, n_target // n_chunks)
    if n_chunks == 1:
        return [(0, min(total_rows, chunk_len))]
    starts = np.linspace(0, total_rows - chunk_len, n_chunks)
    out: list[tuple[int, int]] = []
    last_end = -1
    for s in starts:
        a = int(round(s))
        if a < last_end:
            a = last_end
        b = min(total_rows, a + chunk_len)
        if b > a:
            out.append((a, b))
            last_end = b
    return out


def range_where(chunks: list[tuple[int, int]], col: str = "__row__") -> str:
    q = quote_ident(col)
    return " OR ".join(f"({q} >= {int(a)} AND {q} < {int(b)})" for a, b in chunks) or "TRUE"


def read_chunks(con, chunks: list[tuple[int, int]], columns: Optional[list[str]] = None, table: str = "dataset"):
    """pandas DataFrame with the requested columns for the chunk union, ordered by __row__, plus __chunk__."""
    cols = "*" if not columns else ", ".join(quote_ident(c) for c in (["__row__"] + [c for c in columns if c != "__row__"]))
    df = con.execute(f"SELECT {cols} FROM {table} WHERE {range_where(chunks)} ORDER BY __row__").df()
    if len(df):
        starts = np.array([a for a, _ in chunks], dtype=np.int64)
        df["__chunk__"] = np.searchsorted(starts, df["__row__"].to_numpy(dtype=np.int64), side="right") - 1
    else:
        df["__chunk__"] = np.array([], dtype=np.int64)
    return df


def sample_description(chunks: list[tuple[int, int]], total_rows: int) -> dict[str, Any]:
    n = int(sum(b - a for a, b in chunks))
    return {"method": "contiguous_chunks", "n_chunks": len(chunks), "n_rows": n, "total_rows": int(total_rows), "fraction": round(n / max(1, total_rows), 5), "chunk_rows": int(chunks[0][1] - chunks[0][0]) if chunks else 0}


def assign_blocks(rows: np.ndarray, blocks: list[tuple[int, int, str]]) -> np.ndarray:
    """Map row numbers to group ids given contiguous [start, end, id) blocks sorted by start."""
    if not blocks:
        return np.array(["0"] * len(rows), dtype=object)
    starts = np.array([b[0] for b in blocks], dtype=np.int64)
    ids = np.array([b[2] for b in blocks], dtype=object)
    idx = np.searchsorted(starts, rows.astype(np.int64), side="right") - 1
    idx = np.clip(idx, 0, len(blocks) - 1)
    return ids[idx]
