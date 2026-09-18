"""Egress guard: decides whether a payload may be sent to an external (network) model.

Every external payload passes `check(payload, settings, strict)` first. The guard only knows about
derived artifacts (signal catalog, relations summary, checks, trust, flags, diagnoses, rule text,
evidence statements, patterns, assessor results, chat question/history, schema summary, report sections).
It blocks anything that looks like raw data: long numeric series, row-like records, too many numbers,
aggregates over too few samples, free-text / categorical record values, oversized payloads.
In strict mode original column names are replaced by aliases (S01..) and human free text is dropped.

The guard never sends anything itself; it returns a GuardResult the router acts on.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..config import GuardConfig, Settings

# top-level payload key -> artifact type. Anything else is unknown (blocked in strict, dropped otherwise).
ARTIFACT_KEYS: dict[str, str] = {
    "signals": "signal_catalog", "catalog": "signal_catalog", "signal_catalog": "signal_catalog", "signal": "signal_catalog",
    "relations": "relations", "relations_summary": "relations",
    "checks": "checks", "check": "checks",
    "trust": "trust", "trust_verdict": "trust",
    "flags": "flags", "flag": "flags",
    "diagnosis": "diagnosis", "diagnoses": "diagnosis",
    "critique": "critique",
    "rule_text": "rule_text", "rules": "rules", "rule": "rules",
    "evidence": "evidence", "evidence_statements": "evidence",
    "inferences": "inferences", "inference": "inferences",
    "patterns": "patterns", "pattern": "patterns",
    "propagation": "propagation", "cascade": "propagation",
    "assessor": "assessor", "assessment": "assessor",
    "question": "chat", "message": "chat", "history": "chat", "answer": "chat",
    "schema_summary": "schema_summary", "schema": "schema_summary", "dataset_summary": "schema_summary",
    "report": "report_sections", "sections": "report_sections", "report_sections": "report_sections",
    "domain": "domain", "domain_hint": "domain_hint", "domain_likelihood": "domain",
    "purpose": "meta", "language": "meta", "task": "meta", "meta": "meta", "instructions": "meta",
    "context": "context", "batch": "batch", "batches": "batch",
    "baseline": "baseline", "detect_meta": "detect_meta", "evaluation": "evaluation",
}

# keys whose string values are code-generated text (statements, ids, roles) and may leave in any mode
TEXT_KEYS = {
    "id", "statement", "claim", "reasoning", "kind", "structural_role", "instrument_hypothesis",
    "unit_operation_hypothesis", "units_hypothesis", "cluster_id", "dtype", "status", "category", "check_type",
    "detector", "direction", "explanation", "summary", "steps", "uncertainty", "assumptions", "alternatives",
    "description", "name", "text", "rule_text", "question", "message", "role", "content", "verdict",
    "objections", "fault_type", "cause_class", "likely_cause_class", "computed_by", "source", "stage", "subject",
    "method", "rationale", "format", "grouping_method", "purpose", "language", "domain_hint", "author",
    "from_signal", "to_signal", "signal", "signals", "created_at", "updated_at", "ts", "batch_id", "group_id",
    "pattern_id", "excluded_reason", "narrative_source", "compile_source", "compile_explanation", "title",
    "section", "body", "heading", "label", "reasons", "human_status", "rule_id", "check_id", "flag_ids",
    "evidence_ids", "inference_ids", "check_ids", "groups_affected", "untrusted_signals", "task", "route",
    "dataset_id", "time_column", "order_column", "group_column", "profile", "recommendation", "action",
    "verdicts", "findings", "notes", "note", "caveats", "warning", "warnings", "type", "unit", "units",
    "affected_signals", "top_signals", "related", "human_role_override", "instrument", "unit_operation",
    "hypothesis", "evidence_statement", "trend", "shape", "period", "severity_label", "state", "outcome",
    "columns", "signal_columns", "label_columns", "meta_columns", "group_columns", "source_column",
    "keys", "values_kind", "condition", "operator", "aggregation", "window", "metric", "score_name",
}
TEXT_KEY_SUFFIXES = ("_text", "_statement", "_summary", "_hint", "_note", "_explanation", "_description", "_name",
                     "_id", "_ids", "_role", "_hypothesis", "_class", "_type", "_status", "_kind", "_source",
                     "_label", "_reason", "_signal", "_signals", "_method", "_at", "_title", "_section")
# free-text written by humans: dropped in strict mode (may contain names / PII)
HUMAN_TEXT_KEYS = {"human_note", "note_by_human", "operator_note"}
# keys that carry original column names (dropped/aliased in strict mode)
NAME_KEYS = {"source_column", "columns", "signal_columns", "label_columns", "meta_columns", "group_columns", "time_column", "order_column", "signal_alias"}

ID_RE = re.compile(r"^(EV|INF|CHK|FLAG|PATTERN|DIAG|RULE|EGR|LOG|B|G|S|RUN|CHAT)[-_]?[0-9A-Za-z]{1,12}$")
ALIAS_RE = re.compile(r"^S\d{2,5}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?([+-]\d{2}:?\d{2}|Z)?$")
NUM_STR_RE = re.compile(r"^[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?$")
GROUP_RE = re.compile(r"^(group|batch|run|fold|cluster|regime)[:_-]?[0-9A-Za-z]{1,12}$")

KNOWN_VOCAB = {
    "continuous_measured", "actuator_like", "held_sampled", "constant", "derived_redundant", "counter", "timestamp",
    "categorical", "text", "identifier", "unknown", "process", "sensor", "data", "mixed", "anomaly", "drift",
    "changepoint", "dq", "rule", "cascade", "pass", "warn", "fail", "completeness", "validity", "consistency",
    "timeliness", "inferred", "assumed", "uncertain", "supported", "weakened", "rejected", "up", "down", "noisy",
    "stuck", "shifted", "abrupt", "gradual", "true", "false", "none", "null", "yes", "no", "low", "medium", "high",
    "flow", "pressure", "temperature", "level", "composition", "valve", "power", "speed", "reactor", "separator",
    "stripper", "compressor", "feed", "utility", "int", "float", "float32", "float64", "int64", "string", "bool",
    "object", "datetime", "local", "external", "template", "code", "human", "en", "fi", "sv",
}


@dataclass
class GuardResult:
    allowed: bool
    reason: str
    sanitized_payload: dict[str, Any]
    artifact_types: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    strict: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason, "artifact_types": self.artifact_types, "stats": self.stats, "notes": self.notes, "strict": self.strict}


class _Block(Exception):
    pass


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_text_key(key: Optional[str]) -> bool:
    if key is None:
        return False
    k = str(key)
    return k in TEXT_KEYS or k.endswith(TEXT_KEY_SUFFIXES)


def _is_known_string(s: str) -> bool:
    s2 = s.strip()
    if not s2:
        return True
    if len(s2) > 64:
        return False
    if ALIAS_RE.match(s2) or ID_RE.match(s2) or ISO_RE.match(s2) or NUM_STR_RE.match(s2) or GROUP_RE.match(s2):
        return True
    low = s2.lower()
    if low in KNOWN_VOCAB:
        return True
    # short role-like tokens (snake_case identifiers without spaces) are structural vocabulary
    if re.match(r"^[a-z][a-z0-9_]{0,31}$", low) and low.count(" ") == 0 and not low.isdigit():
        return True
    return False


class _Scanner:
    def __init__(self, cfg: GuardConfig, strict: bool):
        self.cfg = cfg
        self.strict = strict
        self.numeric = 0
        self.max_series = 0
        self.n_strings = 0
        self.dropped = 0  # weak aggregate items dropped (non-strict mode)
        self.dropped_fields = 0  # human free-text fields dropped (strict mode)
        self.notes: list[str] = []

    # returns the (possibly modified) node
    def walk(self, node: Any, key: Optional[str], path: str, in_values: bool = False) -> Any:
        if isinstance(node, dict):
            return self._walk_dict(node, path, in_values)
        if isinstance(node, list):
            return self._walk_list(node, key, path, in_values)
        if _is_num(node):
            self.numeric += 1
            return node
        if isinstance(node, str):
            self.n_strings += 1
            if self.cfg.forbid_categorical_values and (in_values or not _is_text_key(key)) and not _is_known_string(node):
                raise _Block(f"free-text/categorical value under key '{key or '?'}' at {path}")
            return node
        return node

    def _walk_dict(self, node: dict[str, Any], path: str, in_values: bool) -> dict[str, Any]:
        # aggregate sample-size rule
        n = node.get("n_samples") if "n_samples" in node else None
        if _is_num(n) and n < self.cfg.min_aggregate_n:
            raise _Block(f"aggregate at {path} computed over n_samples={n} < min_aggregate_n={self.cfg.min_aggregate_n}")
        out: dict[str, Any] = {}
        for k, v in node.items():
            if self.strict and k in HUMAN_TEXT_KEYS:
                if v not in (None, ""):
                    self.dropped_fields += 1
                    self.notes.append(f"dropped human free text '{k}' at {path}")
                continue
            sub_in_values = in_values or k in ("values", "fingerprint", "signature", "params", "stats", "options_used")
            out[k] = self.walk(v, k, f"{path}.{k}", sub_in_values)
        return out

    def _walk_list(self, node: list[Any], key: Optional[str], path: str, in_values: bool) -> list[Any]:
        if node and all(_is_num(x) for x in node):
            if len(node) > self.cfg.max_series_points:
                raise _Block(f"numeric series of {len(node)} points under '{key or '?'}' at {path} exceeds max_series_points={self.cfg.max_series_points}")
            self.max_series = max(self.max_series, len(node))
            self.numeric += len(node)
            return list(node)
        if self.cfg.forbid_row_like_structures and _looks_row_like(node):
            raise _Block(f"row-like structure ({len(node)} records) under '{key or '?'}' at {path}")
        out: list[Any] = []
        for i, item in enumerate(node):
            try:
                out.append(self.walk(item, key, f"{path}[{i}]", in_values))
            except _Block as e:
                if not self.strict and isinstance(item, dict) and str(e).startswith("aggregate at"):
                    self.dropped += 1
                    self.notes.append(f"dropped item {path}[{i}]: {e}")
                    continue
                raise
        return out


def _looks_row_like(items: list[Any]) -> bool:
    """A list of >=2 dicts sharing >=5 numeric keys that make up most of their keys, or a numeric matrix."""
    if len(items) < 2:
        return False
    if all(isinstance(x, list) and len(x) >= 5 and all(_is_num(v) for v in x) for x in items):
        return True
    if not all(isinstance(x, dict) for x in items):
        return False
    shared_numeric: Optional[set[str]] = None
    shared_keys: Optional[set[str]] = None
    for d in items:
        nk = {k for k, v in d.items() if _is_num(v)}
        ks = set(d.keys())
        shared_numeric = nk if shared_numeric is None else shared_numeric & nk
        shared_keys = ks if shared_keys is None else shared_keys & ks
    if not shared_numeric or not shared_keys:
        return False
    if len(shared_numeric) < 5:
        return False
    has_identity = any(_is_text_key(k) and any(isinstance(d.get(k), str) for d in items) for k in shared_keys)
    ratio = len(shared_numeric) / max(1, len(shared_keys))
    return (ratio >= 0.6) or (not has_identity and ratio >= 0.4)


# ----------------------------------------------------------------------------------------------
# aliasing (strict mode)
# ----------------------------------------------------------------------------------------------


def _collect_alias_map(payload: dict[str, Any]) -> dict[str, str]:
    """original column name -> alias, from schema.signal_alias and descriptors' source_column."""
    amap: dict[str, str] = {}
    for key in ("schema_summary", "schema", "dataset_summary"):
        sch = payload.get(key)
        if isinstance(sch, dict) and isinstance(sch.get("signal_alias"), dict):
            for orig, alias in sch["signal_alias"].items():
                if isinstance(orig, str) and isinstance(alias, str) and orig != alias:
                    amap[orig] = alias
    for key in ("signals", "catalog", "signal_catalog", "signal"):
        items = payload.get(key)
        if isinstance(items, dict):
            items = [items]
        if isinstance(items, list):
            for d in items:
                if isinstance(d, dict) and isinstance(d.get("source_column"), str) and isinstance(d.get("id"), str):
                    if d["source_column"] != d["id"]:
                        amap[d["source_column"]] = d["id"]
    return amap


