"""Diagnosis assembly: one Diagnosis per (group, pattern) or per major flag.

Everything here is template-generated from contract objects (flags, patterns, propagation chains,
baseline.json, schema) and cites evidence ids. An LLM narrative is an optional, labelled enhancement
layered on top (`narrative_source`); the template steps always remain.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

from ..contracts import Diagnosis, Flag, PropagationStep, SignalContribution

CAUSE_WORDS = {"process": "a process fault", "sensor": "a sensor fault", "data": "a data-quality issue", "mixed": "a mix of process and data problems", "unknown": "an unclassified deviation"}


def _fault_type(main: Flag, pattern: Optional[dict[str, Any]], human_labels: list[dict[str, Any]]) -> tuple[str, Optional[str]]:
    """Pattern name if the operator named it, a human-labelled example if one matches (same leading signal
    and the same top-2 set), else a generic name. A human label overrides the code's cause class."""
    top = [s.signal for s in main.signals_ranked[:2]]
    for hl in reversed(human_labels):  # most recent label wins
        hs = hl.get("top_signals") or []
        if hl.get("fault_type") and hs and top and hs[0] == top[0] and set(hs[:2]) == set(top[:2]):
            if hl.get("cause_class"):
                main.likely_cause_class = str(hl["cause_class"])
            return f"{hl['fault_type']} (human-labelled example {hl.get('diagnosis_id', '')})".strip(), "human"
    if main.likely_cause_class == "sensor" and main.signals_ranked:
        return f"sensor fault on {main.signals_ranked[0].signal}", None
    if main.likely_cause_class == "data":
        sig = main.signals_ranked[0].signal if main.signals_ranked else "unknown signal"
        return f"data-quality issue on {sig}", None
    if pattern:
        if pattern.get("name"):
            return str(pattern["name"]), None
        return f"{pattern['id']} (unnamed)", None
    return f"{CAUSE_WORDS.get(main.likely_cause_class, 'deviation')} on {', '.join(top) or 'several signals'}", None


def _ranked_signals(main: Flag, onset_flag: Optional[Flag]) -> list[SignalContribution]:
    out = [sc.model_copy(deep=True) for sc in main.signals_ranked[:5]]
    if onset_flag is not None:
        lag_of = {s.signal: s.lag for s in onset_flag.signals_ranked if s.lag is not None}
        for sc in out:
            if sc.lag is None and sc.signal in lag_of:
                sc.lag = lag_of[sc.signal]
    return out


