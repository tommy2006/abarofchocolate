"""Out-of-fold ensemble scoring.

GroupKFold: for every fold, detectors are fitted on the baseline rows of the training groups (one trimming
pass removes fit rows the fresh ensemble itself finds abnormal), thresholds are calibrated on the baseline
rows of validation groups with a contamination-robust rule (min of the 99.5th percentile and a Tukey
far-out fence), and every row of the held-out groups is scored in bounded chunks by that fold's model.
Detectors are selected by reliability (threshold stability across folds, rank agreement with the others,
speed vs. budget). Detectors are specialists, so the ensemble is max-leaning: 0.7 * max + 0.3 * mean of the
three largest normalized scores (score / threshold), itself calibrated on validation rows at the 99th
percentile, so `ensemble >= 1.0` means "flagged" for every fold. Detector agreement is carried in each
flag's confidence and in the critique checks rather than in the score.

scores.parquet is written incrementally (pyarrow ParquetWriter). Fold models and the final model (fitted
on all baseline rows) are persisted with joblib under <run>/models/ for score_batch.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ..memory import chunk_rows, release
from ._common import Budget, DetectInputs, batch_ids_for_rows, fetch_group_range, fetch_range, segments
from .baseline import BaselineResult, Sample
from .detectors import Detector, build_detectors
from .features import FeatureSpec, fit_feature_spec
from .splits import coarse_signature, group_folds, head_hash, segment_pseudo_groups, train_val_split

NORM_CAP = 20.0
TOP_K_COMBINE = 3


def combine(stack: np.ndarray) -> np.ndarray:
    """Ensemble combination of normalized detector scores (n_det, n). Detectors are specialists (structure
    break, persistent shift, spike, variance change), so the ensemble fires when any specialist does:
    0.7 * max + 0.3 * mean of the TOP_K largest. The combined statistic is itself calibrated on validation
    baseline rows; detector agreement is reported separately in each flag's confidence."""
    k = min(TOP_K_COMBINE, stack.shape[0])
    mx = stack.max(axis=0)
    if k >= stack.shape[0]:
        top = stack.mean(axis=0)
    else:
        top = np.partition(stack, -k, axis=0)[-k:].mean(axis=0)
    return 0.7 * mx + 0.3 * top


def robust_threshold(s: np.ndarray, margin: float = 1.1, q: float = 0.995) -> float:
    """Threshold for a score on (possibly contaminated) baseline rows: the smaller of the q-quantile
    (99.5th percentile by default) and a Tukey far-out fence (q75 + 3 IQR, robust to ~20 % contamination),
    floored at q90."""
    s = np.asarray(s, dtype=np.float64)
    if len(s) == 0:
        return 1.0
    q25, q75, q90 = np.quantile(s, [0.25, 0.75, 0.90])
    q995 = np.quantile(s, min(q, 1.0 - 3.0 / max(30, len(s))))
    fence = q75 + 3.0 * (q75 - q25)
    thr = min(q995, max(fence, q90)) * margin
    return float(max(thr, np.median(s) * 1.5, 1e-6))


# ------------------------------------------------------------------------------------------------
# fold bookkeeping
# ------------------------------------------------------------------------------------------------


class FoldMap:
    """Maps (group, row) -> fold. Normally one fold per group; with too few groups, contiguous segments of a
    group ("pseudo-groups", keys 'g#i') are the units."""

    def __init__(self, fold_of_key: dict[str, int], pseudo: Optional[dict[str, list[int]]] = None):
        self.fold_of_key = fold_of_key
        self.pseudo = pseudo or {}

    def keys(self, groups: np.ndarray, rows: np.ndarray) -> np.ndarray:
        if not self.pseudo:
            return groups
        out = np.empty(len(groups), dtype=object)
        for s, e in segments(groups):
            g = str(groups[s])
            b = self.pseudo.get(g)
            if not b:
                out[s:e] = g
                continue
            seg = np.searchsorted(np.asarray(b), rows[s:e], side="right")
            out[s:e] = np.array([f"{g}#{i}" for i in seg], dtype=object)
        return out

    def fold_of(self, groups: np.ndarray, rows: np.ndarray) -> np.ndarray:
        keys = self.keys(groups, rows)
        uniq, inv = np.unique(keys.astype(str), return_inverse=True)
        table = np.array([self.fold_of_key.get(k, -1) for k in uniq], dtype=np.int16)
        return table[inv]

    def real_groups(self, keys: list[str]) -> list[str]:
        return sorted({k.split("#")[0] for k in keys})


