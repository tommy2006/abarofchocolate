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


RECORD_WORDS = [
    ("sensor fault on", "entry problem in column"), ("a sensor fault", "an entry problem in one column"),
    ("froze at one value", "repeats the same value"), ("is frozen at a constant value", "repeats the same value"),
    ("an instrument problem", "an entry or export problem in one column"), ("faulty instrument", "faulty entries"),
    ("inspect the instrument behind", "check how the values of"), ("(wiring, freeze, calibration)", "are entered or exported"),
    ("a process fault", "a change in the business process"), ("the process itself", "the business process itself"),
]


def is_sensor_data(ws) -> bool:
    """True unless the profile says the table is clearly not a sensor stream (business records, event logs)."""
    try:
        dl = (ws.read_json("domain") or {}).get("domain_likelihood") or {}
    except Exception:
        return True
    return not dl or float(dl.get("sensor_stream", 1.0)) >= 0.4


def speak_domain(text: str, sensor: bool) -> str:
    if sensor or not text:
        return text
    for a, b in RECORD_WORDS:
        text = text.replace(a, b)
    return text


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
    det = main.cause_detail or {}
    kind = det.get("kind")
    if kind == "actuator_saturation":
        return f"actuator saturation: {det.get('signal')} pinned at its {det.get('side', 'upper')} limit", None
    if kind == "common_freeze":
        return f"data problem: {len(det.get('signals') or [])} signals frozen in the same rows", None
    if kind == "duplicate_rows":
        return "data problem: duplicated rows", None
    if main.likely_cause_class == "sensor" and main.signals_ranked:
        return f"sensor fault on {main.signals_ranked[0].signal}", None
    if main.likely_cause_class == "data":
        sig = main.signals_ranked[0].signal if main.signals_ranked else "unknown signal"
        return f"data-quality issue on {sig}", None
    if pattern:
        if pattern.get("name"):
            return str(pattern["name"]), None
        hyp = pattern.get("hypothesis") or {}
        if hyp.get("named"):
            return f"{pattern['id']}: possibly {hyp.get('name')}", None
        return f"{pattern['id']} ({'cannot name' if hyp else 'unnamed'})", None
    spread = det.get("spread")
    if spread:
        grp = f" (cluster {spread['cluster']})" if spread.get("cluster") else ""
        return f"{CAUSE_WORDS.get(main.likely_cause_class, 'deviation')} spread over about {spread.get('n_signals_for_half', '?')} signals{grp}", None
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
    det = main.cause_detail or {}
    spread = det.get("spread")
    if spread:
        grp = f", mostly in cluster {spread['cluster']}" if spread.get("cluster") else ""
        steps.append(f"What changed: no single signal dominates. The strongest one carries only {spread['top_share']:.0%} of the deviation and the five strongest {spread['top5_share']:.0%}; about {spread['n_signals_for_half']} signals are needed to explain half of it{grp}. Treat the ranking below as a group, not as a precise order.")
    if ranked:
        first = ranked[0]
        ref = det.get("lag_reference")
        steps.append(f"What changed first: {first.explanation} It carries {first.contribution:.0%} of the deviation." + (f" Lags are counted from {ref}, the first of these signals to move." if ref else ""))
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
    kind = det.get("kind")
    if kind == "actuator_saturation":
        why = f"Why we think it is a process problem and not a broken instrument: {det.get('signal')} behaves like a manipulated variable (a valve position or controller output) and sits at its {det.get('side', 'upper')} limit. A controller drives its valve to the limit when it can no longer hold its target, so the frozen value is saturation, a symptom of the process (for example a lost feed), not a dead sensor."
    elif kind == "common_freeze":
        why = f"Why we think it is a data problem: {len(det.get('signals') or [])} signals ({', '.join((det.get('signals') or [])[:5])}) froze in the same rows" + (" although they are not related to each other" if not det.get("related") else "") + ". Independent instruments do not all fail at the same moment; the recording or a shared data feed stopped updating. No single sensor is named for that reason."
    elif kind == "duplicate_rows":
        why = f"Why we think it is a data problem: {float(det.get('duplicate_share') or 0):.0%} of these rows are exact copies of other rows, so the recording repeated itself."
    steps.append(why)
    steps.append(f"How confident we are: {conf:.0%} (a heuristic score, not a calibrated probability: it combines detector agreement, how far and how long the score stayed above the threshold, and the baseline's confidence, minus a share for every uncertainty listed). It rests on {main.score:.1f}x the calibrated threshold over {main.row_end - main.row_start + 1} rows, detectors {', '.join(dets) if dets else 'the ensemble'} scored out-of-fold (no model saw this group while fitting)." + (f" Uncertainties: {'; '.join(uncertainty[:3])}." if uncertainty else ""))
    if pattern:
        steps.append(f"Similar events: this matches {pattern['id']}{' (' + pattern['name'] + ')' if pattern.get('name') else ''}, seen {pattern.get('n_events', 0)} times in {len(pattern.get('groups_affected', []))} group(s); pattern classifier reliability {pattern.get('classifier_reliability') if pattern.get('classifier_reliability') is not None else 'n/a'}.")
        if (pattern.get("hypothesis") or {}).get("text"):
            steps.append(f"Which known failure type: {pattern['hypothesis']['text']}.")
    checks = {
        "sensor": f"What to check: inspect the instrument behind {ranked[0].signal if ranked else 'the leading signal'} (wiring, freeze, calibration) and compare it with its peers {', '.join(s.signal for s in ranked[1:3]) or 'in the same cluster'} before acting on the process.",
        "data": "What to check: the data path (units, scaling, transmission gaps) of the leading signal in this batch; re-run detection once the data-quality issue is fixed.",
        "process": f"What to check: the equipment or operating step that drives {', '.join(s.signal for s in ranked[:2]) or 'the leading signals'}; the first signal to move points at where the disturbance entered.",
        "mixed": "What to check: fix or exclude the distrusted signal first, then re-evaluate whether the remaining deviation persists.",
        "unknown": "What to check: trend the leading signals against their peers over the flagged window; if the deviation recurs in other groups, name the pattern so future events are typed automatically.",
    }
    if kind == "actuator_saturation":
        steps.append(f"What to check: why the controller had to drive {det.get('signal')} to its limit. Look upstream first: the supply that feeds it (a lost feed, a blocked line, a closed manual valve), then the controller's setpoint and tuning. The valve itself is doing its job; replacing its sensor would not help.")
    elif kind == "common_freeze":
        steps.append("What to check: the data recording for these rows (historian, network link, export job). Do not replace any sensor on this evidence. Exclude or refill these rows and run the analysis again.")
    elif kind == "duplicate_rows":
        steps.append("What to check: the export or logging step that wrote the same rows twice. Remove the duplicates and run the analysis again.")
    else:
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
    summary = plain_summary(fault_type, group, main, ranked, conf, chain)
    sensor = is_sensor_data(ws)
    if not sensor:  # business records / event logs: no sensors, instruments or process units in the wording
        fault_type, summary = speak_domain(fault_type, False), speak_domain(summary, False)
        steps = [speak_domain(x, False) for x in steps]
    return Diagnosis(id=diag_id, flag_ids=[f.id for f in flags] + ([onset_flag.id] if onset_flag is not None else []), group_id=group, pattern_id=main.pattern_id, fault_type=fault_type, cause_class=main.likely_cause_class, cause_detail=main.cause_detail, ranked_signals=ranked, propagation=chain, steps=steps, summary=summary, confidence=round(conf, 3), uncertainty=uncertainty, assumptions=assumptions[:8], evidence_ids=evidence_ids, narrative_source="template" if src is None else "human+template")


