"""Regime coverage: which operating regimes the data covers, how balanced they are, and which signals carry
information. Units are groups (when there are at least 4) or fixed-size row windows. Each unit gets a fingerprint
(mean and std of every checkable signal, one DuckDB GROUP BY), fingerprints are robustly standardised and
clustered with k-means; k is chosen by silhouette on a subsample. Everything reported is an aggregate.
"""
from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np

from ..quality._common import GROUP_COL, ROW_COL, SignalInfo, finite_sql, global_stats, load_catalog, numeric_signals, quote_ident
from ..quality.checks import CHECKABLE_ROLES

MAX_SIGNALS = 300
MAX_UNITS_SILHOUETTE = 2000


def unit_definition(ws: Any, min_groups: int = 4, target_windows: int = 60) -> dict[str, Any]:
    """{'kind': 'group'|'window', 'expr': sql, 'n_units': int, 'window': int|None, 'n_rows': int}"""
    con = ws.duckdb()
    cols = {r[0] for r in con.execute("DESCRIBE dataset").fetchall()}
    n_rows = int(con.execute("SELECT count(*) FROM dataset").fetchone()[0])
    if GROUP_COL in cols:
        n_groups = int(con.execute(f"SELECT count(DISTINCT {quote_ident(GROUP_COL)}) FROM dataset").fetchone()[0])
        if n_groups >= min_groups:
            return {"kind": "group", "expr": f"CAST({quote_ident(GROUP_COL)} AS VARCHAR)", "n_units": n_groups, "window": None, "n_rows": n_rows}
    w = max(100, int(math.ceil(n_rows / max(1, target_windows))))
    n_units = int(math.ceil(n_rows / w)) if n_rows else 0
    return {"kind": "window", "expr": f"CAST(floor({quote_ident(ROW_COL)} / {w}) AS VARCHAR)", "n_units": n_units, "window": w, "n_rows": n_rows}


def coverage_signals(catalog: list[SignalInfo]) -> list[SignalInfo]:
    sigs = [s for s in numeric_signals(catalog) if s.role in CHECKABLE_ROLES]
    return sigs[:MAX_SIGNALS]


def unit_fingerprints(ws: Any, unit: dict[str, Any], sigs: list[SignalInfo]) -> tuple[list[str], np.ndarray, list[str], np.ndarray]:
    """(units, X[n_units, 2p] of (mean, std) per signal, feature names, unit sizes) ordered by first row."""
    con = ws.duckdb()
    present = {r[0] for r in con.execute("DESCRIBE dataset").fetchall()}
    sigs = [s for s in sigs if s.column in present]
    aggs = ", ".join(f"avg({finite_sql(quote_ident(s.column))}), stddev_samp({finite_sql(quote_ident(s.column))})" for s in sigs)
    q = f"SELECT {unit['expr']} AS u, count(*) AS n, min({quote_ident(ROW_COL)}) AS r0{', ' + aggs if aggs else ''} FROM dataset GROUP BY u ORDER BY r0"
    rows = con.execute(q).fetchall()
    units = [str(r[0]) for r in rows]
    sizes = np.array([int(r[1]) for r in rows], dtype="int64")
    X = np.array([[float(v) if v is not None else np.nan for v in r[3:]] for r in rows], dtype="float64") if sigs else np.zeros((len(rows), 0))
    names = [f"{s.alias}:{k}" for s in sigs for k in ("mean", "std")]
    return units, X, names, sizes


