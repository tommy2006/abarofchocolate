"""diagnose stage: assemble Diagnosis objects from flags/patterns/propagation, critique them, persist.

Public functions (see docs/ARCHITECTURE.md):
    run_diagnose(ws, settings, ctx)              -> summary; writes diagnoses.jsonl
    diagnose_flags(ws, settings, flags)          -> list[Diagnosis] for the streaming path (appended)
    apply_override(ws, settings, decision)       -> accept/question/override/dismiss a diagnosis; overrides
                                                    are stored as human-labelled examples in human_labels.jsonl

Model use: on the local route one diagnosis after the other gets narrative + critique while the LLM allowance lasts.
When both tasks route to the external model and that route is usable (hybrid / eu-hosted), all diagnoses are built from
the templates first and the strongest external_llm.max_narratives_per_run are sent concurrently (docs/HYBRID_SPEC.md 5).
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Optional

from ..contracts import Diagnosis, Flag, HumanDecision, PropagationStep
from .critique import cited_evidence, code_checks, critique_diagnosis, objections_payload, parse_objections
from .diagnosis import add_llm_narrative, apply_narrative, build_diagnosis, merge_narrative, narrative_payload

EVENT_KINDS = ("anomaly", "drift", "cascade")
EXTERNAL_MISSES_BEFORE_STOP = 2  # replies that did not come from the external model before the concurrent path stops asking


def _next_diag_id(ws) -> int:
    n = 0
    for d in ws.read_jsonl("diagnoses"):
        try:
            n = max(n, int(str(d.get("id", "DIAG-0")).split("-")[-1]))
        except ValueError:
            pass
    return n


def _load_context(ws, settings) -> dict[str, Any]:
    baseline = ws.read_json("baseline", {}) or {}
    detect_meta = ws.read_json("detect_meta", {}) or {}
    detect_meta.setdefault("window", int(settings.detect.window))
    patterns = {p.get("id"): p for p in (ws.read_json("patterns", []) or [])}
    propagation = ws.read_json("propagation", {}) or {}
    human_labels = [h for h in ws.read_jsonl("human_labels.jsonl") if isinstance(h, dict)]
    try:
        schema = ws.schema()
    except Exception:
        schema = None
    return {"baseline": baseline, "detect_meta": detect_meta, "patterns": patterns, "propagation": propagation, "human_labels": human_labels, "schema": schema}


def _group_events(flags: list[Flag]) -> list[tuple[str, Optional[str], list[Flag], Optional[Flag]]]:
    """(group, pattern_id, event flags, onset flag) per (group, pattern); flags without a pattern are
    grouped per group and cause class."""
    events = [f for f in flags if f.kind in ("anomaly", "drift") and (f.human_status != "dismissed")]
    onsets = [f for f in flags if f.kind == "changepoint"]
    buckets: dict[tuple[str, str], list[Flag]] = {}
    for f in events:
        key = (f.group_id or "", f.pattern_id or f"cause:{f.likely_cause_class}")
        buckets.setdefault(key, []).append(f)
    out = []
    for (g, key), fl in buckets.items():
        main = max(fl, key=lambda f: f.score * (f.row_end - f.row_start + 1))
        # the onset flag closest before/at the main event start
        cands = [o for o in onsets if o.group_id == g and o.row_start <= main.row_start + 5]
        onset = max(cands, key=lambda o: o.row_start) if cands else None
        out.append((g, key if not key.startswith("cause:") else None, fl, onset))
    return out


def _build_event(ws, c: dict[str, Any], diag_id: str, g: str, pid: Optional[str], fl: list[Flag], onset: Optional[Flag]) -> Diagnosis:
    chain: list[PropagationStep] = []
    for f in sorted(fl, key=lambda f: -f.score):
        steps = c["propagation"].get(f.id)
        if steps:
            chain = [PropagationStep(**s) for s in steps]
            break
    pattern = c["patterns"].get(pid) if pid else None
    return build_diagnosis(ws, diag_id, g, fl, onset, chain, pattern, c["baseline"], c["detect_meta"], c["schema"], c["human_labels"])


def _external_route_ready(ws, settings) -> bool:
    """Narrative and critique both go to the external model and that route can be used right now (profile, allowed
    model, endpoint, API key, run budget). Only then are the model calls made concurrently: there is one local model."""
    try:
        from ..llm import external_ready

        return all(external_ready(task, ws, settings)[0] for task in ("diagnosis_narrative", "critique"))
    except Exception:
        return False


def _ask_model(ws, settings, diag: Diagnosis, payload: dict[str, Any], checks: list[dict[str, Any]], evidence: list[dict[str, Any]], language: str, may_start: Callable[[], bool]) -> tuple[Any, Any]:
    """One worker of the concurrent path: narrative, then critique of ONE diagnosis. Only the model calls happen here.
    `diag` is the worker's own copy; the registries and the decision-log records of the diagnosis stay on the main thread."""
    from ..llm import complete

    res_n = res_c = None
    if may_start():
        res_n = complete("diagnosis_narrative", payload, purpose=f"explain {diag.id}", ws=ws, settings=settings, language=language)
        merge_narrative(diag, res_n)  # the critique sees the narrative, as in the sequential path
    if may_start():
        res_c = complete("critique", objections_payload(diag, checks, evidence), purpose=f"critique {diag.id}", ws=ws, settings=settings, language=language)
    return res_n, res_c


