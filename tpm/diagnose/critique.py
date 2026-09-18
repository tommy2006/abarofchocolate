"""Critique: challenge each diagnosis before it is presented.

Three layers, all recorded in Critique.checks / objections:
  1. code checks     top signal untrusted? contributions concentrated on one signal while the cause says
                     process? baseline confidence low? detectors disagree? evidence thin? event short?
                     pattern classifier unreliable? propagation lags inconsistent with learned relations?
  2. cross-checks    per-detector agreement recorded in the event evidence (fraction of detectors firing)
  3. devil's advocate an LLM objection list citing evidence ids (tpm.llm.complete("critique")), with a
                     template fallback that always produces objections citing evidence ids.
The verdict (supported | weakened | rejected) adjusts the diagnosis confidence.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

from ..contracts import Critique, Diagnosis


def _check(name: str, passed: bool, detail: str, weight: float = 0.1) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail, "weight": weight}


def code_checks(ws, diag: Diagnosis, flags_by_id: dict[str, Any], baseline: dict[str, Any], patterns_by_id: dict[str, dict[str, Any]], window: int) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    flags = [flags_by_id[f] for f in diag.flag_ids if f in flags_by_id]
    main = max(flags, key=lambda f: f.score * (f.row_end - f.row_start + 1)) if flags else None
    top = diag.ranked_signals[0] if diag.ranked_signals else None
    untrusted = set()
    if main is not None and main.trust_context:
        untrusted = set(main.trust_context.get("untrusted_signals") or [])
    if top is not None:
        checks.append(_check("top_signal_trusted", top.signal not in untrusted or diag.cause_class == "data", f"{top.signal} {'is' if top.signal in untrusted else 'is not'} in the batch's untrusted signals; cause class is '{diag.cause_class}'", 0.2))
        conc = top.contribution
        if diag.cause_class == "process":
            checks.append(_check("contributions_spread_for_process", conc < 0.7, f"top signal carries {conc:.0%} of the deviation; a process fault should involve several signals" if conc >= 0.7 else f"contributions are spread (top {conc:.0%})", 0.15))
        if diag.cause_class == "sensor":
            checks.append(_check("single_signal_for_sensor", conc >= 0.4, f"top signal carries {conc:.0%}; a sensor fault should concentrate on one signal", 0.15))
    bconf = float(baseline.get("confidence", 0.5) or 0.5)
    checks.append(_check("baseline_confidence", bconf >= 0.5, f"baseline regime confidence {bconf:.2f} ({baseline.get('status', 'n/a')})", 0.15))
    # detector agreement from the contribution evidence
    agree = None
    for eid in diag.evidence_ids:
        ev = ws.evidence.get(eid)
        if ev is not None and ev.kind == "contribution":
            da = ev.values.get("detector_agreement") or {}
            if da:
                agree = float(np.mean([v >= 0.5 for v in da.values()]))
                break
    if agree is not None:
        checks.append(_check("detectors_agree", agree >= 0.3, f"{agree:.0%} of the detectors fire on more than half of the event rows", 0.15))
    n_ev = sum(1 for eid in diag.evidence_ids if ws.evidence.get(eid) is not None)
    checks.append(_check("evidence_coverage", n_ev >= 2, f"{n_ev} evidence item(s) cited", 0.1))
    if main is not None:
        length = main.row_end - main.row_start + 1
        checks.append(_check("event_length", length >= 2 * window, f"event spans {length} rows (window {window})", 0.1))
    if diag.pattern_id and diag.pattern_id in patterns_by_id:
        rel = patterns_by_id[diag.pattern_id].get("classifier_reliability")
        checks.append(_check("pattern_classifier_reliable", rel is None or rel >= 0.6, f"pattern classifier reliability {rel if rel is not None else 'n/a'}", 0.1))
    if diag.propagation:
        bad = [st for st in diag.propagation if "does not match" in st.explanation]
        checks.append(_check("propagation_consistent", not bad, f"{len(bad)} propagation step(s) contradict the learned lead/lag structure" if bad else "propagation order is consistent with learned relations", 0.1))
    return checks


def template_objections(diag: Diagnosis, checks: list[dict[str, Any]]) -> list[str]:
    ev = diag.evidence_ids[:2] or ["(no evidence)"]
    cite = ", ".join(ev)
    out = []
    for c in checks:
        if not c["passed"]:
            out.append(f"Objection ({c['name']}): {c['detail']} [cites {cite}]")
    if diag.cause_class == "sensor":
        out.append(f"Alternative: a genuine process upset could move only {diag.ranked_signals[0].signal if diag.ranked_signals else 'this signal'} if its peers respond slowly; verify the peers over a longer window [cites {cite}]")
    elif diag.cause_class == "process":
        out.append(f"Alternative: a shared sensor or data-path issue (e.g. a common transmitter or scaling change) could move several signals together without any process change [cites {cite}]")
    elif diag.cause_class == "data":
        out.append(f"Alternative: the data-quality check may have reacted to a real excursion; confirm that the value range change is not physical [cites {cite}]")
    else:
        out.append(f"Alternative: the deviation may be a legitimate operating-mode change not represented in the baseline sample [cites {cite}]")
    return out[:6]


def llm_objections(ws, settings, diag: Diagnosis, checks: list[dict[str, Any]], language: str = "en") -> tuple[list[str], str]:
    try:
        from ..llm import complete
    except Exception:
        return [], "template"
    evidence = []
    for eid in diag.evidence_ids[:12]:
        ev = ws.evidence.get(eid)
        if ev is not None:
            evidence.append({"id": ev.id, "statement": ev.statement})
    payload = {"diagnosis": diag.model_dump(exclude={"critique"}), "checks": checks, "evidence": evidence, "instruction": "Act as a devil's advocate. List the strongest objections to this diagnosis. Every objection must cite at least one evidence id from the list."}
    try:
        res = complete("critique", payload, purpose=f"critique {diag.id}", ws=ws, settings=settings, language=language)
    except Exception:
        return [], "template"
    if res is None or not res.ok:
        return [], "template"
    objs: list[str] = []
    if res.data and isinstance(res.data.get("objections"), list):
        objs = [str(o) for o in res.data["objections"]]
    elif res.text:
        objs = [ln.strip("-* ").strip() for ln in res.text.splitlines() if ln.strip()]
    valid_ids = set(diag.evidence_ids)
    objs = [o for o in objs if any(eid in o for eid in valid_ids)]
    return objs[:6], res.source


def critique_diagnosis(ws, settings, diag: Diagnosis, flags_by_id: dict[str, Any], baseline: dict[str, Any], patterns_by_id: dict[str, dict[str, Any]], window: int, language: str = "en", use_llm: bool = True) -> Diagnosis:
    checks = code_checks(ws, diag, flags_by_id, baseline, patterns_by_id, window)
    objections = template_objections(diag, checks)
    source = "template"
    if use_llm:
        llm_objs, src = llm_objections(ws, settings, diag, checks, language)
        if llm_objs:
            objections = [f"[{src}] {o}" for o in llm_objs] + objections
            source = f"{src}+template"
    failed_weight = sum(c["weight"] for c in checks if not c["passed"])
    total_weight = sum(c["weight"] for c in checks) or 1.0
    penalty = failed_weight / total_weight
    critical = any(not c["passed"] and c["name"] in ("top_signal_trusted",) for c in checks)
    if critical or penalty >= 0.6:
        verdict = "rejected"
        adjusted = max(0.05, diag.confidence * 0.4)
    elif penalty >= 0.25:
        verdict = "weakened"
        adjusted = max(0.05, diag.confidence * (1.0 - 0.6 * penalty))
    else:
        verdict = "supported"
        adjusted = min(0.95, diag.confidence * (1.0 - 0.3 * penalty) + 0.03)
    diag.critique = Critique(verdict=verdict, objections=objections, checks=checks, adjusted_confidence=round(float(adjusted), 3), source=source)
    diag.confidence = round(float(adjusted), 3)
    if verdict != "supported":
        diag.uncertainty = list(diag.uncertainty) + [f"critique verdict: {verdict} ({', '.join(c['name'] for c in checks if not c['passed'])})"]
    ws.log.record("system:diagnose", "critique", "diagnosis", diag.id, {"verdict": verdict, "adjusted_confidence": diag.confidence, "failed_checks": [c["name"] for c in checks if not c["passed"]], "source": source}, diag.evidence_ids)
    return diag
