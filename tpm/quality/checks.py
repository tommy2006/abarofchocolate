"""Baseline data-quality checks per batch (agent B).

Data-quality problems are flagged separately from process faults: a dead sensor, a unit change, a
duplicated block or a timestamp gap is a *data* problem, reported as a CheckResult with a category
(completeness | validity | consistency | timeliness) and consumed by the trust verdict, never as a fault.

The checks run over a batch in row chunks (DuckDB -> pandas float32) with carried state so that a frozen
run or a timestamp gap crossing a chunk boundary is still seen. Everything is vectorised numpy.

Check types produced (check_type -> category):
    missing, dropout, empty_rows                     completeness
    out_of_range, impossible_value, unit_shift,
    quantization_change                              validity
    duplicate_rows, duplicate_key, stuck, saturation,
    sign_violation, relation_break                   consistency
    gap, out_of_order, duplicate_timestamp,
    irregular_sampling, stale                        timeliness
    <category>_ok                                    one per category and batch when nothing was found
"""
from __future__ import annotations

import math
import time
import warnings
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..contracts import CheckResult, TrustVerdict
from ..memory import chunk_rows
from .batches import load_batch_frame
from ._common import (
    GROUP_COL,
    ROW_COL,
    SignalInfo,
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
)

CATEGORY_OF = {
    "missing": "completeness", "dropout": "completeness", "empty_rows": "completeness",
    "out_of_range": "validity", "impossible_value": "validity", "unit_shift": "validity", "quantization_change": "validity", "local_spike": "validity",
    "duplicate_rows": "consistency", "duplicate_key": "consistency", "stuck": "consistency", "saturation": "consistency",
    "sign_violation": "consistency", "relation_break": "consistency",
    "gap": "timeliness", "out_of_order": "timeliness", "duplicate_timestamp": "timeliness", "irregular_sampling": "timeliness", "stale": "timeliness",
}
CATEGORIES = ["completeness", "validity", "consistency", "timeliness"]
CHECKABLE_ROLES = {"continuous_measured", "actuator_like", "held_sampled", "derived_redundant", "unknown"}
MAX_EVENTS = 12  # row ranges kept per check (aggregates only)
MAX_POINTS = 200  # per-run deviations kept per check for the suspicious-rows list (row ids + z, never raw values)
SPIKE_ROLES = {"continuous_measured", "derived_redundant", "unknown"}
ABSURD_ABS = 1e30


@dataclass
class _SigAcc:
    n: int = 0
    n_missing: int = 0
    miss_rows: list[tuple[int, int]] = field(default_factory=list)
    longest_missing: int = 0
    oor_n: int = 0
    oor_maxz: float = 0.0
    oor_rows: list[tuple[int, int]] = field(default_factory=list)
    oor_examples: list[tuple[int, float]] = field(default_factory=list)
    oor_points: list[tuple[int, int, float]] = field(default_factory=list)  # (row_start, row_end, max robust z) per out-of-range run
    spike_n: int = 0
    spike_maxz: float = 0.0
    spike_rows: list[tuple[int, int]] = field(default_factory=list)
    spike_points: list[tuple[int, int, float, str]] = field(default_factory=list)  # (row_start, row_end, local z, direction)
    imp_n: int = 0
    imp_rows: list[tuple[int, int]] = field(default_factory=list)
    shift_windows: list[tuple[int, int, int]] = field(default_factory=list)  # (row_start, row_end, k)
    quant_windows: list[tuple[int, int, float]] = field(default_factory=list)
    n_quant_windows: int = 0
    runs: list[tuple[int, int, float]] = field(default_factory=list)  # (row_start, length, value) of runs >= min run considered
    run_lengths_sum: int = 0
    run_count: int = 0
    stuck_samples: int = 0
    neg_n: int = 0
    neg_rows: list[tuple[int, int]] = field(default_factory=list)
    neg_min: float = 0.0
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