def build_point_diagnosis(ws, diag_id: str, point_flags: list[Flag], suspicious: Optional[dict[str, Any]]) -> Diagnosis:
    """All isolated suspicious readings of a run in ONE diagnosis. No onset, recurring-pattern or propagation
    analysis: those need sustained events. The cause is stated as undecidable from the data."""
    n = len(point_flags)
    groups = sorted({f.group_id for f in point_flags if f.group_id})
    per_sig: dict[str, dict[str, float]] = {}
    for f in point_flags:
        for sc in f.signals_ranked[:3]:
            d = per_sig.setdefault(sc.signal, {"n": 0, "max": 0.0})
            d["n"] += 1
            try:
                d["max"] = max(d["max"], float(sc.explanation.split(" was ")[1].split(" times")[0]))
            except Exception:
                pass
    top = sorted(per_sig.items(), key=lambda kv: (-kv[1]["n"], -kv[1]["max"]))[:5]
    ranked = [SignalContribution(signal=a, contribution=round(v["n"] / max(1, n), 3), direction="deviating", lag=None, explanation=f"{a} is involved in {int(v['n'])} of the {n} isolated readings" + (f" (largest deviation {v['max']:.0f} times its normal spread)." if v["max"] else ".")) for a, v in top]
    regime = (suspicious or {}).get("regime") or {}
    n_rows = (suspicious or {}).get("n_rows") or n
    strongest = sorted(point_flags, key=lambda f: -f.score)[:5]
    rows_txt = ", ".join(f"row {f.row_start}" if f.row_end == f.row_start else f"rows {f.row_start}-{f.row_end}" for f in strongest)
    steps = [
        f"What was found: {n} isolated reading(s) in {len(groups) or 1} group(s) that the detectors score far above anything seen in normal operation, each lasting one or two rows (strongest: {rows_txt}). Together with the data-quality checks, {n_rows} rows are on the list of suspicious rows.",
        "Why these are not treated as process events: a real process change moves related signals and lasts; here the readings before and after each of these rows look normal, and the value returns immediately.",
        ("Which signals: " + "; ".join(r.explanation.rstrip(".") for r in ranked[:3]) + ".") if ranked else "Which signals: no single signal dominates.",
        "What it could be: a glitch (sensor, transmission or entry error) or a deliberate manipulation. The data alone cannot tell which.",
        "What was deliberately not done: no onset, recurring-pattern or propagation analysis. Those describe sustained events and would be meaningless for single readings.",
        "What to check: compare the listed rows with maintenance and calibration logs, operator entries and access records. If the same signal keeps recurring, inspect that instrument and its wiring or transmission. If the odd values are plausible-looking and fall at moments that matter commercially or for safety, treat manipulation as a real possibility and escalate.",
    ]
    if regime.get("point_dominated"):
        steps.insert(2, f"How common this is here: {regime.get('share_points', 0):.0%} of all above-threshold stretches in this data are isolated readings ({regime.get('n_point_stretches')} isolated, {regime.get('n_sustained_stretches')} sustained).")
    conf = float(np.clip(np.mean([f.confidence for f in point_flags]) if point_flags else 0.3, 0.1, 0.85))
    summary = (f"The monitor found {n} isolated suspicious reading(s): single rows where a value does not fit its surroundings and comes straight back. "
               + (f"The signals most often involved are {', '.join(r.signal for r in ranked[:3])}. " if ranked else "")
               + "Each is either a glitch or a deliberate manipulation; the data alone cannot tell which, so no process fault is claimed. "
               + f"We are {_conf_words(conf)} that these readings are genuinely out of line ({conf:.0%}).")
    ev_ids: list[str] = []
    for f in strongest + point_flags[:40]:
        for e in f.evidence_ids:
            if e not in ev_ids:
                ev_ids.append(e)
    return Diagnosis(id=diag_id, flag_ids=[f.id for f in point_flags[:200]], group_id=groups[0] if len(groups) == 1 else None, pattern_id=None, fault_type="isolated suspicious readings", cause_class="unknown", ranked_signals=ranked, propagation=[], steps=steps, summary=summary, confidence=round(conf, 3), uncertainty=["Whether each reading is a glitch or a manipulation cannot be decided from the data.", "A very coarse sampling period could make a short real event look like a single reading."], assumptions=["Readings next to a suspicious row are taken as the reference for what that row should have looked like."], evidence_ids=ev_ids[:40], narrative_source="template")


