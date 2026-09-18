"""Batch definition (decision 27): contiguous row ranges of dataset.parquet.

Shape of one batch, identical to what ``tpm.ingest.stream`` writes to ``batches.json`` (half-open ranges over
``__row__``: ``row_end`` is EXCLUSIVE, ``n_rows = row_end - row_start``):

    {"batch_id": "B0001", "row_start": 0, "row_end": 1000, "n_rows": 1000, "group_ids": ["1", "2"],
     "time_start": iso|None, "time_end": iso|None, "method": "time_window|row_fraction"}

* When the schema names a time column: windows of ``settings.batch.window_seconds`` (one DuckDB aggregation),
  merged/split to respect ``batch.min_rows`` / ``batch.max_rows``.
* Otherwise: ``settings.batch.fallback_fraction`` of the rows per batch (clamped to min/max rows).
* An existing batches.json (from the stream ingest) is reused as is; inclusive-end files are converted.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

from ._common import GROUP_COL, ROW_COL, quote_ident


def _iso(sec: Optional[float]) -> Optional[str]:
    if sec is None or not math.isfinite(sec):
        return None
    try:
        return datetime.fromtimestamp(float(sec), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def normalize_batches(raw: Any) -> list[dict[str, Any]]:
    """Accept batches.json written by anyone; return the canonical half-open list sorted by row_start."""
    out: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        raw = raw.get("batches", [])
    items = [b for b in (raw or []) if isinstance(b, dict) and "row_start" in b and "row_end" in b]
    items.sort(key=lambda b: int(b["row_start"]))
    inclusive = False
    for b in items:
        if b.get("n_rows") is not None and int(b["n_rows"]) == int(b["row_end"]) - int(b["row_start"]) + 1:
            inclusive = True
    if not inclusive and len(items) > 1:
        inclusive = all(int(nb["row_start"]) == int(b["row_end"]) + 1 for b, nb in zip(items, items[1:]))
    for i, b in enumerate(items):
        rs, re_ = int(b["row_start"]), int(b["row_end"]) + (1 if inclusive else 0)
        out.append({
            "batch_id": str(b.get("batch_id") or f"B{i + 1:04d}"),
            "row_start": rs,
            "row_end": re_,
            "n_rows": re_ - rs,
            "group_ids": [str(g) for g in (b.get("group_ids") or [])],
            "time_start": b.get("time_start"),
            "time_end": b.get("time_end"),
            "method": b.get("method", "unknown"),
        })
    return out


def _has_column(con: Any, name: str) -> bool:
    return any(r[0] == name for r in con.execute("DESCRIBE dataset").fetchall())


def _column_type(con: Any, name: str) -> str:
    for r in con.execute("DESCRIBE dataset").fetchall():
        if r[0] == name:
            return str(r[1]).upper()
    return ""


def _time_expr(con: Any, tcol: str, sample_period_seconds: Optional[float]) -> tuple[str, float]:
    """SQL expression giving the time column as a number, plus the multiplier that turns it into seconds."""
    typ = _column_type(con, tcol)
    c = quote_ident(tcol)
    if "TIMESTAMP" in typ or "DATE" in typ:
        return f"epoch(CAST({c} AS TIMESTAMP))", 1.0
    if any(t in typ for t in ("INT", "FLOAT", "DOUBLE", "DECIMAL", "REAL")):
        mult = 1.0
        if sample_period_seconds:
            try:  # numeric time: seconds if its median step matches the sample period, else sample units
                step = con.execute(f"SELECT median(d) FROM (SELECT {c} - lag({c}) OVER (ORDER BY {quote_ident(ROW_COL)}) AS d FROM (SELECT {c}, {quote_ident(ROW_COL)} FROM dataset USING SAMPLE 200000 ROWS)) WHERE d > 0").fetchone()[0]
                if step and abs(float(step) - float(sample_period_seconds)) > 1e-6 * max(1.0, float(sample_period_seconds)):
                    mult = float(sample_period_seconds) / float(step)
            except Exception:
                mult = 1.0
        return f"CAST({c} AS DOUBLE)", mult
    return f"epoch(TRY_CAST({c} AS TIMESTAMP))", 1.0


def _split_merge(ranges: list[dict[str, Any]], min_rows: int, max_rows: int) -> list[dict[str, Any]]:
    """Merge overlapping/tiny half-open ranges, then split oversized ones."""
    merged: list[dict[str, Any]] = []
    for r in sorted(ranges, key=lambda r: r["row_start"]):
        if merged and (r["row_start"] < merged[-1]["row_end"] or merged[-1]["n_rows"] < min_rows):
            m = merged[-1]
            m["row_end"] = max(m["row_end"], r["row_end"])
            m["n_rows"] = m["row_end"] - m["row_start"]
            ts = [x for x in (m["time_start"], r["time_start"]) if x is not None]
            te = [x for x in (m["time_end"], r["time_end"]) if x is not None]
            m["time_start"] = min(ts) if ts else None
            m["time_end"] = max(te) if te else None
        else:
            merged.append(dict(r))
    if len(merged) > 1 and merged[-1]["n_rows"] < min_rows:
        last = merged.pop()
        m = merged[-1]
        m["row_end"] = max(m["row_end"], last["row_end"])
        m["n_rows"] = m["row_end"] - m["row_start"]
        m["time_end"] = last["time_end"] if last["time_end"] is not None else m["time_end"]
    out: list[dict[str, Any]] = []
    for r in merged:
        if r["n_rows"] > max_rows:
            k = math.ceil(r["n_rows"] / max_rows)
            step = math.ceil(r["n_rows"] / k)
            for j in range(k):
                rs = r["row_start"] + j * step
                re_ = min(r["row_end"], rs + step)
                if rs >= re_:
                    break
                out.append({"row_start": rs, "row_end": re_, "n_rows": re_ - rs, "time_start": r["time_start"] if j == 0 else None, "time_end": r["time_end"] if j == k - 1 else None})
        else:
            out.append(r)
    return out


def _assign_groups(con: Any, batches: list[dict[str, Any]]) -> None:
    if not _has_column(con, GROUP_COL):
        return
    spans = con.execute(f"SELECT CAST({quote_ident(GROUP_COL)} AS VARCHAR), min({quote_ident(ROW_COL)}), max({quote_ident(ROW_COL)}) FROM dataset GROUP BY 1 ORDER BY 2").fetchall()
    for b in batches:
        b["group_ids"] = [str(g) for g, lo, hi in spans if lo is not None and not (hi < b["row_start"] or lo >= b["row_end"])]


def define_batches(ws: Any, settings: Any, force: bool = False) -> list[dict[str, Any]]:
    """Return the batch list, reusing batches.json when it already exists (e.g. written by the stream ingest)."""
    if not force and ws.exists("batches"):
        existing = normalize_batches(ws.read_json("batches"))
        if existing:
            return existing
    con = ws.duckdb()
    if not _has_column(con, ROW_COL):
        con.execute(f"CREATE OR REPLACE VIEW dataset AS SELECT row_number() OVER () - 1 AS {quote_ident(ROW_COL)}, * FROM read_parquet('{ws.path('dataset').as_posix()}')")
    n_rows = int(con.execute("SELECT count(*) FROM dataset").fetchone()[0])
    bcfg = settings.batch
    min_rows = max(1, int(bcfg.min_rows))
    max_rows = max(min_rows, int(bcfg.max_rows))
    schema = None
    try:
        schema = ws.schema()
    except Exception:
        schema = None
    tcol = getattr(schema, "time_column", None) if schema else None
    method = "row_fraction"
    ranges: list[dict[str, Any]] = []
    if tcol and _has_column(con, tcol) and n_rows > 0:
        try:
            expr, mult = _time_expr(con, tcol, getattr(schema, "sample_period_seconds", None))
            w = float(bcfg.window_seconds) / mult
            rc = quote_ident(ROW_COL)
            rows = con.execute(
                f"WITH t AS (SELECT {rc} AS r, {expr} AS ts FROM dataset), t0 AS (SELECT min(ts) AS m FROM t) "
                f"SELECT floor((ts - t0.m) / {w}) AS wid, min(r), max(r), count(*), min(ts), max(ts) FROM t, t0 WHERE ts IS NOT NULL GROUP BY wid ORDER BY 2"
            ).fetchall()
            for _wid, lo, hi, _cnt, t_lo, t_hi in rows:
                ranges.append({"row_start": int(lo), "row_end": int(hi) + 1, "n_rows": int(hi - lo + 1), "time_start": _iso(float(t_lo) * mult if t_lo is not None else None), "time_end": _iso(float(t_hi) * mult if t_hi is not None else None)})
            if ranges:  # rows with NULL time fall between windows; close the gaps so every row belongs to a batch
                ranges.sort(key=lambda r: r["row_start"])
                ranges[0]["row_start"] = 0
                for a, b in zip(ranges, ranges[1:]):
                    a["row_end"] = b["row_start"]
                    a["n_rows"] = a["row_end"] - a["row_start"]
                ranges[-1]["row_end"] = n_rows
                ranges[-1]["n_rows"] = ranges[-1]["row_end"] - ranges[-1]["row_start"]
                method = "time_window"
        except Exception:
            ranges = []
    if not ranges and n_rows > 0:
        per = int(round(n_rows * float(bcfg.fallback_fraction)))
        per = max(min_rows, min(max_rows, per))
        for start in range(0, n_rows, per):
            end = min(n_rows, start + per)
            ranges.append({"row_start": start, "row_end": end, "n_rows": end - start, "time_start": None, "time_end": None})
        method = "row_fraction"
    batches = _split_merge(ranges, min_rows, max_rows) if ranges else []
    for i, b in enumerate(batches):
        b["batch_id"] = f"B{i + 1:04d}"
        b.setdefault("group_ids", [])
    _assign_groups(con, batches)
    ordered = [{"batch_id": b["batch_id"], "row_start": b["row_start"], "row_end": b["row_end"], "n_rows": b["n_rows"], "group_ids": b.get("group_ids", []), "time_start": b.get("time_start"), "time_end": b.get("time_end"), "method": method} for b in batches]
    ws.write_json("batches", ordered)
    ev = ws.evidence.add("batching", f"{len(ordered)} batches defined by {method} over {n_rows} rows", values={"n_batches": len(ordered), "n_rows": n_rows, "method": method, "window_seconds": bcfg.window_seconds if method == "time_window" else None, "fallback_fraction": bcfg.fallback_fraction if method != "time_window" else None, "min_rows": min_rows, "max_rows": max_rows}, computed_by="quality.batches.define_batches", n_samples=n_rows)
    ws.log.record("system:quality", "batches", "batches", "batches.json", {"n_batches": len(ordered), "method": method}, [ev.id])
    return ordered


def load_batch_frame(ws: Any, batch: dict[str, Any], columns: Optional[list[str]] = None, row_start: Optional[int] = None, row_end: Optional[int] = None, keep_float64: Optional[set[str]] = None, stride: int = 1) -> Any:
    """Rows [row_start, row_end) of a batch as a pandas frame (float32 for floats), ordered by __row__.

    ``keep_float64`` names columns that must keep full precision (e.g. a numeric time column);
    ``stride`` > 1 keeps every stride-th row (time-budget fallback, recorded by the caller as evidence).
    """
    con = ws.duckdb()
    rs = batch["row_start"] if row_start is None else max(batch["row_start"], row_start)
    re_ = batch["row_end"] if row_end is None else min(batch["row_end"], row_end)
    present = [r[0] for r in con.execute("DESCRIBE dataset").fetchall()]
    cols = [c for c in (columns or present) if c in present]
    for extra in (ROW_COL, GROUP_COL):
        if extra in present and extra not in cols:
            cols.append(extra)
    sel = ", ".join(quote_ident(c) for c in cols)
    rc = quote_ident(ROW_COL)
    where = f"{rc} >= {int(rs)} AND {rc} < {int(re_)}"
    if stride > 1:
        where += f" AND ({rc} - {int(rs)}) % {int(stride)} = 0"
    df = con.execute(f"SELECT {sel} FROM dataset WHERE {where} ORDER BY {rc}").df()
    keep = keep_float64 or set()
    for c in df.columns:
        if c not in keep and str(df[c].dtype) == "float64":
            df[c] = df[c].astype("float32")
    return df