class BatchAccumulator:
    """Accumulates per-signal statistics over the chunks of one batch and turns them into CheckResults."""

    def __init__(self, ws: Any, settings: Any, batch_id: str, catalog: list[SignalInfo], stats: dict[str, dict[str, Any]], schema: Any = None, relations: Any = None, stride: int = 1, fast: bool = False):
        self.ws = ws
        self.settings = settings
        self.q = settings.quality
        self.batch_id = batch_id
        self.catalog = catalog
        self.stats = stats
        self.schema = schema
        self.stride = max(1, int(stride))
        self.fast = fast
        self.sig: dict[str, _SigAcc] = {}
        self.n_rows = 0
        self.row_start: Optional[int] = None
        self.row_end: Optional[int] = None
        self.empty_rows = 0
        self.empty_ranges: list[tuple[int, int]] = []
        self.dup_rows = 0
        self.dup_ranges: list[tuple[int, int]] = []
        self.dupkey_rows = 0
        self.dupkey_ranges: list[tuple[int, int]] = []
        self.group_ids: set[str] = set()
        # time
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
        for s in sigs:
            x = _as_float(df[colmap[s.alias]])
            arrays[s.alias] = x
            nan = np.isnan(x)
            all_nan &= nan
            acc = self.sig.setdefault(s.alias, _SigAcc())
            acc.n += n
            self._completeness(acc, x, nan, rows)
            if s.role in CHECKABLE_ROLES:
                st = self.stats.get(s.alias, {})
                self._validity(acc, s, st, x, nan, rows)
                self._runs(acc, s, st, x, rows)
                self._sign(acc, st, x, rows)
        # rows entirely empty
        if sigs:
            e = int(all_nan.sum())
            if e:
                self.empty_rows += e
                self._add_ranges(self.empty_ranges, contiguous_runs(all_nan), rows)
        # duplicates (full row over signal columns + time)
        tcol = time_column_in(df.columns, self.schema)
        subset = [colmap[s.alias] for s in sigs] + ([tcol] if tcol else [])
        if subset and n > 1:
            dup = df.duplicated(subset=subset, keep="first").to_numpy()
            d = int(dup.sum())
            if d:
                self.dup_rows += d
                self._add_ranges(self.dup_ranges, contiguous_runs(dup), rows)
        key_cols = self._key_columns(df)
        if key_cols and n > 1:
            dk = df.duplicated(subset=key_cols, keep="first").to_numpy()
            d = int(dk.sum())
            if d:
                self.dupkey_rows += d
                self._add_ranges(self.dupkey_ranges, contiguous_runs(dk), rows)
        # time
        if tcol:
            self._time(to_seconds(df[tcol], None if pd.api.types.is_datetime64_any_dtype(df[tcol]) else getattr(self.schema, "sample_period_seconds", None)), rows)
        # relations
        if not self.fast:
            self._relations(arrays)

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
    def _add_ranges(dst: list[tuple[int, int]], runs: list[tuple[int, int]], rows: np.ndarray) -> None:
        for a, b in runs:
            if len(dst) >= MAX_EVENTS:
                break
            dst.append((int(rows[a]), int(rows[b])))

    def _completeness(self, acc: _SigAcc, x: np.ndarray, nan: np.ndarray, rows: np.ndarray) -> None:
        m = int(nan.sum())
        if not m:
            return
        acc.n_missing += m
        runs = contiguous_runs(nan)
        if runs:
            acc.longest_missing = max(acc.longest_missing, max(b - a + 1 for a, b in runs) * self.stride)
            self._add_ranges(acc.miss_rows, runs, rows)

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
        if med is None or scale <= 0 or s.role == "constant":
            return
        z = np.abs(x - med) / scale
        oor = (z > float(self.q.range_sigma)) & ok & ~imp
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
        # unit shift: window median vs global median ~ 10^k (windows of 25 samples; shorter shifts are caught as out_of_range)
        n = x.size
        w = 25 if n >= 50 else max(5, n // 2)
        if n >= w and abs(med) > 3.0 * scale:
            nwin = n // w
            xw = x[: nwin * w].reshape(nwin, w)
            with np.errstate(all="ignore"), warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                wmed = np.nanmedian(xw, axis=1)
                ratio = np.abs(wmed / med)
                lg = np.log10(np.where(ratio > 0, ratio, np.nan))
            k = np.round(lg)
            hit = np.isfinite(lg) & (np.abs(k) >= 1) & (np.abs(lg - k) < 0.2)
            if hit.any():
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
                        hit = okw & (uniq < 0.5 * ref) & (distinct >= 2)
                        for a, b in contiguous_runs(hit):
                            if len(acc.quant_windows) >= MAX_EVENTS:
                                break
                            acc.quant_windows.append((int(rows[a * w]), int(rows[min(n - 1, (b + 1) * w - 1)]), float(np.nanmean(uniq[a : b + 1]) / ref)))

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

    def _runs(self, acc: _SigAcc, s: SignalInfo, st: dict[str, Any], x: np.ndarray, rows: np.ndarray) -> None:
        starts, lengths, values = value_runs(x)
        if starts.size == 0:
            return
        lengths = lengths.astype("int64")
        # carry from the previous chunk
        if acc.carry_len > 0 and not math.isnan(acc.carry_value) and values[0] == acc.carry_value and starts[0] == 0:
            lengths[0] += acc.carry_len
            first_row = acc.carry_start
        else:
            first_row = int(rows[starts[0]])
        acc.run_count += int(starts.size)
        acc.run_lengths_sum += int(lengths.sum())
        min_keep = max(2, int(self.q.stuck_min_run) // 3, (s.hold_period or 0) * 2)
        keep = np.flatnonzero(lengths * self.stride >= min_keep)
        for i in keep.tolist():
            if len(acc.runs) >= 4 * MAX_EVENTS:
                break
            rs = first_row if i == 0 else int(rows[starts[i]])
            acc.runs.append((rs, int(lengths[i]) * self.stride, float(values[i])))
        # carry state: the last run may continue in the next chunk
        last = starts.size - 1
        acc.carry_value = float(values[last])
        acc.carry_len = int(lengths[last])
        acc.carry_start = first_row if last == 0 else int(rows[starts[last]])

    def _sign(self, acc: _SigAcc, st: dict[str, Any], x: np.ndarray, rows: np.ndarray) -> None:
        if not st.get("nonneg"):
            return
        neg = x < 0
        if neg.any():
            acc.neg_n += int(neg.sum())
            acc.neg_min = min(acc.neg_min, float(np.nanmin(x)))
            self._add_ranges(acc.neg_rows, contiguous_runs(neg), rows)

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
        out: list[CheckResult] = []
        n_rows_eff = self.n_rows * self.stride
        found: set[str] = set()
        for alias, acc in self.sig.items():
            s = next((c for c in self.catalog if c.alias == alias), None)
            if s is None or acc.n == 0:
                continue
            out.extend(self._sig_results(alias, s, acc))
        out.extend(self._batch_results())
        found = {c.category for c in out if c.status != "pass"}
        for cat in CATEGORIES:
            if cat not in found:
                out.append(self._make(f"{cat}_ok", cat, [], "pass", 0.0, f"Batch {self.batch_id}: no {cat} problems found in {n_rows_eff} rows", values={"n_rows": n_rows_eff, "n_signals": len(self.sig)}, kind="check_pass"))
        if self.stride > 1:
            ev = self.ws.evidence.add("sampling", f"Batch {self.batch_id} checked on every {self.stride}. row (time budget); run lengths and counts are scaled by {self.stride}", values={"stride": self.stride, "rows_checked": self.n_rows}, computed_by="quality.checks", n_samples=self.n_rows, batch_id=self.batch_id)
            for c in out:
                c.evidence_ids.append(ev.id)
        return out

    def _make(self, check_type: str, category: str, signals: list[str], status: str, severity: float, statement: str, values: Optional[dict[str, Any]] = None, row_start: Optional[int] = None, row_end: Optional[int] = None, evidence: bool = True, kind: Optional[str] = None) -> CheckResult:
        values = values or {}
        ev_ids: list[str] = []
        if evidence:
            ev = self.ws.evidence.add(kind or check_type, statement, signals=signals, values={k: v for k, v in values.items() if k != "examples"}, computed_by=f"quality.checks.{check_type}", n_samples=self.n_rows, batch_id=self.batch_id, group_id=None)
            ev_ids.append(ev.id)
        return CheckResult(check_id=next_check_id(self.ws), check_type=check_type, category=category, signals=signals, batch_id=self.batch_id, status=status, severity=float(min(1.0, max(0.0, severity))), statement=statement, evidence_ids=ev_ids, values=values, row_start=row_start if row_start is not None else self.row_start, row_end=row_end if row_end is not None else self.row_end)

    def _sig_results(self, alias: str, s: SignalInfo, acc: _SigAcc) -> list[CheckResult]:
        q = self.q
        out: list[CheckResult] = []
        n = acc.n
        # completeness
        rate = acc.n_missing / n if n else 0.0
        if rate >= 1.0:
            out.append(self._make("dropout", "completeness", [alias], "fail", 1.0, f"{alias} has no values at all in batch {self.batch_id} ({n * self.stride} rows): whole-signal dropout", {"missing_rate": 1.0, "events": _stride_events(acc.miss_rows, self.stride)}))
        elif rate >= q.missing_warn:
            status = "fail" if rate >= q.missing_fail else "warn"
            sev = min(1.0, 0.8 * rate / max(q.missing_fail, 1e-9)) if status == "fail" else min(0.5, 0.5 * rate / max(q.missing_fail, 1e-9))
            ev = _stride_events(acc.miss_rows, self.stride)
            where = f" (rows {ev[0][0]}-{ev[0][1]})" if ev else ""
            out.append(self._make("missing", "completeness", [alias], status, max(sev, 0.15), f"{alias} is missing in {rate:.1%} of batch {self.batch_id}, longest gap {acc.longest_missing} samples{where}", {"missing_rate": round(rate, 5), "n_missing": acc.n_missing * self.stride, "longest_missing": acc.longest_missing, "events": ev}, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None))
        if s.role not in CHECKABLE_ROLES:
            return out
        # validity
        if acc.imp_n:
            ev = _stride_events(acc.imp_rows, self.stride)
            out.append(self._make("impossible_value", "validity", [alias], "fail", 0.9, f"{alias} contains {acc.imp_n * self.stride} impossible values (inf or absurd magnitude) in batch {self.batch_id}", {"n": acc.imp_n * self.stride, "events": ev}, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None))
        if acc.oor_n:
            frac = acc.oor_n / n
            ratio = acc.oor_maxz / max(float(q.range_sigma), 1e-9)
            status = "fail" if (frac >= 0.01 or ratio >= 5.0) else "warn"
            sev = 0.4 + 0.4 * min(1.0, frac / 0.05) + 0.2 * min(1.0, (ratio - 1.0) / 10.0)
            ev = _stride_events(acc.oor_rows, self.stride)
            ex = ", ".join(f"row {r}: {fmt_num(v)}" for r, v in acc.oor_examples[:3])
            out.append(self._make("out_of_range", "validity", [alias], status, sev, f"{alias} has {acc.oor_n * self.stride} values beyond {q.range_sigma:g} robust sigma of its usual range in batch {self.batch_id} (max {acc.oor_maxz:.0f} sigma; e.g. {ex})", {"n": acc.oor_n * self.stride, "fraction": round(frac, 6), "max_robust_z": round(acc.oor_maxz, 2), "events": ev, "examples": acc.oor_examples, "points": [list(pt) for pt in acc.oor_points]}, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None))
        if acc.spike_n:
            ev = list(acc.spike_rows)
            frac = acc.spike_n / n
            status = "fail" if acc.spike_n >= 10 else "warn"
            sev = float(min(0.7, 0.25 + 0.03 * acc.spike_n + 0.01 * min(20.0, acc.spike_maxz)))
            first = acc.spike_points[0] if acc.spike_points else None
            eg = f"; e.g. row {first[0]}, {first[2]:.0f} times the local noise, {first[3]}" if first else ""
            out.append(self._make("local_spike", "validity", [alias], status, sev, f"{alias} has {acc.spike_n} isolated reading(s) in batch {self.batch_id} that jump away from their neighbours and come straight back (up to {acc.spike_maxz:.0f} times the local noise{eg}). Each is either a glitch or a manipulated value; the data alone cannot tell which.", {"n": acc.spike_n, "fraction": round(frac, 8), "max_local_z": round(acc.spike_maxz, 2), "window": int(getattr(q, "spike_window", 7)), "events": ev, "points": [list(p) for p in acc.spike_points]}, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None))
        if acc.shift_windows:
            ks = sorted({k for _, _, k in acc.shift_windows})
            ev = [(a, b) for a, b, _ in acc.shift_windows]
            out.append(self._make("unit_shift", "validity", [alias], "fail", 0.9, f"{alias} changes scale by x10^{ks[0] if len(ks) == 1 else '/'.join(map(str, ks))} in batch {self.batch_id} (rows {ev[0][0]}-{ev[-1][1]}): likely a unit or decimal-point change, not a process event", {"k": ks, "events": ev}, row_start=ev[0][0], row_end=ev[-1][1]))
        # runs: stuck / stale / saturation (first: quantization windows overlapping a frozen run are not a precision change)
        run_checks = self._run_results(alias, s, acc)
        out.extend(run_checks)
        frozen = [tuple(e) for c in run_checks for e in (c.values.get("events") or [])]
        quant = [(a, b, r) for a, b, r in acc.quant_windows if not any(a <= fe and b >= fs for fs, fe in frozen)]
        if quant and not acc.shift_windows:
            ev = [(a, b) for a, b, _ in quant]
            r = min(q3 for _, _, q3 in quant)
            span = sum(b - a + 1 for a, b in ev)
            out.append(self._make("quantization_change", "validity", [alias], "warn", 0.3, f"{alias} is reported with a much coarser resolution than usual for {span} samples in batch {self.batch_id} (rows {ev[0][0]}-{ev[0][1]}; distinct-value share drops to {r:.0%} of normal): a precision or logger change, not a process change", {"n_samples": span, "uniqueness_ratio": round(r, 4), "events": ev}, row_start=ev[0][0], row_end=ev[-1][1]))
        # sign
        if acc.neg_n:
            ev = _stride_events(acc.neg_rows, self.stride)
            status = "fail" if acc.neg_n >= 3 else "warn"
            out.append(self._make("sign_violation", "consistency", [alias], status, 0.7 if status == "fail" else 0.4, f"{alias} was never negative in the whole dataset but has {acc.neg_n * self.stride} negative values (min {fmt_num(acc.neg_min)}) in batch {self.batch_id}", {"n": acc.neg_n * self.stride, "min": acc.neg_min, "events": ev}, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None))
        return out

    def _run_results(self, alias: str, s: SignalInfo, acc: _SigAcc) -> list[CheckResult]:
        q = self.q
        out: list[CheckResult] = []
        if not acc.runs:
            return out
        st = self.stats.get(alias, {})
        hold = s.hold_period
        if s.role == "held_sampled" and not hold and acc.run_count:
            hold = max(1, int(round(acc.run_lengths_sum / acc.run_count)))
        stuck_thr = int(q.stuck_min_run)
        if hold and hold > 1:
            stuck_thr = max(stuck_thr, 4 * hold)
        gmin, gmax = st.get("min"), st.get("max")
        tol = 1e-9 * max(1.0, abs(gmin or 0.0), abs(gmax or 0.0))
        sat_runs = [r for r in acc.runs if (gmin is not None and abs(r[2] - gmin) <= tol) or (gmax is not None and abs(r[2] - gmax) <= tol)]
        sat_keys = {r[0] for r in sat_runs}
        if s.role == "actuator_like":
            stuck_thr = max(stuck_thr, 20 * int(q.stuck_min_run))  # steps are normal for actuators; only extreme runs count
        plain = [r for r in acc.runs if r[0] not in sat_keys]
        stuck = [r for r in plain if r[1] >= stuck_thr]
        if stuck:
            longest = max(stuck, key=lambda r: r[1])
            frac = sum(r[1] for r in stuck) / max(1, acc.n * self.stride)
            # severity grows with the share of the batch that is frozen and with the longest run; a run that
            # covers a small part of a large batch is a local problem (row-scoped in the trust verdict), not a
            # reason to distrust the signal everywhere.
            sev_frac = min(1.0, frac / max(float(q.stuck_fraction_warn), 1e-6))
            sev_len = min(1.0, longest[1] / (10.0 * stuck_thr))
            sev = min(1.0, 0.15 + 0.45 * sev_frac + 0.4 * sev_len)
            status = "fail" if (frac >= 0.02 or (longest[1] >= 10 * stuck_thr and frac >= 0.005)) else "warn"
            ev = [(r[0], r[0] + r[1] - 1) for r in stuck[:MAX_EVENTS]]
            role_note = f" (held signal, hold period {hold}; threshold {stuck_thr})" if hold and hold > 1 else ""
            at_zero = abs(float(longest[2])) <= tol
            why = "a closed valve or zero flow held exactly at 0, or a dead sensor" if at_zero else "looks like a dead or stale sensor, not a process change"
            out.append(self._make("stuck", "consistency", [alias], status, sev, f"{alias} is frozen at {fmt_num(longest[2])} for {longest[1]} samples in batch {self.batch_id} (rows {longest[0]}-{longest[0] + longest[1] - 1}){role_note}: {why}; the signal is treated as unreliable in those rows ({frac:.2%} of the batch)", {"longest_run": longest[1], "value": longest[2], "threshold": stuck_thr, "stuck_fraction": round(frac, 4), "fraction": round(frac, 6), "n_runs": len(stuck), "at_zero": at_zero, "events": ev}, row_start=ev[0][0], row_end=ev[-1][1]))
        elif hold and hold > 1:
            stale = [r for r in plain if r[1] > 2 * hold]
            if stale:
                longest = max(stale, key=lambda r: r[1])
                out.append(self._make("stale", "timeliness", [alias], "warn", 0.4, f"{alias} did not update for {longest[1]} samples in batch {self.batch_id} although it normally updates every {hold} samples (rows {longest[0]}-{longest[0] + longest[1] - 1})", {"longest_run": longest[1], "hold_period": hold, "events": [(r[0], r[0] + r[1] - 1) for r in stale[:MAX_EVENTS]]}, row_start=longest[0], row_end=longest[0] + longest[1] - 1))
        sat_thr = stuck_thr if s.role != "actuator_like" else max(int(q.stuck_min_run), 4 * int(q.stuck_min_run))
        sat = [r for r in sat_runs if r[1] >= sat_thr]
        if sat:
            longest = max(sat, key=lambda r: r[1])
            at = "minimum" if gmin is not None and abs(longest[2] - gmin) <= tol else "maximum"
            status = "fail" if (longest[1] >= 3 * sat_thr and s.role != "actuator_like") else "warn"
            out.append(self._make("saturation", "consistency", [alias], status, 0.7 if status == "fail" else 0.35, f"{alias} sits at its {at} ({fmt_num(longest[2])}) for {longest[1]} samples in batch {self.batch_id} (rows {longest[0]}-{longest[0] + longest[1] - 1}): possible saturation or clipping", {"longest_run": longest[1], "value": longest[2], "at": at, "events": [(r[0], r[0] + r[1] - 1) for r in sat[:MAX_EVENTS]]}, row_start=longest[0], row_end=longest[0] + longest[1] - 1))
        return out

    def _batch_results(self) -> list[CheckResult]:
        q = self.q
        out: list[CheckResult] = []
        n = max(1, self.n_rows)
        if self.empty_rows:
            frac = self.empty_rows / n
            out.append(self._make("empty_rows", "completeness", [], "fail" if frac >= 0.01 else "warn", 0.6 if frac >= 0.01 else 0.3, f"{self.empty_rows * self.stride} rows in batch {self.batch_id} have no signal values at all ({frac:.2%})", {"n": self.empty_rows * self.stride, "fraction": round(frac, 6), "events": _stride_events(self.empty_ranges, self.stride)}))
        if self.dup_rows:
            frac = self.dup_rows / n
            ev = _stride_events(self.dup_ranges, self.stride)
            out.append(self._make("duplicate_rows", "consistency", [], "fail" if frac >= 0.01 else "warn", min(1.0, 0.3 + 20 * frac), f"{self.dup_rows * self.stride} exact duplicate rows in batch {self.batch_id} ({frac:.2%}; first at rows {ev[0][0]}-{ev[0][1]})" if ev else f"{self.dup_rows} duplicate rows in batch {self.batch_id}", {"n": self.dup_rows * self.stride, "fraction": round(frac, 6), "events": ev}, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None))
        if self.dupkey_rows:
            frac = self.dupkey_rows / n
            ev = _stride_events(self.dupkey_ranges, self.stride)
            out.append(self._make("duplicate_key", "consistency", [], "warn" if frac < 0.01 else "fail", min(1.0, 0.3 + 10 * frac), f"{self.dupkey_rows * self.stride} rows in batch {self.batch_id} repeat an existing (group, order) key", {"n": self.dupkey_rows * self.stride, "fraction": round(frac, 6), "events": ev}, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None))
        # time
        if self.t_n:
            if self.t_neg:
                ev = self.t_neg_rows
                out.append(self._make("out_of_order", "timeliness", [], "fail", 0.6, f"{self.t_neg} timestamps go backwards in batch {self.batch_id} (first at row {ev[0][0] if ev else '?'})", {"n": self.t_neg, "events": ev}, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None))
            if self.t_zero and self.stride == 1:
                ev = self.t_zero_rows
                frac = self.t_zero / self.t_n
                out.append(self._make("duplicate_timestamp", "timeliness", [], "warn" if frac < 0.05 else "fail", 0.3 if frac < 0.05 else 0.6, f"{self.t_zero} repeated timestamps in batch {self.batch_id} ({frac:.2%})", {"n": self.t_zero, "fraction": round(frac, 6), "events": ev}, row_start=ev[0][0] if ev else None, row_end=ev[-1][1] if ev else None))
            if self.t_gaps and self.period_ref:
                ev = [(a, b) for a, b, _ in self.t_gaps]
                big = self.t_max_gap / (self.period_ref * self.stride)
                out.append(self._make("gap", "timeliness", [], "fail" if big >= 20 else "warn", 0.7 if big >= 20 else 0.4, f"{len(self.t_gaps)} timestamp gap(s) in batch {self.batch_id}; largest {_fmt_dur(self.t_max_gap)} = {big:.0f}x the usual period of {_fmt_dur(self.period_ref)} (rows {ev[0][0]}-{ev[0][1]})", {"n": len(self.t_gaps), "max_gap_s": self.t_max_gap, "period_s": self.period_ref, "events": ev, "gaps": [(a, b, g) for a, b, g in self.t_gaps]}, row_start=ev[0][0], row_end=ev[-1][1]))
            if self.period_ref and self.t_irregular / self.t_n >= 0.05:
                frac = self.t_irregular / self.t_n
                out.append(self._make("irregular_sampling", "timeliness", [], "warn", 0.3, f"Sampling is irregular in batch {self.batch_id}: {frac:.1%} of intervals deviate more than 20% from the usual period of {_fmt_dur(self.period_ref)}", {"fraction": round(frac, 5), "period_s": self.period_ref}))
        # relations
        for (a, b), p in self.pairs.items():
            if b == "derived":
                if p.resid_n < 10:
                    continue
                rms = math.sqrt(p.resid_sq / p.resid_n)
                scale = (self.stats.get(a, {}).get("scale") or 0.0)
                if scale > 0 and rms > 0.5 * scale:
                    out.append(self._make("relation_break", "consistency", [a], "warn" if rms < 1.0 * scale else "fail", 0.5 if rms < 1.0 * scale else 0.7, f"{a} no longer matches the formula it is derived from in batch {self.batch_id} (residual {fmt_num(rms)} vs usual spread {fmt_num(scale)})", {"rms_residual": rms, "scale": scale}))
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
                out.append(self._make("relation_break", "consistency", [a, b], "fail" if abs(r) < 0.7 else "warn", 0.7 if abs(r) < 0.7 else 0.5, f"{a} and {b} are normally redundant (r={exp_r:.2f}) but agree only at r={r:.2f} in batch {self.batch_id}: one of them is probably a bad sensor", {"r_batch": round(r, 4), "r_expected": exp_r, "n": p.n}))
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


def _quality_context(ws: Any, settings: Any) -> dict[str, Any]:
    catalog = load_catalog(ws)
    stats = global_stats(ws, settings, catalog) if ws.exists("dataset") else {s.alias: {**_no_stats(), **_fp_stats(s)} for s in catalog}
    schema = None
    try:
        schema = ws.schema()
    except Exception:
        schema = None
    relations = ws.read_json("relations", None)
    return {"catalog": catalog, "stats": stats, "schema": schema, "relations": relations}


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
    acc = BatchAccumulator(ws, settings, batch["batch_id"], catalog, qctx["stats"], schema=schema, relations=qctx.get("relations"), stride=stride, fast=fast)
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
    df = batch_df
    if ROW_COL not in df.columns:
        df = df.copy()
        df[ROW_COL] = np.arange(len(df), dtype="int64")
    acc = BatchAccumulator(ws, settings, batch_id, catalog, qctx["stats"], schema=qctx.get("schema"), relations=qctx.get("relations"))
    acc.update(df)
    checks = acc.results()
    for c in checks:
        ws.append_jsonl("checks", c)
    verdict = trust_verdict(ws, settings, batch_id, checks, n_signals=len(numeric_signals(catalog)) or None)
    ws.log.record("system:quality", "check", "batch", batch_id, {"n_checks": len(checks), "n_fail": sum(c.status == "fail" for c in checks), "n_warn": sum(c.status == "warn" for c in checks), "trusted": verdict.trusted, "trust_score": verdict.trust_score}, [e for c in checks for e in c.evidence_ids][:50])
    return checks, verdict