@dataclass
class FoldModel:
    fold: int
    train_keys: list[str]
    val_keys: list[str]
    heldout_keys: list[str]
    spec: FeatureSpec
    detectors: dict[str, Detector]
    thresholds: dict[str, float] = field(default_factory=dict)
    ens_threshold: float = 1.0
    selected: list[str] = field(default_factory=list)
    n_fit_rows: int = 0
    n_val_rows: int = 0
    timing: dict[str, float] = field(default_factory=dict)
    val_norm: dict[str, np.ndarray] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def reset_states(self) -> None:
        for d in self.detectors.values():
            d.reset()

    def score_features(self, F: np.ndarray, groups: np.ndarray, names: Optional[list[str]] = None, isolate_state: bool = False) -> dict[str, Any]:
        """Score standardized features. Returns ensemble (normalized), ensemble_raw, per-detector normalized
        scores, per-detector contributions and the ensemble contribution shares (n, p)."""
        names = names or self.selected or list(self.detectors)
        n, p = F.shape[0], self.spec.p
        norm: dict[str, np.ndarray] = {}
        contrib: dict[str, np.ndarray] = {}
        timing: dict[str, float] = {}
        for name in names:
            det = self.detectors.get(name)
            if det is None or not det.fitted:
                continue
            t0 = time.time()
            saved = {a: copy.deepcopy(getattr(det, a)) for a in ("state", "cstate", "rstate") if hasattr(det, a)} if (isolate_state and det.stateful) else None
            s, c = det.score(F, self.spec, groups)
            if saved:
                for a, v in saved.items():
                    setattr(det, a, v)
            timing[name] = time.time() - t0
            thr = self.thresholds.get(name, 1.0)
            norm[name] = np.minimum(s / max(thr, 1e-9), NORM_CAP).astype(np.float32)
            contrib[name] = c
        if not norm:
            zeros = np.zeros(n, dtype=np.float32)
            return {"ensemble": zeros, "ensemble_raw": zeros, "norm": {}, "contrib": {}, "shares": np.full((n, p), 1.0 / max(1, p), dtype=np.float32), "timing": timing}
        stack = np.stack(list(norm.values()), axis=0)
        ens_raw = combine(stack)
        ens = ens_raw / max(self.ens_threshold, 1e-9)
        # ensemble attribution: detectors that fire strongly dominate the shares
        w = stack / np.maximum(stack.sum(axis=0, keepdims=True), 1e-6)
        shares = np.zeros((n, p), dtype=np.float32)
        for i, name in enumerate(norm):
            c = contrib[name]
            sh = c / np.maximum(c.sum(axis=1, keepdims=True), 1e-9)
            shares += w[i][:, None] * sh
        shares /= np.maximum(shares.sum(axis=1, keepdims=True), 1e-9)
        return {"ensemble": ens.astype(np.float32), "ensemble_raw": ens_raw.astype(np.float32), "norm": norm, "contrib": contrib, "shares": shares, "timing": timing}

    def describe(self) -> dict[str, Any]:
        return {"fold": self.fold, "n_train_groups": len(self.train_keys), "n_val_groups": len(self.val_keys), "n_heldout_groups": len(self.heldout_keys), "n_fit_rows": self.n_fit_rows, "n_val_rows": self.n_val_rows, "thresholds": {k: round(float(v), 4) for k, v in self.thresholds.items()}, "ens_threshold": round(float(self.ens_threshold), 4), "selected": self.selected, "timing": {k: round(v, 3) for k, v in self.timing.items()}, "detectors": {k: d.describe() for k, d in self.detectors.items()}, "notes": self.notes}


# ------------------------------------------------------------------------------------------------
# folds
# ------------------------------------------------------------------------------------------------


def make_folds(sample: Sample, groups_meta: list[dict[str, Any]], settings, seed: int = 0) -> tuple[FoldMap, dict[str, Any]]:
    n_folds = int(settings.detect.n_folds)
    sizes = {g["group"]: g["n"] for g in groups_meta}
    gindex = sample.group_index()
    pseudo: dict[str, list[int]] = {}
    keys_sizes = dict(sizes)
    key_list = list(sizes)
    if len(sizes) < max(3, min(n_folds, 3)):
        # too few groups for group folds: contiguous segments of each group become the units
        pseudo = segment_pseudo_groups({g["group"]: (g["row_min"], g["row_max"]) for g in groups_meta}, n_folds)
        key_list, keys_sizes = [], {}
        for g in groups_meta:
            b = pseudo.get(g["group"], [])
            edges = [g["row_min"]] + list(b) + [g["row_max"] + 1]
            for i in range(len(edges) - 1):
                k = f"{g['group']}#{i}"
                key_list.append(k)
                keys_sizes[k] = edges[i + 1] - edges[i]
    signatures: dict[str, np.ndarray] = {}
    hashes: dict[str, str] = {}
    if settings.detect.leakage_guard and not pseudo:
        for g, idx in gindex.items():
            X = sample.X[idx]
            signatures[g] = coarse_signature(X, n_points=32)
            hashes[g] = head_hash(X)
    fm = group_folds(key_list, n_folds, leakage_guard=bool(settings.detect.leakage_guard), sizes=keys_sizes, signatures=signatures or None, head_hashes=hashes or None, seed=seed)
    meta = {"n_folds": fm["n_folds"], "fold_sizes": fm["fold_sizes"], "near_duplicate_groups": fm["super_groups"], "duplicate_pairs": [list(d) for d in fm["duplicates"][:50]], "note": fm["note"], "pseudo_groups": {g: len(b) + 1 for g, b in pseudo.items()} if pseudo else None, "leakage_guard": bool(settings.detect.leakage_guard)}
    return FoldMap(fm["fold_of"], pseudo), meta


