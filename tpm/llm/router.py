"""Router: the only module that talks to a model. Route per task from the active profile:

    external -> egress guard (strict per profile) -> AnthropicProvider
             on block / failure -> local
    local    -> OllamaProvider.pick_model() -> chat (JSON schema enforced, one repair round)
             on failure -> template (LLMResult ok=False)

Every attempt (including unavailable providers and guard blocks) is written to the egress ledger.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from ..config import Settings, get_settings
from ..contracts import EgressRecord, LLMResult
from . import guard as guard_mod
from . import ledger as ledger_mod
from . import prompts as prompts_mod
from .providers import AnthropicProvider, OllamaProvider, ProviderError, extract_json, validate_schema

PREVIEW_CHARS = 500


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


def _record(ws: Any, *, task: str, purpose: str, route: str, provider: str, model: str, payload: Any, artifact_types: list[str], guard_result: str, guard_reason: str, response_text: str = "", latency_ms: Optional[int] = None, ok: bool = True, error: Optional[str] = None, preview: bool = False) -> EgressRecord:
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
        payload_preview=raw[:PREVIEW_CHARS] if preview else "",
        guard_result=guard_result,
        guard_reason=guard_reason[:500],
        response_hash=ledger_mod.sha256_of(response_text) if response_text else "",
        latency_ms=latency_ms,
        ok=ok,
        error=(error or None),
    )
    return ledger_mod.record(ws, rec)


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


def _chat_with_repair(provider: Any, messages: list[dict[str, str]], schema: Optional[dict[str, Any]], max_tokens: Optional[int], model: Optional[str] = None) -> tuple[str, Any, int]:
    """One call, plus one repair round when a schema is required and the JSON is missing/invalid."""
    kwargs: dict[str, Any] = {}
    if model:
        kwargs["model"] = model
    text, parsed, latency = provider.chat(messages, schema=schema, max_tokens=max_tokens, **kwargs)
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
    text2, parsed2, latency2 = provider.chat(repair, schema=schema, max_tokens=max_tokens, **kwargs)
    if parsed2 is None:
        parsed2 = extract_json(text2)
    if parsed2 is not None and not validate_schema(parsed2, schema):
        return text2, parsed2, latency + latency2
    # keep the better of the two
    if parsed2 is not None and parsed is None:
        return text2, parsed2, latency + latency2
    return text, parsed, latency + latency2


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
        g = guard_mod.check(payload, settings, strict=prof.guard_strict)
        ext = AnthropicProvider(settings)
        ext_model = settings.external_llm.model
        if not g.allowed:
            _record(ws, task=task, purpose=purpose, route="external", provider=settings.external_llm.provider, model=ext_model, payload=payload, artifact_types=g.artifact_types or artifact_types, guard_result="blocked", guard_reason=g.reason, ok=False, error="blocked by egress guard")
            errors.append(f"guard blocked external route: {g.reason}")
        elif not ext.is_available():
            _record(ws, task=task, purpose=purpose, route="external", provider=settings.external_llm.provider, model=ext_model, payload=g.sanitized_payload, artifact_types=g.artifact_types, guard_result="allowed", guard_reason=g.reason, ok=False, error=f"no API key in env {settings.external_llm.api_key_env}")
            errors.append("external provider unavailable (no API key)")
        else:
            sys_t, user_t = prompts_mod.render(task, g.sanitized_payload, language=language, system_override=system, schema=schema)
            msgs = _messages(sys_t, user_t)
            try:
                text, parsed, latency = _chat_with_repair(ext, msgs, schema, max_tokens or settings.external_llm.max_tokens)
                rec = _record(ws, task=task, purpose=purpose, route="external", provider=settings.external_llm.provider, model=ext_model, payload=g.sanitized_payload, artifact_types=g.artifact_types, guard_result="allowed", guard_reason=g.reason, response_text=text, latency_ms=latency, ok=True, preview=True)
                return _finish(text, parsed, schema, f"llm-external:{ext_model}", ext_model, "external", rec.id, latency)
            except Exception as e:
                _record(ws, task=task, purpose=purpose, route="external", provider=settings.external_llm.provider, model=ext_model, payload=g.sanitized_payload, artifact_types=g.artifact_types, guard_result="allowed", guard_reason=g.reason, ok=False, error=str(e)[:500], preview=True)
                errors.append(f"external failed: {e}")

    # ---------------- local ----------------
    local = OllamaProvider(settings)
    guard_result = "fallback" if came_from_external else "n/a"
    guard_reason = ("; ".join(errors)[:400]) if came_from_external else "local route: guard not required"
    model = local.pick_model() if local.is_available() else None
    if model is None:
        err = "ollama not reachable" if not local.is_available() else f"none of the configured models is pulled ({', '.join(local.candidate_models())})"
        _record(ws, task=task, purpose=purpose, route="local", provider=settings.local_llm.provider, model=settings.local_llm.model, payload=payload, artifact_types=artifact_types, guard_result=guard_result, guard_reason=guard_reason, ok=False, error=err)
        errors.append(err)
    else:
        sys_t, user_t = prompts_mod.render(task, payload, language=language, system_override=system, schema=schema)
        msgs = _messages(sys_t, user_t)
        try:
            text, parsed, latency = _chat_with_repair(local, msgs, schema, max_tokens or 1500, model=model)
            rec = _record(ws, task=task, purpose=purpose, route="local", provider=settings.local_llm.provider, model=model, payload=payload, artifact_types=artifact_types, guard_result=guard_result, guard_reason=guard_reason, response_text=text, latency_ms=latency, ok=True)
            res = _finish(text, parsed, schema, f"llm-local:{model}", model, "local", rec.id, latency)
            if res.ok:
                return res
            errors.append(res.error or "local model returned invalid output")
            res.error = "; ".join(errors)
            return res
        except Exception as e:
            _record(ws, task=task, purpose=purpose, route="local", provider=settings.local_llm.provider, model=model, payload=payload, artifact_types=artifact_types, guard_result=guard_result, guard_reason=guard_reason, ok=False, error=str(e)[:500])
            errors.append(f"local failed: {e}")

    # ---------------- template ----------------
    _log_template(ws, task, purpose, errors)
    return LLMResult(text="", data=None, source="template", model="", route="none", ledger_id=None, ok=False, error="; ".join(errors) or "no model available")


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
    """Raw multi-turn call to the LOCAL model (used by the tool agent). Ledger-recorded; never external."""
    settings = _settings(settings)
    local = OllamaProvider(settings)
    model = local.pick_model() if local.is_available() else None
    payload = {"messages": messages}
    types = artifact_types or ["chat"]
    if model is None:
        err = "ollama not reachable" if not local.is_available() else "no configured model pulled"
        _record(ws, task=task, purpose=purpose, route="local", provider=settings.local_llm.provider, model=settings.local_llm.model, payload=payload, artifact_types=types, guard_result="n/a", guard_reason="local route", ok=False, error=err)
        return LLMResult(text="", data=None, source="template", route="none", ok=False, error=err)
    try:
        text, parsed, latency = local.chat(messages, schema=schema, max_tokens=max_tokens or 1200, model=model, temperature=temperature)
        if schema and parsed is None:
            parsed = extract_json(text)
        rec = _record(ws, task=task, purpose=purpose, route="local", provider=settings.local_llm.provider, model=model, payload=payload, artifact_types=types, guard_result="n/a", guard_reason="local route", response_text=text, latency_ms=latency, ok=True)
        return _finish(text, parsed, schema, f"llm-local:{model}", model, "local", rec.id, latency)
    except Exception as e:
        _record(ws, task=task, purpose=purpose, route="local", provider=settings.local_llm.provider, model=model, payload=payload, artifact_types=types, guard_result="n/a", guard_reason="local route", ok=False, error=str(e)[:500])
        return LLMResult(text="", data=None, source="template", model=model, route="local", ok=False, error=str(e))


def available(settings: Any = None) -> dict[str, Any]:
    """Which routes are reachable right now (UI status bar)."""
    settings = _settings(settings)
    prof = settings.active_profile
    local = OllamaProvider(settings)
    up = local.is_available()
    picked = local.pick_model() if up else None
    ext = AnthropicProvider(settings)
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
        "external": bool(prof.allow_external and ext.is_available()),
        "external_key_present": ext.is_available(),
        "external_model": settings.external_llm.model,
        "external_base_url": settings.external_llm.base_url,
        "routing": {t: settings.route_for(t) for t in prompts_mod.TASKS},
        "mode": "llm-external+local" if (prof.allow_external and ext.is_available() and up and picked) else ("llm-local" if (up and picked) else "template"),
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
        lines.append("External route enabled" + (" and key present." if info["external_key_present"] else f" but env {settings.external_llm.api_key_env} is empty: external tasks fall back to local."))
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
