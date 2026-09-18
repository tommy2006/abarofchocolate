"""Batch / stream access to a run's dataset.

Batches are contiguous half-open row ranges ``[row_start, row_end)`` over ``__row__`` that never cross a
group boundary unless groups are shorter than ``settings.batch.min_rows``. With a timestamp they are
``settings.batch.window_seconds`` windows (5 min by default), otherwise ``fallback_fraction`` of the rows.

    build_batches(ws, settings) -> list[dict]          writes batches.json
    iter_batches(ws, settings) -> (batch_id, row_start, row_end, meta)
    read_rows(ws, row_start, row_end, columns=None) -> pandas.DataFrame  (bounded, via DuckDB)
    replay(ws, settings, callback=None, speed=0.0) -> list[dict]   drives tpm.pipeline.process_batch
    align_incoming(ws, settings, df, batch_id=None) -> DataFrame with the run's columns
    watch_folder(ws, settings, folder, callback=None, poll_s=2.0, ...) -> list[dict]
"""
from __future__ import annotations

import math
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import numpy as np
import pandas as pd

from .readers import detect_format, quote_ident, read_small, sanitize_columns

ACTOR = "system:ingest"
BatchCallback = Callable[[pd.DataFrame, str, dict[str, Any]], Any]


def _batch_id(i: int) -> str:
    return f"B{i:05d}"


def _groups_blocks(ws, n_rows: int) -> list[tuple[int, int, str]]:
    g = ws.read_json("groups.json", None)
    if g:
        return [(int(x["row_start"]), int(x["row_end"]), str(x["group_id"])) for x in g]
    return [(0, n_rows, "0")]


def _row_count_batches(blocks: list[tuple[int, int, str]], n_rows: int, settings) -> list[dict[str, Any]]:
    target = int(max(settings.batch.min_rows, min(settings.batch.max_rows, round(n_rows * settings.batch.fallback_fraction))))
    target = max(1, target)
    out: list[dict[str, Any]] = []
    cur_start: Optional[int] = None
    cur_groups: list[str] = []
    for a, b, gid in blocks:
        n = b - a
        if n > target:
            if cur_start is not None:
                out.append({"row_start": cur_start, "row_end": a, "group_ids": cur_groups})
                cur_start, cur_groups = None, []
            parts = int(math.ceil(n / target))
            edges = np.linspace(a, b, parts + 1).astype(int)
            for i in range(parts):
                out.append({"row_start": int(edges[i]), "row_end": int(edges[i + 1]), "group_ids": [gid]})
            continue
        if cur_start is None:
            cur_start, cur_groups = a, [gid]
        else:
            cur_groups.append(gid)
        if b - cur_start >= target:
            out.append({"row_start": cur_start, "row_end": b, "group_ids": cur_groups})
            cur_start, cur_groups = None, []
    if cur_start is not None:
        out.append({"row_start": cur_start, "row_end": blocks[-1][1], "group_ids": cur_groups})
    return out


