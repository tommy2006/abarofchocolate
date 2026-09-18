"""Dataset schema inference: column typing, timestamp & period, counters, grouping (scored strategies),
label/meta detection, domain likelihood, ``__group__`` materialization and schema overrides.

Everything is computed on ``dataset.parquet`` through DuckDB (aggregates on the full data) plus a bounded
sample of contiguous row chunks (dynamics). Every decision produces Evidence + an Inference; operator
input is never requested: unknowns become recorded assumptions.

Public functions
    infer_schema(ws, settings, ctx, conv, fmt, progress) -> DatasetSchema
    materialize_groups(ws, settings, blocks=None, expr=None, casts=None, progress=None) -> None
    apply_override(ws, settings, decision) -> dict
"""
from __future__ import annotations

import math
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from ..contracts import DatasetSchema, GroupingCandidate
from ..memory import chunk_rows as mem_chunk_rows
from .readers import quote_ident, sql_lit
from .sample import assign_blocks, chunk_plan, read_chunks, sample_description

STAGE = "ingest"
ACTOR = "system:ingest"
NUMERIC_TYPES = {"FLOAT", "DOUBLE", "REAL", "BIGINT", "INTEGER", "SMALLINT", "TINYINT", "HUGEINT", "UBIGINT", "UINTEGER", "USMALLINT", "UTINYINT", "DECIMAL"}
INT_TYPES = {"BIGINT", "INTEGER", "SMALLINT", "TINYINT", "HUGEINT", "UBIGINT", "UINTEGER", "USMALLINT", "UTINYINT"}
NAME_LABEL_RE = re.compile(r"(label|fault|class|target|status|anomal|categor|_type$|^type$|defect|failure|outcome)", re.I)
NAME_META_RE = re.compile(r"(^id$|_id$|^index$|^idx$|^run|_run$|batch|source|split|^set$|file|^name$|uuid|guid|^key$)", re.I)
NAME_TIME_RE = re.compile(r"(time|date|stamp|^ts$|epoch)", re.I)
STRPTIME_FORMATS = ["%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M", "%Y%m%d%H%M%S", "%Y/%m/%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S,%f", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"]
ProgressFn = Optional[Callable[[float, str], None]]


def _prog(progress: ProgressFn, frac: float, msg: str) -> None:
    if progress:
        try:
            progress(float(frac), msg)
        except Exception:
            pass


def _finite(x: Any, default: Any = None) -> Any:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


# --------------------------------------------------------------------------------------------------
# column typing
# --------------------------------------------------------------------------------------------------
def _type_column(name: str, s: pd.Series, ptype: str, chunk: np.ndarray, con=None) -> dict[str, Any]:
    """Structural type of a column from the sample. Returns a dict with kind and statistics."""
    ptype = (ptype or "").upper()
    n = int(len(s))
    nn = s.dropna()
    info: dict[str, Any] = {"name": name, "parquet_type": ptype, "n_sample": n, "missing_rate": float(1 - len(nn) / n) if n else 1.0, "cast": None, "n_unique": int(nn.nunique()) if len(nn) else 0, "integer_valued": False, "low_cardinality": False, "epoch_unit": None}
    if len(nn) == 0:
        info["kind"] = "empty"
        return info
    if info["n_unique"] == 1:
        info["kind"] = "constant"
        info["numeric"] = ptype.split("(")[0] in NUMERIC_TYPES
        return info
    base = ptype.split("(")[0]
    if base in ("TIMESTAMP", "DATE", "TIMESTAMP WITH TIME ZONE", "TIMESTAMPTZ", "TIMESTAMP_NS", "TIMESTAMP_MS", "TIMESTAMP_S"):
        info["kind"] = "datetime"
        return info
    if base == "BOOLEAN":
        info["kind"] = "boolean"
        return info
    if base in NUMERIC_TYPES:
        vals = nn.to_numpy(dtype="float64")
        vals = vals[np.isfinite(vals)]
        if len(vals) == 0:
            info["kind"] = "numeric"
            return info
        int_valued = bool(np.all(np.abs(vals - np.round(vals)) < 1e-9))
        info["integer_valued"] = int_valued
        info["low_cardinality"] = info["n_unique"] <= max(2, min(30, int(0.05 * n)))
        if int_valued and set(np.unique(vals).tolist()) <= {0.0, 1.0}:
            info["kind"] = "boolean"
            return info
        mx = float(np.max(vals))
        mn = float(np.min(vals))
        if int_valued or base in INT_TYPES:
            # epoch-like: seconds since 1990 .. 2100, or milliseconds / nanoseconds; mostly non-decreasing within chunks
            unit = None
            if 6.3e8 <= mn and mx <= 4.2e9:
                unit = "s"
            elif 6.3e11 <= mn and mx <= 4.2e12:
                unit = "ms"
            elif 6.3e17 <= mn and mx <= 4.2e18:
                unit = "ns"
            if unit and info["n_unique"] > 10:
                d = np.diff(vals)
                if len(d) and float(np.mean(d >= 0)) >= 0.9:
                    info["kind"] = "datetime"
                    info["epoch_unit"] = unit
                    info["cast"] = f"to_timestamp(CAST({quote_ident(name)} AS DOUBLE) / {1 if unit == 's' else (1e3 if unit == 'ms' else 1e9)})"
                    return info
            info["kind"] = "integer"
            return info
        info["kind"] = "numeric"
        return info
    # ---- strings
    st = nn.astype(str).str.strip()
    st = st[st != ""]
    if len(st) == 0:
        info["kind"] = "empty"
        return info
    info["n_unique"] = int(st.nunique())
    num_dot = pd.to_numeric(st, errors="coerce")
    rate_dot = float(num_dot.notna().mean())
    num_comma = pd.to_numeric(st.str.replace(",", ".", regex=False), errors="coerce") if rate_dot < 0.95 else num_dot
    rate_comma = float(num_comma.notna().mean())
    if max(rate_dot, rate_comma) >= 0.95:
        vals = (num_dot if rate_dot >= rate_comma else num_comma).dropna().to_numpy(dtype="float64")
        info["kind"] = "numeric"
        info["integer_valued"] = bool(np.all(np.abs(vals - np.round(vals)) < 1e-9)) if len(vals) else False
        info["low_cardinality"] = info["n_unique"] <= max(2, min(30, int(0.05 * n)))
        q = quote_ident(name)
        info["cast"] = f"TRY_CAST({q} AS DOUBLE)" if rate_dot >= rate_comma else f"TRY_CAST(replace({q}, ',', '.') AS DOUBLE)"
        info["parse_rate"] = max(rate_dot, rate_comma)
        return info
    # datetime-like strings
    head = st.iloc[: min(len(st), 5000)]
    try:
        parsed = pd.to_datetime(head, errors="coerce", format="mixed")
        rate_dt = float(parsed.notna().mean())
    except Exception:
        rate_dt = 0.0
    if rate_dt >= 0.95 and head.str.len().median() >= 6 and head.str.contains(r"\d", regex=True).mean() > 0.95:
        info["kind"] = "datetime"
        info["parse_rate"] = rate_dt
        info["cast"] = _datetime_cast(name, con)
        if info["cast"] is None:
            info["kind"] = "text"
            info["note"] = "datetime-like strings but no SQL parse format found; kept as text"
        return info
    frac_unique = info["n_unique"] / max(1, len(st))
    if frac_unique >= 0.9 and len(st) >= 50:
        info["kind"] = "identifier"
    elif info["n_unique"] <= max(20, int(0.02 * len(st))):
        info["kind"] = "categorical"
        info["low_cardinality"] = True
    else:
        info["kind"] = "text"
    return info


def _datetime_cast(name: str, con) -> Optional[str]:
    """SQL expression that parses a VARCHAR column as TIMESTAMP with >= 95 % success on a sample."""
    if con is None:
        return f"TRY_CAST({quote_ident(name)} AS TIMESTAMP)"
    q = quote_ident(name)
    cands = [f"TRY_CAST({q} AS TIMESTAMP)", f"TRY_CAST(TRY_CAST({q} AS TIMESTAMPTZ) AS TIMESTAMP)"] + [f"try_strptime({q}, {sql_lit(f)})" for f in STRPTIME_FORMATS]
    sample = f"(SELECT {q} FROM dataset WHERE {q} IS NOT NULL LIMIT 2000)"
    for expr in cands:
        try:
            ok, tot = con.execute(f"SELECT count({expr}), count(*) FROM {sample}").fetchone()
            if tot and ok / tot >= 0.95:
                return expr
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------------------------------
# time and counters
# --------------------------------------------------------------------------------------------------
def _within_chunk_diffs(values: np.ndarray, chunk: np.ndarray) -> np.ndarray:
    d = np.diff(values)
    same = chunk[1:] == chunk[:-1]
    return d[same]


def _time_stats(ts: pd.Series, chunk: np.ndarray) -> dict[str, Any]:
    t = pd.to_datetime(ts, errors="coerce")
    sec = t.to_numpy(dtype="datetime64[ns]").astype("int64") / 1e9
    sec = np.where(t.notna().to_numpy(), sec, np.nan)
    d = _within_chunk_diffs(sec, chunk)
    d = d[np.isfinite(d)]
    if len(d) == 0:
        return {"monotone": 0.0, "period": None, "irregular": 1.0, "n_diffs": 0}
    pos = d[d > 0]
    period = float(np.median(pos)) if len(pos) else None
    irregular = float(np.mean(np.abs(d - period) > 0.5 * period)) if period else 1.0
    return {"monotone": float(np.mean(d >= 0)), "period": period, "irregular": irregular, "n_diffs": int(len(d)), "zero_diffs": float(np.mean(d == 0)), "negative_diffs": float(np.mean(d < 0))}


def _counter_stats(values: np.ndarray, chunk: np.ndarray) -> dict[str, Any]:
    d = _within_chunk_diffs(values.astype("float64"), chunk)
    d = d[np.isfinite(d)]
    if len(d) < 5:
        return {"step_fraction": 0.0, "step": None, "resets": 0, "n_diffs": int(len(d))}
    pos = d[d > 0]
    if len(pos) == 0:
        return {"step_fraction": 0.0, "step": None, "resets": int(np.sum(d < 0)), "n_diffs": int(len(d))}
    vals, counts = np.unique(pos, return_counts=True)
    step = float(vals[np.argmax(counts)])
    return {"step_fraction": float(np.mean(np.abs(d - step) < 1e-9)), "step": step, "resets": int(np.sum(d < 0)), "zero_fraction": float(np.mean(d == 0)), "n_diffs": int(len(d)), "monotone": float(np.mean(d >= 0))}


# --------------------------------------------------------------------------------------------------
# grouping strategies
# --------------------------------------------------------------------------------------------------
def _regularity(lengths: np.ndarray) -> float:
    if len(lengths) < 2:
        return 1.0
    med = float(np.median(lengths))
    if med <= 0:
        return 0.0
    mad = float(np.median(np.abs(lengths - med)))
    return float(max(0.0, 1.0 - min(1.0, 1.4826 * mad / med)))


def _plausibility(n_groups: int, median_len: float, n_rows: int) -> float:
    if n_groups < 2 or median_len < 3:
        return 0.0
    p = 1.0
    if median_len < 20:
        p *= median_len / 20.0
    if n_groups > n_rows / 10:
        p *= 0.3
    return float(p)


def _key_expr(cols: list[str]) -> str:
    parts = [f"COALESCE(CAST({quote_ident(c)} AS VARCHAR), 'null')" for c in cols]
    return parts[0] if len(parts) == 1 else "concat_ws('|', " + ", ".join(parts) + ")"


def _key_groupby(con, cols: list[str]) -> pd.DataFrame:
    expr = _key_expr(cols)
    return con.execute(f"SELECT {expr} AS key, min(__row__) AS s, max(__row__) AS e, count(*) AS n FROM dataset GROUP BY 1 ORDER BY s").df()


def _blocks_from_window(con, expr: str) -> list[tuple[int, int, str]]:
    """Exact contiguous blocks of a key expression (one sort of (key,__row__) on the full data)."""
    df = con.execute(
        f"SELECT key, min(__row__) AS s, max(__row__) AS e FROM (SELECT key, __row__, sum(chg) OVER (ORDER BY __row__ ROWS UNBOUNDED PRECEDING) AS blk FROM "
        f"(SELECT {expr} AS key, __row__, CASE WHEN {expr} IS DISTINCT FROM lag({expr}) OVER (ORDER BY __row__) THEN 1 ELSE 0 END AS chg FROM dataset)) GROUP BY blk, key ORDER BY s"
    ).df()
    return [(int(r.s), int(r.e) + 1, str(r.key)) for r in df.itertuples()]


def _sample_key_alignment(sample: pd.DataFrame, key_cols: list[str], counter_col: Optional[str]) -> float:
    """Agreement between key changes and counter resets inside the sample chunks (0..1; 0.5 if no counter)."""
    if not counter_col or counter_col not in sample.columns:
        return 0.5
    chunk = sample["__chunk__"].to_numpy()
    same = chunk[1:] == chunk[:-1]
    key = sample[key_cols].astype(str).agg("|".join, axis=1).to_numpy() if len(key_cols) > 1 else sample[key_cols[0]].astype(str).to_numpy()
    kchg = (key[1:] != key[:-1]) & same
    c = sample[counter_col].to_numpy(dtype="float64")
    reset = (np.diff(c) < 0) & same
    if kchg.sum() == 0 and reset.sum() == 0:
        return 0.5
    a = float(np.mean(reset[kchg])) if kchg.sum() else 0.0
    b = float(np.mean(kchg[reset])) if reset.sum() else 0.0
    return float((a + b) / 2)


def _evaluate_key_candidate(con, cols: list[str], n_rows: int, sample: pd.DataFrame, counter_col: Optional[str]) -> dict[str, Any]:
    gb = _key_groupby(con, cols)
    n_groups = int(len(gb))
    contiguous = (gb["n"] == (gb["e"] - gb["s"] + 1)).to_numpy()
    contiguity = float(gb.loc[contiguous, "n"].sum() / max(1, n_rows))
    lengths = gb["n"].to_numpy(dtype="float64")
    median_len = float(np.median(lengths)) if len(lengths) else 0.0
    regularity = _regularity(lengths)
    plaus = _plausibility(n_groups, median_len, n_rows)
    align = _sample_key_alignment(sample, cols, counter_col)
    if counter_col:
        score = (0.4 * contiguity + 0.2 * regularity + 0.25 * align + 0.15) * plaus
    else:
        score = (0.6 * contiguity + 0.25 * regularity + 0.15) * plaus
    blocks = [(int(r.s), int(r.e) + 1, str(r.key)) for r in gb.itertuples()] if bool(contiguous.all()) else None
    return {"method": "key_columns", "columns": list(cols), "n_groups": n_groups, "score": round(float(score), 4), "contiguity": round(contiguity, 4), "regularity": round(regularity, 4), "counter_alignment": round(align, 4), "plausibility": round(plaus, 4), "median_len": median_len, "min_len": float(lengths.min()) if len(lengths) else 0, "max_len": float(lengths.max()) if len(lengths) else 0, "blocks": blocks, "all_contiguous": bool(contiguous.all()), "expr": _key_expr(cols)}


def _evaluate_counter_candidate(con, counter_col: str, n_rows: int, sample: pd.DataFrame, best_key: Optional[dict[str, Any]]) -> dict[str, Any]:
    q = quote_ident(counter_col)
    resets = con.execute(f"SELECT __row__ FROM (SELECT __row__, {q} AS c, lag({q}) OVER (ORDER BY __row__) AS p FROM dataset) WHERE c < p ORDER BY __row__").df()["__row__"].to_numpy(dtype=np.int64)
    starts = np.concatenate([[0], resets])
    ends = np.concatenate([resets, [n_rows]])
    lengths = (ends - starts).astype("float64")
    n_groups = int(len(starts))
    median_len = float(np.median(lengths)) if len(lengths) else 0.0
    regularity = _regularity(lengths)
    plaus = _plausibility(n_groups, median_len, n_rows)
    align = 0.5
    if best_key is not None:
        align = _sample_key_alignment(sample, best_key["columns"], counter_col)
    score = (0.35 + 0.25 * regularity + 0.25 * align + 0.15) * plaus * 0.9  # counter resets carry no ids: slight penalty
    width = max(2, len(str(n_groups)))
    blocks = [(int(a), int(b), f"G{i + 1:0{width}d}") for i, (a, b) in enumerate(zip(starts, ends))]
    return {"method": "counter_reset", "columns": [counter_col], "n_groups": n_groups, "score": round(float(score), 4), "regularity": round(regularity, 4), "counter_alignment": round(align, 4), "plausibility": round(plaus, 4), "median_len": median_len, "min_len": float(lengths.min()), "max_len": float(lengths.max()), "blocks": blocks, "expr": None}


def _evaluate_time_gap_candidate(con, time_expr: str, period: float, n_rows: int, gap_factor: float = 10.0) -> Optional[dict[str, Any]]:
    thr = max(period * gap_factor, 1e-6)
    try:
        df = con.execute(f"SELECT __row__ FROM (SELECT __row__, epoch({time_expr}) AS t, lag(epoch({time_expr})) OVER (ORDER BY __row__) AS p FROM dataset) WHERE t - p > {thr} OR t - p < -{thr} ORDER BY __row__").df()
    except Exception:
        return None
    gaps = df["__row__"].to_numpy(dtype=np.int64)
    if len(gaps) == 0:
        return None
    starts = np.concatenate([[0], gaps])
    ends = np.concatenate([gaps, [n_rows]])
    lengths = (ends - starts).astype("float64")
    n_groups = int(len(starts))
    median_len = float(np.median(lengths))
    regularity = _regularity(lengths)
    plaus = _plausibility(n_groups, median_len, n_rows)
    score = (0.3 + 0.3 * regularity + 0.15) * plaus * 0.8
    width = max(2, len(str(n_groups)))
    blocks = [(int(a), int(b), f"T{i + 1:0{width}d}") for i, (a, b) in enumerate(zip(starts, ends))]
    return {"method": "time_gaps", "columns": [], "n_groups": n_groups, "score": round(float(score), 4), "regularity": round(regularity, 4), "plausibility": round(plaus, 4), "median_len": median_len, "min_len": float(lengths.min()), "max_len": float(lengths.max()), "blocks": blocks, "expr": None, "gap_threshold_s": thr}


def _evaluate_changepoint_candidate(con, cont_cols: list[str], n_rows: int) -> Optional[dict[str, Any]]:
    if not cont_cols or n_rows < 200:
        return None
    try:
        import ruptures as rpt
    except Exception:
        return None
    n_win = int(min(1500, max(50, n_rows // 20)))
    w = max(1, n_rows // n_win)
    cols = cont_cols[:30]
    aggs = ", ".join(f"avg({quote_ident(c)}) AS {quote_ident(c)}" for c in cols)
    df = con.execute(f"SELECT floor(__row__ / {w}) AS win, {aggs} FROM dataset GROUP BY win ORDER BY win").df()
    X = df[cols].to_numpy(dtype="float64")
    X = np.where(np.isfinite(X), X, np.nan)
    mu = np.nanmean(X, axis=0)
    sd = np.nanstd(X, axis=0)
    ok = np.isfinite(sd) & (sd > 0)
    if not ok.any():
        return None
    Z = (X[:, ok] - mu[ok]) / sd[ok]
    Z = np.where(np.isfinite(Z), Z, 0.0)
    Z = np.clip(Z, -6, 6)
    try:
        algo = rpt.Pelt(model="rbf", min_size=3, jump=1).fit(Z)
        bkps = algo.predict(pen=3.0 * math.log(len(Z)))
    except Exception:
        return None
    bkps = [b for b in bkps if 0 < b < len(Z)]
    if not bkps or len(bkps) > 300:
        return None
    starts = np.concatenate([[0], np.array(bkps) * w]).astype(np.int64)
    ends = np.concatenate([np.array(bkps) * w, [n_rows]]).astype(np.int64)
    lengths = (ends - starts).astype("float64")
    n_groups = int(len(starts))
    median_len = float(np.median(lengths))
    regularity = _regularity(lengths)
    plaus = _plausibility(n_groups, median_len, n_rows)
    score = (0.2 + 0.2 * regularity) * plaus  # capped low: segmentation without ids is a weak grouping
    width = max(2, len(str(n_groups)))
    blocks = [(int(a), int(b), f"C{i + 1:0{width}d}") for i, (a, b) in enumerate(zip(starts, ends))]
    return {"method": "changepoint", "columns": [], "n_groups": n_groups, "score": round(float(score), 4), "regularity": round(regularity, 4), "plausibility": round(plaus, 4), "median_len": median_len, "min_len": float(lengths.min()), "max_len": float(lengths.max()), "blocks": blocks, "expr": None, "window_rows": w}


def _candidate_rationale(c: dict[str, Any]) -> str:
    bits = [f"{c['method']}"]
    if c.get("columns"):
        bits.append("columns=" + ",".join(c["columns"]))
    bits.append(f"n_groups={c['n_groups']}")
    for k in ("contiguity", "regularity", "counter_alignment", "plausibility"):
        if k in c:
            bits.append(f"{k}={c[k]:.2f}")
    if "median_len" in c:
        bits.append(f"rows/group median={int(c['median_len'])} [{int(c.get('min_len', 0))}..{int(c.get('max_len', 0))}]")
    return "; ".join(bits)


# --------------------------------------------------------------------------------------------------
# materialization
# --------------------------------------------------------------------------------------------------
def _reset_duck(ws) -> None:
    """Close the shared DuckDB connection so Windows lets us replace dataset.parquet if needed."""
    try:
        with ws._lock:
            if ws._duck is not None:
                ws._duck.close()
                ws._duck = None
    except Exception:
        pass


def materialize_groups(ws, settings, blocks: Optional[list[tuple[int, int, str]]] = None, expr: Optional[str] = None, casts: Optional[dict[str, str]] = None, progress: ProgressFn = None) -> dict[str, Any]:
    """Rewrite dataset.parquet with a ``__group__`` VARCHAR column (and optional column casts) preserving
    ``__row__`` order. Uses a DuckDB projection when the group id is a scalar expression, otherwise a
    chunked pyarrow rewrite over contiguous row blocks (bounded memory)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    con = ws.duckdb()
    ds = ws.path("dataset")
    tmp = ds.with_suffix(".parquet.tmp")
    cols = [r[0] for r in con.execute("DESCRIBE dataset").fetchall() if r[0] != "__group__"]
    casts = casts or {}
    sel = ", ".join(f"{casts[c]} AS {quote_ident(c)}" if c in casts else quote_ident(c) for c in cols)
    n_rows = int(con.execute("SELECT count(*) FROM dataset").fetchone()[0])
    t0 = time.time()
    used = "projection"
    if tmp.exists():
        tmp.unlink()
    ok = False
    if expr is not None:
        try:
            con.execute(f"COPY (SELECT {sel}, {expr} AS __group__ FROM dataset) TO {sql_lit(tmp.as_posix())} (FORMAT PARQUET, COMPRESSION {settings.ingest.parquet_compression})")
            bad = con.execute(f"SELECT count(*) FROM (SELECT __row__, row_number() OVER () - 1 AS rn FROM read_parquet({sql_lit(tmp.as_posix())})) WHERE __row__ <> rn").fetchone()[0]
            ok = int(bad) == 0
            if not ok:
                tmp.unlink()
        except Exception:
            ok = False
    if not ok:
        used = "chunked"
        blocks = blocks or [(0, n_rows, "0")]
        step = max(50_000, min(int(settings.ingest.chunk_rows), mem_chunk_rows(len(cols), bytes_per_value=4)))
        writer = None
        try:
            for a in range(0, n_rows, step):
                b = min(n_rows, a + step)
                res = con.execute(f"SELECT {sel} FROM dataset WHERE __row__ >= {a} AND __row__ < {b} ORDER BY __row__")
                tbl = res.to_arrow_table() if hasattr(res, "to_arrow_table") else res.fetch_arrow_table()
                rows = tbl.column("__row__").to_numpy()
                gid = assign_blocks(rows, blocks)
                tbl = tbl.append_column("__group__", pa.array([str(x) for x in gid], type=pa.string()))
                if writer is None:
                    writer = pq.ParquetWriter(tmp, tbl.schema, compression=settings.ingest.parquet_compression)
                writer.write_table(tbl)
                _prog(progress, b / n_rows, "materializing groups")
        finally:
            if writer is not None:
                writer.close()
    for attempt in range(5):
        try:
            os.replace(tmp, ds)
            break
        except PermissionError:
            _reset_duck(ws)
            time.sleep(0.5)
            if attempt == 4:
                raise
    ws.duckdb()  # refresh the view
    return {"method": used, "seconds": round(time.time() - t0, 2), "n_rows": n_rows}


# --------------------------------------------------------------------------------------------------
# main inference
# --------------------------------------------------------------------------------------------------
def infer_schema(ws, settings, ctx: dict[str, Any], conv: dict[str, Any], fmt: dict[str, Any], progress: ProgressFn = None) -> DatasetSchema:
    options = dict(ctx.get("options") or {})
    con = ws.duckdb()
    ev = ws.evidence
    inf = ws.inferences
    evidence_ids: list[str] = []
    inference_ids: list[str] = []
    assumptions: list[str] = []
    n_rows = int(conv["n_rows"])
    columns = [c for c in conv["columns"] if c not in ("__row__", "__group__")]
    ptypes = {k: v for k, v in conv.get("parquet_types", {}).items()}
    had_header = bool(fmt.get("has_header", True))
    blind = bool(settings.ingest.blind_mode)

    # ---------------- sample
    _prog(progress, 0.0, "sampling rows for typing")
    chunks = chunk_plan(n_rows, int(settings.ingest.sample_rows_for_typing), n_chunks=20)
    sample = read_chunks(con, chunks, columns)
    chunk_idx = sample["__chunk__"].to_numpy()
    sdesc = sample_description(chunks, n_rows)
    e = ev.add("sampling", f"typing sample: {sdesc['n_rows']} rows in {sdesc['n_chunks']} contiguous chunks ({sdesc['fraction']:.1%} of {n_rows} rows)", values=sdesc, computed_by="ingest.schema.sample", n_samples=sdesc["n_rows"])
    evidence_ids.append(e.id)

    # ---------------- typing
    _prog(progress, 0.1, "typing columns")
    types: dict[str, dict[str, Any]] = {}
    for i, c in enumerate(columns):
        types[c] = _type_column(c, sample[c], ptypes.get(c, ""), chunk_idx, con)
        types[c]["index"] = i
    kinds = {c: t["kind"] for c, t in types.items()}
    kind_counts = dict(pd.Series(list(kinds.values())).value_counts()) if columns else {}
    e = ev.add("typing", "column kinds from sample: " + ", ".join(f"{k}={int(v)}" for k, v in kind_counts.items()), values={"kinds": {c: kinds[c] for c in columns}, "n_sample": len(sample)}, computed_by="ingest.schema.type_column", n_samples=len(sample))
    evidence_ids.append(e.id)
    casts: dict[str, str] = {c: t["cast"] for c, t in types.items() if t.get("cast")}

    # name hints (weak evidence only)
    name_hints: dict[str, str] = {}
    if had_header:
        for c in columns:
            if NAME_LABEL_RE.search(c):
                name_hints[c] = "label"
            elif NAME_META_RE.search(c):
                name_hints[c] = "meta"
            elif NAME_TIME_RE.search(c):
                name_hints[c] = "time"
        if name_hints:
            e = ev.add("name_hint", f"header names weakly suggest roles for {len(name_hints)} columns (weight 0.1, never decisive)", values={"hints": name_hints, "weight": 0.1}, computed_by="ingest.schema.name_hint", n_samples=None)
            evidence_ids.append(e.id)

    # ---------------- timestamp & period
    _prog(progress, 0.25, "detecting time column")
    time_col: Optional[str] = None
    period: Optional[float] = None
    time_stats: dict[str, Any] = {}
    forced_time = options.get("time_column")
    dt_cols = [c for c in columns if kinds[c] == "datetime"]
    if forced_time and forced_time in columns:
        dt_cols = [forced_time] + [c for c in dt_cols if c != forced_time]
    best_t = None
    for c in dt_cols:
        if types[c].get("cast") and types[c].get("epoch_unit"):
            unit = types[c]["epoch_unit"]
            ts = pd.to_datetime(sample[c], unit=unit, errors="coerce")
        elif types[c].get("cast"):
            try:
                ts = pd.to_datetime(sample[c], errors="coerce", format="mixed")
            except Exception:
                continue
        else:
            ts = pd.to_datetime(sample[c], errors="coerce")
        st = _time_stats(ts, chunk_idx)
        st["column"] = c
        if forced_time == c:
            st["monotone"] = max(st["monotone"], 0.99)
        if best_t is None or st["monotone"] > best_t["monotone"]:
            best_t = st
    if best_t and best_t["monotone"] >= 0.8 and best_t["period"]:
        time_col = best_t["column"]
        period = float(best_t["period"])
        time_stats = best_t
        e = ev.add("time", f"column {time_col} is monotone in {best_t['monotone']:.1%} of within-chunk steps; median step {period:.3f} s; {best_t['irregular']:.1%} of steps deviate > 50 % from the median", signals=[], values={k: v for k, v in best_t.items()}, computed_by="ingest.schema.time_stats", n_samples=best_t["n_diffs"])
        evidence_ids.append(e.id)
        conf = 0.95 * best_t["monotone"] * (1.0 - 0.5 * best_t["irregular"])
        i1 = inf.add("dataset", f"timestamp column: {time_col}; sample period ~ {period:.3g} s (median of consecutive differences)", status="inferred" if best_t["irregular"] < 0.2 else "uncertain", confidence=round(conf, 3), evidence_ids=[e.id], reasoning="datetime-typed column that increases along the file order; period = median positive difference inside contiguous sample chunks", source="code", stage=STAGE, alternatives=[f"irregular sampling ({best_t['irregular']:.0%} of steps off by > 50 %); treat gaps as data-quality events"] if best_t["irregular"] > 0.05 else [])
        inference_ids.append(i1.id)
        if best_t["irregular"] > 0.05:
            assumptions.append(f"sampling is treated as regular at {period:.3g} s although {best_t['irregular']:.0%} of steps are irregular")
    else:
        e = ev.add("time", "no monotone datetime column found; rows are treated as consecutive samples in file order", values={"datetime_candidates": dt_cols, "best": best_t}, computed_by="ingest.schema.time_stats", n_samples=len(sample))
        evidence_ids.append(e.id)
        i1 = inf.add("dataset", "no timestamp: time is measured in sample units; the sample period is unknown", status="assumed", confidence=0.5, evidence_ids=[e.id], reasoning="no datetime-typed or epoch-like monotone column in the sample", source="code", stage=STAGE)
        inference_ids.append(i1.id)
        assumptions.append("no timestamp column: rows are equally spaced samples in file order; period unknown (sample units)")

    # ---------------- counters
    _prog(progress, 0.35, "detecting counters")
    counters: dict[str, dict[str, Any]] = {}
    for c in columns:
        t = types[c]
        if t["kind"] in ("integer", "numeric") and t.get("integer_valued") and not t.get("low_cardinality"):
            cs = _counter_stats(sample[c].to_numpy(dtype="float64"), chunk_idx)
            if cs["step_fraction"] >= 0.9 and cs.get("step"):
                counters[c] = cs
    order_col: Optional[str] = None
    if counters:
        order_col = max(counters, key=lambda c: (counters[c]["step_fraction"], -counters[c]["resets"]))
        cs = counters[order_col]
        e = ev.add("counter", f"column {order_col} increases by {cs['step']:g} in {cs['step_fraction']:.1%} of steps ({cs['resets']} resets in the sample)", values={"counters": counters}, computed_by="ingest.schema.counter_stats", n_samples=cs["n_diffs"])
        evidence_ids.append(e.id)
        i2 = inf.add("dataset", f"{order_col} is a sample counter and defines the order within groups" + (" (it resets, marking group starts)" if cs["resets"] > 0 else ""), status="inferred", confidence=round(min(0.95, cs["step_fraction"]), 3), evidence_ids=[e.id], reasoning="monotone constant-step integer column", source="code", stage=STAGE)
        inference_ids.append(i2.id)

    # ---------------- grouping
    _prog(progress, 0.45, "evaluating grouping strategies")
    cands: list[dict[str, Any]] = []
    forced_groups = options.get("group_columns")
    key_candidates: list[str] = []
    for c in columns:
        t = types[c]
        if c == order_col or c == time_col or t["kind"] in ("constant", "empty", "datetime", "text", "identifier"):
            continue
        if t["kind"] in ("integer", "categorical", "boolean") or (t["kind"] == "numeric" and t.get("integer_valued")):
            if t["n_unique"] <= max(2, 0.5 * len(sample)):
                key_candidates.append(c)
        elif t["kind"] == "numeric" and t.get("low_cardinality"):
            key_candidates.append(c)
    # sample-based pre-score: contiguity of value blocks inside chunks
    pre: dict[str, float] = {}
    same = chunk_idx[1:] == chunk_idx[:-1]
    for c in key_candidates:
        v = sample[c].astype(str).to_numpy()
        n_blocks = int(((v[1:] != v[:-1]) & same).sum()) + len(chunks)
        pre[c] = min(1.0, types[c]["n_unique"] / max(1, n_blocks))
    ranked = sorted(key_candidates, key=lambda c: -pre[c])
    ranked = [c for c in ranked if pre[c] >= 0.3][:8]
    singles: list[dict[str, Any]] = []
    for c in ranked:
        try:
            singles.append(_evaluate_key_candidate(con, [c], n_rows, sample, order_col))
        except Exception:
            continue
    cands.extend(singles)
    good = [s for s in sorted(singles, key=lambda s: -s["score"]) if s["contiguity"] >= 0.3 or s["n_groups"] >= 2][:4]
    combos_done: set[tuple[str, ...]] = set()
    for i in range(len(good)):
        for j in range(i + 1, len(good)):
            cols = sorted([good[i]["columns"][0], good[j]["columns"][0]], key=lambda c: types[c]["n_unique"])
            key = tuple(cols)
            if key in combos_done:
                continue
            combos_done.add(key)
            try:
                cc = _evaluate_key_candidate(con, cols, n_rows, sample, order_col)
                if cc["n_groups"] > max(good[i]["n_groups"], good[j]["n_groups"]) and cc["median_len"] >= 3:
                    cands.append(cc)
            except Exception:
                continue
    best_key = max([c for c in cands if c["method"] == "key_columns"], key=lambda c: c["score"], default=None)
    if order_col and counters[order_col]["resets"] > 0:
        try:
            cands.append(_evaluate_counter_candidate(con, order_col, n_rows, sample, best_key))
        except Exception:
            pass
    if time_col and period:
        texpr = casts.get(time_col, quote_ident(time_col))
        tg = _evaluate_time_gap_candidate(con, texpr, period, n_rows, gap_factor=float(settings.quality.gap_factor) * 3)
        if tg:
            cands.append(tg)
    cont_cols = [c for c in columns if kinds[c] == "numeric" and not types[c].get("low_cardinality")]
    if not any(c["score"] >= 0.5 for c in cands):
        cp = _evaluate_changepoint_candidate(con, cont_cols, n_rows)
        if cp:
            cands.append(cp)
    cands.append({"method": "none", "columns": [], "n_groups": 1, "score": 0.3, "blocks": [(0, n_rows, "0")], "expr": "'0'", "median_len": float(n_rows), "min_len": float(n_rows), "max_len": float(n_rows)})

    if forced_groups:
        fcols = [c for c in forced_groups if c in columns]
        if fcols:
            forced = _evaluate_key_candidate(con, fcols, n_rows, sample, order_col)
            forced["score"] = 1.0
            forced["forced"] = True
            cands.append(forced)
    # agreement bonus: strategies whose group count matches within 10 % support each other
    for c in cands:
        if c["method"] == "none":
            continue
        agree = [o for o in cands if o is not c and o["method"] != "none" and abs(o["n_groups"] - c["n_groups"]) <= max(1, 0.1 * c["n_groups"])]
        if agree:
            c["agreement"] = len(agree)
            c["score"] = round(min(1.0, c["score"] + 0.05 * len(agree)), 4)
    cands.sort(key=lambda c: (0 if c.get("forced") else 1, -c["score"]))  # operator choice first, then score
    chosen = cands[0]
    for c in cands:
        e = ev.add("grouping", f"grouping candidate {_candidate_rationale(c)} -> score {c['score']:.2f}", values={k: v for k, v in c.items() if k not in ("blocks",)}, computed_by="ingest.schema.grouping", n_samples=n_rows)
        evidence_ids.append(e.id)
        c["evidence_id"] = e.id
    alt_text = [f"{c['method']}({','.join(c['columns'])}) n_groups={c['n_groups']} score={c['score']:.2f}" for c in cands[1:5]]
    status = "inferred" if chosen["score"] >= 0.6 else ("assumed" if chosen["method"] == "none" else "uncertain")
    i3 = inf.add("dataset", f"groups: {chosen['n_groups']} via {chosen['method']}" + (f" on {', '.join(chosen['columns'])}" if chosen["columns"] else ""), status=status, confidence=round(min(0.98, chosen["score"]), 3), evidence_ids=[c["evidence_id"] for c in cands], reasoning=_candidate_rationale(chosen), source="human" if chosen.get("forced") else "code", stage=STAGE, alternatives=alt_text)
    inference_ids.append(i3.id)
    if chosen["method"] == "none":
        assumptions.append("no group structure found: the dataset is treated as one continuous sequence")
    elif chosen["method"] in ("changepoint", "time_gaps"):
        assumptions.append(f"groups come from {chosen['method']} segmentation, not from an explicit key; boundaries are approximate")

    # ---------------- materialize groups
    _prog(progress, 0.6, "materializing groups")
    blocks = chosen.get("blocks")
    expr = chosen.get("expr")
    if blocks is None and chosen["method"] == "key_columns":
        blocks = _blocks_from_window(con, expr)
    mat = materialize_groups(ws, settings, blocks=blocks, expr=expr, casts=casts, progress=lambda f, m: _prog(progress, 0.6 + 0.25 * f, m))
    con = ws.duckdb()
    group_sizes = con.execute("SELECT min(n), median(n), max(n), count(*) FROM (SELECT count(*) AS n FROM dataset GROUP BY __group__)").fetchone()
    n_groups = int(group_sizes[3])
    ws.write_json("groups.json", [{"group_id": g, "row_start": int(a), "row_end": int(b), "n_rows": int(b - a)} for a, b, g in (blocks or [])])

    # ---------------- label / meta detection (structure first, names only as tie-breakers)
    _prog(progress, 0.87, "detecting label and metadata columns")
    label_cols: list[str] = []
    meta_cols: list[str] = []
    signal_cols: list[str] = []
    reasons: dict[str, str] = {}
    group_of_sample = assign_blocks(sample["__row__"].to_numpy(), blocks or [(0, n_rows, "0")])
    key_cols = set(chosen.get("columns", [])) if chosen["method"] == "key_columns" else set()
    for c in columns:
        t = types[c]
        k = t["kind"]
        hint = name_hints.get(c)
        if c == time_col:
            meta_cols.append(c)
            reasons[c] = "timestamp column"
            continue
        if c in key_cols:
            meta_cols.append(c)
            reasons[c] = "group key column"
            continue
        if c == order_col or c in counters:
            meta_cols.append(c)
            reasons[c] = "sample counter / index"
            continue
        if k == "empty":
            meta_cols.append(c)
            reasons[c] = "all values missing"
            continue
        if k == "constant":
            if t.get("numeric"):
                signal_cols.append(c)
                reasons[c] = "constant numeric column (kept as excluded signal)"
            else:
                meta_cols.append(c)
                reasons[c] = "constant non-numeric column"
            continue
        if k == "identifier":
            meta_cols.append(c)
            reasons[c] = "unique identifier-like strings"
            continue
        if k == "datetime":
            meta_cols.append(c)
            reasons[c] = "secondary datetime column"
            continue
        if k in ("text",):
            meta_cols.append(c)
            reasons[c] = "free text"
            continue
        # per-group structure of low-cardinality columns
        within_const = None
        changes_per_group = None
        if k in ("categorical", "boolean") or (k in ("integer", "numeric") and t.get("low_cardinality")):
            v = sample[c].astype(str).to_numpy()
            df_ = pd.DataFrame({"g": group_of_sample, "v": v})
            nun = df_.groupby("g")["v"].nunique()
            within_const = float((nun <= 1).mean()) if len(nun) else 0.0
            chg = ((v[1:] != v[:-1]) & (group_of_sample[1:] == group_of_sample[:-1])).sum()
            changes_per_group = float(chg / max(1, len(nun)))
            across_var = int(df_.groupby("g")["v"].first().nunique()) > 1
        if k == "categorical":
            # string-coded low cardinality
            if within_const is not None and (within_const >= 0.9 or changes_per_group <= 2) and across_var and n_groups > 1:
                target = "meta" if hint == "meta" else "label"
            elif within_const is not None and within_const >= 0.9 and not across_var:
                target = "meta"
            else:
                target = "meta" if hint != "label" else "label"
            (label_cols if target == "label" else meta_cols).append(c)
            reasons[c] = f"{k} column ({t['n_unique']} values); constant within {within_const:.0%} of groups; {changes_per_group:.1f} changes/group" if within_const is not None else f"{k} column"
            continue
        if k == "boolean":
            if within_const is not None and within_const >= 0.9 and across_var and n_groups > 1:
                (meta_cols if hint == "meta" else label_cols).append(c)
                reasons[c] = f"boolean, constant within {within_const:.0%} of groups, varies across groups"
            else:
                signal_cols.append(c)
                reasons[c] = "boolean varying within groups (kept as 0/1 signal)"
            continue
        if k in ("integer", "numeric") and t.get("low_cardinality") and within_const is not None:
            if within_const >= 0.9 and across_var and n_groups > 1:
                (meta_cols if hint == "meta" else label_cols).append(c)
                reasons[c] = f"integer-coded, {t['n_unique']} values, constant within {within_const:.0%} of groups, varies across groups -> label-like"
                continue
        signal_cols.append(c)
        reasons[c] = "numeric measurement"
    e = ev.add("label_detection", f"{len(label_cols)} label-like, {len(meta_cols)} metadata, {len(signal_cols)} signal columns (structure-based; header names used only as tie-breakers)", values={"label_columns": label_cols, "meta_columns": meta_cols, "reasons": reasons}, computed_by="ingest.schema.label_detection", n_samples=len(sample))
    evidence_ids.append(e.id)
    if label_cols:
        i4 = inf.add("dataset", f"label-like columns excluded from detection (evaluation only): {', '.join(label_cols)}", status="inferred", confidence=0.7, evidence_ids=[e.id], reasoning="low-cardinality columns constant within groups but varying across groups", source="code", stage=STAGE, alternatives=["they may be metadata (operator can move them)"])
        inference_ids.append(i4.id)

    # ---------------- aliases
    width = max(2, len(str(len(signal_cols))))
    alias = {c: f"S{i + 1:0{width}d}" for i, c in enumerate(signal_cols)}
    if blind and had_header:
        e = ev.add("name_hint", "blind mode: signals are addressed by aliases; original header names kept only as weak name hints", values={"n_signals": len(signal_cols)}, computed_by="ingest.schema.alias", n_samples=None)
        evidence_ids.append(e.id)

    # ---------------- domain likelihood
    _prog(progress, 0.93, "estimating domain likelihood")
    domain, dexpl, dev = _domain_likelihood(sample, chunk_idx, columns, types, signal_cols, time_col, time_stats, options.get("domain_hint"))
    e = ev.add("domain", dexpl, values=dev, computed_by="ingest.schema.domain_likelihood", n_samples=len(sample))
    evidence_ids.append(e.id)
    top = max(domain, key=domain.get)
    i5 = inf.add("dataset", f"data looks like {top.replace('_', ' ')} (likelihood {domain[top]:.2f})", status="inferred" if domain[top] >= 0.6 else "uncertain", confidence=round(domain[top], 3), evidence_ids=[e.id], reasoning=dexpl, source="code", stage=STAGE, alternatives=[f"{k}: {v:.2f}" for k, v in sorted(domain.items(), key=lambda kv: -kv[1])[1:]])
    inference_ids.append(i5.id)
    if options.get("domain_hint"):
        assumptions.append(f"operator domain hint '{options['domain_hint']}' nudged the domain likelihood")

    schema = DatasetSchema(
        dataset_id=ws.run_id,
        source_path=str(ctx.get("source_path", "")),
        format=str(fmt.get("format", "unknown")),
        n_rows=n_rows,
        n_cols=len(columns),
        had_header=had_header,
        transposed=bool(fmt.get("transposed", False)),
        delimiter=fmt.get("delimiter"),
        columns=columns,
        time_column=time_col,
        sample_period_seconds=period,
        order_column=order_col,
        group_columns=list(chosen.get("columns", [])) if chosen["method"] == "key_columns" else [],
        group_column="__group__",
        grouping_method=chosen["method"],
        grouping_candidates=[GroupingCandidate(method=c["method"], columns=list(c.get("columns", [])), n_groups=int(c["n_groups"]), score=float(c["score"]), rationale=_candidate_rationale(c)) for c in cands],
        label_columns=label_cols,
        meta_columns=meta_cols,
        signal_columns=signal_cols,
        signal_alias=alias,
        n_groups=n_groups,
        group_sizes={"min": int(group_sizes[0]), "median": int(group_sizes[1]), "max": int(group_sizes[2])},
        domain_likelihood=domain,
        assumptions=assumptions,
        inference_ids=inference_ids,
        evidence_ids=evidence_ids,
        options_used={**{k: v for k, v in fmt.items() if k not in ("evidence", "header_names")}, "operator_options": options, "column_kinds": kinds, "column_reasons": reasons, "name_hints": name_hints, "casts": casts, "materialization": mat, "counters": {k: {kk: vv for kk, vv in v.items()} for k, v in counters.items()}, "time_stats": time_stats, "sampling": sdesc, "conversion": {k: v for k, v in conv.items() if k not in ("parquet_types", "source_types")}},
    )
    return schema


def _domain_likelihood(sample: pd.DataFrame, chunk_idx: np.ndarray, columns: list[str], types: dict[str, dict[str, Any]], signal_cols: list[str], time_col: Optional[str], time_stats: dict[str, Any], hint: Optional[str]) -> tuple[dict[str, float], str, dict[str, Any]]:
    n_cols = max(1, len(columns))
    cont = [c for c in signal_cols if types[c]["kind"] == "numeric" and not types[c].get("low_cardinality") and types[c]["n_unique"] > 50]
    share_cont = len(cont) / n_cols
    share_text = sum(1 for c in columns if types[c]["kind"] in ("text", "categorical", "identifier")) / n_cols
    acs = []
    same = chunk_idx[1:] == chunk_idx[:-1]
    for c in cont[:60]:
        v = sample[c].to_numpy(dtype="float64")
        v = np.where(np.isfinite(v), v, np.nan)
        sd = np.nanstd(v)
        if not np.isfinite(sd) or sd == 0:
            continue
        z = (v - np.nanmean(v)) / sd
        a, b = z[1:][same], z[:-1][same]
        ok = np.isfinite(a) & np.isfinite(b)
        if ok.sum() > 10:
            acs.append(float(np.mean(a[ok] * b[ok])))
    autocorr = float(np.median(acs)) if acs else 0.0
    regular = 1.0 if (time_col and time_stats.get("irregular", 1.0) < 0.1) else (0.6 if time_col is None else max(0.0, 1.0 - float(time_stats.get("irregular", 1.0))))
    sensor = 0.45 * share_cont + 0.4 * max(0.0, autocorr) + 0.15 * regular
    records = 0.5 * share_text + 0.3 * max(0.0, 1.0 - max(0.0, autocorr)) + 0.2 * (1.0 if any(types[c]["kind"] == "identifier" for c in columns) else 0.0)
    events = 0.4 * (1.0 if time_col else 0.0) * float(time_stats.get("irregular", 0.0)) + 0.3 * share_text + 0.3 * (1.0 - share_cont)
    unknown = 0.15
    if hint:
        h = str(hint).lower()
        if "sensor" in h or "process" in h:
            sensor += 0.2
        elif "record" in h or "business" in h:
            records += 0.2
        elif "event" in h or "log" in h:
            events += 0.2
    raw = {"sensor_stream": sensor, "business_records": records, "event_log": events, "unknown": unknown}
    tot = sum(raw.values())
    dom = {k: round(v / tot, 3) for k, v in raw.items()}
    expl = f"{share_cont:.0%} of columns are continuous numeric, median lag-1 autocorrelation {autocorr:.2f}, {share_text:.0%} text/categorical columns, sampling regularity {regular:.2f}" + (f", operator hint '{hint}'" if hint else "")
    return dom, expl, {"share_continuous": round(share_cont, 3), "median_autocorr": round(autocorr, 3), "share_text": round(share_text, 3), "regularity": round(regular, 3), "raw": {k: round(v, 3) for k, v in raw.items()}}


# --------------------------------------------------------------------------------------------------
# overrides
# --------------------------------------------------------------------------------------------------
def apply_override(ws, settings, decision) -> dict[str, Any]:
    """Schema decisions from the UI: set group columns / time column / order column, or move a column
    between signal, label and meta. Re-materializes ``__group__`` when grouping changes."""
    schema = ws.schema()
    if schema is None:
        return {"error": "no schema"}
    nv = dict(decision.new_value or {})
    actor = f"human:{decision.actor_name}({decision.role})"
    changed: dict[str, Any] = {}
    if "group_columns" in nv:
        cols = [c for c in (nv["group_columns"] or []) if c in schema.columns]
        con = ws.duckdb()
        n_rows = schema.n_rows
        if cols:
            expr = _key_expr(cols)
            gb = _key_groupby(con, cols)
            contiguous = bool((gb["n"] == (gb["e"] - gb["s"] + 1)).all())
            blocks = [(int(r.s), int(r.e) + 1, str(r.key)) for r in gb.itertuples()] if contiguous else _blocks_from_window(con, expr)
            schema.grouping_method = "key_columns"
        else:
            expr = "'0'"
            blocks = [(0, n_rows, "0")]
            schema.grouping_method = "none"
        materialize_groups(ws, settings, blocks=blocks, expr=expr, casts=None)
        con = ws.duckdb()
        gs = con.execute("SELECT min(n), median(n), max(n), count(*) FROM (SELECT count(*) AS n FROM dataset GROUP BY __group__)").fetchone()
        schema.group_columns = cols
        schema.n_groups = int(gs[3])
        schema.group_sizes = {"min": int(gs[0]), "median": int(gs[1]), "max": int(gs[2])}
        for c in cols:
            if c in schema.signal_columns:
                schema.signal_columns.remove(c)
            if c not in schema.meta_columns and c not in schema.label_columns:
                schema.meta_columns.append(c)
        ws.write_json("groups.json", [{"group_id": g, "row_start": int(a), "row_end": int(b), "n_rows": int(b - a)} for a, b, g in blocks])
        changed["group_columns"] = cols
        changed["n_groups"] = schema.n_groups
        _invalidate_batches(ws)
    if "time_column" in nv:
        tc = nv["time_column"]
        schema.time_column = tc if tc in schema.columns else None
        if tc and tc in schema.signal_columns:
            schema.signal_columns.remove(tc)
            schema.meta_columns.append(tc)
        changed["time_column"] = schema.time_column
        _invalidate_batches(ws)
    if "order_column" in nv:
        schema.order_column = nv["order_column"] if nv["order_column"] in schema.columns else None
        changed["order_column"] = schema.order_column
    if "sample_period_seconds" in nv:
        schema.sample_period_seconds = _finite(nv["sample_period_seconds"])
        changed["sample_period_seconds"] = schema.sample_period_seconds
    if "move" in nv:
        mv = nv["move"] or {}
        col, to = mv.get("column"), mv.get("to")
        if col in schema.columns and to in ("signal", "label", "meta"):
            for lst in (schema.signal_columns, schema.label_columns, schema.meta_columns):
                if col in lst:
                    lst.remove(col)
            {"signal": schema.signal_columns, "label": schema.label_columns, "meta": schema.meta_columns}[to].append(col)
            changed["move"] = {"column": col, "to": to}
    if "move" in changed or "group_columns" in changed or "time_column" in changed:
        # aliases follow column order; keep existing aliases stable, add new ones at the end
        existing = {c: a for c, a in schema.signal_alias.items() if c in schema.signal_columns}
        width = max(2, len(str(len(schema.signal_columns))))
        used = set(existing.values())
        nxt = 1
        for c in schema.signal_columns:
            if c in existing:
                continue
            while f"S{nxt:0{width}d}" in used:
                nxt += 1
            existing[c] = f"S{nxt:0{width}d}"
            used.add(existing[c])
        schema.signal_alias = {c: existing[c] for c in schema.signal_columns}
    schema.assumptions.append(f"operator override ({decision.action}): {changed}")
    ws.write_json("schema", schema)
    ws.log.record(ACTOR, "schema_override_applied", "schema", decision.object_id or ws.run_id, {"actor": actor, "changed": changed, "note": decision.note})
    return {"changed": changed, "schema": {"n_groups": schema.n_groups, "signal_columns": schema.signal_columns, "label_columns": schema.label_columns, "meta_columns": schema.meta_columns}}


def _invalidate_batches(ws) -> None:
    p = ws.path("batches")
    try:
        if p.exists():
            p.unlink()
    except OSError:
        pass
