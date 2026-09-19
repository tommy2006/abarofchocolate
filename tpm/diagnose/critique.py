"""Critique: challenge each diagnosis before it is presented.

Three layers, all recorded in Critique.checks / objections:
  1. code checks     top signal untrusted? contributions concentrated on one signal while the cause says
                     process? baseline confidence low? detectors disagree? evidence thin? event short?
                     pattern classifier unreliable? propagation lags inconsistent with learned relations?
  2. cross-checks    per-detector agreement recorded in the event evidence (fraction of detectors firing)
  3. devil's advocate an LLM objection list citing evidence ids (tpm.llm.complete("critique")), with a
                     template fallback that always produces objections citing evidence ids.
  4. alternatives    (round 6) the critique argues the other explanations - a data problem, a process change, a
                     saturated actuator, a single broken sensor - from the evidence of the event; an alternative at
                     least as well supported as the diagnosis weakens or rejects it, and every disagreement is logged.
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


ALT_NAMES = {"data": "a data problem", "process": "a process change", "actuator_saturation": "a saturated actuator (valve at its limit)", "sensor": "a single broken sensor"}


def _roles(ws) -> dict[str, str]:
    try:
        return {s.id: s.structural_role for s in ws.signals()}
    except Exception:
        return {}


def alternatives(ws, diag: Diagnosis, main: Any) -> list[dict[str, Any]]:
    """Support (0..1) for each explanation of the event, from its own evidence. The diagnosis' own explanation is
    included, so the critique can compare like with like."""
    ranked = list(diag.ranked_signals or [])
    det = diag.cause_detail or (getattr(main, "cause_detail", None) or {})
    tc = (getattr(main, "trust_context", None) or {}) if main is not None else {}
    untrusted = set(tc.get("untrusted_signals") or [])
    roles = _roles(ws)
    stuck = [r for r in ranked if r.direction == "stuck"]
    moved = [r for r in ranked if r.direction != "stuck" and r.contribution >= 0.12]
    top = ranked[0] if ranked else None
    out: list[dict[str, Any]] = []
    # a data problem
    data_s, data_why = 0.0, []
    if det.get("kind") in ("common_freeze", "duplicate_rows", "untrusted_signal"):
        data_s, data_why = 0.8, [f"the cause rule saw {det.get('kind').replace('_', ' ')}"]
    if len(stuck) >= 2:
        data_s = max(data_s, 0.65)
        data_why.append(f"{len(stuck)} of the listed signals froze in the same rows")
    if top is not None and top.signal in untrusted:
        data_s = max(data_s, 0.75)
        data_why.append(f"{top.signal} was distrusted by the data-quality checks")
    if tc and not tc.get("trusted", True):
        data_s = max(data_s, 0.55)
        data_why.append("the batch was marked untrusted")
    out.append({"cause": "data", "support": data_s, "why": "; ".join(data_why) or "no data-quality finding overlaps these rows"})
    # a saturated actuator
    act = [r for r in stuck if roles.get(r.signal) == "actuator_like"]
    sat_s = 0.8 if det.get("kind") == "actuator_saturation" else (0.6 if act else 0.0)
    out.append({"cause": "actuator_saturation", "support": sat_s, "why": (f"{det.get('signal')} pinned at its {det.get('side')} limit" if det.get("kind") == "actuator_saturation" else (f"{act[0].signal} is a valve / controller output that stopped moving" if act else "no valve or controller output is frozen here"))})
    # a process change
    proc_s = 0.0
    if len(moved) >= 2:
        proc_s = 0.55 + (0.15 if diag.propagation else 0.0)
    if det.get("spread"):
        proc_s = max(proc_s, 0.5)
    out.append({"cause": "process", "support": proc_s, "why": (f"{len(moved)} signals moved together" + (" along learned relations" if diag.propagation else "")) if moved else "fewer than two signals moved"})
    # a single broken sensor
    sen_s = 0.0
    if top is not None and top.contribution >= 0.45 and all(r.contribution < 0.15 for r in ranked[1:]) and top.signal not in untrusted and roles.get(top.signal) != "actuator_like":
        sen_s = 0.6 + (0.1 if top.direction in ("stuck", "noisy", "shifted") else 0.0)
    if det.get("spread"):
        sen_s = min(sen_s, 0.2)
    out.append({"cause": "sensor", "support": sen_s, "why": (f"{top.signal} alone carries {top.contribution:.0%} while the others stay below 15%" if sen_s else "no single signal dominates") if top is not None else "no ranked signals"})
    return out


def _own_cause(diag: Diagnosis) -> str:
    det = diag.cause_detail or {}
    return "actuator_saturation" if det.get("kind") == "actuator_saturation" else diag.cause_class


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


def cited_evidence(ws, diag: Diagnosis) -> list[dict[str, Any]]:
    evidence = []
    for eid in diag.evidence_ids[:12]:
        ev = ws.evidence.get(eid)
        if ev is not None:
            evidence.append({"id": ev.id, "statement": ev.statement})
    return evidence


def objections_payload(diag: Diagnosis, checks: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return {"diagnosis": diag.model_dump(exclude={"critique"}), "checks": checks, "evidence": evidence, "instructions": "Act as a devil's advocate. List the strongest objections to this diagnosis. Every objection must cite at least one evidence id from the list."}


def llm_objections(ws, settings, diag: Diagnosis, checks: list[dict[str, Any]], language: str = "en") -> tuple[list[str], str]:
    try:
        from ..llm import complete
    except Exception:
        return [], "template"
    payload = objections_payload(diag, checks, cited_evidence(ws, diag))
    try:
        res = complete("critique", payload, purpose=f"critique {diag.id}", ws=ws, settings=settings, language=language)
    except Exception:
        return [], "template"
    return parse_objections(diag, res)


def parse_objections(diag: Diagnosis, res: Any) -> tuple[list[str], str]:
    """(objections, source) from a critique reply; only objections that cite evidence of this diagnosis are kept."""
    if res is None or not res.ok:
        return [], "template"
    objs: list[str] = []

    def _as_text(o: Any) -> str:
        if isinstance(o, dict):
            txt = str(o.get("text") or o.get("objection") or o.get("statement") or "").strip()
            ids = o.get("evidence_ids") or o.get("evidence") or []
            if isinstance(ids, str):
                ids = [ids]
            ids = [str(i) for i in ids if str(i) not in txt]
            return (txt + (f" (evidence: {', '.join(ids)})" if ids else "")).strip()
        return str(o).strip()

    if res.data and isinstance(res.data.get("objections"), list):
        objs = [_as_text(o) for o in res.data["objections"]]
    elif res.text:
        txt = res.text.strip()
        if txt.startswith("{"):
            try:
                import json as _json

                d = _json.loads(txt)
                objs = [_as_text(o) for o in (d.get("objections") or [])] if isinstance(d, dict) else []
            except Exception:
                objs = []
        else:
            objs = [ln.strip("-* ").strip() for ln in txt.splitlines() if ln.strip()]
    objs = [o for o in objs if o and not o.startswith("{")]
    valid_ids = set(diag.evidence_ids)
    objs = [o for o in objs if any(eid in o for eid in valid_ids)]
    return objs[:6], res.source


def critique_diagnosis(ws, settings, diag: Diagnosis, flags_by_id: dict[str, Any], baseline: dict[str, Any], patterns_by_id: dict[str, dict[str, Any]], window: int, language: str = "en", use_llm: bool = True, model_objections: Optional[tuple[list[str], str]] = None) -> Diagnosis:
    """model_objections: (objections, source) already obtained from the model (the concurrent external path asks the
    model in a worker and applies the answer here); given, no model call is made."""
    checks = code_checks(ws, diag, flags_by_id, baseline, patterns_by_id, window)
    flags = [flags_by_id[f] for f in diag.flag_ids if f in flags_by_id]
    main = max(flags, key=lambda f: f.score * (f.row_end - f.row_start + 1)) if flags else None
    alts = alternatives(ws, diag, main)
    own = _own_cause(diag)
    own_s = next((a["support"] for a in alts if a["cause"] == own), 0.0)
    rivals = sorted([a for a in alts if a["cause"] != own and a["support"] >= 0.5], key=lambda a: -a["support"])
    disagree = [a for a in rivals if a["support"] >= max(0.5, own_s)]
    for a in rivals[:3]:
        checks.append(_check(f"alternative_{a['cause']}", a not in disagree, f"{ALT_NAMES.get(a['cause'], a['cause'])}: support {a['support']:.2f} ({a['why']}) vs {own_s:.2f} for the diagnosis", 0.25 if a in disagree else 0.1))
    objections = template_objections(diag, checks)
    for a in disagree[:2]:
        objections.insert(0, f"Alternative at least as likely: {ALT_NAMES.get(a['cause'], a['cause'])} - {a['why']} [cites {', '.join(diag.evidence_ids[:2]) or '(no evidence)'}]")
    source = "template"
    if use_llm or model_objections is not None:
        llm_objs, src = model_objections if model_objections is not None else llm_objections(ws, settings, diag, checks, language)
        if llm_objs:
            objections = [f"[{src}] {o}" for o in llm_objs] + objections
            source = f"{src}+template"
    before = diag.confidence
    failed_weight = sum(c["weight"] for c in checks if not c["passed"])
    total_weight = sum(c["weight"] for c in checks) or 1.0
    penalty = failed_weight / total_weight
    critical = any(not c["passed"] and c["name"] in ("top_signal_trusted",) for c in checks) or any(a["support"] >= 0.75 and a["support"] > own_s + 0.15 for a in disagree)
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
    ws.log.record("system:diagnose", "critique", "diagnosis", diag.id, {"verdict": verdict, "adjusted_confidence": diag.confidence, "failed_checks": [c["name"] for c in checks if not c["passed"]], "source": source, "alternatives": [{"cause": a["cause"], "support": a["support"]} for a in alts]}, diag.evidence_ids)
    if disagree or verdict != "supported":
        # the critique changed its mind: keep a separate, findable record of what it disagreed with and by how much
        ws.log.record("system:critique", "critique_disagrees" if disagree else "critique_lowered_confidence", "diagnosis", diag.id, {"diagnosis_cause": own, "alternatives": [{"cause": a["cause"], "support": a["support"], "why": a["why"]} for a in disagree], "verdict": verdict, "confidence_before": before, "confidence_after": diag.confidence}, diag.evidence_ids)
    return diag