_DIR_WORDS = {"up": "rose above its normal level", "down": "fell below its normal level", "noisy": "became much noisier than usual", "stuck": "froze at one value", "shifted": "stopped following the signals it normally moves with", "deviating": "deviated from its normal behaviour"}
_CAUSE_SENTENCE = {
    "process": "The pattern points to a change in the process itself rather than a faulty instrument: several related signals moved together.",
    "sensor": "The pattern points to an instrument problem rather than the process: one signal broke away from the signals it normally follows while they stayed consistent with each other.",
    "data": "The pattern points to a data problem: the leading signal was already marked unreliable by the data-quality checks in these rows, so this should be treated as bad data, not as a process event.",
    "mixed": "Both a process change and an instrument or data problem seem to be involved.",
    "unknown": "Whether this is a process change or an instrument problem cannot be settled from the evidence available for this event.",
}


def _conf_words(c: float) -> str:
    if c >= 0.85:
        return "very confident"
    if c >= 0.7:
        return "fairly confident"
    if c >= 0.5:
        return "moderately confident"
    if c >= 0.3:
        return "not very confident"
    return "uncertain"


def plain_summary(fault_type: str, group: str, main: Flag, ranked: list[SignalContribution], conf: float, chain: list[PropagationStep]) -> str:
    """Two to four plain-language sentences a non-specialist can read: what, where, which signals, how sure."""
    where = f"in group {group}, rows {main.row_start}-{main.row_end}" if group else f"in rows {main.row_start}-{main.row_end}"
    first = f"The monitor found {fault_type} {where}."
    parts: list[str] = []
    for r in ranked[:3]:
        share = f" and accounts for {r.contribution:.0%} of the deviation" if r.contribution >= 0.1 else ""
        parts.append(f"{r.signal} {_DIR_WORDS.get(r.direction or 'deviating', 'deviated')}{share}")
    second = ("The signal " if len(parts) == 1 else "The signals involved: ") + "; ".join(parts) + "." if parts else ""
    det = main.cause_detail or {}
    third = _CAUSE_SENTENCE.get(main.likely_cause_class, _CAUSE_SENTENCE["unknown"])
    if det.get("kind") == "actuator_saturation":
        third = f"{det.get('signal')} is a valve or controller output that ran to its {det.get('side', 'upper')} limit: the controller could not keep up, which points to a problem in the process (for example a lost feed), not to a broken sensor."
    elif det.get("kind") == "common_freeze":
        third = "Several signals froze at the same moment, which independent instruments do not do: this is a problem with the recorded data, not with any one sensor."
    elif det.get("kind") == "duplicate_rows":
        third = "Many of these rows are exact copies of other rows: the recording repeated itself, so this is a data problem."
    if det.get("spread"):
        second = f"No single signal stands out: the change is spread over about {det['spread'].get('n_signals_for_half', 'several')} signals" + (f" of cluster {det['spread']['cluster']}" if det["spread"].get("cluster") else "") + "."
    prop = ""
    if chain:
        c0 = chain[0]
        lag = f" about {c0.lag} samples later" if c0.lag else " shortly after"
        prop = f" The disturbance appears to have travelled from {c0.from_signal} to {c0.to_signal}{lag}."
    fourth = f" We are {_conf_words(conf)} in this reading ({conf:.0%})."
    return " ".join(x for x in (first, second, third + prop + fourth) if x)


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    import json as _json
    import re as _re

    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = _re.sub(r"^```[a-zA-Z]*\n?|```$", "", t).strip()
    try:
        d = _json.loads(t)
        return d if isinstance(d, dict) else None
    except Exception:
        m = _re.search(r"\{.*\}", t, _re.S)
        if m:
            try:
                d = _json.loads(m.group(0))
                return d if isinstance(d, dict) else None
            except Exception:
                return None
    return None


