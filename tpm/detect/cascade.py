"""Propagation chains and cascade (sequential failure) detection.

For each event the deviating signals are ordered by onset lag; consecutive signals form PropagationSteps
whose strength is the learned correlation and whose lag is checked against the lead/lag structure in
relations.json. When the chain crosses signal clusters with a clear delay, the event is a cascade: a
`cascade` Flag carries the chain as evidence.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

from ..contracts import Flag, PropagationStep, SignalContribution


def _corr_of(inputs, a: str, b: str) -> tuple[float, Optional[int]]:
    rel = inputs.relations or {}
    for pr in rel.get("pairs") or []:
        if pr["a"] == a and pr["b"] == b:
            return float(pr.get("r", 0.0)), int(pr.get("lag", 0))
        if pr["a"] == b and pr["b"] == a:
            return float(pr.get("r", 0.0)), -int(pr.get("lag", 0))
    corr = rel.get("corr") or {}
    try:
        return float(corr[a][b]), None
    except Exception:
        return 0.0, None


def cluster_of(inputs) -> dict[str, str]:
    out: dict[str, str] = {}
    for cid, members in ((inputs.relations or {}).get("clusters") or {}).items():
        for m in members:
            out[m] = cid
    for s in inputs.signals or []:
        if s.id not in out and getattr(s, "cluster_id", None):
            out[s.id] = str(s.cluster_id)
    return out


def _step_words(a: str, b: str, r: float, rel_lag: Optional[int], delta: Optional[int]) -> str:
    """Upstream / downstream in words, from the learned lead/lag (normal operation) and the observed order."""
    learned = (f"{a} is upstream of {b}: in normal operation {b} follows {a} by {rel_lag} sample(s) (r={r:.2f})" if rel_lag and rel_lag > 0
               else f"{b} is upstream of {a}: in normal operation {a} follows {b} by {-rel_lag} sample(s) (r={r:.2f})" if rel_lag and rel_lag < 0
               else f"{a} and {b} normally move together (r={r:.2f})")
    if delta is None:
        return learned + "; the order in this event could not be measured."
    return learned + (f"; here {b} moved {delta} sample(s) after {a}." if delta > 0 else f"; here they moved together.")


def chain_for_flag(inputs, flag: Flag, window: int) -> list[PropagationStep]:
    """Each signal (in onset order) is linked to the nearest EARLIER signal it has a learned relation with, so a
    chain is not broken by an unrelated signal in between; the lag direction becomes an upstream/downstream
    statement. Steps whose learned lead/lag contradicts the observed order are dropped."""
    ordered = sorted([s for s in flag.signals_ranked if s.lag is not None], key=lambda s: s.lag)
    steps: list[PropagationStep] = []
    tol = max(2, window // 2)
    for j in range(1, len(ordered)):
        b = ordered[j]
        for a in reversed(ordered[:j]):
            r, rel_lag = _corr_of(inputs, a.signal, b.signal)
            if abs(r) < 0.3:
                continue  # no learned relation: their order alone is not a propagation claim
            delta = int(b.lag - a.lag)
            consistent = None
            if rel_lag is not None and delta > 0:
                consistent = (rel_lag > 0 and abs(rel_lag - delta) <= tol) or (rel_lag == 0 and delta <= tol)
            if consistent is False:
                continue  # the learned lead/lag contradicts the observed order
            steps.append(PropagationStep(from_signal=a.signal, to_signal=b.signal, lag=delta, strength=round(float(abs(r)), 3), explanation=_step_words(a.signal, b.signal, r, rel_lag, delta), evidence_ids=list(flag.evidence_ids[:1])))
            break
    if not steps and len(ordered) < 2:
        # no measurable order in the event (a contradicted order is not replaced): say what is upstream of what
        names = [s.signal for s in flag.signals_ranked[:5]]
        for i, x in enumerate(names):
            for y in names[i + 1:]:
                r, rel_lag = _corr_of(inputs, x, y)
                if abs(r) >= 0.5 and rel_lag:
                    up, down, lag = (x, y, rel_lag) if rel_lag > 0 else (y, x, -rel_lag)
                    steps.append(PropagationStep(from_signal=up, to_signal=down, lag=None, strength=round(float(abs(r)), 3), explanation=_step_words(up, down, r, lag, None) + " (learned order, not observed in this event)", evidence_ids=list(flag.evidence_ids[:1])))
        steps = steps[:3]
    return steps


def build_cascades(ws, inputs, flags: list[Flag], ids, settings) -> tuple[list[Flag], dict[str, list[PropagationStep]]]:
    window = int(settings.detect.window)
    cl = cluster_of(inputs)
    chains: dict[str, list[PropagationStep]] = {}
    out: list[Flag] = []
    for f in flags:
        if f.kind not in ("anomaly", "drift"):
            continue
        steps = chain_for_flag(inputs, f, window)
        if steps:
            chains[f.id] = steps
        ordered = sorted([s for s in f.signals_ranked if s.lag is not None], key=lambda s: s.lag)
        if len(ordered) < 2:
            continue
        # first arrival per cluster
        first_by_cluster: dict[str, SignalContribution] = {}
        for s in ordered:
            c = cl.get(s.signal, f"single:{s.signal}")
            if c not in first_by_cluster:
                first_by_cluster[c] = s
        if len(first_by_cluster) < 2:
            continue
        arrivals = sorted(first_by_cluster.items(), key=lambda kv: kv[1].lag)
        gaps = [int(b[1].lag - a[1].lag) for a, b in zip(arrivals[:-1], arrivals[1:])]
        if max(gaps) < 2:
            continue
        # a cascade needs at least one propagation step backed by a learned relation (or a lead/lag that
        # matches the learned one); an ordering between unrelated signals is kept as a chain for the
        # diagnosis but is not a sequential-failure flag on its own
        backed = [st for st in steps if st.strength >= 0.3 or "consistent with the observed order" in st.explanation]
        if not backed:
            continue
        parts = []
        for (c, s) in arrivals:
            members = [x.signal for x in ordered if cl.get(x.signal, f"single:{x.signal}") == c]
            parts.append(f"{c} ({', '.join(members[:3])}) at lag {s.lag:+d}")
        stmt = f"Sequential failure in group {f.group_id}, rows {f.row_start}-{f.row_end}: " + " -> ".join(parts) + f". The deviation spread across {len(arrivals)} signal clusters with delays of {', '.join(str(g) for g in gaps)} sample(s)."
        ev = ws.evidence.add("cascade", stmt, signals=[s.signal for _, s in arrivals], values={"chain": [st.model_dump() for st in steps], "cluster_order": [c for c, _ in arrivals], "gaps": gaps, "parent_flag": f.id}, computed_by="detect.cascade.build_cascades", n_samples=int(f.row_end - f.row_start + 1), group_id=f.group_id)
        conf = float(np.clip(0.3 + 0.1 * len(arrivals) + 0.2 * np.mean([st.strength >= 0.3 for st in steps]), 0.1, 0.85))
        out.append(Flag(id=ids.next(), kind="cascade", batch_id=f.batch_id, group_id=f.group_id, row_start=f.row_start, row_end=f.row_end, severity=min(1.0, f.severity + 0.1), score=f.score, threshold=f.threshold, detector="cascade:lag-order", statement=stmt, signals_ranked=[s for _, s in arrivals], evidence_ids=[ev.id] + list(f.evidence_ids[:1]), likely_cause_class="process" if f.likely_cause_class in ("process", "unknown") else f.likely_cause_class, confidence=round(conf, 3), trust_context=f.trust_context))
        chains[out[-1].id] = steps
    return out, chains
