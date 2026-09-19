"""Common-mode grouping of data-quality findings (round 6, review item 18).

When the same finding shows up in many signals in the same rows (rows 1200-1799 frozen in 30 signals, the same rows
missing in 12 signals, the same windows re-quantized in 20 signals) it has ONE root: the logger, the export or the
data link, not 30 broken sensors. Such findings are reported as ONE grouped check that lists every signal and the row
ranges; the per-signal findings inside the grouped rows are not reported separately (they stay traceable in the
grouped check's ``values["members"]``).

A *block* is a maximal stretch of rows in which at least ``k`` signals (``_common.common_mode_k``: 3, or 10 % of the
signals when there are fewer than 30) carry the finding at the same time, for at least ``min_len`` rows. Blocks with a
similar set of signals are clustered and each cluster becomes one check.

Pure numpy, no workspace access: the functions are unit-tested on their own.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

import numpy as np

MEMBER_SHARE = 0.5  # a signal belongs to a block when >= half of the block, or >= half of its own finding there, overlaps
CLUSTER_JACCARD = 0.5  # blocks whose signal sets overlap this much (Jaccard) are one event family
MAX_CLUSTERS = 5  # grouped checks per batch and finding type (more families are merged into the closest one)


def _as_intervals(iv: Any) -> np.ndarray:
    """(m, 2) int64 array of inclusive [row_start, row_end], sorted by start (per signal they do not overlap)."""
    a = np.asarray(iv, dtype=np.int64).reshape(-1, 2) if len(iv) else np.zeros((0, 2), dtype=np.int64)
    if a.shape[0] > 1 and np.any(np.diff(a[:, 0]) < 0):
        a = a[np.argsort(a[:, 0], kind="stable")]
    return a


def _covered_before(starts: np.ndarray, ends_excl: np.ndarray, cum: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Rows covered by the (sorted, non-overlapping, half-open) intervals strictly before position(s) x."""
    j = np.searchsorted(starts, x, side="left") - 1  # last interval that starts before x
    out = np.zeros(x.shape, dtype=np.int64)
    ok = j >= 0
    if ok.any():
        jj = j[ok]
        full_before = cum[jj] - (ends_excl[jj] - starts[jj])
        out[ok] = full_before + np.minimum(ends_excl[jj], x[ok]) - starts[jj]
    return out


def overlap_with(intervals: Any, a: np.ndarray, b_excl: np.ndarray) -> np.ndarray:
    """For each half-open window [a, b), the number of rows covered by one signal's intervals."""
    iv = _as_intervals(intervals)
    if iv.shape[0] == 0:
        return np.zeros(np.asarray(a).shape, dtype=np.int64)
    s, e = iv[:, 0], iv[:, 1] + 1
    cum = np.cumsum(e - s)
    return _covered_before(s, e, cum, np.asarray(b_excl, dtype=np.int64)) - _covered_before(s, e, cum, np.asarray(a, dtype=np.int64))