def _clean_prose(x: Any) -> str:
    if isinstance(x, dict):
        for k in ("text", "summary", "statement", "step"):
            if isinstance(x.get(k), str):
                return x[k].strip()
        return ""
    return str(x).strip()


def narrative_payload(ws, diag: Diagnosis) -> dict[str, Any]:
    """What the model gets for one diagnosis: the diagnosis itself and the statements of the evidence it cites."""
    evidence = []
    for eid in diag.evidence_ids[:12]:
        ev = ws.evidence.get(eid)
        if ev is not None:
            evidence.append({"id": ev.id, "statement": ev.statement, "n_samples": ev.n_samples})
    return {"diagnosis": diag.model_dump(exclude={"critique"}), "evidence": evidence}


def merge_narrative(diag: Diagnosis, res: Any) -> bool:
    """Layer the prose of a diagnosis_narrative reply on the template; True when something was added. The model
    answers in JSON ({summary, steps, uncertainty}); only its prose is kept -- raw JSON never reaches the operator.
    Touches nothing but `diag`, so a worker thread may call it on its own copy."""
    if res is not None and res.ok and (res.data or (res.text and res.text.strip())):
        data = res.data if isinstance(res.data, dict) else _extract_json(res.text or "")
        model_summary = ""
        model_steps: list[str] = []
        model_unc: list[str] = []
        if data:
            model_summary = _clean_prose(data.get("summary") or data.get("explanation") or "")
            model_steps = [_clean_prose(x) for x in (data.get("steps") or []) if _clean_prose(x)]
            model_unc = [_clean_prose(x) for x in (data.get("uncertainty") or data.get("uncertainties") or []) if _clean_prose(x)]
        else:
            txt = (res.text or "").strip()
            model_summary = "" if txt.startswith("{") else txt[:1500]
        if not (model_summary or model_steps):
            return False
        if model_summary:
            diag.summary = diag.summary + "\n\n" + model_summary[:1500]
        if model_steps:
            diag.steps = diag.steps + ["Model explanation: " + st[:400] for st in model_steps[:6]]
        for u in model_unc[:4]:
            if u not in diag.uncertainty:
                diag.uncertainty.append(u[:300])
        diag.narrative_source = f"{res.source}+template"
        return True
    return False


def apply_narrative(ws, diag: Diagnosis, res: Any) -> Diagnosis:
    """merge_narrative plus the decision-log record (main thread only: the log and the registries stay out of workers)."""
    if merge_narrative(diag, res):
        ws.log.record(f"llm:{res.route}:{res.model}" if res.route in ("local", "external") else "system:diagnose", "narrative", "diagnosis", diag.id, {"source": res.source, "ledger_id": res.ledger_id}, diag.evidence_ids)
    return diag


def add_llm_narrative(ws, settings, diag: Diagnosis, language: str = "en") -> Diagnosis:
    """Optional LLM narrative layered on the template (never replaces the steps)."""
    try:
        from ..llm import complete
    except Exception:
        return diag
    payload = narrative_payload(ws, diag)
    try:
        res = complete("diagnosis_narrative", payload, purpose=f"explain {diag.id}", ws=ws, settings=settings, language=language)
    except Exception:
        return diag
    return apply_narrative(ws, diag, res)
