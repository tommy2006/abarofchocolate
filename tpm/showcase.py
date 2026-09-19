"""Showcase on a finished run: the things a reviewer wants to SEE working, done for real and recorded.

    python -m tpm showcase --run <run_id> [--rules file.md] [--no-chat] [--lang en]

1. Rules to checks: 3-5 plain-language rules (from --rules, or written from the run's own catalogue) are compiled
   into checks, activated and run on every batch; each check carries its rule id and pass / warn / fail.
2. Human in the loop: one diagnosis is accepted, one questioned, one overridden by a named person. The override is
   stored as a human-labelled example, and a later event with the same leading signals is diagnosed again to show
   the effect downstream (it is typed with the person's label).
3. The "why" chat: one real question about a specific flag, answered by the local model (or the evidence template
   when no model is running), with the evidence it cites.
4. The report is regenerated, so section 2 (rules), section 5 (human decisions) and the log show all of it.

Everything goes through the normal code paths (decision log, egress ledger); the results are written to
showcase.json in the run folder.
"""
from __future__ import annotations

import math
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from .contracts import HumanDecision
from .workspace import Workspace

ACTOR_NAME = "Demo reviewer (showcase)"


def _nice(x: float, up: bool) -> float:
    """A round number just outside x (what a person would write in a rule)."""
    if x == 0 or not math.isfinite(x):
        return 0.0
    mag = 10 ** math.floor(math.log10(abs(x)) - 1)
    return (math.ceil(x / mag) if up else math.floor(x / mag)) * mag


def auto_rules(ws: Workspace) -> list[str]:
    """3-5 rules in the operators' own style, written from the catalogue (aliases, observed normal ranges)."""
    sigs = [s for s in ws.signals() if not s.excluded and s.structural_role in ("continuous_measured", "actuator_like", "held_sampled")]
    if not sigs:
        return []
    diags = ws.diagnoses()
    lead = diags[0].ranked_signals[0].signal if diags and diags[0].ranked_signals else sigs[0].id
    by_id = {s.id: s for s in sigs}
    rules: list[str] = []
    s = by_id.get(lead) or sigs[0]
    q95 = s.fingerprint.get("q95")
    if isinstance(q95, (int, float)):
        rules.append(f"{s.id} must stay below {_nice(float(q95) * 1.05 if q95 > 0 else float(q95) * 0.95, True):g}.")
    temp = next((x for x in sigs if "temperature" in str(x.instrument_hypothesis or "")), None) or next((x for x in sigs if x.id != s.id), None)
    if temp is not None and isinstance(temp.fingerprint.get("q05"), (int, float)) and isinstance(temp.fingerprint.get("q95"), (int, float)):
        lo, hi = float(temp.fingerprint["q05"]), float(temp.fingerprint["q95"])
        pad = 0.5 * (hi - lo)
        rules.append(f"{temp.id} must stay between {_nice(lo - pad, False):g} and {_nice(hi + pad, True):g}.")
    act = next((x for x in sigs if x.structural_role == "actuator_like"), None)
    if act is not None:
        rules.append(f"{act.id} must not stay constant for more than 200 samples.")
    other = next((x for x in sigs if x.id not in {s.id, getattr(temp, 'id', None), getattr(act, 'id', None)}), None)
    if other is not None:
        rules.append(f"{other.id} must not be missing for more than 10 consecutive samples.")
    return rules[:5]


def _rules_step(ws: Workspace, settings: Any, rules_file: Optional[str]) -> dict[str, Any]:
    from .quality.rules import add_rules_from_file, run_active_rules

    if rules_file:
        path = Path(rules_file)
        texts = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.strip().startswith("#")]
    else:
        texts = auto_rules(ws)
        path = Path(tempfile.mkstemp(suffix="_rules.md")[1])
        path.write_text("\n".join(texts) + "\n", encoding="utf-8")
    added = add_rules_from_file(ws, settings, path, author=f"showcase:{path.name}", auto_status="active")
    batches = ws.read_json("batches") or []
    batches = batches.get("batches", batches) if isinstance(batches, dict) else batches
    t0 = time.time()
    checks = run_active_rules(ws, settings, list(batches), rules=[r for r in ws.rules() if r.id in {a.id for a in added}])
    by_rule: dict[str, dict[str, int]] = {}
    for c in checks:
        st = by_rule.setdefault(str(c.rule_id), {"pass": 0, "warn": 0, "fail": 0})
        st[c.status] = st.get(c.status, 0) + 1
    return {
        "rules": [{"id": r.id, "text": r.text, "compiled": r.compiled, "compile_source": getattr(r, "compile_source", None), "status": r.status, "results": by_rule.get(r.id, {})} for r in added],
        "n_checks": len(checks), "seconds": round(time.time() - t0, 1),
    }


def _pick(diags: list[Any], used: set[str], pred) -> Optional[Any]:
    return next((d for d in diags if d.id not in used and pred(d)), None)


