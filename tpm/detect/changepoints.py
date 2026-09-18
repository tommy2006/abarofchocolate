"""Onset detection per group on the ensemble score and per-signal z.

Two estimators are combined: an online CUSUM on the normalized ensemble score (exact-row resolution) and
an offline change-point search (ruptures Binseg on a bounded subsample). The onset is classified as
abrupt vs gradual (how fast the score reaches its post-onset level) and first- vs second-order (level
change vs rate-of-change change of the leading signals). Evidence reads like
"onset at row 214 (group 7), abrupt, first-order; first signals to move: S07 (lag 0, up), S13 (lag +3, up)".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np


@dataclass
class Onset:
    group: str
    row: int
    index: int  # position within the group's ordered score series
    kind: str  # abrupt | gradual
    order: str  # first | second
    confidence: float
    methods: dict[str, Any] = field(default_factory=dict)
    first_signals: list[dict[str, Any]] = field(default_factory=list)  # [{signal, lag, direction, row}]
    statement: str = ""
    evidence_id: Optional[str] = None


def _reflect(y: np.ndarray) -> np.ndarray:
    C = np.concatenate([[0.0], np.cumsum(y)])
    m = np.minimum(np.minimum.accumulate(C), 0.0)
    return (C - m)[1:]


def cusum_onset(s: np.ndarray, drift: float = 0.75, h: float = 3.0) -> Optional[tuple[int, int]]:
    """Online CUSUM on the normalized score. Returns (onset_index, alarm_index) or None."""
    if len(s) == 0:
        return None
    c = _reflect(s.astype(np.float64) - drift)
    above = np.flatnonzero(c > h)
    if len(above) == 0:
        return None
    alarm = int(above[0])
    zeros = np.flatnonzero(c[: alarm + 1] <= 1e-9)
    onset = int(zeros[-1]) + 1 if len(zeros) else 0
    return min(onset, alarm), alarm


def ruptures_onset(s: np.ndarray, max_points: int = 2000, min_gain: float = 0.3) -> Optional[tuple[int, float]]:
    """Offline first significant upward change of log1p(score). Returns (onset_index, mean_after) or None."""
    n = len(s)
    if n < 20:
        return None
    step = max(1, int(np.ceil(n / max_points)))
    x = np.log1p(np.maximum(s[::step].astype(np.float64), 0.0))
    m = len(x)
    if m < 10:
        return None
    min_size = max(3, int(0.03 * m))
    bkps: list[int] = []
    try:
        import ruptures as rpt

        k = int(min(3, m // (2 * min_size)))
        if k >= 1:
            bkps = [int(b) for b in rpt.Binseg(model="l2", min_size=min_size, jump=1).fit(x).predict(n_bkps=k)[:-1]]
    except Exception:
        bkps = []
    if not bkps:
        c1 = np.cumsum(x)
        c2 = np.cumsum(x * x)
        best, best_gain = None, 0.0
        tot = c2[-1] - c1[-1] ** 2 / m
        for t in range(min_size, m - min_size + 1):
            a = c2[t - 1] - c1[t - 1] ** 2 / t
            b = (c2[-1] - c2[t - 1]) - (c1[-1] - c1[t - 1]) ** 2 / (m - t)
            g = tot - a - b
            if g > best_gain:
                best, best_gain = t, g
        if best is not None:
            bkps = [best]
    bkps = sorted(bkps)
    prev = 0
    for i, b in enumerate(bkps):
        nxt = bkps[i + 1] if i + 1 < len(bkps) else m
        before, after = x[prev:b], x[b:nxt]
        if len(before) >= 2 and len(after) >= 2 and after.mean() - before.mean() >= min_gain and np.expm1(after.mean()) >= 0.7:
            return int(b * step), float(np.expm1(after.mean()))
        prev = b
    return None


def knee_onset(s: np.ndarray, end: int, window: int, max_points: int = 600) -> Optional[tuple[int, float]]:
    """Flat-then-linear ('knee') fit of log1p(score) on [0, end): the knee is where a gradual trend began.
    Returns (knee_index, gain) where gain = SSE reduction relative to a flat fit (0..1), or None."""
    end = int(min(end, len(s)))
    if end < 4 * window:
        return None
    step = max(1, int(np.ceil(end / max_points)))
    x = np.log1p(np.maximum(s[:end:step].astype(np.float64), 0.0))
    m = len(x)
    if m < 12:
        return None
    t = np.arange(m, dtype=np.float64)
    sse_flat = float(((x - x.mean()) ** 2).sum())
    best, best_sse = None, sse_flat
    lo, hi = max(2, int(0.05 * m)), m - max(3, int(0.1 * m))
    for k in range(lo, hi):
        pre, post = x[:k], x[k:]
        a = pre.mean()
        tt = t[k:] - k
        # post segment: a + b * tt with a fixed by the flat part (continuity at the knee)
        denom = float((tt * tt).sum())
        b = float(((post - a) * tt).sum() / denom) if denom > 0 else 0.0
        if b <= 0:
            continue
        sse = float(((pre - a) ** 2).sum() + ((post - a - b * tt) ** 2).sum())
        if sse < best_sse:
            best, best_sse = k, sse
    if best is None or sse_flat <= 0:
        return None
    gain = 1.0 - best_sse / sse_flat
    return int(best * step), float(gain)


def estimate_onset(s: np.ndarray, window: int) -> Optional[dict[str, Any]]:
    """Combine the online and offline estimates on one group's ordered normalized score series."""
    cu = cusum_onset(s)
    ru = ruptures_onset(s)
    if cu is None and ru is None:
        return None
    if cu is not None and ru is not None:
        agree = abs(cu[0] - ru[0]) <= 3 * window
        onset = cu[0] if agree or cu[0] <= ru[0] else ru[0]
        conf = 0.8 if agree else 0.5
    elif cu is not None:
        onset, conf = cu[0], 0.55
    else:
        onset, conf = ru[0], 0.4
    onset = int(max(0, min(onset, len(s) - 1)))
    # abrupt vs gradual: rows needed for the (3-point smoothed) score to reach 80 % of its post-onset median
    post = s[onset : onset + 6 * window]
    kind = "abrupt"
    t80 = None
    if len(post) >= 3:
        sm = np.convolve(post, np.ones(3) / 3, mode="same")
        target = 0.8 * float(np.median(post))
        hit = np.flatnonzero(sm >= target)
        t80 = int(hit[0]) if len(hit) else len(post)
        kind = "abrupt" if t80 <= window else "gradual"
    knee = None
    if kind == "gradual" or (cu is not None and cu[1] - cu[0] > 2 * window):
        # a slow rise: the alarm-based estimate lands where the score crossed the drift, the knee of a
        # flat-then-linear fit is where the trend began
        kn = knee_onset(s, min(len(s), onset + 6 * window), window)
        if kn is not None and kn[1] >= 0.3 and 0 < onset - kn[0] <= 8 * window:
            knee = {"onset": int(kn[0]), "gain": round(kn[1], 3)}
            onset = int(kn[0])
            kind = "gradual"
            conf = min(conf, 0.6)
    return {"index": onset, "kind": kind, "confidence": conf, "cusum": None if cu is None else {"onset": int(cu[0]), "alarm": int(cu[1])}, "ruptures": None if ru is None else {"onset": int(ru[0]), "mean_after": round(ru[1], 3)}, "knee": knee, "rows_to_80pct": t80}