def find_common_blocks(intervals: dict[str, Any], k: int, min_len: int = 1, member_share: float = MEMBER_SHARE) -> list[dict[str, Any]]:
    """Blocks where >= k signals carry a finding at the same rows for >= min_len consecutive rows.

    intervals: signal -> inclusive [row_start, row_end] pairs (non-overlapping per signal).
    Returns [{"row_start", "row_end" (inclusive), "n_rows", "members": [signals]}] sorted by row_start; every block has
    at least k members after the membership test."""
    arrs = {s: _as_intervals(iv) for s, iv in intervals.items()}
    arrs = {s: a for s, a in arrs.items() if a.shape[0]}
    if k < 2 or len(arrs) < k:
        return []
    starts = np.concatenate([a[:, 0] for a in arrs.values()])
    ends = np.concatenate([a[:, 1] for a in arrs.values()]) + 1
    pos = np.concatenate([starts, ends])
    delta = np.concatenate([np.ones(starts.size, dtype=np.int64), -np.ones(ends.size, dtype=np.int64)])
    order = np.lexsort((delta, pos))  # same position: an interval ending there is removed before one starting there
    pos, delta = pos[order], delta[order]
    cov = np.cumsum(delta)
    seg_a, seg_b, seg_c = pos[:-1], pos[1:], cov[:-1]
    good = np.flatnonzero((seg_c >= k) & (seg_b > seg_a))
    if good.size == 0:
        return []
    a, b = seg_a[good], seg_b[good]
    brk = np.flatnonzero(a[1:] > b[:-1]) + 1
    first = np.concatenate([[0], brk])
    last = np.concatenate([brk - 1, [good.size - 1]])
    blk_a, blk_b = a[first], b[last]
    keep = (blk_b - blk_a) >= max(1, int(min_len))
    blk_a, blk_b = blk_a[keep], blk_b[keep]
    if blk_a.size == 0:
        return []
    blen = blk_b - blk_a
    members: list[list[str]] = [[] for _ in range(blk_a.size)]
    for s, iv in arrs.items():
        ov = overlap_with(iv, blk_a, blk_b)
        # the length of this signal's own finding(s) that touch the block (so a short finding inside a long block counts)
        st, en = iv[:, 0], iv[:, 1] + 1
        cum = np.cumsum(en - st)
        lo = np.searchsorted(en, blk_a, side="right")
        hi = np.searchsorted(st, blk_b, side="left") - 1
        own = np.where(hi >= lo, cum[np.clip(hi, 0, None)] - np.where(lo >= 1, cum[np.clip(lo - 1, 0, None)], 0), 0)
        is_mem = (ov > 0) & ((ov >= member_share * blen) | (ov >= member_share * np.maximum(own, 1)))
        for i in np.flatnonzero(is_mem).tolist():
            members[i].append(s)
    out = []
    for i in range(blk_a.size):
        if len(members[i]) >= k:
            out.append({"row_start": int(blk_a[i]), "row_end": int(blk_b[i]) - 1, "n_rows": int(blen[i]), "members": sorted(members[i])})
    return out


def cluster_blocks(blocks: list[dict[str, Any]], jaccard: float = CLUSTER_JACCARD, max_clusters: int = MAX_CLUSTERS) -> list[dict[str, Any]]:
    """Group blocks that involve (mostly) the same signals: one cluster = one grouped check.

    Returns [{"blocks": [...], "members": sorted union, "n_rows", "longest", "row_start", "row_end",
    "signals_per_block_median", "signals_per_block_max"}], largest first."""
    clusters: list[dict[str, Any]] = []
    for b in blocks:
        m = set(b["members"])
        best, best_j = None, -1.0
        for c in clusters:
            rep = c["_rep"]
            jac = len(m & rep) / max(1, len(m | rep))
            if jac > best_j:
                best, best_j = c, jac
        if best is None or (best_j < jaccard and len(clusters) < max_clusters):
            clusters.append({"_rep": m, "blocks": [b]})
        else:
            best["blocks"].append(b)
    out = []
    for c in clusters:
        bl = c["blocks"]
        union: set[str] = set()
        for b in bl:
            union.update(b["members"])
        sizes = sorted(len(b["members"]) for b in bl)
        out.append({
            "blocks": bl, "members": sorted(union), "n_rows": int(sum(b["n_rows"] for b in bl)),
            "longest": int(max(b["n_rows"] for b in bl)), "row_start": int(min(b["row_start"] for b in bl)), "row_end": int(max(b["row_end"] for b in bl)),
            "signals_per_block_median": int(sizes[len(sizes) // 2]), "signals_per_block_max": int(sizes[-1]),
        })
    out.sort(key=lambda c: -c["n_rows"])
    return out


def explained_mask(intervals: Any, blocks: Iterable[dict[str, Any]], signal: Optional[str] = None, share: float = MEMBER_SHARE) -> np.ndarray:
    """Boolean per interval of one signal: True when >= ``share`` of it lies inside the given blocks (only blocks the
    signal is a member of when ``signal`` is given). Such findings are part of the grouped event, not the signal's own."""
    iv = _as_intervals(intervals)
    if iv.shape[0] == 0:
        return np.zeros(0, dtype=bool)
    bl = [(b["row_start"], b["row_end"]) for b in blocks if signal is None or signal in b.get("members", ())]
    if not bl:
        return np.zeros(iv.shape[0], dtype=bool)
    ov = overlap_with(sorted(bl), iv[:, 0], iv[:, 1] + 1)
    return ov >= share * (iv[:, 1] - iv[:, 0] + 1)
