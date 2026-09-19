"""Per-event attribution: which signals, in which direction, in what order, and why we think the cause is a
process fault, a sensor fault or a data problem.

Sensor vs process (DECISIONS #30): a single signal breaking its relation to peers while the peers agree
-> sensor; several related signals moving together -> process. Trust verdicts (untrusted signals of the
batch) -> data.

Round 6 (expert review): before any sensor is named,
- duplicated rows in the event, or several signals frozen in the same rows -> data (the recording, not N sensors);
- a manipulated variable (valve / controller output) pinned at its limit -> actuator saturation, a process symptom,
  not a dead sensor;
- when no signal dominates the deviation the event says so ("spread over N signals") instead of a precise list.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

from ..contracts import SignalContribution
from ..profile.roles import MANIPULATED_MIN_SCORE, at_limit_side, manipulated_evidence
from .changepoints import signal_onsets

SPREAD_TOP_SHARE = 0.2    # below this share for the strongest signal, no signal dominates the deviation
DUP_EVENT_SHARE = 0.2     # share of an event's rows that are duplicated rows before the event counts as a data problem


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
    listed = [j for j in order[:top_k] if shares[j] >= 0.03 or j == order[0]]
    # every lag is measured from ONE reference: the first of the listed signals to move (not a signal outside the list)
    ref = min((j for j in listed if j in onset_t), key=lambda j: onset_t[j][0], default=None)
    t0 = onset_t[ref][0] if ref is not None else None
    lead_alias = aliases[ref] if ref is not None else None
    ranked: list[SignalContribution] = []
    stats: dict[str, dict[str, float]] = {}
    levels: dict[str, float] = {}
    for j in listed:
        alias = aliases[j]
        d, st = _direction(res["z"][hot, j], res["rstd_z"][hot, j], res["raw_rstd"][hot, j], resid[hot, j] if resid is not None else None)
        stats[alias] = st
        try:
            levels[alias] = float(np.nanmedian(res["X"][hot, j]))
        except Exception:
            pass
        lag = None
        row = None
        if j in onset_t and t0 is not None:
            lag = int(onset_t[j][0] - t0)
            row = int(rows[onset_t[j][0]])
        ranked.append(SignalContribution(signal=alias, contribution=round(float(shares[j]), 4), direction=d, lag=lag, explanation=_explain(alias, d, st, lag, lead_alias, row)))
    ev_rows = (int(row_start), int(row_end))
    cause, why, cause_conf, detail = classify_cause(ranked, stats, untrusted, fm, aliases, res, hot, corr_share, inputs=inputs, ws=ws, levels=levels, ev_rows=ev_rows)
    spread = _spread(shares, aliases, inputs)
    if spread:
        detail = {**(detail or {}), "spread": spread}
    if lead_alias:
        detail = {**(detail or {}), "lag_reference": lead_alias}
    top_sig = [r.signal for r in ranked]
    ev = ws.evidence.add("contribution", f"Rows {row_start}-{row_end - 1} (group {group}): ensemble {float(np.mean(ens[hot])):.1f}x threshold; contributions " + ", ".join(f"{r.signal} {r.contribution:.0%} ({r.direction})" for r in ranked[:4]) + f". Detector agreement {agreement:.0%}.", signals=top_sig, values={"shares": {r.signal: r.contribution for r in ranked}, "directions": {r.signal: r.direction for r in ranked}, "lags": {r.signal: r.lag for r in ranked}, "detector_agreement": det_agree, "mean_ensemble": round(float(np.mean(ens[hot])), 3), "peak_ensemble": round(float(np.max(ens[in_ev])), 3), "signal_stats": stats}, computed_by="detect.attribution.attribute_event", n_samples=int(hot.sum()), group_id=group)
    evidence_ids = [ev.id]
    for r in ranked:
        r.evidence_ids = [ev.id]
    if cause in ("sensor", "data", "mixed") or (detail or {}).get("kind") == "actuator_saturation":
        ev2 = ws.evidence.add("cause", f"Cause class '{cause}' for rows {row_start}-{row_end - 1} (group {group}): {why}", signals=top_sig[:2], values={"cause": cause, "confidence": cause_conf, "untrusted": sorted(untrusted), "detail": detail}, computed_by="detect.attribution.classify_cause", n_samples=int(hot.sum()), group_id=group)
        evidence_ids.append(ev2.id)
    return {"ranked": ranked, "cause": cause, "cause_reason": why, "cause_confidence": cause_conf, "cause_detail": detail, "detector_agreement": det_agree, "agreement": agreement, "mean_ensemble": float(np.mean(ens[hot])), "peak_ensemble": float(np.max(ens[in_ev])), "evidence_ids": evidence_ids, "stats": stats, "lead_signal": lead_alias, "shares": shares}


def _related(a: str, b: str, inputs) -> bool:
    rel = (getattr(inputs, "relations", None) or {}) if inputs is not None else {}
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


def _fp_of(inputs, alias: str) -> dict[str, Any]:
    m = getattr(inputs, "_fp_by_alias", None)
    if m is None:
        m = {}
        for s in getattr(inputs, "signals", None) or []:
            d = s if isinstance(s, dict) else (s.model_dump() if hasattr(s, "model_dump") else {})
            if d.get("id"):
                m[str(d["id"])] = d.get("fingerprint") or {}
        try:
            inputs._fp_by_alias = m
        except Exception:
            pass
    return m.get(alias) or {}


def is_actuator(inputs, alias: str) -> bool:
    """Manipulated variable (valve position / controller output): the profile's role, or - for runs profiled before
    round 6 - the same evidence computed from the stored fingerprint and relations."""
    if inputs is None:
        return False
    if (getattr(inputs, "roles", None) or {}).get(alias) == "actuator_like":
        return True
    fp = _fp_of(inputs, alias)
    if not fp:
        return False
    m = fp.get("manipulated")
    score = float(m.get("score") or 0.0) if isinstance(m, dict) else manipulated_evidence(alias, fp, getattr(inputs, "relations", None))[0]
    return score >= MANIPULATED_MIN_SCORE


def _duplicate_share(ws, inputs, ev_rows: tuple[int, int]) -> float:
    """Share of the event's rows inside duplicated-row stretches found by the data-quality stage."""
    if ws is None or inputs is None:
        return 0.0
    ranges = getattr(inputs, "_dup_ranges", None)
    if ranges is None:
        ranges = []
        try:
            for c in ws.checks():
                if c.check_type in ("duplicate_rows", "duplicate_key") and c.status in ("warn", "fail"):
                    for ev in (c.values or {}).get("events") or []:
                        if isinstance(ev, (list, tuple)) and len(ev) >= 2:
                            ranges.append((int(ev[0]), int(ev[1])))
        except Exception:
            ranges = []
        try:
            inputs._dup_ranges = ranges
        except Exception:
            pass
    a, b = ev_rows
    n = max(1, b - a)
    covered = sum(max(0, min(b, e + 1) - max(a, s)) for s, e in ranges)
    return float(min(1.0, covered / n))