def signal_onsets(z: np.ndarray, rstd_z: np.ndarray, resid: Optional[np.ndarray], start: int, z_thr: float = 2.5, min_run: int = 3, stop: Optional[int] = None) -> list[tuple[int, int, str]]:
    """First sustained deviation per signal inside [start, stop). Returns [(signal_idx, t, direction)] sorted
    by t. Signals that only deviate after `stop` are not part of the onset ordering."""
    n, p = z.shape
    n = n if stop is None else min(n, int(stop))
    out = []
    for j in range(p):
        dev_up = z[:, j] > z_thr
        dev_dn = z[:, j] < -z_thr
        noisy = rstd_z[:, j] > 3.0
        stuck = rstd_z[:, j] < -2.5
        shifted = np.abs(resid[:, j]) > 3.0 if resid is not None else np.zeros(n, dtype=bool)
        any_dev = dev_up | dev_dn | noisy | stuck | shifted
        # sustained: min_run consecutive rows, searching from `start`
        run = 0
        t_first = None
        for t in range(start, n):
            run = run + 1 if any_dev[t] else 0
            if run >= min_run:
                t_first = t - min_run + 1
                break
        if t_first is None:
            continue
        w = slice(t_first, min(z.shape[0], t_first + 10))
        if stuck[w].mean() > 0.5:
            d = "stuck"
        elif noisy[w].mean() > 0.5 and abs(z[w, j].mean()) < z_thr:
            d = "noisy"
        elif dev_up[w].mean() >= dev_dn[w].mean() and dev_up[w].any():
            d = "up"
        elif dev_dn[w].any():
            d = "down"
        else:
            d = "shifted"
        out.append((j, int(t_first), d))
    out.sort(key=lambda t: t[1])
    return out


