"""Egress ledger: every model call (local and external, attempted or completed) becomes an EgressRecord in
workspace/<run>/egress_ledger.jsonl and a hash-chained decision-log entry (action "egress").
With ws=None an in-memory ledger is kept so tests and ad-hoc calls still have a trail."""
from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter
from typing import Any, Optional

from ..config import Settings
from ..contracts import EgressRecord, now_iso

_MEM: list[EgressRecord] = []
_MEM_MAX = 1000
_lock = threading.RLock()
_counters: dict[str, int] = {}


def sha256_of(obj: Any) -> str:
    if isinstance(obj, (bytes, bytearray)):
        data = bytes(obj)
    elif isinstance(obj, str):
        data = obj.encode("utf-8")
    else:
        data = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _ws_key(ws: Any) -> str:
    return str(getattr(ws, "dir", "")) if ws is not None else "__memory__"


def next_id(ws: Any = None) -> str:
    key = _ws_key(ws)
    with _lock:
        if key not in _counters:
            n = 0
            if ws is not None:
                try:
                    for rec in ws.read_jsonl("egress_ledger"):
                        try:
                            n = max(n, int(str(rec.get("id", "EGR-0")).split("-")[-1]))
                        except Exception:
                            continue
                except Exception:
                    n = 0
            else:
                n = len(_MEM)
            _counters[key] = n
        _counters[key] += 1
        return f"EGR-{_counters[key]:06d}"


def record(ws: Any, rec: EgressRecord) -> EgressRecord:
    """Persist one record (jsonl + decision log) or keep it in memory when ws is None. Never raises."""
    if not rec.id:
        rec.id = next_id(ws)
    if not rec.ts:
        rec.ts = now_iso()
    with _lock:
        _MEM.append(rec)
        if len(_MEM) > _MEM_MAX:
            del _MEM[: len(_MEM) - _MEM_MAX]
    if ws is None:
        return rec
    try:
        with _lock:  # several threads (complete_many, chat turns) and Workspace objects append to the same file
            ws.append_jsonl("egress_ledger", rec.model_dump())
    except Exception:
        pass
    try:
        actor = f"llm:{rec.route}:{rec.model or 'none'}"
        payload = rec.model_dump(exclude={"payload_preview"})
        payload["payload_preview"] = rec.payload_preview[:120] if rec.payload_preview else ""
        ws.log.record(actor, "egress", "egress", rec.id, payload)
    except Exception:
        pass
    return rec


def read(ws: Any = None) -> list[EgressRecord]:
    if ws is None:
        with _lock:
            return list(_MEM)
    out: list[EgressRecord] = []
    try:
        for d in ws.read_jsonl("egress_ledger"):
            try:
                out.append(EgressRecord(**d))
            except Exception:
                continue
    except Exception:
        pass
    return out


def memory_records() -> list[EgressRecord]:
    with _lock:
        return list(_MEM)


def clear_memory() -> None:
    with _lock:
        _MEM.clear()
        _counters.pop("__memory__", None)


def summary(ws: Any = None, last_n: int = 20) -> dict[str, Any]:
    recs = read(ws)
    by = lambda attr: dict(Counter(getattr(r, attr) or "" for r in recs))  # noqa: E731
    ext = [r for r in recs if r.route == "external"]
    return {
        "n_records": len(recs),
        "n_external": len(ext),
        "n_external_sent": len([r for r in ext if r.guard_result == "allowed" and r.ok]),
        "n_blocked": len([r for r in recs if r.guard_result == "blocked"]),
        "n_fallback": len([r for r in recs if r.guard_result == "fallback"]),
        "n_failed": len([r for r in recs if not r.ok]),
        "by_route": by("route"),
        "by_model": by("model"),
        "by_task": by("task"),
        "by_guard_result": by("guard_result"),
        "total_payload_bytes": int(sum(r.payload_bytes for r in recs)),
        "external_payload_bytes": int(sum(r.payload_bytes for r in ext if r.guard_result == "allowed" and r.ok)),
        "total_latency_ms": int(sum(r.latency_ms or 0 for r in recs)),
        "last": [r.model_dump() for r in recs[-last_n:]],
    }


def _avg(values: list[int]) -> Optional[int]:
    return int(sum(values) / len(values)) if values else None


