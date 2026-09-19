"""Baseline data-quality checks per batch (agent B).

Data-quality problems are flagged separately from process faults: a dead sensor, a unit change, a duplicated block or
a timestamp gap is a *data* problem, reported as a CheckResult with a category (completeness | validity | consistency |
timeliness) and consumed by the trust verdict, never as a fault.

The checks run over a batch in row chunks (DuckDB -> pandas float32) with carried state so that a frozen run, a
repeated row or a timestamp gap crossing a chunk boundary is still seen. Everything is vectorised numpy.

Check types produced (check_type -> category):
    missing, dropout, empty_rows, missing_block                      completeness
    out_of_range, plausibility, impossible_value, unit_shift,
    quantization_change, quantization_block, local_spike             validity
    duplicate_rows, duplicate_key, stuck, saturation, frozen_block,
    relation_break                                                   consistency
    gap, out_of_order, duplicate_timestamp, irregular_sampling,
    stale, timeliness_not_testable (status "not_testable")           timeliness
    <category>_ok                                    one per category and batch when nothing was found

Round 6 (review items 18-20, 7):
* common-mode findings - the same rows frozen, missing or re-quantized in >= 3 signals (or >= 10 % of them) - are ONE
  grouped check (frozen_block, missing_block, quantization_block; see grouping.py). The per-signal findings inside the
  grouped rows are not reported again; they stay traceable in the grouped check's values["members"];
* plausibility: every reading is tested against an explicit plausible range (plausibility.py). out_of_range reports
  only readings inside that range (unusual but possible), so a reading is never reported by both;
* no time column (or no usable timestamps in a batch): one timeliness check with status "not_testable" instead of a
  trivial pass;
* every check carries values["confidence"] / values["confidence_basis"] (heuristic, from its own counters);
* run statistics are no longer capped at 48 runs per signal (the stuck share was underestimated on long batches) and
  exact duplicates are found across the whole batch, not only inside one read chunk.
"""
from __future__ import annotations

import math
import time
import warnings
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..contracts import CheckResult, TrustVerdict
from ..memory import chunk_rows
from .batches import load_batch_frame
from ._common import (
    GROUP_COL,
    NOT_TESTABLE,
    ROW_COL,
    WORDING,
    SignalInfo,
    check_confidence,
    data_wording,
    common_mode_k,
    contiguous_runs,
    fmt_num,
    global_stats,
    load_catalog,
    next_check_id,
    numeric_signals,
    resolve_columns,
    time_column_in,
    to_seconds,
    value_runs,
    _fp_get,
)
from .grouping import cluster_blocks, explained_mask, find_common_blocks, overlap_with
from .plausibility import plausible_ranges, range_words

CATEGORY_OF = {
    "missing": "completeness", "dropout": "completeness", "empty_rows": "completeness", "missing_block": "completeness",
    "out_of_range": "validity", "plausibility": "validity", "impossible_value": "validity", "unit_shift": "validity", "quantization_change": "validity",
    "quantization_block": "validity", "local_spike": "validity",
    "duplicate_rows": "consistency", "duplicate_key": "consistency", "stuck": "consistency", "saturation": "consistency", "frozen_block": "consistency",
    "sign_violation": "consistency", "relation_break": "consistency",
    "gap": "timeliness", "out_of_order": "timeliness", "duplicate_timestamp": "timeliness", "irregular_sampling": "timeliness", "stale": "timeliness",
    "timeliness_not_testable": "timeliness",
}
# grouped (common-mode) check -> the per-signal check types it replaces inside its rows
GROUPED_TYPES = {"frozen_block": ("stuck", "saturation", "stale"), "missing_block": ("missing", "dropout"), "quantization_block": ("quantization_change",)}
CATEGORIES = ["completeness", "validity", "consistency", "timeliness"]
CHECKABLE_ROLES = {"continuous_measured", "actuator_like", "held_sampled", "derived_redundant", "unknown"}
MAX_EVENTS = 12  # row ranges kept per single-signal check (aggregates only)
MAX_RECORD_EVENTS = 200  # row ranges kept for record-level findings (duplicates, empty rows, common-mode blocks): row-scoped trust
MAX_POINTS = 200  # per-run deviations kept per check for the suspicious-rows list (row ids + z, never raw values)
MAX_RUNS_STORED = 50_000  # runs of identical readings kept per signal and batch; exact counters beyond
MAX_WINDOW_RUNS = 5_000  # re-quantized window runs kept per signal and batch
MAX_BLOCKS_LISTED = 100  # blocks listed in a grouped check (all of them are counted)
RECORD_SHARE_REF = 0.10  # share of the rows at which a record-level problem is at full severity (see trust.RECORD_BATCH_SHARE)
SPIKE_ROLES = {"continuous_measured", "derived_redundant", "unknown"}
ABSURD_ABS = 1e30


@dataclass
class _SigAcc:
    n: int = 0
    n_missing: int = 0
    n_missing_own: int = 0  # missing where fewer than k signals are missing at once: the signal's own gaps
    co_missing: int = 0  # missing while >= k signals are missing at once (part of a common-mode gap or an empty row)
    miss_rows: list[tuple[int, int]] = field(default_factory=list)
    longest_missing: int = 0
    oor_n: int = 0
    oor_maxz: float = 0.0
    oor_rows: list[tuple[int, int]] = field(default_factory=list)
    oor_examples: list[tuple[int, float]] = field(default_factory=list)
    oor_points: list[tuple[int, int, float]] = field(default_factory=list)  # (row_start, row_end, max robust z) per out-of-range run
    pl_n: int = 0
    pl_below: int = 0
    pl_above: int = 0
    pl_lowest: float = math.inf
    pl_highest: float = -math.inf
    pl_worst_excess: float = 0.0
    pl_worst_row: int = -1
    pl_worst_value: float = math.nan
    pl_rows: list[tuple[int, int]] = field(default_factory=list)
    pl_points: list[tuple[int, int, float, str]] = field(default_factory=list)
    spike_n: int = 0
    spike_maxz: float = 0.0
    spike_rows: list[tuple[int, int]] = field(default_factory=list)
    spike_points: list[tuple[int, int, float, str]] = field(default_factory=list)  # (row_start, row_end, local z, direction)
    imp_n: int = 0
    imp_rows: list[tuple[int, int]] = field(default_factory=list)
    shift_windows: list[tuple[int, int, int]] = field(default_factory=list)  # (row_start, row_end, k)
    quant_windows: list[tuple[int, int, float]] = field(default_factory=list)
    n_quant_windows: int = 0
    # runs of identical readings (>= min_keep), each committed once (a run crossing a chunk boundary is merged first)
    run_starts: list[np.ndarray] = field(default_factory=list)
    run_lens: list[np.ndarray] = field(default_factory=list)
    run_vals: list[np.ndarray] = field(default_factory=list)
    n_runs_stored: int = 0
    runs_overflow: Counter = field(default_factory=Counter)  # (at global min/max, length) -> count beyond MAX_RUNS_STORED
    run_lengths_sum: int = 0
    run_count: int = 0
    # carry between chunks
    carry_value: float = math.nan
    carry_len: int = 0
    carry_start: int = -1
    min_seen: float = math.inf
    max_seen: float = -math.inf


@dataclass
class _PairAcc:
    n: int = 0
    sx: float = 0.0
    sy: float = 0.0
    sxx: float = 0.0
    syy: float = 0.0
    sxy: float = 0.0
    resid_sq: float = 0.0
    resid_n: int = 0


def _stride_events(events: list[tuple[int, int]], stride: int) -> list[tuple[int, int]]:
    if stride <= 1:
        return events
    return [(a, b + stride - 1) for a, b in events]


def _drop_runs_touching(mask: np.ndarray, other: np.ndarray) -> np.ndarray:
    """``mask`` without its contiguous runs that overlap ``other`` (same length)."""
    if not mask.any() or not other.any():
        return mask
    out = mask.copy()
    cs = np.concatenate([[0], np.cumsum(other, dtype=np.int64)])
    for a, b in contiguous_runs(mask):
        if cs[b + 1] - cs[a] > 0:
            out[a : b + 1] = False
    return out


