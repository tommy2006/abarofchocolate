"""assessor stage (agent B): the DATA QUALITY ASSESSOR.

A human asks in natural language whether adding more or less data would improve the dataset; the assessor
answers from evidence: data-quality scores (checks + trust), regime coverage (clustering of unit fingerprints)
and ML fitness (learning curves with bounded experiments). Nothing is applied without human approval.

Public functions (see docs/ARCHITECTURE.md):
    run_assess(ws, settings, ctx)                      -> summary; writes assessor.json
    ask(ws, settings, question, actor)                 -> {"answer", "action", "evaluation", "evidence_ids", ...}
    apply_override(ws, settings, decision)             -> apply an approved action (object_type "assessor")
"""
from __future__ import annotations

import json as _json
import re as _re
import time
from typing import Any, Optional

from ..quality.trust import signal_trust_summary
from .actions import ACTION_TYPES, apply_action, assess_new_file, evaluate_action, parse_action
from .coverage import regime_coverage
from .fitness import learning_curve
from .scores import CATEGORIES, dq_scores

__all__ = ["run_assess", "ask", "apply_override", "dq_scores", "regime_coverage", "learning_curve", "parse_action", "evaluate_action", "assess_new_file", "apply_action", "ACTION_TYPES"]


def _candidate_actions(ws: Any, settings: Any, dq: dict[str, Any], coverage: dict[str, Any], fitness: dict[str, Any]) -> list[dict[str, Any]]:
    """Actions worth evaluating because the evidence hints at them (decision 40)."""
    cands: list[dict[str, Any]] = []
    checks = ws.checks()
    verdicts = ws.trust()
    if any(c.check_type == "duplicate_rows" and c.status != "pass" for c in checks):
        cands.append({"type": "drop_duplicates", "params": {}, "source": "auto"})
    n_batches = max(1, len(verdicts))
    per_signal = signal_trust_summary(verdicts)
    bad = [s for s, d in per_signal.items() if d["fraction"] >= 0.3]
    for s in sorted(bad, key=lambda s: -per_signal[s]["fraction"])[:3]:
        cands.append({"type": "drop_signal", "params": {"signals": [s]}, "source": "auto"})
    low = (coverage.get("signals") or {}).get("near_constant") or []
    for s in low[:2]:
        if not any(a["type"] == "drop_signal" and a["params"]["signals"] == [s] for a in cands):
            cands.append({"type": "drop_signal", "params": {"signals": [s]}, "source": "auto"})
    batches = ws.read_json("batches", []) or []
    untrusted = {v.batch_id for v in verdicts if not v.trusted}
    if untrusted and n_batches > 2:
        groups: dict[str, int] = {}
        for b in batches:
            if b["batch_id"] in untrusted:
                for g in b.get("group_ids") or []:
                    groups[g] = groups.get(g, 0) + 1
        for g in sorted(groups, key=lambda g: -groups[g])[:2]:
            cands.append({"type": "drop_group", "params": {"group_ids": [g]}, "source": "auto"})
    cands.append({"type": "add_more_like", "params": {"n_units": None, "unit": (coverage.get("unit") or {}).get("kind", "unit")}, "source": "auto"})
    if fitness.get("diminishing_returns_fraction") is not None:
        cands.append({"type": "downsample", "params": {"factor": 2}, "source": "auto"})
    return cands


_LEAD_RE = _re.compile(r"^\s*(?:yes|no|unclear|uncertain)\s*[:.,;!–—-]\s*", _re.I)


def _strip_lead(text: Any) -> str:
    """'Yes: 5 duplicate rows ...' -> '5 duplicate rows ...'. The verdict word is shown once, as the headline of a
    verdict card or as the lead of a chat answer; the reason that follows must read as a sentence of its own."""
    s = "" if text is None else str(text).strip()
    out = _LEAD_RE.sub("", s, count=1).lstrip()
    if not out or out == s:
        return s
    return out[0].upper() + out[1:]