def _time_window_batches(ws, schema, settings) -> Optional[list[dict[str, Any]]]:
    """Segments of consecutive rows sharing (group, time window). Windows are `window_seconds`, widened to a
    multiple of it so that a window holds at least `min_rows` samples at the inferred period. Scanning
    consecutive rows (LAG over __row__) keeps segments contiguous even when timestamps repeat or jump."""
    con = ws.duckdb()
    q = quote_ident(schema.time_column)
    w = float(settings.batch.window_seconds)
    period = float(schema.sample_period_seconds or 0)
    if period > 0 and settings.batch.min_rows * period > w:
        w = w * math.ceil(settings.batch.min_rows * period / w)
    try:
        df = con.execute(
            f"SELECT __row__ AS s, g, w, t FROM (SELECT __row__, __group__ AS g, floor(epoch({q}) / {w}) AS w, {q} AS t, "
            f"lag(__group__) OVER (ORDER BY __row__) AS pg, lag(floor(epoch({q}) / {w})) OVER (ORDER BY __row__) AS pw FROM dataset) "
            f"WHERE pg IS NULL OR pg IS DISTINCT FROM g OR pw IS DISTINCT FROM w ORDER BY s"
        ).df()
    except Exception:
        return None
    if len(df) == 0 or int(df["s"].iloc[0]) != 0:
        return None
    s = df["s"].to_numpy(dtype=np.int64)
    e = np.concatenate([s[1:], [schema.n_rows]])
    t0s = [str(t) if t is not None and t == t else None for t in df["t"]]
    if len(df) <= 5000:
        try:
            con.register("_seg", pd.DataFrame({"s": s, "e": e}))
            tt = con.execute(f"SELECT seg.s, min(d.{q}), max(d.{q}) FROM _seg seg JOIN dataset d ON d.__row__ >= seg.s AND d.__row__ < seg.e GROUP BY seg.s ORDER BY seg.s").fetchall()
            con.unregister("_seg")
            tmap = {int(r[0]): (str(r[1]) if r[1] is not None else None, str(r[2]) if r[2] is not None else None) for r in tt}
        except Exception:
            tmap = {}
    else:
        tmap = {}
    wins = []
    for i, (a, b, g) in enumerate(zip(s, e, df["g"])):
        t_start, t_end = tmap.get(int(a), (t0s[i], t0s[i + 1] if i + 1 < len(t0s) else None))
        wins.append({"row_start": int(a), "row_end": int(b), "group_ids": [str(g)], "time_start": t_start, "time_end": t_end})
    # coalesce tiny windows and split huge ones
    out: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    for win in wins:
        if cur is None:
            cur = dict(win)
            continue
        same_group = cur["group_ids"][-1] == win["group_ids"][0]
        small = (cur["row_end"] - cur["row_start"]) < settings.batch.min_rows
        if small and (same_group or (win["row_end"] - win["row_start"]) < settings.batch.min_rows):
            cur["row_end"] = win["row_end"]
            cur["time_end"] = win["time_end"]
            if not same_group:
                cur["group_ids"].append(win["group_ids"][0])
            continue
        out.append(cur)
        cur = dict(win)
    if cur is not None:
        out.append(cur)
    final: list[dict[str, Any]] = []
    for b in out:
        n_b = b["row_end"] - b["row_start"]
        if n_b > settings.batch.max_rows:
            parts = int(math.ceil(n_b / settings.batch.max_rows))
            edges = np.linspace(b["row_start"], b["row_end"], parts + 1).astype(int)
            for i in range(parts):
                final.append({**b, "row_start": int(edges[i]), "row_end": int(edges[i + 1]), "time_start": None, "time_end": None})
        else:
            final.append(b)
    return final


def build_batches(ws, settings, schema=None, force: bool = False) -> list[dict[str, Any]]:
    """Plan batches and write batches.json. Idempotent unless force=True."""
    if not force:
        existing = ws.read_json("batches", None)
        if existing:
            return existing
    schema = schema or ws.schema()
    if schema is None:
        raise RuntimeError("schema.json missing: run ingest first")
    n_rows = int(schema.n_rows)
    batches: Optional[list[dict[str, Any]]] = None
    method = "rows"
    if schema.time_column and schema.sample_period_seconds:
        batches = _time_window_batches(ws, schema, settings)
        if batches:
            method = "time_window"
    if not batches:
        batches = _row_count_batches(_groups_blocks(ws, n_rows), n_rows, settings)
    out = []
    for i, b in enumerate(batches):
        out.append({"batch_id": _batch_id(i + 1), "row_start": int(b["row_start"]), "row_end": int(b["row_end"]), "n_rows": int(b["row_end"] - b["row_start"]), "group_ids": [str(g) for g in b.get("group_ids", [])], "time_start": b.get("time_start"), "time_end": b.get("time_end"), "method": method})
    if schema.time_column and method == "rows" and len(out) <= 500:
        con = ws.duckdb()
        q = quote_ident(schema.time_column)
        for b in out:
            try:
                t0, t1 = con.execute(f"SELECT min({q}), max({q}) FROM dataset WHERE __row__ >= {b['row_start']} AND __row__ < {b['row_end']}").fetchone()
                b["time_start"], b["time_end"] = (str(t0) if t0 is not None else None), (str(t1) if t1 is not None else None)
            except Exception:
                pass
    ws.write_json("batches", out)
    ws.log.record(ACTOR, "batches_planned", "dataset", ws.run_id, {"n_batches": len(out), "method": method, "window_seconds": settings.batch.window_seconds if method == "time_window" else None, "rows_per_batch_median": int(np.median([b["n_rows"] for b in out])) if out else 0})
    return out


def iter_batches(ws, settings) -> Iterator[tuple[str, int, int, dict[str, Any]]]:
    """Yields (batch_id, row_start, row_end, meta) in file order. row_end is exclusive."""
    for b in build_batches(ws, settings):
        yield b["batch_id"], int(b["row_start"]), int(b["row_end"]), b


