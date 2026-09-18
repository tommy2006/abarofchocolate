"""Prompt templates (Jinja2, one system + one user template per task) and the JSON schema per task.

    from tpm.llm.prompts import render, schema_for
    system_text, user_text = render("diagnosis_narrative", payload, language="fi")

Every template inherits the trust rules from _trust_rules.j2: reason only over the provided derived
artifacts, cite evidence IDs, separate inferred / assumed / uncertain, give a confidence in [0, 1],
output strictly the requested JSON, answer in the requested language.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

PROMPT_DIR = Path(__file__).resolve().parent

LANGUAGE_NAMES = {"en": "English", "fi": "Finnish (suomi)", "sv": "Swedish (svenska)"}

TASKS = ["column_roles", "sensor_hypotheses", "rule_compile", "diagnosis_narrative", "critique", "why_chat", "assessor_chat", "report_narrative"]

_env = Environment(loader=FileSystemLoader(str(PROMPT_DIR)), autoescape=select_autoescape(default=False), undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)


def _json(obj: Any, indent: Optional[int] = 1) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=indent, default=str)


_env.filters["tojson_pretty"] = _json


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get((code or "en").lower(), "English")


def has_task(task: str) -> bool:
    return (PROMPT_DIR / f"{task}.user.j2").exists()


def render(task: str, payload: dict[str, Any], language: str = "en", system_override: Optional[str] = None, schema: Optional[dict[str, Any]] = None, **extra: Any) -> tuple[str, str]:
    """Return (system_text, user_text) for the task. Unknown tasks use the generic template."""
    name = task if has_task(task) else "generic"
    ctx = {
        "task": task,
        "payload": payload or {},
        "payload_json": _json(payload or {}),
        "language": (language or "en").lower(),
        "language_name": language_name(language),
        "schema_json": _json(schema if schema is not None else schema_for(task), indent=None),
        "has_schema": (schema if schema is not None else schema_for(task)) is not None,
    }
    ctx.update(extra)
    system_text = system_override if system_override else _env.get_template(f"{name}.system.j2").render(**ctx).strip()
    user_text = _env.get_template(f"{name}.user.j2").render(**ctx).strip()
    return system_text, user_text


def render_template(name: str, **ctx: Any) -> str:
    return _env.get_template(name).render(**ctx).strip()


# ----------------------------------------------------------------------------------------------
# JSON schemas per task (objects at top level; validated with providers.validate_schema)
# ----------------------------------------------------------------------------------------------

_CONF = {"type": "number", "minimum": 0, "maximum": 1}
_IDS = {"type": "array", "items": {"type": "string"}}
_STATUS = {"type": "string", "enum": ["inferred", "assumed", "uncertain"]}

STRUCTURAL_ROLES = ["continuous_measured", "actuator_like", "held_sampled", "constant", "derived_redundant", "counter", "timestamp", "categorical", "text", "identifier", "unknown"]
RULE_TYPES = ["threshold", "range", "rate_of_change", "acceleration", "duration", "cross_signal", "rolling_stats", "missing", "stale"]

SCHEMAS: dict[str, dict[str, Any]] = {
    "column_roles": {
        "type": "object",
        "properties": {
            "roles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "signal": {"type": "string"},
                        "structural_role": {"type": "string", "enum": STRUCTURAL_ROLES},
                        "confidence": _CONF,
                        "status": _STATUS,
                        "reasoning": {"type": "string"},
                        "evidence_ids": _IDS,
                    },
                    "required": ["signal", "structural_role", "confidence", "status", "reasoning", "evidence_ids"],
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["roles"],
    },
    "sensor_hypotheses": {
        "type": "object",
        "properties": {
            "hypotheses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "signal": {"type": "string"},
                        "instrument_hypothesis": {"type": "string"},
                        "instrument_confidence": _CONF,
                        "unit_operation_hypothesis": {"type": "string"},
                        "unit_operation_confidence": _CONF,
                        "units_hypothesis": {"type": "string"},
                        "status": _STATUS,
                        "reasoning": {"type": "string"},
                        "evidence_ids": _IDS,
                        "alternatives": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["signal", "instrument_hypothesis", "instrument_confidence", "status", "reasoning", "evidence_ids"],
                },
            },
            "process_hypothesis": {"type": "string"},
            "process_confidence": _CONF,
            "uncertainty": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["hypotheses", "process_hypothesis", "process_confidence", "uncertainty"],
    },
    "rule_compile": {
        "type": "object",
        "properties": {
            "rule_type": {"type": "string", "enum": RULE_TYPES},
            "signals": _IDS,
            "params": {
                "type": "object",
                "properties": {
                    "min": {"type": ["number", "null"]},
                    "max": {"type": ["number", "null"]},
                    "max_rate": {"type": ["number", "null"]},
                    "max_acceleration": {"type": ["number", "null"]},
                    "window": {"type": ["integer", "null"]},
                    "min_duration": {"type": ["integer", "null"]},
                    "statistic": {"type": ["string", "null"]},
                    "threshold": {"type": ["number", "null"]},
                    "operator": {"type": ["string", "null"]},
                    "other_signal": {"type": ["string", "null"]},
                    "max_stale_samples": {"type": ["integer", "null"]},
                },
            },
            "severity": _CONF,
            "explanation": {"type": "string"},
            "confidence": _CONF,
            "status": _STATUS,
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "evidence_ids": _IDS,
            "unresolved": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["rule_type", "signals", "params", "severity", "explanation", "confidence", "status", "assumptions", "evidence_ids"],
    },
    "diagnosis_narrative": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "steps": {"type": "array", "items": {"type": "string"}},
            "uncertainty": {"type": "array", "items": {"type": "string"}},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "cause_class": {"type": "string", "enum": ["process", "sensor", "data", "mixed", "unknown"]},
            "confidence": _CONF,
            "evidence_ids": _IDS,
        },
        "required": ["summary", "steps", "uncertainty", "confidence", "evidence_ids"],
    },
    "critique": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["supported", "weakened", "rejected"]},
            "objections": {
                "type": "array",
                "items": {"type": "object", "properties": {"text": {"type": "string"}, "evidence_ids": _IDS, "severity": _CONF}, "required": ["text", "evidence_ids"]},
            },
            "alternative_explanations": {"type": "array", "items": {"type": "string"}},
            "adjusted_confidence": _CONF,
            "reasoning": {"type": "string"},
        },
        "required": ["verdict", "objections", "adjusted_confidence", "reasoning"],
    },
    "report_narrative": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "executive_summary": {"type": "string"},
            "sections": {
                "type": "array",
                "items": {"type": "object", "properties": {"heading": {"type": "string"}, "body": {"type": "string"}, "evidence_ids": _IDS}, "required": ["heading", "body", "evidence_ids"]},
            },
            "uncertainty": {"type": "array", "items": {"type": "string"}},
            "confidence": _CONF,
        },
        "required": ["title", "executive_summary", "sections", "uncertainty", "confidence"],
    },
    "why_chat": {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "citations": _IDS,
            "confidence": _CONF,
            "suggested_followups": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["answer", "citations", "confidence", "suggested_followups"],
    },
}
SCHEMAS["assessor_chat"] = SCHEMAS["why_chat"]

# The local tool-agent step (model-agnostic JSON-action loop, no native tool calling needed)
AGENT_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "action": {"type": "string"},
        "args": {"type": "object"},
        "answer": {"type": "string"},
        "citations": _IDS,
        "confidence": _CONF,
        "suggested_followups": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["thought", "action"],
}


def schema_for(task: str) -> Optional[dict[str, Any]]:
    return SCHEMAS.get(task)