# ------------------------------------------------------------------------------------------------
# fitting
# ------------------------------------------------------------------------------------------------


def _dilate_within_blocks(mask: np.ndarray, groups: np.ndarray, k: int) -> np.ndarray:
    """mask OR rows within k positions of a True row, without crossing block (group-run) boundaries."""
    out = mask.copy()
    for s, e in segments(groups):
        m = mask[s:e]
        if not m.any() or m.all():
            continue
        idx = np.flatnonzero(m)
        n = e - s
        c = np.zeros(n + 1, dtype=np.int32)
        np.add.at(c, np.maximum(idx - k, 0), 1)
        np.add.at(c, np.minimum(idx + k + 1, n), -1)
        out[s:e] = np.cumsum(c[:-1]) > 0
    return out


def _fit_fold(fold: int, train_keys: list[str], val_keys: list[str], heldout_keys: list[str], sample: Sample, sample_keys: np.ndarray, baseline: BaselineResult, inputs: DetectInputs, settings, detector_names: list[str], budget: Budget, seed: int = 0) -> FoldModel:
    t0 = time.time()
    window = int(settings.detect.window)
    train_set, val_set = set(train_keys), set(val_keys)
    in_train = np.isin(sample_keys, list(train_set))
    in_val = np.isin(sample_keys, list(val_set))
    fit_mask = in_train & baseline.mask
    notes: list[str] = []
    if fit_mask.sum() < max(50, 3 * window):
        notes.append("too few baseline rows in training groups; using all their sampled rows")
        fit_mask = in_train
    spec = fit_feature_spec(sample.X, sample.groups, sample.aliases, window, baseline_mask=fit_mask)
    F = spec.transform(sample.X, sample.groups)
    max_fit = int(settings.detect.max_fit_rows)
    if fit_mask.sum() > max_fit:
        rng = np.random.default_rng(seed + fold)
        keep = np.flatnonzero(fit_mask)
        drop = rng.choice(keep, int(fit_mask.sum()) - max_fit, replace=False)
        fit_mask = fit_mask.copy()
        fit_mask[drop] = False
    clusters = inputs.relations.get("clusters") if inputs.relations else None
    timing: dict[str, float] = {}

    def fit_all(mask: np.ndarray) -> dict[str, Detector]:
        dets = build_detectors(detector_names, settings, clusters=clusters, roles=inputs.roles, seed=seed + fold)
        for name, det in list(dets.items()):
            if name == "autoencoder" and (not settings.detect.use_autoencoder or budget.fraction_used() > 0.45):
                if "autoencoder skipped (disabled or time budget)" not in notes:
                    notes.append("autoencoder skipped (disabled or time budget)")
                del dets[name]
                continue
            t1 = time.time()
            try:
                det.fit(F, spec, sample.groups, mask)
            except Exception as e:
                notes.append(f"{name} failed to fit: {e}")
                del dets[name]
                continue
            timing[f"fit_{name}"] = timing.get(f"fit_{name}", 0.0) + (time.time() - t1)
        return dets

    def score_rows(dets: dict[str, Detector], rows: np.ndarray) -> dict[str, np.ndarray]:
        """Score a contiguous row subset (states isolated) -> per-detector raw scores."""
        out: dict[str, np.ndarray] = {}
        for name, det in dets.items():
            t1 = time.time()
            s, _ = det.score(F[rows], spec, sample.groups[rows])
            det.reset()
            timing[f"score_{name}"] = timing.get(f"score_{name}", 0.0) + (time.time() - t1)
            out[name] = s
        return out

    dets = fit_all(fit_mask)
    # one trimming pass: rows of the fit set that the freshly fitted ensemble itself finds abnormal are
    # dropped (the unsupervised baseline is never perfectly clean), then refit
    train_rows = np.flatnonzero(in_train)
    n_trimmed = 0
    if train_rows.size and not budget.exhausted(margin=budget.seconds * 0.4):
        raw = score_rows(dets, train_rows)
        fit_pos = fit_mask[train_rows]
        norms = []
        for name, s in raw.items():
            thr = robust_threshold(s[fit_pos])
            norms.append(np.minimum(s / thr, NORM_CAP))
        ens = combine(np.stack(norms))
        fence = robust_threshold(ens[fit_pos], margin=1.0)
        bad = train_rows[(ens > fence) & fit_pos]
        if 0 < len(bad) < 0.4 * fit_mask.sum():
            fit_mask = fit_mask.copy()
            fit_mask[bad] = False
            n_trimmed = int(len(bad))
            dets = fit_all(fit_mask)
    fm = FoldModel(fold=fold, train_keys=list(train_keys), val_keys=list(val_keys), heldout_keys=list(heldout_keys), spec=spec, detectors=dets, n_fit_rows=int(fit_mask.sum()), notes=notes)
    fm.timing["trimmed_rows"] = n_trimmed
    # calibrate thresholds on validation baseline rows (scored in sequence, then masked)
    val_rows = np.flatnonzero(in_val)
    if len(val_rows) == 0:
        val_rows = np.flatnonzero(in_train)
        notes.append("no validation groups; thresholds calibrated on training rows (optimistic)")
    vmask = baseline.mask[val_rows].copy()
    # Extend the calibration set to the temporal neighbourhood (+-window rows, same block) of baseline rows: a
    # baseline picked near per-signal modes is a low-variance core, and thresholds calibrated on it alone flag
    # the normal regime's own wandering (false alarms). Dilation stays local, so the bulk of a fault inside a
    # partly-normal group is not pulled into the calibration set.
    n_before = int(vmask.sum())
    vmask = _dilate_within_blocks(vmask, sample.groups[val_rows], window)
    n_ext = int(vmask.sum()) - n_before
    if n_ext:
        notes.append(f"calibration set extended by {n_ext} neighbouring rows (+-{window}) of baseline rows (natural variability)")
    if vmask.sum() < 30:
        vmask = np.ones(len(val_rows), dtype=bool)
    raw = score_rows(dets, val_rows)
    for name, s in raw.items():
        sb = s[vmask]
        thr = robust_threshold(sb)
        fm.thresholds[name] = thr
        fm.val_norm[name] = np.minimum(sb / thr, NORM_CAP).astype(np.float32)
    fm.reset_states()
    fm.n_val_rows = int(vmask.sum())
    fm.timing.update(timing | {"fold_total": time.time() - t0})
    return fm