def _verdicts(dq: dict[str, Any], coverage: dict[str, Any], fitness: dict[str, Any], evaluations: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    thin = coverage.get("thin_regimes") or []
    more: dict[str, Any] = {"would_help": None, "why": "", "estimated_gain": None, "evidence_ids": list(fitness.get("evidence_ids") or []) + list(coverage.get("evidence_ids") or [])}
    if fitness.get("available") and fitness.get("slope") is not None:
        more["estimated_gain"] = fitness.get("estimated_gain_more_data")
        wh = fitness.get("would_help_more_data")
        if wh is True:
            more["would_help"] = True
            more["why"] = f"The learning curve is still rising: {fitness['primary_metric']} would gain about {fitness['estimated_gain_more_data']:+.3f} with 50% more data of the same kind."
        elif wh is False:
            more["would_help"] = bool(thin)
            more["why"] = f"The learning curve is flat from {fitness['diminishing_returns_fraction']:.0%} of the data; more of the same data changes little." if fitness.get("diminishing_returns_fraction") is not None else f"The slope at the end of the learning curve is only {fitness['slope']:+.3f}; more of the same data changes little."
            if thin:
                more["why"] += f" More data from the thin regime(s) {', '.join(thin)} would still improve coverage."
        else:
            more["why"] = f"The estimated gain is {fitness.get('estimated_gain_more_data'):+.3f} with uncertainty +/-{(fitness.get('slope_uncertainty') or 0) * 0.5:.3f}; the experiments are too noisy to call it."
            if thin:
                more["why"] += f" Coverage-wise, data from the thin regime(s) {', '.join(thin)} would help regardless."
    else:
        more["why"] = fitness.get("reason") or "No learning curve could be computed."
        if thin:
            more["would_help"] = True
            more["why"] += f" Coverage says more data from the thin regime(s) {', '.join(thin)} would help."
    less_recs = [e for e in evaluations if e["recommendation"] == "recommend" and e["action"]["type"] in ("drop_duplicates", "drop_signal", "drop_group", "drop_range", "drop_regime")]
    less: dict[str, Any] = {"would_help": bool(less_recs), "why": "", "estimated_gain": None, "evidence_ids": [i for e in less_recs for i in e.get("evidence_ids", [])][:20]}
    if less_recs:
        gains = [e["expected_effect"].get("dq_scores", {}).get("overall", {}).get("delta", 0) or 0 for e in less_recs]
        less["estimated_gain"] = round(max(gains), 4) if gains else None
        less["why"] = " ".join(_strip_lead(e["rationale"]) for e in less_recs[:3])
    else:
        down = next((e for e in evaluations if e["action"]["type"] == "downsample"), None)
        less["why"] = "The evidence supports no removal: there are no duplicates, and no signal or group is bad enough to drop." + (" " + _strip_lead(down["rationale"]) if down else "")
    return more, less


def run_assess(ws: Any, settings: Any, ctx: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    ctx = ctx or {}
    t0 = time.time()
    progress = ctx.get("progress") or (lambda f, m="": None)
    budget = float(settings.assessor.experiment_time_budget_s)
    ws.log.record("system:assessor", "stage", "stage", "assess", {"state": "started", "experiment_budget_s": budget})
    progress(0.05, "data-quality scores")
    dq = dq_scores(ws, settings)
    progress(0.2, "regime coverage")
    coverage = regime_coverage(ws, settings) if ws.exists("dataset") else {"available": False, "findings": ["no dataset"], "evidence_ids": [], "coverage_score": None}
    progress(0.4, "learning curve")
    fitness = learning_curve(ws, settings, coverage=coverage, time_budget_s=budget * 0.6) if ws.exists("dataset") else {"available": False, "reason": "no dataset", "curve": [], "evidence_ids": []}
    progress(0.7, "evaluating candidate actions")
    remaining = max(5.0, budget - (time.time() - t0))
    cands = _candidate_actions(ws, settings, dq, coverage, fitness)
    evaluations: list[dict[str, Any]] = []
    for i, a in enumerate(cands):
        left = max(2.0, remaining - (time.time() - t0 - (budget - remaining)))
        per = max(2.0, min(20.0, left / max(1, len(cands) - i)))
        if time.time() - t0 > budget * 1.2 and evaluations:
            break
        evaluations.append(evaluate_action(ws, settings, a, time_budget_s=per))
    recommendations = []
    for i, e in enumerate(evaluations):
        if e["recommendation"] == "recommend":
            recommendations.append({"id": f"REC-{len(recommendations) + 1:03d}", "text": _strip_lead(e["rationale"]), "action": e["action"], "expected_effect": e.get("expected_effect", {}), "confidence": e["confidence"], "evidence_ids": e.get("evidence_ids", [])})
    more, less = _verdicts(dq, coverage, fitness, evaluations)
    fit_score = fitness.get("fitness_score")
    cov_score = coverage.get("coverage_score")
    parts = [(0.4, fit_score), (0.3, cov_score), (0.3, dq.get("overall"))]
    wsum = sum(w for w, v in parts if v is not None)
    combined = round(sum(w * v for w, v in parts if v is not None) / wsum, 4) if wsum else None
    summary_text = f"Combined score {combined if combined is not None else 'n/a'} (fitness {fit_score if fit_score is not None else 'n/a'}, coverage {cov_score if cov_score is not None else 'n/a'}, data quality {dq['overall']}). More data: {'yes' if more['would_help'] else 'no' if more['would_help'] is False else 'unclear'}. Less data: {'yes' if less['would_help'] else 'no'}. {len(recommendations)} recommendation(s)."
    out = {
        "dq_scores": {k: dq[k] for k in CATEGORIES + ["overall", "mean_trust", "n_untrusted_batches", "n_batches", "n_signals", "counts", "details", "worst_signals", "evidence_ids"]},
        "coverage": coverage, "fitness": fitness, "combined_score": combined, "weights": {"fitness": 0.4, "coverage": 0.3, "data_quality": 0.3},
        "recommendations": recommendations, "evaluations": evaluations, "more_data_verdict": more, "less_data_verdict": less,
        "summary": summary_text, "seconds": round(time.time() - t0, 2), "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    ws.write_json("assessor", out)
    ws.log.record("system:assessor", "assess", "assessor", "assessor.json", {"combined_score": combined, "fitness_score": fit_score, "coverage_score": cov_score, "dq_overall": dq["overall"], "n_recommendations": len(recommendations), "more_data": more["would_help"], "less_data": less["would_help"], "seconds": out["seconds"]}, (dq.get("evidence_ids", []) + coverage.get("evidence_ids", []) + fitness.get("evidence_ids", []))[:40])
    ws.log.record("system:assessor", "stage", "stage", "assess", {"state": "done", "seconds": out["seconds"]})
    progress(1.0, summary_text)
    return {"combined_score": combined, "fitness_score": fit_score, "coverage_score": cov_score, "dq_overall": dq["overall"], "n_recommendations": len(recommendations), "more_data": more["would_help"], "less_data": less["would_help"], "seconds": out["seconds"], "message": summary_text}


def _template_answer(question: str, action: Optional[dict[str, Any]], evaluation: Optional[dict[str, Any]], assessor: dict[str, Any]) -> str:
    if action is None or evaluation is None:
        s = assessor.get("summary")
        more, less = assessor.get("more_data_verdict") or {}, assessor.get("less_data_verdict") or {}
        base = "I can evaluate data-curation questions such as dropping a signal, a group/run, a row range, duplicates, downsampling, removing a regime, adding a file or collecting more runs."
        if s:
            base += f" Current assessment: {s} More data: {more.get('why', '')} Less data: {less.get('why', '')}"
        return base
    lead = {"recommend": "Yes.", "advise_against": "No.", "neutral": "Unclear."}[evaluation["recommendation"]]
    txt = f"{lead} {_strip_lead(evaluation['rationale'])}"
    eff = evaluation.get("expected_effect") or {}
    fit = eff.get("fitness")
    if isinstance(fit, dict) and fit.get("available") and fit.get("delta") is not None and "stability" not in evaluation["rationale"]:
        txt += f" Model {fit['primary_metric']} would go from {fit['before']:.3f} to {fit['after']:.3f}."
    if evaluation["recommendation"] == "recommend" and action["type"] not in ("add_more_like",):
        txt += " Nothing is changed until you approve the action."
    return txt


_ID_RE = _re.compile(r"\b(?:EV|CHK|FLAG|DIAG|INF|RULE|PATTERN)-[A-Z0-9]+\b")


def _llm_answer_text(res: Any) -> str:
    """The router may return plain text or a JSON object ({"answer": ...}); take the answer only."""
    if not getattr(res, "ok", False):
        return ""
    data = res.data if isinstance(getattr(res, "data", None), dict) else None
    text = (getattr(res, "text", "") or "").strip()
    if data is None and text.startswith("{"):
        try:
            data = _json.loads(text)
        except Exception:
            data = None
    if isinstance(data, dict):
        for k in ("answer", "text", "response", "message"):
            if isinstance(data.get(k), str) and data[k].strip():
                return data[k].strip()
        return ""
    return text


def ask(ws: Any, settings: Any, question: str, actor: str = "human:unknown", time_budget_s: Optional[float] = None) -> dict[str, Any]:
    """Chat entry point: parse -> evaluate -> plain-language answer (template, optionally LLM-enhanced locally)."""
    t0 = time.time()
    assessor = ws.read_json("assessor", None) or {}
    action = parse_action(question, ws, settings)
    evaluation = evaluate_action(ws, settings, action, time_budget_s=time_budget_s) if action else None
    answer = _template_answer(question, action, evaluation, assessor)
    source = "template"
    ev_ids = list((evaluation or {}).get("evidence_ids") or [])
    if not action:
        ev_ids = list((assessor.get("dq_scores") or {}).get("evidence_ids") or [])[:5]
    if settings.route_for("assessor_chat") == "local":
        try:
            from ..llm import complete

            res = complete("assessor_chat", {"question": question, "action": action, "evaluation": {k: v for k, v in (evaluation or {}).items() if k in ("recommendation", "rationale", "expected_effect", "confidence")}, "template_answer": answer, "evidence_ids": ev_ids[:10], "instruction": "Rewrite the template answer for an operator in 2-4 sentences. Keep every number and the recommendation; cite only the given evidence ids."}, purpose="explain assessor evaluation", ws=ws, settings=settings)
            llm_text = _llm_answer_text(res)
            if llm_text and len(llm_text) > 20:
                # never let a model invent evidence: strip ids that are not ours
                llm_text = _ID_RE.sub(lambda m: m.group(0) if m.group(0) in ev_ids else "", llm_text)
                llm_text = _re.sub(r"\(\s*(?:evidence|see)?:?\s*,?\s*\)", "", llm_text, flags=_re.I).strip()
                answer = llm_text
                source = res.source
        except Exception:
            pass
    if ev_ids:
        answer += f" (Evidence: {', '.join(ev_ids[:6])})"
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "actor": actor, "question": question, "answer": answer, "action": action, "recommendation": (evaluation or {}).get("recommendation"), "answer_source": source, "evidence_ids": ev_ids}
    try:
        ws.append_jsonl("chat", {"channel": "assessor", **entry})
    except Exception:
        pass
    ws.log.record(actor, "assessor_question", "assessor", "chat", {"question": question[:500], "action": action, "recommendation": entry["recommendation"], "answer_source": source, "seconds": round(time.time() - t0, 2)}, ev_ids[:20])
    return {"answer": answer, "action": action, "evaluation": evaluation, "evidence_ids": ev_ids, "answer_source": source, "seconds": round(time.time() - t0, 2)}


def apply_override(ws: Any, settings: Any, decision: Any) -> dict[str, Any]:
    """HumanDecision(object_type='assessor'): apply an approved action (new_value.action or a REC-xxx id), or
    dismiss a recommendation. Everything is logged with the human actor."""
    actor = f"human:{getattr(decision, 'actor_name', 'unknown')}({getattr(decision, 'role', '?')})"
    action_name = (getattr(decision, "action", "") or "").lower()
    new_value = getattr(decision, "new_value", None) or {}
    assessor = ws.read_json("assessor", None) or {}
    act = new_value.get("action") if isinstance(new_value.get("action"), dict) else None
    if act is None:
        rec = next((r for r in assessor.get("recommendations", []) if r["id"] == getattr(decision, "object_id", None)), None)
        act = rec["action"] if rec else None
    if action_name in ("dismiss", "reject", "question"):
        ws.log.record(actor, f"assessor_{action_name}", "assessor", getattr(decision, "object_id", "?"), {"note": getattr(decision, "note", None)})
        return {"applied": False, "status": action_name}
    if action_name not in ("apply_assessor_action", "approve", "accept", "apply", "override"):
        return {"applied": False, "error": f"unknown assessor action {action_name!r}"}
    if act is None:
        return {"applied": False, "error": "no action to apply: give new_value.action or a recommendation id as object_id"}
    if act.get("type") not in ACTION_TYPES:
        return {"applied": False, "error": f"unknown action type {act.get('type')!r}"}
    result = apply_action(ws, settings, act, actor=actor)
    if result.get("applied"):
        for r in assessor.get("recommendations", []):
            if r.get("action") == act:
                r["status"] = "applied"
                r["applied_by"] = actor
        ws.write_json("assessor", assessor)
    return result
