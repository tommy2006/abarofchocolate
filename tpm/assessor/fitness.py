"""ML fitness via learning curves (decision 39): hold back a stratified share of units, fit on growing fractions
of the remaining units and score the held-back ones. The experiment function is ``tpm.detect.fit_score_subset``
when it exists (agent C); otherwise a self-contained PCA reconstruction estimator (``fallback_fit_score_subset``)
so the assessor always works. All experiments respect ``settings.assessor.experiment_time_budget_s``.

Metrics (normalised to "higher is better", 0..1 where possible):
    stability          1 - threshold coefficient of variation across internal folds (agent C) or between two
                       half-models (fallback)
    agreement          rank correlation of scores between detectors (agent C) or between two half-models (fallback)
    flagged_fraction   share of held-back rows flagged (informative, not "better")
    auroc              only when label-like columns exist (evaluation only)
The primary metric is stability (agreement when stability is unavailable).
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Optional

import numpy as np

from ..quality._common import ROW_COL, SignalInfo, load_catalog, quote_ident
from .coverage import coverage_signals, unit_definition

MAX_TRAIN_ROWS = 60_000
MAX_EVAL_ROWS = 30_000


def detect_fit_fn() -> Optional[Callable[..., dict[str, Any]]]:
    try:
        from .. import detect  # agent C

        fn = getattr(detect, "fit_score_subset", None)
        return fn if callable(fn) else None
    except Exception:
        return None


def normalize_metrics(res: dict[str, Any]) -> dict[str, Optional[float]]:
    """Map agent C's or the fallback's raw result onto the common metric names."""
    out: dict[str, Optional[float]] = {"stability": None, "agreement": None, "flagged_fraction": None, "auroc": None}
    if not isinstance(res, dict) or res.get("error"):
        return out
    if res.get("stability") is not None:
        out["stability"] = float(res["stability"])
    elif res.get("threshold_cv_mean") is not None:
        out["stability"] = float(max(0.0, 1.0 - min(1.0, float(res["threshold_cv_mean"]))))
    ag = res.get("agreement", res.get("detector_agreement"))
    if ag is not None and math.isfinite(float(ag)):
        out["agreement"] = float(max(0.0, min(1.0, float(ag))))
    if res.get("flagged_fraction") is not None:
        out["flagged_fraction"] = float(res["flagged_fraction"])
    if res.get("auroc") is not None:
        out["auroc"] = float(res["auroc"])
    return out


def primary_metric(m: dict[str, Optional[float]]) -> tuple[str, Optional[float]]:
    if m.get("stability") is not None:
        return "stability", m["stability"]
    if m.get("agreement") is not None:
        return "agreement", m["agreement"]
    return "stability", None


# ---------------------------------------------------------------- fallback estimator
def _load_rows(ws: Any, unit: dict[str, Any], units: list[str], sigs: list[SignalInfo], max_rows: int, label_col: Optional[str] = None, seed: int = 0) -> tuple[np.ndarray, Optional[np.ndarray], np.ndarray]:
    con = ws.duckdb()
    present = {r[0] for r in con.execute("DESCRIBE dataset").fetchall()}
    sigs = [s for s in sigs if s.column in present]
    if not sigs or not units:
        return np.zeros((0, len(sigs))), None, np.zeros(0)
    sel = ", ".join(quote_ident(s.column) for s in sigs)
    lab = f", CAST({quote_ident(label_col)} AS VARCHAR) AS __lab" if label_col and label_col in present else ""
    lst = ", ".join("'" + str(u).replace("'", "''") + "'" for u in units)
    q = f"SELECT * FROM (SELECT {quote_ident(ROW_COL)} AS __r, {sel}{lab} FROM dataset WHERE {unit['expr']} IN ({lst})) USING SAMPLE reservoir({int(max_rows)} ROWS) REPEATABLE ({int(seed) + 1})"
    cur = con.execute(q)
    tbl = cur.to_arrow_table() if hasattr(cur, "to_arrow_table") else cur.fetch_arrow_table()
    rows = tbl.column("__r").to_numpy()
    X = np.column_stack([tbl.column(s.column).to_numpy(zero_copy_only=False).astype("float64") for s in sigs]) if sigs else np.zeros((tbl.num_rows, 0))
    labels = np.array(tbl.column("__lab").to_pylist(), dtype=object) if lab else None
    return X, labels, rows


def _pca_fit(X: np.ndarray, max_components: int = 10) -> dict[str, Any]:
    med = np.nanmedian(X, axis=0)
    q75, q25 = np.nanpercentile(X, 75, axis=0), np.nanpercentile(X, 25, axis=0)
    sc = (q75 - q25) / 1.349
    sd = np.nanstd(X, axis=0)
    sc = np.where(sc > 0, sc, sd)
    sc = np.where(np.isfinite(sc) & (sc > 1e-12), sc, 1.0)
    Z = np.nan_to_num((X - med) / sc, nan=0.0, posinf=0.0, neginf=0.0)
    Z = np.clip(Z, -10, 10)
    p = Z.shape[1]
    if p == 0 or Z.shape[0] < 5:
        return {"med": med, "sc": sc, "comp": np.zeros((0, p)), "n_comp": 0}
    U, S, Vt = np.linalg.svd(Z - Z.mean(axis=0), full_matrices=False)
    var = S**2 / max(1, Z.shape[0] - 1)
    cum = np.cumsum(var) / max(var.sum(), 1e-12)
    n_comp = int(np.searchsorted(cum, 0.9) + 1)
    n_comp = max(1, min(n_comp, max_components, p - 1 if p > 1 else 1))
    return {"med": med, "sc": sc, "mean": Z.mean(axis=0), "comp": Vt[:n_comp], "n_comp": n_comp}


def _spe(model: dict[str, Any], X: np.ndarray) -> np.ndarray:
    Z = np.nan_to_num((X - model["med"]) / model["sc"], nan=0.0, posinf=0.0, neginf=0.0)
    Z = np.clip(Z, -10, 10)
    if model["n_comp"] == 0:
        return np.zeros(Z.shape[0])
    Zc = Z - model["mean"]
    proj = Zc @ model["comp"].T @ model["comp"]
    return ((Zc - proj) ** 2).sum(axis=1)


def _auroc(score: np.ndarray, pos: np.ndarray) -> Optional[float]:
    try:
        from sklearn.metrics import roc_auc_score

        if pos.sum() == 0 or pos.sum() == len(pos):
            return None
        return float(roc_auc_score(pos.astype(int), score))
    except Exception:
        return None


def fallback_fit_score_subset(ws: Any, settings: Any, train_units: list[str], eval_units: list[str], time_budget_s: float = 30.0, unit: Optional[dict[str, Any]] = None, exclude_signals: Optional[list[str]] = None, seed: int = 0) -> dict[str, Any]:
    """PCA reconstruction (SPE) estimator: fit on train units, score eval units; stability/agreement from two
    half-models fitted on disjoint halves of the train units. Aggregates only."""
    t0 = time.time()
    catalog = load_catalog(ws)
    ex = set(exclude_signals or [])
    sigs = [s for s in coverage_signals(catalog) if s.alias not in ex]
    unit = unit or unit_definition(ws)
    schema = None
    try:
        schema = ws.schema()
    except Exception:
        schema = None
    label_col = (getattr(schema, "label_columns", None) or [None])[0] if schema else None
    train_units, eval_units = [str(u) for u in train_units], [str(u) for u in eval_units]
    max_train = int(min(MAX_TRAIN_ROWS, max(3000, 1500 * time_budget_s)))
    Xtr, _, _ = _load_rows(ws, unit, train_units, sigs, max_train, seed=seed)
    Xev, lab, _ = _load_rows(ws, unit, eval_units, sigs, min(MAX_EVAL_ROWS, max_train // 2), label_col=label_col, seed=seed + 7)
    if Xtr.shape[0] < 10 or Xev.shape[0] < 5 or Xtr.shape[1] == 0:
        return {"error": "not enough rows for the fallback estimator", "estimator": "fallback_pca", "n_train_rows": int(Xtr.shape[0]), "n_eval_rows": int(Xev.shape[0]), "seconds": round(time.time() - t0, 2)}
    full = _pca_fit(Xtr)
    spe_tr = _spe(full, Xtr)
    thr = float(np.percentile(spe_tr, 99))
    spe_ev = _spe(full, Xev)
    flagged = float((spe_ev > thr).mean())
    # two half-models on disjoint unit halves (falls back to row halves when there is a single train unit)
    rng = np.random.default_rng(seed)
    perm = list(rng.permutation(train_units))
    ha, hb = perm[0::2], perm[1::2]
    if ha and hb:
        Xa, _, _ = _load_rows(ws, unit, ha, sigs, max_train // 2, seed=seed + 11)
        Xb, _, _ = _load_rows(ws, unit, hb, sigs, max_train // 2, seed=seed + 13)
    else:
        idx = rng.permutation(Xtr.shape[0])
        Xa, Xb = Xtr[idx[0::2]], Xtr[idx[1::2]]
    stability = agreement = None
    if Xa.shape[0] >= 10 and Xb.shape[0] >= 10:
        ma, mb = _pca_fit(Xa), _pca_fit(Xb)
        ta, tb = float(np.percentile(_spe(ma, Xa), 99)), float(np.percentile(_spe(mb, Xb), 99))
        cv = abs(ta - tb) / max((ta + tb) / 2.0, 1e-12)
        sa, sb = _spe(ma, Xev), _spe(mb, Xev)
        qa, qb = np.percentile(sa, [50, 90, 99]), np.percentile(sb, [50, 90, 99])
        rel = float(np.mean(np.abs(qa - qb) / np.maximum(np.maximum(qa, qb), 1e-12)))
        stability = float(max(0.0, min(1.0, 1.0 - 0.5 * min(1.0, cv) - 0.5 * min(1.0, rel))))
        try:
            from scipy.stats import spearmanr

            r = spearmanr(sa, sb).correlation
            agreement = float(max(0.0, min(1.0, r))) if r is not None and np.isfinite(r) else None
        except Exception:
            agreement = None
    auroc = None
    if lab is not None:
        vals, counts = np.unique(lab.astype(str), return_counts=True)
        majority = vals[np.argmax(counts)]
        auroc = _auroc(spe_ev, lab.astype(str) != majority)
    return {"estimator": "fallback_pca", "stability": None if stability is None else round(stability, 4), "agreement": None if agreement is None else round(agreement, 4), "flagged_fraction": round(flagged, 4), "auroc": None if auroc is None else round(auroc, 4), "n_train_rows": int(Xtr.shape[0]), "n_eval_rows": int(Xev.shape[0]), "n_train_units": len(train_units), "n_eval_units": len(eval_units), "n_components": int(full["n_comp"]), "n_signals": len(sigs), "seconds": round(time.time() - t0, 2)}


# ---------------------------------------------------------------- learning curve
def _stratified_split(units: list[str], regime_of: dict[str, str], holdback: float, rng: np.random.Generator) -> tuple[list[str], list[str]]:
    by: dict[str, list[str]] = {}
    for u in units:
        by.setdefault(regime_of.get(u, "R1"), []).append(u)
    train, ev = [], []
    for r, us in by.items():
        us = list(rng.permutation(us))
        n_ev = int(round(holdback * len(us)))
        if len(us) >= 2 and n_ev == 0:
            n_ev = 1
        if n_ev >= len(us):
            n_ev = len(us) - 1
        ev.extend(us[:n_ev])
        train.extend(us[n_ev:])
    if not ev and len(train) >= 2:
        ev.append(train.pop())
    return train, ev


def _stratified_take(pool: list[str], regime_of: dict[str, str], n: int, rng: np.random.Generator) -> list[str]:
    by: dict[str, list[str]] = {}
    for u in pool:
        by.setdefault(regime_of.get(u, "R1"), []).append(u)
    n = max(1, min(n, len(pool)))
    take: list[str] = []
    quotas = {r: n * len(us) / len(pool) for r, us in by.items()}
    for r, us in by.items():
        us = list(rng.permutation(us))
        k = int(math.floor(quotas[r]))
        take.extend(us[:k])
        by[r] = us[k:]
    rest = [u for us in by.values() for u in us]
    rest = list(rng.permutation(rest))
    take.extend(rest[: n - len(take)])
    return take[:n]


def run_experiment(ws: Any, settings: Any, train_units: list[str], eval_units: list[str], time_budget_s: float, unit: dict[str, Any], use_detect: bool = True, exclude_signals: Optional[list[str]] = None, seed: int = 0) -> tuple[dict[str, Any], str]:
    """One (train, eval) experiment: agent C's estimator for group units without exclusions, else the fallback."""
    fn = detect_fit_fn() if (use_detect and unit.get("kind") == "group" and not exclude_signals) else None
    if fn is not None:
        try:
            res = fn(ws, settings, list(train_units), list(eval_units), time_budget_s=float(time_budget_s))
            if isinstance(res, dict) and not res.get("error"):
                return res, "detect.fit_score_subset"
        except Exception as e:  # degrade to the fallback, but say so
            ws.log.record("system:assessor", "experiment_fallback", "assessor", "fit_score_subset", {"error": str(e)[:300]})
    res = fallback_fit_score_subset(ws, settings, train_units, eval_units, time_budget_s=time_budget_s, unit=unit, exclude_signals=exclude_signals, seed=seed)
    return res, "fallback_pca"


