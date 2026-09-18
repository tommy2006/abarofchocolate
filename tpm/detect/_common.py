"""Shared helpers for the detect package.

* tolerant loading of the upstream artifacts (schema, signals, relations, batches, trust) with fallbacks
  so detection runs even when an upstream stage did not produce its optional artifact;
* signal resolution: which parquet columns are signals, their aliases (S01..), structural roles;
* bounded DuckDB access (row-range chunks, block samples) returning float32 arrays;
* a time budget helper.

Nothing here reads label columns.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

import numpy as np

EXCLUDED_ROLES = {"constant", "counter", "timestamp", "categorical", "text", "identifier"}
RESERVED = {"__row__", "__group__"}
NUMERIC_DUCK_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "FLOAT", "DOUBLE", "DECIMAL", "REAL")


class Budget:
    """Wall-clock budget. Stages degrade (fewer detectors, smaller samples) instead of exceeding it."""

    def __init__(self, seconds: float, t0: Optional[float] = None):
        self.seconds = float(max(1.0, seconds))
        self.t0 = t0 if t0 is not None else time.time()

    def elapsed(self) -> float:
        return time.time() - self.t0

    def remaining(self) -> float:
        return max(0.0, self.seconds - self.elapsed())

    def fraction_used(self) -> float:
        return min(1.0, self.elapsed() / self.seconds)

    def exhausted(self, margin: float = 0.0) -> bool:
        return self.remaining() <= margin


def qident(name: str) -> str:
    """Quote an identifier for DuckDB."""
    return '"' + str(name).replace('"', '""') + '"'


def qstr(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


# --------------------------------------------------------------------------------------
# artifact loading
# --------------------------------------------------------------------------------------


@dataclass
class DetectInputs:
    """Everything the detect stage knows about the dataset, resolved with fallbacks."""

    columns: list[str]  # parquet column names of the signals (in alias order)
    aliases: list[str]  # S01.. per column
    roles: dict[str, str]  # alias -> structural role
    n_rows: int
    label_columns: list[str] = field(default_factory=list)
    time_column: Optional[str] = None
    group_col: str = "__group__"
    schema: Any = None
    signals: list[Any] = field(default_factory=list)
    relations: dict[str, Any] = field(default_factory=dict)
    batches: list[dict[str, Any]] = field(default_factory=list)
    trust: dict[str, Any] = field(default_factory=dict)  # batch_id -> TrustVerdict
    notes: list[str] = field(default_factory=list)
    signal_names: dict[str, str] = field(default_factory=dict)  # alias -> original column (weak evidence)

    @property
    def p(self) -> int:
        return len(self.columns)

    @property
    def alias_index(self) -> dict[str, int]:
        return {a: i for i, a in enumerate(self.aliases)}


def parquet_columns(ws) -> dict[str, str]:
    """{column: duckdb type} of dataset.parquet."""
    con = ws.duckdb()
    rows = con.execute("DESCRIBE dataset").fetchall()
    return {r[0]: str(r[1]).upper() for r in rows}


def load_inputs(ws, settings, coltypes: Optional[dict[str, str]] = None) -> DetectInputs:
    """coltypes: optional {column: duckdb-like type} override for workspaces without dataset.parquet
    (streaming path); numeric types are those in NUMERIC_DUCK_TYPES."""
    notes: list[str] = []
    schema = None
    try:
        schema = ws.schema()
    except Exception as e:  # malformed schema -> fallback
        notes.append(f"schema.json unreadable ({e}); falling back to parquet columns")
    if coltypes is None:
        coltypes = parquet_columns(ws)
    numeric_cols = [c for c, t in coltypes.items() if any(t.startswith(k) for k in NUMERIC_DUCK_TYPES)]
    label_columns: list[str] = []
    meta_columns: list[str] = []
    time_column = None
    group_col = "__group__"
    alias_map: dict[str, str] = {}
    signal_columns: list[str] = []
    if schema is not None:
        label_columns = list(schema.label_columns or [])
        meta_columns = list(schema.meta_columns or [])
        time_column = schema.time_column
        group_col = schema.group_column or "__group__"
        alias_map = dict(schema.signal_alias or {})
        signal_columns = list(schema.signal_columns or [])
    if group_col not in coltypes:
        if "__group__" in coltypes:
            group_col = "__group__"
        else:
            notes.append("no group column in dataset.parquet; treating the dataset as one group")
            group_col = ""
    if not signal_columns:
        signal_columns = [c for c in numeric_cols if c not in RESERVED and c not in label_columns and c not in meta_columns and c != time_column]
        notes.append("schema.signal_columns empty; using all numeric parquet columns")

    # signals.json: roles and exclusions
    signals = []
    try:
        signals = ws.signals()
    except Exception as e:
        notes.append(f"signals.json unreadable ({e})")
    by_alias = {s.id: s for s in signals}
    by_source = {s.source_column: s for s in signals if s.source_column}

    columns: list[str] = []
    aliases: list[str] = []
    roles: dict[str, str] = {}
    names: dict[str, str] = {}
    for i, c in enumerate(signal_columns):
        alias = alias_map.get(c) or (by_source[c].id if c in by_source else None) or f"S{i + 1:02d}"
        # which parquet column holds this signal
        col = c if c in coltypes else (alias if alias in coltypes else None)
        if col is None or col not in numeric_cols:
            continue
        if col in label_columns or col in RESERVED:
            continue
        desc = by_alias.get(alias) or by_source.get(c)
        role = "unknown"
        if desc is not None:
            role = desc.human_role_override or desc.structural_role or "unknown"
            if desc.excluded:
                continue
        if role in EXCLUDED_ROLES:
            continue
        columns.append(col)
        aliases.append(alias)
        roles[alias] = role
        names[alias] = c
    if not columns:
        raise ValueError("no usable signal columns for detection")

    if schema is not None and schema.n_rows:
        n_rows = int(schema.n_rows)
    elif ws.exists("dataset"):
        n_rows = int(ws.duckdb().execute("SELECT COUNT(*) FROM dataset").fetchone()[0])
    else:
        n_rows = 0
    relations = load_relations(ws)
    batches = load_batches(ws)
    trust = load_trust(ws)
    return DetectInputs(columns=columns, aliases=aliases, roles=roles, n_rows=n_rows, label_columns=label_columns, time_column=time_column, group_col=group_col, schema=schema, signals=signals, relations=relations, batches=batches, trust=trust, notes=notes, signal_names=names)


def load_relations(ws) -> dict[str, Any]:
    """Tolerant reader for relations.json -> {clusters: {cid: [alias]}, pairs: [{a,b,r,lag}], corr: {a:{b:r}}}."""
    d = ws.read_json("relations", None)
    if not isinstance(d, dict):
        return {}
    out: dict[str, Any] = {"clusters": {}, "pairs": [], "corr": {}, "leaders": {}, "redundancy": d.get("redundancy", [])}
    clusters = d.get("clusters", {})
    if isinstance(clusters, dict):
        for cid, members in clusters.items():
            if isinstance(members, dict):
                members = members.get("signals") or members.get("members") or []
            out["clusters"][str(cid)] = [str(m) for m in members]
    elif isinstance(clusters, list):
        for i, c in enumerate(clusters):
            if isinstance(c, dict):
                cid = str(c.get("id") or c.get("cluster_id") or f"C{i + 1:02d}")
                members = c.get("signals") or c.get("members") or []
            else:
                cid, members = f"C{i + 1:02d}", c
            out["clusters"][cid] = [str(m) for m in members]
    pairs = d.get("pairs", [])
    if isinstance(pairs, list):
        for pr in pairs:
            if isinstance(pr, dict) and "a" in pr and "b" in pr:
                r = pr.get("r_at_lag", pr.get("r", pr.get("r0", 0.0)))
                out["pairs"].append({"a": str(pr["a"]), "b": str(pr["b"]), "r": float(r or 0.0), "lag": int(pr.get("lag", 0) or 0)})
    corr = d.get("corr")
    if isinstance(corr, dict):
        out["corr"] = {str(a): {str(b): float(v) for b, v in row.items()} for a, row in corr.items() if isinstance(row, dict)}
    elif isinstance(corr, list) and d.get("signals"):
        sig = [str(s) for s in d["signals"]]
        for i, a in enumerate(sig):
            row = corr[i] if i < len(corr) else []
            out["corr"][a] = {b: float(row[j]) for j, b in enumerate(sig) if j < len(row)}
    leaders = d.get("leaders", {})
    if isinstance(leaders, dict):
        out["leaders"] = {str(k): v for k, v in leaders.items()}
    return out


def load_batches(ws) -> list[dict[str, Any]]:
    d = ws.read_json("batches", None)
    if isinstance(d, dict):
        d = d.get("batches", [])
    if not isinstance(d, list):
        return []
    out = []
    for b in d:
        if isinstance(b, dict) and "batch_id" in b and "row_start" in b:
            out.append({"batch_id": str(b["batch_id"]), "row_start": int(b["row_start"]), "row_end": int(b.get("row_end", b["row_start"]))})
    out.sort(key=lambda b: b["row_start"])
    return out


def batch_ids_for_rows(batches: list[dict[str, Any]], rows: np.ndarray) -> Optional[np.ndarray]:
    """Vectorised batch lookup (row_end is treated as exclusive if row_end > row_start of the next batch, else inclusive)."""
    if not batches:
        return None
    starts = np.array([b["row_start"] for b in batches], dtype=np.int64)
    ids = np.array([b["batch_id"] for b in batches], dtype=object)
    idx = np.searchsorted(starts, rows, side="right") - 1
    out = np.where(idx >= 0, ids[np.clip(idx, 0, len(ids) - 1)], None)
    return out


def load_trust(ws) -> dict[str, Any]:
    try:
        verdicts = ws.trust()
    except Exception:
        return {}
    return {v.batch_id: v for v in verdicts}


# --------------------------------------------------------------------------------------
# data access
# --------------------------------------------------------------------------------------


def _arrow(rel):
    """DuckDB result -> pyarrow Table across duckdb versions."""
    for name in ("to_arrow_table", "fetch_arrow_table", "arrow"):
        fn = getattr(rel, name, None)
        if fn is not None:
            return fn()
    raise RuntimeError("duckdb result has no arrow conversion")


def group_table(ws, group_col: str) -> list[dict[str, Any]]:
    """[{group, row_min, row_max, n}] ordered by first row. Groups need not be contiguous."""
    con = ws.duckdb()
    if group_col:
        q = f"SELECT CAST({qident(group_col)} AS VARCHAR) AS g, MIN(__row__), MAX(__row__), COUNT(*) FROM dataset GROUP BY g ORDER BY 2"
    else:
        q = "SELECT '0' AS g, MIN(__row__), MAX(__row__), COUNT(*) FROM dataset"
    rows = con.execute(q).fetchall()
    return [{"group": str(r[0]), "row_min": int(r[1]), "row_max": int(r[2]), "n": int(r[3])} for r in rows]


def _select_clause(columns: list[str], group_col: str) -> str:
    g = f"CAST({qident(group_col)} AS VARCHAR) AS __group__" if group_col else "'0' AS __group__"
    cols = ", ".join(f"CAST({qident(c)} AS FLOAT) AS {qident(c)}" for c in columns)
    return f"__row__, {g}, {cols}"


def _table_to_arrays(tbl, columns: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rows = tbl.column("__row__").to_numpy().astype(np.int64, copy=False)
    groups = np.asarray(tbl.column("__group__").to_pylist(), dtype=object)
    n = len(rows)
    X = np.empty((n, len(columns)), dtype=np.float32)
    for j, c in enumerate(columns):
        col = tbl.column(c)
        arr = col.to_numpy(zero_copy_only=False)
        X[:, j] = arr.astype(np.float32, copy=False) if arr.dtype != np.float32 else arr
    return rows, groups, X


def fetch_range(ws, columns: list[str], group_col: str, row_start: int, row_end: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rows with row_start <= __row__ < row_end ordered by __row__ -> (rows, groups, X float32)."""
    con = ws.duckdb()
    q = f"SELECT {_select_clause(columns, group_col)} FROM dataset WHERE __row__ >= {int(row_start)} AND __row__ < {int(row_end)} ORDER BY __row__"
    tbl = _arrow(con.execute(q))
    return _table_to_arrays(tbl, columns)


