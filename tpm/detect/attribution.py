"""Per-event attribution: which signals, in which direction, in what order, and why we think the cause is a
process fault, a sensor fault or a data problem.

Sensor vs process (DECISIONS #30): a single signal breaking its relation to peers while the peers agree
-> sensor; several related signals moving together -> process. Trust verdicts (untrusted signals of the
batch) -> data.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

from ..contracts import SignalContribution
from .changepoints import signal_onsets


def _direction(zc: np.ndarray, rstd_zc: np.ndarray, raw_rstd_c: np.ndarray, resid_c: Optional[np.ndarray]) -> tuple[str, dict[str, float]]:
    mz = float(np.mean(zc))
    mr = float(np.mean(rstd_zc))
    frozen = float(np.mean(raw_rstd_c <= 1e-6)) if len(raw_rstd_c) else 0.0
    rr = float(np.mean(np.abs(resid_c))) if resid_c is not None else 0.0
    resid_sd = float(np.std(resid_c)) if resid_c is not None and len(resid_c) > 3 else 0.0
    resid_mean = float(np.mean(resid_c)) if resid_c is not None else 0.0
    stats = {"mean_z": round(mz, 2), "mean_rstd_z": round(mr, 2), "frozen_fraction": round(frozen, 2), "mean_abs_resid": round(rr, 2), "resid_sd": round(resid_sd, 2)}
    if frozen >= 0.6:
        return "stuck", stats
    if (mr > 2.0 and abs(mz) < 1.5) or (resid_sd > 2.5 and abs(resid_mean) < 0.5 * resid_sd and abs(mz) < 1.5):
        return "noisy", stats
    if mz >= 1.0:
        return "up", stats
    if mz <= -1.0:
        return "down", stats
    if rr >= 2.0:
        return "shifted", stats
    if abs(mz) >= 0.5:
        return "up" if mz > 0 else "down", stats
    return "shifted", stats


def _explain(alias: str, direction: str, st: dict[str, float], lag: Optional[int], lead: Optional[str], row: Optional[int]) -> str:
    if direction == "up":
        core = f"{alias} rose {abs(st['mean_z']):.1f} sigma above its normal level"
    elif direction == "down":
        core = f"{alias} fell {abs(st['mean_z']):.1f} sigma below its normal level"
    elif direction == "stuck":
        core = f"{alias} is frozen at one value (rolling spread 0 for {st['frozen_fraction']:.0%} of the event) while its level stayed in range"
    elif direction == "noisy":
        core = f"{alias} became much noisier than normal (rolling spread {st['mean_rstd_z']:.1f} sigma above baseline, residual spread {st.get('resid_sd', 0):.1f})"
    else:
        core = f"{alias} disagrees with its correlated peers (residual {st['mean_abs_resid']:.1f} sigma) although its own level is within range"
    if row is not None:
        core += f", starting at row {row}"
    if lag is not None and lead and lead != alias and lag > 0:
        core += f", {lag} samples after {lead}"
    elif lag == 0 and lead == alias:
        core += " (first to move)"
    return core + "."


def attribute_event(ws, inputs, fm, res: dict[str, Any], row_start: int, row_end: int, group: str, untrusted: set[str], top_k: int = 5) -> dict[str, Any]:
    """res = rescore_window(...) for a window covering the event. Returns ranked SignalContributions, cause
    class, per-detector agreement and evidence ids."""
    aliases = inputs.aliases
    rows = res["rows"]
    in_ev = (rows >= row_start) & (rows < row_end)
    if not in_ev.any():
        in_ev = np.ones(len(rows), dtype=bool)
    ens = res["ensemble"]
    hot = in_ev & (ens >= 1.0)
    if hot.sum() < 3:
        hot = in_ev
    shares = res["shares"][hot].mean(axis=0)
    shares = shares / max(shares.sum(), 1e-9)
    order = np.argsort(-shares)
    resid = np.sqrt(np.maximum(res["contrib"]["corr_break"], 0)) if "corr_break" in res["contrib"] else None
    corr_share = None
    if resid is not None:
        c = res["contrib"]["corr_break"][hot].mean(axis=0)
        corr_share = c / max(c.sum(), 1e-9)
    # detector agreement inside the event
    det_agree = {n: float(np.mean(res["norm"][n][hot] >= 1.0)) for n in res["norm"]}
    agreement = float(np.mean([v >= 0.5 for v in det_agree.values()])) if det_agree else 0.0
    # onset order inside the window
    start = int(np.argmax(in_ev))
    so = signal_onsets(res["z"], res["rstd_z"], resid, max(0, start - fm.spec.window), stop=start + 3 * fm.spec.window)
    onset_t = {j: (t, d) for j, t, d in so}
    t0 = so[0][1] if so else None
    lead_alias = aliases[so[0][0]] if so else None
    ranked: list[SignalContribution] = []
    stats: dict[str, dict[str, float]] = {}
    for j in order[:top_k]:
        if shares[j] < 0.03 and ranked:
            break
        alias = aliases[j]
        d, st = _direction(res["z"][hot, j], res["rstd_z"][hot, j], res["raw_rstd"][hot, j], resid[hot, j] if resid is not None else None)
        stats[alias] = st
        lag = None
        row = None
        if j in onset_t and t0 is not None:
            lag = int(onset_t[j][0] - t0)
            row = int(rows[onset_t[j][0]])
        ranked.append(SignalContribution(signal=alias, contribution=round(float(shares[j]), 4), direction=d, lag=lag, explanation=_explain(alias, d, st, lag, lead_alias, row)))
    cause, why, cause_conf = classify_cause(ranked, stats, untrusted, fm, aliases, res, hot, corr_share)
    top_sig = [r.signal for r in ranked]
    ev = ws.evidence.add("contribution", f"Rows {row_start}-{row_end - 1} (group {group}): ensemble {float(np.mean(ens[hot])):.1f}x threshold; contributions " + ", ".join(f"{r.signal} {r.contribution:.0%} ({r.direction})" for r in ranked[:4]) + f". Detector agreement {agreement:.0%}.", signals=top_sig, values={"shares": {r.signal: r.contribution for r in ranked}, "directions": {r.signal: r.direction for r in ranked}, "lags": {r.signal: r.lag for r in ranked}, "detector_agreement": det_agree, "mean_ensemble": round(float(np.mean(ens[hot])), 3), "peak_ensemble": round(float(np.max(ens[in_ev])), 3), "signal_stats": stats}, computed_by="detect.attribution.attribute_event", n_samples=int(hot.sum()), group_id=group)
    evidence_ids = [ev.id]
    for r in ranked:
        r.evidence_ids = [ev.id]
    if cause in ("sensor", "data", "mixed"):
        ev2 = ws.evidence.add("cause", f"Cause class '{cause}' for rows {row_start}-{row_end - 1} (group {group}): {why}", signals=top_sig[:2], values={"cause": cause, "confidence": cause_conf, "untrusted": sorted(untrusted)}, computed_by="detect.attribution.classify_cause", n_samples=int(hot.sum()), group_id=group)
        evidence_ids.append(ev2.id)
    return {"ranked": ranked, "cause": cause, "cause_reason": why, "cause_confidence": cause_conf, "detector_agreement": det_agree, "agreement": agreement, "mean_ensemble": float(np.mean(ens[hot])), "peak_ensemble": float(np.max(ens[in_ev])), "evidence_ids": evidence_ids, "stats": stats, "lead_signal": lead_alias, "shares": shares}


def _related(a: str, b: str, inputs) -> bool:
    rel = inputs.relations or {}
    for members in (rel.get("clusters") or {}).values():
        if a in members and b in members:
            return True
    for pr in rel.get("pairs") or []:
        if {pr["a"], pr["b"]} == {a, b} and abs(pr.get("r", 0)) >= 0.4:
            return True
    corr = rel.get("corr") or {}
    try:
        return abs(float(corr[a][b])) >= 0.4
    except Exception:
        return False


def classify_cause(ranked: list[SignalContribution], stats: dict[str, dict[str, float]], untrusted: set[str], fm, aliases: list[str], res: dict[str, Any], hot: np.ndarray, corr_share: Optional[np.ndarray]) -> tuple[str, str, float]:
    if not ranked:
        return "unknown", "no signal contributions", 0.2
    top = ranked[0]
    if top.signal in untrusted:
        return "data", f"{top.signal} was marked untrusted by the data-quality checks of this batch", 0.8
    if top.direction == "stuck":
        return "sensor", f"{top.signal} is frozen at a constant value while its correlated peers keep moving", 0.8
    # single-signal disagreement with peers (peers themselves quiet)
    cb = fm.detectors.get("corr_break")
    idx = {a: i for i, a in enumerate(aliases)}
    if cb is not None and getattr(cb, "fitted", False) and corr_share is not None:
        j = idx[top.signal]
        peers = cb.peers[j] if j < len(cb.peers) else []
        if peers and top.contribution >= 0.45 and corr_share[j] >= 0.5 and stats[top.signal]["mean_abs_resid"] >= 3.0:
            peer_z = float(np.mean(np.abs(res["z"][hot][:, peers])))
            peer_res = float(np.mean(np.sqrt(np.maximum(res["contrib"]["corr_break"][hot][:, peers], 0))))
            if peer_z < 1.5 and peer_res < 2.0:
                names = ", ".join(aliases[k] for k in peers[:4])
                return "sensor", f"{top.signal} broke its relation to its peers ({names}) while the peers stayed consistent with each other", 0.7
    strong = [r for r in ranked if r.contribution >= 0.12]
    inputs_like = getattr(fm, "_inputs", None)
    if len(strong) >= 2:
        rel_pairs = 0
        if inputs_like is not None:
            for a in strong:
                for b in strong:
                    if a.signal < b.signal and _related(a.signal, b.signal, inputs_like):
                        rel_pairs += 1
        if rel_pairs > 0:
            return "process", f"{len(strong)} correlated signals ({', '.join(r.signal for r in strong[:4])}) moved together", 0.7
        if any(r.signal in untrusted for r in strong):
            return "mixed", "several signals moved, at least one of them marked untrusted in this batch", 0.5
        return "process", f"{len(strong)} signals ({', '.join(r.signal for r in strong[:4])}) moved together", 0.55
    if any(r.signal in untrusted for r in ranked[:3]):
        return "mixed", "an untrusted signal is among the top contributors", 0.45
    if top.contribution >= 0.6 and top.direction in ("noisy",):
        return "sensor", f"{top.signal} alone became noisy while its peers stayed normal", 0.5
    if top.contribution >= 0.6:
        return "unknown", f"{top.signal} dominates the deviation but its peers do not confirm a relation break", 0.4
    return "unknown", "contributions are spread over several weakly related signals", 0.35
