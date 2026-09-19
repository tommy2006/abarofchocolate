"""Turn score sequences into Flag objects.

anomaly     rows where the score stays high: the median score of one analysis window is above the sustained
            threshold (calibrated on the held-out baseline stretches, see calibrate_sustained_threshold);
            adjacent stretches merged, at least min_event_len rows. Single readings above threshold are points.
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
from ._common import Budget, batch_ids_for_rows, fetch_blocks, fetch_group_range, runs_of_true
from .attribution import attribute_event
from .changepoints import analyse_group_onset
from .ensemble import FoldMap, FoldModel, ScoreStore, rescore_window, robust_threshold

MAX_EVENTS_PER_GROUP = 8
MAX_FLAGS_TOTAL = 4000        # readable cap; every group is still summarised in group_scores.json
MAX_RICH_GROUPS = 600         # groups that get full attribution + onset analysis (strongest first, within budget)
MAX_SUMMARY_EVENTS_PER_GROUP = 2
POINT_MAX_LEN = 2             # a point anomaly is at most this many consecutive readings
POINT_MIN_PEAK = 1.5          # ...clearly above threshold (isolated marginal exceedances are the expected ~1 % noise)
Z_POINT = 4.0                 # level deviation (in normal spreads) that counts as 'this reading is off'
MAX_POINT_FLAGS = 500
MAX_POINT_FETCH_ROWS = 120_000
SUSTAIN_FLOOR, SUSTAIN_CEIL = 0.5, 1.0   # the calibrated sustained threshold stays inside these bounds


def sustained_mask(ens: np.ndarray, persist: int, thr: float) -> np.ndarray:
    """True where the MEDIAN score of the `persist` rows around a row is >= thr. A median cannot be lifted by one or
    two high readings (those are point findings), only by a lasting rise - moderate or strong."""
    n = len(ens)
    if persist <= 1:
        return ens >= thr
    if n < persist:
        return np.zeros(n, dtype=bool)
    from scipy.ndimage import median_filter

    return median_filter(np.asarray(ens, dtype=np.float64), size=int(persist), mode="nearest") >= thr


def calibrate_sustained_threshold(ws, store: ScoreStore, window: int) -> dict[str, Any]:
    """Label-free: the robust 99.5th percentile (x1.1, the rule used for every threshold here) of the window-median
    out-of-fold score on the baseline stretches. Every group is scored by a model that never saw it, so these are
    held-out normal rows."""
    from scipy.ndimage import median_filter

    ranges = (ws.read_json("baseline") or {}).get("ranges") or {}
    gidx = store.group_indices()
    vals: list[np.ndarray] = []
    for g, rr in ranges.items():
        idx = gidx.get(str(g))
        if idx is None:
            continue
        rows = store.rows[idx]
        order = np.argsort(rows, kind="stable")
        rows, e = rows[order], store.ens[idx][order]
        for s, t in rr:
            a, b = int(np.searchsorted(rows, int(s))), int(np.searchsorted(rows, int(t), side="right"))
            seg = np.asarray(e[a:b], dtype=np.float64)
            if len(seg) >= window:
                h = window // 2
                vals.append(median_filter(seg, size=window, mode="nearest")[h: len(seg) - h] if len(seg) > 2 * h else median_filter(seg, size=window, mode="nearest"))
    if not vals:
        return {"persist_rows": int(window), "sustained_threshold": 1.0, "n_windows": 0, "method": "no baseline stretch is one window long: the row threshold (1.0) is used for the window average"}
    v = np.concatenate(vals)
    raw = float(robust_threshold(v, margin=1.1, q=0.995))
    thr = float(np.clip(raw, SUSTAIN_FLOOR, SUSTAIN_CEIL))
    return {"persist_rows": int(window), "sustained_threshold": round(thr, 4), "raw_threshold": round(raw, 4), "n_windows": int(len(v)), "median_window_score": round(float(np.median(v)), 4),
            "method": f"an event needs the median score of {window} consecutive rows to reach {thr:.2f}: the robust 99.5th percentile (x1.1) of that median on the held-out baseline stretches, kept within [{SUSTAIN_FLOOR}, {SUSTAIN_CEIL}]; one or two high readings alone are point findings"}


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


def find_segments(ens: np.ndarray, min_len: int, merge_gap: int, thr: float = 1.0, window: Optional[int] = None, persist: Optional[int] = None, sustain_thr: Optional[float] = None) -> list[tuple[int, int]]:
    """Runs of ens >= thr; runs separated by <= merge_gap rows are merged; short runs dropped. A short
    segment (< 2 windows) must also be clearly above threshold (mean >= 1.25 or peak >= 2) -- isolated
    marginal exceedances are the expected ~1 % baseline noise, not events.
    With `persist` (round 6) a run is also kept only when the median score of `persist` rows reaches `sustain_thr`
    somewhere in it, and a lasting moderate rise (at least two windows) that never crosses the row threshold becomes a
    segment of its own. Boundaries of kept runs are unchanged."""
    runs = runs_of_true(ens >= thr)
    smask = sustained_mask(ens, int(persist), float(sustain_thr if sustain_thr is not None else thr)) if persist and persist > 1 else None
    merged: list[list[int]] = []
    for s, e in runs:
        if merged and s - merged[-1][1] <= merge_gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    out: list[tuple[int, int]] = []
    short = 2 * (window or merge_gap)
    covered = np.zeros(len(ens), dtype=bool)
    for s, e in merged:
        if e - s < min_len:
            continue
        seg = ens[s:e]
        hot = seg[seg >= thr]
        if e - s < short and not (hot.mean() >= 1.25 * thr or seg.max() >= 2.0 * thr):
            continue
        if smask is not None and not smask[s:e].any():
            continue  # no lasting rise anywhere in this run: a burst of isolated readings, not an event
        out.append((s, e))
        covered[s:e] = True
    if smask is not None:
        for s, e in runs_of_true(smask):
            if e - s >= max(min_len, short) and not covered[s:e].any():
                out.append((s, e))  # a lasting moderate rise that never crossed the row threshold: long by definition
        out.sort()
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
        for lu in (getattr(v, "local_untrusted", None) or []):
            try:
                if int(lu.get("row_end", -1)) >= row_start and int(lu.get("row_start", 1 << 62)) <= row_end:
                    untrusted.add(str(lu.get("signal")))
            except (TypeError, ValueError):
                continue
    ctx["untrusted_signals"] = sorted(untrusted)
    return ctx, untrusted


def _group_summary(group: str, rows: np.ndarray, ens: np.ndarray, top1: np.ndarray, segs: list[tuple[int, int]], aliases: np.ndarray) -> dict[str, Any]:
    hot = ens >= 1.0
    n = int(len(ens))
    top_sig = None
    if hot.any():
        cnt = np.bincount(top1[hot].astype(np.int64), minlength=len(aliases))
        top_sig = str(aliases[int(cnt.argmax())])
    strength = max(float(ens[a:b].mean()) * (b - a) for a, b in segs) if segs else 0.0
    return {"group": group, "n_rows": n, "row_start": int(rows[0]) if n else None, "row_end": int(rows[-1]) if n else None, "max_score": round(float(ens.max()), 3) if n else 0.0, "mean_score": round(float(ens.mean()), 3) if n else 0.0, "flagged_fraction": round(float(hot.mean()), 4) if n else 0.0, "first_cross_row": int(rows[int(np.argmax(hot))]) if hot.any() else None, "top_signal": top_sig, "n_segments": len(segs), "strength": round(strength, 2)}


def _summary_flags(ws, inputs, ids: FlagIds, group: str, rows: np.ndarray, ens: np.ndarray, top1: np.ndarray, segs: list[tuple[int, int]], aliases: np.ndarray, selected: list[str], window: int, min_len: int) -> list[Flag]:
    """Cheap flags for a group: attribution from the per-row leading signal recorded during scoring (no re-scoring,
    no change-point search). Used when the group is outside the rich budget."""
    out: list[Flag] = []
    ranked_segs = sorted(segs, key=lambda se: -(float(ens[se[0] : se[1]].mean()) * (se[1] - se[0])))
    for s_, e_ in sorted(ranked_segs[:MAX_SUMMARY_EVENTS_PER_GROUP]):
        row_start, row_end = int(rows[s_]), int(rows[e_ - 1]) + 1
        n_ev = e_ - s_
        seg_top = top1[s_:e_].astype(np.int64)
        cnt = np.bincount(seg_top, minlength=len(aliases))
        tot = max(1, int(cnt.sum()))
        order = [int(j) for j in np.argsort(-cnt)[:3] if cnt[j] > 0]
        ranked = [SignalContribution(signal=str(aliases[j]), contribution=round(float(cnt[j] / tot), 3), direction="deviating", lag=None, explanation=f"{aliases[j]} was the largest contributor to the anomaly score in {cnt[j] / tot:.0%} of the {n_ev} rows of this event.") for j in order]
        tctx, untrusted = trust_context(inputs, row_start, row_end)
        if ranked and ranked[0].signal in untrusted:
            cause, why = "data", f"{ranked[0].signal} was marked unreliable by the data-quality checks in these rows"
        elif len([r for r in ranked if r.contribution >= 0.15]) >= 2:
            cause, why = "process", "several signals lead the deviation, which points to the process rather than one instrument"
        elif ranked and ranked[0].contribution >= 0.85:
            cause, why = "unknown", f"one signal ({ranked[0].signal}) dominates; a sensor fault cannot be excluded without the full attribution"
        else:
            cause, why = "unknown", "attribution is a summary from the scoring pass"
        mean_norm = segment_score(ens[s_:e_])
        peak = float(ens[s_:e_].max())
        ev = ws.evidence.add("segment", f"Group {group}: ensemble score at or above threshold for rows {row_start}-{row_end - 1} ({n_ev} rows), mean {mean_norm:.2f}x, peak {peak:.2f}x; leading signal {ranked[0].signal if ranked else 'n/a'} in {ranked[0].contribution:.0%} of rows" if ranked else f"Group {group}: ensemble score above threshold for rows {row_start}-{row_end - 1} ({n_ev} rows), mean {mean_norm:.2f}x, peak {peak:.2f}x", signals=[r.signal for r in ranked], values={"row_start": row_start, "row_end": row_end - 1, "n_rows": n_ev, "mean_norm": round(mean_norm, 4), "peak": round(peak, 4), "leading_signal_shares": {str(aliases[j]): round(float(cnt[j] / tot), 4) for j in order}}, computed_by="detect.events.summary", n_samples=n_ev, group_id=group)
        lead = ", ".join(f"{r.signal} ({r.contribution:.0%})" for r in ranked)
        statement = f"Anomaly in group {group}, rows {row_start}-{row_end - 1} ({n_ev} rows): ensemble score {mean_norm:.1f}x threshold (peak {peak:.1f}x). Leading signals: {lead or 'n/a'}. Likely cause: {cause} ({why}). Summary flag: attribution comes from the scoring pass; full attribution and onset analysis were reserved for the strongest {MAX_RICH_GROUPS} groups within the time budget."
        conf = float(np.clip(0.25 + 0.2 * min(1.0, (mean_norm - 1.0) / 2.0) + 0.15 * min(1.0, n_ev / (3.0 * min_len)) + (0.15 if ranked and ranked[0].contribution >= 0.5 else 0.0), 0.05, 0.8))
        bids = (tctx or {}).get("batch_ids") or []
        out.append(Flag(id=ids.next(), kind="anomaly", batch_id=bids[0] if bids else None, group_id=group, row_start=row_start, row_end=row_end - 1, severity=severity_of(mean_norm, n_ev, window), score=round(mean_norm, 4), threshold=1.0, detector="ensemble:" + "+".join(selected) + " (summary)", statement=statement, signals_ranked=ranked, evidence_ids=[ev.id], likely_cause_class=cause, confidence=round(conf, 3), trust_context=tctx))
    return out

def _quality_single_readings(ws) -> dict[int, list[tuple[str, float, Optional[str], str]]]:
    """Rows where the data-quality checks found a single odd reading (local spike, or an out-of-range /
    impossible run of <= POINT_MAX_LEN rows). Used to recognise the ECHO of such a reading: one bad value keeps
    rolling-window features and drift detectors above threshold for a window or more, which is not an event."""
    out: dict[int, list[tuple[str, float, Optional[str], str]]] = {}
    try:
        checks = ws.read_jsonl("checks")
    except Exception:
        return out
    for c in checks:
        ct = c.get("check_type")
        if ct not in ("local_spike", "out_of_range", "plausibility", "impossible_value") or c.get("status") in ("pass", "not_testable"):
            continue
        sig = (c.get("signals") or [None])[0]
        if not sig:
            continue
        for pt in (c.get("values") or {}).get("points") or []:
            rs, re_ = int(pt[0]), int(pt[1])
            if re_ - rs + 1 > POINT_MAX_LEN:
                continue
            z = float(pt[2]) if len(pt) > 2 and pt[2] is not None else 0.0
            d = pt[3] if len(pt) > 3 else None
            for r in range(rs, re_ + 1):
                out.setdefault(r, []).append((sig, z, d, ct))
    return out


POINT_WORDING = "Each of these readings is either a glitch (sensor, transmission or entry error) or a deliberate manipulation; the data alone cannot tell which."


MAX_ECHO_TESTS = 300


def _is_echo(ws, inputs, fm, group: str, row_start: int, row_end: int, spike_rows: list[int], window: int, min_len: int, merge_gap: int) -> bool:
    """Counterfactual test: would this above-threshold stretch exist WITHOUT the known single odd readings?
    Each such reading is repaired (replaced by the median of its neighbours), the stretch is re-scored with the
    same fold model, and the stretch is an echo when no sustained segment is left."""
    rows, groups, X = fetch_group_range(ws, inputs.columns, inputs.group_col, group, max(0, int(row_start) - 2 * window), int(row_end) + 1)
    if len(rows) < min_len:
        return False
    Xr = np.array(X, dtype=np.float32, copy=True)
    pos = {int(r): i for i, r in enumerate(rows.tolist())}
    n = len(rows)
    for sr in spike_rows:
        for r_ in (sr - 1, sr, sr + 1):   # a glitch may smear over the adjacent reading (POINT_MAX_LEN = 2)
            i = pos.get(int(r_))
            if i is None or (r_ != sr and int(r_) in spike_rows):
                continue
            if r_ != sr:
                continue
            nb = np.concatenate([Xr[max(0, i - 3) : i], Xr[i + 1 : min(n, i + 4)]])
            if len(nb):
                Xr[i] = np.nanmedian(nb, axis=0)
    F = fm.spec.transform(Xr, groups)
    res = fm.score_features(F, groups, isolate_state=True)
    fm.reset_states()
    ens = np.asarray(res["ensemble"])[rows >= int(row_start)]
    return not find_segments(ens, min_len, merge_gap, window=window)


def _classify_points(ws, inputs, spec, pcand: list[dict[str, Any]], window: int, quality_rows: Optional[dict[int, list]] = None, echo_test=None) -> tuple[list[dict[str, Any]], set[tuple[str, int, int]]]:
    """pcand: short above-threshold stretches. A stretch is a POINT anomaly when the raw level deviates in at most
    POINT_MAX_LEN readings and the rest of the stretch is only the echo of those readings in the rolling
    features (a single spike keeps a rolling std high for a whole window). Returns (points, segments to drop from
    the sustained-event list). One batched fetch; bounded rows."""
    if not pcand:
        return [], set()
    pcand = sorted(pcand, key=lambda c: -c["peak"])
    blocks, kept, total = [], [], 0
    for c in pcand:
        n = c["row_end"] - c["row_start"] + 1
        if total + n > MAX_POINT_FETCH_ROWS:
            continue
        blocks.append((c["row_start"], c["row_end"] + 1))
        kept.append(c)
        total += n
    rows, _groups, X = fetch_blocks(ws, inputs.columns, inputs.group_col, blocks)
    if len(rows) == 0:
        return [], set()
    z = spec.zscore(X)
    z = np.nan_to_num(z, nan=0.0)
    pos = {int(r): i for i, r in enumerate(rows.tolist())}
    aliases = list(inputs.aliases)
    points: list[dict[str, Any]] = []
    drop: set[tuple[str, int, int]] = set()
    n_echo_tests = 0
    for c in kept:
        idx = [pos[r] for r in range(c["row_start"], c["row_end"] + 1) if r in pos]
        if not idx:
            continue
        zz = np.abs(z[idx])                      # (n_rows_in_stretch, p)
        dev = zz.max(axis=1) >= Z_POINT
        n_dev = int(dev.sum())
        if c["kind"] == "segment" and c.get("spikes"):
            # ECHO of known single readings: no other level deviation inside the stretch -> not an event
            srows = sorted(set(int(r) for r in c["spikes"]))
            near = np.zeros(len(idx), dtype=bool)
            for k_, r_ in enumerate(range(c["row_start"], c["row_end"] + 1)):
                if k_ < len(near) and any(abs(r_ - sr) <= 1 for sr in srows):
                    near[k_] = True
            n_echo_tests += 1
            if echo_test is None or n_echo_tests > MAX_ECHO_TESTS or not echo_test(c):
                continue  # the stretch survives without those readings: a real sustained event that merely contains a spike
            drop.add((c["group"], c["seg"][0], c["seg"][1]))
            for sr in srows:
                if sr not in pos:
                    continue
                zrow = z[pos[sr]]
                order_ = [int(j) for j in np.argsort(-np.abs(zrow))[:3] if abs(zrow[j]) >= 3.0]
                sigs_ = [{"signal": aliases[j], "deviation": round(abs(float(zrow[j])), 1), "direction": "up" if zrow[j] > 0 else "down", "explanation": f"{aliases[j]} was {abs(float(zrow[j])):.1f} times its normal spread {'above' if zrow[j] > 0 else 'below'} its usual level"} for j in order_]
                for (qs, qz, qd, qk) in (quality_rows or {}).get(sr, []):
                    if not any(x["signal"] == qs for x in sigs_):
                        sigs_.append({"signal": qs, "deviation": round(float(qz), 1), "direction": qd or "changed", "explanation": (f"{qs} jumped {qz:.0f} times the local noise {'upwards' if qd == 'up' else 'downwards' if qd == 'down' else 'away'} from its neighbours and came straight back" if qk == "local_spike" else f"{qs} was {qz:.0f} times its normal spread away from its usual level")})
                points.append({"group": c["group"], "row_start": sr, "row_end": sr, "peak": float(c["peak"]), "signals": sigs_[:3], "max_dev": float(np.abs(zrow).max())})
            continue
        if c["kind"] == "segment":
            # point-like only if the level deviation is confined to <= POINT_MAX_LEN contiguous readings near the start
            if not (1 <= n_dev <= POINT_MAX_LEN):
                continue
            where = np.flatnonzero(dev)
            if where[-1] - where[0] + 1 > POINT_MAX_LEN or where[0] > 3:
                continue
            drop.add((c["group"], c["seg"][0], c["seg"][1]))
            use = [idx[int(w)] for w in where]
            r0, r1 = c["row_start"] + int(where[0]), c["row_start"] + int(where[-1])
        else:
            use = idx
            r0, r1 = c["row_start"], c["row_end"]
        zsig = z[use]                             # signed
        mag = np.abs(zsig).max(axis=0)
        order = [int(j) for j in np.argsort(-mag)[:3] if mag[j] >= 3.0]
        sigs = []
        for j in order:
            signed = float(zsig[int(np.argmax(np.abs(zsig[:, j]))), j])
            sigs.append({"signal": aliases[j], "deviation": round(abs(signed), 1), "direction": "up" if signed > 0 else "down", "explanation": f"{aliases[j]} was {abs(signed):.1f} times its normal spread {'above' if signed > 0 else 'below'} its usual level"})
        if not sigs and c.get("top1") is not None:
            a = aliases[int(c["top1"])]
            sigs.append({"signal": a, "deviation": None, "direction": "changed", "explanation": f"{a} stayed within its usual levels; what stood out was how abruptly it changed from one reading to the next"})
        points.append({"group": c["group"], "row_start": int(r0), "row_end": int(r1), "peak": float(c["peak"]), "signals": sigs, "max_dev": float(mag.max()) if mag.size else 0.0})
    return points, drop


def _point_flags(ws, inputs, ids: "FlagIds", points: list[dict[str, Any]], selected: list[str]) -> list[Flag]:
    out: list[Flag] = []
    for pt in sorted(points, key=lambda q: -(q["peak"] + 0.1 * q["max_dev"]))[:MAX_POINT_FLAGS]:
        n = pt["row_end"] - pt["row_start"] + 1
        where = f"row {pt['row_start']}" if n == 1 else f"rows {pt['row_start']}-{pt['row_end']}"
        lead = "; ".join(s["explanation"] for s in pt["signals"][:3]) or "no single signal stands out"
        tctx, untrusted = trust_context(inputs, pt["row_start"], pt["row_end"] + 1)
        ranked = []
        tot = sum((s["deviation"] or 0.0) for s in pt["signals"]) or 1.0
        for s in pt["signals"]:
            ranked.append(SignalContribution(signal=s["signal"], contribution=round(float((s["deviation"] or 0.0) / tot), 3) if s["deviation"] else round(1.0 / max(1, len(pt["signals"])), 3), direction=s["direction"], lag=None, explanation=s["explanation"] + f" at {where}."))
        statement = f"Isolated suspicious reading in group {pt['group']}, {where}: {lead}. It lasts {n} reading(s) and the neighbouring readings look normal (score {pt['peak']:.1f}x threshold). {POINT_WORDING}"
        if ranked and ranked[0].signal in untrusted:
            statement += f" The data-quality checks also marked {ranked[0].signal} as unreliable in these rows."
        ev = ws.evidence.add("point_anomaly", f"Group {pt['group']}, {where}: {lead}; anomaly score {pt['peak']:.1f}x threshold for {n} reading(s), neighbours normal.", signals=[s["signal"] for s in pt["signals"]], values={"row_start": pt["row_start"], "row_end": pt["row_end"], "n_readings": n, "peak": round(pt["peak"], 3), "deviations": {s["signal"]: s["deviation"] for s in pt["signals"]}, "directions": {s["signal"]: s["direction"] for s in pt["signals"]}}, computed_by="detect.events.points", n_samples=n, group_id=pt["group"])
        conf = float(np.clip(0.35 + 0.08 * min(5.0, pt["peak"] - 1.0) + 0.04 * min(5.0, pt["max_dev"] / 4.0), 0.1, 0.85))
        bids = (tctx or {}).get("batch_ids") or []
        out.append(Flag(id=ids.next(), kind="point", batch_id=bids[0] if bids else None, group_id=pt["group"], row_start=pt["row_start"], row_end=pt["row_end"], severity=float(np.clip(0.25 + 0.12 * (pt["peak"] - 1.0), 0.1, 1.0)), score=round(pt["peak"], 4), threshold=1.0, detector="ensemble:" + "+".join(selected) + " (single reading)", statement=statement, signals_ranked=ranked, evidence_ids=[ev.id], likely_cause_class="unknown", confidence=round(conf, 3), trust_context=tctx))
    return out


def build_flags(ws, inputs, settings, store: ScoreStore, models: list[FoldModel], fold_map: FoldMap, budget: Budget, progress=None) -> tuple[list[Flag], list[Any], dict[str, Any]]:
    """Returns (flags, onsets, meta). Two passes: a cheap scan of every group (segments, strength, summary written to
    group_scores.json), then rich attribution + onset analysis for the strongest groups while the budget lasts and
    summary flags for the rest, up to MAX_FLAGS_TOTAL."""
    window = int(settings.detect.window)
    min_len = int(settings.detect.min_event_len)
    merge_gap = max(min_len, window)
    by_fold = {m.fold: m for m in models}
    for m in models:
        m._inputs = inputs  # used by cause classification (relations)
    ids = FlagIds(ws)
    flags: list[Flag] = []
    onsets: list[Any] = []
    aliases = np.asarray(list(inputs.aliases), dtype=object)
    gidx = store.group_indices()
    n_groups = len(gidx)
    selected = list(models[0].selected) if models else []

    # ---- the event rule: sustained score over one window, threshold calibrated on held-out baseline stretches
    event_rule = calibrate_sustained_threshold(ws, store, window)
    persist, sustain_thr = int(event_rule["persist_rows"]), float(event_rule["sustained_threshold"])
    ws.log.record("system:detect", "event_rule", "dataset", "events", event_rule)

    # ---- pass 1: every group, numpy only
    t_scan = time.time()
    cand: list[tuple[float, str, np.ndarray, np.ndarray, np.ndarray, list[tuple[int, int]]]] = []
    summary: list[dict[str, Any]] = []
    pcand: list[dict[str, Any]] = []
    quality_rows = _quality_single_readings(ws)
    q_rows = np.array(sorted(quality_rows), dtype=np.int64)
    for gi, (group, idx) in enumerate(gidx.items()):
        if progress and gi % 500 == 0:
            progress(0.8 + 0.04 * gi / max(1, n_groups), f"events: scanning group {gi + 1}/{n_groups}")
        rows = store.rows[idx]
        order = np.argsort(rows, kind="stable")
        rows, ens, top1 = rows[order], store.ens[idx][order], store.top1[idx][order]
        segs = find_segments(ens, min_len, merge_gap, window=window, persist=persist, sustain_thr=sustain_thr)
        # point candidates: (a) raw stretches of <= POINT_MAX_LEN readings clearly above threshold that are not part
        # of a sustained segment, (b) short segments (a single spike keeps rolling features high for ~a window)
        covered = np.zeros(len(ens), dtype=bool)
        for a_, b_ in segs:
            covered[a_:b_] = True
        for a_, b_ in runs_of_true(ens >= 1.0):
            if b_ - a_ <= POINT_MAX_LEN and not covered[a_:b_].any() and float(ens[a_:b_].max()) >= POINT_MIN_PEAK:
                pcand.append({"group": group, "row_start": int(rows[a_]), "row_end": int(rows[b_ - 1]), "peak": float(ens[a_:b_].max()), "kind": "run", "seg": None, "top1": int(top1[a_ + int(np.argmax(ens[a_:b_]))])})
        for a_, b_ in segs:
            if int(rows[b_ - 1]) - int(rows[a_]) != b_ - a_ - 1:
                continue
            rs_, re_ = int(rows[a_]), int(rows[b_ - 1])
            lo_, hi_ = int(np.searchsorted(q_rows, rs_ - 2)), int(np.searchsorted(q_rows, re_, side="right"))
            spikes_ = [int(r) for r in q_rows[lo_:hi_]]
            echo_ok = bool(spikes_) and spikes_[0] <= rs_ + 3 and (b_ - a_) <= window * (1 + len(set(spikes_))) + 10
            if b_ - a_ <= window + 5 or echo_ok:
                pcand.append({"group": group, "row_start": rs_, "row_end": re_, "peak": float(ens[a_:b_].max()), "kind": "segment", "seg": (int(a_), int(b_)), "top1": int(top1[a_ + int(np.argmax(ens[a_:b_]))]), "spikes": spikes_ if echo_ok else []})
        summ = _group_summary(group, rows, ens, top1, segs, aliases)
        summary.append(summ)
        if segs:
            cand.append((summ["strength"], group, rows, ens, top1, segs))
    # ---- point anomalies: classify short stretches, take point-like segments out of the sustained-event list
    points: list[dict[str, Any]] = []
    try:
        def _echo_test(c_: dict[str, Any]) -> bool:
            fold_ = int(fold_map.fold_of(np.array([c_["group"]], dtype=object), np.array([c_["row_start"]]))[0])
            return _is_echo(ws, inputs, by_fold.get(fold_, models[0]), c_["group"], c_["row_start"], c_["row_end"], list(c_.get("spikes") or []), window, min_len, merge_gap)

        points, drop = _classify_points(ws, inputs, models[0].spec, pcand, window, quality_rows, _echo_test) if models else ([], set())
    except Exception as ex:  # never let the point pass break flagging
        points, drop = [], set()
        ws.log.record("system:detect", "warning", "dataset", "points", {"error": str(ex)[:300]})
    if drop:
        kept_cand = []
        for (strength, group, rows_g, ens_g, top1_g, segs_g) in cand:
            segs2 = [sg for sg in segs_g if (group, int(sg[0]), int(sg[1])) not in drop]
            if segs2:
                kept_cand.append((max(float(ens_g[a_:b_].mean()) * (b_ - a_) for a_, b_ in segs2), group, rows_g, ens_g, top1_g, segs2))
        cand = kept_cand
    n_sustained = int(sum(len(c[5]) for c in cand))
    n_points = len(points)
    share_points = float(n_points / max(1, n_points + n_sustained))
    point_dominated = bool(n_points >= 3 and share_points >= 0.5)
    seen_pt: dict[tuple[str, int], dict[str, Any]] = {}
    for pt in sorted(points, key=lambda q: -len(q["signals"])):
        key = next((k for k in ((pt["group"], pt["row_start"] + d_) for d_ in (0, -1, 1)) if k in seen_pt), None)
        if key is None:
            seen_pt[(pt["group"], pt["row_start"])] = pt
    points = list(seen_pt.values())
    n_points = len(points)
    share_points = float(n_points / max(1, n_points + n_sustained))
    point_dominated = bool(n_points >= 3 and share_points >= 0.5)
    flags.extend(_point_flags(ws, inputs, ids, points, selected))
    if n_points:
        pev = ws.evidence.add("point_regime", f"{n_points} above-threshold stretches are isolated readings of at most {POINT_MAX_LEN} rows and {n_sustained} are sustained events ({share_points:.0%} points).", values={"n_point_stretches": n_points, "n_sustained_stretches": n_sustained, "share_points": round(share_points, 4), "point_max_len": POINT_MAX_LEN}, computed_by="detect.events.points", n_samples=int(n_points + n_sustained))
        if point_dominated:
            ws.inferences.add("dataset", "Most deviations in this data are isolated single readings, not sustained events: onset, pattern and propagation analysis do not apply to them.", status="inferred", confidence=float(min(0.95, 0.5 + 0.5 * share_points)), evidence_ids=[pev.id], reasoning="A sustained process change keeps the score above threshold for many consecutive readings; here most exceedances last one or two readings while the neighbouring readings are normal.", source="code", stage="detect", alternatives=["a very short sampling of longer events (check the sampling period)"])
    cand.sort(key=lambda c: -c[0])
    ws.write_json("group_scores.json", {"n_groups": n_groups, "n_groups_over_threshold": len(cand), "threshold": 1.0, "note": "per-group summary of the out-of-fold ensemble score; flags list the strongest groups, this table covers all of them", "groups": summary})
    scan_s = time.time() - t_scan

    # ---- pass 2: strongest groups first
    t_end_rich = time.time() + max(45.0, budget.remaining() * 0.6)  # rich attribution is the most valuable output: always at least 45 s
    rich_done = 0
    summary_done = 0
    skipped_cap = 0
    capped_groups = 0
    for ci, (strength, group, rows, ens, top1, segs) in enumerate(cand):
        if progress and ci % 100 == 0:
            progress(0.84 + 0.08 * ci / max(1, len(cand)), f"events: group {ci + 1}/{len(cand)} (rich {rich_done}, summary {summary_done})")
        if len([f for f in flags if f.kind != "point"]) >= MAX_FLAGS_TOTAL:
            skipped_cap += 1
            continue
        rich = rich_done < MAX_RICH_GROUPS and time.time() < t_end_rich
        if not rich:
            flags.extend(_summary_flags(ws, inputs, ids, group, rows, ens, top1, segs, aliases, selected, window, min_len))
            summary_done += 1
            continue
        rich_done += 1
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
            fl = Flag(id=ids.next(), kind=kind, batch_id=bids[0] if bids else None, group_id=group, row_start=row_start, row_end=row_end - 1, severity=severity_of(mean_norm, n_ev, window), score=round(mean_norm, 4), threshold=1.0, detector="ensemble:" + "+".join(fm.selected), statement=statement, signals_ranked=ranked, evidence_ids=ev_ids, likely_cause_class=att["cause"], cause_detail=att.get("cause_detail"), confidence=round(conf, 3), trust_context=tctx)
            flags.append(fl)
            if onset is not None:
                first = onset.first_signals[:5]
                contribs = [SignalContribution(signal=f["signal"], contribution=round(1.0 / max(1, len(first)), 3), direction=f["direction"], lag=f["lag"], explanation=f"{f['signal']} was {'first' if f['lag'] == 0 else f'{f['lag']} samples after the first signal'} to deviate ({f['direction']}) at row {f['row']}.", evidence_ids=[onset.evidence_id] if onset.evidence_id else []) for f in first]
                octx, _ = trust_context(inputs, onset.row, onset.row + 1)
                pos = int(np.searchsorted(rows, onset.row))
                peak_after = float(ens[pos : pos + 6 * window].max()) if pos < len(ens) else 0.0
                flags.append(Flag(id=ids.next(), kind="changepoint", batch_id=((octx or {}).get("batch_ids") or [None])[0], group_id=group, row_start=onset.row, row_end=onset.row, severity=severity_of(peak_after, 6 * window, window) * 0.8, score=round(peak_after, 4), threshold=1.0, detector="changepoints:cusum+ruptures+knee", statement=onset.statement + f" (leads to {fl.id})", signals_ranked=contribs, evidence_ids=([onset.evidence_id] if onset.evidence_id else []) + ev_ids[:1], likely_cause_class=att["cause"], confidence=round(onset.confidence, 3), trust_context=octx))
    if skipped_cap or len(cand) > rich_done:
        gev = ws.evidence.add("group_scores", f"{len(cand)} of {n_groups} groups have at least one sustained stretch above the anomaly threshold; {rich_done} received full attribution, {summary_done} summary flags, {skipped_cap} are listed only in the per-group table.", values={"n_groups": n_groups, "n_groups_over_threshold": len(cand), "rich_groups": rich_done, "summary_groups": summary_done, "groups_only_in_table": skipped_cap, "flag_cap": MAX_FLAGS_TOTAL}, computed_by="detect.events.build_flags", n_samples=int(n_groups))
        ws.inferences.add("dataset", f"{len(cand)} of {n_groups} groups exceed the anomaly threshold; {rich_done} strongest groups received full attribution and onset analysis, {summary_done} received summary flags, {skipped_cap} are listed only in group_scores.json (flag cap {MAX_FLAGS_TOTAL}).", status="inferred", confidence=0.9, evidence_ids=[gev.id], reasoning="Per-group scores are complete (out-of-fold); the flag list is capped for readability and time budget. Rich analysis is prioritised by event strength.", source="code", stage="detect")
    meta_points = {"n_point_stretches": n_points, "n_sustained_stretches": n_sustained, "share_points": round(share_points, 4), "point_dominated": point_dominated, "n_point_flags": len([f for f in flags if f.kind == "point"])}
    meta = {"event_rule": event_rule, "points": meta_points, "n_groups": n_groups, "n_groups_over_threshold": len(cand), "n_groups_with_events": len({f.group_id for f in flags if f.kind in ('anomaly', 'drift')}), "rich_groups": rich_done, "summary_groups": summary_done, "groups_capped": capped_groups, "groups_skipped_for_cap": skipped_cap, "max_events_per_group": MAX_EVENTS_PER_GROUP, "max_rich_groups": MAX_RICH_GROUPS, "scan_seconds": round(scan_s, 2)}
    return flags, onsets, meta
