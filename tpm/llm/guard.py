"""Egress guard: decides what may be sent to an external (network) model, and in which form.

Every external payload passes `check(payload, settings, strict, ws)` first. The guard only knows about derived
artifacts (signal catalog, relations summary, checks, trust, flags, diagnoses, rule text, evidence statements,
patterns, assessor results, chat question/history, schema summary, report sections, tool results).

Guard v2 works in two steps. It first SANITISES the payload field by field (fail-closed per field, not per payload):
unknown top-level keys, free text under non-text keys, aggregates over too few samples, row-like structures and long
numeric series are dropped with a note; keys that carry single readings, data timestamps, file names or label-based
evaluation are removed at any depth; every float is rounded to a few significant digits (also inside sentences); ISO
timestamps become [time]; values of the dataset's text / label columns become [value]; original column names become
aliases (S01..). It then VERIFIES an invariant on the result, the last line of defence: anything that still looks like
raw data blocks the payload. In strict mode human free text is dropped as well.

The guard never sends anything itself; it returns a GuardResult the router acts on.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..config import GuardConfig, Settings

# top-level payload key -> artifact type. Anything else is unknown: dropped with a note.
ARTIFACT_KEYS: dict[str, str] = {
    "signals": "signal_catalog", "catalog": "signal_catalog", "signal_catalog": "signal_catalog", "signal": "signal_catalog",
    "signal_aliases": "signal_catalog",
    "relations": "relations", "relations_summary": "relations",
    "checks": "checks", "check": "checks",
    "trust": "trust", "trust_verdict": "trust",
    "flags": "flags", "flag": "flags",
    "diagnosis": "diagnosis", "diagnoses": "diagnosis",
    "critique": "critique",
    "rule_text": "rule_text", "rules": "rules", "rule": "rules", "candidates": "rules", "template_parser_error": "rules",
    "evidence": "evidence", "evidence_statements": "evidence", "evidence_ids": "evidence",
    "inferences": "inferences", "inference": "inferences",
    "patterns": "patterns", "pattern": "patterns",
    "propagation": "propagation", "cascade": "propagation",
    "assessor": "assessor", "assessment": "assessor", "action": "assessor", "action_types": "assessor", "template_answer": "assessor",
    "question": "chat", "message": "chat", "history": "chat", "answer": "chat",
    "tool_result": "tool_result", "tool_results": "tool_result",
    "schema_summary": "schema_summary", "schema": "schema_summary", "dataset_summary": "schema_summary",
    "report": "report_sections", "sections": "report_sections", "report_sections": "report_sections",
    "domain": "domain", "domain_hint": "domain_hint", "domain_likelihood": "domain",
    "purpose": "meta", "language": "meta", "task": "meta", "meta": "meta", "instructions": "meta", "instruction": "meta",
    "context": "context", "batch": "batch", "batches": "batch",
    "baseline": "baseline", "detect_meta": "detect_meta", "evaluation": "evaluation",
}
# artifact types that hold operator-written rule content: their limits are not data, so min/max/value stay and their
# numbers are not rounded (a rule "S01 below 2715.5" must reach the compiler unchanged)
RULE_ARTIFACT_TYPES = {"rules", "rule_text"}
RULE_LIMIT_KEYS = {"min", "max", "value", "values"}
RULE_TYPES = {"threshold", "range", "rate_of_change", "acceleration", "duration", "cross_signal", "rolling_stat", "rolling_stats", "missing", "stale", "stuck", "drift"}

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
    "section", "body", "heading", "reasons", "human_status", "rule_id", "check_id", "flag_ids",
    "evidence_ids", "inference_ids", "check_ids", "groups_affected", "untrusted_signals", "task", "route",
    "dataset_id", "time_column", "order_column", "group_column", "profile", "recommendation", "action",
    "verdicts", "findings", "notes", "note", "caveats", "warning", "warnings", "type", "unit", "units",
    "affected_signals", "top_signals", "related", "human_role_override", "instrument", "unit_operation",
    "hypothesis", "evidence_statement", "trend", "shape", "period", "severity_label", "state", "outcome",
    "columns", "signal_columns", "label_columns", "meta_columns", "group_columns", "source_column",
    "keys", "values_kind", "condition", "operator", "aggregation", "window", "metric", "score_name",
    # code-written task instructions and template / parser texts (dry run 2026-09-19: these were false blocks)
    "instruction", "instructions", "heuristic_instrument", "detail", "template_parser_error", "template_answer",
    "candidates", "expected_effect", "error", "withheld", "reason", "tool", "overview", "answer", "thought",
}
TEXT_KEY_SUFFIXES = ("_text", "_statement", "_summary", "_hint", "_note", "_explanation", "_description", "_name",
                     "_id", "_ids", "_role", "_hypothesis", "_class", "_type", "_status", "_kind", "_source",
                     "_label", "_reason", "_signal", "_signals", "_method", "_at", "_title", "_section")
# free-text written by humans: dropped in strict mode (may contain names / PII)
HUMAN_TEXT_KEYS = {"human_note", "note_by_human", "operator_note"}
# keys that carry original column names (dropped / aliased whenever names are aliased)
NAME_KEYS = {"source_column", "columns", "signal_columns", "label_columns", "meta_columns", "group_columns", "time_column", "order_column", "signal_alias"}
# strings under these keys keep their numbers as typed (operator-written rule text)
NUMBER_EXEMPT_KEYS = {"rule_text"}
# integral floats under these keys are row locators / counts, not readings: they become ints instead of being rounded
_LOCATOR_KEY_RE = re.compile(r"^(row|row_start|row_end|start_row|end_row|onset|onset_row|index|idx|lag|window|count|n|n_[a-z0-9_]+|[a-z0-9_]+_count|column_index|per_samples|min_samples|max_run|hold_period)$")
# header words that say nothing about a dataset: a non-signal column with such a name is not rewritten inside sentences
# (it is still removed wherever a payload field names columns)
GENERIC_COLUMN_WORDS = {
    "time", "timestamp", "datetime", "date", "id", "index", "idx", "row", "rows", "sample", "samples", "run", "runs",
    "batch", "group", "label", "labels", "class", "target", "value", "values", "step", "cycle", "seq", "sequence",
    "status", "state", "mode", "type", "name", "unit", "units", "fault", "anomaly", "normal", "split", "fold", "set",
}
# category / label values that are everyday words of this domain: redacting them would only damage the code-written
# sentences around them ("normal operation"), and they say nothing about a particular dataset
GENERIC_VALUE_WORDS = {
    "normal", "abnormal", "fault", "faulty", "anomaly", "anomalous", "attack", "good", "bad", "open", "closed", "start",
    "stop", "running", "stopped", "idle", "active", "inactive", "auto", "manual", "train", "test", "valid", "invalid",
}

REDACT_TIME = "[time]"
REDACT_VALUE = "[value]"
REDACT_COLUMN = "[column]"
REDACT_FILE = "[file]"
_REDACTION_TOKENS = {REDACT_TIME, REDACT_VALUE, REDACT_COLUMN, REDACT_FILE}

VOCAB_MAX_VALUES = 5000
VOCAB_TIME_BOX_S = 5.0
VOCAB_MIN_LEN = 3
VOCAB_FILE = "egress_vocab.json"

ID_RE = re.compile(r"^(EV|INF|CHK|FLAG|PATTERN|DIAG|RULE|EGR|LOG|B|G|S|RUN|CHAT)[-_]?[0-9A-Za-z]{1,12}$")
ALIAS_RE = re.compile(r"^S\d{2,5}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?([+-]\d{2}:?\d{2}|Z)?$")
NUM_STR_RE = re.compile(r"^[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?$")
GROUP_RE = re.compile(r"^(group|batch|run|fold|cluster|regime)[:_-]?[0-9A-Za-z]{1,12}$")
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# tokens inside sentences
_ISO_TOKEN_RE = re.compile(r"(?<![\w.])\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?)?(?:Z|[+-]\d{2}:?\d{2})?(?![\w])")
_DATE_TOKEN_RE = re.compile(r"(?<![\w.])(?:\d{1,2}[./]\d{1,2}[./]\d{4}|\d{4}/\d{1,2}/\d{1,2})(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?)?(?![\w])")
_CLOCK_TOKEN_RE = re.compile(r"(?<![\w.:])\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?(?![\w:])")
# epoch time stamps written as plain integers (seconds: 10 digits, milliseconds: 13); row numbers never get that long
_EPOCH_TOKEN_RE = re.compile(r"(?<![\w.-])\d{10}(?:\d{3})?(?![\w.])")
# any data-file name inside a sentence, also when it is not this run's own file
_DATAFILE_TOKEN_RE = re.compile(r"(?<![\w])[\w.\\/:~-]*\.(?:csv|tsv|dat|txt|xlsx?|parquet|jsonl?)(?![\w])", re.IGNORECASE)
# a decimal number that is not part of an id (EV-000123, S05), a version (1.2.3) or a longer token
_DECIMAL_TOKEN_RE = re.compile(r"(?<![\w.])(?:\d+\.\d+|\.\d+)(?:[eE][-+]?\d+)?(?!\d|\.\d)")
# word boundaries where "_" separates words too: a column name inside press_r__roll_mean is still found
_LB = r"(?<![^\W_])"
_LA = r"(?![^\W_])"

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
    alias_map: dict[str, str] = field(default_factory=dict)  # original column name -> alias (never sent; for local post-processing)
    sanitizer: dict[str, Any] = field(default_factory=dict)  # counts of what the sanitiser changed (goes to the ledger)

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason, "artifact_types": self.artifact_types, "stats": self.stats, "notes": self.notes, "strict": self.strict, "sanitizer": self.sanitizer}


class _Drop(Exception):
    """Raised by the sanitiser for one field / item; the enclosing container drops it and adds a note."""


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
    if s2 in _REDACTION_TOKENS:
        return True
    if ALIAS_RE.match(s2) or ID_RE.match(s2) or ISO_RE.match(s2) or NUM_STR_RE.match(s2) or GROUP_RE.match(s2):
        return True
    low = s2.lower()
    if low in KNOWN_VOCAB:
        return True
    # short role-like tokens (snake_case identifiers without spaces) are structural vocabulary
    if re.match(r"^[a-z][a-z0-9_]{0,31}$", low) and low.count(" ") == 0 and not low.isdigit():
        return True
    return False


def _is_known_key(k: str) -> bool:
    return bool(_KEY_RE.match(k)) or _is_known_string(k)


def _is_rule_def(node: dict[str, Any]) -> bool:
    """A compiled operating rule ({"type": "range", "signal": "S03", "min": 100, ...}): its limits are operator-written."""
    t = node.get("rule_type") if isinstance(node.get("rule_type"), str) else node.get("type")
    return isinstance(t, str) and t in RULE_TYPES and any(k in node for k in ("signal", "signals", "if", "then", "params", "condition"))


# ----------------------------------------------------------------------------------------------
# number rounding
# ----------------------------------------------------------------------------------------------


def round_sig(x: float, digits: int) -> float:
    """x rounded to `digits` significant digits (NaN / inf unchanged)."""
    if x == 0 or not math.isfinite(x):
        return x
    return float(f"{x:.{max(1, int(digits))}g}")


def _token_sig_digits(tok: str) -> int:
    mant = re.split(r"[eE]", tok.lstrip("+-"))[0]
    return len(mant.replace(".", "").lstrip("0"))


def _format_sig(x: float, digits: int) -> str:
    r = round_sig(x, digits)
    if r == 0 or not math.isfinite(r):
        return "0" if r == 0 else str(r)
    mag = math.floor(math.log10(abs(r)))
    if mag >= 15 or mag < -6:
        return f"{r:.{max(1, digits)}g}"
    return f"{r:.{max(0, digits - 1 - mag)}f}"


# ----------------------------------------------------------------------------------------------
# what the guard knows about the dataset (names, file name, text values): all of it stays local
# ----------------------------------------------------------------------------------------------

_WS_LOCK = threading.RLock()
_WS_NAMES: dict[str, tuple[Any, dict[str, str], list[str]]] = {}  # ws dir -> (stamp, alias map, file tokens)
_WS_VOCAB: dict[str, tuple[Any, dict[str, Any]]] = {}  # ws dir -> (stamp, vocabulary record)


def _stamp(p: Path) -> Any:
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _file_tokens(*sources: Any) -> list[str]:
    out: set[str] = set()
    for src in sources:
        if not isinstance(src, str) or not src.strip() or "://" in src:
            continue
        out.add(src.strip())
        p = Path(src.strip())
        if len(p.name) >= VOCAB_MIN_LEN:
            out.add(p.name)
        if p.suffix and len(p.stem) >= 4 and not NUM_STR_RE.match(p.stem):
            out.add(p.stem)
    return sorted(out, key=len, reverse=True)


def _names_from_ws(ws: Any) -> tuple[dict[str, str], list[str]]:
    """(original column name -> alias or [column], source file tokens) from schema.json / signals.json / meta.json."""
    if ws is None:
        return {}, []
    key = str(getattr(ws, "dir", ""))
    try:
        stamp = (_stamp(ws.path("schema")), _stamp(ws.path("signals")), _stamp(ws.path("meta")))
    except Exception:
        return {}, []
    with _WS_LOCK:
        hit = _WS_NAMES.get(key)
        if hit and hit[0] == stamp:
            return dict(hit[1]), list(hit[2])
    amap: dict[str, str] = {}
    sources: list[Any] = []
    try:
        sch = ws.read_json("schema", None) or {}
        sources.append(sch.get("source_path"))
        for orig, alias in (sch.get("signal_alias") or {}).items():
            if isinstance(orig, str) and isinstance(alias, str) and orig != alias:
                amap[orig] = alias
        if sch.get("had_header", True):
            for c in sch.get("columns") or []:
                if isinstance(c, str) and c not in amap and not c.startswith("__") and c.lower() not in GENERIC_COLUMN_WORDS:
                    amap[c] = REDACT_COLUMN
        for d in ws.read_json("signals", []) or []:
            if isinstance(d, dict) and isinstance(d.get("source_column"), str) and isinstance(d.get("id"), str) and d["source_column"] != d["id"]:
                amap[d["source_column"]] = d["id"]
        sources.append((ws.read_json("meta", None) or {}).get("source_path"))
        sources.append((ws.read_json("status", None) or {}).get("source_path"))
    except Exception:
        pass
    tokens = _file_tokens(*sources)
    with _WS_LOCK:
        _WS_NAMES[key] = (stamp, dict(amap), list(tokens))
    return amap, tokens


def _usable_vocab_value(v: Any) -> bool:
    if not isinstance(v, str):
        return False
    s = v.strip()
    return len(s) >= VOCAB_MIN_LEN and not NUM_STR_RE.match(s) and not ISO_RE.match(s) and s.lower() not in KNOWN_VOCAB and s.lower() not in GENERIC_VALUE_WORDS


def _scan_vocabulary(ws: Any) -> dict[str, Any]:
    """SELECT DISTINCT over the text / categorical columns of dataset.parquet, capped and time-boxed. Columns the app
    materialised itself (__group__, __row__) are skipped: their ids are generated, or built from key columns that are
    scanned anyway. Columns with few distinct values (labels, categories) go in first, so that an id-like column with
    thousands of values cannot use up the cap."""
    import duckdb

    from ..memory import duckdb_memory_limit

    path = ws.path("dataset").as_posix().replace("'", "''")
    values: list[str] = []
    seen: set[str] = set()
    columns: list[str] = []
    per_column: list[tuple[str, list[str]]] = []
    complete, capped = True, False
    con = duckdb.connect(database=":memory:")
    timer = threading.Timer(VOCAB_TIME_BOX_S, con.interrupt)
    timer.daemon = True
    t0 = time.time()
    try:
        con.execute(f"SET memory_limit='{duckdb_memory_limit()}'")
        con.execute("SET threads=2")
        timer.start()
        described = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()
        text_cols = [str(r[0]) for r in described if str(r[1]).upper().startswith(("VARCHAR", "ENUM")) and not str(r[0]).startswith("__")]
        for col in text_cols:
            quoted = '"' + col.replace('"', '""') + '"'
            rows = con.execute(f"SELECT DISTINCT CAST({quoted} AS VARCHAR) FROM read_parquet('{path}') WHERE {quoted} IS NOT NULL LIMIT {VOCAB_MAX_VALUES + 1}").fetchall()
            columns.append(col)
            if len(rows) > VOCAB_MAX_VALUES:
                capped = True
            per_column.append((col, [v for (v,) in rows]))
    except Exception:
        complete = False  # interrupted by the time box, or the file could not be read: keep what was collected
    finally:
        timer.cancel()
        try:
            con.close()
        except Exception:
            pass
    for _, col_values in sorted(per_column, key=lambda cv: len(cv[1])):
        for v in col_values:
            if _usable_vocab_value(v) and v.strip() not in seen:
                if len(values) >= VOCAB_MAX_VALUES:
                    capped = True
                    break
                seen.add(v.strip())
                values.append(v.strip())
    return {"values": values, "columns": columns, "complete": complete, "capped": capped, "seconds": round(time.time() - t0, 2)}


def vocabulary_record(ws: Any) -> dict[str, Any]:
    """{"values", "columns", "complete", "capped"} for the run, cached in workspace/<run>/egress_vocab.json and in memory
    for as long as dataset.parquet does not change. Never raises."""
    empty = {"values": [], "columns": [], "complete": True, "capped": False}
    if ws is None:
        return empty
    try:
        if not ws.exists("dataset"):
            return empty
        key = str(ws.dir)
        stamp = _stamp(ws.path("dataset"))
        with _WS_LOCK:
            hit = _WS_VOCAB.get(key)
            if hit and hit[0] == stamp:
                return hit[1]
            rec = ws.read_json(VOCAB_FILE, None)
            if not (isinstance(rec, dict) and rec.get("dataset_stamp") == list(stamp or []) and isinstance(rec.get("values"), list)):
                rec = _scan_vocabulary(ws)
                rec["dataset_stamp"] = list(stamp or [])
                rec["n_values"] = len(rec["values"])
                try:
                    ws.write_json(VOCAB_FILE, rec)
                except Exception:
                    pass
            _WS_VOCAB[key] = (stamp, rec)
            return rec
    except Exception:
        return {"values": [], "columns": [], "complete": False, "capped": False}


def data_vocabulary(ws: Any) -> list[str]:
    """Distinct values of the dataset's text / categorical / label columns (at most 5000, scan time-boxed to 5 s). Values
    shorter than 3 characters, purely numeric ones, the code's own structural words and everyday words such as
    "normal" are left out. No ws -> []."""
    return [v for v in vocabulary_record(ws).get("values", []) if _usable_vocab_value(v)]


# ----------------------------------------------------------------------------------------------
# string redaction (names, file name, timestamps, data values, long numbers)
# ----------------------------------------------------------------------------------------------


class _Redactor:
    """Rewrites one string at a time. All state is local knowledge about the dataset; nothing here is ever sent."""

    def __init__(self, cfg: GuardConfig, amap: Optional[dict[str, str]] = None, vocab: Optional[list[str]] = None, file_tokens: Optional[list[str]] = None):
        self.digits = max(1, int(cfg.external_sig_digits))
        self.counts: dict[str, int] = {"strings_redacted": 0, "names_aliased": 0, "times_redacted": 0, "values_redacted": 0, "files_redacted": 0, "numbers_in_strings_rounded": 0}
        names = {k: v for k, v in (amap or {}).items() if isinstance(k, str) and k and isinstance(v, str) and k != v and not k.startswith("__") and not NUM_STR_RE.match(k)}
        self._names_ci = {k.lower(): v for k, v in names.items() if len(k) >= 3}
        self._names_cs = {k: v for k, v in names.items() if len(k) < 3}
        self._names_ci_re = self._alternation(self._names_ci, re.IGNORECASE)
        self._names_cs_re = self._alternation(self._names_cs, 0)
        vocab = [v.strip() for v in (vocab or []) if isinstance(v, str) and len(v.strip()) >= VOCAB_MIN_LEN]
        self._vocab_words = {v.lower() for v in vocab if re.fullmatch(r"\w+", v)}
        self._vocab_phrases = sorted({v.lower() for v in vocab if not re.fullmatch(r"\w+", v)}, key=len, reverse=True)
        self._files = [f for f in (file_tokens or []) if f]
        self._files_re = re.compile("(?<![\\w])(?:" + "|".join(re.escape(f) for f in self._files) + ")(?![\\w])", re.IGNORECASE) if self._files else None

    @staticmethod
    def _alternation(names: dict[str, str], flags: int) -> Optional[re.Pattern]:
        if not names:
            return None
        return re.compile(_LB + "(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + ")" + _LA, flags)

    # ---- detection (used by the invariant) ----
    def find_name(self, s: str) -> bool:
        low = s.lower()
        if self._names_ci_re is not None and any(n in low for n in self._names_ci) and self._names_ci_re.search(s):
            return True
        return bool(self._names_cs_re is not None and any(n in s for n in self._names_cs) and self._names_cs_re.search(s))

    def _vocab_hits(self, s: str) -> list[str]:
        if not self._vocab_words and not self._vocab_phrases:
            return []
        low = s.lower()
        # whole tokens ("stuck_sensor") and their "_"-separated parts ("zeta" in "zeta_2")
        hits = (set(re.findall(r"\w+", low)) | set(re.split(r"[\W_]+", low))) & self._vocab_words
        hits |= {p for p in self._vocab_phrases if p in low}
        return sorted(hits, key=len, reverse=True)

    def find_value(self, s: str) -> bool:
        hits = self._vocab_hits(s)
        return bool(hits and re.search(_LB + "(?:" + "|".join(re.escape(h) for h in hits) + ")" + _LA, s, re.IGNORECASE))

    def find_file(self, s: str) -> bool:
        return bool(self._files_re is not None and self._files_re.search(s))

    @staticmethod
    def find_time(s: str) -> bool:
        return bool(_ISO_TOKEN_RE.search(s) or _DATE_TOKEN_RE.search(s) or _CLOCK_TOKEN_RE.search(s) or _EPOCH_TOKEN_RE.search(s))

    def find_long_number(self, s: str) -> bool:
        return any(_token_sig_digits(m.group(0)) > self.digits for m in _DECIMAL_TOKEN_RE.finditer(s))

    # ---- rewriting ----
    def _sub(self, pattern: Optional[re.Pattern], repl: Any, s: str, counter: str) -> str:
        if pattern is None:
            return s
        s2, n = pattern.subn(repl, s)
        self.counts[counter] += n
        return s2

    def redact(self, s: str, numbers: bool = True) -> str:
        if not s:
            return s
        out = self._sub(self._files_re, REDACT_FILE, s, "files_redacted")
        out = self._sub(_DATAFILE_TOKEN_RE, REDACT_FILE, out, "files_redacted")
        for pat in (_ISO_TOKEN_RE, _DATE_TOKEN_RE, _CLOCK_TOKEN_RE, _EPOCH_TOKEN_RE):
            out = self._sub(pat, REDACT_TIME, out, "times_redacted")
        low = out.lower()
        if self._names_ci_re is not None and any(n in low for n in self._names_ci):
            out = self._sub(self._names_ci_re, lambda m: self._names_ci.get(m.group(0).lower(), REDACT_COLUMN), out, "names_aliased")
        if self._names_cs_re is not None and any(n in out for n in self._names_cs):
            out = self._sub(self._names_cs_re, lambda m: self._names_cs.get(m.group(0), REDACT_COLUMN), out, "names_aliased")
        hits = self._vocab_hits(out)
        if hits:
            out = self._sub(re.compile(_LB + "(?:" + "|".join(re.escape(h) for h in hits) + ")" + _LA, re.IGNORECASE), REDACT_VALUE, out, "values_redacted")
        if numbers:
            out = _DECIMAL_TOKEN_RE.sub(self._round_token, out)
        if out != s:
            self.counts["strings_redacted"] += 1
        return out

    def _round_token(self, m: re.Match) -> str:
        tok = m.group(0)
        if _token_sig_digits(tok) <= self.digits:
            return tok
        try:
            self.counts["numbers_in_strings_rounded"] += 1
            return _format_sig(float(tok), self.digits)
        except ValueError:
            return tok


# ----------------------------------------------------------------------------------------------
# sanitiser: one walk over the payload, fail-closed per field
# ----------------------------------------------------------------------------------------------


class _Sanitizer:
    def __init__(self, cfg: GuardConfig, strict: bool, redactor: _Redactor):
        self.cfg = cfg
        self.strict = strict
        self.red = redactor
        self.drop_keys = {str(k).lower() for k in cfg.drop_keys_external}
        self.digits = max(1, int(cfg.external_sig_digits))
        self.numeric = 0
        self.max_series = 0
        self.n_strings = 0
        self.dropped = 0  # list items dropped (weak aggregates, free text, row-like blocks)
        self.dropped_fields = 0  # dict fields dropped for the same reasons
        self.dropped_human = 0  # human free-text fields dropped (strict mode)
        self.floats_rounded = 0
        self.key_drops: dict[str, int] = {}
        self.notes: list[str] = []

    # returns the sanitised node, or raises _Drop when the node itself must not leave
    def walk(self, node: Any, key: Optional[str], path: str, in_values: bool = False, in_rule: bool = False) -> Any:
        if isinstance(node, dict):
            return self._walk_dict(node, key, path, in_values, in_rule)
        if isinstance(node, list):
            return self._walk_list(node, key, path, in_values, in_rule)
        if isinstance(node, bool) or node is None:
            return node
        if isinstance(node, float):
            self.numeric += 1
            return self._number(node, key, in_rule)
        if isinstance(node, int):
            self.numeric += 1
            if abs(node) >= 10**9 and not in_rule and not (key is not None and _LOCATOR_KEY_RE.match(str(key))):
                return int(self._number(float(node), None, in_rule))  # epoch-sized integers are data, not counts
            return node
        if isinstance(node, str):
            self.n_strings += 1
            out = self.red.redact(node, numbers=not (in_rule or key in NUMBER_EXEMPT_KEYS))
            if self.cfg.forbid_categorical_values and (in_values or not _is_text_key(key)) and not (_is_known_string(node) or _is_known_string(out)):
                raise _Drop(f"free-text/categorical value under key '{key or '?'}'")
            return out
        return node

    def _number(self, x: float, key: Optional[str], in_rule: bool) -> Any:
        if in_rule or not math.isfinite(x):
            return x
        if x == int(x) and key is not None and _LOCATOR_KEY_RE.match(str(key)) and abs(x) < 2**53:
            return int(x)
        r = round_sig(x, self.digits)
        if r != x:
            self.floats_rounded += 1
        return r

    def _walk_dict(self, node: dict[str, Any], key: Optional[str], path: str, in_values: bool, in_rule: bool) -> dict[str, Any]:
        n = node.get("n_samples") if "n_samples" in node else None
        if _is_num(n) and n < self.cfg.min_aggregate_n:
            raise _Drop(f"aggregate computed over n_samples={n} < min_aggregate_n={self.cfg.min_aggregate_n}")
        in_rule = in_rule or _is_rule_def(node)
        out: dict[str, Any] = {}
        for k, v in node.items():
            kl = str(k).lower()
            if kl in self.drop_keys and not (in_rule and kl in RULE_LIMIT_KEYS):
                if v not in (None, "", [], {}):
                    self.key_drops[kl] = self.key_drops.get(kl, 0) + 1
                continue
            if self.strict and k in HUMAN_TEXT_KEYS:
                if v not in (None, ""):
                    self.dropped_human += 1
                    self.notes.append(f"dropped human free text '{k}' at {path}")
                continue
            k2 = self.red.redact(str(k), numbers=not in_rule)  # keys can carry values too (counts keyed by reading)
            if self.cfg.forbid_categorical_values and not _is_text_key(key) and not _is_known_key(k2):
                self.dropped_fields += 1
                self.notes.append(f"dropped a field with a free-text key at {path}")
                continue
            sub_in_values = in_values or k in ("values", "fingerprint", "signature", "params", "stats", "options_used")
            try:
                out[k2] = self.walk(v, str(k), f"{path}.{k2}", sub_in_values, in_rule)
            except _Drop as e:
                self.dropped_fields += 1
                self.notes.append(f"dropped field {path}.{k2}: {e}")
        return out

    def _walk_list(self, node: list[Any], key: Optional[str], path: str, in_values: bool, in_rule: bool) -> list[Any]:
        if node and all(_is_num(x) for x in node):
            if len(node) > self.cfg.max_series_points:
                raise _Drop(f"numeric series of {len(node)} points exceeds max_series_points={self.cfg.max_series_points}")
            self.max_series = max(self.max_series, len(node))
            self.numeric += len(node)
            return [self._number(x, None, in_rule) if isinstance(x, float) else x for x in node]
        if self.cfg.forbid_row_like_structures and _looks_row_like(node):
            raise _Drop(f"row-like structure ({len(node)} records)")
        out: list[Any] = []
        for i, item in enumerate(node):
            try:
                out.append(self.walk(item, key, f"{path}[{i}]", in_values, in_rule))
            except _Drop as e:
                self.dropped += 1
                self.notes.append(f"dropped item {path}[{i}]: {e}")
        return out

    def counts(self) -> dict[str, int]:
        c = dict(self.red.counts)
        c.update({"floats_rounded": self.floats_rounded, "keys_dropped": int(sum(self.key_drops.values())), "fields_dropped": self.dropped_fields, "items_dropped": self.dropped, "human_notes_dropped": self.dropped_human})
        return c


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
# aliasing of fields that name columns
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
                        continue  # never leaks the original header; the alias is in "id"
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
# invariant: the last line of defence, checked on exactly what is about to be sent
# ----------------------------------------------------------------------------------------------


def verify_invariant(sanitized: Any, amap: Optional[dict[str, str]], vocab: Optional[list[str]], cfg: GuardConfig) -> list[str]:
    """Violations found in `sanitized` (a payload, or any JSON-like value such as a list of message texts). Empty list =
    safe to send. Checked: no float with more than external_sig_digits significant digits (also inside strings); no ISO
    date / time; no original column name; no data-vocabulary value; no list of more than max_series_points numbers; no
    key from drop_keys_external. Operator-written rule limits are exempt from the number and min/max/value rules, as
    in the sanitiser. Messages never contain the offending value."""
    red = _Redactor(cfg, amap, vocab, None)
    drop_keys = {str(k).lower() for k in cfg.drop_keys_external}
    digits = max(1, int(cfg.external_sig_digits))
    out: list[str] = []

    def check_str(s: str, path: str, numbers: bool) -> None:
        if red.find_time(s):
            out.append(f"date/time token at {path}")
        if red.find_name(s):
            out.append(f"original column name at {path}")
        if red.find_value(s):
            out.append(f"data vocabulary value at {path}")
        if numbers and red.find_long_number(s):
            out.append(f"number with more than {digits} significant digits inside a string at {path}")

    def walk(node: Any, key: Optional[str], path: str, in_rule: bool) -> None:
        if len(out) >= 20:
            return
        if isinstance(node, dict):
            rule = in_rule or _is_rule_def(node)
            for k, v in node.items():
                ks = str(k)
                if ks.lower() in drop_keys and not (rule and ks.lower() in RULE_LIMIT_KEYS) and v not in (None, "", [], {}):
                    out.append(f"dropped key '{ks.lower()}' present at {path}")
                check_str(ks, f"{path} (key)", numbers=not rule)
                walk(v, ks, f"{path}.{ks}" if _KEY_RE.match(ks) else f"{path}.?", rule)
        elif isinstance(node, list):
            if len(node) > cfg.max_series_points and all(_is_num(x) for x in node):
                out.append(f"list of {len(node)} numbers at {path} exceeds max_series_points={cfg.max_series_points}")
            for i, v in enumerate(node):
                walk(v, key, f"{path}[{i}]", in_rule)
        elif isinstance(node, float) and not isinstance(node, bool):
            if not in_rule and math.isfinite(node) and round_sig(node, digits) != node:
                out.append(f"float with more than {digits} significant digits at {path}")
        elif isinstance(node, str):
            check_str(node, path, numbers=not (in_rule or key in NUMBER_EXEMPT_KEYS))

    if isinstance(sanitized, dict):
        for k, v in sanitized.items():
            ks = str(k)
            in_rule = ARTIFACT_KEYS.get(ks) in RULE_ARTIFACT_TYPES
            if ks.lower() in drop_keys and v not in (None, "", [], {}):
                out.append(f"dropped key '{ks.lower()}' present at $")
            check_str(ks, "$ (key)", numbers=False)
            walk(v, ks, f"$.{ks}" if _KEY_RE.match(ks) else "$.?", in_rule)
    else:
        walk(sanitized, None, "$", False)
    return out[:20]


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


def _aliasing_on(cfg: GuardConfig, strict: bool) -> bool:
    return bool(cfg.alias_names_external or (strict and cfg.alias_column_names_in_strict) or not cfg.allow_column_names)


def alias_map(ws: Any = None, payload: Optional[dict[str, Any]] = None) -> dict[str, str]:
    """original column name -> alias (S01..) or "[column]" for columns without an alias, from the run's schema / signal
    catalog and from the payload itself. Local knowledge: never part of anything that is sent."""
    amap = dict(_names_from_ws(ws)[0])
    if isinstance(payload, dict):
        amap.update(_collect_alias_map(payload))
    return amap


def _is_empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def check(payload: dict[str, Any], settings: Settings, strict: Optional[bool] = None, ws: Any = None) -> GuardResult:
    """Sanitise `payload` for an external model and decide whether the result may leave. Never raises.
    ws (optional) gives the guard the run's column names, source file name and text-column values to redact."""
    cfg = settings.guard
    if strict is None:
        strict = settings.active_profile.guard_strict
    strict = bool(strict)
    notes: list[str] = []
    if not isinstance(payload, dict):
        return GuardResult(False, "payload must be a JSON object keyed by artifact type", {}, [], {"payload_bytes": payload_bytes(payload)}, notes, strict)
    work = to_plain(payload)
    if not isinstance(work, dict):
        return GuardResult(False, "payload could not be normalized to a JSON object", {}, [], {}, notes, strict)

    # 1. whitelist by top-level key (unknown keys are dropped, never sent)
    artifact_types: list[str] = []
    sanitized: dict[str, Any] = {}
    for k, v in work.items():
        atype = ARTIFACT_KEYS.get(str(k))
        if atype is None:
            notes.append(f"dropped unknown top-level key '{k}' (not in the egress whitelist)" if _KEY_RE.match(str(k)) else "dropped an unknown top-level key (not in the egress whitelist)")
            continue
        if atype not in artifact_types:
            artifact_types.append(atype)
        sanitized[k] = v
    if not sanitized:
        return GuardResult(False, "payload contains no whitelisted artifacts", {}, [], {"payload_bytes": payload_bytes(payload)}, notes, strict)

    # 2. aliasing of original column names (fields that name columns; sentences are handled by the redactor)
    aliasing = _aliasing_on(cfg, strict)
    ws_names, file_tokens = _names_from_ws(ws)
    amap: dict[str, str] = {}
    if aliasing:
        amap = dict(ws_names)
        amap.update(_collect_alias_map(sanitized))
        sanitized = _strip_names(sanitized, amap, notes)
        if amap:
            notes.append(f"aliased {len([a for a in amap.values() if a != REDACT_COLUMN])} original column names")

    # 3. sanitise: drop keys, round floats, redact strings; fail-closed per field
    vrec = vocabulary_record(ws)
    vocab = [v for v in vrec.get("values", []) if _usable_vocab_value(v)]
    if ws is not None and (not vrec.get("complete", True) or vrec.get("capped")):
        notes.append("data vocabulary is partial (scan capped or timed out): text values are also stopped by the free-text rule")
    red = _Redactor(cfg, amap, vocab, file_tokens)
    san = _Sanitizer(cfg, strict, red)
    out: dict[str, Any] = {}
    top_drop = {str(k).lower() for k in cfg.drop_keys_external}
    for k, v in sanitized.items():
        in_rule = ARTIFACT_KEYS.get(str(k)) in RULE_ARTIFACT_TYPES
        if str(k).lower() in top_drop:
            if not _is_empty(v):
                san.key_drops[str(k).lower()] = san.key_drops.get(str(k).lower(), 0) + 1
            continue
        try:
            out[k] = san.walk(v, str(k), f"$.{k}", False, in_rule)
        except _Drop as e:
            san.dropped_fields += 1
            san.notes.append(f"dropped field $.{k}: {e}")
    sanitized = out
    if san.key_drops:
        notes.append("dropped keys that carry single readings, timestamps, file names or label-based evaluation: " + ", ".join(f"{k} x{n}" for k, n in sorted(san.key_drops.items())))
    notes.extend(san.notes)
    counts = san.counts()
    counts["vocabulary_size"] = len(vocab)
    stats: dict[str, Any] = {"payload_bytes": payload_bytes(sanitized), "numeric_values": san.numeric, "max_series_len": san.max_series, "n_strings": san.n_strings, "items_dropped": san.dropped, "fields_dropped": san.dropped_fields + san.dropped_human, "names_aliased": counts.get("names_aliased", 0), "artifact_types": artifact_types}

    def blocked(reason: str) -> GuardResult:
        return GuardResult(False, reason, {}, artifact_types, stats, notes, strict, amap, counts)

    useful = [k for k, v in sanitized.items() if ARTIFACT_KEYS.get(str(k)) != "meta" and not _is_empty(v)]
    if not useful:
        why = "; ".join(n for n in notes if n.startswith("dropped"))[:300]
        return blocked("nothing useful left after sanitising" + (f" ({why})" if why else ""))
    if san.numeric > cfg.max_numeric_values_per_payload:
        return blocked(f"{san.numeric} numeric values exceed max_numeric_values_per_payload={cfg.max_numeric_values_per_payload}")

    # 4. invariant on exactly what would be sent
    violations = verify_invariant(sanitized, amap, vocab + file_tokens, cfg)
    if violations:
        counts["invariant_violations"] = len(violations)
        notes.extend(f"invariant: {v}" for v in violations[:8])
        return blocked("egress invariant violated: " + "; ".join(violations[:3]))

    # 5. size
    nbytes = stats["payload_bytes"]
    if nbytes > cfg.max_payload_bytes:
        return blocked(f"payload of {nbytes} bytes exceeds max_payload_bytes={cfg.max_payload_bytes}")

    reason = "allowed: derived artifacts only (" + ", ".join(artifact_types) + ")"
    if san.dropped:
        reason += f"; {san.dropped} item(s) dropped"
    if san.dropped_fields or san.key_drops:
        reason += f"; {san.dropped_fields + int(sum(san.key_drops.values()))} field(s) dropped"
    if san.dropped_human:
        reason += f"; {san.dropped_human} human note(s) dropped"
    if san.floats_rounded or counts.get("numbers_in_strings_rounded"):
        reason += f"; numbers rounded to {cfg.external_sig_digits} significant digits"
    return GuardResult(True, reason, sanitized, artifact_types, stats, notes, strict, amap, counts)