def change_order(z: np.ndarray, onset: int, window: int, signal_idx: list[int]) -> str:
    """first-order (level change) vs second-order (rate change) on the leading signals around the onset."""
    votes = []
    for j in signal_idx[:3]:
        pre = z[max(0, onset - 2 * window) : onset, j]
        post = z[onset : onset + 3 * window, j]
        if len(pre) < 4 or len(post) < 4:
            continue
        level_jump = abs(post[: max(2, window)].mean() - pre[-max(2, window) :].mean())
        sp = np.polyfit(np.arange(len(pre)), pre, 1)[0]
        so = np.polyfit(np.arange(len(post)), post, 1)[0]
        slope_change = abs(so - sp) * len(post)
        if slope_change >= 1.0 and slope_change > 1.5 * level_jump:
            votes.append("second")
        else:
            votes.append("first")
    if not votes:
        return "first"
    return "second" if votes.count("second") > votes.count("first") else "first"


def analyse_group_onset(ws, group: str, s: np.ndarray, rows: np.ndarray, window: int, rescore, aliases: list[str]) -> Optional[Onset]:
    """s/rows: ordered ensemble score and row ids of one group. `rescore(row_start, row_end)` returns the
    rescore_window dict (z, rstd_z, contrib...) or None."""
    est = estimate_onset(s, window)
    if est is None:
        return None
    idx = est["index"]
    onset_row = int(rows[idx])
    win_start = int(rows[max(0, idx - 2 * window)])
    win_end = int(rows[min(len(rows) - 1, idx + 6 * window)]) + 1
    first: list[dict[str, Any]] = []
    order = "first"
    res = rescore(win_start, win_end)
    if res is not None and len(res["rows"]) > 0:
        r_rows = res["rows"]
        pos = int(np.searchsorted(r_rows, onset_row))
        start = max(0, pos - window)
        resid = None
        if "corr_break" in res["contrib"]:
            resid = np.sqrt(np.maximum(res["contrib"]["corr_break"], 0))
        so = signal_onsets(res["z"], res["rstd_z"], resid, start, stop=pos + 3 * window)
        if so:
            t0 = so[0][1]
            for j, t, d in so[:6]:
                first.append({"signal": aliases[j], "lag": int(t - t0), "direction": d, "row": int(r_rows[t])})
            order = change_order(res["z"], pos, window, [j for j, _, _ in so])
    lead = ", ".join(f"{f['signal']} (lag {f['lag']:+d}, {f['direction']})" if f["lag"] else f"{f['signal']} (lag 0, {f['direction']})" for f in first[:4])
    statement = f"Onset at row {onset_row} (group {group}), {est['kind']}, {order}-order change" + (f"; first signals to move: {lead}" if lead else "") + "."
    ev = ws.evidence.add("changepoint", statement, signals=[f["signal"] for f in first[:4]], values={"row": onset_row, "kind": est["kind"], "order": order, "cusum": est["cusum"], "ruptures": est["ruptures"], "rows_to_80pct": est["rows_to_80pct"], "first_signals": first[:6]}, computed_by="detect.changepoints.analyse_group_onset", n_samples=int(len(s)), group_id=group)
    return Onset(group=group, row=onset_row, index=idx, kind=est["kind"], order=order, confidence=float(est["confidence"]), methods={"cusum": est["cusum"], "ruptures": est["ruptures"], "rows_to_80pct": est["rows_to_80pct"]}, first_signals=first, statement=statement, evidence_id=ev.id)