def read_rows(ws, row_start: int, row_end: int, columns: Optional[list[str]] = None) -> pd.DataFrame:
    """Rows [row_start, row_end) of dataset.parquet, ordered by __row__ (bounded by the range size)."""
    con = ws.duckdb()
    if columns:
        cols = ["__row__"] + [c for c in columns if c != "__row__"]
        sel = ", ".join(quote_ident(c) for c in cols)
    else:
        sel = "*"
    return con.execute(f"SELECT {sel} FROM dataset WHERE __row__ >= {int(row_start)} AND __row__ < {int(row_end)} ORDER BY __row__").df()


def replay(ws, settings, callback: Optional[BatchCallback] = None, speed: float = 0.0, max_batches: Optional[int] = None, columns: Optional[list[str]] = None, stop_event: Optional[threading.Event] = None) -> list[dict[str, Any]]:
    """Replay the dataset batch by batch. ``callback(df, batch_id, meta)`` defaults to
    ``tpm.pipeline.process_batch``. ``speed`` = seconds of data per wall-clock second (0 = as fast as
    possible; 1 = real time when a timestamp exists, else one sample per second)."""
    schema = ws.schema()
    results: list[dict[str, Any]] = []
    if callback is None:
        from ..pipeline import process_batch

        def callback(df: pd.DataFrame, batch_id: str, meta: dict[str, Any]) -> Any:
            return process_batch(ws, settings, df, batch_id)

    for i, (bid, a, b, meta) in enumerate(iter_batches(ws, settings)):
        if stop_event is not None and stop_event.is_set():
            break
        if max_batches is not None and i >= max_batches:
            break
        t_wall = time.time()
        df = read_rows(ws, a, b, columns)
        res = callback(df, bid, meta)
        results.append({"batch_id": bid, "row_start": a, "row_end": b, "result": res if isinstance(res, dict) else None})
        ws.log.record(ACTOR, "replay_batch", "batch", bid, {"row_start": a, "row_end": b, "n_rows": b - a})
        if speed and speed > 0:
            if schema is not None and schema.sample_period_seconds:
                data_seconds = (b - a) * float(schema.sample_period_seconds)
            else:
                data_seconds = float(b - a)
            wait = data_seconds / speed - (time.time() - t_wall)
            if wait > 0:
                if stop_event is not None:
                    stop_event.wait(min(wait, 3600))
                else:
                    time.sleep(min(wait, 3600))
    return results


# --------------------------------------------------------------------------------------------------
# incoming data (HTTP push / watch folder)
# --------------------------------------------------------------------------------------------------
def _stream_state(ws) -> dict[str, Any]:
    return ws.read_json("stream_state.json", {"next_row": None, "seen_files": {}, "n_batches": 0})


def _save_stream_state(ws, st: dict[str, Any]) -> None:
    ws.write_json("stream_state.json", st)