def learning_curve(ws: Any, settings: Any, coverage: Optional[dict[str, Any]] = None, time_budget_s: Optional[float] = None, use_detect: bool = True, exclude_signals: Optional[list[str]] = None, seed: int = 0, fractions: Optional[list[float]] = None) -> dict[str, Any]:
    t0 = time.time()
    budget = float(time_budget_s if time_budget_s is not None else settings.assessor.experiment_time_budget_s)
    unit = (coverage or {}).get("unit") or unit_definition(ws)
    regime_of: dict[str, str] = (coverage or {}).get("unit_regime") or {}
    if coverage and coverage.get("unit_regime"):
        units = list(coverage["unit_regime"].keys())
    else:
        from .coverage import unit_fingerprints

        units, _, _, _ = unit_fingerprints(ws, unit, [])
    fractions = sorted({float(f) for f in (fractions or settings.assessor.learning_curve_fractions) if 0 < float(f) <= 1.0}) or [1.0]
    if len(units) < 3:
        return {"available": False, "reason": f"only {len(units)} {unit['kind']}(s): a learning curve needs at least 3", "unit": unit, "curve": [], "seconds": round(time.time() - t0, 2)}
    rng = np.random.default_rng(seed)
    train_pool, eval_units = _stratified_split(units, regime_of, float(settings.assessor.holdback_fraction), rng)
    if len(train_pool) < 2 or not eval_units:
        return {"available": False, "reason": "not enough units after holding back an evaluation share", "unit": unit, "curve": [], "seconds": round(time.time() - t0, 2)}
    per_point = max(2.0, budget / (len(fractions) + 1))
    curve: list[dict[str, Any]] = []
    estimators: set[str] = set()
    deadline = t0 + budget

    def one_pass(pass_seed: int, pool: list[str]) -> list[dict[str, Any]]:
        pts = []
        prng = np.random.default_rng(pass_seed)
        for f in fractions:
            if time.time() > deadline and pts:
                break
            n_take = max(1, int(round(f * len(pool))))
            tr = _stratified_take(pool, regime_of, n_take, prng)
            res, est = run_experiment(ws, settings, tr, eval_units, per_point, unit, use_detect=use_detect, exclude_signals=exclude_signals, seed=pass_seed)
            estimators.add(est)
            m = normalize_metrics(res)
            name, val = primary_metric(m)
            pts.append({"fraction": f, "n_train_units": len(tr), "n_eval_units": len(eval_units), "metrics": m, "primary": val, "primary_name": name, "estimator": est, "raw": {k: v for k, v in res.items() if k in ("n_train_rows", "n_eval_rows", "n_fit_rows", "detectors", "n_components", "seconds", "error", "baseline_strategy")}})
        return pts

    pass1 = one_pass(seed, train_pool)
    pass2: list[dict[str, Any]] = []
    if pass1 and (time.time() - t0) < 0.4 * budget:
        rng2 = np.random.default_rng(seed + 101)
        pool2, eval2 = _stratified_split(units, regime_of, float(settings.assessor.holdback_fraction), rng2)
        if len(pool2) >= 2 and eval2:
            eval_backup = eval_units
            eval_units = eval2
            pass2 = one_pass(seed + 101, pool2)
            eval_units = eval_backup
    for i, p in enumerate(pass1):
        q = pass2[i] if i < len(pass2) and pass2[i]["fraction"] == p["fraction"] else None
        vals = [v for v in (p["primary"], q["primary"] if q else None) if v is not None]
        point = dict(p)
        point["primary"] = float(np.mean(vals)) if vals else None
        point["uncertainty"] = float(abs(vals[0] - vals[1]) / 2.0) if len(vals) == 2 else None
        point["repeats"] = len(vals)
        curve.append(point)
    xs = [c["fraction"] for c in curve if c["primary"] is not None]
    ys = [c["primary"] for c in curve if c["primary"] is not None]
    slope = slope_unc = None
    dim_frac = None
    seg_slopes: list[float] = []
    if len(xs) >= 2:
        seg_slopes = [(ys[i + 1] - ys[i]) / max(xs[i + 1] - xs[i], 1e-9) for i in range(len(xs) - 1)]
        slope = float(seg_slopes[-1])  # slope at the end of the curve = what one more unit of data buys now
        uncs = [c["uncertainty"] for c in curve[-2:] if c["uncertainty"] is not None]
        slope_unc = float(math.sqrt(sum(u * u for u in uncs)) / max(xs[-1] - xs[-2], 1e-9)) if uncs else (float(np.std(seg_slopes)) if len(seg_slopes) >= 2 else None)
        for i, s in enumerate(seg_slopes):
            if all(abs(t) < 0.05 for t in seg_slopes[i:]):
                dim_frac = xs[i]
                break
    est_gain = None
    would_help: Optional[bool] = None
    if slope is not None:
        est_gain = float(max(-0.2, min(0.5, slope * 0.5)))  # +50 % data, linear extrapolation of the last segment
        unc = slope_unc * 0.5 if slope_unc is not None else 0.0
        if dim_frac is not None and dim_frac <= xs[-2]:
            would_help = False  # the curve is flat already before the full dataset
        elif slope_unc is not None and abs(est_gain) <= unc:
            would_help = None  # the uncertainty dominates the effect: say "unclear", never guess
        elif est_gain > 0.01 and (ys[-1] is None or ys[-1] < 0.98):
            would_help = True
        elif est_gain < 0.003:
            would_help = False
        else:
            would_help = None
    fitness_score = ys[-1] if ys else None
    name = curve[-1]["primary_name"] if curve else "stability"
    seconds = round(time.time() - t0, 2)
    desc = f"Learning curve ({name}, estimator {'/'.join(sorted(estimators)) or 'none'}): " + ", ".join(f"{x:.0%} of {len(train_pool)} {unit['kind']}s -> {y:.3f}" for x, y in zip(xs, ys)) if xs else "Learning curve could not be computed"
    if slope is not None:
        desc += f"; slope at the end {slope:+.3f} per full dataset" + (f" (+/-{slope_unc:.3f})" if slope_unc is not None else "") + (f"; diminishing returns from {dim_frac:.0%}" if dim_frac is not None else "; no plateau reached")
    ev = ws.evidence.add("learning_curve", desc, values={"fractions": xs, "primary": ys, "primary_name": name, "slope": slope, "slope_uncertainty": slope_unc, "diminishing_returns_fraction": dim_frac, "estimated_gain_more_data": est_gain, "n_eval_units": len(eval_units), "n_train_pool": len(train_pool), "estimators": sorted(estimators), "seconds": seconds}, computed_by="assessor.fitness.learning_curve", n_samples=len(units))
    out = {"available": bool(curve), "unit": unit, "primary_metric": name, "curve": curve, "slope": slope, "slope_uncertainty": slope_unc, "diminishing_returns_fraction": dim_frac, "estimated_gain_more_data": est_gain, "would_help_more_data": would_help, "fitness_score": fitness_score, "estimators": sorted(estimators), "n_train_pool": len(train_pool), "n_eval_units": len(eval_units), "eval_units": eval_units[:50], "seconds": seconds, "statement": desc, "evidence_ids": [ev.id]}
    ws.log.record("system:assessor", "learning_curve", "assessor", "fitness", {k: out[k] for k in ("primary_metric", "slope", "diminishing_returns_fraction", "estimated_gain_more_data", "would_help_more_data", "fitness_score", "estimators", "seconds")}, [ev.id])
    return out