def fetch_group_range(ws, columns: list[str], group_col: str, group: str, row_start: int, row_end: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    con = ws.duckdb()
    where = f"__row__ >= {int(row_start)} AND __row__ < {int(row_end)}"
    if group_col:
        where += f" AND CAST({qident(group_col)} AS VARCHAR) = {qstr(group)}"
    q = f"SELECT {_select_clause(columns, group_col)} FROM dataset WHERE {where} ORDER BY __row__"
    tbl = _arrow(con.execute(q))
    return _table_to_arrays(tbl, columns)


def fetch_blocks(ws, columns: list[str], group_col: str, blocks: list[tuple[int, int]]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rows falling in any [start, end) block, ordered by __row__."""
    import pyarrow as pa

    con = ws.duckdb()
    if not blocks:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=object), np.zeros((0, len(columns)), dtype=np.float32)
    starts = pa.array([int(b[0]) for b in blocks], type=pa.int64())
    ends = pa.array([int(b[1]) for b in blocks], type=pa.int64())
    blk = pa.table({"s": starts, "e": ends})
    con.register("__blk", blk)
    try:
        q = f"SELECT {_select_clause(columns, group_col)} FROM dataset d JOIN __blk b ON d.__row__ >= b.s AND d.__row__ < b.e ORDER BY d.__row__"
        tbl = _arrow(con.execute(q))
    finally:
        try:
            con.unregister("__blk")
        except Exception:
            pass
    return _table_to_arrays(tbl, columns)


def iter_chunks(n_rows: int, chunk: int) -> Iterator[tuple[int, int]]:
    start = 0
    while start < n_rows:
        end = min(n_rows, start + chunk)
        yield start, end
        start = end


def row_bounds(ws) -> tuple[int, int]:
    r = ws.duckdb().execute("SELECT MIN(__row__), MAX(__row__) FROM dataset").fetchone()
    return int(r[0]), int(r[1])


def plan_blocks(groups: list[dict[str, Any]], max_rows: int, block_len: int, lead: int) -> list[tuple[int, int, str]]:
    """Contiguous row blocks per group, evenly spread, totalling <= max_rows (+ lead-in rows that are dropped
    after feature computation). Returns [(row_start, row_end, group)]."""
    total = sum(g["n"] for g in groups)
    if total <= max_rows:
        return [(g["row_min"], g["row_max"] + 1, g["group"]) for g in groups]
    out: list[tuple[int, int, str]] = []
    # quota proportional to group size but at least one block per group (bounded by group size)
    n_groups = len(groups)
    min_quota = min(block_len, max_rows // max(1, n_groups))
    for g in groups:
        quota = max(min_quota, int(max_rows * g["n"] / total))
        span = g["row_max"] + 1 - g["row_min"]
        if span <= quota + lead:
            out.append((g["row_min"], g["row_max"] + 1, g["group"]))
            continue
        n_blocks = max(1, int(math.ceil(quota / block_len)))
        blen = min(block_len, quota)
        if n_blocks == 1:
            starts = [g["row_min"]]
        else:
            step = (span - blen - lead) / (n_blocks - 1)
            starts = [g["row_min"] + int(round(i * step)) for i in range(n_blocks)]
        for s in starts:
            e = min(g["row_max"] + 1, s + blen + lead)
            out.append((s, e, g["group"]))
    out.sort()
    return out


def segments(groups: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous runs of equal group id in an ordered array -> [(start, end)]."""
    n = len(groups)
    if n == 0:
        return []
    change = np.flatnonzero(groups[1:] != groups[:-1]) + 1
    starts = np.concatenate([[0], change])
    ends = np.concatenate([change, [n]])
    return list(zip(starts.tolist(), ends.tolist()))


def runs_of_true(mask: np.ndarray) -> list[tuple[int, int]]:
    """[(start, end)] of True runs (end exclusive)."""
    if mask.size == 0:
        return []
    m = np.concatenate([[False], mask.astype(bool), [False]])
    d = np.diff(m.astype(np.int8))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    return list(zip(starts.tolist(), ends.tolist()))


def robust_scale(x: np.ndarray, axis: int = 0, floor: float = 1e-6) -> tuple[np.ndarray, np.ndarray]:
    """Median and MAD*1.4826 (floored) along axis, NaN-aware."""
    med = np.nanmedian(x, axis=axis)
    mad = np.nanmedian(np.abs(x - np.expand_dims(med, axis)), axis=axis) * 1.4826
    std = np.nanstd(x, axis=axis)
    # signals whose MAD is 0 but std is not (heavily quantised) -> fall back to std/2
    mad = np.where(mad < floor, np.maximum(std * 0.5, floor), mad)
    return np.nan_to_num(med, nan=0.0), np.nan_to_num(mad, nan=1.0)


def topk_mean_sq(a: np.ndarray, k: int) -> np.ndarray:
    """sqrt(mean of the k largest squared entries per row)."""
    k = max(1, min(k, a.shape[1]))
    sq = a * a
    if k >= a.shape[1]:
        return np.sqrt(sq.mean(axis=1))
    part = np.partition(sq, -k, axis=1)[:, -k:]
    return np.sqrt(part.mean(axis=1))


def compute_relations(X: np.ndarray, aliases: list[str], roles: Optional[dict[str, str]] = None, max_lag: int = 10, groups: Optional[np.ndarray] = None, corr_min: float = 0.5, cluster_min: float = 0.6) -> dict[str, Any]:
    """Fallback relations computation (also used by tests): correlation matrix, lagged pairs, clusters.
    Lag search is done on a bounded subsample so it stays cheap."""
    n, p = X.shape
    Xf = np.where(np.isfinite(X), X, np.nan).astype(np.float64)
    med = np.nanmedian(Xf, axis=0)
    Xf = np.where(np.isnan(Xf), med, Xf)
    std = Xf.std(axis=0)
    ok = std > 1e-12
    Z = (Xf - Xf.mean(axis=0)) / np.where(ok, std, 1.0)
    Z[:, ~ok] = 0.0
    C = (Z.T @ Z) / max(1, n - 1)
    C = np.clip(np.nan_to_num(C), -1, 1)
    np.fill_diagonal(C, 1.0)
    corr = {aliases[i]: {aliases[j]: round(float(C[i, j]), 4) for j in range(p)} for i in range(p)}
    pairs = []
    # lagged xcorr on a bounded subsample of rows (contiguous prefix keeps the time structure)
    m = min(n, 20000)
    Zs = Z[:m]
    for i in range(p):
        for j in range(i + 1, p):
            r0 = float(C[i, j])
            if abs(r0) < corr_min or not ok[i] or not ok[j]:
                continue
            best_r, best_lag = r0, 0
            for lag in range(1, max_lag + 1):
                if m - lag < 30:
                    break
                r_pos = float(np.mean(Zs[lag:, i] * Zs[:-lag, j]))  # i lags j by `lag`
                r_neg = float(np.mean(Zs[:-lag, i] * Zs[lag:, j]))  # j lags i
                if abs(r_pos) > abs(best_r) + 0.01:
                    best_r, best_lag = r_pos, -lag
                if abs(r_neg) > abs(best_r) + 0.01:
                    best_r, best_lag = r_neg, lag
            pairs.append({"a": aliases[i], "b": aliases[j], "r": round(best_r, 4), "lag": int(best_lag), "r0": round(r0, 4)})
    # clusters: connected components over |r| >= cluster_min (excluding constants)
    parent = list(range(p))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(p):
        for j in range(i + 1, p):
            if ok[i] and ok[j] and abs(C[i, j]) >= cluster_min:
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[ra] = rb
    comp: dict[int, list[int]] = {}
    for i in range(p):
        if ok[i]:
            comp.setdefault(find(i), []).append(i)
    clusters: dict[str, list[str]] = {}
    leaders: dict[str, str] = {}
    k = 0
    for members in sorted(comp.values(), key=lambda mm: (-len(mm), mm[0])):
        k += 1
        cid = f"C{k:02d}"
        clusters[cid] = [aliases[i] for i in members]
        # leader = member with highest mean |r| to the others
        if len(members) > 1:
            sub = np.abs(C[np.ix_(members, members)])
            leaders[cid] = aliases[members[int(np.argmax(sub.mean(axis=1)))]]
        else:
            leaders[cid] = aliases[members[0]]
    redundancy = [{"a": pr["a"], "b": pr["b"], "r": pr["r0"]} for pr in pairs if abs(pr["r0"]) > 0.995]
    return {"signals": aliases, "corr": corr, "pairs": pairs, "clusters": clusters, "leaders": leaders, "redundancy": redundancy, "computed_by": "detect._common.compute_relations", "n_samples": int(n)}


def progress_cb(ctx: Optional[dict[str, Any]]) -> Callable[[float, str], None]:
    fn = (ctx or {}).get("progress")
    if callable(fn):
        def cb(frac: float, msg: str = "") -> None:
            try:
                fn(float(frac), str(msg))
            except Exception:
                pass
        return cb
    return lambda frac, msg="": None


def stage_budget(settings, ctx: Optional[dict[str, Any]], default_key: str = "detect") -> Budget:
    """Detect budget = min(settings.detect.time_budget_s, what is left of the pipeline budget)."""
    seconds = float(getattr(settings.detect, "time_budget_s", 600))
    ctx = ctx or {}
    if ctx.get("time_budget_s") and ctx.get("t_start"):
        left = float(ctx["time_budget_s"]) - (time.time() - float(ctx["t_start"]))
        # leave room for diagnose/assess/report
        seconds = min(seconds, max(30.0, left * 0.6))
    elif ctx.get("time_budget_s"):
        seconds = min(seconds, float(ctx["time_budget_s"]))
    return Budget(seconds)