def _model_replies(ws, settings, diags: list[Diagnosis], flags_by_id: dict[str, Flag], c: dict[str, Any], window: int, language: str, start_within_s: float, wait_s: float) -> dict[str, tuple[Any, Any]]:
    """(narrative reply, critique reply) per diagnosis id, asked concurrently with external_llm.max_parallel workers. No
    call starts after start_within_s; calls on the wire may finish until wait_s, later ones are abandoned (their
    diagnosis stays template-only). A reply that did not come from the external model means the route failed and the
    call fell back to the single local model: after EXTERNAL_MISSES_BEFORE_STOP of those no further call is started."""
    if not diags or start_within_s <= 0:
        return {}
    t0 = time.time()
    stop = threading.Event()
    misses: list[str] = []

    def may_start() -> bool:
        return not stop.is_set() and time.time() - t0 < start_within_s

    def work(job: tuple[Diagnosis, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]) -> tuple[Any, Any]:
        out = _ask_model(ws, settings, *job, language, may_start)
        misses.extend(job[0].id for res in out if res is not None and res.route != "external")
        if len(misses) >= EXTERNAL_MISSES_BEFORE_STOP:
            stop.set()
        return out

    # payloads, code checks and evidence statements are read here, on the main thread
    jobs = [(d.model_copy(deep=True), narrative_payload(ws, d), code_checks(ws, d, flags_by_id, c["baseline"], c["patterns"], window), cited_evidence(ws, d)) for d in diags]
    pool = ThreadPoolExecutor(max_workers=max(1, min(int(settings.external_llm.max_parallel), len(jobs))), thread_name_prefix="tpm-diagnose-llm")
    futures = {job[0].id: pool.submit(work, job) for job in jobs}
    replies: dict[str, tuple[Any, Any]] = {}
    for did, fut in futures.items():
        try:
            replies[did] = fut.result(timeout=max(0.0, t0 + max(wait_s, start_within_s) - time.time()))
        except FutureTimeout:
            stop.set()
        except Exception:
            pass
    pool.shutdown(wait=False, cancel_futures=True)
    return replies


def _diagnose_concurrent(ws, settings, c: dict[str, Any], groups: list[tuple[str, Optional[str], list[Flag], Optional[Flag]]], n: int, flags_by_id: dict[str, Flag], window: int, language: str, t0: float, budget_s: float, llm_budget: float) -> tuple[list[Diagnosis], int, float]:
    """External route: every diagnosis is built from the templates first, then the strongest
    external_llm.max_narratives_per_run get narrative + critique from the model concurrently, inside the LLM allowance
    of the stage. The replies are applied here, in diagnosis order, with the same log records as the sequential path."""
    built: list[Diagnosis] = []
    for g, pid, fl, onset in groups:
        n += 1
        built.append(_build_event(ws, c, f"DIAG-{n:06d}", g, pid, fl, onset))
        if time.time() - t0 > budget_s:
            ws.log.record("system:diagnose", "warning", "dataset", "diagnose", {"note": f"time budget reached after {len(built)} diagnoses; {len(groups) - len(built)} event groups left undiagnosed"})
            break
    t1 = time.time()
    chosen = built[: max(0, int(settings.external_llm.max_narratives_per_run))]
    replies = _model_replies(ws, settings, chosen, flags_by_id, c, window, language, start_within_s=min(llm_budget, budget_s * 0.7 - (t1 - t0)), wait_s=budget_s - (t1 - t0))
    llm_seconds = time.time() - t1
    out: list[Diagnosis] = []
    for d in built:
        res_n, res_c = replies.get(d.id, (None, None))
        if res_n is not None:
            d = apply_narrative(ws, d, res_n)
        d = critique_diagnosis(ws, settings, d, flags_by_id, c["baseline"], c["patterns"], window, language, use_llm=False, model_objections=parse_objections(d, res_c) if res_c is not None else None)
        ws.log.record("system:diagnose", "diagnosis", "diagnosis", d.id, {"group_id": d.group_id, "fault_type": d.fault_type, "cause_class": d.cause_class, "confidence": d.confidence, "verdict": d.critique.verdict if d.critique else None, "flag_ids": d.flag_ids}, d.evidence_ids)
        out.append(d)
    return out, sum(1 for r in replies.values() if r[0] is not None), llm_seconds