def usage(ws: Any = None, settings: Optional[Settings] = None) -> dict[str, Any]:
    """External-model use of one run, for the budget and the Data-flow view: calls, tokens, blocks, what is left of
    the per-run call budget, and average latency per route (overall and per task)."""
    recs = read(ws)
    if settings is None:
        settings = getattr(ws, "settings", None)
    ext = [r for r in recs if r.route == "external"]
    ext_ok = [r for r in ext if r.ok and r.guard_result == "allowed"]
    local_ok = [r for r in recs if r.route == "local" and r.ok]
    by_task: dict[str, dict[str, Any]] = {}
    for task in sorted({r.task for r in recs}):
        t_ext = [r for r in ext_ok if r.task == task]
        t_loc = [r for r in local_ok if r.task == task]
        by_task[task] = {
            "external_ok": len(t_ext),
            "local_ok": len(t_loc),
            "blocked": len([r for r in ext if r.task == task and r.guard_result == "blocked"]),
            "input_tokens": int(sum(r.input_tokens for r in t_ext)),
            "output_tokens": int(sum(r.output_tokens for r in t_ext)),
            "avg_latency_ms_external": _avg([r.latency_ms for r in t_ext if r.latency_ms is not None]),
            "avg_latency_ms_local": _avg([r.latency_ms for r in t_loc if r.latency_ms is not None]),
        }
    out_tokens = int(sum(r.output_tokens for r in ext))
    cap_calls = int(settings.external_llm.max_calls_per_run) if settings is not None else None
    cap_tokens = int(settings.external_llm.max_output_tokens_per_run) if settings is not None else None
    return {
        "external_calls": len(ext),
        "external_ok": len(ext_ok),
        "input_tokens": int(sum(r.input_tokens for r in ext)),
        "output_tokens": out_tokens,
        "blocked": len([r for r in ext if r.guard_result == "blocked"]),
        "budget_refused": len([r for r in ext if r.guard_result == "budget"]),
        "max_calls_per_run": cap_calls,
        "budget_left_calls": max(0, cap_calls - len(ext_ok)) if cap_calls is not None else None,
        "max_output_tokens_per_run": cap_tokens,
        "budget_left_output_tokens": max(0, cap_tokens - out_tokens) if cap_tokens is not None else None,
        "avg_latency_ms_external": _avg([r.latency_ms for r in ext_ok if r.latency_ms is not None]),
        "avg_latency_ms_local": _avg([r.latency_ms for r in local_ok if r.latency_ms is not None]),
        "by_task": by_task,
    }


def data_flow_statement(ws: Any, settings: Settings, language: str = "en") -> str:
    """Plain-language 'what left the operator environment, to which model, and why' for the Data-flow record."""
    s = summary(ws, last_n=0)
    recs = read(ws)
    prof = settings.active_profile
    lines: list[str] = []
    lines.append(f"Profile: {settings.profile} ({'external model allowed through the egress guard' if prof.allow_external else 'no network model calls allowed'}).")
    lines.append(f"Local model configured: {settings.local_llm.model} via {settings.local_llm.provider} at {settings.local_llm.base_url} (data stays on this machine).")
    used_local = Counter(r.model for r in recs if r.route == "local" and r.ok and r.model)
    if used_local:
        used = ", ".join(f"{m} x{n}" for m, n in used_local.most_common())
        note = "" if settings.local_llm.model in used_local else f" (configured model not pulled; fell back to the first available model)"
        lines.append(f"Local model actually used in this run: {used}{note}.")
    if prof.allow_external:
        ep = settings.external_llm.base_url or "provider default endpoint"
        lines.append(f"External model: {settings.external_llm.model} via {settings.external_llm.provider} ({ep}).")
    lines.append("")
    if not recs:
        lines.append("No language-model calls have been made in this run. Nothing left the operator environment.")
        return "\n".join(lines)
    lines.append(f"Model calls recorded: {s['n_records']} (local: {s['by_route'].get('local', 0)}, external attempts: {s['n_external']}).")
    if s["n_external_sent"]:
        tasks = Counter(r.task for r in recs if r.route == "external" and r.guard_result == "allowed" and r.ok)
        types = Counter(t for r in recs if r.route == "external" and r.guard_result == "allowed" and r.ok for t in r.artifact_types)
        lines.append(
            f"{s['n_external_sent']} payload(s), {s['external_payload_bytes']} bytes in total, were sent to the external model. "
            f"Tasks: {', '.join(f'{k} x{v}' for k, v in tasks.items())}. Artifact types: {', '.join(f'{k} x{v}' for k, v in types.items()) or 'n/a'}. "
            "Each payload passed the egress guard: derived artifacts only (aliases, aggregates, statements, evidence IDs), no raw rows, no long series, no categorical values; "
            f"numbers rounded to {settings.guard.external_sig_digits} significant digits, dates, column names, file names and single readings removed."
        )
        tok_in, tok_out = int(sum(r.input_tokens for r in recs if r.route == "external")), int(sum(r.output_tokens for r in recs if r.route == "external"))
        if tok_in or tok_out:
            lines.append(f"Tokens reported by the external provider: {tok_in} in, {tok_out} out.")
    else:
        lines.append("No payload was sent to an external model. Nothing left the operator environment.")
    if s["n_blocked"]:
        reasons = Counter(r.guard_reason.split(" at ")[0] for r in recs if r.guard_result == "blocked")
        lines.append(f"The guard blocked {s['n_blocked']} payload(s): " + "; ".join(f"{k} (x{v})" for k, v in reasons.items()) + ". Those tasks were answered locally or by a template.")
    n_budget = len([r for r in recs if r.guard_result == "budget"])
    if n_budget:
        lines.append(f"{n_budget} call(s) were not sent because the external budget of this run was used up (external_llm.max_calls_per_run / max_output_tokens_per_run); they ran locally.")
    if s["n_fallback"]:
        lines.append(f"{s['n_fallback']} call(s) ran on the local model as a fallback after a block or an external failure.")
    if s["n_failed"]:
        lines.append(f"{s['n_failed']} call(s) failed (model unavailable or error) and were answered by code templates.")
    lines.append("")
    lines.append("Why: local models may see raw rows (they run inside the operator environment); the external model only receives "
                 "derived artifacts so that it can write hypotheses and explanations on top of them, citing evidence IDs. "
                 "Every call, including blocked and failed ones, is in egress_ledger.jsonl and in the hash-chained decision log.")
    return "\n".join(lines)
