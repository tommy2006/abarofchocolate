"""Event-log / free-text adapter (review item 27, third domain): logs have few numbers. What matters is how often each
kind of entry occurs (the share of ERROR lines, of a status value, of one service) and how the free text changes (the
length of the messages). This adapter turns those into ordinary numeric signals, so the same detectors, checks and
diagnoses run on them:

    share of <column> = <value> in the last <W> rows    for per-row categories (2..12 values) that change row to row
    length of <column>                                 for free-text columns

The derived columns are added to dataset.parquet and to the signal catalogue with a plain description; the original
columns stay as they were (meta). Generic: nothing depends on column names. Applied only to event-like tables
(timestamps plus per-row categories or text) of at most MAX_ROWS rows; sensor tables are untouched.
"""
from __future__ import annotations

import re
from typing import Any

WINDOW = 50
MAX_ROWS = 20_000_000
MAX_VALUES_PER_COLUMN = 4
MAX_DERIVED = 16


def _qi(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _qs(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(s)).strip("_")[:24] or "x"


def event_like(schema: Any) -> bool:
    dl = dict(getattr(schema, "domain_likelihood", {}) or {})
    return float(dl.get("event_log", 0.0)) >= 0.3 or (float(dl.get("sensor_stream", 1.0)) < 0.4 and bool(getattr(schema, "time_column", None)))


def derive(ws, settings, schema: Any) -> list[dict[str, Any]]:
    """Adds derived signal columns in place (dataset.parquet rewritten once). Returns [{column, source, description}]."""
    if not event_like(schema) or int(getattr(schema, "n_rows", 0) or 0) > MAX_ROWS:
        return []
    con = ws.duckdb()
    skip = set(getattr(schema, "label_columns", []) or []) | set(getattr(schema, "group_columns", []) or []) | {getattr(schema, "time_column", None), getattr(schema, "order_column", None)}
    derived: list[tuple[str, str, str]] = []  # (new column, SQL expression, description)
    n_rows = int(schema.n_rows or 0)
    for col in list(getattr(schema, "meta_columns", []) or []):
        if col in skip or len(derived) >= MAX_DERIVED:
            continue
        try:
            n_distinct, avg_len, typ = con.execute(f"SELECT COUNT(DISTINCT {_qi(col)}), AVG(length(CAST({_qi(col)} AS VARCHAR))), ANY_VALUE(typeof({_qi(col)})) FROM (SELECT {_qi(col)} FROM dataset USING SAMPLE 200000 ROWS)").fetchone()
        except Exception:
            continue
        textual = str(typ or "").upper() in ("VARCHAR", "BOOLEAN")
        kind = "categorical" if textual and 2 <= int(n_distinct or 0) <= 12 else ("text" if textual and float(avg_len or 0) >= 15 and int(n_distinct or 0) > 12 else "")
        try:
            if kind in ("categorical", "boolean"):
                rows = con.execute(f"SELECT CAST({_qi(col)} AS VARCHAR) AS v, COUNT(*) AS n FROM dataset GROUP BY v ORDER BY n ASC").fetchall()
                vals = [(v, n) for v, n in rows if v is not None and n >= max(5, 0.001 * n_rows)]
                if not (2 <= len(rows) <= 12) or not vals:
                    continue
                changes = con.execute(f"SELECT AVG(CASE WHEN v IS DISTINCT FROM p THEN 1.0 ELSE 0.0 END) FROM (SELECT CAST({_qi(col)} AS VARCHAR) AS v, LAG(CAST({_qi(col)} AS VARCHAR)) OVER (ORDER BY __row__) AS p FROM dataset)").fetchone()[0] or 0.0
                if changes < 0.02:
                    continue  # a column that barely changes is a grouping or a label, not a per-row attribute
                for v, _n in vals[:MAX_VALUES_PER_COLUMN]:  # the rarest values first: ERROR, 500, a failing service
                    name = f"share_{_slug(col)}_{_slug(v)}"
                    expr = f"AVG(CASE WHEN CAST({_qi(col)} AS VARCHAR) = {_qs(v)} THEN 1.0 ELSE 0.0 END) OVER (ORDER BY __row__ ROWS BETWEEN {WINDOW - 1} PRECEDING AND CURRENT ROW)"
                    derived.append((name, expr, f"share of {col} = {v} in the last {WINDOW} rows"))
            elif kind == "text":
                name = f"length_{_slug(col)}"
                derived.append((name, f"CAST(length(CAST({_qi(col)} AS VARCHAR)) AS DOUBLE)", f"length of the text in {col}"))
        except Exception as e:  # one odd column never blocks the others
            ws.log.record("system:ingest", "warning", "column", col, {"event_adapter": str(e)[:200]})
    if not derived:
        return []
    path = ws.path("dataset")
    tmp = path.with_name(path.name + ".derived.tmp")
    sel = ", ".join(f"{expr} AS {_qi(name)}" for name, expr, _ in derived)
    con.execute(f"COPY (SELECT *, {sel} FROM read_parquet({_qs(str(path))}) ORDER BY __row__) TO {_qs(str(tmp))} (FORMAT PARQUET, COMPRESSION ZSTD)")
    try:
        con.execute("DROP VIEW IF EXISTS dataset")
    except Exception:
        pass
    from ..workspace import _replace_with_retry

    _replace_with_retry(tmp, path)
    ws.duckdb()  # re-create the view over the new file
    out = []
    width = max(2, len(str(len(schema.signal_columns) + len(derived))))
    used = set(schema.signal_alias.values())
    nxt = 1
    for name, _expr, desc in derived:
        schema.signal_columns.append(name)
        if name not in schema.columns:
            schema.columns.append(name)
        while f"S{nxt:0{width}d}" in used:
            nxt += 1
        schema.signal_alias[name] = f"S{nxt:0{width}d}"
        used.add(schema.signal_alias[name])
        out.append({"column": name, "alias": schema.signal_alias[name], "description": desc})
    schema.assumptions.append(f"Event-like table: {len(out)} derived signals were added (how often each kind of entry occurs over the last {WINDOW} rows, and text lengths), so the same checks and detectors apply.")
    ws.evidence.add("derived_signals", f"{len(out)} signals derived from categories and free text: " + "; ".join(f"{o['alias']} = {o['description']}" for o in out[:8]), values={"derived": out, "window": WINDOW}, computed_by="ingest.events_adapter.derive", n_samples=n_rows)
    ws.log.record("system:ingest", "derived_signals", "dataset", ws.run_id, {"n": len(out), "columns": [o["column"] for o in out]})
    return out