def _steps(main: Flag, ranked: list[SignalContribution], onset_flag: Optional[Flag], chain: list[PropagationStep], baseline: dict[str, Any], detect_meta: dict[str, Any], pattern: Optional[dict[str, Any]], conf: float, uncertainty: list[str]) -> list[str]:
    steps: list[str] = []
    strat = baseline.get("strategy", "unknown")
    frac = baseline.get("selected_fraction_of_sample")
    bconf = baseline.get("confidence")
    steps.append(f"What was normal: the baseline regime was learned without labels from the data itself (strategy '{strat}', {frac:.0%} of the sampled rows, confidence {bconf:.2f}); every signal's level, spread and relation to its correlated peers were measured on those rows." if isinstance(frac, (int, float)) and isinstance(bconf, (int, float)) else f"What was normal: the baseline regime was learned without labels from the data itself (strategy '{strat}').")
    dets = detect_meta.get("detectors_used") or []
    if onset_flag is not None:
        steps.append(f"When it started: {onset_flag.statement}")
    else:
        steps.append(f"When it started: the deviation is first visible at row {main.row_start} of group {main.group_id} (no separate onset estimate).")
    if ranked:
        first = ranked[0]
        steps.append(f"What changed first: {first.explanation} It carries {first.contribution:.0%} of the deviation.")
        rest = [s for s in ranked[1:] if s.contribution >= 0.05]
        if rest:
            steps.append("What followed: " + " ".join(f"{s.explanation} ({s.contribution:.0%})" for s in rest[:3]) + ".")
    if chain:
        steps.append("How it propagated: " + " ".join(st.explanation for st in chain[:4]))
    why = {
        "process": "Why we think it is a process fault: several correlated signals moved together in a way consistent with their learned lead/lag relations, and the batch's data-quality checks did not distrust them.",
        "sensor": "Why we think it is a sensor fault rather than a process fault: one signal broke away from its correlated peers (or froze) while the peers stayed consistent with each other; a real process change would move the peers too.",
        "data": "Why we think it is a data problem: the leading signal was marked untrusted by the data-quality checks of this batch (e.g. unit shift, out-of-range spike or missing block), so the deviation is more likely in the data path than in the process.",
        "mixed": "Why the cause is mixed: some of the deviating signals were distrusted by the data-quality checks while others moved consistently, so a process effect and a data problem may overlap.",
        "unknown": "Why the cause is unclassified: the contributions are spread over signals whose relations do not confirm either a single-sensor break or a coherent multi-signal process change.",
    }[main.likely_cause_class if main.likely_cause_class in ("process", "sensor", "data", "mixed") else "unknown"]
    steps.append(why)
    steps.append(f"How confident we are: {conf:.0%}. It rests on {main.score:.1f}x the calibrated threshold over {main.row_end - main.row_start + 1} rows, detectors {', '.join(dets) if dets else 'the ensemble'} scored out-of-fold (no model saw this group while fitting)." + (f" Uncertainties: {'; '.join(uncertainty[:3])}." if uncertainty else ""))
    if pattern:
        steps.append(f"Similar events: this matches {pattern['id']}{' (' + pattern['name'] + ')' if pattern.get('name') else ''}, seen {pattern.get('n_events', 0)} times in {len(pattern.get('groups_affected', []))} group(s); pattern classifier reliability {pattern.get('classifier_reliability') if pattern.get('classifier_reliability') is not None else 'n/a'}.")
    checks = {
        "sensor": f"What to check: inspect the instrument behind {ranked[0].signal if ranked else 'the leading signal'} (wiring, freeze, calibration) and compare it with its peers {', '.join(s.signal for s in ranked[1:3]) or 'in the same cluster'} before acting on the process.",
        "data": "What to check: the data path (units, scaling, transmission gaps) of the leading signal in this batch; re-run detection once the data-quality issue is fixed.",
        "process": f"What to check: the equipment or operating step that drives {', '.join(s.signal for s in ranked[:2]) or 'the leading signals'}; the first signal to move points at where the disturbance entered.",
        "mixed": "What to check: fix or exclude the distrusted signal first, then re-evaluate whether the remaining deviation persists.",
        "unknown": "What to check: trend the leading signals against their peers over the flagged window; if the deviation recurs in other groups, name the pattern so future events are typed automatically.",
    }
    steps.append(checks[main.likely_cause_class if main.likely_cause_class in checks else "unknown"])
    return steps


