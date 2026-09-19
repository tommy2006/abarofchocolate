"""What the pipeline stages write into the decision log for the objects they create: one entry per flag and per
data-quality check, written in bulk (DecisionLog.record_many: one transaction per 5,000 entries), plus the stage's
inferences that the stage registered without an entry of their own.

Entries carry ids, kind / status, signals (aliases), row ranges, scores and evidence ids. They never carry readings:
a check statement such as "S01 is frozen at 0.0" or a check's `values` stay in checks.jsonl, next to the data.

    from tpm.log.stage_log import log_flags, log_checks, log_stage_inferences
    log_flags(ws, flags)                 -> {"n": 4064, "seconds": 0.13}
    log_checks(ws, checks)               -> {"n": 653, "seconds": 0.02}
    log_stage_inferences(ws, "detect")   -> number of inferences logged now
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Optional


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _round(x: Any, digits: int = 4) -> Any:
    try:
        return round(float(x), digits) if x is not None and not isinstance(x, bool) else x
    except (TypeError, ValueError):
        return None


def flag_entry(f: Any, actor: str = "system:detect") -> tuple[str, str, str, str, dict[str, Any], list[str]]:
    """(actor, action, object_type, object_id, payload, evidence_ids) of one flag (a Flag or its dict)."""
    ranked = _get(f, "signals_ranked") or []
    top = [str(_get(s, "signal")) for s in ranked[:3]]
    payload = {
        "kind": _get(f, "kind"), "batch_id": _get(f, "batch_id"), "group_id": _get(f, "group_id"), "row_start": _get(f, "row_start"), "row_end": _get(f, "row_end"),
        "score": _round(_get(f, "score")), "severity": _round(_get(f, "severity"), 3), "cause": _get(f, "likely_cause_class"), "pattern_id": _get(f, "pattern_id"), "top_signals": top,
    }
    return actor, "flag", "flag", str(_get(f, "id")), payload, list(_get(f, "evidence_ids") or [])


def check_entry(c: Any, actor: str = "system:quality") -> tuple[str, str, str, str, dict[str, Any], list[str]]:
    """(actor, action, object_type, object_id, payload, evidence_ids) of one data-quality check (a CheckResult or its
    dict). Ids, type, status, signals and rows only: the statement and `values` may quote readings and stay out."""
    payload = {
        "check_type": _get(c, "check_type"), "category": _get(c, "category"), "status": _get(c, "status"), "severity": _round(_get(c, "severity"), 3),
        "signals": list(_get(c, "signals") or []), "batch_id": _get(c, "batch_id"), "group_id": _get(c, "group_id"), "row_start": _get(c, "row_start"), "row_end": _get(c, "row_end"), "rule_id": _get(c, "rule_id"),
    }
    return actor, "check", "check", str(_get(c, "check_id")), payload, list(_get(c, "evidence_ids") or [])


def log_flags(ws: Any, flags: Iterable[Any], actor: str = "system:detect") -> dict[str, Any]:
    """One entry per flag, all flags (there is no cap), in bulk. Returns {"n", "seconds"}."""
    t0 = time.perf_counter()
    out = ws.log.record_many(flag_entry(f, actor) for f in flags)
    return {"n": len(out), "seconds": round(time.perf_counter() - t0, 3)}


def log_checks(ws: Any, checks: Iterable[Any], actor: str = "system:quality") -> dict[str, Any]:
    """One entry per data-quality check (baseline checks and rule checks), in bulk. Returns {"n", "seconds"}."""
    t0 = time.perf_counter()
    out = ws.log.record_many(check_entry(c, actor) for c in checks)
    return {"n": len(out), "seconds": round(time.perf_counter() - t0, 3)}


def log_stage_inferences(ws: Any, stage: str, actor: Optional[str] = None) -> int:
    """Log, in one transaction, every inference of `stage` that has no decision-log entry yet: claims the stage's
    modules registered in inferences.jsonl without logging them. Returns how many were logged. Never raises (the
    completeness audit of `tpm verify-log` names whatever is still missing)."""
    try:
        logged = ws.log.logged_ids("inference")
        todo = [i for i in ws.inferences.all() if i.stage == stage and i.id not in logged]
        if not todo:
            return 0
        ws.log.record_many(
            (actor or f"system:{stage}", "inference", "inference", i.id, {"subject": i.subject, "claim": str(i.claim)[:300], "status": i.status, "confidence": i.confidence, "source": i.source, "created_at": i.created_at, "logged_at_stage_end": True}, i.evidence_ids)
            for i in todo
        )
        return len(todo)
    except Exception:
        return 0