def align_incoming(ws, settings, df: pd.DataFrame, batch_id: Optional[str] = None) -> pd.DataFrame:
    """Map an incoming DataFrame onto the run's columns (by name, case-insensitive name, alias S01..,
    or position for headerless data). Missing columns are filled with NaN and recorded as evidence;
    extra columns are dropped. Adds __row__ (continuing the run) and __group__."""
    schema = ws.schema()
    if schema is None:
        raise RuntimeError("schema.json missing: run ingest first")
    df = df.copy()
    df.columns = sanitize_columns(list(df.columns))
    incoming = list(df.columns)
    lower = {c.lower(): c for c in incoming}
    alias_rev = {a: c for c, a in schema.signal_alias.items()}
    mapping: dict[str, Optional[str]] = {}
    headerless = all(c.startswith("col_") for c in incoming) and len(incoming) == len(schema.columns)
    for i, c in enumerate(schema.columns):
        if c in incoming:
            mapping[c] = c
        elif c.lower() in lower:
            mapping[c] = lower[c.lower()]
        elif schema.signal_alias.get(c) in incoming:
            mapping[c] = schema.signal_alias[c]
        elif headerless:
            mapping[c] = incoming[i]
        else:
            mapping[c] = None
    missing = [c for c, m in mapping.items() if m is None]
    extra = [c for c in incoming if c not in set(m for m in mapping.values() if m) and c not in ("__row__", "__group__")]
    out = pd.DataFrame(index=df.index)
    kinds = (schema.options_used or {}).get("column_kinds", {})
    for c, m in mapping.items():
        if m is None:
            out[c] = np.nan
            continue
        s = df[m]
        if c == schema.time_column or kinds.get(c) == "datetime":
            out[c] = pd.to_datetime(s, errors="coerce")
        elif c in schema.signal_columns or kinds.get(c) in ("numeric", "integer", "boolean"):
            out[c] = pd.to_numeric(s, errors="coerce")
        else:
            out[c] = s
    st = _stream_state(ws)
    start = int(st["next_row"]) if st.get("next_row") is not None else int(schema.n_rows)
    out["__row__"] = np.arange(start, start + len(out), dtype=np.int64)
    bid = batch_id or f"SB{int(st.get('n_batches', 0)) + 1:05d}"  # stream batches: SB..., replay batches: B...
    out["__group__"] = f"stream:{bid}"
    st["next_row"] = start + len(out)
    st["n_batches"] = int(st.get("n_batches", 0)) + 1
    _save_stream_state(ws, st)
    if missing or extra:
        e = ws.evidence.add("alignment", f"incoming batch {bid}: {len(missing)} expected columns missing (filled with NaN), {len(extra)} unknown columns dropped", signals=[schema.signal_alias[c] for c in missing if c in schema.signal_alias], values={"missing": missing, "extra": extra, "n_rows": int(len(out))}, computed_by="ingest.stream.align_incoming", n_samples=int(len(out)), batch_id=bid)
        ws.log.record(ACTOR, "batch_aligned", "batch", bid, {"missing": missing, "extra": extra, "n_rows": int(len(out))}, evidence_ids=[e.id])
    else:
        ws.log.record(ACTOR, "batch_aligned", "batch", bid, {"n_rows": int(len(out)), "mapping": "exact"})
    out.attrs["batch_id"] = bid
    return out


def watch_folder(ws, settings, folder: str | Path, callback: Optional[BatchCallback] = None, poll_s: float = 2.0, stop_event: Optional[threading.Event] = None, max_iterations: Optional[int] = None, patterns: tuple[str, ...] = (".csv", ".tsv", ".txt", ".dat", ".parquet", ".json", ".jsonl", ".xlsx")) -> list[dict[str, Any]]:
    """Poll ``folder`` for new files; each stable new file becomes one aligned batch passed to
    ``callback(df, batch_id, meta)`` (default: tpm.pipeline.process_batch). Returns processed batches."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if callback is None:
        from ..pipeline import process_batch

        def callback(df: pd.DataFrame, batch_id: str, meta: dict[str, Any]) -> Any:
            return process_batch(ws, settings, df, batch_id)

    processed: list[dict[str, Any]] = []
    pending_sizes: dict[str, int] = {}
    it = 0
    while True:
        st = _stream_state(ws)
        seen = st.get("seen_files", {})
        files = sorted([p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in patterns and not p.name.startswith(".")], key=lambda p: p.stat().st_mtime)
        for p in files:
            key = str(p.resolve())
            if key in seen:
                continue
            size = p.stat().st_size
            if pending_sizes.get(key) != size:  # wait until the file stops growing
                pending_sizes[key] = size
                continue
            try:
                fmt = detect_format(p)
                raw = read_small(p, fmt)
                df = align_incoming(ws, settings, raw)
                bid = df.attrs.get("batch_id")
                meta = {"source_file": str(p), "n_rows": int(len(df)), "format": fmt.get("format")}
                res = callback(df, bid, meta)
                processed.append({"batch_id": bid, "file": str(p), "n_rows": int(len(df)), "result": res if isinstance(res, dict) else None})
                st = _stream_state(ws)
                st.setdefault("seen_files", {})[key] = {"batch_id": bid, "size": size, "ts": time.time()}
                _save_stream_state(ws, st)
                ws.log.record(ACTOR, "file_ingested", "batch", bid, meta)
            except Exception as e:
                st = _stream_state(ws)
                st.setdefault("seen_files", {})[key] = {"error": str(e)[:500], "size": size, "ts": time.time()}
                _save_stream_state(ws, st)
                ws.log.record(ACTOR, "file_failed", "batch", p.name, {"error": str(e)[:500]})
        it += 1
        if max_iterations is not None and it >= max_iterations:
            break
        if stop_event is not None:
            if stop_event.wait(poll_s):
                break
        else:
            time.sleep(poll_s)
    return processed