def build_diagnosis(ws, diag_id: str, group: str, flags: list[Flag], onset_flag: Optional[Flag], chain: list[PropagationStep], pattern: Optional[dict[str, Any]], baseline: dict[str, Any], detect_meta: dict[str, Any], schema: Any, human_labels: list[dict[str, Any]]) -> Diagnosis:
    main = max(flags, key=lambda f: f.score * (f.row_end - f.row_start + 1))
    ranked = _ranked_signals(main, onset_flag)
    fault_type, src = _fault_type(main, pattern, human_labels)
    uncertainty: list[str] = []
    if isinstance(baseline.get("confidence"), (int, float)) and baseline["confidence"] < 0.5:
        uncertainty.append(f"baseline regime is only assumed (confidence {baseline['confidence']:.2f})")
    if main.trust_context and not main.trust_context.get("trusted", True):
        uncertainty.append("the batch was marked untrusted by the data-quality checks")
    if main.confidence < 0.5:
        uncertainty.append(f"detector agreement on this event is limited (flag confidence {main.confidence:.2f})")
    if ranked and ranked[0].contribution < 0.35:
        uncertainty.append("no single signal dominates the deviation")
    if pattern and pattern.get("classifier_reliability") is not None and pattern["classifier_reliability"] < 0.6:
        uncertainty.append(f"pattern classifier reliability is low ({pattern['classifier_reliability']:.2f})")
    if main.row_end - main.row_start + 1 < 3 * int(detect_meta.get("window", 20) if isinstance(detect_meta.get("window"), int) else 20):
        uncertainty.append("the event is short")
    conf = float(np.clip(0.5 * main.confidence + 0.2 * float(baseline.get("confidence", 0.5) or 0.5) + 0.3 * (1.0 - 0.15 * len(uncertainty)), 0.05, 0.95))
    steps = _steps(main, ranked, onset_flag, chain, baseline, detect_meta, pattern, conf, uncertainty)
    assumptions = list(baseline.get("assumptions") or [])
    if schema is not None:
        assumptions += list(getattr(schema, "assumptions", []) or [])[:3]
        if getattr(schema, "sample_period_seconds", None) is None:
            assumptions.append("Sample period unknown: rows are used as time units.")
    evidence_ids: list[str] = []
    for f in flags:
        for e in f.evidence_ids:
            if e not in evidence_ids:
                evidence_ids.append(e)
    if onset_flag is not None:
        for e in onset_flag.evidence_ids:
            if e not in evidence_ids:
                evidence_ids.append(e)
    for e in (baseline.get("evidence_ids") or [])[-1:]:
        if e not in evidence_ids:
            evidence_ids.append(e)
    if pattern:
        for e in pattern.get("evidence_ids") or []:
            if e not in evidence_ids:
                evidence_ids.append(e)
    lead = ", ".join(f"{s.signal} ({s.direction}, {s.contribution:.0%})" for s in ranked[:3])
    summary = f"{fault_type} in group {group}: rows {main.row_start}-{main.row_end}, {CAUSE_WORDS.get(main.likely_cause_class, 'deviation')}; leading signals {lead}. Confidence {conf:.0%}."
    return Diagnosis(id=diag_id, flag_ids=[f.id for f in flags] + ([onset_flag.id] if onset_flag is not None else []), group_id=group, pattern_id=main.pattern_id, fault_type=fault_type, cause_class=main.likely_cause_class, ranked_signals=ranked, propagation=chain, steps=steps, summary=summary, confidence=round(conf, 3), uncertainty=uncertainty, assumptions=assumptions[:8], evidence_ids=evidence_ids, narrative_source="template" if src is None else "human+template")


def add_llm_narrative(ws, settings, diag: Diagnosis, language: str = "en") -> Diagnosis:
    """Optional LLM narrative layered on the template (never replaces the steps)."""
    try:
        from ..llm import complete
    except Exception:
        return diag
    evidence = []
    for eid in diag.evidence_ids[:12]:
        ev = ws.evidence.get(eid)
        if ev is not None:
            evidence.append({"id": ev.id, "statement": ev.statement, "n_samples": ev.n_samples})
    payload = {"diagnosis": diag.model_dump(exclude={"critique"}), "evidence": evidence}
    try:
        res = complete("diagnosis_narrative", payload, purpose=f"explain {diag.id}", ws=ws, settings=settings, language=language)
    except Exception:
        return diag
    if res is not None and res.ok and res.text and res.text.strip():
        diag.summary = diag.summary + f"\n\n[{res.source}] " + res.text.strip()[:2000]
        diag.narrative_source = f"{res.source}+template"
        ws.log.record(f"llm:{res.route}:{res.model}" if res.route in ("local", "external") else "system:diagnose", "narrative", "diagnosis", diag.id, {"source": res.source, "ledger_id": res.ledger_id}, diag.evidence_ids)
    return diag
