"""Shared helpers for the quality stage (agent B).

* Signal catalog access that tolerates missing artifacts (no signals.json -> derive from schema.json ->
  derive from dataset.parquet columns).
* Alias <-> column resolution: dataset.parquet carries the original column names (or col_i) plus
  ``__row__`` / ``__group__``; streamed batches may carry alias columns (S01..) instead. Every check works on
  aliases and resolves whichever naming the frame actually has.
* CHK id allocation (sequential per workspace, continues from checks.jsonl).
* Global robust statistics per signal (median, MAD, min, max, q01, q99) taken from the fingerprints when
  present and otherwise computed once with DuckDB (approximate quantiles, one scan) and cached in
  ``quality_stats.json`` so the stream path never rescans the dataset.
"""
from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

ROW_COL = "__row__"
GROUP_COL = "__group__"
RESERVED = {ROW_COL, GROUP_COL}
ALIAS_RE = re.compile(r"^S\d{2,}$")

NUMERIC_ROLES = {"continuous_measured", "actuator_like", "held_sampled", "derived_redundant", "unknown", "constant"}
NON_SIGNAL_ROLES = {"timestamp", "counter", "categorical", "text", "identifier"}


@dataclass
class SignalInfo:
    alias: str
    column: str  # column name in dataset.parquet
    role: str = "unknown"
    dtype: str = "float"
    excluded: bool = False
    hold_period: Optional[int] = None
    fingerprint: dict[str, Any] = field(default_factory=dict)
    instrument: Optional[str] = None
    unit_operation: Optional[str] = None
    units: Optional[str] = None
    source_column: Optional[str] = None
    related: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_numeric_signal(self) -> bool:
        return self.role not in NON_SIGNAL_ROLES and not self.excluded


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def finite_sql(col_sql: str) -> str:
    """A numeric column, NULL where it holds NaN or +/-inf (any numeric type). DuckDB treats those as ordinary values
    and stddev_samp / var_samp raise 'out of range' on them. Ingest stores them as NULL already; this keeps aggregates
    safe on workspaces written before that and on files that are read directly, not through ingest."""
    return f"(CASE WHEN isfinite(TRY_CAST({col_sql} AS DOUBLE)) THEN {col_sql} END)"


def _fp_get(fp: dict[str, Any], *keys: str) -> Optional[float]:
    """Fetch the first present numeric key, looking also inside a nested 'quantiles' dict."""
    for k in keys:
        if k in fp and fp[k] is not None:
            try:
                v = float(fp[k])
                if math.isfinite(v):
                    return v
            except (TypeError, ValueError):
                pass
        q = fp.get("quantiles")
        if isinstance(q, dict) and k in q and q[k] is not None:
            try:
                v = float(q[k])
                if math.isfinite(v):
                    return v
            except (TypeError, ValueError):
                pass
    return None


def load_catalog(ws: Any) -> list[SignalInfo]:
    """Signal catalog as SignalInfo list. Works with signals.json, else schema.json, else the parquet columns."""
    schema = None
    try:
        schema = ws.schema()
    except Exception:
        schema = None
    alias_of: dict[str, str] = dict(getattr(schema, "signal_alias", {}) or {}) if schema else {}
    column_of: dict[str, str] = {v: k for k, v in alias_of.items()}
    out: list[SignalInfo] = []
    sigs = []
    try:
        sigs = ws.signals()
    except Exception:
        sigs = []
    if sigs:
        for s in sigs:
            col = column_of.get(s.id) or s.source_column or s.id
            fp = dict(s.fingerprint or {})
            hp = fp.get("hold_period")
            try:
                hp = int(hp) if hp is not None and float(hp) > 1 else None
            except (TypeError, ValueError):
                hp = None
            role = s.human_role_override or s.structural_role or "unknown"
            out.append(SignalInfo(alias=s.id, column=col, role=role, dtype=str(s.dtype), excluded=bool(s.excluded), hold_period=hp, fingerprint=fp, instrument=s.instrument_hypothesis, unit_operation=s.unit_operation_hypothesis, units=s.units_hypothesis, source_column=s.source_column, related=list(s.related_signals or [])))
        return out
    if schema and (schema.signal_columns or alias_of):
        cols = list(schema.signal_columns) or list(alias_of.keys())
        for i, c in enumerate(cols):
            alias = alias_of.get(c) or f"S{i + 1:02d}"
            out.append(SignalInfo(alias=alias, column=c, source_column=c))
        return out
    # last resort: numeric parquet columns
    try:
        con = ws.duckdb()
        rows = con.execute("DESCRIBE dataset").fetchall()
    except Exception:
        return out
    i = 0
    for r in rows:
        name, typ = r[0], str(r[1]).upper()
        if name in RESERVED:
            continue
        if any(t in typ for t in ("INT", "FLOAT", "DOUBLE", "DECIMAL", "REAL", "HUGEINT")):
            i += 1
            out.append(SignalInfo(alias=f"S{i:02d}", column=name, source_column=name))
    return out


