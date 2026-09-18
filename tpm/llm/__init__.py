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


def available() -> dict[str, Any]:
    """Which routes are reachable right now (for the UI status bar)."""
    try:
        from .router import available as _available

        return _available()
    except Exception as e:
        return {"local": False, "external": False, "error": str(e)}
