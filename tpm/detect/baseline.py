"""Baseline ("normal") regime estimation without labels.

No normal data is given: the whole dataset is the object of analysis. Several candidate selections of
"normal" rows are computed on a bounded sample and scored for self-consistency:

  (a) consensus_of_modes   rows where most signals sit near their density mode
  (b) pre_changepoint      per group, the segment before the first significant change (ruptures)
  (c) densest_windows      windows whose [mean, std] fingerprint falls in the tightest cluster
  (d) robust_covariance    rows inside the robust (MinCovDet) Mahalanobis ellipsoid

Scores: dispersion tightness, cross-group agreement (a true baseline recurs across groups), size
plausibility and consensus with the other candidates. The winner becomes an Inference with evidence and
explicit assumptions; low confidence -> status "assumed". An operator-set reference period overrides all.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from ._common import Budget, robust_scale, runs_of_true, segments
from .features import _rolling_mean_std


@dataclass
class Sample:
    rows: np.ndarray  # (n,) int64 dataset row ids, ascending
    groups: np.ndarray  # (n,) object (str)
    X: np.ndarray  # (n, p) float32 raw values
    aliases: list[str]
    blocks: list[tuple[int, int, str]] = field(default_factory=list)
    description: dict[str, Any] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.rows)

    def group_index(self) -> dict[str, np.ndarray]:
        out: dict[str, list[int]] = {}
        for s, e in segments(self.groups):
            out.setdefault(str(self.groups[s]), []).append((s, e))
        return {g: np.concatenate([np.arange(s, e) for s, e in segs]) for g, segs in out.items()}


@dataclass
class BaselineResult:
    strategy: str
    mask: np.ndarray  # (n,) bool on the sample
    ranges: dict[str, list[list[int]]]  # group -> [[row_start, row_end), ...]
    confidence: float
    status: str
    candidates: list[dict[str, Any]]
    assumptions: list[str]
    evidence_ids: list[str]
    inference_id: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        n_sel = int(self.mask.sum())
        return {"strategy": self.strategy, "confidence": round(float(self.confidence), 3), "status": self.status, "candidates": self.candidates, "selected_fraction_of_sample": round(n_sel / max(1, len(self.mask)), 4), "n_selected_sample_rows": n_sel, "ranges": self.ranges, "n_groups_with_baseline": int(sum(1 for v in self.ranges.values() if v)), "assumptions": self.assumptions, "evidence_ids": self.evidence_ids, "inference_id": self.inference_id, "notes": self.notes}


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------


def smooth_mask(mask: np.ndarray, groups: np.ndarray, window: int, min_run: Optional[int] = None) -> np.ndarray:
    """Majority filter over `window` rows inside each group segment, then drop runs shorter than min_run."""
    out = np.zeros_like(mask, dtype=bool)
    w = max(3, int(window))
    min_run = min_run or w
    for s, e in segments(groups):
        m = mask[s:e].astype(np.float32)[:, None]
        rm, _ = _rolling_mean_std(m, w)
        # centre the causal rolling mean by shifting half a window
        shift = w // 2
        rm = rm[:, 0]
        centred = np.concatenate([rm[shift:], np.full(shift, rm[-1])]) if len(rm) > shift else rm
        sel = centred >= 0.5
        for a, b in runs_of_true(sel):
            if b - a >= min_run:
                out[s + a : s + b] = True
    return out


def mask_to_ranges(mask: np.ndarray, rows: np.ndarray, groups: np.ndarray) -> dict[str, list[list[int]]]:
    """Consecutive selected sample rows (consecutive row ids, same group) -> row ranges per group."""
    out: dict[str, list[list[int]]] = {}
    n = len(mask)
    i = 0
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and mask[j + 1] and groups[j + 1] == groups[i] and rows[j + 1] == rows[j] + 1:
            j += 1
        out.setdefault(str(groups[i]), []).append([int(rows[i]), int(rows[j]) + 1])
        i = j + 1
    return out


def ranges_to_mask(ranges: dict[str, list[list[int]]], rows: np.ndarray, groups: np.ndarray) -> np.ndarray:
    mask = np.zeros(len(rows), dtype=bool)
    for g, rr in ranges.items():
        gm = groups == g
        if not gm.any():
            continue
        idx = np.flatnonzero(gm)
        r = rows[idx]
        for s, e in rr:
            mask[idx[(r >= s) & (r < e)]] = True
    return mask


def _otsu(values: np.ndarray, bins: int = 128) -> float:
    hist, edges = np.histogram(values, bins=bins)
    hist = hist.astype(np.float64)
    total = hist.sum()
    if total <= 0:
        return float(np.median(values))
    centers = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)
    w1 = total - w0
    m0 = np.cumsum(hist * centers) / np.maximum(w0, 1e-12)
    m1 = (np.sum(hist * centers) - np.cumsum(hist * centers)) / np.maximum(w1, 1e-12)
    between = w0 * w1 * (m0 - m1) ** 2
    between[(w0 == 0) | (w1 == 0)] = -1
    k = int(np.argmax(between))
    return float(edges[k + 1])


def _zscore(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    med, scale = robust_scale(X.astype(np.float64), axis=0)
    z = (X.astype(np.float32) - med.astype(np.float32)) / scale.astype(np.float32)
    z = np.nan_to_num(z, nan=0.0, posinf=10.0, neginf=-10.0)
    return np.clip(z, -10, 10), med, scale


# ----------------------------------------------------------------------------------------------
# candidates
# ----------------------------------------------------------------------------------------------


def global_spe(z: np.ndarray, informative: np.ndarray, var_explained: float = 0.9, max_components: int = 40) -> tuple[np.ndarray, dict[str, Any]]:
    """Standardized PCA residual (SPE) of every row against the dominant correlation structure of the whole
    sample. Slow drivers move signals *together* (inside the principal subspace); faults and sensor
    problems break the structure and raise the SPE. Returns (spe_z, info)."""
    cols = np.flatnonzero(informative)
    n = z.shape[0]
    if len(cols) < 2 or n < 20:
        return np.zeros(n, dtype=np.float32), {"note": "not enough signals for SPE"}
    Zc = z[:, cols].astype(np.float64)
    mean = Zc.mean(axis=0)
    Zc = Zc - mean
    cov = (Zc.T @ Zc) / max(1, n - 1) + np.eye(len(cols)) * 1e-4
    w, V = np.linalg.eigh(cov)
    order = np.argsort(w)[::-1]
    w, V = w[order], V[:, order]
    cum = np.cumsum(w) / w.sum()
    k = int(np.searchsorted(cum, var_explained) + 1)
    k = max(1, min(k, max_components, len(cols) - 1))
    P = V[:, :k]
    R = Zc - (Zc @ P) @ P.T
    spe = (R * R).sum(axis=1)
    med = float(np.median(spe))
    mad = float(np.median(np.abs(spe - med)) * 1.4826) + 1e-6
    return ((spe - med) / mad).astype(np.float32), {"n_components": k, "var_explained": round(float(cum[k - 1]), 3)}


def rolling_std_z(z: np.ndarray, groups: np.ndarray, window: int) -> np.ndarray:
    """Rolling std of z inside group segments, standardized per signal (robust)."""
    rs = np.empty_like(z)
    for s, e in segments(groups):
        _, sd = _rolling_mean_std(z[s:e], window)
        rs[s:e] = sd
    c, sc = robust_scale(rs.astype(np.float64), axis=0, floor=1e-3)
    sc = np.maximum(sc, 0.05)
    return np.clip((rs - c) / sc, -10, 10).astype(np.float32)


def cand_consensus_of_modes(z: np.ndarray, informative: np.ndarray, spe_z: Optional[np.ndarray] = None) -> tuple[np.ndarray, dict[str, Any]]:
    """Rows where most signals sit near their density mode and the correlation structure is intact."""
    n, p = z.shape
    cols = np.flatnonzero(informative)
    if len(cols) == 0:
        return np.ones(n, dtype=bool), {"note": "no informative signals"}
    modes = np.zeros(p, dtype=np.float32)
    edges = np.linspace(-10, 10, 201)
    for j in cols:
        h, _ = np.histogram(z[:, j], bins=edges)
        h = np.convolve(h, np.ones(5) / 5, mode="same")
        k = int(np.argmax(h))
        modes[j] = (edges[k] + edges[k + 1]) / 2
    d = z[:, cols] - modes[cols]
    typ = np.exp(-0.5 * d * d).mean(axis=1)
    if spe_z is not None:
        typ = typ * np.exp(-0.5 * (np.maximum(spe_z, 0.0) / 2.0) ** 2)
    thr = _otsu(typ)
    lo, hi = np.quantile(typ, [0.10, 0.70])
    thr = float(min(max(thr, lo), hi))
    return typ >= thr, {"threshold": round(thr, 4), "n_informative": int(len(cols))}


def _first_significant_change(F: np.ndarray, min_size: int, effect_min: float = 1.0, max_bkps: int = 3) -> tuple[Optional[int], float]:
    """F: (m, d) trajectory of per-chunk means. Returns (index of first significant break, effect size)."""
    m = F.shape[0]
    if m < 2 * min_size + 1:
        return None, 0.0
    bkps: list[int] = []
    try:
        import ruptures as rpt

        algo = rpt.Binseg(model="l2", min_size=min_size, jump=1).fit(F.astype(np.float64))
        k = min(max_bkps, (m // min_size) - 1)
        if k >= 1:
            bkps = [int(b) for b in algo.predict(n_bkps=k)[:-1]]
    except Exception:
        bkps = []
    if not bkps:
        # own single-split search on the l2 cost
        c1 = np.cumsum(F, axis=0)
        c2 = np.cumsum(F * F, axis=0)
        best, best_gain = None, 0.0
        tot = (c2[-1] - c1[-1] ** 2 / m).sum()
        for t in range(min_size, m - min_size + 1):
            a = (c2[t - 1] - c1[t - 1] ** 2 / t).sum()
            b = ((c2[-1] - c2[t - 1]) - (c1[-1] - c1[t - 1]) ** 2 / (m - t)).sum()
            gain = tot - a - b
            if gain > best_gain:
                best, best_gain = t, gain
        bkps = [best] if best is not None else []
    bkps = sorted(b for b in bkps if min_size <= b <= m - min_size)
    prev = 0
    for i, b in enumerate(bkps):
        nxt = bkps[i + 1] if i + 1 < len(bkps) else m
        before, after = F[prev:b], F[b:nxt]
        if len(before) < min_size or len(after) < 2:
            prev = b
            continue
        diff = np.abs(after.mean(axis=0) - before.mean(axis=0))
        pooled = np.sqrt((before.var(axis=0) + after.var(axis=0)) / 2 + 0.05)
        effect = float(np.max(diff / pooled)) if diff.size else 0.0
        # shift must be large both relative to the local noise and in absolute (robust-sigma) units
        if effect >= 2.0 and float(np.max(diff)) >= effect_min:
            return b, float(np.max(diff))
        prev = b
    return None, 0.0


def cand_pre_changepoint(z: np.ndarray, groups: np.ndarray, window: int, budget: Budget, max_points: int = 400, rstd_z: Optional[np.ndarray] = None, spe_z: Optional[np.ndarray] = None) -> tuple[np.ndarray, dict[str, Any]]:
    """Per group: rows before the first significant change of the structure-sensitive channels
    [rolling std of z (standardized), PCA residual]. Level channels are deliberately left out: slow
    drivers move levels legitimately, faults change variability and correlation structure."""
    n, p = z.shape
    mask = np.ones(n, dtype=bool)
    onsets: dict[str, int] = {}
    n_groups = 0
    skipped = 0
    t_end = time.time() + max(2.0, budget.remaining() * 0.35)
    if rstd_z is None:
        rstd_z = rolling_std_z(z, groups, window)
    chan = rstd_z if spe_z is None else np.hstack([rstd_z, spe_z[:, None]])
    gi: dict[str, list[tuple[int, int]]] = {}
    for s, e in segments(groups):
        gi.setdefault(str(groups[s]), []).append((s, e))
    for g, segs in gi.items():
        n_groups += 1
        if time.time() > t_end:
            skipped += 1
            continue
        idx = np.concatenate([np.arange(s, e) for s, e in segs])
        F = chan[idx]
        m = len(idx)
        n_chunks = min(max_points, m)
        if n_chunks < 8:
            continue
        bounds = np.linspace(0, m, n_chunks + 1).astype(int)
        Fm = np.stack([F[a:b].mean(axis=0) for a, b in zip(bounds[:-1], bounds[1:]) if b > a])
        min_size = max(3, int(0.08 * len(Fm)), int(math.ceil(2 * window / max(1, m / len(Fm)))))
        bp, effect = _first_significant_change(Fm, min_size=min_size, effect_min=1.5)
        if bp is not None:
            row_pos = int(bounds[bp])
            onsets[g] = int(row_pos)
            mask[idx[row_pos:]] = False
    return mask, {"n_groups": n_groups, "n_groups_with_change": len(onsets), "groups_skipped_for_budget": skipped, "change_positions_sample": {k: v for k, v in list(onsets.items())[:50]}}


def cand_early_segment(z: np.ndarray, groups: np.ndarray, window: int, budget: Budget, rstd_z: Optional[np.ndarray] = None, spe_z: Optional[np.ndarray] = None, max_points: int = 200) -> tuple[np.ndarray, dict[str, Any]]:
    """Temporal prior: the earliest sampled rows of each group are the most likely normal reference (runs and
    batches usually start in normal operation). Per group: its first contiguous sampled segment, cut at the
    first significant change in ANY channel -- levels included, because a fault that settles into a new steady
    state is still a fault (pre_changepoint deliberately ignores level channels). Whether this prior holds is
    decided by the same cross-group scoring as every other candidate."""
    n, p = z.shape
    mask = np.zeros(n, dtype=bool)
    if rstd_z is None:
        rstd_z = rolling_std_z(z, groups, window)
    chan_all = np.hstack([z, rstd_z] + ([spe_z[:, None]] if spe_z is not None else []))
    gi: dict[str, list[tuple[int, int]]] = {}
    for s, e in segments(groups):
        gi.setdefault(str(groups[s]), []).append((s, e))
    n_cut = 0
    t_end = time.time() + max(2.0, budget.remaining() * 0.25)
    skipped = 0
    for g, segs in gi.items():
        s, e = segs[0]
        m = e - s
        if len(segs) == 1:  # whole group (or one block) sampled: its earliest 40 % is the candidate
            e = s + max(min(m, 2 * window), int(0.4 * m))
        idx = np.arange(s, e)
        if time.time() < t_end and len(idx) >= 8:
            k = min(max_points, len(idx))
            bounds = np.linspace(0, len(idx), k + 1).astype(int)
            Fm = np.stack([chan_all[idx[a:b]].mean(axis=0) for a, b in zip(bounds[:-1], bounds[1:]) if b > a])
            if len(Fm) >= 8:
                bp, _eff = _first_significant_change(Fm, min_size=max(3, int(0.08 * len(Fm))), effect_min=1.5)
                if bp is not None and int(bounds[bp]) >= max(5, window // 2):
                    idx = idx[: int(bounds[bp])]
                    n_cut += 1
        elif time.time() >= t_end:
            skipped += 1
        mask[idx] = True
    return mask, {"n_groups": len(gi), "n_groups_cut_at_change": n_cut, "groups_skipped_for_budget": skipped, "assumption": "the earliest sampled rows of each group are the most likely normal reference"}


def cand_densest_windows(z: np.ndarray, groups: np.ndarray, window: int, seed: int = 0, spe_z: Optional[np.ndarray] = None) -> tuple[np.ndarray, dict[str, Any]]:
    """Window fingerprints = [std of z per signal, mean PCA residual]; the tightest sizeable cluster is the
    normal regime (levels are left out on purpose, see cand_pre_changepoint)."""
    from sklearn.cluster import MiniBatchKMeans

    n, p = z.shape
    w = max(4, int(window))
    wins: list[tuple[int, int]] = []
    for s, e in segments(groups):
        for a in range(s, e - w + 1, w):
            wins.append((a, a + w))
    if len(wins) < 10:
        return np.ones(n, dtype=bool), {"note": "too few windows"}
    if len(wins) > 60000:
        rng = np.random.default_rng(seed)
        keep = np.sort(rng.choice(len(wins), 60000, replace=False))
        wins = [wins[i] for i in keep]
    Fp = np.empty((len(wins), p + 1), dtype=np.float32)
    for i, (a, b) in enumerate(wins):
        seg = z[a:b]
        Fp[i, :p] = seg.std(axis=0)
        Fp[i, p] = float(np.mean(spe_z[a:b])) if spe_z is not None else 0.0
    c, s = robust_scale(Fp.astype(np.float64), axis=0, floor=1e-3)
    Fs = (Fp - c) / s
    k = int(min(6, max(2, len(wins) // 30)))
    km = MiniBatchKMeans(n_clusters=k, random_state=seed, n_init=3, batch_size=2048).fit(Fs)
    lab = km.labels_
    dist = np.linalg.norm(Fs - km.cluster_centers_[lab], axis=1)
    best, best_val, info = None, np.inf, {}
    for c_id in range(k):
        m = lab == c_id
        share = m.mean()
        if share < 0.12:
            continue
        tight = float(np.median(dist[m]))
        info[str(c_id)] = {"share": round(float(share), 3), "tightness": round(tight, 3)}
        if tight < best_val:
            best, best_val = c_id, tight
    mask = np.zeros(n, dtype=bool)
    if best is None:
        return np.ones(n, dtype=bool), {"note": "no cluster with >= 12 % of windows"}
    for i, (a, b) in enumerate(wins):
        if lab[i] == best:
            mask[a:b] = True
    return mask, {"k": k, "chosen_cluster": int(best), "clusters": info, "n_windows": len(wins)}


def cand_robust_covariance(z: np.ndarray, seed: int = 0, max_rows: int = 3000, max_dims: int = 150) -> tuple[Optional[np.ndarray], dict[str, Any]]:
    from scipy.stats import chi2
    from sklearn.covariance import MinCovDet

    n, p = z.shape
    if p > max_dims or n < 5 * p:
        return None, {"note": f"skipped (p={p}, n={n})"}
    rng = np.random.default_rng(seed)
    sub = z if n <= max_rows else z[np.sort(rng.choice(n, max_rows, replace=False))]
    # drop constant columns for the covariance estimate
    keep = sub.std(axis=0) > 1e-6
    if keep.sum() < 2:
        return None, {"note": "too few varying signals"}
    try:
        mcd = MinCovDet(random_state=seed, support_fraction=0.7).fit(sub[:, keep].astype(np.float64))
        d2 = mcd.mahalanobis(z[:, keep].astype(np.float64))
    except Exception as e:
        return None, {"note": f"MinCovDet failed: {e}"}
    thr = float(chi2.ppf(0.975, int(keep.sum())))
    return d2 <= thr, {"threshold": round(thr, 2), "dims": int(keep.sum())}


# ----------------------------------------------------------------------------------------------
# scoring
# ----------------------------------------------------------------------------------------------


def _pca_scores(fit: np.ndarray, X: np.ndarray, var_explained: float = 0.9, max_components: int = 40) -> np.ndarray:
    """T2 + SPE (each normalized by its in-sample 90th percentile) of X under a PCA fitted on `fit`."""
    mean = fit.mean(axis=0)
    Fc = fit - mean
    d = fit.shape[1]
    cov = (Fc.T @ Fc) / max(1, len(fit) - 1) + np.eye(d) * 1e-4
    w, V = np.linalg.eigh(cov)
    order = np.argsort(w)[::-1]
    w, V = np.maximum(w[order], 1e-6), V[:, order]
    cum = np.cumsum(w) / w.sum()
    k = max(1, min(int(np.searchsorted(cum, var_explained) + 1), max_components, d - 1))
    P, lam = V[:, :k], w[:k]

    def sc(A: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        Ac = A - mean
        T = Ac @ P
        t2 = ((T * T) / lam).sum(axis=1)
        R = Ac - T @ P.T
        return t2, (R * R).sum(axis=1)

    _, spef = sc(fit)
    _, spe = sc(X)
    # SPE only: slow drivers move signals inside the principal subspace (large T2 but normal); faults and
    # sensor problems leave the subspace (large SPE)
    return spe / max(np.percentile(spef, 90), 1e-6)


def _rank_auc(scores: np.ndarray, positive: np.ndarray) -> Optional[float]:
    from scipy.stats import rankdata

    pos = positive.astype(bool)
    n_pos, n_neg = int(pos.sum()), int((~pos).sum())
    if n_pos < 5 or n_neg < 5:
        return None
    r = rankdata(scores.astype(np.float64))
    return float((r[pos].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def cross_group_fit(mask: np.ndarray, zc: np.ndarray, gindex: dict[str, np.ndarray], seed: int = 0, max_fit: int = 40000) -> tuple[Optional[float], Optional[float]]:
    """Fit a normal model on the candidate rows of half the groups, evaluate on the other half (both ways).
    separation = AUROC of the model score for non-candidate vs candidate rows of the held-out half
    (a true baseline makes the excluded rows look abnormal); generalization = median held-out candidate
    score / median in-sample candidate score (a regime that recurs across groups gives ~1)."""
    keys = sorted(gindex)
    if len(keys) < 2:
        return None, None
    rng = np.random.default_rng(seed)
    perm = list(rng.permutation(keys))
    halves = [perm[0::2], perm[1::2]]
    seps, gens = [], []
    for a, b in ((0, 1), (1, 0)):
        idx_a = np.concatenate([gindex[g] for g in halves[a]])
        idx_b = np.concatenate([gindex[g] for g in halves[b]])
        fit_idx = idx_a[mask[idx_a]]
        if len(fit_idx) < 30 or len(idx_b) < 30:
            continue
        if len(fit_idx) > max_fit:
            fit_idx = np.sort(rng.choice(fit_idx, max_fit, replace=False))
        fit = zc[fit_idx].astype(np.float64)
        s_b = _pca_scores(fit, zc[idx_b].astype(np.float64))
        s_a = _pca_scores(fit, fit)
        auc = _rank_auc(s_b, ~mask[idx_b])
        if auc is not None:
            seps.append(auc)
        in_b = mask[idx_b]
        if in_b.sum() >= 10:
            gens.append(float(np.median(s_b[in_b]) / max(np.median(s_a), 1e-6)))
    return (float(np.mean(seps)) if seps else None), (float(np.mean(gens)) if gens else None)


def score_candidate(mask: np.ndarray, z: np.ndarray, groups: np.ndarray, gindex: dict[str, np.ndarray], informative: np.ndarray, others: list[np.ndarray], window: int, seed: int = 0, rstd_z: Optional[np.ndarray] = None) -> dict[str, Any]:
    n = len(mask)
    frac = float(mask.mean()) if n else 0.0
    cols = np.flatnonzero(informative)
    if mask.sum() < max(20, 2 * window) or len(cols) == 0:
        return {"score": 0.0, "fraction": round(frac, 4), "separation": 0.0, "generalization": 0.0, "coverage": 0.0, "size": 0.0, "consensus": 0.0, "note": "too few rows"}
    zc = z[:, cols] if rstd_z is None else np.hstack([z[:, cols], rstd_z[:, cols]])
    sep_auc, gen_ratio = cross_group_fit(mask, zc, gindex, seed=seed)
    separation = 0.5 if sep_auc is None else float(np.clip((sep_auc - 0.5) / 0.4, 0.0, 1.0))
    generalization = 0.5 if gen_ratio is None else float(np.clip(1.0 - max(0.0, gen_ratio - 1.0) / 2.0, 0.0, 1.0))
    covered = sum(1 for g, idx in gindex.items() if mask[idx].sum() >= max(5, min(window, len(idx) // 10)))
    coverage = covered / max(1, len(gindex))
    if 0.3 <= frac <= 0.85:
        size = 1.0
    elif frac < 0.3:
        size = float(max(0.0, (frac - 0.05) / 0.25))
    else:
        size = float(max(0.0, (1.0 - frac) / 0.15))
    if others:
        jac = []
        for o in others:
            inter = np.logical_and(mask, o).sum()
            union = np.logical_or(mask, o).sum()
            jac.append(inter / max(1, union))
        consensus = float(np.mean(jac))
    else:
        consensus = 0.5
    score = 0.35 * separation + 0.20 * generalization + 0.15 * coverage + 0.15 * size + 0.15 * consensus
    return {"score": round(float(score), 4), "fraction": round(frac, 4), "separation": round(separation, 4), "separation_auroc": None if sep_auc is None else round(sep_auc, 4), "generalization": round(generalization, 4), "generalization_ratio": None if gen_ratio is None else round(gen_ratio, 4), "coverage": round(coverage, 4), "agreement": round(0.5 * coverage + 0.5 * generalization, 4), "size": round(size, 4), "consensus": round(consensus, 4)}


# ----------------------------------------------------------------------------------------------
# reference period override
# ----------------------------------------------------------------------------------------------


def parse_reference_period(ref: Any) -> Optional[dict[str, Any]]:
    """Accepts {"rows":[s,e]} | {"row_start","row_end"} | {"row_ranges":[[s,e],..]} | {"groups":[..]} |
    "rows:100-2000" | "groups:1,2". Returns {"row_ranges": [...], "groups": [...]} or None."""
    if ref is None:
        return None
    out: dict[str, Any] = {"row_ranges": [], "groups": []}
    if isinstance(ref, str):
        for part in ref.split(";"):
            part = part.strip()
            if part.startswith("rows:"):
                a, _, b = part[5:].partition("-")
                try:
                    out["row_ranges"].append([int(a), int(b)])
                except ValueError:
                    pass
            elif part.startswith("groups:"):
                out["groups"].extend(x.strip() for x in part[7:].split(",") if x.strip())
    elif isinstance(ref, dict):
        if "rows" in ref and isinstance(ref["rows"], (list, tuple)) and len(ref["rows"]) == 2:
            out["row_ranges"].append([int(ref["rows"][0]), int(ref["rows"][1])])
        if "row_start" in ref and "row_end" in ref:
            out["row_ranges"].append([int(ref["row_start"]), int(ref["row_end"])])
        for rr in ref.get("row_ranges", []) or []:
            if len(rr) == 2:
                out["row_ranges"].append([int(rr[0]), int(rr[1])])
        out["groups"].extend(str(g) for g in (ref.get("groups", []) or []))
    elif isinstance(ref, (list, tuple)):
        for rr in ref:
            if isinstance(rr, (list, tuple)) and len(rr) == 2:
                out["row_ranges"].append([int(rr[0]), int(rr[1])])
    if not out["row_ranges"] and not out["groups"]:
        return None
    return out


# ----------------------------------------------------------------------------------------------
# main entry
# ----------------------------------------------------------------------------------------------


class _NullRegistry:
    """Evidence/inference sink used in quiet mode (bounded experiments must not spam the registries)."""

    def add(self, *args: Any, **kwargs: Any):
        class _Obj:
            id = "EV-quiet"
            claim = ""

        return _Obj()


class _NullLog:
    def record(self, *args: Any, **kwargs: Any) -> None:
        return None


class _QuietWs:
    def __init__(self, ws):
        self.evidence = _NullRegistry()
        self.inferences = _NullRegistry()
        self.log = _NullLog()
        self._ws = ws

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ws, name)


def estimate_baseline(ws, settings, sample: Sample, roles: dict[str, str], budget: Budget, ctx: Optional[dict[str, Any]] = None, seed: int = 0, quiet: bool = False) -> BaselineResult:
    if quiet:
        ws = _QuietWs(ws)
    window = int(settings.detect.window)
    z, med, scale = _zscore(sample.X)
    n, p = z.shape
    aliases = sample.aliases
    informative = np.array([roles.get(a, "unknown") not in ("constant", "derived_redundant") and scale[i] > 1e-9 and np.nanstd(sample.X[:, i]) > 1e-9 for i, a in enumerate(aliases)])
    gindex = sample.group_index()
    evidence_ids: list[str] = []
    assumptions = [
        "No normal data was provided: the baseline regime is estimated from the data itself.",
        "Assumes the dominant/stable regime that recurs across groups is the normal one.",
        f"Baseline statistics are computed on a bounded sample of {n} rows ({sample.description.get('description', 'contiguous blocks per group')}).",
    ]

    # operator override
    ref = parse_reference_period(((ctx or {}).get("options") or {}).get("reference_period"))
    if ref:
        mask = np.zeros(n, dtype=bool)
        for s, e in ref["row_ranges"]:
            mask |= (sample.rows >= s) & (sample.rows < e)
        for g in ref["groups"]:
            mask |= sample.groups == g
        if mask.sum() >= 10:
            ranges = mask_to_ranges(mask, sample.rows, sample.groups)
            ev = ws.evidence.add("baseline", f"Operator reference period covers {int(mask.sum())} of {n} sampled rows ({mask.mean():.0%}).", values={"reference": ref, "fraction": float(mask.mean())}, computed_by="detect.baseline.reference_period", n_samples=int(mask.sum()))
            inf = ws.inferences.add("dataset", "Baseline regime = operator-provided reference period", status="inferred", confidence=0.9, evidence_ids=[ev.id], reasoning="The operator designated a reference period; candidate strategies were not used.", source="human", stage="detect")
            return BaselineResult(strategy="operator_reference", mask=mask, ranges=ranges, confidence=0.9, status="inferred", candidates=[], assumptions=["Operator-designated reference period is representative of normal operation."], evidence_ids=[ev.id], inference_id=inf.id)

    cands: dict[str, tuple[np.ndarray, dict[str, Any]]] = {}
    t0 = time.time()
    spe_z, spe_info = global_spe(z, informative)
    rstd_z = rolling_std_z(z, sample.groups, window)
    channels_seconds = round(time.time() - t0, 2)
    t0 = time.time()
    m, info = cand_consensus_of_modes(z, informative, spe_z)
    cands["consensus_of_modes"] = (smooth_mask(m, sample.groups, window), info | {"seconds": round(time.time() - t0, 2)})
    t0 = time.time()
    m, info = cand_pre_changepoint(z, sample.groups, window, budget, rstd_z=rstd_z, spe_z=spe_z)
    cands["pre_changepoint"] = (m, info | {"seconds": round(time.time() - t0, 2)})
    t0 = time.time()
    m, info = cand_early_segment(z, sample.groups, window, budget, rstd_z=rstd_z, spe_z=spe_z)
    cands["early_segment"] = (m, info | {"seconds": round(time.time() - t0, 2)})
    if not budget.exhausted(margin=budget.seconds * 0.5):
        t0 = time.time()
        try:
            m, info = cand_densest_windows(z, sample.groups, window, seed=seed, spe_z=spe_z)
            cands["densest_windows"] = (smooth_mask(m, sample.groups, window), info | {"seconds": round(time.time() - t0, 2)})
        except Exception as e:
            cands["densest_windows"] = (None, {"note": f"failed: {e}"})
        t0 = time.time()
        m, info = cand_robust_covariance(z[:, informative] if informative.any() else z, seed=seed)
        cands["robust_covariance"] = (smooth_mask(m, sample.groups, window) if m is not None else None, info | {"seconds": round(time.time() - t0, 2)})
    valid = {k: v[0] for k, v in cands.items() if v[0] is not None}
    scored: list[dict[str, Any]] = []
    for name, mask in valid.items():
        others = [v for k, v in valid.items() if k != name]
        sc = score_candidate(mask, z, sample.groups, gindex, informative, others, window, seed=seed, rstd_z=rstd_z)
        sc["name"] = name
        sc["details"] = cands[name][1]
        scored.append(sc)
        ev = ws.evidence.add("baseline_candidate", f"Baseline candidate '{name}' keeps {sc['fraction']:.0%} of sampled rows; cross-group separation AUROC {sc.get('separation_auroc', 'n/a')}, generalization ratio {sc.get('generalization_ratio', 'n/a')}, group coverage {sc['coverage']:.0%}, consensus with other candidates {sc['consensus']:.2f}, score {sc['score']:.2f}.", values={k: v for k, v in sc.items() if k not in ("details",)}, computed_by=f"detect.baseline.{name}", n_samples=int(mask.sum()))
        sc["evidence_id"] = ev.id
        evidence_ids.append(ev.id)
    for name, (mask, info) in cands.items():
        if mask is None:
            scored.append({"name": name, "score": 0.0, "note": info.get("note", "not computed"), "details": info})
    scored.sort(key=lambda s: -s["score"])
    if not valid:
        mask = np.ones(n, dtype=bool)
        strategy = "all_rows"
        confidence = 0.2
    else:
        best = scored[0]
        strategy = best["name"]
        mask = valid[strategy]
        second = scored[1]["score"] if len(scored) > 1 else 0.0
        margin = float(np.clip((best["score"] - second) / 0.15, 0, 1))
        confidence = float(np.clip(0.45 * best["score"] + 0.25 * best.get("consensus", 0) + 0.2 * best.get("agreement", 0) + 0.1 * margin, 0.05, 0.95))
        # union with the candidate rows that every strategy agrees on keeps the fit set representative
    if mask.sum() < max(50, 0.05 * n):
        notes = [f"winning candidate too small ({int(mask.sum())} rows); falling back to all sampled rows"]
        mask = np.ones(n, dtype=bool)
        strategy = strategy + "+fallback_all"
        confidence = min(confidence, 0.3)
    else:
        notes = []
    if strategy.startswith("early_segment"):
        assumptions.append("Assumes each group (run/batch) starts in normal operation; the earliest sampled rows, cut at the first significant change, form the reference. Supported by cross-group separation and generalization scores, not by labels.")
    ranges = mask_to_ranges(mask, sample.rows, sample.groups)
    status = "inferred" if confidence >= 0.5 else "assumed"
    n_g = sum(1 for v in ranges.values() if v)
    ev = ws.evidence.add("baseline", f"Baseline strategy '{strategy}' selected {int(mask.sum())} of {n} sampled rows ({mask.mean():.0%}) spanning {n_g} of {len(gindex)} groups.", values={"strategy": strategy, "fraction": float(mask.mean()), "n_groups": n_g, "candidate_scores": {s['name']: s['score'] for s in scored}}, computed_by="detect.baseline.estimate_baseline", n_samples=int(mask.sum()))
    evidence_ids.append(ev.id)
    reasoning = "Candidates were scored on cross-group separation (a model fitted on the candidate rows of half the groups must make the excluded rows of the other half look abnormal), generalization across groups, group coverage, size plausibility and mutual consensus; " + ", ".join(f"{s['name']}={s['score']:.2f}" for s in scored if 'score' in s)
    inf = ws.inferences.add("dataset", f"Baseline (normal) regime = rows selected by '{strategy}' ({mask.mean():.0%} of the sample)", status=status, confidence=confidence, evidence_ids=evidence_ids, reasoning=reasoning, source="code", alternatives=[s["name"] for s in scored[1:]], stage="detect")
    ws.log.record("system:detect", "inference", "inference", inf.id, {"claim": inf.claim, "confidence": confidence, "status": status}, evidence_ids)
    notes.append(f"structure channels: PCA residual with {spe_info.get('n_components', 'n/a')} components ({channels_seconds}s)")
    return BaselineResult(strategy=strategy, mask=mask, ranges=ranges, confidence=confidence, status=status, candidates=scored, assumptions=assumptions, evidence_ids=evidence_ids, inference_id=inf.id, notes=notes)