def numeric_signals(catalog: Iterable[SignalInfo]) -> list[SignalInfo]:
    return [s for s in catalog if s.is_numeric_signal]


def resolve_columns(columns: Iterable[str], catalog: Iterable[SignalInfo]) -> dict[str, str]:
    """alias -> column name present in a frame (alias itself, original column, or source column)."""
    cols = set(columns)
    out: dict[str, str] = {}
    for s in catalog:
        for cand in (s.alias, s.column, s.source_column):
            if cand and cand in cols:
                out[s.alias] = cand
                break
    return out


def time_column_in(columns: Iterable[str], schema: Any) -> Optional[str]:
    cols = set(columns)
    if schema is None:
        return None
    tc = getattr(schema, "time_column", None)
    if tc and tc in cols:
        return tc
    alias = (getattr(schema, "signal_alias", {}) or {}).get(tc) if tc else None
    if alias and alias in cols:
        return alias
    return None


def to_seconds(series: pd.Series, sample_period_seconds: Optional[float] = None) -> np.ndarray:
    """Timestamp-like series -> float seconds (NaN where unparseable)."""
    if pd.api.types.is_datetime64_any_dtype(series):
        s = series
        try:
            if getattr(s.dt, "tz", None) is not None:
                s = s.dt.tz_convert(None)
        except Exception:
            pass
        vals = s.to_numpy(dtype="datetime64[ns]")
        out = vals.astype("int64").astype("float64") / 1e9
        out[np.isnat(vals)] = np.nan
        return out
    if pd.api.types.is_numeric_dtype(series):
        x = series.to_numpy(dtype="float64")
        return x * float(sample_period_seconds) if sample_period_seconds else x
    parsed = pd.to_datetime(series, errors="coerce", utc=True)
    return pd.to_numeric(parsed, errors="coerce").to_numpy(dtype="float64") / 1e9


# ---------------------------------------------------------------- CHK ids
_COUNTERS: dict[str, int] = {}
_COUNTER_LOCK = threading.RLock()


def _scan_max_check_id(ws: Any) -> int:
    p = ws.path("checks")
    n = 0
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                m = re.search(r'"check_id":\s*"CHK-(\d+)"', line)
                if m:
                    n = max(n, int(m.group(1)))
    return n


def next_check_id(ws: Any) -> str:
    key = str(ws.dir)
    with _COUNTER_LOCK:
        if key not in _COUNTERS:
            _COUNTERS[key] = _scan_max_check_id(ws)
        _COUNTERS[key] += 1
        return f"CHK-{_COUNTERS[key]:06d}"


def reset_check_ids(ws: Any) -> None:
    with _COUNTER_LOCK:
        _COUNTERS[str(ws.dir)] = 0


# ---------------------------------------------------------------- global stats
_STATS_ARTIFACT = "quality_stats.json"


def _stats_from_fingerprint(fp: dict[str, Any]) -> dict[str, Optional[float]]:
    return {
        "median": _fp_get(fp, "median", "q50", "p50"),
        "mad": _fp_get(fp, "mad"),
        "min": _fp_get(fp, "min"),
        "max": _fp_get(fp, "max"),
        "q01": _fp_get(fp, "q01", "p01"),
        "q25": _fp_get(fp, "q25", "p25"),
        "q75": _fp_get(fp, "q75", "p75"),
        "q99": _fp_get(fp, "q99", "p99"),
        "mean": _fp_get(fp, "mean"),
        "std": _fp_get(fp, "std"),
        "n": _fp_get(fp, "count", "n"),
        "quantization_step": _fp_get(fp, "quantization_step"),
        "stuck_fraction": _fp_get(fp, "stuck_fraction"),
    }