def _standardize(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Robust z per column; drops columns without spread. Returns (Z, center, scale, keep_mask)."""
    if X.size == 0:
        return X, np.zeros(0), np.ones(0), np.zeros(0, dtype=bool)
    center = np.nanmedian(X, axis=0)
    q75, q25 = np.nanpercentile(X, 75, axis=0), np.nanpercentile(X, 25, axis=0)
    scale = (q75 - q25) / 1.349
    sd = np.nanstd(X, axis=0)
    scale = np.where(scale > 0, scale, sd)
    keep = np.isfinite(scale) & (scale > 1e-12) & np.isfinite(center)
    Z = (X[:, keep] - center[keep]) / scale[keep]
    Z = np.nan_to_num(Z, nan=0.0, posinf=0.0, neginf=0.0)
    Z = np.clip(Z, -8, 8)
    return Z, center, scale, keep


def _choose_k(Z: np.ndarray, k_max: int = 8, seed: int = 0) -> tuple[int, float, Optional[np.ndarray], Optional[np.ndarray]]:
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    n = Z.shape[0]
    if n < 4 or Z.shape[1] == 0:
        return 1, 0.0, None, None
    rng = np.random.default_rng(seed)
    sub = rng.choice(n, size=min(n, MAX_UNITS_SILHOUETTE), replace=False) if n > MAX_UNITS_SILHOUETTE else np.arange(n)
    best = (1, -1.0, None, None)
    for k in range(2, min(k_max, n - 1) + 1):
        km = KMeans(n_clusters=k, n_init=5, random_state=seed).fit(Z)
        labels = km.labels_
        if len(set(labels[sub].tolist())) < 2:
            continue
        try:
            sil = float(silhouette_score(Z[sub], labels[sub]))
        except ValueError:
            continue
        if sil > best[1]:
            best = (k, sil, labels, km.cluster_centers_)
    if best[1] < 0.15:  # no convincing structure
        return 1, best[1], None, None
    return best


def _label_balance(ws: Any, schema: Any) -> Optional[dict[str, Any]]:
    cols = list(getattr(schema, "label_columns", []) or []) if schema else []
    if not cols:
        return None
    con = ws.duckdb()
    present = {r[0] for r in con.execute("DESCRIBE dataset").fetchall()}
    col = next((c for c in cols if c in present), None)
    if col is None:
        return None
    rows = con.execute(f"SELECT CAST({quote_ident(col)} AS VARCHAR) v, count(*) n FROM dataset GROUP BY v ORDER BY n DESC LIMIT 20").fetchall()
    total = sum(int(r[1]) for r in rows) or 1
    shares = [int(r[1]) / total for r in rows]
    ent = -sum(p * math.log(p) for p in shares if p > 0)
    balance = ent / math.log(len(shares)) if len(shares) > 1 else 1.0
    return {"column": col, "n_classes": len(rows), "majority_share": round(max(shares), 4), "balance": round(balance, 4), "note": "labels are used for evaluation only, never for detection", "class_counts": [{"value": str(r[0])[:40], "n": int(r[1])} for r in rows[:10]]}


def _redundant_pairs(ws: Any, sigs: list[SignalInfo], relations: Any, sample_rows: int = 20000) -> list[dict[str, Any]]:
    pairs: dict[tuple[str, str], float] = {}
    aliases = {s.alias for s in sigs}

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            a = obj.get("a") or obj.get("signal_a") or obj.get("x")
            b = obj.get("b") or obj.get("signal_b") or obj.get("y")
            r = obj.get("r", obj.get("corr", obj.get("correlation")))
            if a in aliases and b in aliases and a != b and r is not None:
                try:
                    if abs(float(r)) >= 0.98:
                        pairs[tuple(sorted((a, b)))] = float(r)  # type: ignore[index]
                except (TypeError, ValueError):
                    pass
            for v in obj.values():
                if isinstance(v, (list, dict)):
                    walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(relations)
    if not pairs and len(sigs) >= 2:
        con = ws.duckdb()
        present = {r[0] for r in con.execute("DESCRIBE dataset").fetchall()}
        sigs = [s for s in sigs if s.column in present][:200]
        sel = ", ".join(quote_ident(s.column) for s in sigs)
        try:
            X = con.execute(f"SELECT * FROM (SELECT {sel} FROM dataset) USING SAMPLE reservoir({int(sample_rows)} ROWS)").df().to_numpy(dtype="float64")
        except Exception:
            return []
        ok = np.isfinite(X).all(axis=1)
        X = X[ok]
        if X.shape[0] >= 30:
            with np.errstate(all="ignore"):
                C = np.corrcoef(X, rowvar=False)
            for i in range(len(sigs)):
                for j in range(i + 1, len(sigs)):
                    r = C[i, j]
                    if np.isfinite(r) and abs(r) >= 0.98:
                        pairs[(sigs[i].alias, sigs[j].alias)] = float(r)
    return [{"a": a, "b": b, "r": round(r, 4)} for (a, b), r in sorted(pairs.items())]


def regime_coverage(ws: Any, settings: Any, catalog: Optional[list[SignalInfo]] = None, seed: int = 0) -> dict[str, Any]:
    catalog = catalog or load_catalog(ws)
    sigs = coverage_signals(catalog)
    stats = global_stats(ws, settings, catalog)
    schema = None
    try:
        schema = ws.schema()
    except Exception:
        schema = None
    unit = unit_definition(ws)
    units, X, names, sizes = unit_fingerprints(ws, unit, sigs)
    n_units = len(units)
    Z, center, scale, keep = _standardize(X)
    k, sil, labels, centers = _choose_k(Z, seed=seed)
    if labels is None:
        labels = np.zeros(n_units, dtype=int)
        centers = Z.mean(axis=0, keepdims=True) if Z.size else np.zeros((1, 0))
        k = 1
    # regimes ordered by size
    order = np.argsort([-int((labels == c).sum()) for c in range(k)])
    remap = {int(c): i for i, c in enumerate(order)}
    lab = np.array([remap[int(c)] for c in labels], dtype=int)
    centers = centers[order] if centers is not None and len(centers) == k else centers
    kept_names = [n for n, kp in zip(names, keep) if kp]
    regimes = []
    total_rows = int(sizes.sum()) if sizes.size else 0
    for i in range(k):
        m = lab == i
        n_u = int(m.sum())
        share = n_u / max(1, n_units)
        rid = f"R{i + 1}"
        distinguishing = []
        if k > 1 and centers is not None and Z.shape[1]:
            cz = centers[i]
            top = np.argsort(-np.abs(cz))[:4]
            distinguishing = [{"feature": kept_names[j], "z": round(float(cz[j]), 2), "direction": "high" if cz[j] > 0 else "low"} for j in top if abs(cz[j]) > 0.5]
        regimes.append({"regime_id": rid, "n_units": n_u, "share": round(share, 4), "n_rows": int(sizes[m].sum()) if sizes.size else 0, "units": [units[j] for j in np.flatnonzero(m)[:50]], "distinguishing": distinguishing, "thin": bool(k > 1 and (share < max(0.05, 2.0 / max(1, n_units)) or n_u < 3))})
    thin = [r["regime_id"] for r in regimes if r["thin"]]
    shares = [r["share"] for r in regimes]
    balance = (-sum(p * math.log(p) for p in shares if p > 0) / math.log(k)) if k > 1 else 1.0
    # per-signal information
    near_constant = []
    for s in sigs:
        st = stats.get(s.alias, {})
        fp = s.fingerprint or {}
        med, sc = st.get("median"), st.get("scale") or 0.0
        sf = fp.get("stuck_fraction")
        stuck_like = sf is not None and float(sf) > 0.9 and s.role in ("continuous_measured", "unknown")  # actuators/held signals hold by nature
        if s.role == "constant" or sc <= 0 or (med is not None and abs(med) > 0 and sc / abs(med) < 1e-6) or stuck_like:
            near_constant.append(s.alias)
    const_roles = [s.alias for s in numeric_signals(catalog) if s.role == "constant" and s.alias not in near_constant]
    near_constant.extend(const_roles)
    redundant = _redundant_pairs(ws, [s for s in sigs if s.alias not in near_constant], ws.read_json("relations", None))
    low_info = sorted(set(near_constant) | {p["b"] for p in redundant})
    label_info = _label_balance(ws, schema)
    coverage_score = round(0.7 if k == 1 else (0.6 * balance + 0.4 * (1.0 - len(thin) / k)), 4)
    findings: list[str] = []
    if k == 1:
        findings.append(f"No distinct operating regimes found among {n_units} {unit['kind']}s (silhouette {sil:.2f}); the data looks like one regime.")
    else:
        findings.append(f"{k} operating regimes found among {n_units} {unit['kind']}s (silhouette {sil:.2f}); shares " + ", ".join(f"{r['regime_id']} {r['share']:.0%}" for r in regimes) + f"; balance {balance:.2f}.")
        for r in regimes:
            if r["thin"]:
                findings.append(f"Regime {r['regime_id']} is thin: only {r['n_units']} {unit['kind']}(s); a model will rarely see it, more data from this regime would help coverage.")
    if near_constant:
        findings.append(f"{len(near_constant)} signal(s) carry (almost) no information: {', '.join(near_constant[:10])}.")
    if redundant:
        findings.append(f"{len(redundant)} redundant pair(s) (|r| >= 0.98): " + ", ".join(f"{p['a']}~{p['b']}" for p in redundant[:6]) + ".")
    if label_info:
        findings.append(f"Label column {label_info['column']} has {label_info['n_classes']} classes, majority share {label_info['majority_share']:.0%} (evaluation only).")
    ev_ids = []
    ev = ws.evidence.add("regime_coverage", findings[0], values={"k": k, "silhouette": round(sil, 4), "n_units": n_units, "unit_kind": unit["kind"], "shares": shares, "balance": round(balance, 4), "thin": thin}, computed_by="assessor.coverage.regime_coverage", n_samples=n_units)
    ev_ids.append(ev.id)
    if near_constant or redundant:
        ev2 = ws.evidence.add("signal_information", f"{len(near_constant)} near-constant signals, {len(redundant)} redundant pairs", signals=(near_constant + [p["a"] for p in redundant] + [p["b"] for p in redundant])[:30], values={"near_constant": near_constant, "redundant_pairs": redundant}, computed_by="assessor.coverage.regime_coverage", n_samples=total_rows)
        ev_ids.append(ev2.id)
    out = {
        "unit": unit, "n_units": n_units, "k": k, "silhouette": round(float(sil), 4), "balance": round(float(balance), 4), "regimes": regimes, "thin_regimes": thin,
        "unit_regime": {u: f"R{int(l) + 1}" for u, l in zip(units, lab)}, "unit_sizes": {u: int(n) for u, n in zip(units, sizes)},
        "signals": {"near_constant": near_constant, "redundant_pairs": redundant, "low_information": low_info, "n_used": len(sigs)},
        "labels": label_info, "coverage_score": coverage_score, "findings": findings, "evidence_ids": ev_ids,
        # the scaler and centroids let a new file be projected onto the same regimes (aggregates only)
        "scaler": {"features": names, "center": [float(c) if np.isfinite(c) else None for c in center], "scale": [float(s) if np.isfinite(s) else None for s in scale], "keep": [bool(x) for x in keep]},
        "centroids": centers.tolist() if centers is not None else [],
        "novelty_threshold": None,
    }
    if centers is not None and Z.shape[1] and k >= 1:
        d = np.linalg.norm(Z - centers[lab], axis=1)
        out["novelty_threshold"] = float(np.percentile(d, 95)) if d.size else None
    ws.log.record("system:assessor", "coverage", "assessor", "coverage", {"k": k, "silhouette": round(float(sil), 4), "n_units": n_units, "thin": thin, "coverage_score": coverage_score}, ev_ids)
    return out


def project_units(coverage: dict[str, Any], X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Assign new unit fingerprints (same feature order as coverage['scaler']['features']) to regimes.
    Returns (regime index per unit, distance to the nearest centroid)."""
    sc = coverage.get("scaler") or {}
    centers = np.asarray(coverage.get("centroids") or [], dtype="float64")
    keep = np.asarray(sc.get("keep") or [], dtype=bool)
    if X.size == 0 or centers.size == 0 or keep.size != X.shape[1]:
        return np.zeros(X.shape[0], dtype=int), np.full(X.shape[0], np.nan)
    center = np.asarray([c if c is not None else 0.0 for c in sc["center"]], dtype="float64")
    scale = np.asarray([s if s is not None else 1.0 for s in sc["scale"]], dtype="float64")
    Z = (X[:, keep] - center[keep]) / scale[keep]
    Z = np.clip(np.nan_to_num(Z, nan=0.0, posinf=0.0, neginf=0.0), -8, 8)
    d = np.linalg.norm(Z[:, None, :] - centers[None, :, :], axis=2)
    return d.argmin(axis=1), d.min(axis=1)
