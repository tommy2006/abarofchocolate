"""Relations between signals on the bounded profile subsample: Pearson + Spearman matrices, lagged
cross-correlation for the strongest pairs (within groups, pre-whitened by first differences with a
level-based fallback), hierarchical clusters on 1-|r|, redundancy (|r|>0.995 or near-exact linear
combination) and a lead/lag graph. Writes evidence for strong pairs, lags and clusters.

    rel = compute_relations(ws, settings, aliases, sample, fingerprints, max_lag=30, top_pairs=80)
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

import numpy as np

ProgressFn = Optional[Callable[[float, str], None]]
STAGE = "profile"


def _prog(progress: ProgressFn, f: float, m: str) -> None:
    if progress:
        try:
            progress(float(f), m)
        except Exception:
            pass


def _pooled(sample: dict[str, Any]) -> np.ndarray:
    arrays = [a for a in sample["arrays"] if a.shape[0] > 0]
    return np.vstack(arrays) if arrays else np.empty((0, len(sample["columns"])))


def _corr_matrix(X: np.ndarray) -> np.ndarray:
    """Pearson correlation with NaNs replaced by column means; constant columns -> NaN row/col."""
    n, k = X.shape
    if n < 3:
        return np.full((k, k), np.nan)
    mu = np.nanmean(X, axis=0)
    Xf = np.where(np.isfinite(X), X, mu)
    Xf = np.where(np.isfinite(Xf), Xf, 0.0)
    sd = Xf.std(axis=0)
    ok = sd > 0
    Z = np.zeros_like(Xf)
    Z[:, ok] = (Xf[:, ok] - Xf[:, ok].mean(axis=0)) / sd[ok]
    C = (Z.T @ Z) / n
    C[~ok, :] = np.nan
    C[:, ~ok] = np.nan
    np.fill_diagonal(C, np.where(ok, 1.0, np.nan))
    return np.clip(C, -1.0, 1.0)


def _rank(X: np.ndarray) -> np.ndarray:
    from scipy.stats import rankdata

    R = np.empty_like(X)
    for j in range(X.shape[1]):
        col = X[:, j]
        ok = np.isfinite(col)
        r = np.full(len(col), np.nan)
        if ok.sum() > 2:
            r[ok] = rankdata(col[ok])
        R[:, j] = r
    return R


def _xcorr_curve(x: np.ndarray, y: np.ndarray, max_lag: int) -> np.ndarray:
    """r[k] for k in -max_lag..max_lag where k>0 means x leads y (y(t) ~ x(t-k)). NaN-safe, standardized."""
    from scipy.signal import correlate

    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 3 * max_lag + 10:
        return np.full(2 * max_lag + 1, np.nan)
    xs = np.where(ok, x, np.nan)
    ys = np.where(ok, y, np.nan)
    xs = (xs - np.nanmean(xs)) / (np.nanstd(xs) or np.nan)
    ys = (ys - np.nanmean(ys)) / (np.nanstd(ys) or np.nan)
    if not (np.isfinite(xs).any() and np.isfinite(ys).any()):
        return np.full(2 * max_lag + 1, np.nan)
    xs = np.where(np.isfinite(xs), xs, 0.0)
    ys = np.where(np.isfinite(ys), ys, 0.0)
    n = len(xs)
    full = correlate(ys, xs, mode="full", method="fft")  # index n-1+k = sum_t y[t] * x[t-k]
    mid = n - 1
    ks = np.arange(-max_lag, max_lag + 1)
    vals = full[mid + ks]
    counts = n - np.abs(ks)
    return vals / np.maximum(counts, 1)


def _detrend(x: np.ndarray) -> np.ndarray:
    ok = np.isfinite(x)
    if ok.sum() < 3:
        return x
    t = np.arange(len(x), dtype=np.float64)
    coef = np.polyfit(t[ok], x[ok], 1)
    return x - np.polyval(coef, t)


def _update_mask(v: np.ndarray) -> np.ndarray:
    """True at samples where a held signal takes a new value (its update instants)."""
    d = np.diff(v)
    return np.concatenate([[True], (d != 0) & np.isfinite(d)])


def _masked_xcorr(sample: dict[str, Any], i: int, j: int, max_lag: int, held_i: bool, held_j: bool) -> tuple[np.ndarray, float]:
    """Cross-correlation of detrended levels evaluated only where the held signal(s) were just updated,
    so a sample-and-hold channel reveals its true lag instead of a plateau over its hold period.
    Masked Pearson at every lag from six FFT lag-sums (counts, sums, cross- and squared sums)."""
    from scipy.signal import correlate

    ks = np.arange(-max_lag, max_lag + 1)
    acc = np.zeros(len(ks))
    cnt_tot = np.zeros(len(ks))
    for arr in sample["arrays"]:
        n = arr.shape[0]
        if n < 3 * max_lag + 10:
            continue
        x_raw, y_raw = arr[:, i], arr[:, j]
        x, y = _detrend(x_raw), _detrend(y_raw)
        fin = np.isfinite(x) & np.isfinite(y)
        mx = ((_update_mask(x_raw) if held_i else np.ones(n, dtype=bool)) & fin).astype(np.float64)
        my = ((_update_mask(y_raw) if held_j else np.ones(n, dtype=bool)) & fin).astype(np.float64)
        if mx.sum() < 20 or my.sum() < 20:
            continue
        x = np.where(fin, x, 0.0)
        y = np.where(fin, y, 0.0)
        sx, sy = x[fin].std(), y[fin].std()
        if sx == 0 or sy == 0:
            continue
        x = (x - x[fin].mean()) / sx
        y = (y - y[fin].mean()) / sy
        mid = n - 1

        def lagsum(a: np.ndarray, b: np.ndarray) -> np.ndarray:  # sum_t b[t] * a[t-k] for k in ks
            return correlate(b, a, mode="full", method="fft")[mid + ks]

        cnt = lagsum(mx, my)
        Sx, Sy = lagsum(x * mx, my), lagsum(mx, y * my)
        Sxy = lagsum(x * mx, y * my)
        Sxx, Syy = lagsum(x * x * mx, my), lagsum(mx, y * y * my)
        with np.errstate(invalid="ignore", divide="ignore"):
            vx = Sxx - Sx * Sx / cnt
            vy = Syy - Sy * Sy / cnt
            r = (Sxy - Sx * Sy / cnt) / np.sqrt(vx * vy)
        ok = (cnt >= 20) & np.isfinite(r) & (vx > 1e-9) & (vy > 1e-9)
        acc[ok] += np.clip(r[ok], -1, 1) * cnt[ok]
        cnt_tot[ok] += cnt[ok]
    curve = np.where(cnt_tot > 0, acc / np.maximum(cnt_tot, 1), np.nan)
    return curve, float(cnt_tot.max()) if len(cnt_tot) else 0.0


def lagged_xcorr(sample: dict[str, Any], i: int, j: int, max_lag: int, held_i: bool = False, held_j: bool = False) -> dict[str, Any]:
    """Aggregate (row-weighted) cross-correlation curves over chunks; choose the best lag. Pre-whitened
    (first differences) for continuous pairs, update-instant levels when a sample-and-hold signal is involved."""
    ks = np.arange(-max_lag, max_lag + 1)
    if held_i or held_j:
        curve, n_eff = _masked_xcorr(sample, i, j, max_lag, held_i, held_j)
        if np.isfinite(curve).sum() >= 3:
            a = np.abs(curve)
            best = int(np.nanargmax(a))
            return {"lag": int(ks[best]), "r_at_lag": float(curve[best]), "r_lag0": float(curve[max_lag]) if np.isfinite(curve[max_lag]) else None, "r_level_at_lag": float(curve[best]), "r_diff_at_lag": None, "method": "update_instants", "n": int(n_eff), "peak_prominence": float(a[best] / (np.nanmedian(a) + 1e-9))}
    acc_d = np.zeros(len(ks))
    acc_l = np.zeros(len(ks))
    w_d = w_l = 0.0
    for arr in sample["arrays"]:
        x, y = arr[:, i], arr[:, j]
        if len(x) < 3 * max_lag + 10:
            continue
        cl = _xcorr_curve(_detrend(x), _detrend(y), max_lag)
        if np.isfinite(cl).all():
            acc_l += cl * len(x)
            w_l += len(x)
        cd = _xcorr_curve(np.diff(x), np.diff(y), max_lag)
        if np.isfinite(cd).all():
            acc_d += cd * len(x)
            w_d += len(x)
    if w_l == 0 and w_d == 0:
        return {"lag": 0, "r_at_lag": None, "r_lag0": None, "method": "none", "n": 0}
    cl = acc_l / w_l if w_l else np.full(len(ks), np.nan)
    cd = acc_d / w_d if w_d else np.full(len(ks), np.nan)
    use_diff = w_d > 0 and np.nanmax(np.abs(cd)) >= 0.1
    curve = cd if use_diff else cl
    a = np.abs(curve)
    mx = float(np.nanmax(a))
    cand = np.flatnonzero(a >= 0.9 * mx)  # plateau: prefer the smallest |lag| within 10 % of the peak
    best = int(cand[np.argmin(np.abs(ks[cand]))]) if len(cand) else int(np.nanargmax(a))
    lag = int(ks[best])
    return {"lag": lag, "r_at_lag": float(curve[best]), "r_lag0": float(cl[max_lag]) if w_l else None, "r_level_at_lag": float(cl[best]) if w_l else None, "r_diff_at_lag": float(cd[best]) if w_d else None, "method": "diff" if use_diff else "level", "n": int(w_l or w_d), "peak_prominence": float(mx / (np.nanmedian(a) + 1e-9))}


def _clusters(C: np.ndarray, aliases: list[str], threshold: float = 0.5) -> dict[str, list[str]]:
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import squareform

    ok = np.array([np.isfinite(C[i, i]) for i in range(len(aliases))])
    idx = np.flatnonzero(ok)
    if len(idx) < 2:
        return {}
    sub = np.abs(C[np.ix_(idx, idx)])
    sub = np.where(np.isfinite(sub), sub, 0.0)
    D = 1.0 - sub
    np.fill_diagonal(D, 0.0)
    D = (D + D.T) / 2
    Z = linkage(squareform(D, checks=False), method="average")
    labels = fcluster(Z, t=threshold, criterion="distance")
    groups: dict[int, list[str]] = {}
    for lab, i in zip(labels, idx):
        groups.setdefault(int(lab), []).append(aliases[i])
    multi = sorted([m for m in groups.values() if len(m) >= 2], key=lambda m: (-len(m), m[0]))
    width = max(2, len(str(len(multi))))
    return {f"C{n + 1:0{width}d}": m for n, m in enumerate(multi)}


def _ols_r2(y: np.ndarray, X: np.ndarray) -> tuple[float, np.ndarray]:
    ok = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
    if ok.sum() < X.shape[1] + 5:
        return 0.0, np.zeros(X.shape[1])
    A = np.column_stack([X[ok], np.ones(ok.sum())])
    coef, *_ = np.linalg.lstsq(A, y[ok], rcond=None)
    resid = y[ok] - A @ coef
    ss_tot = float(np.sum((y[ok] - y[ok].mean()) ** 2))
    if ss_tot <= 0:
        return 0.0, coef[:-1]
    return float(1.0 - np.sum(resid**2) / ss_tot), coef[:-1]


def _robust_clip(sample: dict[str, Any], n_mad: float = 6.0) -> dict[str, Any]:
    """Winsorize every column at median +- n_mad * 1.4826 * MAD (pooled) so single spikes cannot wreck
    Pearson / cross-correlation. Returns a new sample dict (arrays copied)."""
    X = _pooled(sample)
    if X.shape[0] == 0:
        return sample
    med = np.nanmedian(X, axis=0)
    mad = np.nanmedian(np.abs(X - med), axis=0) * 1.4826
    q1, q3 = np.nanpercentile(X, 25, axis=0), np.nanpercentile(X, 75, axis=0)
    scale = np.where(mad > 0, mad, (q3 - q1) / 1.349)
    lo = np.where(scale > 0, med - n_mad * scale, -np.inf)
    hi = np.where(scale > 0, med + n_mad * scale, np.inf)
    arrays = [np.clip(a, lo, hi) for a in sample["arrays"]]
    n_clipped = int(sum(((a < lo) | (a > hi)).sum() for a in sample["arrays"]))
    out = dict(sample)
    out["arrays"] = arrays
    out["description"] = {**sample["description"], "winsorized": f"median +- {n_mad:g} MAD", "n_values_clipped": n_clipped}
    return out


def _chunk_r2(sample: dict[str, Any], j: int, regs: list[int]) -> tuple[float, float, np.ndarray]:
    """(R2 at the 70th percentile over chunks, median R2, median coefficients). A derived signal is
    near-exact in most groups; a few groups damaged by faults or data-quality issues must not hide it."""
    r2s, coefs = [], []
    for arr in sample["arrays"]:
        if arr.shape[0] < len(regs) + 20:
            continue
        r2, coef = _ols_r2(arr[:, j], arr[:, regs])
        r2s.append(r2)
        coefs.append(coef)
    if not r2s:
        X = _pooled(sample)
        r2, coef = _ols_r2(X[:, j], X[:, regs])
        return r2, r2, coef
    return float(np.percentile(r2s, 70)), float(np.median(r2s)), np.median(np.vstack(coefs), axis=0)


def _exact_enough(q70: float, med: float, threshold: float) -> bool:
    """Near-exact in at least ~30 % of the groups and still strongly related in the median group
    (groups damaged by faults or data-quality issues must not hide a derived signal)."""
    return q70 >= threshold and med >= 0.95


def _minimal_regressors(sample: dict[str, Any], j: int, regs: list[int], threshold: float) -> list[int]:
    """Backward elimination: drop regressors while the fit stays near-exact."""
    regs = list(regs)
    changed = True
    while changed and len(regs) > 1:
        changed = False
        for p in list(regs):
            trial = [q for q in regs if q != p]
            q70, med, _ = _chunk_r2(sample, j, trial)
            if _exact_enough(q70, med, threshold):
                regs = trial
                changed = True
                break
    return regs


def _redundancy(sample: dict[str, Any], C: np.ndarray, aliases: list[str], r2_threshold: float = 0.9995, dup_threshold: float = 0.995, max_regressors: int = 4) -> list[dict[str, Any]]:
    X = _pooled(sample)
    k = len(aliases)
    var = np.nanvar(X, axis=0)
    mu = np.nanmean(X, axis=0)
    Xf = np.where(np.isfinite(X), X, mu)
    Xf = np.where(np.isfinite(Xf), Xf, 0.0)
    cands: list[dict[str, Any]] = []
    for j in range(k):
        if not np.isfinite(C[j, j]) or var[j] == 0:
            continue
        r = np.abs(np.where(np.isfinite(C[j]), C[j], 0.0)).copy()
        r[j] = 0.0
        pool = [int(p) for p in np.argsort(-r) if np.isfinite(C[p, p]) and p != j and var[p] > 0][: (k if k <= 60 else 30)]
        if not pool or r[pool[0]] < 0.2:
            continue
        if not pool:
            continue
        # stepwise forward selection on the residual (finds sums such as y = a + b even when corr(y, b) is modest)
        chosen: list[int] = []
        resid = Xf[:, j] - Xf[:, j].mean()
        best = None
        for m in range(1, max_regressors + 1):
            rest = [p for p in pool if p not in chosen]
            if not rest:
                break
            rs = resid.std()
            if rs == 0:
                break
            scores = [abs(np.corrcoef(resid, Xf[:, p])[0, 1]) if Xf[:, p].std() > 0 else 0.0 for p in rest]
            pick = rest[int(np.argmax(scores))]
            chosen.append(pick)
            q70, med, coef = _chunk_r2(sample, j, chosen)
            A = np.column_stack([Xf[:, chosen], np.ones(len(Xf))])
            cpool, *_ = np.linalg.lstsq(A, Xf[:, j], rcond=None)
            resid = Xf[:, j] - A @ cpool
            if _exact_enough(q70, med, r2_threshold):
                minimal = _minimal_regressors(sample, j, chosen, r2_threshold)
                q70, med, coef = _chunk_r2(sample, j, minimal)
                signs = np.sign(coef[np.abs(coef) > 1e-12])
                best = {"signal": aliases[j], "partners": [aliases[p] for p in minimal], "r2": round(q70, 6), "r2_median": round(med, 6), "coefficients": [round(float(c), 6) for c in coef], "n_regressors": len(minimal), "same_sign": bool(len(signs) <= 1 or np.all(signs == signs[0])), "max_abs_r": round(float(r[pool[0]]), 4)}
                break
        if best is None and r[pool[0]] >= dup_threshold:
            best = {"signal": aliases[j], "partners": [aliases[pool[0]]], "r2": round(float(r[pool[0]] ** 2), 6), "coefficients": [], "n_regressors": 1, "same_sign": True, "max_abs_r": round(float(r[pool[0]]), 4)}
        if best:
            best["variance"] = float(var[j])
            cands.append(best)
    # dependency sets (union-find) -> one "derived" member per set. A total/average is reproduced with
    # same-sign coefficients (sum = a + b) while its parts need mixed signs (a = sum - b); ties -> largest variance.
    parent: dict[str, str] = {}

    def find(a: str) -> str:
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for c in cands:
        for p in c["partners"]:
            union(c["signal"], p)
    sets: dict[str, list[dict[str, Any]]] = {}
    for c in cands:
        sets.setdefault(find(c["signal"]), []).append(c)
    out: list[dict[str, Any]] = []
    for members in sets.values():
        members.sort(key=lambda c: (0 if c.get("same_sign") else 1, -c["variance"], c["signal"]))
        derived = members[0]
        for c in members:
            c["derived"] = c is derived
            others = {m["signal"] for m in members if m is not c} | set(c["partners"])
            c["alternatives"] = sorted(others - {c["signal"]})
            out.append(c)
    return out


def compute_relations(ws, settings, aliases: list[str], sample: dict[str, Any], fingerprints: dict[str, dict[str, Any]], max_lag: int = 30, top_pairs: int = 80, min_abs_r: float = 0.3, progress: ProgressFn = None) -> dict[str, Any]:
    """aliases[i] is the alias of sample['columns'][i]. Returns the relations.json structure."""
    t0 = time.time()
    sample = _robust_clip(sample)  # winsorized copy: spikes must not dominate Pearson / xcorr / OLS
    X = _pooled(sample)
    n, k = X.shape
    const = np.array([(fingerprints.get(c, {}).get("n_unique", 2) or 2) <= 1 for c in sample["columns"]])
    C = _corr_matrix(X) if n >= 3 else np.full((k, k), np.nan)
    C[const, :] = np.nan
    C[:, const] = np.nan
    _prog(progress, 0.2, "spearman")
    S = _corr_matrix(_rank(X)) if n >= 3 and k <= 2000 else np.full((k, k), np.nan)
    S[const, :] = np.nan
    S[:, const] = np.nan
    ev = ws.evidence
    evidence_ids: list[str] = []
    # ---- top pairs
    pairs_idx = []
    iu = np.triu_indices(k, 1)
    absr = np.abs(np.where(np.isfinite(C[iu]), C[iu], 0.0))
    order = np.argsort(-absr)
    for o in order[: top_pairs]:
        if absr[o] < min_abs_r:
            break
        pairs_idx.append((int(iu[0][o]), int(iu[1][o])))
    pairs: list[dict[str, Any]] = []
    held = [bool((fingerprints.get(c, {}).get("stuck_fraction") or 0) >= 0.4 and (fingerprints.get(c, {}).get("hold_period") or 1) >= 2) for c in sample["columns"]]
    for pi, (i, j) in enumerate(pairs_idx):
        lx = lagged_xcorr(sample, i, j, max_lag, held_i=held[i], held_j=held[j])
        a, b = aliases[i], aliases[j]
        lag = lx["lag"]
        if lag < 0:  # normalise: `a` leads `b` by lag >= 0
            a, b, lag = b, a, -lag
            lx["lag"] = lag
        rec = {"a": a, "b": b, "r": round(float(C[i, j]), 4), "spearman": round(float(S[i, j]), 4) if np.isfinite(S[i, j]) else None, "lag": int(lag), "r_at_lag": round(lx["r_at_lag"], 4) if lx["r_at_lag"] is not None else None, "sign": int(np.sign(lx["r_at_lag"] or C[i, j])), "method": lx["method"], "n": int(lx["n"]), "peak_prominence": round(lx.get("peak_prominence", 0.0), 2)}
        pairs.append(rec)
        e = ev.add("correlation", f"{a} and {b} correlate r={rec['r']:+.2f} (Spearman {rec['spearman'] if rec['spearman'] is not None else 'n/a'}) at lag 0", signals=[a, b], values={"r": rec["r"], "spearman": rec["spearman"], "n": n}, computed_by="profile.relations.corr", n_samples=n)
        evidence_ids.append(e.id)
        rec["evidence_ids"] = [e.id]
        if lag > 0 and lx["r_at_lag"] is not None:
            e2 = ev.add("lag", f"{a} leads {b} by {lag} samples (cross-correlation {lx['r_at_lag']:+.2f} on {lx['method']}s, prominence {rec['peak_prominence']:.1f})", signals=[a, b], values={"lag": lag, "r_at_lag": rec["r_at_lag"], "method": lx["method"], "r_level_at_lag": lx.get("r_level_at_lag"), "r_diff_at_lag": lx.get("r_diff_at_lag")}, computed_by="profile.relations.lagged_xcorr", n_samples=int(lx["n"]))
            evidence_ids.append(e2.id)
            rec["evidence_ids"].append(e2.id)
        if pi % 10 == 0:
            _prog(progress, 0.2 + 0.5 * pi / max(1, len(pairs_idx)), f"lagged cross-correlation {pi + 1}/{len(pairs_idx)}")
    # ---- clusters
    clusters = _clusters(C, aliases)
    for cid, members in clusters.items():
        e = ev.add("cluster", f"cluster {cid}: {', '.join(members)} move together (average |r| >= 0.5)", signals=members, values={"members": members}, computed_by="profile.relations.clusters", n_samples=n)
        evidence_ids.append(e.id)
    # ---- redundancy
    red = _redundancy(sample, C, aliases) if n >= 20 else []
    for r in red:
        e = ev.add("redundancy", f"{r['signal']} is reproduced by {' + '.join(r['partners'])} (R2={r['r2']:.5f}, {r['n_regressors']} regressor(s))" + (" -> likely derived" if r["derived"] else " (reference of a redundant set)"), signals=[r["signal"]] + r["partners"], values={k2: v for k2, v in r.items() if k2 not in ("alternatives",)}, computed_by="profile.relations.redundancy", n_samples=n)
        evidence_ids.append(e.id)
        r["evidence_id"] = e.id
    # ---- leaders
    leaders: dict[str, list[dict[str, Any]]] = {}
    for p in pairs:
        if p["lag"] > 0:
            leaders.setdefault(p["b"], []).append({"leader": p["a"], "lag": p["lag"], "r": p["r_at_lag"]})
    rel = {
        "signals": aliases,
        "corr": {"signals": aliases, "matrix": [[None if not np.isfinite(v) else round(float(v), 4) for v in row] for row in C]},
        "spearman": {"signals": aliases, "matrix": [[None if not np.isfinite(v) else round(float(v), 4) for v in row] for row in S]},
        "pairs": pairs,
        "clusters": clusters,
        "redundancy": red,
        "leaders": leaders,
        "max_lag": max_lag,
        "preprocessing": sample["description"].get("winsorized"),
        "sampling": sample["description"],
        "n_samples": n,
        "evidence_ids": evidence_ids,
        "seconds": round(time.time() - t0, 2),
    }
    ws.log.record("system:profile", "relations", "dataset", ws.run_id, {"n_pairs": len(pairs), "n_clusters": len(clusters), "n_redundant": sum(1 for r in red if r["derived"]), "n_samples": n, "seconds": rel["seconds"]}, evidence_ids=evidence_ids[:50])
    return rel
