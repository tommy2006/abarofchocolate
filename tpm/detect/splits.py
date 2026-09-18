"""Group-aware splits for out-of-fold scoring.

* `group_folds` assigns every group to one fold (balanced by row count). With the leakage guard on,
  near-duplicate groups (identical first-k rows, or > 0.999 correlation between their standardized coarse
  trajectories -- simulations often reuse random seeds) are forced into the same fold.
* `train_val_split` splits groups for hyper-parameter / threshold selection, respecting the same
  near-duplicate structure.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional, Sequence

import numpy as np


def _union_find(n: int):
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    return find, union


def coarse_signature(X: np.ndarray, n_points: int = 32) -> np.ndarray:
    """Resample a (n, p) trajectory to n_points per signal and standardize -> flat float32 vector."""
    n, p = X.shape
    if n == 0:
        return np.zeros(n_points * p, dtype=np.float32)
    Xf = np.where(np.isfinite(X), X, np.nan).astype(np.float64)
    med = np.nanmedian(Xf, axis=0)
    Xf = np.where(np.isnan(Xf), np.nan_to_num(med), Xf)
    src = np.linspace(0, n - 1, n)
    dst = np.linspace(0, n - 1, n_points)
    out = np.empty((n_points, p), dtype=np.float64)
    for j in range(p):
        out[:, j] = np.interp(dst, src, Xf[:, j])
    out -= out.mean(axis=0)
    sd = out.std(axis=0)
    out /= np.where(sd > 1e-12, sd, 1.0)
    return out.T.reshape(-1).astype(np.float32)


def head_hash(X: np.ndarray, k: int = 25, decimals: int = 4) -> str:
    """Hash of the rounded first k rows (identical-start detector)."""
    h = X[: min(k, len(X))]
    return hashlib.sha1(np.round(np.nan_to_num(h.astype(np.float64)), decimals).tobytes()).hexdigest()


def near_duplicate_groups(groups: Sequence[str], signatures: Optional[dict[str, np.ndarray]] = None, head_hashes: Optional[dict[str, str]] = None, corr_threshold: float = 0.999, max_pairwise: int = 2500) -> tuple[dict[str, int], list[tuple[str, str, str]]]:
    """Returns (group -> super-group index, list of (a, b, reason)). Groups without evidence are singletons."""
    groups = list(groups)
    idx = {g: i for i, g in enumerate(groups)}
    find, union = _union_find(len(groups))
    dup_pairs: list[tuple[str, str, str]] = []
    if head_hashes:
        seen: dict[str, str] = {}
        for g in groups:
            h = head_hashes.get(g)
            if h is None:
                continue
            if h in seen:
                union(idx[g], idx[seen[h]])
                dup_pairs.append((seen[h], g, "identical first rows"))
            else:
                seen[h] = g
    if signatures:
        gs = [g for g in groups if g in signatures]
        if 2 <= len(gs) <= max_pairwise:
            M = np.stack([signatures[g] for g in gs]).astype(np.float64)
            M -= M.mean(axis=1, keepdims=True)
            nrm = np.linalg.norm(M, axis=1, keepdims=True)
            M /= np.where(nrm > 1e-12, nrm, 1.0)
            C = M @ M.T
            ii, jj = np.where(np.triu(C, 1) > corr_threshold)
            for a, b in zip(ii.tolist(), jj.tolist()):
                union(idx[gs[a]], idx[gs[b]])
                dup_pairs.append((gs[a], gs[b], f"trajectory correlation {C[a, b]:.4f}"))
        elif len(gs) > max_pairwise:
            # too many groups for a dense comparison: hash the rounded signature instead
            seen2: dict[str, str] = {}
            for g in gs:
                h = hashlib.sha1(np.round(signatures[g], 2).tobytes()).hexdigest()
                if h in seen2:
                    union(idx[g], idx[seen2[h]])
                    dup_pairs.append((seen2[h], g, "identical coarse trajectory"))
                else:
                    seen2[h] = g
    roots = {g: find(idx[g]) for g in groups}
    remap = {r: i for i, r in enumerate(sorted(set(roots.values())))}
    return {g: remap[r] for g, r in roots.items()}, dup_pairs


def group_folds(groups: Sequence[str], n_folds: int, leakage_guard: bool = True, sizes: Optional[dict[str, int]] = None, signatures: Optional[dict[str, np.ndarray]] = None, head_hashes: Optional[dict[str, str]] = None, seed: int = 0) -> dict[str, Any]:
    """Assign groups to folds. Returns {"fold_of": {group: fold}, "n_folds": k, "super_groups": {...},
    "duplicates": [...], "note": str}."""
    groups = list(dict.fromkeys(str(g) for g in groups))
    sizes = sizes or {g: 1 for g in groups}
    if leakage_guard and (signatures or head_hashes):
        sg, dups = near_duplicate_groups(groups, signatures, head_hashes)
    else:
        sg, dups = ({g: i for i, g in enumerate(groups)}, [])
    # aggregate super-groups
    members: dict[int, list[str]] = {}
    for g, s in sg.items():
        members.setdefault(s, []).append(g)
    units = [(sum(sizes.get(g, 1) for g in ms), sid, ms) for sid, ms in members.items()]
    rng = np.random.default_rng(seed)
    rng.shuffle(units)
    units.sort(key=lambda u: -u[0])
    k = max(1, min(int(n_folds), len(units)))
    load = [0] * k
    fold_of: dict[str, int] = {}
    for size, _sid, ms in units:
        f = int(np.argmin(load))
        load[f] += size
        for g in ms:
            fold_of[g] = f
    note = ""
    if k < n_folds:
        note = f"only {len(units)} independent group(s): using {k} fold(s)"
    return {"fold_of": fold_of, "n_folds": k, "super_groups": {str(sid): ms for sid, ms in members.items() if len(ms) > 1}, "duplicates": dups, "note": note, "fold_sizes": load}


def train_val_split(groups: Sequence[str], val_fraction: float = 0.25, sizes: Optional[dict[str, int]] = None, super_groups: Optional[dict[str, int]] = None, seed: int = 0) -> tuple[list[str], list[str]]:
    """Split groups into (train, val) by group, keeping near-duplicates together. Always leaves >= 1 group
    on each side when there are >= 2 independent units."""
    groups = list(dict.fromkeys(str(g) for g in groups))
    sizes = sizes or {g: 1 for g in groups}
    sg = super_groups or {g: i for i, g in enumerate(groups)}
    members: dict[int, list[str]] = {}
    for g in groups:
        members.setdefault(sg.get(g, hash(g)), []).append(g)
    units = list(members.values())
    if len(units) < 2:
        return groups, []
    rng = np.random.default_rng(seed)
    rng.shuffle(units)
    total = sum(sizes.get(g, 1) for g in groups)
    target = max(1e-9, val_fraction * total)
    val: list[str] = []
    acc = 0
    for ms in units:
        if acc >= target or len(val) + len(ms) >= len(groups):
            break
        val.extend(ms)
        acc += sum(sizes.get(g, 1) for g in ms)
    if not val:
        val = units[0]
    train = [g for g in groups if g not in set(val)]
    if not train:
        train, val = val[: len(val) // 2] or val[:1], val[len(val) // 2 :] or []
    return train, val


def segment_pseudo_groups(group_rows: dict[str, tuple[int, int]], n_segments: int) -> dict[str, list[int]]:
    """For datasets with too few groups: split each group's row span into n contiguous segments. Returns
    group -> boundaries (row ids where a new segment starts, excluding the first)."""
    out: dict[str, list[int]] = {}
    for g, (rmin, rmax) in group_rows.items():
        span = rmax + 1 - rmin
        n = max(1, min(n_segments, span // 50))
        out[g] = [rmin + int(round(i * span / n)) for i in range(1, n)]
    return out
