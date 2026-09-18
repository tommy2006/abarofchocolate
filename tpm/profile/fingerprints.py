"""Per-signal fingerprints: global aggregates over the full dataset (DuckDB, column batches sized by the
memory budget) plus dynamics computed on a bounded subsample of whole groups / contiguous chunks.

Only aggregates are stored (SignalDescriptor.fingerprint); raw values never leave this module.

    sample = load_dynamics_sample(ws, settings, signal_cols)    # shared with relations.py
    fps = compute_fingerprints(ws, settings, signal_cols, sample, progress)
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from ..ingest.readers import quote_ident
from ..ingest.sample import chunk_plan, sample_description
from ..memory import budget_bytes

QUANTILES = [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]
FLOAT_TYPES = {"FLOAT", "DOUBLE", "REAL"}
ProgressFn = Optional[Callable[[float, str], None]]


def _prog(progress: ProgressFn, f: float, m: str) -> None:
    if progress:
        try:
            progress(float(f), m)
        except Exception:
            pass


def _f(x: Any, nd: int = 6) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return round(v, nd)


# --------------------------------------------------------------------------------------------------
# global aggregates
# --------------------------------------------------------------------------------------------------
def compute_global_stats(ws, settings, signal_cols: list[str], progress: ProgressFn = None) -> dict[str, dict[str, Any]]:
    con = ws.duckdb()
    types = {r[0]: r[1].upper() for r in con.execute("DESCRIBE dataset").fetchall()}
    n_rows = int(con.execute("SELECT count(*) FROM dataset").fetchone()[0])
    exact = n_rows <= 2_000_000
    per_col_bytes = n_rows * 8 * (3 if exact else 0.05) + 1
    batch = int(max(1, min(16 if not exact else 12, budget_bytes(0.3) // per_col_bytes)))
    out: dict[str, dict[str, Any]] = {}
    cols = list(signal_cols)
    qlist = "[" + ", ".join(str(q) for q in QUANTILES) + "]"
    for bi in range(0, len(cols), batch):
        chunk = cols[bi : bi + batch]
        parts = []
        for c in chunk:
            q = quote_ident(c)
            expr = f"(CASE WHEN isnan({q}) THEN NULL ELSE {q} END)" if types.get(c) in FLOAT_TYPES else q
            qfn = "quantile_cont" if exact else "approx_quantile"
            parts.append(
                f"count({expr}), avg({expr}), stddev_samp({expr}), min({expr}), max({expr}), {qfn}({expr}, {qlist}), skewness({expr}), kurtosis({expr}), approx_count_distinct({expr}), "
                + (f"sum(CASE WHEN isinf({q}) THEN 1 ELSE 0 END)" if types.get(c) in FLOAT_TYPES else "0")
                + f", sum(CASE WHEN {expr} <> floor({expr}) THEN 1 ELSE 0 END)"
            )
        row = con.execute(f"SELECT {', '.join(parts)} FROM dataset").fetchone()
        k = 11
        for i, c in enumerate(chunk):
            cnt, mean, sd, mn, mx, qs, sk, ku, nu, ninf, nfrac = row[i * k : (i + 1) * k]
            qs = list(qs) if qs is not None else [None] * len(QUANTILES)
            out[c] = {
                "count": int(cnt or 0),
                "missing_rate": _f(1.0 - (cnt or 0) / n_rows if n_rows else 1.0),
                "mean": _f(mean),
                "std": _f(sd),
                "min": _f(mn),
                "max": _f(mx),
                **{f"q{int(q * 100):02d}": _f(v) for q, v in zip(QUANTILES, qs)},
                "skew": _f(sk),
                "kurtosis": _f(ku),
                "n_unique": int(nu or 0),
                "n_inf": int(ninf or 0),
                "integer_valued": bool((nfrac or 0) == 0 and (cnt or 0) > 0),
                "quantile_method": "exact" if exact else "approx",
            }
            out[c]["iqr"] = _f((out[c]["q75"] or 0) - (out[c]["q25"] or 0)) if out[c]["q75"] is not None and out[c]["q25"] is not None else None
        _prog(progress, min(1.0, (bi + len(chunk)) / max(1, len(cols))) * 0.5, f"global statistics {bi + len(chunk)}/{len(cols)}")
    # share at min / max (second pass, only where min != max)
    parts = []
    todo = [c for c in cols if out[c]["min"] is not None and out[c]["max"] is not None and out[c]["min"] != out[c]["max"]]
    for bi in range(0, len(todo), 40):
        chunk = todo[bi : bi + 40]
        parts = []
        for c in chunk:
            q = quote_ident(c)
            parts.append(f"sum(CASE WHEN {q} = {out[c]['min']!r} THEN 1 ELSE 0 END), sum(CASE WHEN {q} = {out[c]['max']!r} THEN 1 ELSE 0 END)")
        row = con.execute(f"SELECT {', '.join(parts)} FROM dataset").fetchone()
        for i, c in enumerate(chunk):
            cnt = max(1, out[c]["count"])
            out[c]["share_at_min"] = _f((row[2 * i] or 0) / cnt)
            out[c]["share_at_max"] = _f((row[2 * i + 1] or 0) / cnt)
    for c in cols:
        out[c].setdefault("share_at_min", 1.0 if out[c]["count"] else None)
        out[c].setdefault("share_at_max", 1.0 if out[c]["count"] else None)
    return out


# --------------------------------------------------------------------------------------------------
# subsample for dynamics / relations
# --------------------------------------------------------------------------------------------------
def load_dynamics_sample(ws, settings, signal_cols: list[str], max_rows: int = 120_000, max_chunks: int = 40, min_chunk: int = 200) -> dict[str, Any]:
    """Whole groups (or contiguous chunks) spread over the file, bounded in rows. Returns
    {"chunks": [(row_start,row_end,group_id)], "arrays": [np.ndarray (n_i x k) float64], "columns": [...], "description": {...}}"""
    con = ws.duckdb()
    n_rows = int(con.execute("SELECT count(*) FROM dataset").fetchone()[0])
    k = max(1, len(signal_cols))
    max_rows = int(min(max_rows, max(2_000, budget_bytes(0.25) // (k * 8 * 4))))
    blocks = ws.read_json("groups.json", None) or [{"group_id": "0", "row_start": 0, "row_end": n_rows}]
    blocks = [(int(b["row_start"]), int(b["row_end"]), str(b["group_id"])) for b in blocks if int(b["row_end"]) > int(b["row_start"])]
    lengths = np.array([b - a for a, b, _ in blocks], dtype=np.int64)
    median_len = float(np.median(lengths)) if len(lengths) else float(n_rows)
    chunks: list[tuple[int, int, str]] = []
    if len(blocks) > 1 and median_len < max_rows:
        n_pick = int(np.clip(max_rows // max(1, median_len), 6, max_chunks))
        n_pick = min(n_pick, len(blocks))
        idx = np.unique(np.linspace(0, len(blocks) - 1, n_pick).round().astype(int))
        cap = max(min_chunk, max_rows // max(1, len(idx)))
        for i in idx:
            a, b, g = blocks[i]
            chunks.append((a, min(b, a + cap), g))
    else:
        n_chunks = int(np.clip(max_rows // 5000, 4, 24))
        plan = chunk_plan(n_rows, max_rows, n_chunks=n_chunks, min_chunk=min_chunk)
        starts = np.array([a for a, _, _ in blocks], dtype=np.int64)
        ids = [g for _, _, g in blocks]
        for a, b in plan:
            gi = int(np.searchsorted(starts, a, side="right") - 1)
            chunks.append((a, b, ids[max(0, gi)]))
    arrays: list[np.ndarray] = []
    sel = ", ".join(quote_ident(c) for c in signal_cols)
    for a, b, _ in chunks:
        df = con.execute(f"SELECT {sel} FROM dataset WHERE __row__ >= {a} AND __row__ < {b} ORDER BY __row__").df()
        arr = np.empty((len(df), len(signal_cols)), dtype=np.float64)
        for j, c in enumerate(signal_cols):
            try:
                arr[:, j] = pd.to_numeric(df[c], errors="coerce").to_numpy(dtype="float64")
            except Exception:
                arr[:, j] = np.nan
        arrays.append(arr)
    total = int(sum(len(a) for a in arrays))
    desc = {"method": "whole_groups" if (len(blocks) > 1 and median_len < max_rows) else "contiguous_chunks", "n_chunks": len(chunks), "n_rows": total, "total_rows": n_rows, "fraction": round(total / max(1, n_rows), 5), "groups": [g for _, _, g in chunks][:50]}
    return {"chunks": chunks, "arrays": arrays, "columns": list(signal_cols), "description": desc}


# --------------------------------------------------------------------------------------------------
# dynamics
# --------------------------------------------------------------------------------------------------
def _autocorr(z: np.ndarray, lag: int) -> np.ndarray:
    """Lag autocorrelation per column of a standardized (n x k) array with NaNs -> 0."""
    if z.shape[0] <= lag + 2:
        return np.full(z.shape[1], np.nan)
    a, b = z[lag:], z[:-lag]
    ok = np.isfinite(a) & np.isfinite(b)
    a = np.where(ok, a, 0.0)
    b = np.where(ok, b, 0.0)
    n = ok.sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 5, (a * b).sum(axis=0) / np.maximum(n, 1), np.nan)


def _run_lengths(x: np.ndarray) -> np.ndarray:
    x = x[np.isfinite(x)]
    if len(x) < 2:
        return np.array([len(x)], dtype=np.int64)
    chg = np.flatnonzero(x[1:] != x[:-1]) + 1
    edges = np.concatenate([[0], chg, [len(x)]])
    return np.diff(edges)


def _level_sequence(x: np.ndarray) -> np.ndarray:
    x = x[np.isfinite(x)]
    if len(x) < 2:
        return x
    keep = np.concatenate([[True], x[1:] != x[:-1]])
    return x[keep]


def _dominant_period(x: np.ndarray) -> tuple[Optional[float], float]:
    x = x.astype(np.float64)
    ok = np.isfinite(x)
    if ok.sum() < 32:
        return None, 0.0
    if not ok.all():
        idx = np.arange(len(x))
        x = np.interp(idx, idx[ok], x[ok])
    n = len(x)
    t = np.arange(n)
    coef = np.polyfit(t, x, 1)
    x = x - np.polyval(coef, t)
    if np.std(x) == 0:
        return None, 0.0
    x = x * np.hanning(n)
    p = np.abs(np.fft.rfft(x)) ** 2
    if len(p) < 6:
        return None, 0.0
    p[:2] = 0.0  # DC and the lowest bin (trend leftovers)
    i = int(np.argmax(p))
    med = float(np.median(p[2:]))
    prom = float(p[i] / med) if med > 0 else 0.0
    period = n / i if i > 0 else None
    if period is None or period > n / 3:
        return None, prom
    return float(period), prom


def compute_dynamics(sample: dict[str, Any], signal_cols: list[str], progress: ProgressFn = None) -> dict[str, dict[str, Any]]:
    cols = sample["columns"]
    idx = [cols.index(c) for c in signal_cols]
    per: dict[str, list[dict[str, Any]]] = {c: [] for c in signal_cols}
    arrays = sample["arrays"]
    for ci, arr in enumerate(arrays):
        X = arr[:, idx]
        n = X.shape[0]
        if n < 10:
            continue
        mu = np.nanmean(X, axis=0)
        sd = np.nanstd(X, axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            Z = (X - mu) / np.where(sd > 0, sd, np.nan)
        ac1 = _autocorr(Z, 1)
        ac5 = _autocorr(Z, 5)
        D = np.diff(X, axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            noise = np.nanstd(D, axis=0) / np.where(sd > 0, sd, np.nan)
        finite_d = np.isfinite(D)
        n_d = finite_d.sum(axis=0)
        stuck = np.where(n_d > 0, ((D == 0) & finite_d).sum(axis=0) / np.maximum(n_d, 1), np.nan)
        t = np.arange(n, dtype=np.float64)
        tz = (t - t.mean()) / (t.std() or 1.0)
        Zt = np.where(np.isfinite(Z), Z, 0.0)
        trend = np.abs((Zt * tz[:, None]).sum(axis=0) / np.maximum(np.isfinite(Z).sum(axis=0), 1))
        for j, c in enumerate(signal_cols):
            x = X[:, j]
            d = D[:, j]
            d = d[np.isfinite(d)]
            rl = _run_lengths(x)
            lv = _level_sequence(x)
            lv_ac = np.nan
            if len(lv) > 10 and np.std(lv) > 0:
                zl = (lv - lv.mean()) / lv.std()
                lv_ac = float(np.mean(zl[1:] * zl[:-1]))
            nz = np.abs(d[d != 0])
            qstep = float(np.percentile(nz, 1)) if len(nz) else None
            jump_ratio = float(np.median(nz) / sd[j]) if len(nz) and sd[j] > 0 else None
            large_jump = float(np.mean(nz > 0.5 * sd[j])) if len(nz) and sd[j] > 0 else None
            med_rl = float(np.median(rl))
            mad_rl = float(np.median(np.abs(rl - med_rl)))
            hold_reg = float(max(0.0, 1.0 - min(1.0, 1.4826 * mad_rl / med_rl))) if med_rl > 0 else 0.0
            period, prom = _dominant_period(x) if sd[j] > 0 else (None, 0.0)
            per[c].append({"autocorr_lag1": ac1[j], "autocorr_lag5": ac5[j], "noise_level": noise[j], "stuck_fraction": stuck[j], "hold_period": med_rl, "hold_regularity": hold_reg, "level_autocorr": lv_ac, "quantization_step": qstep, "quantization_rel": (qstep / sd[j]) if (qstep is not None and sd[j] > 0) else None, "jump_ratio": jump_ratio, "large_jump_share": large_jump, "dominant_period": period, "dominant_period_prominence": prom, "trend_strength": trend[j], "n": n})
        _prog(progress, 0.5 + 0.5 * (ci + 1) / max(1, len(arrays)), f"dynamics chunk {ci + 1}/{len(arrays)}")
    out: dict[str, dict[str, Any]] = {}
    for c in signal_cols:
        rows = per[c]
        if not rows:
            out[c] = {"n_samples_dynamics": 0, "n_chunks_dynamics": 0}
            continue
        agg: dict[str, Any] = {}
        for key in ("autocorr_lag1", "autocorr_lag5", "noise_level", "stuck_fraction", "hold_period", "hold_regularity", "level_autocorr", "quantization_step", "quantization_rel", "jump_ratio", "large_jump_share", "trend_strength"):
            vals = np.array([r[key] for r in rows if r[key] is not None], dtype=np.float64)
            vals = vals[np.isfinite(vals)]
            agg[key] = _f(np.median(vals)) if len(vals) else None
        periods = np.array([r["dominant_period"] for r in rows if r["dominant_period"] is not None and r["dominant_period_prominence"] >= 8.0], dtype=np.float64)
        proms = np.array([r["dominant_period_prominence"] for r in rows], dtype=np.float64)
        if len(periods) >= max(1, len(rows) // 2):
            agg["dominant_period"] = _f(np.median(periods), 2)
        else:
            agg["dominant_period"] = None
        agg["dominant_period_prominence"] = _f(np.median(proms), 2) if len(proms) else None
        agg["n_samples_dynamics"] = int(sum(r["n"] for r in rows))
        agg["n_chunks_dynamics"] = len(rows)
        out[c] = agg
    return out


def _shape(fp: dict[str, Any]) -> str:
    sk, ku, n = fp.get("skew"), fp.get("kurtosis"), fp.get("count") or 0
    if fp.get("n_unique", 0) <= 1:
        return "constant"
    if fp.get("n_unique", 0) <= 10:
        return "discrete"
    if sk is None or ku is None or n < 50:
        return "unknown"
    bc = (sk * sk + 1.0) / (ku + 3.0 * (n - 1) ** 2 / max(1.0, (n - 2) * (n - 3)))
    fp["bimodality_coefficient"] = _f(bc, 4)
    if bc > 0.555:
        return "bimodal_or_skewed" if abs(sk) > 1.0 else "bimodal"
    if abs(sk) > 1.0:
        return "skewed"
    if ku > 3.0:
        return "heavy_tailed"
    return "unimodal"


def _boundedness(fp: dict[str, Any]) -> str:
    mn, mx = fp.get("min"), fp.get("max")
    if mn is None or mx is None:
        return "unknown"
    if mn >= 0 and mx <= 100 and (mx - mn) >= 20 and (mn < 5 or mx > 95 or (fp.get("share_at_min") or 0) > 0.001 or (fp.get("share_at_max") or 0) > 0.001):
        return "0-100"
    if mn >= 0 and mx <= 1.0000001 and (mx - mn) > 0.2:
        return "0-1"
    if mn >= 0:
        return "nonnegative"
    return "unbounded"


def compute_fingerprints(ws, settings, signal_cols: list[str], sample: dict[str, Any], progress: ProgressFn = None) -> dict[str, dict[str, Any]]:
    t0 = time.time()
    glob = compute_global_stats(ws, settings, signal_cols, progress=lambda f, m: _prog(progress, f, m))
    dyn = compute_dynamics(sample, signal_cols, progress=lambda f, m: _prog(progress, f, m))
    out: dict[str, dict[str, Any]] = {}
    for c in signal_cols:
        fp = {**glob.get(c, {}), **dyn.get(c, {})}
        fp["distribution_shape"] = _shape(fp)
        fp["boundedness"] = _boundedness(fp)
        fp["range_0_100"] = fp["boundedness"] == "0-100"
        fp["sampling"] = sample["description"]
        out[c] = fp
    ws.log.record("system:profile", "fingerprints", "dataset", ws.run_id, {"n_signals": len(signal_cols), "seconds": round(time.time() - t0, 2), "sampling": sample["description"]})
    return out