def calibrate_ensemble_threshold(fm: FoldModel, selected: list[str]) -> float:
    names = [n for n in selected if n in fm.val_norm]
    if not names:
        return 1.0
    # the per-detector thresholds already control each specialist's false-alarm rate; the combined statistic
    # is calibrated at the 99th percentile without extra margin so one specialist firing clearly is enough
    m = combine(np.stack([fm.val_norm[n] for n in names]))
    return max(robust_threshold(m, margin=1.0, q=0.99), 0.05)


SPEED_DROPPED: dict[str, str] = {}


def _prefilter_by_speed(fm: FoldModel, sample: Sample, sample_keys: np.ndarray, budget: Budget, n_rows_total: int) -> tuple[list[str], dict[str, str]]:
    """Measure seconds/row of every detector on fold 0's held-out sample rows and keep the set whose projected
    full-dataset scoring time fits ~45 % of the stage budget (cheapest first; at least two detectors)."""
    idx = np.flatnonzero(np.isin(sample_keys, fm.heldout_keys))
    names = list(fm.detectors)
    if len(idx) < 200 or len(names) <= 2:
        return names, {}
    F = fm.spec.transform(sample.X[idx], sample.groups[idx])
    res = fm.score_features(F, sample.groups[idx], names=names, isolate_state=True)
    fm.reset_states()
    per_row = {n: res["timing"].get(n, 0.0) / max(1, len(idx)) for n in names}
    projected = {n: per_row[n] * n_rows_total for n in names}
    allowed = budget.seconds * 0.45
    keep: list[str] = []
    used = 0.0
    dropped: dict[str, str] = {}
    for n in sorted(names, key=lambda k: projected[k]):
        if used + projected[n] <= allowed or len(keep) < 2:
            keep.append(n)
            used += projected[n]
        else:
            dropped[n] = f"too slow for the budget (projected {projected[n]:.0f}s of {allowed:.0f}s)"
    return keep, dropped


