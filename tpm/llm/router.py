"""Router: the only module that talks to a model. Route per task from the active profile:

    external -> route usable? (allowed model, endpoint, API key) -> egress guard (sanitise + invariant)
             -> run budget -> AnthropicProvider
             on block / no budget / failure -> local
    local    -> OllamaProvider.pick_model() -> chat (JSON schema enforced, one repair round)
             on failure -> template (LLMResult ok=False)

Every attempt (including unavailable providers, guard blocks and budget refusals) is written to the egress ledger.
complete(), complete_many() and agent_chat() may be called from several threads at once.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from ..config import Settings, get_settings
from ..contracts import EgressRecord, LLMResult
from . import guard as guard_mod
from . import ledger as ledger_mod
from . import prompts as prompts_mod
from .providers import AnthropicProvider, OllamaProvider, ProviderError, extract_json, validate_schema

PREVIEW_CHARS = 500
EXTERNAL_MODEL_CHOICES = ["claude-sonnet-5", "claude-opus-5"]  # what the UI offers; any id still has to pass model_allowed
SANITIZER_NOTES_KEPT = 12

_BUDGET_LOCK = threading.Lock()
_INFLIGHT: dict[str, int] = {}  # run workspace -> external calls on the wire (counted against the budget until recorded)
_LOCAL_FALLBACK_LOCK = threading.Lock()  # parallel external jobs that fall back must not pile up on the one local model


def _settings(settings: Optional[Settings]) -> Settings:
    return settings or get_settings()


def _messages(system_text: str, user_text: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": system_text}, {"role": "user", "content": user_text}]


def _artifact_types(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    out: list[str] = []
    for k in payload.keys():
        t = guard_mod.ARTIFACT_KEYS.get(str(k), f"other:{k}")
        if t not in out:
            out.append(t)
    return out


def _record(ws: Any, *, task: str, purpose: str, route: str, provider: str, model: str, payload: Any, artifact_types: list[str], guard_result: str, guard_reason: str, response_text: str = "", latency_ms: Optional[int] = None, ok: bool = True, error: Optional[str] = None, preview: bool = False, usage: Optional[dict[str, int]] = None, sanitizer: Optional[dict[str, Any]] = None) -> EgressRecord:
    try:
        raw = json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else ""
    except Exception:
        raw = str(payload)
    rec = EgressRecord(
        id=ledger_mod.next_id(ws),
        task=task,
        purpose=purpose,
        route=route,
        provider=provider,
        model=model or "",
        artifact_types=list(artifact_types),
        payload_bytes=len(raw.encode("utf-8")),
        payload_hash=ledger_mod.sha256_of(raw),
        payload_preview=raw[:PREVIEW_CHARS] if preview else "",  # callers pass preview=True for SANITISED payloads only
        guard_result=guard_result,
        guard_reason=guard_reason[:500],
        response_hash=ledger_mod.sha256_of(response_text) if response_text else "",
        latency_ms=latency_ms,
        ok=ok,
        error=(error or None),
        input_tokens=int((usage or {}).get("input_tokens") or 0),
        output_tokens=int((usage or {}).get("output_tokens") or 0),
        sanitizer=dict(sanitizer or {}),
    )
    return ledger_mod.record(ws, rec)


def _sanitizer_record(g: Any) -> dict[str, Any]:
    """What the guard changed, for the ledger: counts plus the first notes (paths and reasons, never values)."""
    out = {k: v for k, v in (getattr(g, "sanitizer", None) or {}).items() if v}
    notes = [str(n)[:200] for n in (getattr(g, "notes", None) or [])]
    if notes:
        out["notes"] = notes[:SANITIZER_NOTES_KEPT]
        if len(notes) > SANITIZER_NOTES_KEPT:
            out["notes_omitted"] = len(notes) - SANITIZER_NOTES_KEPT
    return out


def _finish(text: str, parsed: Any, schema: Optional[dict[str, Any]], source: str, model: str, route: str, ledger_id: Optional[str], latency: Optional[int]) -> LLMResult:
    data = parsed if isinstance(parsed, dict) else ({"result": parsed} if parsed is not None else None)
    errors = validate_schema(parsed, schema) if (schema and parsed is not None) else []
    ok = True
    err = None
    if schema and parsed is None:
        ok, err = False, "model returned no parseable JSON"
    elif errors:
        err = "schema deviations: " + "; ".join(errors[:5])
    return LLMResult(text=text, data=data, source=source, model=model, route=route, ledger_id=ledger_id, ok=ok, error=err, latency_ms=latency)


def _usage_of(provider: Any) -> dict[str, int]:
    u = getattr(provider, "last_usage", None)
    return {"input_tokens": int((u or {}).get("input_tokens") or 0), "output_tokens": int((u or {}).get("output_tokens") or 0)} if isinstance(u, dict) else {"input_tokens": 0, "output_tokens": 0}


def _reset_usage(provider: Any) -> None:
    try:
        provider.last_usage = None
    except Exception:
        pass


def _chat_with_repair(provider: Any, messages: list[dict[str, str]], schema: Optional[dict[str, Any]], max_tokens: Optional[int], model: Optional[str] = None, usage: Optional[dict[str, int]] = None) -> tuple[str, Any, int]:
    """One call, plus one repair round when a schema is required and the JSON is missing/invalid. `usage`, when given,
    accumulates the provider-reported tokens of both rounds."""
    kwargs: dict[str, Any] = {}
    if model:
        kwargs["model"] = model

    def call(msgs: list[dict[str, str]]) -> tuple[str, Any, int]:
        _reset_usage(provider)
        out = provider.chat(msgs, schema=schema, max_tokens=max_tokens, **kwargs)
        if usage is not None:
            for k, v in _usage_of(provider).items():
                usage[k] = usage.get(k, 0) + v
        return out

    text, parsed, latency = call(messages)
    if not schema:
        return text, parsed, latency
    if parsed is None:
        parsed = extract_json(text)
    errors = validate_schema(parsed, schema) if parsed is not None else ["no JSON object found"]
    if parsed is not None and not errors:
        return text, parsed, latency
    repair = messages + [
        {"role": "assistant", "content": text[:6000] if text else "{}"},
        {"role": "user", "content": "Your previous output did not match the required JSON schema (" + "; ".join(errors[:4]) + "). Return ONLY one corrected JSON object matching the schema, nothing else."},
    ]
    text2, parsed2, latency2 = call(repair)
    if parsed2 is None:
        parsed2 = extract_json(text2)
    if parsed2 is not None and not validate_schema(parsed2, schema):
        return text2, parsed2, latency + latency2
    # keep the better of the two
    if parsed2 is not None and parsed is None:
        return text2, parsed2, latency + latency2
    return text, parsed, latency + latency2


# ----------------------------------------------------------------------------------------------
# external route: availability and run budget
# ----------------------------------------------------------------------------------------------


def _external_unavailable(settings: Settings, task: Optional[str], ext: Optional[AnthropicProvider] = None) -> Optional[str]:
    """Why the external route cannot be used right now (None = usable): profile, blocked model, EU endpoint, API key."""
    why = settings.external_block_reason(task)
    if why:
        return why
    ext = ext or AnthropicProvider(settings)
    if not ext.is_available():
        return f"no API key in env {settings.external_llm.api_key_env}"
    return None


def _budget(ws: Any, settings: Settings, reserve: bool = False) -> tuple[bool, str]:
    """May this run make one more external call? Counts successful external calls in the run's ledger plus the calls
    that are on the wire, and the output tokens used. reserve=True books a slot; give it back with _release().
    Without a run workspace there is no budget to keep."""
    if ws is None:
        return True, "no run workspace: budget not tracked"
    cfg = settings.external_llm
    key = ledger_mod._ws_key(ws)
    with _BUDGET_LOCK:
        used = ledger_mod.usage(ws, settings)
        inflight = _INFLIGHT.get(key, 0)
        if used["external_ok"] + inflight >= int(cfg.max_calls_per_run):
            return False, f"external call budget of this run is used up ({used['external_ok']} of {cfg.max_calls_per_run} calls; external_llm.max_calls_per_run)"
        if used["output_tokens"] >= int(cfg.max_output_tokens_per_run):
            return False, f"external output-token budget of this run is used up ({used['output_tokens']} of {cfg.max_output_tokens_per_run}; external_llm.max_output_tokens_per_run)"
        if reserve:
            _INFLIGHT[key] = inflight + 1
    return True, "ok"


def _release(ws: Any) -> None:
    if ws is None:
        return
    key = ledger_mod._ws_key(ws)
    with _BUDGET_LOCK:
        n = _INFLIGHT.get(key, 0) - 1
        if n > 0:
            _INFLIGHT[key] = n
        else:
            _INFLIGHT.pop(key, None)


def external_ready(task: str, ws: Any = None, settings: Any = None) -> tuple[bool, str]:
    """(usable, reason): does `task` route to the external model and can a call be made right now (profile, allowed
    model, endpoint, API key, run budget)? The chat agent asks once per turn to choose its mode."""
    settings = _settings(settings)
    if settings.route_for(task) != "external":
        return False, f"task '{task}' routes to the local model in profile '{settings.profile}'"
    why = _external_unavailable(settings, task)
    if why:
        return False, why
    ok, why = _budget(ws, settings)
    return (True, "ok") if ok else (False, why)


# ----------------------------------------------------------------------------------------------
# public API
# ----------------------------------------------------------------------------------------------


def complete(
    task: str,
    payload: dict[str, Any],
    *,
    purpose: str,
    ws: Any = None,
    settings: Any = None,
    schema: Optional[dict[str, Any]] = None,
    language: str = "en",
    system: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> LLMResult:
    settings = _settings(settings)
    prof = settings.active_profile
    route = settings.route_for(task)
    schema = schema if schema is not None else prompts_mod.schema_for(task)
    payload = guard_mod.to_plain(payload if isinstance(payload, dict) else {"payload": payload})
    artifact_types = _artifact_types(payload)
    errors: list[str] = []
    came_from_external = False

    # ---------------- external ----------------
    if route == "external" and prof.allow_external:
        came_from_external = True
        ext = AnthropicProvider(settings)
        ext_model = settings.external_model_for(task)
        provider_name = settings.external_llm.provider
        why_not = _external_unavailable(settings, task, ext)
        if why_not:
            _record(ws, task=task, purpose=purpose, route="external", provider=provider_name, model=ext_model, payload=payload, artifact_types=artifact_types, guard_result="unavailable", guard_reason=why_not, ok=False, error=why_not[:500])
            errors.append(f"external provider unavailable ({why_not})")
        else:
            g = guard_mod.check(payload, settings, strict=prof.guard_strict, ws=ws)
            sanitizer = _sanitizer_record(g)
            if not g.allowed:
                _record(ws, task=task, purpose=purpose, route="external", provider=provider_name, model=ext_model, payload=payload, artifact_types=g.artifact_types or artifact_types, guard_result="blocked", guard_reason=g.reason, ok=False, error="blocked by egress guard", sanitizer=sanitizer)
                errors.append(f"guard blocked external route: {g.reason}")
            else:
                has_budget, why_budget = _budget(ws, settings, reserve=True)
                if not has_budget:
                    _record(ws, task=task, purpose=purpose, route="external", provider=provider_name, model=ext_model, payload=g.sanitized_payload, artifact_types=g.artifact_types, guard_result="budget", guard_reason=why_budget, ok=False, error="external budget exhausted", sanitizer=sanitizer)
                    errors.append(f"external budget exhausted: {why_budget}")
                else:
                    usage: dict[str, int] = {}
                    try:
                        sys_override = guard_mod.sanitize_text(system, settings, ws=ws, amap=g.alias_map)[0] if system else None
                        sys_t, user_t = prompts_mod.render(task, g.sanitized_payload, language=language, system_override=sys_override, schema=schema)
                        msgs = _messages(sys_t, user_t)
                        out_cap = max(int(max_tokens or 0), int(settings.external_llm.max_tokens))  # callers size max_tokens for the local model; thinking needs headroom
                        text, parsed, latency = _chat_with_repair(ext, msgs, schema, out_cap, model=ext_model, usage=usage)
                        rec = _record(ws, task=task, purpose=purpose, route="external", provider=provider_name, model=ext_model, payload=g.sanitized_payload, artifact_types=g.artifact_types, guard_result="allowed", guard_reason=g.reason, response_text=text, latency_ms=latency, ok=True, preview=True, usage=usage, sanitizer=sanitizer)
                        return _finish(text, parsed, schema, f"llm-external:{ext_model}", ext_model, "external", rec.id, latency)
                    except Exception as e:
                        _record(ws, task=task, purpose=purpose, route="external", provider=provider_name, model=ext_model, payload=g.sanitized_payload, artifact_types=g.artifact_types, guard_result="allowed", guard_reason=g.reason, ok=False, error=str(e)[:500], preview=True, usage=usage, sanitizer=sanitizer)
                        errors.append(f"external failed: {e}")
                    finally:
                        _release(ws)

    # ---------------- local ----------------
    # The local model runs on this machine: nothing leaves it, so the guard removes nothing. It still looks at the payload
    # in audit mode, and the ledger says what it would have removed had the call gone out (guard_mod.audit_local).
    local = OllamaProvider(settings)
    guard_result = "fallback" if came_from_external else "n/a"
    if came_from_external:
        guard_reason, audit = ("; ".join(errors)[:300] + "; answered on this machine instead (nothing left it)"), None
    else:
        guard_reason, audit = guard_mod.audit_local(payload, settings, ws=ws)
    model = local.pick_model() if local.is_available() else None
    if model is None:
        err = "ollama not reachable" if not local.is_available() else f"none of the configured models is pulled ({', '.join(local.candidate_models())})"
        _record(ws, task=task, purpose=purpose, route="local", provider=settings.local_llm.provider, model=settings.local_llm.model, payload=payload, artifact_types=artifact_types, guard_result=guard_result, guard_reason=guard_reason, ok=False, error=err, sanitizer=audit)
        errors.append(err)
    else:
        sys_t, user_t = prompts_mod.render(task, payload, language=language, system_override=system, schema=schema)
        msgs = _messages(sys_t, user_t)
        try:
            if came_from_external:
                with _LOCAL_FALLBACK_LOCK:
                    text, parsed, latency = _chat_with_repair(local, msgs, schema, max_tokens or 1500, model=model)
            else:
                text, parsed, latency = _chat_with_repair(local, msgs, schema, max_tokens or 1500, model=model)
            rec = _record(ws, task=task, purpose=purpose, route="local", provider=settings.local_llm.provider, model=model, payload=payload, artifact_types=artifact_types, guard_result=guard_result, guard_reason=guard_reason, response_text=text, latency_ms=latency, ok=True, sanitizer=audit)
            res = _finish(text, parsed, schema, f"llm-local:{model}", model, "local", rec.id, latency)
            if res.ok:
                return res
            errors.append(res.error or "local model returned invalid output")
            res.error = "; ".join(errors)
            return res
        except Exception as e:
            _record(ws, task=task, purpose=purpose, route="local", provider=settings.local_llm.provider, model=model, payload=payload, artifact_types=artifact_types, guard_result=guard_result, guard_reason=guard_reason, ok=False, error=str(e)[:500], sanitizer=audit)
            errors.append(f"local failed: {e}")

    # ---------------- template ----------------
    _log_template(ws, task, purpose, errors)
    return LLMResult(text="", data=None, source="template", model="", route="none", ledger_id=None, ok=False, error="; ".join(errors) or "no model available")


def complete_many(jobs: list[dict[str, Any]], *, ws: Any = None, settings: Any = None, max_parallel: Optional[int] = None, deadline_s: Optional[float] = None) -> list[LLMResult]:
    """Run several complete() jobs; results come back in the order of `jobs`. A job is the kwargs dict of complete()
    ({"task", "payload", "purpose", ...}; ws / settings default to the ones given here). Jobs whose route is external
    and usable run in a thread pool (external_llm.max_parallel workers); all others run one after the other, because
    there is a single local model. deadline_s: jobs that have not started that many seconds after the call return a
    template result. Never raises."""
    settings = _settings(settings)
    t0 = time.time()
    results: list[Optional[LLMResult]] = [None] * len(jobs)

    def run(i: int) -> None:
        job = dict(jobs[i])
        job.setdefault("ws", ws)
        job.setdefault("settings", settings)
        if deadline_s is not None and time.time() - t0 > float(deadline_s):
            results[i] = LLMResult(text="", data=None, source="template", route="none", ok=False, error="not started: time budget of the batch used up")
            return
        try:
            task = job.pop("task")
            payload = job.pop("payload")
            results[i] = complete(task, payload, **job)
        except Exception as e:  # a bad job must not take the batch down
            results[i] = LLMResult(text="", data=None, source="template", route="none", ok=False, error=str(e))

    def is_external(job: dict[str, Any]) -> bool:
        s = _settings(job.get("settings") or settings)
        return s.route_for(str(job.get("task"))) == "external" and _external_unavailable(s, str(job.get("task"))) is None

    pooled = [i for i, job in enumerate(jobs) if is_external(job)]
    workers = max(1, min(int(max_parallel or settings.external_llm.max_parallel), len(pooled) or 1))
    if len(pooled) > 1 and workers > 1:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tpm-llm") as pool:
            list(pool.map(run, pooled))
    else:
        for i in pooled:
            run(i)
    for i in range(len(jobs)):
        if results[i] is None:
            run(i)
    return [r if r is not None else LLMResult(text="", data=None, source="template", route="none", ok=False, error="job did not run") for r in results]


def _log_template(ws: Any, task: str, purpose: str, errors: list[str]) -> None:
    if ws is None:
        return
    try:
        ws.log.record("system:llm", "llm_template_fallback", "task", task, {"purpose": purpose, "errors": errors[:5]})
    except Exception:
        pass


def local_chat(
    messages: list[dict[str, str]],
    *,
    task: str,
    purpose: str,
    ws: Any = None,
    settings: Any = None,
    schema: Optional[dict[str, Any]] = None,
    max_tokens: Optional[int] = None,
    artifact_types: Optional[list[str]] = None,
    temperature: Optional[float] = None,
) -> LLMResult:
    """Raw multi-turn call to the LOCAL model (used by the tool agent). Ledger-recorded; never external. The messages
    are sent as they are (the agent's local tools may have read exact rows); the ledger records, in audit mode, what the
    guard would have replaced had they gone out."""
    settings = _settings(settings)
    local = OllamaProvider(settings)
    model = local.pick_model() if local.is_available() else None
    payload = {"messages": messages}
    types = artifact_types or ["chat"]
    guard_reason, audit = guard_mod.audit_messages(messages, settings, ws=ws)
    if model is None:
        err = "ollama not reachable" if not local.is_available() else "no configured model pulled"
        _record(ws, task=task, purpose=purpose, route="local", provider=settings.local_llm.provider, model=settings.local_llm.model, payload=payload, artifact_types=types, guard_result="n/a", guard_reason=guard_reason, ok=False, error=err, sanitizer=audit)
        return LLMResult(text="", data=None, source="template", route="none", ok=False, error=err)
    try:
        text, parsed, latency = local.chat(messages, schema=schema, max_tokens=max_tokens or 1200, model=model, temperature=temperature)
        if schema and parsed is None:
            parsed = extract_json(text)
        rec = _record(ws, task=task, purpose=purpose, route="local", provider=settings.local_llm.provider, model=model, payload=payload, artifact_types=types, guard_result="n/a", guard_reason=guard_reason, response_text=text, latency_ms=latency, ok=True, sanitizer=audit)
        return _finish(text, parsed, schema, f"llm-local:{model}", model, "local", rec.id, latency)
    except Exception as e:
        _record(ws, task=task, purpose=purpose, route="local", provider=settings.local_llm.provider, model=model, payload=payload, artifact_types=types, guard_result="n/a", guard_reason=guard_reason, ok=False, error=str(e)[:500], sanitizer=audit)
        return LLMResult(text="", data=None, source="template", model=model, route="local", ok=False, error=str(e))


def agent_chat(
    messages: list[dict[str, str]],
    *,
    task: str,
    purpose: str,
    ws: Any = None,
    settings: Any = None,
    schema: Optional[dict[str, Any]] = None,
    max_tokens: Optional[int] = None,
    payload_parts: Optional[list[Any]] = None,
    artifact_types: Optional[list[str]] = None,
    temperature: Optional[float] = None,
) -> LLMResult:
    """Multi-turn call for the tool agent, routed like complete(). It goes to the external model only when the task
    routes external, the route is usable (profile, allowed model, endpoint, API key), the run budget allows it AND
    every element of `payload_parts` (the structured objects the messages were built from: context, tool results)
    passes guard.check. The message texts themselves are then run through the string sanitiser and the invariant,
    and that sanitised text is what is sent and previewed in the ledger.

    Anything that stops the call BEFORE sending (local route, unavailable, guard, budget) makes this behave exactly
    like local_chat(). A failure of the external call itself returns ok=False with route="external": the agent
    restarts the turn in local mode instead of mixing the two in one conversation."""
    settings = _settings(settings)
    local_kwargs: dict[str, Any] = dict(task=task, purpose=purpose, ws=ws, settings=settings, schema=schema, max_tokens=max_tokens, artifact_types=artifact_types, temperature=temperature)
    if settings.route_for(task) != "external":
        return local_chat(messages, **local_kwargs)
    prof = settings.active_profile
    ext = AnthropicProvider(settings)
    ext_model = settings.external_model_for(task)
    provider_name = settings.external_llm.provider
    types = artifact_types or ["chat"]

    def refuse(guard_result: str, reason: str, payload: Any, sanitizer: Optional[dict[str, Any]] = None) -> LLMResult:
        _record(ws, task=task, purpose=purpose, route="external", provider=provider_name, model=ext_model, payload=payload, artifact_types=types, guard_result=guard_result, guard_reason=reason, ok=False, error=reason[:500], sanitizer=sanitizer)
        return local_chat(messages, **local_kwargs)

    why_not = _external_unavailable(settings, task, ext)
    if why_not:
        return refuse("unavailable", why_not, {"messages": len(messages)})
    sanitizer: dict[str, Any] = {}
    amap: dict[str, str] = {}
    for i, part in enumerate(payload_parts or []):
        g = guard_mod.check(part if isinstance(part, dict) else {"tool_result": part}, settings, strict=prof.guard_strict, ws=ws)
        amap.update(g.alias_map)
        for k, v in (g.sanitizer or {}).items():
            if isinstance(v, int) and v:
                sanitizer[k] = sanitizer.get(k, 0) + v
        if not g.allowed:
            return refuse("blocked", f"payload part {i}: {g.reason}", {"messages": len(messages)}, _sanitizer_record(g))
    clean: list[dict[str, str]] = []
    for m in messages:
        text, counts = guard_mod.sanitize_text(str(m.get("content") or ""), settings, ws=ws, amap=amap)
        clean.append({"role": str(m.get("role") or "user"), "content": text})
        for k, v in counts.items():
            if v:
                sanitizer[k] = sanitizer.get(k, 0) + v
    violations = guard_mod.verify_texts([m["content"] for m in clean], settings, ws=ws, amap=amap)
    if violations:
        sanitizer["notes"] = [f"invariant: {v}" for v in violations[:SANITIZER_NOTES_KEPT]]
        return refuse("blocked", "egress invariant violated in the message text: " + "; ".join(violations[:3]), {"messages": len(messages)}, sanitizer)
    sent = {"messages": clean}
    has_budget, why_budget = _budget(ws, settings, reserve=True)
    if not has_budget:
        return refuse("budget", why_budget, sent, sanitizer)
    reason = "allowed: tool-agent messages, every structured part passed the guard; text sanitised and checked against the invariant"
    usage: dict[str, int] = {}
    try:
        _reset_usage(ext)
        out_cap = max(int(max_tokens or 0), int(settings.external_llm.max_tokens))
        text, parsed, latency = ext.chat(clean, schema=schema, max_tokens=out_cap, model=ext_model)
        usage = _usage_of(ext)
        if schema and parsed is None:
            parsed = extract_json(text)
        rec = _record(ws, task=task, purpose=purpose, route="external", provider=provider_name, model=ext_model, payload=sent, artifact_types=types, guard_result="allowed", guard_reason=reason, response_text=text, latency_ms=latency, ok=True, preview=True, usage=usage, sanitizer=sanitizer)
        return _finish(text, parsed, schema, f"llm-external:{ext_model}", ext_model, "external", rec.id, latency)
    except Exception as e:
        _record(ws, task=task, purpose=purpose, route="external", provider=provider_name, model=ext_model, payload=sent, artifact_types=types, guard_result="allowed", guard_reason=reason, ok=False, error=str(e)[:500], preview=True, usage=_usage_of(ext), sanitizer=sanitizer)
        return LLMResult(text="", data=None, source="template", model=ext_model, route="external", ok=False, error=f"external failed: {e}")
    finally:
        _release(ws)


def available(settings: Any = None) -> dict[str, Any]:
    """Which routes are reachable right now (UI status bar)."""
    settings = _settings(settings)
    prof = settings.active_profile
    local = OllamaProvider(settings)
    up = local.is_available()
    picked = local.pick_model() if up else None
    ext = AnthropicProvider(settings)
    models = [settings.external_llm.model] + [m for m in (settings.external_llm.model_by_task or {}).values() if m]
    blocked = next((why for ok, why in (settings.external_llm.model_allowed(m) for m in models) if not ok), None)
    unavailable = _external_unavailable(settings, None, ext)  # profile, default model, endpoint, key; a blocked model_by_task entry only stops that task
    external = bool(prof.allow_external and unavailable is None)
    return {
        "profile": settings.profile,
        "allow_external": prof.allow_external,
        "guard_strict": prof.guard_strict,
        "local": bool(up and picked),
        "ollama_running": up,
        "local_model": picked,
        "configured_local_model": settings.local_llm.model,
        "pulled_models": local.list_models() if up else [],
        "missing_models": local.missing_models() if up else local.candidate_models() + [settings.local_llm.embedding_model],
        "pull_commands": local.pull_commands() if up else [f"ollama pull {m}" for m in local.candidate_models() + [settings.local_llm.embedding_model]],
        "embeddings": bool(up and local.has_embedding_model()),
        "embedding_model": settings.local_llm.embedding_model,
        "external": external,
        "external_key_present": ext.is_available(),
        "external_model": settings.external_llm.model,
        "external_model_by_task": dict(settings.external_llm.model_by_task or {}),
        "external_models_allowed": [m for m in EXTERNAL_MODEL_CHOICES if settings.external_llm.model_allowed(m)[0]],
        "external_model_blocked_reason": blocked,
        "external_unavailable_reason": unavailable,
        "external_base_url": settings.external_llm.base_url,
        "routing": {t: settings.route_for(t) for t in prompts_mod.TASKS},
        "mode": "llm-external+local" if (external and up and picked) else ("llm-local" if (up and picked) else ("llm-external" if external else "template")),
    }


def ensure_models(settings: Any = None) -> dict[str, Any]:
    """Report for the UI / README / check_models.py: what is pulled, what is missing, exact pull commands."""
    settings = _settings(settings)
    info = available(settings)
    local = OllamaProvider(settings)
    lines: list[str] = []
    if not info["ollama_running"]:
        lines.append(f"Ollama is not reachable at {settings.local_llm.base_url}. Start it (`ollama serve`) or install it from https://ollama.com. The app keeps working in template mode.")
    elif info["local_model"] is None:
        lines.append("Ollama is running but none of the configured models is pulled. The app runs in template mode until one is pulled.")
    elif info["local_model"] != settings.local_llm.model and not _same(info["local_model"], settings.local_llm.model):
        lines.append(f"Configured model {settings.local_llm.model} is not pulled; using fallback {info['local_model']}.")
    else:
        lines.append(f"Local model ready: {info['local_model']}.")
    if info["ollama_running"] and not info["embeddings"]:
        lines.append(f"Embedding model {settings.local_llm.embedding_model} is not pulled; search falls back to TF-IDF (local).")
    if info["pull_commands"]:
        lines.append("To complete the setup run:\n  " + "\n  ".join(info["pull_commands"]))
    if info["allow_external"]:
        if info["external"]:
            lines.append(f"External route enabled: {settings.external_llm.model}, key present. Only sanitised summaries leave (see the egress guard).")
        else:
            lines.append(f"External route enabled but not usable ({info['external_unavailable_reason']}): external tasks fall back to local.")
    else:
        lines.append("Profile is no-egress: no network model is ever called.")
    info["candidates"] = local.candidate_models()
    info["message"] = "\n".join(lines)
    return info


def _same(a: Optional[str], b: Optional[str]) -> bool:
    from .providers import _same_model

    return bool(a and b and _same_model(a, b))


def explain_guard(settings: Any = None, language: str = "en") -> str:
    settings = _settings(settings)
    return guard_mod.explain(settings, language=language)
