"""Operating rules (decisions 24-26): plain-language rules compiled into a CLOSED JSON spec that only this
module's executor runs. Rules are never arbitrary code.

RULE JSON SCHEMA (``RULE_JSON_SCHEMA`` below is the machine-readable version handed to the LLM compiler)
--------------------------------------------------------------------------------------------------
Every compiled rule is an object with a ``type`` and type-specific fields. Signals are aliases (``"S03"``).
Optional on every type: ``"group"`` (evaluate only rows of this group id) and ``"severity"`` (0..1, default 0.7).

    range           {"type": "range", "signal": "S03", "min": 100, "max": 140}          (min or max may be omitted)
    rate_of_change  {"type": "rate_of_change", "signal": "S07", "max_abs_delta": 5, "per_samples": 1}
    acceleration    {"type": "acceleration", "signal": "S02", "max_abs_second_diff": 3}
    duration        {"type": "duration", "signal": "S03", "condition": {"op": "above", "value": 100}, "min_samples": 21}
                     condition.op in above | below | between (min, max) | missing | constant; the rule is violated when the
                     condition holds for at least min_samples consecutive samples
    cross_signal    {"type": "cross_signal", "if": {"signal": "S09", "op": ">", "value": 90}, "then": {"signal": "S01", "op": ">", "value": 45}}
                     op in > >= < <= == !=
    rolling_stat    {"type": "rolling_stat", "signal": "S04", "stat": "std", "window": 60, "op": "<", "value": 8}
                     stat in mean | std | min | max ; violated where the rolling statistic does not satisfy ``op value``
    missing         {"type": "missing", "signal": "S05", "max_consecutive": 10}
    stuck           {"type": "stuck", "signal": "S12", "max_constant_samples": 30}
    drift           {"type": "drift", "signal": "S10", "window": 200, "max_abs_change": 15}
                     violated where (rolling max - rolling min) over the window exceeds max_abs_change

Differences, rolling windows and runs never cross a group boundary when the frame carries ``__group__``.
NaN never violates a numeric comparison (missing data is reported by the ``missing`` type and the baseline checks).

Lifecycle: draft -> approved -> active -> retired, or draft -> rejected. Human decisions come through
``apply_override`` (object_type "rule"). Rules persist in rules.json (list[Rule]).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..contracts import CheckResult, Rule, now_iso
from ._common import GROUP_COL, ROW_COL, SignalInfo, check_confidence, contiguous_runs, fmt_num, load_catalog, next_check_id, numeric_signals, resolve_columns, value_runs

RULE_TYPES = ["range", "rate_of_change", "acceleration", "duration", "cross_signal", "rolling_stat", "missing", "stuck", "drift"]
OPS = [">", ">=", "<", "<=", "==", "!="]
STATS = ["mean", "std", "min", "max"]
DURATION_OPS = ["above", "below", "between", "missing", "constant"]
DEFAULT_SEVERITY = 0.7

RULE_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "closed-rule-spec-v1",
    "description": "Exactly one object. 'type' selects the check; signals are catalog aliases like 'S03'. Optional on all types: 'group' (string), 'severity' (0..1).",
    "oneOf": [
        {"type": "range", "required": ["signal"], "properties": {"signal": "alias", "min": "number?", "max": "number?"}},
        {"type": "rate_of_change", "required": ["signal", "max_abs_delta"], "properties": {"signal": "alias", "max_abs_delta": "number>0", "per_samples": "int>=1 (default 1)"}},
        {"type": "acceleration", "required": ["signal", "max_abs_second_diff"], "properties": {"signal": "alias", "max_abs_second_diff": "number>0"}},
        {"type": "duration", "required": ["signal", "condition", "min_samples"], "properties": {"signal": "alias", "condition": {"op": "above|below|between|missing|constant", "value": "number (above/below)", "min": "number (between)", "max": "number (between)"}, "min_samples": "int>=1"}},
        {"type": "cross_signal", "required": ["if", "then"], "properties": {"if": {"signal": "alias", "op": "> >= < <= == !=", "value": "number"}, "then": {"signal": "alias", "op": "> >= < <= == !=", "value": "number"}}},
        {"type": "rolling_stat", "required": ["signal", "stat", "window", "op", "value"], "properties": {"signal": "alias", "stat": "mean|std|min|max", "window": "int>=2", "op": "> >= < <= == !=", "value": "number"}},
        {"type": "missing", "required": ["signal", "max_consecutive"], "properties": {"signal": "alias", "max_consecutive": "int>=0"}},
        {"type": "stuck", "required": ["signal", "max_constant_samples"], "properties": {"signal": "alias", "max_constant_samples": "int>=1"}},
        {"type": "drift", "required": ["signal", "window", "max_abs_change"], "properties": {"signal": "alias", "window": "int>=2", "max_abs_change": "number>0"}},
    ],
}


# ================================================================== validation
def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v: Any) -> Optional[int]:
    f = _num(v)
    if f is None or abs(f - round(f)) > 1e-9:
        return None
    return int(round(f))


def validate_compiled(spec: Any, aliases: Optional[set[str]] = None) -> tuple[bool, list[str], Optional[dict[str, Any]]]:
    """Strict validation against the closed schema. Returns (ok, errors, normalized_spec)."""
    errors: list[str] = []
    if not isinstance(spec, dict):
        return False, ["spec must be an object"], None
    t = spec.get("type")
    if t not in RULE_TYPES:
        return False, [f"unknown rule type {t!r}; allowed: {', '.join(RULE_TYPES)}"], None
    out: dict[str, Any] = {"type": t}

    def sig(key: str, node: dict[str, Any], dst: dict[str, Any]) -> None:
        s = node.get(key)
        if not isinstance(s, str) or not re.fullmatch(r"S\d{2,}", s):
            errors.append(f"{key} must be a signal alias like 'S03' (got {s!r})")
            return
        if aliases is not None and s not in aliases:
            errors.append(f"signal {s} is not in the signal catalog")
            return
        dst[key] = s

    def num(key: str, node: dict[str, Any], dst: dict[str, Any], required: bool = True, positive: bool = False) -> None:
        if key not in node or node[key] is None:
            if required:
                errors.append(f"{key} is required")
            return
        v = _num(node[key])
        if v is None or (positive and v <= 0):
            errors.append(f"{key} must be a {'positive ' if positive else ''}number (got {node[key]!r})")
            return
        dst[key] = v

    def integer(key: str, node: dict[str, Any], dst: dict[str, Any], minimum: int, default: Optional[int] = None) -> None:
        if key not in node or node[key] is None:
            if default is None:
                errors.append(f"{key} is required")
            else:
                dst[key] = default
            return
        v = _int(node[key])
        if v is None or v < minimum:
            errors.append(f"{key} must be an integer >= {minimum} (got {node[key]!r})")
            return
        dst[key] = v

    def cond(key: str, node: dict[str, Any], dst: dict[str, Any]) -> None:
        c = node.get(key)
        if not isinstance(c, dict):
            errors.append(f"{key} must be an object {{signal, op, value}}")
            return
        d: dict[str, Any] = {}
        sig("signal", c, d)
        if c.get("op") not in OPS:
            errors.append(f"{key}.op must be one of {' '.join(OPS)}")
        else:
            d["op"] = c["op"]
        num("value", c, d)
        dst[key] = d

    if t == "range":
        sig("signal", spec, out)
        num("min", spec, out, required=False)
        num("max", spec, out, required=False)
        if "min" not in out and "max" not in out and not errors:
            errors.append("range needs min and/or max")
        if "min" in out and "max" in out and out["min"] > out["max"]:
            errors.append("range min must be <= max")
    elif t == "rate_of_change":
        sig("signal", spec, out)
        num("max_abs_delta", spec, out, positive=True)
        integer("per_samples", spec, out, 1, default=1)
    elif t == "acceleration":
        sig("signal", spec, out)
        num("max_abs_second_diff", spec, out, positive=True)
    elif t == "duration":
        sig("signal", spec, out)
        c = spec.get("condition")
        if isinstance(c, str):
            c = {"op": c, "value": spec.get("value"), "min": spec.get("min"), "max": spec.get("max")}
        if not isinstance(c, dict) or c.get("op") not in DURATION_OPS:
            errors.append(f"duration.condition.op must be one of {' '.join(DURATION_OPS)}")
        else:
            d: dict[str, Any] = {"op": c["op"]}
            if c["op"] in ("above", "below"):
                num("value", c, d)
            elif c["op"] == "between":
                num("min", c, d)
                num("max", c, d)
            out["condition"] = d
        integer("min_samples", spec, out, 1)
    elif t == "cross_signal":
        cond("if", spec, out)
        cond("then", spec, out)
    elif t == "rolling_stat":
        sig("signal", spec, out)
        if spec.get("stat") not in STATS:
            errors.append(f"stat must be one of {' '.join(STATS)}")
        else:
            out["stat"] = spec["stat"]
        integer("window", spec, out, 2)
        if spec.get("op") not in OPS:
            errors.append(f"op must be one of {' '.join(OPS)}")
        else:
            out["op"] = spec["op"]
        num("value", spec, out)
    elif t == "missing":
        sig("signal", spec, out)
        integer("max_consecutive", spec, out, 0)
    elif t == "stuck":
        sig("signal", spec, out)
        integer("max_constant_samples", spec, out, 1)
    elif t == "drift":
        sig("signal", spec, out)
        integer("window", spec, out, 2)
        num("max_abs_change", spec, out, positive=True)
    if spec.get("group") not in (None, ""):
        out["group"] = str(spec["group"])
    if spec.get("severity") is not None:
        sv = _num(spec["severity"])
        if sv is None or not 0.0 <= sv <= 1.0:
            errors.append("severity must be within 0..1")
        else:
            out["severity"] = sv
    extra = set(spec) - set(out) - {"condition", "value", "min", "max", "per_samples", "severity", "group"}
    if extra:
        errors.append(f"unknown fields: {', '.join(sorted(map(str, extra)))}")
    return (not errors), errors, (out if not errors else None)


def rule_signals(spec: dict[str, Any]) -> list[str]:
    if spec.get("type") == "cross_signal":
        return [spec["if"]["signal"], spec["then"]["signal"]]
    return [spec["signal"]]


# ================================================================== explanation (template)
_OP_WORDS = {">": "above", ">=": "at least", "<": "below", "<=": "at most", "==": "equal to", "!=": "different from"}


def explain_rule(spec: dict[str, Any]) -> str:
    t = spec["type"]
    g = f" (only in group {spec['group']})" if spec.get("group") else ""
    if t == "range":
        if "min" in spec and "max" in spec:
            body = f"{spec['signal']} must stay within {fmt_num(spec['min'])} to {fmt_num(spec['max'])}; any sample outside this band is a violation"
        elif "min" in spec:
            body = f"{spec['signal']} must stay at or above {fmt_num(spec['min'])}; any lower sample is a violation"
        else:
            body = f"{spec['signal']} must stay at or below {fmt_num(spec['max'])}; any higher sample is a violation"
    elif t == "rate_of_change":
        per = "per sample" if spec.get("per_samples", 1) == 1 else f"over {spec['per_samples']} samples"
        body = f"{spec['signal']} may not change by more than {fmt_num(spec['max_abs_delta'])} {per} (absolute difference)"
    elif t == "acceleration":
        body = f"the second difference (acceleration) of {spec['signal']} may not exceed {fmt_num(spec['max_abs_second_diff'])} in absolute value"
    elif t == "duration":
        c = spec["condition"]
        if c["op"] in ("above", "below"):
            what = f"{c['op']} {fmt_num(c['value'])}"
        elif c["op"] == "between":
            what = f"between {fmt_num(c['min'])} and {fmt_num(c['max'])}"
        else:
            what = c["op"]
        body = f"{spec['signal']} may not stay {what} for {spec['min_samples']} consecutive samples or longer"
    elif t == "cross_signal":
        a, b = spec["if"], spec["then"]
        body = f"whenever {a['signal']} is {_OP_WORDS[a['op']]} {fmt_num(a['value'])}, {b['signal']} must be {_OP_WORDS[b['op']]} {fmt_num(b['value'])}"
    elif t == "rolling_stat":
        body = f"the rolling {spec['window']}-sample {spec['stat']} of {spec['signal']} must be {_OP_WORDS[spec['op']]} {fmt_num(spec['value'])}"
    elif t == "missing":
        body = f"{spec['signal']} may not be missing for more than {spec['max_consecutive']} consecutive samples"
    elif t == "stuck":
        body = f"{spec['signal']} may not keep exactly the same value for more than {spec['max_constant_samples']} consecutive samples"
    else:
        body = f"{spec['signal']} may not move by more than {fmt_num(spec['max_abs_change'])} (max minus min) within any {spec['window']}-sample window"
    return body[0].upper() + body[1:] + g + "."


# ================================================================== executor
def _segment_mask(df: pd.DataFrame, k: int) -> np.ndarray:
    """True for the first k rows of every group segment (where differences/windows are undefined)."""
    n = len(df)
    idx = np.arange(n)
    if GROUP_COL in df.columns and n > 1:
        g = df[GROUP_COL].astype(str).to_numpy()
        boundary = np.zeros(n, dtype=bool)
        boundary[0] = True
        boundary[1:] = g[1:] != g[:-1]
    else:
        boundary = np.zeros(n, dtype=bool)
        if n:
            boundary[0] = True
    seg_start = np.maximum.accumulate(np.where(boundary, idx, 0))
    return (idx - seg_start) < k


def _col(df: pd.DataFrame, alias: str, colmap: Optional[dict[str, str]]) -> np.ndarray:
    name = alias if alias in df.columns else (colmap or {}).get(alias)
    if name is None or name not in df.columns:
        raise KeyError(f"signal {alias} not present in the frame")
    return pd.to_numeric(df[name], errors="coerce").to_numpy(dtype="float64", na_value=np.nan)


def _cmp(x: np.ndarray, op: str, value: float) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        if op == ">":
            return x > value
        if op == ">=":
            return x >= value
        if op == "<":
            return x < value
        if op == "<=":
            return x <= value
        if op == "==":
            return x == value
        return (x != value) & ~np.isnan(x)


def _runs_at_least(mask: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    return [(a, b) for a, b in contiguous_runs(mask) if b - a + 1 >= min_len]


def run_rule(rule: dict[str, Any] | Rule, df: pd.DataFrame, colmap: Optional[dict[str, str]] = None) -> list[dict[str, Any]]:
    """Vectorised evaluation of one compiled rule on a batch frame. Returns violation episodes
    [{row_start, row_end, n, signal, value_min, value_max, detail}] using __row__ numbers when present."""
    spec = rule.compiled if isinstance(rule, Rule) else rule
    if not spec:
        return []
    ok, errs, spec = validate_compiled(spec)
    if not ok:
        raise ValueError("invalid rule spec: " + "; ".join(errs))
    if spec.get("group") is not None and GROUP_COL in df.columns:
        df = df[df[GROUP_COL].astype(str) == str(spec["group"])]
    n = len(df)
    if n == 0:
        return []
    rows = df[ROW_COL].to_numpy(dtype="int64") if ROW_COL in df.columns else np.arange(n, dtype="int64")
    t = spec["type"]
    signal = spec.get("signal") or spec["then"]["signal"]
    x = _col(df, signal, colmap)
    episodes: list[tuple[int, int]] = []
    detail = ""
    if t == "range":
        v = np.zeros(n, dtype=bool)
        if "min" in spec:
            v |= _cmp(x, "<", spec["min"])
        if "max" in spec:
            v |= _cmp(x, ">", spec["max"])
        episodes = contiguous_runs(v)
        detail = "outside allowed range"
    elif t == "rate_of_change":
        k = int(spec.get("per_samples", 1))
        d = np.full(n, np.nan)
        if n > k:
            d[k:] = x[k:] - x[:-k]
        v = _cmp(np.abs(d), ">", spec["max_abs_delta"]) & ~_segment_mask(df, k)
        episodes = contiguous_runs(v)
        detail = f"change over {k} sample(s) exceeds {fmt_num(spec['max_abs_delta'])}"
    elif t == "acceleration":
        a = np.full(n, np.nan)
        if n > 2:
            a[2:] = x[2:] - 2 * x[1:-1] + x[:-2]
        v = _cmp(np.abs(a), ">", spec["max_abs_second_diff"]) & ~_segment_mask(df, 2)
        episodes = contiguous_runs(v)
        detail = f"second difference exceeds {fmt_num(spec['max_abs_second_diff'])}"
    elif t == "duration":
        c = spec["condition"]
        if c["op"] == "above":
            m = _cmp(x, ">", c["value"])
        elif c["op"] == "below":
            m = _cmp(x, "<", c["value"])
        elif c["op"] == "between":
            m = _cmp(x, ">=", c["min"]) & _cmp(x, "<=", c["max"])
        elif c["op"] == "missing":
            m = np.isnan(x)
        else:  # constant
            m = np.zeros(n, dtype=bool)
            starts, lengths, _vals = value_runs(x)
            for s0, L in zip(starts.tolist(), lengths.tolist()):
                if L >= 2:
                    m[s0 : s0 + L] = True
        m &= ~_segment_boundary_break(df, m)
        episodes = _runs_at_least(m, int(spec["min_samples"]))
        detail = f"condition '{c['op']}' held for >= {spec['min_samples']} samples"
    elif t == "cross_signal":
        a, b = spec["if"], spec["then"]
        xa = _col(df, a["signal"], colmap)
        v = _cmp(xa, a["op"], a["value"]) & ~_cmp(x, b["op"], b["value"]) & ~np.isnan(x)
        episodes = contiguous_runs(v)
        detail = f"{a['signal']} {a['op']} {fmt_num(a['value'])} but {b['signal']} not {b['op']} {fmt_num(b['value'])}"
    elif t == "rolling_stat":
        w = int(spec["window"])
        s = pd.Series(x)
        r = s.rolling(w, min_periods=w)
        stat = {"mean": r.mean, "std": r.std, "min": r.min, "max": r.max}[spec["stat"]]().to_numpy(dtype="float64")
        v = ~_cmp(stat, spec["op"], spec["value"]) & ~np.isnan(stat) & ~_segment_mask(df, w - 1)
        episodes = contiguous_runs(v)
        detail = f"rolling {w}-sample {spec['stat']} not {spec['op']} {fmt_num(spec['value'])}"
        x = stat
    elif t == "missing":
        m = np.isnan(x)
        episodes = _runs_at_least(m, int(spec["max_consecutive"]) + 1)
        detail = f"missing for more than {spec['max_consecutive']} consecutive samples"
    elif t == "stuck":
        m = np.zeros(n, dtype=bool)
        starts, lengths, _vals = value_runs(x)
        lim = int(spec["max_constant_samples"])
        for s0, L in zip(starts.tolist(), lengths.tolist()):
            if L > lim:
                m[s0 : s0 + L] = True
        m &= ~_segment_boundary_break(df, m)
        episodes = _runs_at_least(m, lim + 1)
        detail = f"constant for more than {spec['max_constant_samples']} samples"
    elif t == "drift":
        w = int(spec["window"])
        s = pd.Series(x)
        rng = (s.rolling(w, min_periods=w).max() - s.rolling(w, min_periods=w).min()).to_numpy(dtype="float64")
        v = _cmp(rng, ">", spec["max_abs_change"]) & ~_segment_mask(df, w - 1)
        episodes = contiguous_runs(v)
        detail = f"range within {w} samples exceeds {fmt_num(spec['max_abs_change'])}"
        x = rng
    out: list[dict[str, Any]] = []
    for a0, b0 in episodes:
        seg = x[a0 : b0 + 1]
        fin = seg[np.isfinite(seg)]
        out.append({"row_start": int(rows[a0]), "row_end": int(rows[b0]), "n": int(b0 - a0 + 1), "signal": signal, "value_min": float(fin.min()) if fin.size else None, "value_max": float(fin.max()) if fin.size else None, "detail": detail})
    return out


def _segment_boundary_break(df: pd.DataFrame, m: np.ndarray) -> np.ndarray:
    """Runs must not continue across group boundaries: mark boundary rows so contiguous_runs splits there."""
    n = len(df)
    if GROUP_COL not in df.columns or n < 2:
        return np.zeros(n, dtype=bool)
    g = df[GROUP_COL].astype(str).to_numpy()
    b = np.zeros(n, dtype=bool)
    b[1:] = g[1:] != g[:-1]
    return b & m & np.concatenate([[False], m[:-1]])  # break only where the run would otherwise continue


def rule_check_result(ws: Any, rule: Rule, violations: list[dict[str, Any]], batch_id: str, n_rows: int, group_id: Optional[str] = None) -> CheckResult:
    spec = rule.compiled or {}
    sev = float(spec.get("severity", DEFAULT_SEVERITY))
    signals = rule_signals(spec) if spec else []
    if violations:
        n_v = sum(v["n"] for v in violations)
        first = violations[0]
        vals = f"; values {fmt_num(first['value_min'])}..{fmt_num(first['value_max'])}" if first.get("value_min") is not None else ""
        statement = f"{rule.id} violated in batch {batch_id}: {n_v} samples in {len(violations)} episode(s) ({first['detail']}); first at rows {first['row_start']}-{first['row_end']}{vals}. Rule: {rule.text}"
        ev = ws.evidence.add("rule_violation", statement, signals=signals, values={"rule_id": rule.id, "n_violating": n_v, "n_episodes": len(violations), "episodes": [(v["row_start"], v["row_end"]) for v in violations[:12]]}, computed_by="quality.rules.run_rule", n_samples=n_rows, batch_id=batch_id, group_id=group_id)
        conf, basis = check_confidence(n_rows, exact=True)  # a compiled rule is an exact comparison: only the sample size limits it
        return CheckResult(check_id=next_check_id(ws), check_type=f"rule:{rule.id}", category="rule", signals=signals, batch_id=batch_id, group_id=group_id, status="fail", severity=min(1.0, sev), statement=statement, evidence_ids=[ev.id], rule_id=rule.id, values={"rule_type": spec.get("type"), "n_violating": n_v, "n_episodes": len(violations), "events": [(v["row_start"], v["row_end"]) for v in violations[:12]], "episodes": violations[:12], "confidence": conf, "confidence_basis": basis}, row_start=first["row_start"], row_end=violations[-1]["row_end"])
    statement = f"{rule.id} holds in batch {batch_id} ({n_rows} samples checked). Rule: {rule.text}"
    ev = ws.evidence.add("rule_pass", statement, signals=signals, values={"rule_id": rule.id, "n_rows": n_rows}, computed_by="quality.rules.run_rule", n_samples=n_rows, batch_id=batch_id, group_id=group_id)
    conf, basis = check_confidence(n_rows, exact=True)
    return CheckResult(check_id=next_check_id(ws), check_type=f"rule:{rule.id}", category="rule", signals=signals, batch_id=batch_id, group_id=group_id, status="pass", severity=0.0, statement=statement, evidence_ids=[ev.id], rule_id=rule.id, values={"rule_type": spec.get("type"), "n_violating": 0, "n_rows": n_rows, "confidence": conf, "confidence_basis": basis + "; nothing was found"})


# ================================================================== template parser
NUM = r"[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?"
SIG = r"S\d{1,3}"
_MUST = r"(?:must|should|shall|has to|needs to|is required to|may)"
_BE = r"(?:be|stay|remain|lie|keep|hold)"
_ABOVE = r"(?:above|over|greater than|higher than|more than|larger than|at least|no less than|not below|>=|>)"
_BELOW = r"(?:below|under|less than|lower than|smaller than|at most|no more than|not above|not exceed|<=|<)"


@dataclass
class ParseResult:
    spec: Optional[dict[str, Any]] = None
    confidence: float = 0.0
    explanation: str = ""
    candidates: dict[str, list[str]] = field(default_factory=dict)  # unresolved phrase -> candidate aliases
    error: str = ""


def _normalize(text: str) -> str:
    t = " " + text.strip().lower() + " "
    t = re.sub(r"\s+", " ", t)
    t = t.replace("’", "'")
    reps = [
        (r"\bstandard deviation\b", "std"), (r"\bstd\.? ?dev(?:iation)?\b", "std"), (r"\bstdev\b", "std"), (r"\bsigma\b", "std"),
        (r"\baverage\b", "mean"), (r"\bminimum\b", "min"), (r"\bmaximum\b", "max"),
        (r"\bper sample squared\b", "per sample squared"), (r"\bper sample\^?2\b", "per sample squared"), (r"\bper sample²\b", "per sample squared"), (r"\bunits? per sample squared\b", "per sample squared"),
        (r"\bmay not\b", "must not"), (r"\bcan ?not\b", "must not"), (r"\bmust never\b", "must not"), (r"\bshould never\b", "must not"), (r"\bshould not\b", "must not"), (r"\bshall not\b", "must not"), (r"\bis not allowed to\b", "must not"), (r"\bmustn't\b", "must not"), (r"\bshouldn't\b", "must not"),
        (r"\bshould\b", "must"), (r"\bshall\b", "must"), (r"\bhas to\b", "must"), (r"\bneeds to\b", "must"), (r"\bis required to\b", "must"), (r"\bought to\b", "must"),
        (r"\bin excess of\b", "more than"), (r"\bmore than or equal to\b", "at least"), (r"\bless than or equal to\b", "at most"), (r"\bgreater than or equal to\b", "at least"), (r"\b(?:the )?value of (s\d+)\b", r"\1"),
        (r"\bconsecutive samples\b", "samples"), (r"\bsamples in a row\b", "samples"), (r"\bsuccessive samples\b", "samples"), (r"\bsample points\b", "samples"), (r"\brows\b", "samples"), (r"\bvalues\b", "samples"), (r"\bdata points\b", "samples"), (r"\bpoints\b", "samples"),
        (r"\bwindow of (\d+) samples\b", r"\1-sample window"), (r"\b(\d+) samples? window\b", r"\1-sample window"), (r"\b(\d+) sample\b", r"\1-sample"),
        (r"\bnegative\b", "below 0"), (r"\bpositive\b", "above 0"), (r"\bnon-below 0\b", "at least 0"),
        (r"\bunits?\b", ""), (r"\bthe\b", ""), (r"\bthen ,", "then"), (r"\s*,\s*then\b", " then"), (r"\bof (s\d+) must\b", r"of \1 must"),
    ]
    for pat, rep in reps:
        t = re.sub(pat, rep, t)
    t = re.sub(r"[.;!]+\s*$", "", t.strip())
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _alias_lookup(catalog: list[SignalInfo]) -> dict[int, str]:
    out: dict[int, str] = {}
    for s in catalog:
        m = re.fullmatch(r"S(\d+)", s.alias)
        if m:
            out[int(m.group(1))] = s.alias
    return out


def _canon_alias(tok: str, lookup: dict[int, str]) -> Optional[str]:
    m = re.fullmatch(r"s(\d+)", tok.lower())
    if not m:
        return None
    n = int(m.group(1))
    if lookup:
        return lookup.get(n)
    return f"S{n:02d}"


def _role_phrases(catalog: list[SignalInfo]) -> dict[str, list[str]]:
    """phrase -> aliases, built from hypotheses and (weak) source names. Only used when allow_role_names."""
    out: dict[str, list[str]] = {}

    def add(phrase: Optional[str], alias: str) -> None:
        if not phrase:
            return
        p = re.sub(r"[_\-]+", " ", str(phrase).strip().lower())
        p = re.sub(r"\s+", " ", p)
        if len(p) < 3:
            return
        out.setdefault(p, [])
        if alias not in out[p]:
            out[p].append(alias)

    for s in catalog:
        add(s.instrument, s.alias)
        if s.instrument and s.unit_operation:
            add(f"{s.unit_operation} {s.instrument}", s.alias)
            add(f"{s.instrument} of {s.unit_operation}", s.alias)
        add(s.source_column, s.alias)
    return out


def _resolve_role_names(t: str, catalog: list[SignalInfo]) -> tuple[str, dict[str, list[str]], float]:
    """Replace role-name phrases by aliases (exact, then fuzzy). Returns (text, unresolved candidates, penalty)."""
    phrases = _role_phrases(catalog)
    cands: dict[str, list[str]] = {}
    penalty = 0.0
    if not phrases:
        return t, cands, penalty
    for p in sorted(phrases, key=len, reverse=True):
        if re.search(rf"\b{re.escape(p)}\b", t):
            al = phrases[p]
            if len(al) == 1:
                t = re.sub(rf"\b{re.escape(p)}\b", al[0], t)
                penalty = max(penalty, 0.3)
            else:
                cands[p] = al
                t = re.sub(rf"\b{re.escape(p)}\b", "S??", t)
    # fuzzy: any 1-3 word chunk before 'must'/'is'/'acceleration' that is not a number/alias
    for m in re.finditer(r"\b((?:[a-z][a-z_]+ ){0,2}[a-z][a-z_]+)\b(?= (?:must|is|acceleration|rate|rolling))", t):
        chunk = m.group(1)
        if chunk in ("if", "when", "whenever", "then", "of", "and", "rolling", "acceleration") or re.search(r"\bs\d+\b", chunk):
            continue
        best: list[tuple[float, str]] = []
        for p, al in phrases.items():
            r = SequenceMatcher(None, chunk, p).ratio()
            if r >= 0.75:
                for a in al:
                    best.append((r, a))
        best.sort(reverse=True)
        uniq = list(dict.fromkeys(a for _, a in best))
        if len(uniq) == 1 and best[0][0] >= 0.85:
            t = t.replace(chunk, uniq[0], 1)
            penalty = max(penalty, 0.4)
        elif uniq:
            cands[chunk] = uniq[:5]
            t = t.replace(chunk, "S??", 1)
    return t, cands, penalty


def _extract_group(t: str) -> tuple[str, Optional[str]]:
    m = re.search(r"\b(?:in|for|within|during|on) (?:group|run|batch|segment) ([\w\-]+)\b", t)
    if not m:
        return t, None
    return (t[: m.start()] + t[m.end():]).strip(), m.group(1)


def _op_from_words(w: str, negated: bool = False) -> str:
    w = w.strip()
    if w in (">", ">="):
        op = w
    elif w in ("<", "<="):
        op = w
    elif re.fullmatch(_ABOVE, w):
        op = ">=" if w in ("at least", "no less than", "not below") else ">"
    elif re.fullmatch(_BELOW, w):
        op = "<=" if w in ("at most", "no more than", "not above", "not exceed") else "<"
    elif w in ("equal to", "=", "==", "equals"):
        op = "=="
    else:
        op = ">"
    if negated:
        op = {">": "<=", ">=": "<", "<": ">=", "<=": ">", "==": "!=", "!=": "=="}[op]
    return op


def _grammar(t: str) -> Optional[tuple[dict[str, Any], str]]:
    """Return (spec, pattern_name) for the normalized text, or None. Signals appear as s\\d+ / S\\d+ or S?? tokens."""
    S = r"([sS]\d+|S\?\?)"
    N = f"({NUM})"
    pats: list[tuple[str, str, Any]] = [
        ("cross_signal", rf"^(?:if|when|whenever) {S} (?:is |are |goes |gets |becomes |stays |remains )?({_ABOVE}|{_BELOW}|equal to|=|==) {N}(?:,? then|,) {S} must (not )?(?:{_BE} )?(not )?({_ABOVE}|{_BELOW}|equal to|=|==) {N}$",
         lambda m: {"type": "cross_signal", "if": {"signal": m[1], "op": _op_from_words(m[2]), "value": float(m[3])}, "then": {"signal": m[4], "op": _op_from_words(m[7], negated=bool(m[5] or m[6])), "value": float(m[8])}}),
        ("cross_signal", rf"^(?:if|when|whenever) {S} (>=|<=|>|<|=|==) {N}(?:,? then|,) {S} (>=|<=|>|<|=|==) {N}$",
         lambda m: {"type": "cross_signal", "if": {"signal": m[1], "op": _op_from_words(m[2]), "value": float(m[3])}, "then": {"signal": m[4], "op": _op_from_words(m[5]), "value": float(m[6])}}),
        ("drift", rf"^{S} must not (?:drift|change|move|vary|wander|shift) (?:by )?more than {N} (?:over|within|in|during|across) (?:any |a |an |one )?{N}-sample(?:s)? window$",
         lambda m: {"type": "drift", "signal": m[1], "window": int(float(m[3])), "max_abs_change": float(m[2])}),
        ("drift", rf"^(?:drift|change|range) (?:of|in) {S} (?:over|within|in) (?:any |a |an |one )?{N}-sample(?:s)? window must (?:not exceed|{_BE} (?:{_BELOW})) {N}$",
         lambda m: {"type": "drift", "signal": m[1], "window": int(float(m[2])), "max_abs_change": float(m[3])}),
        ("rolling_stat", rf"^rolling {N}-sample (mean|std|min|max) of {S} must (?:{_BE} )?(?:(not) )?({_ABOVE}|{_BELOW}|>=|<=|>|<) {N}$",
         lambda m: {"type": "rolling_stat", "signal": m[3], "stat": m[2], "window": int(float(m[1])), "op": _op_from_words(m[5], negated=bool(m[4])), "value": float(m[6])}),
        ("rolling_stat", rf"^(?:rolling )?(mean|std|min|max) of {S} over (?:any |a |an )?{N}-sample(?:s)? ?(?:window)? ?must (?:{_BE} )?(?:(not) )?({_ABOVE}|{_BELOW}|>=|<=|>|<) {N}$",
         lambda m: {"type": "rolling_stat", "signal": m[2], "stat": m[1], "window": int(float(m[3])), "op": _op_from_words(m[5], negated=bool(m[4])), "value": float(m[6])}),
        ("rolling_stat", rf"^{S} rolling (mean|std|min|max) over {N} samples must (?:{_BE} )?(?:(not) )?({_ABOVE}|{_BELOW}|>=|<=|>|<) {N}$",
         lambda m: {"type": "rolling_stat", "signal": m[1], "stat": m[2], "window": int(float(m[3])), "op": _op_from_words(m[5], negated=bool(m[4])), "value": float(m[6])}),
        ("rolling_stat", rf"^{N}-sample rolling (mean|std|min|max) of {S} must (?:{_BE} )?(?:(not) )?({_ABOVE}|{_BELOW}|>=|<=|>|<) {N}$",
         lambda m: {"type": "rolling_stat", "signal": m[3], "stat": m[2], "window": int(float(m[1])), "op": _op_from_words(m[5], negated=bool(m[4])), "value": float(m[6])}),
        ("acceleration", rf"^(?:acceleration|second[- ]order change|second difference) of {S} must (?:not exceed|not (?:be|go) (?:{_ABOVE})|{_BE} (?:{_BELOW})|{_BE} within) {N}(?: per sample squared)?$",
         lambda m: {"type": "acceleration", "signal": m[1], "max_abs_second_diff": float(m[2])}),
        ("acceleration", rf"^{S} (?:acceleration|second[- ]order change|second difference) must (?:not exceed|not (?:be|go) (?:{_ABOVE})|{_BE} (?:{_BELOW})|{_BE} within) {N}(?: per sample squared)?$",
         lambda m: {"type": "acceleration", "signal": m[1], "max_abs_second_diff": float(m[2])}),
        ("acceleration", rf"^{S} must not accelerate (?:by )?more than {N}(?: per sample squared)?$",
         lambda m: {"type": "acceleration", "signal": m[1], "max_abs_second_diff": float(m[2])}),
        ("rate_of_change", rf"^{S} must not (?:change|move|vary|jump|rise|fall|increase|decrease|drop|climb|step) (?:by )?more than {N} (?:per|in|over|within|every) (?:(\d+) |one |a |an |each |single )?samples?$",
         lambda m: {"type": "rate_of_change", "signal": m[1], "max_abs_delta": float(m[2]), "per_samples": int(m[3]) if m[3] else 1}),
        ("rate_of_change", rf"^(?:rate of change|change rate|slope|delta) of {S} must (?:not exceed|{_BE} (?:{_BELOW})) {N}(?: (?:per|over) (?:(\d+) |one |a |an |each )?samples?)?$",
         lambda m: {"type": "rate_of_change", "signal": m[1], "max_abs_delta": float(m[2]), "per_samples": int(m[3]) if m[3] else 1}),
        ("rate_of_change", rf"^{S} (?:rate of change|change rate|slope|delta) must (?:not exceed|{_BE} (?:{_BELOW})) {N}(?: (?:per|over) (?:(\d+) |one |a |an |each )?samples?)?$",
         lambda m: {"type": "rate_of_change", "signal": m[1], "max_abs_delta": float(m[2]), "per_samples": int(m[3]) if m[3] else 1}),
        ("missing", rf"^{S} must not be (?:missing|absent|empty|null|nan|unavailable|blank) for more than {N} samples?$",
         lambda m: {"type": "missing", "signal": m[1], "max_consecutive": int(float(m[2]))}),
        ("missing", rf"^{S} must not have more than {N} (?:missing|absent|empty|null|nan|blank) samples?(?: in a row)?$",
         lambda m: {"type": "missing", "signal": m[1], "max_consecutive": int(float(m[2]))}),
        ("missing", rf"^(?:gaps|missing samples|missing stretches) in {S} must not (?:exceed|be longer than|last longer than) {N} samples?$",
         lambda m: {"type": "missing", "signal": m[1], "max_consecutive": int(float(m[2]))}),
        ("stuck", rf"^{S} must not (?:stay|remain|be|keep|read) (?:constant|frozen|stuck|flat|unchanged|same|identical|(?:exactly )?(?:the )?same(?: value)?) for (?:more than |longer than )?{N} samples?$",
         lambda m: {"type": "stuck", "signal": m[1], "max_constant_samples": int(float(m[2]))}),
        ("stuck", rf"^{S} must change at least (?:once )?every {N} samples?$",
         lambda m: {"type": "stuck", "signal": m[1], "max_constant_samples": int(float(m[2]))}),
        ("stuck", rf"^{S} must not be stuck (?:for )?(?:more than |longer than )?{N} samples?$",
         lambda m: {"type": "stuck", "signal": m[1], "max_constant_samples": int(float(m[2]))}),
        ("duration", rf"^{S} must not (?:{_BE} |go |exceed |be )?(?:(above|over|higher than|greater than|more than)|(below|under|lower than|less than)) {N} for (?:more than |longer than )?{N} samples?$",
         lambda m: {"type": "duration", "signal": m[1], "condition": {"op": "above" if m[2] else "below", "value": float(m[4])}, "min_samples": int(float(m[5])) + 1}),
        ("duration", rf"^{S} must not exceed {N} for (?:more than |longer than )?{N} samples?$",
         lambda m: {"type": "duration", "signal": m[1], "condition": {"op": "above", "value": float(m[2])}, "min_samples": int(float(m[3])) + 1}),
        ("duration", rf"^{S} must not (?:{_BE} )?between {N} and {N} for (?:more than |longer than )?{N} samples?$",
         lambda m: {"type": "duration", "signal": m[1], "condition": {"op": "between", "min": float(m[2]), "max": float(m[3])}, "min_samples": int(float(m[4])) + 1}),
        ("range", rf"^{S} must (?:{_BE} )?(?:between|within|in (?:the )?range|from) {N} (?:and|to|-|\.\.|up to) {N}$",
         lambda m: {"type": "range", "signal": m[1], "min": float(m[2]), "max": float(m[3])}),
        ("range", rf"^{S} must not (?:exceed|go above|rise above|be above|go over|be over|be higher than|be greater than|be more than|be larger than|surpass) {N}$",
         lambda m: {"type": "range", "signal": m[1], "max": float(m[2])}),
        ("range", rf"^{S} must not (?:drop|fall|go|dip|be|sink) below {N}$",
         lambda m: {"type": "range", "signal": m[1], "min": float(m[2])}),
        ("range", rf"^{S} must not be (?:less than|lower than|under|smaller than) {N}$",
         lambda m: {"type": "range", "signal": m[1], "min": float(m[2])}),
        ("range", rf"^{S} must (?:{_BE} )?({_ABOVE}) {N}$",
         lambda m: {"type": "range", "signal": m[1], "min": float(m[3])}),
        ("range", rf"^{S} must (?:{_BE} )?({_BELOW}) {N}$",
         lambda m: {"type": "range", "signal": m[1], "max": float(m[3])}),
        ("range", rf"^{S} must (?:{_BE} )?({_ABOVE}) {N} and (?:{_BE} )?({_BELOW}) {N}$",
         lambda m: {"type": "range", "signal": m[1], "min": float(m[3]), "max": float(m[5])}),
        ("range", rf"^{S} must (?:{_BE} )?({_BELOW}) {N} and (?:{_BE} )?({_ABOVE}) {N}$",
         lambda m: {"type": "range", "signal": m[1], "max": float(m[3]), "min": float(m[5])}),
        ("range", rf"^{S} (>=|>) {N}$", lambda m: {"type": "range", "signal": m[1], "min": float(m[3])}),
        ("range", rf"^{S} (<=|<) {N}$", lambda m: {"type": "range", "signal": m[1], "max": float(m[3])}),
        ("range", rf"^{N} (<=|<) {S} (<=|<) {N}$", lambda m: {"type": "range", "signal": m[3], "min": float(m[1]), "max": float(m[5])}),
        ("range", rf"^{S} must (?:{_BE} )?(?:in|inside|within) \[?{N},? ?{N}\]?$", lambda m: {"type": "range", "signal": m[1], "min": float(m[2]), "max": float(m[3])}),
    ]
    for name, pat, build in pats:
        m = re.match(pat, t)
        if m:
            try:
                return build(m), name
            except (ValueError, IndexError):
                continue
    return None


def parse_rule_text_detailed(text: str, signal_catalog: list[SignalInfo], allow_role_names: bool = False) -> ParseResult:
    t = _normalize(text)
    t, group = _extract_group(t)
    cands: dict[str, list[str]] = {}
    penalty = 0.0
    if allow_role_names:
        t, cands, penalty = _resolve_role_names(t, signal_catalog)
    g = _grammar(t)
    if g is None:
        return ParseResult(error=f"no rule pattern matched: '{t}'", candidates=cands)
    spec, pattern = g
    lookup = _alias_lookup(signal_catalog)
    unresolved: list[str] = []

    def fix(node: dict[str, Any]) -> None:
        for k, v in list(node.items()):
            if isinstance(v, dict):
                fix(v)
            elif k == "signal":
                if v == "S??":
                    unresolved.append(v)
                    continue
                a = _canon_alias(str(v), lookup)
                if a is None:
                    unresolved.append(str(v).upper())
                else:
                    node[k] = a

    fix(spec)
    if group:
        spec["group"] = group
    if unresolved or cands:
        missing = [u for u in unresolved if u != "S??"]
        msg = "ambiguous signal reference(s): " + "; ".join(f"'{p}' could be {', '.join(a)}" for p, a in cands.items()) if cands else ""
        if missing:
            msg = (msg + "; " if msg else "") + "unknown signal(s): " + ", ".join(missing)
        return ParseResult(spec=None, confidence=0.0, explanation=msg, candidates=cands, error=msg)
    ok, errs, norm = validate_compiled(spec, {s.alias for s in signal_catalog} if signal_catalog else None)
    if not ok:
        return ParseResult(error="; ".join(errs), candidates=cands)
    conf = max(0.5, 0.95 - penalty)
    return ParseResult(spec=norm, confidence=conf, explanation=explain_rule(norm) + f" (parsed by the template grammar as '{pattern}')", candidates=cands)


def parse_rule_text(text: str, signal_catalog: list[SignalInfo], allow_role_names: bool = False) -> Optional[dict[str, Any]]:
    """Compiled spec for a plain-language rule, or None when the grammar cannot parse it unambiguously."""
    return parse_rule_text_detailed(text, signal_catalog, allow_role_names).spec


# ================================================================== compile (template first, LLM optional)
def catalog_payload(catalog: list[SignalInfo], stats: Optional[dict[str, dict[str, Any]]] = None) -> list[dict[str, Any]]:
    """Aggregates only: what the rule compiler (local or external) may see about the signals. The scale of a signal
    is given by q01 / median / q99; a minimum or maximum is one single reading and is removed by the egress guard."""
    out = []
    for s in catalog:
        if s.excluded:
            continue
        st = (stats or {}).get(s.alias, {})
        fp = s.fingerprint or {}
        out.append({"id": s.alias, "role": s.role, "instrument": s.instrument, "unit_operation": s.unit_operation, "units": s.units, "q01": st.get("q01", fp.get("q01")), "median": st.get("median", fp.get("q50")), "q99": st.get("q99", fp.get("q99"))})
    return out


def _next_rule_id(rules: list[Rule]) -> str:
    n = 0
    for r in rules:
        m = re.search(r"(\d+)$", r.id)
        if m:
            n = max(n, int(m.group(1)))
    return f"RULE-{n + 1:03d}"


def _save_rules(ws: Any, rules: list[Rule]) -> None:
    ws.write_json("rules", [r.model_dump() for r in rules])


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and "rule" in obj and isinstance(obj["rule"], dict):
        obj = obj["rule"]
    return obj if isinstance(obj, dict) else None


def compile_rule(ws: Any, settings: Any, text: str, author: str = "human", persist: bool = True) -> Rule:
    """Template grammar first; LLM (tpm.llm.complete, task 'rule_compile') only when the grammar fails."""
    catalog = load_catalog(ws)
    aliases = {s.alias for s in catalog}
    rules = ws.rules()
    rid = _next_rule_id(rules)
    allow_roles = bool(settings.rules.allow_role_names)
    pr = parse_rule_text_detailed(text, catalog, allow_roles)
    rule = Rule(id=rid, text=text.strip(), author=author, status="draft")
    ev_ids: list[str] = []
    if pr.spec is not None:
        rule.compiled = pr.spec
        rule.compile_source = "template"
        rule.compile_confidence = pr.confidence
        rule.compile_explanation = pr.explanation
        ev = ws.evidence.add("rule_compile", f"{rid}: template grammar compiled '{text.strip()}' into a {pr.spec['type']} check on {', '.join(rule_signals(pr.spec))}", signals=rule_signals(pr.spec), values={"spec": pr.spec, "confidence": pr.confidence}, computed_by="quality.rules.parse_rule_text")
        ev_ids.append(ev.id)
    else:
        # the closed rule schema travels through schema= (system prompt), not in the payload: the payload is data for the egress guard
        payload = {"rule_text": text.strip(), "signal_catalog": catalog_payload(catalog, ws.read_json("quality_stats.json", {}).get("stats") if ws.exists("quality_stats.json") else None), "template_parser_error": pr.error, "candidates": pr.candidates}
        res = None
        try:
            from ..llm import complete

            res = complete("rule_compile", payload, purpose="compile operating rule into the closed JSON rule schema", ws=ws, settings=settings, schema=RULE_JSON_SCHEMA)
        except Exception as e:  # never break on the LLM layer
            res = None
            pr.error = f"{pr.error}; llm error: {e}"
        spec = None
        llm_reject = ""
        if res is not None and res.ok:
            spec = res.data if isinstance(res.data, dict) else _extract_json(res.text)
        if spec is not None:
            ok, errs, norm = validate_compiled(spec, aliases or None)
            if ok and norm is not None:
                rule.compiled = norm
                rule.compile_source = res.source or "llm-local"
                rule.compile_confidence = 0.7
                rule.compile_explanation = explain_rule(norm) + f" (compiled by {rule.compile_source}; validated against the closed schema)"
                ev = ws.evidence.add("rule_compile", f"{rid}: {rule.compile_source} compiled '{text.strip()}' into a {norm['type']} check on {', '.join(rule_signals(norm))}", signals=rule_signals(norm), values={"spec": norm, "confidence": 0.7, "route": res.route, "ledger_id": res.ledger_id}, computed_by="quality.rules.compile_rule")
                ev_ids.append(ev.id)
            else:
                llm_reject = "the model proposed a rule that does not fit the closed schema (" + "; ".join(errs) + ")"
        if rule.compiled is None:
            why = pr.error or "no parse"
            if llm_reject:
                why += "; " + llm_reject
            elif res is not None and not res.ok:
                why += f"; no language model available ({res.error})"
            hint = " Try the form '<signal> must stay between <a> and <b>', '<signal> must not change by more than <x> per sample', 'if <signal> is above <x> then <signal> must be above <y>', ..."
            if pr.candidates:
                hint = " Please confirm the signal: " + "; ".join(f"'{p}' -> {', '.join(a)}" for p, a in pr.candidates.items())
            rule.compile_explanation = f"Could not compile this rule automatically ({why}).{hint}"
            rule.compile_confidence = 0.0
    inf = ws.inferences.add(subject=rid, claim=rule.compile_explanation[:300], status="inferred" if rule.compiled is not None else "uncertain", confidence=rule.compile_confidence, evidence_ids=ev_ids, reasoning=f"rule text: {text.strip()}", source=rule.compile_source if rule.compiled is not None else "code", stage="quality")
    rule.inference_ids = [inf.id]
    if persist:
        rules.append(rule)
        _save_rules(ws, rules)
    ws.log.record("system:quality", "rule", "rule", rid, {"text": rule.text, "status": rule.status, "compiled": rule.compiled, "compile_source": rule.compile_source, "confidence": rule.compile_confidence, "candidates": pr.candidates}, ev_ids)
    return rule


# ================================================================== lifecycle
def apply_override(ws: Any, settings: Any, decision: Any) -> dict[str, Any]:
    """Human decision on a rule: approve | reject | edit | retire | activate (object_type 'rule')."""
    rules = ws.rules()
    rule = next((r for r in rules if r.id == decision.object_id), None)
    if rule is None:
        return {"error": f"unknown rule {decision.object_id}"}
    action = (decision.action or "").lower()
    actor = f"human:{getattr(decision, 'actor_name', 'unknown')}({getattr(decision, 'role', '?')})"
    new_value = getattr(decision, "new_value", None) or {}
    effect: dict[str, Any] = {"rule_id": rule.id, "previous_status": rule.status}
    if action in ("approve", "approve_rule", "accept", "activate"):
        if rule.compiled is None:
            effect["error"] = "rule has no compiled form; edit it first"
        else:
            rule.status = "active" if (settings.rules.auto_activate_on_approve or action == "activate") else "approved"
    elif action in ("reject", "reject_rule", "dismiss"):
        rule.status = "rejected"
    elif action in ("retire", "deactivate"):
        rule.status = "retired"
    elif action in ("edit", "override", "update"):
        if isinstance(new_value.get("compiled"), dict):
            ok, errs, norm = validate_compiled(new_value["compiled"], {s.alias for s in load_catalog(ws)} or None)
            if not ok:
                effect["error"] = "invalid compiled rule: " + "; ".join(errs)
            else:
                rule.compiled = norm
                rule.compile_source = "human"
                rule.compile_confidence = 1.0
                rule.compile_explanation = explain_rule(norm) + " (edited by a human)"
                rule.status = "draft"
                if new_value.get("text"):
                    rule.text = str(new_value["text"])
        elif new_value.get("text"):
            fresh = compile_rule(ws, settings, str(new_value["text"]), author=rule.author, persist=False)
            rule.text, rule.compiled, rule.compile_source = fresh.text, fresh.compiled, fresh.compile_source
            rule.compile_confidence, rule.compile_explanation, rule.inference_ids = fresh.compile_confidence, fresh.compile_explanation, fresh.inference_ids
            rule.status = "draft"
        else:
            effect["error"] = "edit needs new_value.text or new_value.compiled"
    else:
        effect["error"] = f"unknown rule action {action!r}"
    rule.updated_at = now_iso()
    _save_rules(ws, rules)
    effect["status"] = rule.status
    effect["rule"] = rule.model_dump()
    ws.log.record(actor, f"rule_{action}", "rule", rule.id, {"status": rule.status, "note": getattr(decision, "note", None), "error": effect.get("error")})
    ws.log.record("system:quality", "rule_status", "rule", rule.id, {"status": rule.status, "by": actor})
    return effect


# ================================================================== files
def load_rules_file(path: str | Path) -> list[str]:
    """One rule per non-empty line; '#' comments, Markdown headings/bullets and YAML '- ' lists are tolerated."""
    p = Path(path)
    out: list[str] = []
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml

            data = yaml.safe_load(text)
            items = data.get("rules", data) if isinstance(data, dict) else data
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, str) and it.strip():
                        out.append(it.strip())
                    elif isinstance(it, dict) and it.get("text"):
                        out.append(str(it["text"]).strip())
                return out
        except Exception:
            pass
    in_code = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if not line or line.startswith("#") or line.startswith("<!--") or line.startswith("//"):
            continue
        line = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)
        line = line.strip("`").strip()
        if not line:
            continue
        # a rule names a signal or at least states a number with an obligation; prose lines are documentation
        has_signal = bool(re.search(r"\b[sS]\d+\b", line))
        has_number = bool(re.search(r"\d", line))
        has_obligation = bool(re.search(r"\b(must|should|shall|may not|cannot|if|when|whenever)\b", line, re.I) or re.search(r"[<>]=?", line))
        if has_obligation and has_number and (has_signal or not re.search(r"\b(rules?|catalog|approved|compiled)\b", line, re.I)):
            out.append(line)
    return out


def add_rules_from_file(ws: Any, settings: Any, path: str | Path, author: str = "file", auto_status: Optional[str] = None) -> list[Rule]:
    """Compile every rule line of a file. ``auto_status`` may set 'active' for trusted files (logged)."""
    added: list[Rule] = []
    known = {" ".join(r.text.split()).lower(): r for r in ws.rules()}
    for line in load_rules_file(path):
        same = known.get(" ".join(line.split()).lower())
        if same is not None:  # the same file loaded again (a rerun, `run --rules` then showcase): keep the rule, its id and its status
            ws.log.record("system:quality", "rule_reused", "rule", same.id, {"status": same.status, "by": f"file:{Path(path).name}"})
            added.append(same)
            continue
        r = compile_rule(ws, settings, line, author=author)
        if auto_status and r.compiled is not None:
            rules = ws.rules()
            for rr in rules:
                if rr.id == r.id:
                    rr.status = auto_status
                    rr.updated_at = now_iso()
                    r = rr
            _save_rules(ws, rules)
            ws.log.record("system:quality", "rule_status", "rule", r.id, {"status": auto_status, "by": f"file:{Path(path).name}"})
        added.append(r)
        known[" ".join(r.text.split()).lower()] = r
    ws.log.record("system:quality", "rules_loaded", "rules", str(path), {"n": len(added), "n_compiled": sum(r.compiled is not None for r in added)})
    return added


# ================================================================== run over batches
def run_active_rules(ws: Any, settings: Any, batches: list[dict[str, Any]], rules: Optional[list[Rule]] = None, persist: bool = True, deadline: Optional[float] = None) -> list[CheckResult]:
    from .batches import load_batch_frame

    rules = [r for r in (rules if rules is not None else ws.rules()) if r.status == "active" and r.compiled]
    if not rules or not batches:
        return []
    catalog = load_catalog(ws)
    alias_to_col = {s.alias: s.column for s in catalog}
    needed = sorted({a for r in rules for a in rule_signals(r.compiled)})
    cols = [alias_to_col.get(a, a) for a in needed]
    out: list[CheckResult] = []
    import time as _time

    for b in batches:
        if deadline is not None and _time.time() > deadline:
            ws.log.record("system:quality", "rules_skipped", "batch", b["batch_id"], {"reason": "time budget exhausted"})
            break
        df = load_batch_frame(ws, b, columns=cols)
        colmap = {a: alias_to_col.get(a, a) for a in needed}
        for r in rules:
            try:
                viol = run_rule(r, df, colmap)
            except KeyError as e:
                ws.log.record("system:quality", "rule_error", "rule", r.id, {"batch_id": b["batch_id"], "error": str(e)})
                continue
            res = rule_check_result(ws, r, viol, b["batch_id"], len(df))
            out.append(res)
            if persist:
                ws.append_jsonl("checks", res)
        del df
    ws.log.record("system:quality", "rules_evaluated", "rules", "rules.json", {"n_rules": len(rules), "n_batches": len(batches), "n_fail": sum(c.status == "fail" for c in out)}, [e for c in out for e in c.evidence_ids][:50])
    return out


def run_rules_on_frame(ws: Any, settings: Any, df: pd.DataFrame, batch_id: str, rules: Optional[list[Rule]] = None, persist: bool = True) -> list[CheckResult]:
    """Stream path: active rules on an in-memory batch (alias or original column names)."""
    rules = [r for r in (rules if rules is not None else ws.rules()) if r.status == "active" and r.compiled]
    if not rules:
        return []
    catalog = load_catalog(ws)
    colmap = resolve_columns(df.columns, catalog)
    out: list[CheckResult] = []
    for r in rules:
        try:
            viol = run_rule(r, df, colmap)
        except KeyError as e:
            ws.log.record("system:quality", "rule_error", "rule", r.id, {"batch_id": batch_id, "error": str(e)})
            continue
        res = rule_check_result(ws, r, viol, batch_id, len(df))
        out.append(res)
        if persist:
            ws.append_jsonl("checks", res)
    return out