def fit_fold_models(sample: Sample, fold_map: FoldMap, baseline: BaselineResult, inputs: DetectInputs, settings, budget: Budget, detector_names: list[str], seed: int = 0, progress=None, n_rows_total: int = 0) -> tuple[list[FoldModel], np.ndarray]:
    speed_dropped: dict[str, str] = {}
    SPEED_DROPPED.clear()
    sample_keys = fold_map.keys(sample.groups, sample.rows).astype(str)
    key_sizes: dict[str, int] = {}
    for k in sample_keys:
        key_sizes[k] = key_sizes.get(k, 0) + 1
    all_keys = list(fold_map.fold_of_key)
    n_folds = max(fold_map.fold_of_key.values()) + 1 if all_keys else 1
    models: list[FoldModel] = []
    names_for_fold = list(detector_names)
    for f in range(n_folds):
        heldout = [k for k in all_keys if fold_map.fold_of_key[k] == f]
        rest = [k for k in all_keys if fold_map.fold_of_key[k] != f]
        if not rest:  # single fold: fit on everything, validation = training (noted)
            rest = heldout
        train, val = train_val_split(rest, val_fraction=0.25, sizes=key_sizes, seed=seed + f)
        fm = _fit_fold(f, train, val, heldout, sample, sample_keys, baseline, inputs, settings, names_for_fold, budget, seed=seed)
        models.append(fm)
        if f == 0 and n_folds > 1 and n_rows_total:
            # pilot on the first fold only: detectors whose projected full-scoring time does not fit the
            # budget are dropped now, before they are fitted on every other fold
            names_for_fold, dropped = _prefilter_by_speed(fm, sample, sample_keys, budget, n_rows_total)
            if dropped:
                fm.notes.append("dropped after fold-0 speed pilot: " + ", ".join(f"{k} ({v})" for k, v in dropped.items()))
                speed_dropped.update(dropped)
        if progress:
            progress(0.25 + 0.2 * (f + 1) / n_folds, f"fitted fold {f + 1}/{n_folds}")
        if budget.fraction_used() > 0.5 and f + 1 < n_folds:
            # degrade: remaining folds reuse this model's detectors (still out-of-fold for their held-out groups)
            for g in range(f + 1, n_folds):
                ho = [k for k in all_keys if fold_map.fold_of_key[k] == g]
                clone = copy.deepcopy(fm)
                clone.fold = g
                clone.heldout_keys = ho
                clone.notes = fm.notes + [f"fold {g} reuses fold {f}'s model (time budget); held-out groups that were in its training set are scored in-sample"]
                models.append(clone)
            break
    SPEED_DROPPED.update(speed_dropped)
    return models, sample_keys


def fit_final_model(sample: Sample, baseline: BaselineResult, inputs: DetectInputs, settings, folds: list[FoldModel], detector_names: list[str], budget: Budget, seed: int = 0) -> FoldModel:
    """Model fitted on all baseline rows; thresholds = median across folds (stable)."""
    keys = np.asarray(["all"] * sample.n, dtype=object)
    bl = baseline
    fm = _fit_fold(-1, ["all"], [], [], sample, keys, bl, inputs, settings, detector_names, budget, seed=seed + 101)
    fm.notes.append("final model: fitted on all baseline rows; thresholds are the median of the fold thresholds")
    for name in fm.detectors:
        vals = [f.thresholds[name] for f in folds if name in f.thresholds]
        if vals:
            fm.thresholds[name] = float(np.median(vals))
    return fm


# ------------------------------------------------------------------------------------------------
# detector selection
# ------------------------------------------------------------------------------------------------