def _diagnose(ws, settings, flags: list[Flag], ctx_opts: dict[str, Any], start_id: int, use_llm: bool = True, budget_s: float = 120.0) -> list[Diagnosis]:
    c = _load_context(ws, settings)
    language = str(ctx_opts.get("language") or getattr(settings.report, "default_language", "en"))
    window = int(settings.detect.window)
    flags_by_id = {f.id: f for f in flags}
    t0 = time.time()
    diags: list[Diagnosis] = []
    n = start_id
    point_diag = None
    point_first = False
    # isolated suspicious readings: ONE aggregated diagnosis, no onset / pattern / propagation analysis
    point_flags = [f for f in flags if f.kind == "point" and f.human_status != "dismissed"]
    if point_flags:
        from .diagnosis import build_point_diagnosis

        n += 1
        pd_ = build_point_diagnosis(ws, f"DIAG-{n:06d}", point_flags, ws.read_json("suspicious_rows.json", None))
        try:
            pd_ = critique_diagnosis(ws, settings, pd_, flags_by_id, c["baseline"], c["patterns"], window, language, use_llm=False)
        except Exception:
            pass
        ws.log.record("system:diagnose", "diagnosis", "diagnosis", pd_.id, {"fault_type": pd_.fault_type, "cause_class": pd_.cause_class, "confidence": pd_.confidence, "n_point_flags": len(point_flags)}, pd_.evidence_ids)
        point_diag = pd_
        point_first = bool(((ws.read_json("suspicious_rows.json", None) or {}).get("regime") or {}).get("point_dominated"))
        if point_first:
            diags.append(pd_)
    groups = _group_events(flags)
    # strongest first so a budget cut keeps the important ones
    groups.sort(key=lambda t: -max(f.score * (f.row_end - f.row_start + 1) for f in t[2]))
    llm_seconds = 0.0
    llm_budget = budget_s * 0.5  # LLM enhancement is optional: it never takes more than half the budget
    n_llm = 0
    llm_how = ""
    concurrent = use_llm and _external_route_ready(ws, settings)
    if concurrent:
        event_diags, n_llm, llm_seconds = _diagnose_concurrent(ws, settings, c, groups, n, flags_by_id, window, language, t0, budget_s, llm_budget)
        diags.extend(event_diags)
        llm_how = f"external model, up to {int(settings.external_llm.max_parallel)} calls at once, at most {int(settings.external_llm.max_narratives_per_run)} diagnoses per run; "
    else:
        for g, pid, fl, onset in groups:
            n += 1
            d = _build_event(ws, c, f"DIAG-{n:06d}", g, pid, fl, onset)
            llm_ok = use_llm and llm_seconds < llm_budget and (time.time() - t0) < budget_s * 0.7
            if llm_ok:
                t1 = time.time()
                d = add_llm_narrative(ws, settings, d, language)
                llm_seconds += time.time() - t1
                n_llm += 1
            t1 = time.time()
            d = critique_diagnosis(ws, settings, d, flags_by_id, c["baseline"], c["patterns"], window, language, use_llm=llm_ok)
            if llm_ok:
                llm_seconds += time.time() - t1
            ws.log.record("system:diagnose", "diagnosis", "diagnosis", d.id, {"group_id": g, "fault_type": d.fault_type, "cause_class": d.cause_class, "confidence": d.confidence, "verdict": d.critique.verdict if d.critique else None, "flag_ids": d.flag_ids}, d.evidence_ids)
            diags.append(d)
            if time.time() - t0 > budget_s:
                ws.log.record("system:diagnose", "warning", "dataset", "diagnose", {"note": f"time budget reached after {len(diags)} diagnoses; {len(groups) - len(diags)} event groups left undiagnosed"})
                break
    if point_diag is not None and not point_first:
        diags.append(point_diag)  # sustained events lead; the isolated readings follow as one aggregate
    if use_llm and n_llm < len(diags):
        ws.log.record("system:diagnose", "note", "dataset", "diagnose", {"note": f"LLM narrative/critique applied to the {n_llm} strongest of {len(diags)} diagnoses ({llm_how}LLM time {llm_seconds:.0f}s of a {llm_budget:.0f}s allowance); the rest are template-only"})
    return diags