def _spread(shares: np.ndarray, aliases: list[str], inputs) -> Optional[dict[str, Any]]:
    """When no signal dominates, describe the group instead of a falsely precise ranking."""
    order = np.argsort(-shares)
    top = float(shares[order[0]]) if len(order) else 0.0
    if top >= SPREAD_TOP_SHARE:
        return None
    cum = np.cumsum(shares[order])
    n_half = int(np.searchsorted(cum, 0.5) + 1)
    members = [aliases[j] for j in order[:max(5, n_half)]]
    cluster, best = None, 0
    for cid, mem in ((getattr(inputs, "relations", None) or {}).get("clusters") or {}).items():
        k = len(set(mem) & set(members))
        if k > best:
            cluster, best = cid, k
    return {"top_share": round(top, 3), "top5_share": round(float(cum[min(4, len(cum) - 1)]), 3), "n_signals_for_half": n_half, "cluster": cluster if best >= 2 else None, "cluster_members_in_event": best}


def classify_cause(ranked: list[SignalContribution], stats: dict[str, dict[str, float]], untrusted: set[str], fm, aliases: list[str], res: dict[str, Any], hot: np.ndarray, corr_share: Optional[np.ndarray], inputs=None, ws=None, levels: Optional[dict[str, float]] = None, ev_rows: Optional[tuple[int, int]] = None) -> tuple[str, str, float, dict[str, Any]]:
    if not ranked:
        return "unknown", "no signal contributions", 0.2, {"kind": "none"}
    inputs = inputs if inputs is not None else getattr(fm, "_inputs", None)
    top = ranked[0]
    # 1. the recording itself: duplicated rows inside the event
    if ev_rows is not None:
        dup = _duplicate_share(ws, inputs, ev_rows)
        if dup >= DUP_EVENT_SHARE:
            return "data", f"{dup:.0%} of these rows are exact duplicates of other rows: the recording repeated itself, so this is a data problem, not a process or sensor event", 0.8, {"kind": "duplicate_rows", "duplicate_share": round(dup, 3)}
    if top.signal in untrusted:
        return "data", f"{top.signal} was marked untrusted by the data-quality checks of this batch", 0.8, {"kind": "untrusted_signal", "signal": top.signal}
    stuck = [r for r in ranked if r.direction == "stuck"]
    # 2. a manipulated variable pinned at its limit: the controller ran out of room (process symptom)
    for r in stuck:
        lvl = (levels or {}).get(r.signal)
        if lvl is not None and is_actuator(inputs, r.signal):
            side = at_limit_side(_fp_of(inputs, r.signal), lvl)
            if side:
                others = [x.signal for x in ranked if x.signal != r.signal][:3]
                why = (f"{r.signal} is a manipulated variable (valve position / controller output) pinned at its {side} limit ({lvl:.4g}): "
                       f"the controller has run out of room. That is a symptom of a process problem (for example a lost feed or a blocked line), not a dead sensor"
                       + (f"; {', '.join(others)} changed with it" if others else ""))
                return "process", why, 0.7, {"kind": "actuator_saturation", "signal": r.signal, "side": side, "level": round(lvl, 4), "with": others}
    # 3. several signals frozen in the same rows: the recording or a shared feed, not N broken sensors
    if len(stuck) >= 2:
        names = [r.signal for r in stuck]
        related = any(_related(a, b, inputs) for i, a in enumerate(names) for b in names[i + 1:]) if inputs is not None else False
        why = (f"{len(names)} signals ({', '.join(names[:5])}) froze in the same rows"
               + (" although they are not related to each other" if not related else " together")
               + f": that points to the recording or a shared data feed (for example a historian repeating the last values), not to {len(names)} broken sensors")
        return "data", why, 0.75 if not related else 0.6, {"kind": "common_freeze", "signals": names, "related": related}
    if top.direction == "stuck":
        return "sensor", f"{top.signal} is frozen at a constant value while its correlated peers keep moving", 0.8, {"kind": "single_sensor", "signal": top.signal, "how": "frozen"}
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
                return "sensor", f"{top.signal} broke its relation to its peers ({names}) while the peers stayed consistent with each other", 0.7, {"kind": "single_sensor", "signal": top.signal, "how": "relation_break"}
    strong = [r for r in ranked if r.contribution >= 0.12]
    inputs_like = inputs
    if len(strong) >= 2:
        rel_pairs = 0
        if inputs_like is not None:
            for a in strong:
                for b in strong:
                    if a.signal < b.signal and _related(a.signal, b.signal, inputs_like):
                        rel_pairs += 1
        if rel_pairs > 0:
            return "process", f"{len(strong)} correlated signals ({', '.join(r.signal for r in strong[:4])}) moved together", 0.7, {"kind": "related_move", "signals": [r.signal for r in strong]}
        if any(r.signal in untrusted for r in strong):
            return "mixed", "several signals moved, at least one of them marked untrusted in this batch", 0.5, {"kind": "mixed"}
        return "process", f"{len(strong)} signals ({', '.join(r.signal for r in strong[:4])}) moved together", 0.55, {"kind": "joint_move", "signals": [r.signal for r in strong]}
    if any(r.signal in untrusted for r in ranked[:3]):
        return "mixed", "an untrusted signal is among the top contributors", 0.45, {"kind": "mixed"}
    if top.contribution >= 0.6 and top.direction in ("noisy",):
        return "sensor", f"{top.signal} alone became noisy while its peers stayed normal", 0.5, {"kind": "single_sensor", "signal": top.signal, "how": "noisy"}
    if top.contribution >= 0.6:
        return "unknown", f"{top.signal} dominates the deviation but its peers do not confirm a relation break", 0.4, {"kind": "unconfirmed"}
    return "unknown", "contributions are spread over several weakly related signals", 0.35, {"kind": "unconfirmed"}