def pilot_and_select(models: list[FoldModel], sample: Sample, sample_keys: np.ndarray, settings, budget: Budget, n_rows_total: int) -> dict[str, Any]:
    """Score the held-out sample rows of every fold with every detector (pilot), then choose detectors by
    threshold stability, rank agreement and projected speed."""
    from scipy.stats import spearmanr

    names = sorted({n for m in models for n in m.detectors})
    pilot: dict[str, list[np.ndarray]] = {n: [] for n in names}
    seconds: dict[str, float] = {n: 0.0 for n in names}
    n_pilot = 0
    for fm in models:
        idx = np.flatnonzero(np.isin(sample_keys, fm.heldout_keys))
        if len(idx) == 0:
            continue
        F = fm.spec.transform(sample.X[idx], sample.groups[idx])
        res = fm.score_features(F, sample.groups[idx], names=list(fm.detectors), isolate_state=True)
        for n in names:
            if n in res["norm"]:
                pilot[n].append(res["norm"][n])
                seconds[n] += res["timing"].get(n, 0.0)
        n_pilot += len(idx)
        fm.reset_states()
    stability: dict[str, float] = {}
    for n in names:
        thr = [m.thresholds[n] for m in models if n in m.thresholds]
        if len(thr) >= 2:
            cv = float(np.std(thr) / max(np.mean(thr), 1e-9))
        else:
            cv = 0.0
        stability[n] = float(np.clip(1.0 - cv, 0.0, 1.0))
    agreement: dict[str, float] = {}
    corr: dict[str, dict[str, float]] = {}
    complete = [n for n in names if len(pilot[n]) == len([m for m in models if len(np.flatnonzero(np.isin(sample_keys, m.heldout_keys))) > 0]) and pilot[n]]
    if len(complete) >= 2:
        M = {n: np.concatenate(pilot[n]) for n in complete}
        # subsample for speed
        L = len(next(iter(M.values())))
        sel = np.arange(L) if L <= 50_000 else np.sort(np.random.default_rng(0).choice(L, 50_000, replace=False))
        for a in complete:
            corr[a] = {}
            for b in complete:
                if a == b:
                    continue
                try:
                    r = spearmanr(M[a][sel], M[b][sel]).correlation
                except Exception:
                    r = 0.0
                corr[a][b] = float(0.0 if r is None or np.isnan(r) else r)
            agreement[a] = float(np.mean(list(corr[a].values()))) if corr[a] else 0.0
    for n in names:
        agreement.setdefault(n, 0.0)
    sec_per_row = {n: (seconds[n] / max(1, n_pilot)) for n in names}
    projected = {n: sec_per_row[n] * n_rows_total for n in names}
    reliability = {n: 0.5 * stability[n] + 0.5 * float(np.clip(agreement[n], 0.0, 1.0)) for n in names}
    ranked = sorted(names, key=lambda n: -reliability[n])
    best = reliability[ranked[0]] if ranked else 0.0
    selected = [n for n in ranked if reliability[n] >= 0.6 * best and agreement[n] > 0.05]
    if len(selected) < min(3, len(ranked)):
        selected = ranked[: min(3, len(ranked))]
    dropped: dict[str, str] = {n: "low reliability" for n in names if n not in selected}
    dropped.update({n: v for n, v in SPEED_DROPPED.items() if n not in names})
    # speed vs budget: the scoring pass may use ~55 % of what is left (features + I/O take the rest)
    allowed = budget.remaining() * 0.55
    while len(selected) > 2 and sum(projected[n] for n in selected) > allowed:
        slow = max(selected, key=lambda n: projected[n])
        selected.remove(slow)
        dropped[slow] = f"too slow for the budget (projected {projected[slow]:.0f}s)"
    for fm in models:
        fm.selected = [n for n in selected if n in fm.detectors]
        fm.ens_threshold = calibrate_ensemble_threshold(fm, fm.selected)
    return {"selected": selected, "dropped": dropped, "reliability": {n: round(v, 3) for n, v in reliability.items()}, "threshold_stability": {n: round(v, 3) for n, v in stability.items()}, "agreement": {n: round(v, 3) for n, v in agreement.items()}, "rank_correlation": {a: {b: round(v, 3) for b, v in row.items()} for a, row in corr.items()}, "projected_scoring_seconds": {n: round(v, 1) for n, v in projected.items()}, "pilot_rows": int(n_pilot), "thresholds_per_fold": {n: [round(float(m.thresholds[n]), 4) for m in models if n in m.thresholds] for n in names}}


# ------------------------------------------------------------------------------------------------
# full scoring pass
# ------------------------------------------------------------------------------------------------


@dataclass
class ScoreStore:
    """In-memory per-row essentials for the event/onset stages (float32/int32: ~10 bytes per row)."""

    rows: np.ndarray
    grp_codes: np.ndarray
    groups: list[str]
    ens: np.ndarray
    top1: np.ndarray
    n: int = 0

    @classmethod
    def alloc(cls, n_rows: int) -> "ScoreStore":
        return cls(rows=np.zeros(n_rows, dtype=np.int64), grp_codes=np.zeros(n_rows, dtype=np.int32), groups=[], ens=np.zeros(n_rows, dtype=np.float32), top1=np.zeros(n_rows, dtype=np.int16), n=0)

    def _grow(self, extra: int) -> None:
        cap = len(self.rows)
        if self.n + extra <= cap:
            return
        new = max(cap * 2, self.n + extra)
        for name in ("rows", "grp_codes", "ens", "top1"):
            arr = getattr(self, name)
            out = np.zeros(new, dtype=arr.dtype)
            out[: self.n] = arr[: self.n]
            setattr(self, name, out)

    def append(self, rows: np.ndarray, groups: np.ndarray, ens: np.ndarray, top1: np.ndarray, code_map: dict[str, int]) -> None:
        m = len(rows)
        self._grow(m)
        codes = np.empty(m, dtype=np.int32)
        for s, e in segments(groups):
            g = str(groups[s])
            if g not in code_map:
                code_map[g] = len(self.groups)
                self.groups.append(g)
            codes[s:e] = code_map[g]
        self.rows[self.n : self.n + m] = rows
        self.grp_codes[self.n : self.n + m] = codes
        self.ens[self.n : self.n + m] = ens
        self.top1[self.n : self.n + m] = top1
        self.n += m

    def finalize(self) -> None:
        for name in ("rows", "grp_codes", "ens", "top1"):
            setattr(self, name, getattr(self, name)[: self.n])

    def group_indices(self) -> dict[str, np.ndarray]:
        order = np.argsort(self.grp_codes, kind="stable")
        codes = self.grp_codes[order]
        out: dict[str, np.ndarray] = {}
        if len(codes) == 0:
            return out
        change = np.flatnonzero(codes[1:] != codes[:-1]) + 1
        starts = np.concatenate([[0], change])
        ends = np.concatenate([change, [len(codes)]])
        for s, e in zip(starts, ends):
            out[self.groups[int(codes[s])]] = order[s:e]
        return out


