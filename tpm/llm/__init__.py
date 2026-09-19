"""LLM layer entry point. The ONLY way any stage may talk to a language model.

    from tpm.llm import complete
    res = complete("diagnosis_narrative", payload, purpose="explain DIAG-000003", ws=ws, settings=settings)

`task` selects the route from the active profile (local Ollama / external Anthropic through the egress
guard). The implementation lives in tpm.llm.router (agent D). Until it exists, or when no model is
reachable, `complete` returns LLMResult(source="template", ok=False) and callers use their
code-generated narrative. Callers must always have a template version ready.
"""
from __future__ import annotations

from typing import Any, Optional

from ..contracts import LLMResult


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
    try:
        from .router import complete as _complete  # implemented by agent D
    except Exception as e:  # router not present yet
        return LLMResult(text="", data=None, source="template", route="none", ok=False, error=f"router unavailable: {e}")
    try:
        return _complete(task, payload, purpose=purpose, ws=ws, settings=settings, schema=schema, language=language, system=system, max_tokens=max_tokens)
    except Exception as e:  # never let an LLM failure break the pipeline
        return LLMResult(text="", data=None, source="template", route="none", ok=False, error=str(e))


def complete_many(jobs: list[dict[str, Any]], *, ws: Any = None, settings: Any = None, max_parallel: Optional[int] = None, deadline_s: Optional[float] = None) -> list[LLMResult]:
    """Several complete() jobs at once (a job is the kwargs dict of complete). External jobs run concurrently
    (external_llm.max_parallel), local ones one after the other. Results in job order; never raises."""
    try:
        from .router import complete_many as _many

        return _many(jobs, ws=ws, settings=settings, max_parallel=max_parallel, deadline_s=deadline_s)
    except Exception as e:
        return [LLMResult(text="", data=None, source="template", route="none", ok=False, error=str(e)) for _ in jobs]


def external_ready(task: str, ws: Any = None, settings: Any = None) -> tuple[bool, str]:
    """(usable, reason): does the task route to the external model and can a call be made right now?"""
    try:
        from .router import external_ready as _ready

        return _ready(task, ws=ws, settings=settings)
    except Exception as e:
        return False, str(e)


def usage(ws: Any, settings: Any = None) -> dict[str, Any]:
    """External-model use of a run (calls, tokens, budget left, average latency local vs external, per task)."""
    try:
        from .ledger import usage as _usage

        return _usage(ws, settings)
    except Exception as e:
        return {"error": str(e)}


def available(settings: Any = None) -> dict[str, Any]:
    """Which routes are reachable right now (for the UI status bar)."""
    try:
        from .router import available as _available

        return _available(settings)
    except Exception as e:
        return {"local": False, "external": False, "error": str(e)}


def chat(
    ws: Any,
    settings: Any = None,
    message: str = "",
    context: Optional[dict[str, Any]] = None,
    history: Optional[list[dict[str, str]]] = None,
    actor: str = "human",
    *,
    task: str = "why_chat",
    language: str = "en",
) -> dict[str, Any]:
    """Operator "why" chat (task="why_chat") and assessor chat (task="assessor_chat"). Local tool-agent when a
    local model is available, deterministic evidence-based answer otherwise. Never raises.
    Returns {"answer", "citations", "tool_trace", "source", "suggested_followups", "series", "turn_id"}."""
    try:
        from .agent import chat as _chat

        return _chat(ws, settings, message, context=context, history=history, actor=actor, task=task, language=language)
    except Exception as e:
        return {"answer": f"Chat is unavailable: {e}", "citations": [], "tool_trace": [], "source": "template", "suggested_followups": [], "series": None, "turn_id": None, "error": str(e)}


def ensure_models(settings: Any = None) -> dict[str, Any]:
    """Model availability report + exact `ollama pull` commands (README / UI setup panel)."""
    try:
        from .router import ensure_models as _ensure

        return _ensure(settings)
    except Exception as e:
        return {"local": False, "external": False, "message": f"llm layer unavailable: {e}", "pull_commands": []}