def _alias_strings(node: Any, amap: dict[str, str], counter: list[int]) -> Any:
    if not amap:
        return node
    if isinstance(node, dict):
        return {k: _alias_strings(v, amap, counter) for k, v in node.items()}
    if isinstance(node, list):
        return [_alias_strings(v, amap, counter) for v in node]
    if isinstance(node, str):
        s = node
        for orig in sorted(amap, key=len, reverse=True):
            if orig and orig in s:
                s2 = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(orig) + r"(?![A-Za-z0-9_])", amap[orig], s)
                if s2 != s:
                    counter[0] += 1
                    s = s2
        return s
    return node


def _strip_names(node: Any, amap: dict[str, str], notes: list[str], path: str = "$") -> Any:
    """Remove fields that carry original column names; keep aliases only."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "signal_alias" and isinstance(v, dict):
                out["signal_aliases"] = sorted(set(str(a) for a in v.values()))
                notes.append(f"replaced signal_alias map at {path} with alias list")
                continue
            if k in NAME_KEYS:
                if isinstance(v, list):
                    out[k] = [amap.get(x, x) if isinstance(x, str) else x for x in v]
                    out[k] = [x for x in out[k] if not isinstance(x, str) or x in amap.values() or ALIAS_RE.match(x) or x.startswith("__")]
                elif isinstance(v, str):
                    if k == "source_column":
                        continue  # never leaks the original header in strict mode; the alias is in "id"
                    if v in amap:
                        out[k] = amap[v]
                    elif ALIAS_RE.match(v) or v.startswith("__"):
                        out[k] = v
                    else:
                        continue  # drop original name
                else:
                    out[k] = v
                continue
            if k == "kind" and v == "name_hint":
                out[k] = v
                continue
            out[k] = _strip_names(v, amap, notes, f"{path}.{k}")
        return out
    if isinstance(node, list):
        cleaned = []
        for i, v in enumerate(node):
            if isinstance(v, dict) and v.get("kind") == "name_hint":
                notes.append(f"dropped name_hint evidence at {path}[{i}]")
                continue
            cleaned.append(_strip_names(v, amap, notes, f"{path}[{i}]"))
        return cleaned
    return node


# ----------------------------------------------------------------------------------------------
# public API
# ----------------------------------------------------------------------------------------------


def payload_bytes(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def _json_default(o: Any) -> Any:
    if hasattr(o, "model_dump"):
        return o.model_dump()
    if hasattr(o, "item"):
        try:
            return o.item()
        except Exception:
            pass
    if hasattr(o, "tolist"):
        return o.tolist()
    if isinstance(o, (set, frozenset)):
        return sorted(str(x) for x in o)
    return str(o)


def _clean(node: Any) -> Any:
    if isinstance(node, dict):
        return {str(k): _clean(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_clean(v) for v in node]
    if isinstance(node, float) and (node != node or node in (float("inf"), float("-inf"))):
        return None
    return node


def to_plain(payload: Any) -> Any:
    """Plain JSON (dicts/lists/str/num/bool/None): pydantic models, numpy scalars/arrays and NaN are normalized so
    the guard walks EVERYTHING it will send. Never raises."""
    try:
        return _clean(json.loads(json.dumps(payload, default=_json_default, ensure_ascii=False, allow_nan=True)))
    except Exception:
        try:
            return _clean(json.loads(json.dumps(payload, default=str, ensure_ascii=False)))
        except Exception:
            return {"unserializable": str(payload)[:1000]}


def check(payload: dict[str, Any], settings: Settings, strict: Optional[bool] = None) -> GuardResult:
    """Decide whether `payload` may leave the operator environment. Never raises."""
    cfg = settings.guard
    if strict is None:
        strict = settings.active_profile.guard_strict
    notes: list[str] = []
    stats: dict[str, Any] = {}
    if not isinstance(payload, dict):
        return GuardResult(False, "payload must be a JSON object keyed by artifact type", {}, [], {"payload_bytes": payload_bytes(payload)}, notes, bool(strict))
    work = to_plain(payload)
    if not isinstance(work, dict):
        return GuardResult(False, "payload could not be normalized to a JSON object", {}, [], {}, notes, bool(strict))

    # 1. whitelist by top-level key
    artifact_types: list[str] = []
    sanitized: dict[str, Any] = {}
    for k, v in work.items():
        atype = ARTIFACT_KEYS.get(str(k))
        if atype is None:
            if strict:
                return GuardResult(False, f"unknown artifact type '{k}' (not in the egress whitelist)", {}, [], {"payload_bytes": payload_bytes(payload)}, notes, True)
            notes.append(f"dropped unknown top-level key '{k}'")
            continue
        if atype not in artifact_types:
            artifact_types.append(atype)
        sanitized[k] = v
    if not sanitized:
        return GuardResult(False, "payload contains no whitelisted artifacts", {}, [], {"payload_bytes": payload_bytes(payload)}, notes, bool(strict))

    # 2. aliasing of original column names
    amap = _collect_alias_map(sanitized)
    aliased = 0
    if strict and cfg.alias_column_names_in_strict or not cfg.allow_column_names:
        counter = [0]
        sanitized = _strip_names(sanitized, amap, notes)
        sanitized = _alias_strings(sanitized, amap, counter)
        aliased = counter[0]
        if amap:
            notes.append(f"aliased {len(amap)} original column names")

    # 3. structural scanners
    scanner = _Scanner(cfg, bool(strict))
    try:
        sanitized = scanner.walk(sanitized, None, "$")
    except _Block as e:
        stats = {"payload_bytes": payload_bytes(payload), "numeric_values": scanner.numeric, "max_series_len": scanner.max_series}
        return GuardResult(False, str(e), {}, artifact_types, stats, notes + scanner.notes, bool(strict))
    notes.extend(scanner.notes)
    if scanner.numeric > cfg.max_numeric_values_per_payload:
        stats = {"payload_bytes": payload_bytes(payload), "numeric_values": scanner.numeric, "max_series_len": scanner.max_series}
        return GuardResult(False, f"{scanner.numeric} numeric values exceed max_numeric_values_per_payload={cfg.max_numeric_values_per_payload}", {}, artifact_types, stats, notes, bool(strict))

    # 4. size
    nbytes = payload_bytes(sanitized)
    stats = {
        "payload_bytes": nbytes,
        "numeric_values": scanner.numeric,
        "max_series_len": scanner.max_series,
        "n_strings": scanner.n_strings,
        "items_dropped": scanner.dropped,
        "fields_dropped": scanner.dropped_fields,
        "names_aliased": aliased,
        "artifact_types": artifact_types,
    }
    if nbytes > cfg.max_payload_bytes:
        return GuardResult(False, f"payload of {nbytes} bytes exceeds max_payload_bytes={cfg.max_payload_bytes}", {}, artifact_types, stats, notes, bool(strict))

    reason = "allowed: derived artifacts only (" + ", ".join(artifact_types) + ")"
    if scanner.dropped:
        reason += f"; {scanner.dropped} weak aggregate item(s) dropped"
    if scanner.dropped_fields:
        reason += f"; {scanner.dropped_fields} human note(s) dropped"
    return GuardResult(True, reason, sanitized, artifact_types, stats, notes, bool(strict))


def explain(settings: Settings, strict: Optional[bool] = None, language: str = "en") -> str:
    """Plain-language description for the UI: what may leave the machine under the current settings."""
    cfg = settings.guard
    prof = settings.active_profile
    if strict is None:
        strict = prof.guard_strict
    if not prof.allow_external:
        head = (f"Profile '{settings.profile}': nothing leaves this machine. All language-model work runs on the local "
                f"model ({settings.local_llm.model}) or on code templates. No network model is ever called.")
    else:
        head = (f"Profile '{settings.profile}': derived artifacts may be sent to the external model "
                f"{settings.external_llm.model} ({settings.external_llm.provider}"
                + (f", endpoint {settings.external_llm.base_url}" if settings.external_llm.base_url else "") + ") after passing the egress guard.")
    lines = [
        head,
        "",
        "What may leave (only after passing the guard):",
        "- the signal catalog: aliases, structural roles, hypotheses with confidence, aggregate fingerprints",
        "- relation summaries (correlation / lag between signals), data-quality check statements, trust verdicts",
        "- flags and diagnoses (statements, ranked signals, propagation steps), evidence statements with their IDs",
        "- rule text written by the operator, report section drafts, chat questions",
        "",
        "What never leaves:",
        f"- raw rows or any numeric series longer than {cfg.max_series_points} points",
        f"- payloads with more than {cfg.max_numeric_values_per_payload} numbers or larger than {cfg.max_payload_bytes // 1000} kB",
        f"- aggregates computed over fewer than {cfg.min_aggregate_n} samples",
        "- record-like structures (lists of records sharing numeric fields)" if cfg.forbid_row_like_structures else "- (row-like structures are allowed by config)",
        "- free-text or categorical record values (names, comments, labels)" if cfg.forbid_categorical_values else "- (categorical values are allowed by config)",
        f"- original column names (replaced by aliases S01..): {'yes, strict mode' if (strict and cfg.alias_column_names_in_strict) or not cfg.allow_column_names else 'names may leave in this non-strict profile'}",
        "- human notes on flags / diagnoses" + (" (dropped in strict mode)" if strict else " (kept in non-strict mode)"),
        "",
        "If the guard blocks a payload the task is answered by the local model instead, then by a code template; "
        "the block and the fallback are written to the egress ledger.",
    ]
    return "\n".join(lines)