def global_stats(ws: Any, settings: Any, catalog: list[SignalInfo], force: bool = False) -> dict[str, dict[str, Any]]:
    """Per-alias robust stats. Uses fingerprints, fills gaps with one DuckDB scan, caches in the workspace."""
    if not force:
        cached = ws.read_json(_STATS_ARTIFACT)
        if isinstance(cached, dict) and cached.get("stats"):
            return cached["stats"]
    stats: dict[str, dict[str, Any]] = {}
    need: list[SignalInfo] = []
    for s in numeric_signals(catalog):
        st = _stats_from_fingerprint(s.fingerprint)
        stats[s.alias] = st
        if any(st.get(k) is None for k in ("median", "min", "max", "q01", "q99")) or (st.get("mad") is None and (st.get("q25") is None or st.get("q75") is None)):
            need.append(s)
    if need and ws.exists("dataset"):
        con = ws.duckdb()
        present = {r[0] for r in con.execute("DESCRIBE dataset").fetchall()}
        need = [s for s in need if s.column in present]
        # one streaming scan; approx_quantile (t-digest) keeps memory flat and avoids sorting 15M rows
        for start in range(0, len(need), 40):
            part = need[start : start + 40]
            exprs = []
            for s in part:
                c = finite_sql(quote_ident(s.column))
                exprs.append(f"count({c}), min({c}), max({c}), avg({c}), stddev_samp({c}), approx_quantile({c}, [0.01, 0.25, 0.5, 0.75, 0.99])")
            row = con.execute(f"SELECT {', '.join(exprs)} FROM dataset").fetchone()
            for i, s in enumerate(part):
                n_, mn, mx, mean, std, qs = row[i * 6 : (i + 1) * 6]
                qs = list(qs) if qs is not None else [None] * 5
                vals = {"n": n_, "min": mn, "max": mx, "mean": mean, "std": std, "q01": qs[0], "q25": qs[1], "median": qs[2], "q75": qs[3], "q99": qs[4]}
                st = stats[s.alias]
                for k, v in vals.items():
                    if st.get(k) is None:
                        try:
                            st[k] = float(v) if v is not None and math.isfinite(float(v)) else None
                        except (TypeError, ValueError):
                            st[k] = None
    for s in numeric_signals(catalog):
        st = stats[s.alias]
        st["scale"] = robust_scale(st)
        st["nonneg"] = bool(st.get("min") is not None and st["min"] >= 0.0)
    ws.write_json(_STATS_ARTIFACT, {"stats": stats, "computed_by": "quality._common.global_stats"})
    return stats


def robust_scale(st: dict[str, Any]) -> float:
    """Robust spread: 1.4826*MAD (guarded from below by a quarter of the quantile spread so a tiny MAD on
    quantized data does not make z explode), else IQR/1.349, else the 1-99 % spread, else std (outlier
    sensitive, last resort). 0.0 means 'no spread known'."""
    mad = st.get("mad")
    q01, q99, q25, q75, std = st.get("q01"), st.get("q99"), st.get("q25"), st.get("q75"), st.get("std")
    spread = None
    if q25 is not None and q75 is not None and q75 > q25:
        spread = (float(q75) - float(q25)) / 1.349
    if q01 is not None and q99 is not None and q99 > q01:
        s2 = (float(q99) - float(q01)) / 4.65
        spread = s2 if spread is None else max(spread, s2)
    if mad is not None and mad > 0:
        m = 1.4826 * float(mad)
        return float(max(m, 0.25 * spread)) if spread else m
    if spread:
        return float(spread)
    if std is not None and std > 0:
        return float(std)
    return 0.0


def contiguous_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[(start, end_inclusive)] of True runs in a boolean array."""
    if mask.size == 0:
        return []
    m = np.asarray(mask, dtype=bool)
    d = np.diff(np.concatenate([[0], m.astype(np.int8), [0]]))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1) - 1
    return list(zip(starts.tolist(), ends.tolist()))


def value_runs(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Runs of identical consecutive non-NaN values: (starts, lengths, values). NaN never continues a run."""
    n = x.size
    if n == 0:
        return np.zeros(0, int), np.zeros(0, int), np.zeros(0, float)
    xf = np.asarray(x, dtype="float64")
    same = np.zeros(n, dtype=bool)
    same[1:] = (xf[1:] == xf[:-1]) & ~np.isnan(xf[1:])
    starts = np.flatnonzero(~same)
    lengths = np.diff(np.append(starts, n))
    return starts, lengths, xf[starts]


def fmt_num(v: Any) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if not math.isfinite(v):
        return str(v)
    if abs(v) >= 1e5 or (abs(v) < 1e-3 and v != 0):
        return f"{v:.3g}"
    return f"{v:.4g}"