def _hitl_step(ws: Workspace, settings: Any) -> dict[str, Any]:
    from .diagnose import diagnose_flags
    from .pipeline import apply_decision

    diags = [d for d in ws.diagnoses() if d.flag_ids]
    if len(diags) < 3:
        return {"skipped": f"only {len(diags)} diagnoses in this run"}
    used: set[str] = set()
    acc = _pick(diags, used, lambda d: (d.critique is None or d.critique.verdict == "supported")) or diags[0]
    used.add(acc.id)
    q = _pick(diags, used, lambda d: d.cause_class != acc.cause_class) or _pick(diags, used, lambda d: True)
    used.add(q.id)
    # override the most common signature, so the downstream effect can be shown on another event of the same kind
    sig = lambda d: tuple(s.signal for s in (d.ranked_signals or [])[:2])  # noqa: E731
    counts: dict[tuple, int] = {}
    for d in diags:
        if d.id not in used and len(sig(d)) == 2:
            counts[sig(d)] = counts.get(sig(d), 0) + 1
    target_sig = max(counts, key=counts.get) if counts else None
    ov = _pick(diags, used, lambda d: sig(d) == target_sig) if target_sig else _pick(diags, used, lambda d: True)
    used.add(ov.id)
    before = {d.id: {"fault_type": d.fault_type, "cause_class": d.cause_class, "confidence": d.confidence} for d in (acc, q, ov)}
    new_label = f"operator-named fault on {', '.join(sig(ov)) or 'these signals'}"
    decisions = [
        HumanDecision(actor_name=ACTOR_NAME, role="engineer", action="accept", object_type="diagnosis", object_id=acc.id, note="Demonstration (showcase script): accepted to show the decision path."),
        HumanDecision(actor_name=ACTOR_NAME, role="engineer", action="question", object_type="diagnosis", object_id=q.id, note="Demonstration (showcase script): questioned - asks for the instrument log of these rows before acting."),
        HumanDecision(actor_name=ACTOR_NAME, role="engineer", action="override", object_type="diagnosis", object_id=ov.id, note="Demonstration (showcase script): a person names this kind of event; the label is reused for similar events.", new_value={"fault_type": new_label, "cause_class": "process"}),
    ]
    effects = [apply_decision(ws, settings, d) for d in decisions]
    # downstream: another event with the same leading signals is diagnosed again and picks up the person's label
    flags_by_id = {f.id: f for f in ws.flags()}
    later = _pick(diags, used, lambda d: sig(d) == sig(ov))
    downstream = None
    if later is not None:
        fl = [flags_by_id[i] for i in later.flag_ids if i in flags_by_id and flags_by_id[i].kind in ("anomaly", "drift")]
        if fl:
            new = diagnose_flags(ws, settings, fl, use_llm=False)
            if new:
                downstream = {"event_of": later.id, "before": {"fault_type": later.fault_type, "cause_class": later.cause_class}, "after": {"id": new[0].id, "fault_type": new[0].fault_type, "cause_class": new[0].cause_class}}
    after = {d.id: {"fault_type": d.fault_type, "cause_class": d.cause_class, "human_status": d.human_status} for d in ws.diagnoses() if d.id in before}
    return {"decisions": [{"action": d.action, "diagnosis": d.object_id, "note": d.note, "new_value": d.new_value} for d in decisions], "before": before, "after": after, "effects": effects, "downstream": downstream}


def _chat_step(ws: Workspace, settings: Any, lang: str) -> dict[str, Any]:
    from .llm.agent import chat

    flags = [f for f in ws.flags() if f.kind in ("anomaly", "drift") and f.signals_ranked]
    if not flags:
        return {"skipped": "no sustained event in this run"}
    f = max(flags, key=lambda x: x.severity)
    question = f"Why was {f.id} flagged, and is it the process or the data?"
    t0 = time.time()
    out = chat(ws, settings, question, context={"object_type": "flag", "object_id": f.id, "flag_id": f.id}, actor=f"human:{ACTOR_NAME}(engineer)", language=lang)
    return {"flag": f.id, "question": question, "answer": out.get("answer"), "citations": out.get("citations"), "source": out.get("source"), "seconds": round(time.time() - t0, 1)}


def run_showcase(run_id: str, settings: Any, rules_file: Optional[str] = None, chat_q: bool = True, lang: str = "en") -> dict[str, Any]:
    ws = Workspace(run_id=run_id, settings=settings)
    if not ws.exists("diagnoses"):
        raise RuntimeError(f"run {run_id} has no diagnoses yet: run the analysis first")
    res: dict[str, Any] = {"run_id": run_id}
    res["rules"] = _rules_step(ws, settings, rules_file)
    res["human_in_the_loop"] = _hitl_step(ws, settings)
    res["chat"] = _chat_step(ws, settings, lang) if chat_q else {"skipped": "--no-chat"}
    try:
        from .report import ensure_report

        rep = ensure_report(ws, settings, lang=lang, use_llm=False, force=True, ask_model=False)
        res["report"] = rep.get("path") if isinstance(rep, dict) else str(rep)
    except Exception as e:  # the showcase results stand on their own
        res["report_error"] = str(e)[:300]
    ws.write_json("showcase.json", res)
    ws.log.record(f"human:{ACTOR_NAME}(engineer)", "showcase", "run", run_id, {"n_rules": len(res["rules"]["rules"]), "n_rule_checks": res["rules"]["n_checks"], "downstream": bool((res["human_in_the_loop"] or {}).get("downstream"))})
    return res