def sanitize_text(text: str, settings: Settings, ws: Any = None, amap: Optional[dict[str, str]] = None) -> tuple[str, dict[str, int]]:
    """String sanitiser for message text that is not a JSON payload (tool-agent prompts): file name -> [file], dates and
    times -> [time], column names -> aliases, data values -> [value], long decimals rounded. Returns (text, counts)."""
    cfg = settings.guard
    ws_names, file_tokens = _names_from_ws(ws)
    names = dict(ws_names)
    names.update(amap or {})
    red = _Redactor(cfg, names if _aliasing_on(cfg, settings.active_profile.guard_strict) else {}, data_vocabulary(ws), file_tokens)
    return red.redact(text or ""), dict(red.counts)


def verify_texts(texts: list[str], settings: Settings, ws: Any = None, amap: Optional[dict[str, str]] = None) -> list[str]:
    """Invariant check for final message texts (agent route). Empty list = safe to send."""
    cfg = settings.guard
    ws_names, file_tokens = _names_from_ws(ws)
    names = dict(ws_names)
    names.update(amap or {})
    if not _aliasing_on(cfg, settings.active_profile.guard_strict):
        names = {}
    return verify_invariant([t for t in texts if isinstance(t, str)], names, data_vocabulary(ws) + file_tokens, cfg)


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
        head = (f"Profile '{settings.profile}': your raw data stays on this machine. Only summaries that the app computed "
                f"itself may be sent to the external model {settings.external_llm.model} ({settings.external_llm.provider}"
                + (f", endpoint {settings.external_llm.base_url}" if settings.external_llm.base_url else "") + "), and only after the egress guard has cleaned and checked them.")
        why_not = settings.external_block_reason()
        if why_not:
            head += f" Right now the external model is NOT used: {why_not}."
    aliasing = _aliasing_on(cfg, bool(strict))
    lines = [
        head,
        "",
        "What may leave (only after passing the guard):",
        "- the signal catalog: aliases (S01, S02, ...), structural roles, hypotheses with confidence, aggregate fingerprints",
        "- relation summaries (correlation / lag between signals), data-quality check statements, trust verdicts",
        "- flags and diagnoses (statements, ranked signals, propagation steps, row numbers), evidence statements with their IDs",
        "- rule text written by the operator, report section drafts, chat questions, tool results made of aggregates",
        "",
        "How the guard cleans a payload before it leaves:",
        f"- every number with decimals is rounded to {cfg.external_sig_digits} significant digits, also inside sentences; counts and row numbers stay",
        "- dates and times are replaced by [time]; row numbers remain the locator",
        "- values of the dataset's text, category and label columns are replaced by [value]; the source file name by [file]",
        "- original column names are replaced by aliases (S01..)" + ("" if aliasing else " -- switched OFF in the settings (guard.alias_names_external)") + "; very generic header words such as 'time' or 'id' are only removed where a field lists columns",
        "- fields that carry single readings, timestamps, file names or label-based evaluation are removed at any depth: " + ", ".join(cfg.drop_keys_external[:12]) + ", ...",
        "- limits the operator wrote into a rule (for example 'below 2715.5') are kept exactly: they are not data",
        "- a field that still looks like raw data is dropped on its own; the rest of the payload is still sent",
        "",
        "What never leaves:",
        f"- raw rows or any numeric series longer than {cfg.max_series_points} points",
        f"- payloads with more than {cfg.max_numeric_values_per_payload} numbers or larger than {cfg.max_payload_bytes // 1000} kB",
        f"- aggregates computed over fewer than {cfg.min_aggregate_n} samples",
        "- record-like structures (lists of records sharing numeric fields)" if cfg.forbid_row_like_structures else "- (row-like structures are allowed by config)",
        "- free-text or categorical record values (names, comments, labels)" if cfg.forbid_categorical_values else "- (categorical values are allowed by config)",
        "- single readings (min, max, first, last, observed values), data timestamps, file names and paths",
        "- human notes on flags / diagnoses" + (" (dropped in strict mode)" if strict else " (kept in non-strict mode)"),
        "",
        "Last check: just before sending, the cleaned payload is tested once more (no long numbers, no dates, no column names, "
        "no data values, no long number lists, no removed fields). If that test fails, or nothing useful is left, the payload is "
        "blocked: the task is answered by the local model instead, then by a code template. Every block, fallback and what "
        "the cleaner changed is written to the egress ledger.",
    ]
    return "\n".join(lines)