def run_diagnose(ws, settings, ctx: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    t_start = time.time()
    ctx = ctx or {}
    opts = ctx.get("options") or {}
    progress = ctx.get("progress") if callable(ctx.get("progress")) else (lambda *a, **k: None)
    flags = ws.flags()
    progress(0.05, f"{len(flags)} flags loaded")
    budget = float(min(getattr(settings, "time_budget_s", 1200) * 0.15, 300.0))
    diags = _diagnose(ws, settings, flags, opts, start_id=0, use_llm=not opts.get("no_llm", False), budget_s=budget)
    ws.rewrite_jsonl("diagnoses", [d.model_dump() for d in diags])
    verdicts: dict[str, int] = {}
    causes: dict[str, int] = {}
    for d in diags:
        v = d.critique.verdict if d.critique else "n/a"
        verdicts[v] = verdicts.get(v, 0) + 1
        causes[d.cause_class] = causes.get(d.cause_class, 0) + 1
    progress(1.0, "done")
    summary = {"message": f"{len(diags)} diagnoses ({', '.join(f'{k}: {v}' for k, v in causes.items())}); critique {', '.join(f'{k}: {v}' for k, v in verdicts.items())}; {time.time() - t_start:.1f}s", "n_diagnoses": len(diags), "cause_classes": causes, "critique_verdicts": verdicts, "seconds": round(time.time() - t_start, 2), "narrative_sources": sorted({d.narrative_source for d in diags})}
    ws.log.record("system:diagnose", "stage_summary", "dataset", "diagnose", {k: v for k, v in summary.items() if k != "message"})
    return summary


def diagnose_flags(ws, settings, flags: list[Flag], use_llm: bool = True) -> list[Diagnosis]:
    """Streaming path: diagnose the flags of one batch and append to diagnoses.jsonl."""
    if not flags:
        return []
    diags = _diagnose(ws, settings, list(flags), {}, start_id=_next_diag_id(ws), use_llm=use_llm, budget_s=30.0)
    for d in diags:
        ws.append_jsonl("diagnoses", d.model_dump())
    return diags


def apply_override(ws, settings, decision: HumanDecision) -> dict[str, Any]:
    if decision.object_type != "diagnosis":
        return {}
    status = {"accept": "accepted", "question": "questioned", "override": "overridden", "dismiss": "dismissed"}.get(decision.action)
    rows = ws.read_jsonl("diagnoses")
    found = None
    for d in rows:
        if d.get("id") != decision.object_id:
            continue
        found = d
        if status:
            d["human_status"] = status
        if decision.note is not None:
            d["human_note"] = decision.note
        if decision.action == "override" and decision.new_value:
            for k in ("fault_type", "cause_class", "confidence"):
                if k in decision.new_value:
                    d[k] = decision.new_value[k]
            d["narrative_source"] = "human+" + str(d.get("narrative_source", "template"))
    if found is None:
        return {"diagnosis_id": decision.object_id, "found": False}
    ws.rewrite_jsonl("diagnoses", rows)
    effect: dict[str, Any] = {"diagnosis_id": decision.object_id, "found": True, "human_status": found.get("human_status")}
    if decision.action in ("override", "accept"):
        # store as a human-labelled example so later runs can reuse the label for matching signatures
        label = {"diagnosis_id": found["id"], "group_id": found.get("group_id"), "pattern_id": found.get("pattern_id"), "top_signals": [s.get("signal") for s in (found.get("ranked_signals") or [])[:3]], "directions": [s.get("direction") for s in (found.get("ranked_signals") or [])[:3]], "fault_type": found.get("fault_type"), "cause_class": found.get("cause_class"), "action": decision.action, "actor": f"{decision.actor_name}({decision.role})", "note": decision.note, "ts": decision.ts}
        ws.append_jsonl("human_labels.jsonl", label)
        effect["human_label_stored"] = True
    ws.log.record("system:diagnose", "override_applied", "diagnosis", decision.object_id, effect)
    return effect