def compare_with_without(ws: Any, settings: Any, exclude_signals: list[str], coverage: Optional[dict[str, Any]], time_budget_s: float, seed: int = 0) -> dict[str, Any]:
    """Bounded experiment for drop_signal: same units, full training pool, with vs without the signals (fallback estimator)."""
    unit = (coverage or {}).get("unit") or unit_definition(ws)
    regime_of = (coverage or {}).get("unit_regime") or {}
    units = list(regime_of.keys()) if regime_of else []
    if not units:
        from .coverage import unit_fingerprints

        units, _, _, _ = unit_fingerprints(ws, unit, [])
    if len(units) < 3:
        return {"available": False, "reason": "too few units"}
    rng = np.random.default_rng(seed)
    train, ev = _stratified_split(units, regime_of, float(settings.assessor.holdback_fraction), rng)
    half = max(2.0, time_budget_s / 2.0)
    base = fallback_fit_score_subset(ws, settings, train, ev, time_budget_s=half, unit=unit, seed=seed)
    alt = fallback_fit_score_subset(ws, settings, train, ev, time_budget_s=half, unit=unit, exclude_signals=exclude_signals, seed=seed)
    mb, ma = normalize_metrics(base), normalize_metrics(alt)
    name, vb = primary_metric(mb)
    _, va = primary_metric(ma)
    delta = (va - vb) if (va is not None and vb is not None) else None
    return {"available": delta is not None, "primary_metric": name, "before": vb, "after": va, "delta": None if delta is None else round(delta, 4), "before_metrics": mb, "after_metrics": ma, "estimator": "fallback_pca", "n_train_units": len(train), "n_eval_units": len(ev)}