def _repeated_rows(df: pd.DataFrame, cols: list[str], seen: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Rows repeating an earlier row of the same batch (over ``cols``), also across read chunks: 64-bit row hashes
    of every row seen so far are kept (sorted, unique; 8 bytes per row)."""
    h = pd.util.hash_pandas_object(df[cols], index=False).to_numpy(dtype=np.uint64)
    dup = np.array(pd.Series(h).duplicated(keep="first").to_numpy(), dtype=bool, copy=True)
    if seen.size:
        idx = np.clip(np.searchsorted(seen, h), 0, seen.size - 1)
        dup |= seen[idx] == h
    return dup, np.union1d(seen, h)


class BatchAccumulator:
    """Accumulates per-signal statistics over the chunks of one batch and turns them into CheckResults."""

    def __init__(self, ws: Any, settings: Any, batch_id: str, catalog: list[SignalInfo], stats: dict[str, dict[str, Any]], schema: Any = None, relations: Any = None, stride: int = 1, fast: bool = False, plausible: Optional[dict[str, dict[str, Any]]] = None, missing_expected: float = 0.0, wording: str = "sensor"):
        self.ws = ws
        self.wording = wording if wording in WORDING else "sensor"
        self.w = WORDING[self.wording]
        self.settings = settings
        self.q = settings.quality
        self.batch_id = batch_id
        self.catalog = catalog
        self.by_alias = {s.alias: s for s in catalog}
        self.stats = stats
        self.schema = schema
        self.stride = max(1, int(stride))
        self.fast = fast
        self.plaus = plausible or {}
        self.sig: dict[str, _SigAcc] = {}
        self.n_rows = 0
        self.row_start: Optional[int] = None
        self.row_end: Optional[int] = None
        self.empty_rows = 0
        self.empty_ranges: list[tuple[int, int]] = []
        self.dup_rows = 0
        self.dup_ranges: list[tuple[int, int]] = []
        self.dup_iv: list[np.ndarray] = []  # every duplicate-row range, (k, 2) int64 per chunk (rows a frozen block shares with duplicates count once)
        self.dupkey_rows = 0
        self.dupkey_ranges: list[tuple[int, int]] = []
        self.dupkey_also_dup = 0  # repeated-key rows that are exact duplicate rows too (counted once for trust)
        self._dup_seen = np.zeros(0, dtype=np.uint64)
        self._key_seen = np.zeros(0, dtype=np.uint64)
        self.group_ids: set[str] = set()
        # common mode: k signals with the same finding in the same rows (missing: raised above what chance explains)
        n_sig = len(numeric_signals(catalog))
        self.k = common_mode_k(n_sig)
        exp = max(0.0, float(missing_expected or 0.0))
        self.k_missing = max(self.k, int(math.ceil(exp + 4.0 * math.sqrt(exp) + 1.0))) if exp > 0 else self.k
        self.mb_rows = 0  # rows (not all-missing) with >= k_missing signals missing at once
        self.mb_events: list[tuple[int, int]] = []
        self.mb_count_sum = 0
        self.mb_count_max = 0
        self.mb_member_rows: dict[str, int] = {}
        # time
        self.saw_time = False
        self.t_last: float = math.nan
        self.t_last_row: int = -1
        self.t_n = 0
        self.t_neg = 0
        self.t_neg_rows: list[tuple[int, int]] = []
        self.t_zero = 0
        self.t_zero_rows: list[tuple[int, int]] = []
        self.t_gaps: list[tuple[int, int, float]] = []
        self.t_max_gap = 0.0
        self.t_irregular = 0
        self.period_ref: Optional[float] = getattr(schema, "sample_period_seconds", None) if schema else None
        self.t_diff_sample: list[np.ndarray] = []
        self.pairs: dict[tuple[str, str], _PairAcc] = {}
        self.pair_specs = _redundant_pairs(relations, catalog, stats)
        self.derived_specs = _derived_specs(relations, catalog)
        self.n_chunks = 0

    # ------------------------------------------------------------ per chunk
    def update(self, df: pd.DataFrame) -> None:
        self.n_chunks += 1
        n = len(df)
        if n == 0:
            return
        rows = df[ROW_COL].to_numpy(dtype="int64") if ROW_COL in df.columns else np.arange(self.n_rows, self.n_rows + n, dtype="int64")
        r0, r1 = int(rows[0]), int(rows[-1])
        self.row_start = r0 if self.row_start is None else min(self.row_start, r0)
        self.row_end = r1 if self.row_end is None else max(self.row_end, r1)
        self.n_rows += n
        if GROUP_COL in df.columns:
            try:
                self.group_ids.update(str(g) for g in pd.unique(df[GROUP_COL].dropna()))
            except Exception:
                pass
        if GROUP_COL in df.columns and n > 1:
            gv = df[GROUP_COL].to_numpy()
            self._boundary = np.concatenate([[True], gv[1:] != gv[:-1]])
        else:
            self._boundary = None
        colmap = resolve_columns(df.columns, self.catalog)
        sigs = [s for s in numeric_signals(self.catalog) if s.alias in colmap]
        all_nan = np.ones(n, dtype=bool) if sigs else np.zeros(n, dtype=bool)
        arrays: dict[str, np.ndarray] = {}
        nans: dict[str, np.ndarray] = {}
        for s in sigs:
            x = _as_float(df[colmap[s.alias]])
            arrays[s.alias] = x
            nan = np.isnan(x)
            nans[s.alias] = nan
            all_nan &= nan
            self.sig.setdefault(s.alias, _SigAcc()).n += n
        common = self._common_missing(nans, all_nan, rows) if len(nans) >= max(2, self.k_missing) else None
        for s in sigs:
            acc = self.sig[s.alias]
            x, nan = arrays[s.alias], nans[s.alias]
            self._completeness(acc, nan, nan if common is None else (nan & ~common), rows)
            if s.role in CHECKABLE_ROLES:
                st = self.stats.get(s.alias, {})
                self._validity(acc, s, st, x, nan, rows)
                self._runs(acc, s, x, rows)
        # rows entirely empty
        if sigs:
            e = int(all_nan.sum())
            if e:
                self.empty_rows += e
                self._add_ranges(self.empty_ranges, contiguous_runs(all_nan), rows, MAX_RECORD_EVENTS)
        # exact duplicates over the whole batch (signal columns + time), empty rows excluded (reported as such)
        tcol = time_column_in(df.columns, self.schema)
        subset = [colmap[s.alias] for s in sigs] + ([tcol] if tcol else [])
        dup = None
        if subset:
            dup, self._dup_seen = _repeated_rows(df, subset, self._dup_seen)
            if sigs:
                dup &= ~all_nan
            d = int(dup.sum())
            if d:
                self.dup_rows += d
                edges = np.diff(np.concatenate([[0], dup.astype(np.int8), [0]]))
                st_i, en_i = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1) - 1
                self.dup_iv.append(np.stack([rows[st_i], rows[en_i]], axis=1).astype(np.int64))
                room = MAX_RECORD_EVENTS - len(self.dup_ranges)
                if room > 0:
                    self.dup_ranges.extend((int(a), int(b)) for a, b in zip(rows[st_i[:room]], rows[en_i[:room]]))
        key_cols = self._key_columns(df)
        if key_cols:
            dk, self._key_seen = _repeated_rows(df, key_cols, self._key_seen)
            d = int(dk.sum())
            if d:
                self.dupkey_rows += d
                if dup is not None:
                    self.dupkey_also_dup += int(np.count_nonzero(dk & dup))
                self._add_ranges(self.dupkey_ranges, contiguous_runs(dk), rows, MAX_RECORD_EVENTS)
        # time
        if tcol:
            self.saw_time = True
            self._time(to_seconds(df[tcol], None if pd.api.types.is_datetime64_any_dtype(df[tcol]) else getattr(self.schema, "sample_period_seconds", None)), rows)
        # relations
        if not self.fast:
            self._relations(arrays)

    def _common_missing(self, nans: dict[str, np.ndarray], all_nan: np.ndarray, rows: np.ndarray) -> Optional[np.ndarray]:
        """Rows where >= k_missing signals are missing at once (a logging or transmission gap): counted once for the
        grouped missing_block check instead of once per signal. Returns the mask (None when there is none)."""
        cnt = np.zeros(all_nan.size, dtype=np.int32)
        for m in nans.values():
            cnt += m
        common = cnt >= self.k_missing
        if not common.any():
            return None
        partial = common & ~all_nan  # all-missing rows are reported as empty_rows
        for alias, m in nans.items():
            c = int(np.count_nonzero(m & common))
            if c:
                self.sig[alias].co_missing += c
            if partial.any():
                cp = int(np.count_nonzero(m & partial))
                if cp:
                    self.mb_member_rows[alias] = self.mb_member_rows.get(alias, 0) + cp
        p = int(partial.sum())
        if p:
            self.mb_rows += p
            self.mb_count_sum += int(cnt[partial].sum())
            self.mb_count_max = max(self.mb_count_max, int(cnt[partial].max()))
            self._add_ranges(self.mb_events, contiguous_runs(partial), rows, MAX_RECORD_EVENTS)
        return common

    def _key_columns(self, df: pd.DataFrame) -> list[str]:
        if self.schema is None:
            return []
        oc = getattr(self.schema, "order_column", None)
        if not oc or oc not in df.columns:
            return []
        keys = [oc]
        gcols = list(getattr(self.schema, "group_columns", []) or [])
        if GROUP_COL in df.columns:
            keys.append(GROUP_COL)
        elif gcols and all(g in df.columns for g in gcols):
            keys.extend(gcols)
        elif gcols or int(getattr(self.schema, "n_groups", 1) or 1) > 1:
            return []  # cannot tell groups apart in this frame: the order key alone is not unique
        return keys

    @staticmethod
    def _add_ranges(dst: list[tuple[int, int]], runs: list[tuple[int, int]], rows: np.ndarray, cap: int = MAX_EVENTS) -> None:
        for a, b in runs:
            if len(dst) >= cap:
                break
            dst.append((int(rows[a]), int(rows[b])))

    def _completeness(self, acc: _SigAcc, nan: np.ndarray, own: np.ndarray, rows: np.ndarray) -> None:
        m = int(nan.sum())
        if not m:
            return
        acc.n_missing += m
        o = int(own.sum())
        if not o:
            return
        acc.n_missing_own += o
        runs = contiguous_runs(own)
        if runs:
            acc.longest_missing = max(acc.longest_missing, max(b - a + 1 for a, b in runs) * self.stride)
            self._add_ranges(acc.miss_rows, runs, rows)

    def _plausibility(self, acc: _SigAcc, s: SignalInfo, x: np.ndarray, ok: np.ndarray, rows: np.ndarray, z: Optional[np.ndarray], shifted: Optional[np.ndarray] = None) -> Optional[np.ndarray]:
        """Readings outside the signal's plausible range (see plausibility.py). Returns the mask (None: no range).
        Stretches that touch a detected unit shift are left to the unit_shift check."""
        pr = self.plaus.get(s.alias)
        if pr is None:
            return None
        lo, hi = float(pr["lo"]), float(pr["hi"])
        with np.errstate(invalid="ignore"):
            below = ok & (x < lo)
            above = ok & (x > hi)
        pl = below | above
        if shifted is not None and pl.any():
            pl = _drop_runs_touching(pl, shifted)
            below &= pl
            above &= pl
        if not pl.any():
            return pl
        acc.pl_n += int(pl.sum())
        nb, na = int(below.sum()), int(above.sum())
        acc.pl_below += nb
        acc.pl_above += na
        if nb:
            acc.pl_lowest = min(acc.pl_lowest, float(np.min(x[below])))
        if na:
            acc.pl_highest = max(acc.pl_highest, float(np.max(x[above])))
        exc = np.where(below, lo - np.nan_to_num(x, nan=lo), 0.0) + np.where(above, np.nan_to_num(x, nan=hi) - hi, 0.0)
        i = int(np.argmax(exc))
        if exc[i] > acc.pl_worst_excess:
            acc.pl_worst_excess, acc.pl_worst_row, acc.pl_worst_value = float(exc[i]), int(rows[i]), float(x[i])
        runs = contiguous_runs(pl)
        self._add_ranges(acc.pl_rows, runs, rows)
        for a, b in runs:
            if len(acc.pl_points) >= MAX_POINTS:
                break
            zz = round(float(np.nanmax(z[a : b + 1])), 2) if z is not None else 0.0
            acc.pl_points.append((int(rows[a]), int(rows[b]), zz, "down" if below[a] else "up"))
        return pl

    def _validity(self, acc: _SigAcc, s: SignalInfo, st: dict[str, Any], x: np.ndarray, nan: np.ndarray, rows: np.ndarray) -> None:
        ok = ~nan
        if not ok.any():
            return
        xv = x[ok]
        acc.min_seen = min(acc.min_seen, float(xv.min()))
        acc.max_seen = max(acc.max_seen, float(xv.max()))
        # impossible values
        imp = ~np.isfinite(x) & ~nan
        imp |= np.abs(np.nan_to_num(x, nan=0.0)) > ABSURD_ABS
        if imp.any():
            acc.imp_n += int(imp.sum())
            self._add_ranges(acc.imp_rows, contiguous_runs(imp), rows)
        med, scale = st.get("median"), st.get("scale") or 0.0
        z = np.abs(x - med) / scale if (med is not None and scale > 0) else None
        # unit shift / re-quantization windows first: readings inside a detected unit shift are reported by the
        # unit_shift check (the cause), not a second time as implausible or out of range
        shifted = self._windows(acc, x, rows, float(med), float(scale)) if (z is not None and s.role != "constant" and not self.fast) else None
        pl = self._plausibility(acc, s, x, ok & ~imp, rows, z, shifted)
        if z is None or s.role == "constant":
            return
        oor = (z > float(self.q.range_sigma)) & ok & ~imp
        if pl is not None:
            oor &= ~pl  # outside the plausible range is reported by the plausibility check, not twice
        if shifted is not None:
            oor = _drop_runs_touching(oor, shifted)
        if oor.any():
            c = int(oor.sum())
            acc.oor_n += c
            acc.oor_maxz = max(acc.oor_maxz, float(np.nanmax(np.where(oor, z, 0.0))))
            self._add_ranges(acc.oor_rows, contiguous_runs(oor), rows)
            if len(acc.oor_examples) < 5:
                idx = np.flatnonzero(oor)[: 5 - len(acc.oor_examples)]
                acc.oor_examples.extend((int(rows[i]), float(x[i])) for i in idx)
            for a, b in contiguous_runs(oor):
                if len(acc.oor_points) >= MAX_POINTS:
                    break
                acc.oor_points.append((int(rows[a]), int(rows[b]), round(float(np.nanmax(z[a : b + 1])), 2)))
        if self.fast:
            return
        self._local_spikes(acc, s, x, ok & ~imp, rows, float(med), float(scale))

    def _windows(self, acc: _SigAcc, x: np.ndarray, rows: np.ndarray, med: float, scale: float) -> Optional[np.ndarray]:
        """Unit shift (window median vs global median ~ 10^k, windows of 25 samples; shorter shifts are caught as
        implausible / out of range) and re-quantization windows. Returns the rows inside unit-shift windows."""
        n = x.size
        w = 25 if n >= 50 else max(5, n // 2)
        if n < w or abs(med) <= 3.0 * scale:
            return None
        nwin = n // w
        xw = x[: nwin * w].reshape(nwin, w)
        shifted = None
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            wmed = np.nanmedian(xw, axis=1)
            ratio = np.abs(wmed / med)
            lg = np.log10(np.where(ratio > 0, ratio, np.nan))
        k = np.round(lg)
        hit = np.isfinite(lg) & (np.abs(k) >= 1) & (np.abs(lg - k) < 0.2)
        if hit.any():
            shifted = np.zeros(n, dtype=bool)
            shifted[: nwin * w] = np.repeat(hit, w)
            for a, b in contiguous_runs(hit):
                if len(acc.shift_windows) >= MAX_EVENTS:
                    break
                acc.shift_windows.append((int(rows[a * w]), int(rows[min(n - 1, (b + 1) * w - 1)]), int(k[a])))
        # quantization / precision change: a continuous-looking signal (almost all values distinct within a
        # window) that suddenly shows only a handful of distinct values was re-quantized (vectorised: sort rows)
        if nwin >= 4:
            xs = np.sort(xw, axis=1)  # NaN sorts last
            valid = np.isfinite(xw).sum(axis=1)
            with np.errstate(invalid="ignore"):
                distinct = 1 + (np.diff(xs, axis=1) > 0).sum(axis=1)
            okw = valid >= max(10, w // 2)
            if okw.sum() >= 4:
                uniq = np.where(okw, distinct / np.maximum(valid, 1), np.nan)
                ref = float(np.nanmedian(uniq))
                acc.n_quant_windows += int(okw.sum())
                if ref >= 0.6:
                    hitq = okw & (uniq < 0.5 * ref) & (distinct >= 2)
                    for a, b in contiguous_runs(hitq):
                        if len(acc.quant_windows) >= MAX_WINDOW_RUNS:
                            break
                        acc.quant_windows.append((int(rows[a * w]), int(rows[min(n - 1, (b + 1) * w - 1)]), float(np.nanmean(uniq[a : b + 1]) / ref)))
        return shifted

    def _local_spikes(self, acc: _SigAcc, s: SignalInfo, x: np.ndarray, ok: np.ndarray, rows: np.ndarray, med: float, scale: float) -> None:
        """Local spike check: a reading that jumps away from its own neighbourhood (rolling median) and comes
        straight back, even when it stays inside the signal's global range. The noise scale is local too (rolling
        median of the absolute residual), so a noisy regime does not turn ordinary readings into spikes. A spike
        must (1) be at most `spike_max_len` readings long, (2) return to the neighbourhood on both sides (a step or
        a fault onset does not), (3) be isolated in time (a burst of candidates is a regime change, handled by the
        detectors) and (4) not sit at a group boundary. Step-like and sample-and-hold signals are skipped (jumps are
        their normal behaviour); strided reads are skipped (neighbours missing)."""
        q = self.q
        w = int(getattr(q, "spike_window", 7))
        k = float(getattr(q, "spike_sigma", 10.0))
        if self.stride != 1 or s.role not in SPIKE_ROLES or x.size < max(60, 3 * w) or scale <= 0:
            return
        from scipy.ndimage import median_filter, uniform_filter1d

        filled = np.where(ok & np.isfinite(x), x, med)
        # mirror padding: "nearest" replicates the last residual, which is often exactly 0 (the last value is the
        # median of its own padded window), and made the local noise collapse near chunk ends
        r = filled - median_filter(filled, size=w, mode="mirror")
        a = np.abs(r)
        # local noise = rolling MEAN absolute residual (x 1.2533 = sigma for Gaussian noise): O(n), and unlike a
        # rolling median it cannot collapse to 0 when many residuals are exactly 0
        # leave each reading out of its own noise estimate, so a spike does not inflate the scale it is judged by
        loc = 1.2533 * np.maximum(uniform_filter1d(a, size=51, mode="mirror") * 51.0 - a, 0.0) / 50.0
        glob = 1.2533 * float(np.mean(a))
        floor = float(getattr(q, "spike_min_scale", 0.15)) * scale / max(k, 1e-9)  # visible at the signal's own scale
        zloc = a / np.maximum(np.maximum(loc, 0.5 * glob), max(floor, 1e-12))
        cand = [(c0, c1) for c0, c1 in contiguous_runs((zloc > k) & ok)]
        if not cand:
            return
        starts = np.array([c0 for c0, _ in cand])
        max_len = int(getattr(q, "spike_max_len", 2))
        boundary = getattr(self, "_boundary", None)
        n = x.size
        for c0, c1 in cand:
            if c1 - c0 + 1 > max_len:
                continue  # a longer excursion is a level change, not a spike
            if boundary is not None and boundary[max(0, c0 - 3) : c1 + 4].any():
                continue  # level jumps between groups are not glitches
            if int((np.abs(starts - c0) <= 20).sum()) - 1 >= 2:
                continue  # burst of candidates: regime change, not an isolated reading
            peak = float(a[c0 : c1 + 1].max())
            left = float(a[c0 - 1]) if c0 > 0 else 0.0
            right = float(a[c1 + 1]) if c1 + 1 < n else 0.0
            if max(left, right) >= 0.4 * peak:
                continue  # does not come straight back
            zmax = float(zloc[c0 : c1 + 1].max())
            acc.spike_n += c1 - c0 + 1
            acc.spike_maxz = max(acc.spike_maxz, zmax)
            if len(acc.spike_rows) < MAX_EVENTS:
                acc.spike_rows.append((int(rows[c0]), int(rows[c1])))
            if len(acc.spike_points) < MAX_POINTS:
                acc.spike_points.append((int(rows[c0]), int(rows[c1]), round(zmax, 2), "up" if r[c0 + int(np.argmax(a[c0 : c1 + 1]))] > 0 else "down"))

    # ------------------------------------------------------------ runs of identical readings
    def _is_sat(self, alias: str, vals: np.ndarray) -> np.ndarray:
        st = self.stats.get(alias, {})
        gmin, gmax = st.get("min"), st.get("max")
        tol = 1e-9 * max(1.0, abs(gmin or 0.0), abs(gmax or 0.0))
        out = np.zeros(vals.shape, dtype=bool)
        if gmin is not None:
            out |= np.abs(vals - float(gmin)) <= tol
        if gmax is not None:
            out |= np.abs(vals - float(gmax)) <= tol
        return out

    def _commit_runs(self, acc: _SigAcc, s: SignalInfo, rs: np.ndarray, ln: np.ndarray, vals: np.ndarray) -> None:
        """Record finished runs once (rs: first row, ln: length in read samples, vals: the repeated value)."""
        if rs.size == 0:
            return
        acc.run_count += int(rs.size)
        acc.run_lengths_sum += int(ln.sum())
        L = ln.astype(np.int64) * self.stride
        min_keep = max(2, int(self.q.stuck_min_run) // 3, (s.hold_period or 0) * 2)
        keep = (L >= min_keep) & np.isfinite(vals)
        if not keep.any():
            return
        rs, L, vals = rs[keep].astype(np.int64), L[keep], vals[keep].astype(np.float64)
        room = MAX_RUNS_STORED - acc.n_runs_stored
        if room > 0:
            take = min(room, rs.size)
            acc.run_starts.append(rs[:take].copy())
            acc.run_lens.append(L[:take].copy())
            acc.run_vals.append(vals[:take].copy())
            acc.n_runs_stored += take
            rs, L, vals = rs[take:], L[take:], vals[take:]
        if rs.size:  # beyond the storage cap: exact counters (no row positions)
            for flag, length in zip(self._is_sat(s.alias, vals).tolist(), L.tolist()):
                acc.runs_overflow[(bool(flag), int(length))] += 1

    def _runs(self, acc: _SigAcc, s: SignalInfo, x: np.ndarray, rows: np.ndarray) -> None:
        starts, lengths, values = value_runs(x)
        if starts.size == 0:
            return
        lengths = lengths.astype("int64")
        rstart = rows[starts].astype("int64")
        if acc.carry_len > 0 and not math.isnan(acc.carry_value) and values[0] == acc.carry_value and starts[0] == 0:
            lengths[0] += acc.carry_len  # the run continues from the previous chunk
            rstart[0] = acc.carry_start
        elif acc.carry_len > 0:
            self._commit_runs(acc, s, np.array([acc.carry_start]), np.array([acc.carry_len]), np.array([acc.carry_value]))
        if starts.size > 1:
            self._commit_runs(acc, s, rstart[:-1], lengths[:-1], values[:-1])
        # the last run may continue in the next chunk: committed then (or in results)
        acc.carry_start, acc.carry_len, acc.carry_value = int(rstart[-1]), int(lengths[-1]), float(values[-1])

    def _finalize_runs(self) -> None:
        for alias, acc in self.sig.items():
            if acc.carry_len > 0:
                s = self.by_alias.get(alias)
                if s is not None:
                    self._commit_runs(acc, s, np.array([acc.carry_start]), np.array([acc.carry_len]), np.array([acc.carry_value]))
                acc.carry_len = 0

    def _time(self, t: np.ndarray, rows: np.ndarray) -> None:
        ok = np.isfinite(t)
        if not ok.any():
            return
        tv = t[ok]
        rv = rows[ok]
        if not math.isnan(self.t_last):
            tv = np.concatenate([[self.t_last], tv])
            rv = np.concatenate([[self.t_last_row], rv])
        d = np.diff(tv)
        if d.size == 0:
            self.t_last, self.t_last_row = float(tv[-1]), int(rv[-1])
            return
        self.t_n += int(d.size)
        if self.period_ref is None:
            pos = d[d > 0]
            if pos.size:
                self.period_ref = float(np.median(pos)) * self.stride
        if len(self.t_diff_sample) < 4:
            self.t_diff_sample.append(d[: 50_000])
        neg = d < 0
        if neg.any():
            self.t_neg += int(neg.sum())
            self._add_ranges(self.t_neg_rows, contiguous_runs(neg), rv[1:])
        zero = d == 0
        if zero.any():
            self.t_zero += int(zero.sum())
            self._add_ranges(self.t_zero_rows, contiguous_runs(zero), rv[1:])
        if self.period_ref:
            exp = float(self.period_ref) * self.stride
            gap = d > float(self.q.gap_factor) * exp
            if gap.any():
                self.t_max_gap = max(self.t_max_gap, float(d[gap].max()))
                for i in np.flatnonzero(gap).tolist():
                    if len(self.t_gaps) >= MAX_EVENTS:
                        break
                    self.t_gaps.append((int(rv[i]), int(rv[i + 1]), float(d[i])))
            self.t_irregular += int(((np.abs(d - exp) > 0.2 * exp) & ~gap & ~neg & ~zero).sum())
        self.t_last, self.t_last_row = float(tv[-1]), int(rv[-1])

    def _relations(self, arrays: dict[str, np.ndarray]) -> None:
        for a, b, _r in self.pair_specs:
            if a in arrays and b in arrays:
                x, y = arrays[a], arrays[b]
                ok = np.isfinite(x) & np.isfinite(y)
                if ok.sum() < 3:
                    continue
                xv, yv = x[ok].astype("float64"), y[ok].astype("float64")
                p = self.pairs.setdefault((a, b), _PairAcc())
                p.n += int(xv.size)
                p.sx += float(xv.sum())
                p.sy += float(yv.sum())
                p.sxx += float((xv * xv).sum())
                p.syy += float((yv * yv).sum())
                p.sxy += float((xv * yv).sum())
        for spec in self.derived_specs:
            tgt, of, coef, icpt = spec["signal"], spec["of"], spec["coef"], spec["intercept"]
            if tgt not in arrays or any(o not in arrays for o in of):
                continue
            pred = np.full(arrays[tgt].shape, float(icpt), dtype="float64")
            for o, c in zip(of, coef):
                pred += float(c) * arrays[o]
            resid = arrays[tgt] - pred
            ok = np.isfinite(resid)
            if ok.any():
                p = self.pairs.setdefault((tgt, "derived"), _PairAcc())
                p.resid_sq += float((resid[ok] ** 2).sum())
                p.resid_n += int(ok.sum())

    # ------------------------------------------------------------ results
    def results(self) -> list[CheckResult]:
        self._finalize_runs()
        out: list[CheckResult] = []
        n_rows_eff = self.n_rows * self.stride
        live = [(alias, self.by_alias[alias], acc) for alias, acc in self.sig.items() if alias in self.by_alias and acc.n > 0]
        cands = {alias: self._frozen_candidates(alias, s, acc) for alias, s, acc in live if s.role in CHECKABLE_ROLES}
        # common mode first: the grouped checks get their ids before the per-signal checks that point to them
        frozen_checks, frozen_expl = self._group_frozen(cands)
        out.extend(frozen_checks)
        quant = {alias: self._quant_candidates(acc, cands.get(alias)) for alias, s, acc in live if s.role in CHECKABLE_ROLES}
        quant_checks, quant_rem = self._group_quant(quant)
        out.extend(quant_checks)
        miss_group = self._missing_block_check()
        if miss_group is not None:
            out.append(miss_group)
        for alias, s, acc in live:
            out.extend(self._sig_results(alias, s, acc, cands.get(alias), frozen_expl.get(alias), quant_rem.get(alias), miss_group))
        out.extend(self._batch_results())
        nt = self._timeliness_not_testable()
        if nt is not None:
            out.append(nt)
        found = {c.category for c in out if c.status != "pass"}  # a not-testable category is not a pass either
        for cat in CATEGORIES:
            if cat not in found:
                extra = ""
                if cat == "validity" and self.plaus:
                    n_pr = sum(1 for a in self.sig if a in self.plaus)
                    extra = f"; every reading of the {n_pr} signal(s) with a plausible range lies inside it"
                out.append(self._make(f"{cat}_ok", cat, [], "pass", 0.0, f"Batch {self.batch_id}: no {cat} problems found in {n_rows_eff} rows{extra}", values={"n_rows": n_rows_eff, "n_signals": len(self.sig)}, kind="check_pass", support=n_rows_eff * max(1, len(self.sig))))
        if self.stride > 1:
            ev = self.ws.evidence.add("sampling", f"Batch {self.batch_id} checked on every {self.stride}. row (time budget); run lengths and counts are scaled by {self.stride}", values={"stride": self.stride, "rows_checked": self.n_rows}, computed_by="quality.checks", n_samples=self.n_rows, batch_id=self.batch_id)
            for c in out:
                c.evidence_ids.append(ev.id)
        return out

    def _make(self, check_type: str, category: str, signals: list[str], status: str, severity: float, statement: str, values: Optional[dict[str, Any]] = None, row_start: Optional[int] = None, row_end: Optional[int] = None, evidence: bool = True, kind: Optional[str] = None, support: Optional[int] = None, ratio: Optional[float] = None, exact: bool = False) -> CheckResult:
        values = values or {}
        if self.wording != "sensor":
            values.setdefault("wording", self.wording)
        if "confidence" not in values:
            conf, basis = check_confidence(support if support is not None else self.n_rows * self.stride, ratio, exact)
            if status == "pass":
                basis += "; nothing was found"
            values["confidence"], values["confidence_basis"] = conf, basis
        ev_ids: list[str] = []
        if evidence:
            ev = self.ws.evidence.add(kind or check_type, statement, signals=signals, values={k: v for k, v in values.items() if k not in ("examples", "block_members")}, computed_by=f"quality.checks.{check_type}", n_samples=self.n_rows, batch_id=self.batch_id, group_id=None)
            ev_ids.append(ev.id)
        return CheckResult(check_id=next_check_id(self.ws), check_type=check_type, category=category, signals=signals, batch_id=self.batch_id, status=status, severity=float(min(1.0, max(0.0, severity))), statement=statement, evidence_ids=ev_ids, values=values, row_start=row_start if row_start is not None else self.row_start, row_end=row_end if row_end is not None else self.row_end)

    # ------------------------------------------------------------ frozen runs and common-mode blocks
    def _frozen_candidates(self, alias: str, s: SignalInfo, acc: _SigAcc) -> dict[str, Any]:
        """Stored runs of one signal classified with the signal's own thresholds (as the stuck / saturation / stale
        checks use them)."""
        q = self.q
        hold = s.hold_period
        if s.role == "held_sampled" and not hold and acc.run_count:
            hold = max(1, int(round(acc.run_lengths_sum / acc.run_count)))
        stuck_thr = int(q.stuck_min_run)
        if hold and hold > 1:
            stuck_thr = max(stuck_thr, 4 * hold)
        if s.role == "actuator_like":
            stuck_thr = max(stuck_thr, 20 * int(q.stuck_min_run))  # steps are normal for actuators; only extreme runs count
            sat_thr = max(int(q.stuck_min_run), 4 * int(q.stuck_min_run))
        else:
            sat_thr = stuck_thr
        if acc.run_starts:
            starts = np.concatenate(acc.run_starts)
            lens = np.concatenate(acc.run_lens)
            vals = np.concatenate(acc.run_vals)
        else:
            starts = np.zeros(0, dtype=np.int64)
            lens = np.zeros(0, dtype=np.int64)
            vals = np.zeros(0, dtype=np.float64)
        sat = self._is_sat(alias, vals)
        ov = {"stuck_n": 0, "stuck_rows": 0, "stuck_longest": 0, "sat_n": 0, "sat_rows": 0, "sat_longest": 0}
        for (is_sat, length), cnt in acc.runs_overflow.items():
            key = "sat" if is_sat else "stuck"
            if length >= (sat_thr if is_sat else stuck_thr):
                ov[f"{key}_n"] += cnt
                ov[f"{key}_rows"] += cnt * length
                ov[f"{key}_longest"] = max(ov[f"{key}_longest"], length)
        return {"hold": hold, "stuck_thr": stuck_thr, "sat_thr": sat_thr, "starts": starts, "lens": lens, "vals": vals, "sat": sat,
                "stuck_m": ~sat & (lens >= stuck_thr), "sat_m": sat & (lens >= sat_thr), "overflow": ov}

    @staticmethod
    def _iv(starts: np.ndarray, lens: np.ndarray, m: np.ndarray) -> np.ndarray:
        return np.stack([starts[m], starts[m] + lens[m] - 1], axis=1) if m.any() else np.zeros((0, 2), dtype=np.int64)

    def _group_frozen(self, cands: dict[str, dict[str, Any]]) -> tuple[list[CheckResult], dict[str, dict[str, Any]]]:
        """Rows frozen in >= k signals at once: one frozen_block check per family of blocks."""
        intervals = {a: self._iv(c["starts"], c["lens"], c["stuck_m"] | c["sat_m"]) for a, c in cands.items()}
        blocks = find_common_blocks(intervals, self.k, min_len=max(2, int(self.q.stuck_min_run) // 2))
        if not blocks:
            return [], {}
        clusters = cluster_blocks(blocks)
        n_rows_eff = max(1, self.n_rows * self.stride)
        expl: dict[str, dict[str, Any]] = {}
        out: list[CheckResult] = []
        for cl in clusters:
            members: dict[str, dict[str, Any]] = {}
            for alias, c in cands.items():
                if c["starts"].size == 0:
                    continue
                all_iv = np.stack([c["starts"], c["starts"] + c["lens"] - 1], axis=1)
                m = explained_mask(all_iv, cl["blocks"])  # >= half of the run inside this family's blocks
                if not m.any():
                    continue
                finding = c["stuck_m"] | c["sat_m"]
                if c["hold"] and c["hold"] > 1:
                    finding = finding | (~c["sat"] & (c["lens"] > 2 * c["hold"]))
                fm = m & finding
                e = expl.setdefault(alias, {"mask": np.zeros(c["starts"].size, dtype=bool), "check_ids": []})
                e["mask"] |= m
                if fm.any():
                    li = np.flatnonzero(fm)[int(np.argmax(c["lens"][fm]))]
                    kind = "saturation" if c["sat_m"][li] else ("stuck" if c["stuck_m"][li] else "stale")
                    members[alias] = {"n_runs": int(fm.sum()), "rows": int(c["lens"][fm].sum()), "longest_run": int(c["lens"][li]), "value": float(c["vals"][li]), "as": kind}
            sigs = sorted(set(cl["members"]) | set(members))
            nb, n_sig = len(cl["blocks"]), len(sigs)
            frac = cl["n_rows"] / n_rows_eff
            # rows of the blocks that are exact duplicate rows as well (every signal frozen) are counted once for trust
            also_dup = 0
            if self.dup_iv:
                ba = np.array([b["row_start"] for b in cl["blocks"]], dtype=np.int64)
                bb = np.array([b["row_end"] for b in cl["blocks"]], dtype=np.int64) + 1
                also_dup = int(overlap_with(np.concatenate(self.dup_iv), ba, bb).sum())  # true row numbers already (no stride factor)
            big = max(cl["blocks"], key=lambda b: b["n_rows"])
            listed = ", ".join(sigs[:8]) + (f" and {n_sig - 8} more" if n_sig > 8 else "")
            if nb == 1:
                statement = f"Rows {big['row_start']}-{big['row_end']} of batch {self.batch_id} are frozen in {n_sig} {self.w['signals']} at once ({listed}): {self.w['frozen_block'].format(n=n_sig)}."
            else:
                statement = f"{nb} blocks of rows in batch {self.batch_id} are frozen in {cl['signals_per_block_median']} {self.w['signals']} at once (e.g. rows {big['row_start']}-{big['row_end']}; {cl['n_rows']:,} rows = {frac:.1%} of the batch; {n_sig} {self.w['signals']} in total: {listed}): {self.w['frozen_block'].format(n=n_sig)}."
            status = "fail" if (frac >= 0.005 or cl["longest"] >= 10 * int(self.q.stuck_min_run)) else "warn"
            sev = 0.5 + 0.5 * min(1.0, frac / RECORD_SHARE_REF)
            ordered = cl["blocks"][:MAX_RECORD_EVENTS]
            vals = {"n_blocks": nb, "n_rows": int(cl["n_rows"]), "fraction": round(frac, 6), "n_signals": n_sig, "signals_per_block_median": cl["signals_per_block_median"],
                    "signals_per_block_max": cl["signals_per_block_max"], "k_threshold": self.k, "longest_block": cl["longest"],
                    "rows_also_duplicate": also_dup, "record_fraction": round(max(0.0, cl["n_rows"] - also_dup) / n_rows_eff, 6),
                    "blocks": [[b["row_start"], b["row_end"], len(b["members"])] for b in cl["blocks"][:MAX_BLOCKS_LISTED]],
                    "events": [[b["row_start"], b["row_end"]] for b in ordered], "members": members, "grouped_types": list(GROUPED_TYPES["frozen_block"])}
            chk = self._make("frozen_block", "consistency", sigs, status, sev, statement, vals, row_start=cl["row_start"], row_end=cl["row_end"], support=int(cl["n_rows"]) * max(1, n_sig), ratio=cl["signals_per_block_median"] / max(1, self.k))
            out.append(chk)
            for alias in members:
                expl[alias]["check_ids"].append(chk.check_id)
        return out, expl

    def _quant_candidates(self, acc: _SigAcc, cand: Optional[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
        """Re-quantized window runs of one signal that do not overlap its own frozen runs (a frozen run is not a
        precision change)."""
        if not acc.quant_windows:
            return np.zeros((0, 2), dtype=np.int64), np.zeros(0)
        qa = np.array([(a, b) for a, b, _ in acc.quant_windows], dtype=np.int64)
        qr = np.array([r for _, _, r in acc.quant_windows], dtype=np.float64)
        if cand is not None:
            fz = self._iv(cand["starts"], cand["lens"], cand["stuck_m"] | cand["sat_m"])
            if fz.shape[0]:
                keep = overlap_with(fz, qa[:, 0], qa[:, 1] + 1) == 0
                qa, qr = qa[keep], qr[keep]
        return qa, qr

    def _group_quant(self, quant: dict[str, tuple[np.ndarray, np.ndarray]]) -> tuple[list[CheckResult], dict[str, tuple[np.ndarray, np.ndarray]]]:
        """The same windows re-quantized in >= k signals at once: one quantization_block check per family."""
        rem = dict(quant)
        intervals = {a: qa for a, (qa, _) in quant.items() if qa.shape[0]}
        blocks = find_common_blocks(intervals, self.k, min_len=1)
        if not blocks:
            return [], rem
        n_rows_eff = max(1, self.n_rows * self.stride)
        out: list[CheckResult] = []
        for cl in cluster_blocks(blocks):
            members: dict[str, dict[str, Any]] = {}
            for alias, (qa, qr) in list(rem.items()):
                if not qa.shape[0]:
                    continue
                m = explained_mask(qa, cl["blocks"])
                if m.any():
                    members[alias] = {"n_windows": int(m.sum()), "rows": int((qa[m, 1] - qa[m, 0] + 1).sum()), "uniqueness_ratio": round(float(qr[m].min()), 4)}
                    rem[alias] = (qa[~m], qr[~m])
            sigs = sorted(set(cl["members"]) | set(members))
            nb, n_sig = len(cl["blocks"]), len(sigs)
            frac = cl["n_rows"] / n_rows_eff
            big = max(cl["blocks"], key=lambda b: b["n_rows"])
            listed = ", ".join(sigs[:8]) + (f" and {n_sig - 8} more" if n_sig > 8 else "")
            statement = f"{nb} stretch(es) of rows in batch {self.batch_id} are recorded with a much coarser resolution in {cl['signals_per_block_median']} {self.w['signals']} at once (e.g. rows {big['row_start']}-{big['row_end']}; {cl['n_rows']:,} rows; {n_sig} {self.w['signals']} in total: {listed}): {self.w['quant_block'].format(n=n_sig)}."
            vals = {"n_blocks": nb, "n_rows": int(cl["n_rows"]), "fraction": round(frac, 6), "n_signals": n_sig, "signals_per_block_median": cl["signals_per_block_median"], "k_threshold": self.k,
                    "blocks": [[b["row_start"], b["row_end"], len(b["members"])] for b in cl["blocks"][:MAX_BLOCKS_LISTED]], "events": [[b["row_start"], b["row_end"]] for b in cl["blocks"][:MAX_EVENTS]],
                    "members": members, "grouped_types": list(GROUPED_TYPES["quantization_block"])}
            out.append(self._make("quantization_block", "validity", sigs, "warn", 0.35, statement, vals, row_start=cl["row_start"], row_end=cl["row_end"], support=int(cl["n_rows"]) * max(1, n_sig), ratio=cl["signals_per_block_median"] / max(1, self.k)))
        return out, rem

    def _missing_block_check(self) -> Optional[CheckResult]:
        """Rows missing in >= k signals at once (but not in all of them: those are empty rows)."""
        if not self.mb_rows or not self.mb_member_rows:
            return None
        q = self.q
        n_rows_eff = max(1, self.n_rows * self.stride)
        rows_eff = self.mb_rows * self.stride
        frac = rows_eff / n_rows_eff
        core = sorted(a for a, r in self.mb_member_rows.items() if r >= 0.5 * self.mb_rows)
        if len(core) < 2:
            core = sorted(sorted(self.mb_member_rows, key=lambda a: -self.mb_member_rows[a])[: max(2, self.k_missing)])
        typical = int(round(self.mb_count_sum / max(1, self.mb_rows)))
        ev = _stride_events(self.mb_events, self.stride)
        listed = ", ".join(core[:8]) + (f" and {len(core) - 8} more" if len(core) > 8 else "")
        where = f"e.g. rows {ev[0][0]}-{ev[0][1]}; " if ev else ""
        statement = f"{rows_eff:,} rows of batch {self.batch_id} ({frac:.1%}) are missing in {typical} {self.w['signals']} at once ({where}{len(core)} {self.w['signals']} involved: {listed}): {self.w['missing_block'].format(n=len(core))}."
        status = "fail" if frac >= float(q.missing_warn) else "warn"
        sev = 0.4 + 0.6 * min(1.0, frac / RECORD_SHARE_REF)
        members = {a: {"rows": int(r) * self.stride} for a, r in sorted(self.mb_member_rows.items(), key=lambda kv: -kv[1])[:200]}
        vals = {"n_rows": rows_eff, "fraction": round(frac, 6), "n_signals": len(self.mb_member_rows), "signals_per_row_typical": typical, "signals_per_row_max": self.mb_count_max,
                "k_threshold": self.k_missing, "events": ev, "members": members, "grouped_types": list(GROUPED_TYPES["missing_block"])}
        return self._make("missing_block", "completeness", core, status, sev, statement, vals, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None, support=rows_eff * max(1, typical), ratio=typical / max(1, self.k_missing))

    def _timeliness_not_testable(self) -> Optional[CheckResult]:
        """Without timestamps the timeliness checks cannot run: say so instead of passing trivially."""
        if self.t_n > 0 or self.n_rows == 0:
            return None
        if not self.saw_time:
            why, reason = "the file has no time column", "no_time_column"
        elif self.n_rows < 2:
            why, reason = "the batch has fewer than two rows", "too_few_rows"
        else:
            why, reason = f"the time column holds no usable timestamps in batch {self.batch_id}", "no_timestamps"
        held = any(s.role == "held_sampled" or (s.hold_period or 0) > 1 for s in self.catalog)
        also = " Only the sample-based staleness of slowly updating signals was checked." if held else ""
        statement = f"Timeliness cannot be tested: {why}. Gaps, repeated or out-of-order timestamps and irregular sampling need timestamps, so batch {self.batch_id} is neither passed nor failed on timeliness.{also}"
        vals = {"reason": reason, "n_rows": self.n_rows * self.stride, "confidence": 0.0, "confidence_basis": "not testable: nothing could be measured"}
        return self._make("timeliness_not_testable", "timeliness", [], NOT_TESTABLE, 0.0, statement, vals, kind="timeliness_not_testable")

    # ------------------------------------------------------------ per signal
    def _sig_results(self, alias: str, s: SignalInfo, acc: _SigAcc, cand: Optional[dict[str, Any]] = None, expl: Optional[dict[str, Any]] = None, quant_rem: Optional[tuple[np.ndarray, np.ndarray]] = None, miss_group: Optional[CheckResult] = None) -> list[CheckResult]:
        q = self.q
        out: list[CheckResult] = []
        n = acc.n
        n_eff = n * self.stride
        n_valid = max(0, n - acc.n_missing) * self.stride
        # completeness: the signal's own gaps (rows where >= k signals are missing at once belong to missing_block / empty_rows)
        rate_all = acc.n_missing / n if n else 0.0
        rate = acc.n_missing_own / n if n else 0.0
        grp = ""
        if acc.co_missing and acc.n_missing_own < acc.n_missing:
            where = f" {miss_group.check_id}" if miss_group is not None else ""
            grp = f"; another {acc.co_missing / n:.1%} of the rows are missing in many signals at once (common-mode gap{where} or empty rows)"
        if rate_all >= 1.0 and rate >= q.missing_fail:
            out.append(self._make("dropout", "completeness", [alias], "fail", 1.0, f"{alias} has no values at all in batch {self.batch_id} ({n_eff} rows): {self.w['dropout']}", {"missing_rate": 1.0, "events": _stride_events(acc.miss_rows, self.stride)}, support=n_eff, exact=True))
        elif rate >= q.missing_warn:
            status = "fail" if rate >= q.missing_fail else "warn"
            sev = min(1.0, 0.8 * rate / max(q.missing_fail, 1e-9)) if status == "fail" else min(0.5, 0.5 * rate / max(q.missing_fail, 1e-9))
            ev = _stride_events(acc.miss_rows, self.stride)
            where = f" (rows {ev[0][0]}-{ev[0][1]})" if ev else ""
            vals = {"missing_rate": round(rate, 5), "n_missing": acc.n_missing_own * self.stride, "longest_missing": acc.longest_missing, "events": ev}
            if grp:
                vals["missing_rate_total"] = round(rate_all, 5)
                if miss_group is not None:
                    vals["grouped_in"] = [miss_group.check_id]
            out.append(self._make("missing", "completeness", [alias], status, max(sev, 0.15), f"{alias} is missing in {rate:.1%} of batch {self.batch_id}, longest gap {acc.longest_missing} {self.w['samples']}{where}{grp}", vals, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None, support=n_eff, ratio=rate / max(float(q.missing_warn), 1e-9)))
        if s.role not in CHECKABLE_ROLES:
            return out
        # validity
        if acc.imp_n:
            ev = _stride_events(acc.imp_rows, self.stride)
            out.append(self._make("impossible_value", "validity", [alias], "fail", 0.9, f"{alias} contains {acc.imp_n * self.stride} impossible values (inf or absurd magnitude) in batch {self.batch_id}", {"n": acc.imp_n * self.stride, "events": ev}, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None, support=n_eff, exact=True))
        if acc.pl_n:
            out.append(self._plausibility_result(alias, acc, n, n_valid))
        if acc.oor_n:
            frac = acc.oor_n / n
            ratio = acc.oor_maxz / max(float(q.range_sigma), 1e-9)
            status = "fail" if (frac >= 0.01 or ratio >= 5.0) else "warn"
            sev = 0.4 + 0.4 * min(1.0, frac / 0.05) + 0.2 * min(1.0, (ratio - 1.0) / 10.0)
            ev = _stride_events(acc.oor_rows, self.stride)
            ex = ", ".join(f"row {r}: {fmt_num(v)}" for r, v in acc.oor_examples[:3])
            inside = f", all inside its plausible range (unusual but possible: a process excursion or a glitch)" if alias in self.plaus else ""
            out.append(self._make("out_of_range", "validity", [alias], status, sev, f"{alias} has {acc.oor_n * self.stride} values beyond {q.range_sigma:g} robust sigma of its usual range in batch {self.batch_id} (max {acc.oor_maxz:.0f} sigma; e.g. {ex}){inside}", {"n": acc.oor_n * self.stride, "fraction": round(frac, 6), "max_robust_z": round(acc.oor_maxz, 2), "events": ev, "examples": acc.oor_examples, "points": [list(pt) for pt in acc.oor_points]}, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None, support=n_valid, ratio=ratio))
        if acc.spike_n:
            ev = list(acc.spike_rows)
            frac = acc.spike_n / n
            status = "fail" if acc.spike_n >= 10 else "warn"
            sev = float(min(0.7, 0.25 + 0.03 * acc.spike_n + 0.01 * min(20.0, acc.spike_maxz)))
            first = acc.spike_points[0] if acc.spike_points else None
            eg = f"; e.g. row {first[0]}, {first[2]:.0f} times the local noise, {first[3]}" if first else ""
            out.append(self._make("local_spike", "validity", [alias], status, sev, f"{alias} has {acc.spike_n} isolated {self.w['reading']}(s) in batch {self.batch_id} that jump away from their neighbours and come straight back (up to {acc.spike_maxz:.0f} times the local noise{eg}). Each is either a glitch or a manipulated value; the data alone cannot tell which.", {"n": acc.spike_n, "fraction": round(frac, 8), "max_local_z": round(acc.spike_maxz, 2), "window": int(getattr(q, "spike_window", 7)), "events": ev, "points": [list(p) for p in acc.spike_points]}, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None, support=n_valid, ratio=acc.spike_maxz / max(float(getattr(q, "spike_sigma", 10.0)), 1e-9)))
        if acc.shift_windows:
            ks = sorted({k for _, _, k in acc.shift_windows})
            ev = [(a, b) for a, b, _ in acc.shift_windows]
            out.append(self._make("unit_shift", "validity", [alias], "fail", 0.9, f"{alias} changes scale by x10^{ks[0] if len(ks) == 1 else '/'.join(map(str, ks))} in batch {self.batch_id} (rows {ev[0][0]}-{ev[-1][1]}): {self.w['unit']}", {"k": ks, "events": ev}, row_start=ev[0][0], row_end=ev[-1][1], support=n_valid, ratio=1.0 + 0.5 * len(acc.shift_windows)))
        # runs: stuck / stale / saturation (what is left after the common-mode blocks)
        out.extend(self._run_results(alias, s, acc, cand, expl))
        # quantization / precision change (windows inside the signal's own frozen runs and common-mode windows removed)
        if quant_rem is not None and quant_rem[0].shape[0] and not acc.shift_windows:
            qa, qr = quant_rem
            ev = [(int(a), int(b)) for a, b in qa.tolist()]
            r = float(qr.min())
            span = int((qa[:, 1] - qa[:, 0] + 1).sum())
            out.append(self._make("quantization_change", "validity", [alias], "warn", 0.3, f"{alias} is reported with a much coarser resolution than usual for {span} {self.w['samples']} in batch {self.batch_id} (rows {ev[0][0]}-{ev[0][1]}; distinct-value share drops to {r:.0%} of normal): {self.w['quant']}", {"n_samples": span, "uniqueness_ratio": round(r, 4), "events": ev[:MAX_EVENTS]}, row_start=ev[0][0], row_end=ev[-1][1], support=n_valid, ratio=0.5 / max(r, 1e-6)))
        return out

    def _plausibility_result(self, alias: str, acc: _SigAcc, n: int, n_valid: int) -> CheckResult:
        pr = self.plaus[alias]
        lo, hi, span = float(pr["lo"]), float(pr["hi"]), max(float(pr["span"]), 1e-12)
        k = acc.pl_n * self.stride
        frac = acc.pl_n / max(1, n)
        excess_spans = acc.pl_worst_excess / span
        status = "fail" if (acc.pl_n >= 3 or frac >= 0.001 or excess_spans >= 1.0) else "warn"
        sev = min(1.0, 0.45 + 0.35 * min(1.0, frac / 0.02) + 0.2 * min(1.0, excess_spans))
        ev = _stride_events(acc.pl_rows, self.stride)
        sides = []
        if acc.pl_below:
            sides.append(f"{acc.pl_below * self.stride} below, lowest {fmt_num(acc.pl_lowest)}")
        if acc.pl_above:
            sides.append(f"{acc.pl_above * self.stride} above, highest {fmt_num(acc.pl_highest)}")
        statement = (f"{alias} has {k} {self.w['reading']}(s) outside its plausible range {fmt_num(lo)} to {fmt_num(hi)} in batch {self.batch_id} ({'; '.join(sides)}; worst at row {acc.pl_worst_row}). "
                     f"{range_words(pr)} A{'n' if self.w['reading'][0] in 'aeiou' else ''} {self.w['reading']} outside this range cannot be a real value of {alias}: {self.w['implausible']}.")
        vals = {"n": k, "fraction": round(frac, 6), "n_below": acc.pl_below * self.stride, "n_above": acc.pl_above * self.stride, "lo": lo, "hi": hi, "lo_source": pr["lo_source"], "hi_source": pr["hi_source"],
                "hint": pr.get("hint"), "rules": pr.get("rules") or [], "span": round(span, 6), "worst_excess": round(acc.pl_worst_excess, 6), "worst_row": acc.pl_worst_row,
                "lowest": acc.pl_lowest if math.isfinite(acc.pl_lowest) else None, "highest": acc.pl_highest if math.isfinite(acc.pl_highest) else None,
                "events": ev, "points": [list(p) for p in acc.pl_points]}
        return self._make("plausibility", "validity", [alias], status, sev, statement, vals, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None, support=n_valid, ratio=1.0 + acc.pl_worst_excess / (0.1 * span))

    def _run_results(self, alias: str, s: SignalInfo, acc: _SigAcc, cand: Optional[dict[str, Any]], expl: Optional[dict[str, Any]]) -> list[CheckResult]:
        q = self.q
        out: list[CheckResult] = []
        if cand is None:
            return out
        starts, lens, vals = cand["starts"], cand["lens"], cand["vals"]
        ov = cand["overflow"]
        if starts.size == 0 and not (ov["stuck_n"] or ov["sat_n"]):
            return out
        hold, stuck_thr, sat_thr = cand["hold"], cand["stuck_thr"], cand["sat_thr"]
        ex = expl["mask"] if expl else np.zeros(starts.size, dtype=bool)
        stuck_m = cand["stuck_m"] & ~ex
        sat_m = cand["sat_m"] & ~ex
        n_ex = int(((cand["stuck_m"] | cand["sat_m"]) & ex).sum())
        grp = f"; {n_ex} further frozen stretch(es) of {alias} are part of the common-mode block(s) {', '.join(expl['check_ids'])}" if (expl and n_ex and expl.get("check_ids")) else ""
        n_eff = max(1, acc.n * self.stride)
        n_valid = max(0, acc.n - acc.n_missing) * self.stride
        st = self.stats.get(alias, {})
        gmin, gmax = st.get("min"), st.get("max")
        tol = 1e-9 * max(1.0, abs(gmin or 0.0), abs(gmax or 0.0))
        idx = np.flatnonzero(stuck_m)
        if idx.size or ov["stuck_n"]:
            tot = int(lens[idx].sum()) + ov["stuck_rows"]
            li = int(idx[np.argmax(lens[idx])]) if idx.size else None
            longest = max(int(lens[li]) if li is not None else 0, ov["stuck_longest"])
            l_start = int(starts[li]) if li is not None else (self.row_start or 0)
            l_val = float(vals[li]) if li is not None else math.nan
            frac = tot / n_eff
            # severity grows with the share of the batch that is frozen and with the longest run; a run that
            # covers a small part of a large batch is a local problem (row-scoped in the trust verdict), not a
            # reason to distrust the signal everywhere.
            sev_frac = min(1.0, frac / max(float(q.stuck_fraction_warn), 1e-6))
            sev_len = min(1.0, longest / (10.0 * stuck_thr))
            sev = min(1.0, 0.15 + 0.45 * sev_frac + 0.4 * sev_len)
            status = "fail" if (frac >= 0.02 or (longest >= 10 * stuck_thr and frac >= 0.005)) else "warn"
            ev = [(int(starts[i]), int(starts[i] + lens[i] - 1)) for i in idx[:MAX_EVENTS]]
            role_note = f" (held signal, hold period {hold}; threshold {stuck_thr})" if hold and hold > 1 else ""
            at_zero = math.isfinite(l_val) and abs(l_val) <= tol
            why = self.w["frozen_zero"] if at_zero else self.w["frozen_why"]
            vals_d = {"longest_run": longest, "value": l_val if math.isfinite(l_val) else None, "threshold": stuck_thr, "stuck_fraction": round(frac, 4), "fraction": round(frac, 6), "n_runs": int(idx.size + ov["stuck_n"]), "at_zero": at_zero, "events": ev}
            if grp:
                vals_d["grouped_in"] = list(expl["check_ids"])
            out.append(self._make("stuck", "consistency", [alias], status, sev, f"{alias} is frozen at {fmt_num(l_val)} for {longest} {self.w['samples']} in batch {self.batch_id} (rows {l_start}-{l_start + longest - 1}){role_note}: {why}; {self.w['unreliable']} ({frac:.2%} of the batch){grp}", vals_d, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None, support=n_valid, ratio=longest / max(1, stuck_thr)))
        elif hold and hold > 1:
            stale_m = ~cand["sat"] & (lens > 2 * hold) & ~ex
            sidx = np.flatnonzero(stale_m)
            if sidx.size:
                li = int(sidx[np.argmax(lens[sidx])])
                longest, l_start = int(lens[li]), int(starts[li])
                out.append(self._make("stale", "timeliness", [alias], "warn", 0.4, f"{alias} did not update for {longest} {self.w['samples']} in batch {self.batch_id} although it normally updates every {hold} {self.w['samples']} (rows {l_start}-{l_start + longest - 1})", {"longest_run": longest, "hold_period": hold, "events": [(int(starts[i]), int(starts[i] + lens[i] - 1)) for i in sidx[:MAX_EVENTS]]}, row_start=l_start, row_end=l_start + longest - 1, support=n_valid, ratio=longest / max(1.0, 2.0 * hold)))
        sidx = np.flatnonzero(sat_m)
        if sidx.size or ov["sat_n"]:
            li = int(sidx[np.argmax(lens[sidx])]) if sidx.size else None
            longest = max(int(lens[li]) if li is not None else 0, ov["sat_longest"])
            l_start = int(starts[li]) if li is not None else (self.row_start or 0)
            l_val = float(vals[li]) if li is not None else float(gmax if gmax is not None else math.nan)
            at = "minimum" if gmin is not None and abs(l_val - gmin) <= tol else "maximum"
            status = "fail" if (longest >= 3 * sat_thr and s.role != "actuator_like") else "warn"
            vals_d = {"longest_run": longest, "value": l_val, "at": at, "events": [(int(starts[i]), int(starts[i] + lens[i] - 1)) for i in sidx[:MAX_EVENTS]]}
            if grp:
                vals_d["grouped_in"] = list(expl["check_ids"])
            out.append(self._make("saturation", "consistency", [alias], status, 0.7 if status == "fail" else 0.35, f"{alias} sits at its {at} ({fmt_num(l_val)}) for {longest} {self.w['samples']} in batch {self.batch_id} (rows {l_start}-{l_start + longest - 1}): {self.w['saturation']}", vals_d, row_start=l_start, row_end=l_start + longest - 1, support=n_valid, ratio=longest / max(1, sat_thr)))
        return out

    # ------------------------------------------------------------ batch level
    def _batch_results(self) -> list[CheckResult]:
        q = self.q
        out: list[CheckResult] = []
        n = max(1, self.n_rows)
        n_eff = n * self.stride
        if self.empty_rows:
            frac = self.empty_rows / n
            out.append(self._make("empty_rows", "completeness", [], "fail" if frac >= 0.01 else "warn", 0.6 if frac >= 0.01 else 0.3, f"{self.empty_rows * self.stride} rows in batch {self.batch_id} have no signal values at all ({frac:.2%})", {"n": self.empty_rows * self.stride, "fraction": round(frac, 6), "events": _stride_events(self.empty_ranges, self.stride)}, support=n_eff, exact=True))
        if self.dup_rows:
            frac = self.dup_rows / n
            ev = _stride_events(self.dup_ranges, self.stride)
            statement = (f"{self.dup_rows * self.stride} exact duplicate rows in batch {self.batch_id} ({frac:.2%}; first at rows {ev[0][0]}-{ev[0][1]}): every {self.w['signal']} repeats an earlier row of the batch exactly, "
                         f"{self.w['dup']}") if ev else f"{self.dup_rows} duplicate rows in batch {self.batch_id}"
            out.append(self._make("duplicate_rows", "consistency", [], "fail" if frac >= 0.01 else "warn", min(1.0, 0.3 + 20 * frac), statement, {"n": self.dup_rows * self.stride, "fraction": round(frac, 6), "events": ev}, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None, support=n_eff, exact=True))
        if self.dupkey_rows:
            frac = self.dupkey_rows / n
            ev = _stride_events(self.dupkey_ranges, self.stride)
            also = f" ({self.dupkey_also_dup * self.stride} of them are exact duplicate rows as well)" if self.dupkey_also_dup else ""
            out.append(self._make("duplicate_key", "consistency", [], "warn" if frac < 0.01 else "fail", min(1.0, 0.3 + 10 * frac), f"{self.dupkey_rows * self.stride} rows in batch {self.batch_id} repeat an existing (group, order) key{also}", {"n": self.dupkey_rows * self.stride, "fraction": round(frac, 6), "rows_also_duplicate": self.dupkey_also_dup * self.stride, "record_fraction": round(max(0, self.dupkey_rows - self.dupkey_also_dup) / n, 6), "events": ev}, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None, support=n_eff, exact=True))
        # time
        if self.t_n:
            if self.t_neg:
                ev = self.t_neg_rows
                out.append(self._make("out_of_order", "timeliness", [], "fail", 0.6, f"{self.t_neg} timestamps go backwards in batch {self.batch_id} (first at row {ev[0][0] if ev else '?'})", {"n": self.t_neg, "events": ev}, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None, support=self.t_n, exact=True))
            if self.t_zero and self.stride == 1:
                ev = self.t_zero_rows
                frac = self.t_zero / self.t_n
                out.append(self._make("duplicate_timestamp", "timeliness", [], "warn" if frac < 0.05 else "fail", 0.3 if frac < 0.05 else 0.6, f"{self.t_zero} repeated timestamps in batch {self.batch_id} ({frac:.2%})", {"n": self.t_zero, "fraction": round(frac, 6), "events": ev}, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None, support=self.t_n, exact=True))
            if self.t_gaps and self.period_ref:
                ev = [(a, b) for a, b, _ in self.t_gaps]
                big = self.t_max_gap / (self.period_ref * self.stride)
                out.append(self._make("gap", "timeliness", [], "fail" if big >= 20 else "warn", 0.7 if big >= 20 else 0.4, f"{len(self.t_gaps)} timestamp gap(s) in batch {self.batch_id}; largest {_fmt_dur(self.t_max_gap)} = {big:.0f}x the usual period of {_fmt_dur(self.period_ref)} (rows {ev[0][0]}-{ev[0][1]})", {"n": len(self.t_gaps), "max_gap_s": self.t_max_gap, "period_s": self.period_ref, "events": ev, "gaps": [(a, b, g) for a, b, g in self.t_gaps]}, row_start=ev[0][0], row_end=ev[-1][1], support=self.t_n, ratio=big / max(float(q.gap_factor), 1e-9)))
            if self.period_ref and self.t_irregular / self.t_n >= 0.05:
                frac = self.t_irregular / self.t_n
                out.append(self._make("irregular_sampling", "timeliness", [], "warn", 0.3, f"Sampling is irregular in batch {self.batch_id}: {frac:.1%} of intervals deviate more than 20% from the usual period of {_fmt_dur(self.period_ref)}", {"fraction": round(frac, 5), "period_s": self.period_ref}, support=self.t_n, ratio=frac / 0.05))
        # relations
        for (a, b), p in self.pairs.items():
            if b == "derived":
                if p.resid_n < 10:
                    continue
                rms = math.sqrt(p.resid_sq / p.resid_n)
                scale = (self.stats.get(a, {}).get("scale") or 0.0)
                if scale > 0 and rms > 0.5 * scale:
                    out.append(self._make("relation_break", "consistency", [a], "warn" if rms < 1.0 * scale else "fail", 0.5 if rms < 1.0 * scale else 0.7, f"{a} no longer matches the formula it is derived from in batch {self.batch_id} (residual {fmt_num(rms)} vs usual spread {fmt_num(scale)})", {"rms_residual": rms, "scale": scale}, support=p.resid_n, ratio=rms / (0.5 * scale)))
                continue
            if p.n < 10:
                continue
            vx = p.sxx / p.n - (p.sx / p.n) ** 2
            vy = p.syy / p.n - (p.sy / p.n) ** 2
            if vx <= 0 or vy <= 0:
                continue
            r = (p.sxy / p.n - (p.sx / p.n) * (p.sy / p.n)) / math.sqrt(vx * vy)
            exp_r = next((er for x, y, er in self.pair_specs if (x, y) == (a, b)), 0.98)
            if abs(r) < 0.9 and abs(exp_r) >= 0.98:
                out.append(self._make("relation_break", "consistency", [a, b], "fail" if abs(r) < 0.7 else "warn", 0.7 if abs(r) < 0.7 else 0.5, f"{a} and {b} are normally redundant (r={exp_r:.2f}) but agree only at r={r:.2f} in batch {self.batch_id}: {self.w['relation']}", {"r_batch": round(r, 4), "r_expected": exp_r, "n": p.n}, support=p.n, ratio=0.9 / max(abs(r), 0.05)))
        return out


# ---------------------------------------------------------------- helpers
def _as_float(series: pd.Series) -> np.ndarray:
    if pd.api.types.is_bool_dtype(series):
        return series.to_numpy(dtype="float64", na_value=np.nan)
    if pd.api.types.is_numeric_dtype(series):
        try:
            return series.to_numpy(dtype="float64", na_value=np.nan)
        except (TypeError, ValueError):
            pass
    return pd.to_numeric(series, errors="coerce").to_numpy(dtype="float64", na_value=np.nan)


def _fmt_dur(s: Optional[float]) -> str:
    if s is None or not math.isfinite(s):
        return "?"
    if s < 120:
        return f"{s:.0f} s"
    if s < 7200:
        return f"{s / 60:.1f} min"
    return f"{s / 3600:.1f} h"


def _redundant_pairs(relations: Any, catalog: list[SignalInfo], stats: dict[str, dict[str, Any]]) -> list[tuple[str, str, float]]:
    """(a, b, expected_r) for pairs a later batch must keep agreeing on. Tolerant to relation formats."""
    aliases = {s.alias for s in catalog}
    pairs: dict[tuple[str, str], float] = {}

    def add(a: Any, b: Any, r: Any) -> None:
        if a in aliases and b in aliases and a != b:
            try:
                rr = float(r)
            except (TypeError, ValueError):
                return
            if abs(rr) >= 0.98:
                pairs[tuple(sorted((a, b)))] = rr  # type: ignore[index]

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            kind = str(obj.get("kind") or obj.get("type") or obj.get("relation") or "").lower()
            a = obj.get("a") or obj.get("signal_a") or obj.get("from") or obj.get("x")
            b = obj.get("b") or obj.get("signal_b") or obj.get("to") or obj.get("y")
            r = obj.get("r") if obj.get("r") is not None else obj.get("corr", obj.get("correlation"))
            if a and b and (r is not None or "redund" in kind or "derived" in kind):
                add(a, b, r if r is not None else 1.0)
            for v in obj.values():
                if isinstance(v, (list, dict)):
                    walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(relations)
    for s in catalog:
        if s.role == "derived_redundant" or s.related:
            for rel in s.related:
                if isinstance(rel, dict):
                    add(s.alias, rel.get("signal"), rel.get("r", 0.0))
    return [(a, b, r) for (a, b), r in pairs.items()]


def _derived_specs(relations: Any, catalog: list[SignalInfo]) -> list[dict[str, Any]]:
    aliases = {s.alias for s in catalog}
    out: list[dict[str, Any]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            tgt = obj.get("signal") or obj.get("target")
            of = obj.get("of") or obj.get("inputs") or obj.get("sources")
            coef = obj.get("coef") or obj.get("coefficients") or obj.get("weights")
            if tgt in aliases and isinstance(of, list) and isinstance(coef, list) and len(of) == len(coef) and all(o in aliases for o in of):
                out.append({"signal": tgt, "of": list(of), "coef": [float(c) for c in coef], "intercept": float(obj.get("intercept", 0.0) or 0.0)})
            for v in obj.values():
                if isinstance(v, (list, dict)):
                    walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(relations)
    return out


def _missing_expected(catalog: list[SignalInfo]) -> float:
    """Signals expected to be missing in one row by chance (sum of the per-signal missing rates of the fingerprints):
    the common-mode missing threshold is raised above what independent gaps would produce."""
    tot = 0.0
    for s in numeric_signals(catalog):
        r = _fp_get(s.fingerprint or {}, "missing_rate")
        if r is not None and 0.0 < r < 1.0:
            tot += r
    return tot


def _quality_context(ws: Any, settings: Any) -> dict[str, Any]:
    catalog = load_catalog(ws)
    stats = global_stats(ws, settings, catalog) if ws.exists("dataset") else {s.alias: {**_no_stats(), **_fp_stats(s)} for s in catalog}
    schema = None
    try:
        schema = ws.schema()
    except Exception:
        schema = None
    relations = ws.read_json("relations", None)
    rules: list[Any] = []
    try:
        rules = ws.rules()
    except Exception:
        rules = []
    wording = data_wording(ws)
    plaus = plausible_ranges(numeric_signals(catalog), stats, rules, wording=wording)
    return {"catalog": catalog, "stats": stats, "schema": schema, "relations": relations, "plausible": plaus, "missing_expected": _missing_expected(catalog), "rules": rules, "wording": wording}


def _no_stats() -> dict[str, Any]:
    return {"median": None, "mad": None, "min": None, "max": None, "q01": None, "q99": None, "scale": 0.0, "nonneg": False}


def _fp_stats(s: SignalInfo) -> dict[str, Any]:
    from ._common import _stats_from_fingerprint, robust_scale

    st = _stats_from_fingerprint(s.fingerprint)
    st["scale"] = robust_scale(st)
    st["nonneg"] = bool(st.get("min") is not None and st["min"] >= 0.0)
    return st


def _stats_from_frame(df: pd.DataFrame, catalog: list[SignalInfo], stats: dict[str, dict[str, Any]]) -> None:
    """Stream path without a dataset: fill missing global stats from the batch itself (recorded as such)."""
    colmap = resolve_columns(df.columns, catalog)
    from ._common import robust_scale

    for s in numeric_signals(catalog):
        st = stats.setdefault(s.alias, _no_stats())
        if st.get("median") is not None and st.get("scale", 0) > 0:
            continue
        if s.alias not in colmap:
            continue
        x = _as_float(df[colmap[s.alias]])
        x = x[np.isfinite(x)]
        if x.size < 10:
            continue
        med = float(np.median(x))
        st.update({"median": med, "mad": float(np.median(np.abs(x - med))), "min": float(x.min()), "max": float(x.max()), "q01": float(np.percentile(x, 1)), "q99": float(np.percentile(x, 99)), "self_reference": True})
        st["scale"] = robust_scale(st)
        st["nonneg"] = bool(st["min"] >= 0.0)


# ---------------------------------------------------------------- public API
def run_checks_for_batch(ws: Any, settings: Any, batch: dict[str, Any], qctx: dict[str, Any], deadline: Optional[float] = None, stride: int = 1) -> list[CheckResult]:
    """Baseline checks for one batch of dataset.parquet, chunked. Returns the CheckResults (not yet persisted)."""
    catalog: list[SignalInfo] = qctx["catalog"]
    schema = qctx.get("schema")
    cols = [s.column for s in numeric_signals(catalog)]
    keep64: set[str] = set()
    tcol = getattr(schema, "time_column", None) if schema else None
    if tcol:
        cols.append(tcol)
        keep64.add(tcol)
    oc = getattr(schema, "order_column", None) if schema else None
    if oc:
        cols.append(oc)
    fast = bool(deadline is not None and time.time() > deadline)
    acc = BatchAccumulator(ws, settings, batch["batch_id"], catalog, qctx["stats"], schema=schema, relations=qctx.get("relations"), stride=stride, fast=fast, plausible=qctx.get("plausible"), missing_expected=qctx.get("missing_expected", 0.0), wording=qctx.get("wording", "sensor"))
    n_cols = max(1, len(cols))
    step = min(500_000, chunk_rows(n_cols, bytes_per_value=4))
    rs, re_ = int(batch["row_start"]), int(batch["row_end"])  # half-open [rs, re_)
    start = rs
    while start < re_:
        end = min(re_, start + step)
        df = load_batch_frame(ws, batch, columns=cols, row_start=start, row_end=end, keep_float64=keep64, stride=stride)
        acc.update(df)
        del df
        start = end
    return acc.results()


def check_batch(ws: Any, settings: Any, batch_df: pd.DataFrame, batch_id: str) -> tuple[list[CheckResult], TrustVerdict]:
    """Stream path: checks + trust verdict for an in-memory batch (alias or original column names)."""
    from .trust import trust_verdict

    qctx = _quality_context(ws, settings)
    catalog = qctx["catalog"]
    if not catalog:  # nothing known yet: derive aliases from the frame itself
        i = 0
        for c in batch_df.columns:
            if c in (ROW_COL, GROUP_COL):
                continue
            if pd.api.types.is_numeric_dtype(batch_df[c]):
                i += 1
                catalog.append(SignalInfo(alias=c if str(c).startswith("S") and str(c)[1:].isdigit() else f"S{i:02d}", column=str(c), source_column=str(c)))
        qctx["stats"] = {s.alias: _no_stats() for s in catalog}
    _stats_from_frame(batch_df, catalog, qctx["stats"])
    qctx["plausible"] = plausible_ranges(numeric_signals(catalog), qctx["stats"], qctx.get("rules") or [], wording=qctx.get("wording", "sensor"))
    df = batch_df
    if ROW_COL not in df.columns:
        df = df.copy()
        df[ROW_COL] = np.arange(len(df), dtype="int64")
    acc = BatchAccumulator(ws, settings, batch_id, catalog, qctx["stats"], schema=qctx.get("schema"), relations=qctx.get("relations"), plausible=qctx["plausible"], missing_expected=qctx.get("missing_expected", 0.0), wording=qctx.get("wording", "sensor"))
    acc.update(df)
    checks = acc.results()
    for c in checks:
        ws.append_jsonl("checks", c)
    verdict = trust_verdict(ws, settings, batch_id, checks, n_signals=len(numeric_signals(catalog)) or None, n_rows=len(df))
    ws.log.record("system:quality", "check", "batch", batch_id, {"n_checks": len(checks), "n_fail": sum(c.status == "fail" for c in checks), "n_warn": sum(c.status == "warn" for c in checks), "trusted": verdict.trusted, "trust_score": verdict.trust_score}, [e for c in checks for e in c.evidence_ids][:50])
    return checks, verdict