def _parquet_schema(inputs: DetectInputs, selected: list[str], full_contrib: bool):
    import pyarrow as pa

    fields = [("__row__", pa.int64()), ("__group__", pa.string()), ("batch_id", pa.string()), ("fold", pa.int16()), ("ensemble", pa.float32()), ("ensemble_raw", pa.float32()), ("is_flagged", pa.bool_())]
    fields += [(f"score_{n}", pa.float32()) for n in selected]
    for k in (1, 2, 3):
        fields += [(f"top{k}_signal", pa.string()), (f"top{k}_share", pa.float32())]
    if full_contrib:
        fields += [(f"contrib_{a}", pa.float32()) for a in inputs.aliases]
    return pa.schema(fields)


def score_all_rows(ws, inputs: DetectInputs, models: list[FoldModel], fold_map: FoldMap, selected: list[str], settings, budget: Budget, progress=None) -> tuple[ScoreStore, dict[str, Any]]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    window = int(settings.detect.window)
    lookback = window + 2
    p = inputs.p
    n_rows = inputs.n_rows
    full_contrib = n_rows * p <= 40_000_000
    chunk = int(min(settings.ingest.chunk_rows, max(20_000, chunk_rows(n_cols=6 * p, bytes_per_value=4))))
    row_min, row_max = ws.duckdb().execute("SELECT MIN(__row__), MAX(__row__) FROM dataset").fetchone()
    row_min, row_max = int(row_min), int(row_max)
    schema = _parquet_schema(inputs, selected, full_contrib)
    path = ws.path("scores")
    tmp = path.with_suffix(".parquet.tmp")
    writer = pq.ParquetWriter(str(tmp), schema, compression="zstd")
    store = ScoreStore.alloc(n_rows)
    code_map: dict[str, int] = {}
    by_fold = {m.fold: m for m in models}
    for m in models:
        m.reset_states()
    aliases = np.array(inputs.aliases, dtype=object)
    t0 = time.time()
    timing = {"fetch": 0.0, "features": 0.0, "score": 0.0, "write": 0.0}
    per_det: dict[str, float] = {n: 0.0 for n in selected}
    n_done = 0
    n_chunks = 0
    active = list(selected)
    degraded: list[str] = []
    start = row_min
    while start <= row_max:
        end = start + chunk
        t1 = time.time()
        rows_ext, groups_ext, X_ext = fetch_range(ws, inputs.columns, inputs.group_col, max(row_min, start - lookback), end)
        timing["fetch"] += time.time() - t1
        if len(rows_ext) == 0:
            start = end
            continue
        keep = rows_ext >= start
        first = int(np.argmax(keep)) if keep.any() else len(rows_ext)
        if first >= len(rows_ext):
            start = end
            continue
        folds = fold_map.fold_of(groups_ext, rows_ext)
        n_chunk = len(rows_ext) - first
        ens = np.zeros(n_chunk, dtype=np.float32)
        ens_raw = np.zeros(n_chunk, dtype=np.float32)
        det_scores = {n: np.full(n_chunk, np.nan, dtype=np.float32) for n in selected}
        shares = np.zeros((n_chunk, p), dtype=np.float32)
        fold_col = np.full(n_chunk, -1, dtype=np.int16)
        for f in np.unique(folds[first:]):
            fm = by_fold.get(int(f))
            if fm is None:
                fm = models[0]
            # rows of this fold, plus lookback rows of the same groups for exact rolling features
            in_fold = folds == f
            t1 = time.time()
            F = fm.spec.transform(X_ext[in_fold], groups_ext[in_fold])
            sel_rows = rows_ext[in_fold] >= start
            F = F[sel_rows]
            g_f = groups_ext[in_fold][sel_rows]
            timing["features"] += time.time() - t1
            t1 = time.time()
            res = fm.score_features(F, g_f, names=[n for n in active if n in fm.detectors])
            timing["score"] += time.time() - t1
            for n, sec in res["timing"].items():
                per_det[n] = per_det.get(n, 0.0) + sec
            pos = np.flatnonzero(in_fold[first:])
            ens[pos] = res["ensemble"]
            ens_raw[pos] = res["ensemble_raw"]
            for n, arr in res["norm"].items():
                det_scores[n][pos] = arr
            shares[pos] = res["shares"]
            fold_col[pos] = int(f)
            del F, res
        rows_c, groups_c = rows_ext[first:], groups_ext[first:]
        k = min(3, p)
        top = np.argsort(-shares, axis=1)[:, :k]
        t1 = time.time()
        cols: dict[str, Any] = {"__row__": rows_c, "__group__": pa.array(groups_c.astype(str)), "fold": fold_col, "ensemble": ens, "ensemble_raw": ens_raw, "is_flagged": ens >= 1.0}
        b = batch_ids_for_rows(inputs.batches, rows_c)
        cols["batch_id"] = pa.array(b.tolist() if b is not None else [None] * n_chunk, type=pa.string())
        for n in selected:
            cols[f"score_{n}"] = det_scores[n]
        for j in range(3):
            if j < k:
                cols[f"top{j + 1}_signal"] = pa.array(aliases[top[:, j]].astype(str))
                cols[f"top{j + 1}_share"] = np.take_along_axis(shares, top[:, j : j + 1], axis=1)[:, 0]
            else:
                cols[f"top{j + 1}_signal"] = pa.array([None] * n_chunk, type=pa.string())
                cols[f"top{j + 1}_share"] = np.zeros(n_chunk, dtype=np.float32)
        if full_contrib:
            for j, a in enumerate(inputs.aliases):
                cols[f"contrib_{a}"] = shares[:, j]
        writer.write_table(pa.table({name: cols[name] for name in schema.names}, schema=schema))
        timing["write"] += time.time() - t1
        store.append(rows_c, groups_c, ens, top[:, 0].astype(np.int16), code_map)
        n_done += n_chunk
        n_chunks += 1
        start = end
        if progress and n_chunks % 5 == 0:
            progress(0.5 + 0.3 * min(1.0, n_done / max(1, n_rows)), f"scored {n_done:,} rows")
        # budget projection: drop the slowest detector for the remaining chunks if we would overrun
        elapsed = time.time() - t0
        remaining_rows = max(0, (row_max + 1 - end))
        if remaining_rows > 0 and n_done > 0:
            projected = elapsed / n_done * remaining_rows
            if projected > budget.remaining() * 1.2 and len(active) > 2:
                slow = max(active, key=lambda n: per_det.get(n, 0.0))
                active.remove(slow)
                degraded.append(f"dropped {slow} after {n_done:,} rows (projected overrun)")
        del X_ext, rows_ext, groups_ext, shares
        if n_chunks % 10 == 0:
            release()
    writer.close()
    tmp.replace(path)
    store.finalize()
    ws.duckdb()  # refresh the scores view
    timing["total"] = time.time() - t0
    meta = {"chunk_rows": chunk, "n_chunks": n_chunks, "n_rows_scored": int(store.n), "full_contribution_vector": full_contrib, "timing_s": {k: round(v, 2) for k, v in timing.items()}, "detector_seconds": {k: round(v, 2) for k, v in per_det.items()}, "degraded": degraded}
    return store, meta


