"""Turn score sequences into Flag objects.

anomaly     contiguous rows with ensemble >= 1 lasting >= min_event_len rows (adjacent segments merged)
drift       such a segment whose onset is gradual and sustained (>= 3 windows)
changepoint one per group with a detected onset (from changepoints.py)
cascade     produced by cascade.py

Flags are capped per group (strongest kept, noted in the flag) so the list stays readable.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import numpy as np

from ..contracts import Flag, SignalContribution
from ._common import Budget, batch_ids_for_rows, runs_of_true
from .attribution import attribute_event
from .changepoints import analyse_group_onset
from .ensemble import FoldMap, FoldModel, ScoreStore, rescore_window

MAX_EVENTS_PER_GROUP = 8
MAX_FLAGS_TOTAL = 3000


def next_flag_id(ws) -> str:
    existing = ws.read_jsonl("flags")
    n = 0
    for f in existing:
        try:
            n = max(n, int(str(f.get("id", "FLAG-0")).split("-")[-1]))
        except ValueError:
            pass
    return f"FLAG-{n + 1:06d}"


class FlagIds:
    def __init__(self, ws):
        self._n = int(next_flag_id(ws).split("-")[-1]) - 1

    def next(self) -> str:
        self._n += 1
        return f"FLAG-{self._n:06d}"


def find_segments(ens: np.ndarray, min_len: int, merge_gap: int, thr: float = 1.0, window: Optional[int] = None) -> list[tuple[int, int]]:
    """Runs of ens >= thr; runs separated by <= merge_gap rows are merged; short runs dropped. A short
    segment (< 2 windows) must also be clearly above threshold (mean >= 1.25 or peak >= 2) -- isolated
    marginal exceedances are the expected ~1 % baseline noise, not events."""
    runs = runs_of_true(ens >= thr)
    if not runs:
        return []
    merged: list[list[int]] = [list(runs[0])]
    for s, e in runs[1:]:
        if s - merged[-1][1] <= merge_gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    out = []
    short = 2 * (window or merge_gap)
    for s, e in merged:
        if e - s < min_len:
            continue
        seg = ens[s:e]
        hot = seg[seg >= thr]
        if e - s < short and not (hot.mean() >= 1.25 * thr or seg.max() >= 2.0 * thr):
            continue
        out.append((s, e))
    return out


def segment_score(ens: np.ndarray, thr: float = 1.0) -> float:
    """Mean normalized score of the rows at or above threshold inside a segment."""
    hot = ens[ens >= thr]
    return float(hot.mean()) if len(hot) else float(ens.mean())


def severity_of(mean_norm: float, n_rows: int, window: int) -> float:
    return float(np.clip(0.2 + 0.5 * np.tanh((mean_norm - 1.0) / 3.0) + 0.3 * min(1.0, n_rows / (5.0 * window)), 0.0, 1.0))


def trust_context(inputs, row_start: int, row_end: int) -> tuple[Optional[dict[str, Any]], set[str]]:
    if not inputs.batches:
        return None, set()
    ids = batch_ids_for_rows(inputs.batches, np.array([row_start, max(row_start, row_end - 1)]))
    bids = sorted({str(b) for b in ids if b is not None})
    untrusted: set[str] = set()
    ctx: dict[str, Any] = {"batch_ids": bids, "trusted": True, "untrusted_signals": [], "trust_scores": {}}
    for b in bids:
        v = inputs.trust.get(b)
        if v is None:
            continue
        ctx["trust_scores"][b] = round(float(v.trust_score), 3)
        if not v.trusted:
            ctx["trusted"] = False
        untrusted.update(v.untrusted_signals or [])
    ctx["untrusted_signals"] = sorted(untrusted)
    return ctx, untrusted


def build_flags(ws, inputs, settings, store: ScoreStore, models: list[FoldModel], fold_map: FoldMap, budget: Budget, progress=None) -> tuple[list[Flag], list[Any], dict[str, Any]]:
    """Returns (flags, onsets, meta)."""
    window = int(settings.detect.window)
    min_len = int(settings.detect.min_event_len)
    merge_gap = max(min_len, window)
    by_fold = {m.fold: m for m in models}
    for m in models:
        m._inputs = inputs  # used by cause classification (relations)
    ids = FlagIds(ws)
    flags: list[Flag] = []
    onsets: list[Any] = []
    gidx = store.group_indices()
    n_groups = len(gidx)
    capped_groups = 0
    t_end = time.time() + max(5.0, budget.remaining() * 0.8)
    skipped_budget = 0
    for gi, (group, idx) in enumerate(gidx.items()):
        if progress and gi % 20 == 0:
            progress(0.8 + 0.12 * gi / max(1, n_groups), f"events: group {gi + 1}/{n_groups}")
        rows = store.rows[idx]
        order = np.argsort(rows, kind="stable")
        rows, ens = rows[order], store.ens[idx][order]
        segs = find_segments(ens, min_len, merge_gap, window=window)
        if not segs:
            continue
        if time.time() > t_end or len(flags) >= MAX_FLAGS_TOTAL:
            skipped_budget += 1
            continue
        fold = int(fold_map.fold_of(np.array([group], dtype=object), rows[:1])[0])
        fm = by_fold.get(fold, models[0])

        def rescore(rs: int, re_: int, _fm=fm, _g=group):
            return rescore_window(ws, inputs, _fm, _g, rs, re_, lookback=2 * window)

        # strongest segments first when capping
        ranked_segs = sorted(segs, key=lambda se: -(float(ens[se[0] : se[1]].mean()) * (se[1] - se[0])))
        capped = len(ranked_segs) > MAX_EVENTS_PER_GROUP
        if capped:
            capped_groups += 1
        keep = sorted(ranked_segs[:MAX_EVENTS_PER_GROUP])
        prev_end = 0
        group_onsets: list[Any] = []
        for s, e in keep:
            row_start, row_end = int(rows[s]), int(rows[e - 1]) + 1
            n_ev = e - s
            # onset of this event: estimated on the score series since the previous event ended
            onset = None
            try:
                onset = analyse_group_onset(ws, group, ens[prev_end:e], rows[prev_end:e], window, rescore, inputs.aliases)
            except Exception as ex:  # never let onset analysis break flagging
                ws.log.record("system:detect", "warning", "group", group, {"onset_error": str(ex)[:300]})
            prev_end = e
            if onset is not None:
                onsets.append(onset)
                group_onsets.append(onset)
            # attribution window: first <= 1500 rows of the event (plus lookback) and its tail if long
            win_end = row_end if n_ev <= 1500 else int(rows[s + 1500])
            res = rescore(row_start, win_end)
            if res is None:
                continue
            tctx, untrusted = trust_context(inputs, row_start, row_end)
            att = attribute_event(ws, inputs, fm, res, row_start, win_end, group, untrusted)
            mean_norm = segment_score(ens[s:e])
            peak = float(ens[s:e].max())
            kind = "anomaly"
            if onset is not None and onset.kind == "gradual" and n_ev >= 3 * window and abs(onset.row - row_start) <= 3 * window:
                kind = "drift"
            conf = float(np.clip(0.3 + 0.3 * att["agreement"] + 0.2 * min(1.0, (mean_norm - 1.0) / 2.0) + 0.2 * min(1.0, n_ev / (3.0 * min_len)), 0.05, 0.95))
            ranked = att["ranked"]
            lead = ", ".join(f"{r.signal} ({r.contribution:.0%}, {r.direction})" for r in ranked[:3])
            statement = f"{'Drift' if kind == 'drift' else 'Anomaly'} in group {group}, rows {row_start}-{row_end - 1} ({n_ev} rows): ensemble score {mean_norm:.1f}x threshold (peak {peak:.1f}x). Leading signals: {lead}. Likely cause: {att['cause']} ({att['cause_reason']})."
            if capped:
                statement += f" Note: group had {len(segs)} segments; only the {MAX_EVENTS_PER_GROUP} strongest are listed."
            ev_ids = list(att["evidence_ids"])
            if onset is not None and onset.evidence_id and abs(onset.row - row_start) <= 3 * window:
                ev_ids.append(onset.evidence_id)
            bids = (tctx or {}).get("batch_ids") or []
            fl = Flag(id=ids.next(), kind=kind, batch_id=bids[0] if bids else None, group_id=group, row_start=row_start, row_end=row_end - 1, severity=severity_of(mean_norm, n_ev, window), score=round(mean_norm, 4), threshold=1.0, detector="ensemble:" + "+".join(fm.selected), statement=statement, signals_ranked=ranked, evidence_ids=ev_ids, likely_cause_class=att["cause"], confidence=round(conf, 3), trust_context=tctx)
            flags.append(fl)
            if onset is not None:
                first = onset.first_signals[:5]
                contribs = [SignalContribution(signal=f["signal"], contribution=round(1.0 / max(1, len(first)), 3), direction=f["direction"], lag=f["lag"], explanation=f"{f['signal']} was {'first' if f['lag'] == 0 else f'{f['lag']} samples after the first signal'} to deviate ({f['direction']}) at row {f['row']}.", evidence_ids=[onset.evidence_id] if onset.evidence_id else []) for f in first]
                octx, _ = trust_context(inputs, onset.row, onset.row + 1)
                pos = int(np.searchsorted(rows, onset.row))
                peak_after = float(ens[pos : pos + 6 * window].max()) if pos < len(ens) else 0.0
                flags.append(Flag(id=ids.next(), kind="changepoint", batch_id=((octx or {}).get("batch_ids") or [None])[0], group_id=group, row_start=onset.row, row_end=onset.row, severity=severity_of(peak_after, 6 * window, window) * 0.8, score=round(peak_after, 4), threshold=1.0, detector="changepoints:cusum+ruptures+knee", statement=onset.statement + f" (leads to {fl.id})", signals_ranked=contribs, evidence_ids=([onset.evidence_id] if onset.evidence_id else []) + ev_ids[:1], likely_cause_class=att["cause"], confidence=round(onset.confidence, 3), trust_context=octx))
    meta = {"n_groups": n_groups, "n_groups_with_events": len({f.group_id for f in flags if f.kind in ('anomaly', 'drift')}), "groups_capped": capped_groups, "groups_skipped_for_budget": skipped_budget, "max_events_per_group": MAX_EVENTS_PER_GROUP}
    return flags, onsets, meta