# ------------------------------------------------------------------------------------------------
# re-scoring a window (for onset/lag analysis of events) and persistence
# ------------------------------------------------------------------------------------------------


def rescore_window(ws, inputs: DetectInputs, fm: FoldModel, group: str, row_start: int, row_end: int, lookback: int) -> Optional[dict[str, Any]]:
    rows, groups, X = fetch_group_range(ws, inputs.columns, inputs.group_col, group, max(0, row_start - lookback), row_end)
    if len(rows) == 0:
        return None
    F = fm.spec.transform(X, groups)
    res = fm.score_features(F, groups, names=list(fm.detectors), isolate_state=True)
    z = F[:, fm.spec.block_slice("value")]
    rstd_z = F[:, fm.spec.block_slice("rstd")]
    raw_rstd = fm.spec.raw_rstd(X, groups)
    return {"rows": rows, "X": X, "F": F, "z": z, "rstd_z": rstd_z, "raw_rstd": raw_rstd, **res}


def save_models(ws, models: list[FoldModel], final: FoldModel) -> Path:
    import joblib

    d = ws.dir / "models"
    d.mkdir(parents=True, exist_ok=True)
    for m in models:
        m.val_norm = {}
        joblib.dump(m, d / f"detect_fold{m.fold}.joblib", compress=3)
    final.val_norm = {}
    joblib.dump(final, d / "detect_final.joblib", compress=3)
    return d


def load_final_model(ws) -> Optional[FoldModel]:
    import joblib

    p = ws.dir / "models" / "detect_final.joblib"
    if not p.exists():
        return None
    try:
        return joblib.load(p)
    except Exception:
        return None


def load_fold_models(ws) -> list[FoldModel]:
    import joblib

    d = ws.dir / "models"
    out = []
    if not d.exists():
        return out
    for p in sorted(d.glob("detect_fold*.joblib")):
        try:
            out.append(joblib.load(p))
        except Exception:
            continue
    return out
