"""Plausible range per signal (round 6, review item 19): readings are tested directly against an explicit range.

Roles of the two range checks (a reading is reported by at most one of them):

* ``plausibility`` - the reading lies outside the range the signal can plausibly take at all: a sensor, conversion or
  logging error (validity; lowers trust like any single-signal data problem, exposure-weighted).
* ``out_of_range`` - the reading is inside the plausible range but unusually far from the signal's usual level
  (beyond ``quality.range_sigma`` robust sigma): rare but possible, a process excursion or a glitch.

How the plausible range [lo, hi] is derived, per bound, weakest to strongest source:

1. data: the normal part of the signal (1st to 99th percentile of all its readings) widened by three normal spans on
   each side (span = p99 - p1). Generous on purpose: a real process excursion, even a fault, stays inside; a unit
   error, a sentinel value (0, -9999, 65535) or a corrupted reading does not.
2. physical hints from the data's normal part, each with a small tolerance for noise and resolution:
   * non-negative: every normal reading (from the 1st percentile up) is >= 0, like a flow, level, concentration or
     speed -> nothing plausibly below 0;
   * percentage-like: the normal part lies within 0..100, uses a real part of that scale and sits at a bound (>= 1 %
     of the readings at 0 or at 100, the mark of a clamped valve position or percentage) or the signal is
     actuator-like -> nothing plausibly outside 0..100.
3. operator rules: an active or approved ``range`` rule for the signal (compiled limits reused as they are, the rule
   compiler is untouched) states what the operator considers allowed. Its band is always inside the plausible range
   (a reading the operator allows is plausible by definition), so plausibility never contradicts a rule; readings
   outside the operator's band but inside the plausible range are reported by the rule check itself (category rule).
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Optional

from ._common import fmt_num

SPAN_MARGIN = 3.0  # data-derived bounds: p1 - 3 spans .. p99 + 3 spans
TOL_SPAN_SHARE = 0.01  # physical bounds tolerate 1 % of the normal span (or of the 0..100 scale) as noise/offset
TOL_QSTEPS = 2.0  # ...and at least two recording steps
PCT_TOL = 1.0  # the normal part may exceed 0..100 by this much and still count as within the scale
PCT_MIN_SPREAD = 10.0  # a percentage-like signal uses at least this much of the 0..100 scale
PCT_AT_BOUND = 0.5  # p1 <= 0.5 or p99 >= 99.5: at least 1 % of the readings sit at a bound


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def rule_limits(rules: Iterable[Any]) -> dict[str, list[tuple[Optional[float], Optional[float], str]]]:
    """signal -> [(min, max, rule_id)] from active/approved compiled ``range`` rules without a group restriction."""
    out: dict[str, list[tuple[Optional[float], Optional[float], str]]] = {}
    for r in rules or []:
        status = getattr(r, "status", None) if not isinstance(r, dict) else r.get("status")
        spec = getattr(r, "compiled", None) if not isinstance(r, dict) else r.get("compiled")
        rid = getattr(r, "id", None) if not isinstance(r, dict) else r.get("id")
        if status not in ("active", "approved") or not isinstance(spec, dict) or spec.get("type") != "range" or spec.get("group") is not None:
            continue
        sig = spec.get("signal")
        lo, hi = _f(spec.get("min")), _f(spec.get("max"))
        if sig and (lo is not None or hi is not None):
            out.setdefault(str(sig), []).append((lo, hi, str(rid or "rule")))
    return out


def percentage_like(st: dict[str, Any], role: str = "unknown") -> bool:
    """Only the normal part (p1..p99) decides: min / max would include the very readings the range must judge."""
    q01, q99 = _f(st.get("q01")), _f(st.get("q99"))
    if q01 is None or q99 is None:
        return False
    if q01 < -PCT_TOL or q99 > 100.0 + PCT_TOL:
        return False
    if (q99 - q01) < PCT_MIN_SPREAD:
        return False
    return q01 <= PCT_AT_BOUND or q99 >= 100.0 - PCT_AT_BOUND or role == "actuator_like"


HINT_WORDS = {
    "sensor": {"readings": "readings", "pct": "a percentage or valve position", "nonneg": "a flow, level or concentration"},
    "records": {"readings": "entries", "pct": "a percentage", "nonneg": "a count, amount or price"},
}


def plausible_range(st: dict[str, Any], fingerprint: Optional[dict[str, Any]] = None, role: str = "unknown", limits: Optional[list[tuple[Optional[float], Optional[float], str]]] = None, wording: str = "sensor") -> Optional[dict[str, Any]]:
    """The plausible range of one signal with its derivation, or None when the signal has no usable spread."""
    fp = fingerprint or {}
    hw = HINT_WORDS.get(wording, HINT_WORDS["sensor"])
    q01, q99 = _f(st.get("q01")), _f(st.get("q99"))
    scale = _f(st.get("scale")) or 0.0
    if q01 is None or q99 is None:
        return None
    span = q99 - q01
    if span <= 0:
        span = 4.65 * scale  # quantized/near-constant middle: the robust spread stands in for the 1-99 % span
    if not span or span <= 0:
        return None
    qstep = _f(fp.get("quantization_step")) or _f(st.get("quantization_step")) or 0.0
    lo, hi = q01 - SPAN_MARGIN * span, q99 + SPAN_MARGIN * span
    lo_src = hi_src = "data"
    lo_why = hi_why = f"the normal {hw['readings']} lie between {fmt_num(q01)} and {fmt_num(q99)} (1st to 99th percentile), widened by three times that span"
    tol = max(TOL_SPAN_SHARE * span, TOL_QSTEPS * qstep)
    kind = None
    if percentage_like(st, role):
        kind = "percentage"
        tol_p = max(TOL_SPAN_SHARE * 100.0, TOL_QSTEPS * qstep)
        if -tol_p > lo:
            lo, lo_src = -tol_p, "percentage"
            lo_why = f"it behaves like {hw['pct']} (normal {hw['readings']} within 0..100, at least 1 % of them at a bound), so nothing below 0 is plausible (tolerance {fmt_num(tol_p)})"
        if 100.0 + tol_p < hi:
            hi, hi_src = 100.0 + tol_p, "percentage"
            hi_why = f"it behaves like {hw['pct']} (normal {hw['readings']} within 0..100, at least 1 % of them at a bound), so nothing above 100 is plausible (tolerance {fmt_num(tol_p)})"
    elif q01 >= 0.0 and -tol > lo:
        kind = "non_negative"
        lo, lo_src = -tol, "non_negative"
        lo_why = f"all its normal {hw['readings']} are >= 0 (a non-negative quantity such as {hw['nonneg']}), so nothing below 0 is plausible (tolerance {fmt_num(tol)})"
    rules_used: list[str] = []
    for rmin, rmax, rid in limits or []:
        if rmin is not None and rmin < lo:
            lo, lo_src = rmin, "rule"
            lo_why = f"operator rule {rid} allows {hw['readings']} down to {fmt_num(rmin)}"
            rules_used.append(rid)
        if rmax is not None and rmax > hi:
            hi, hi_src = rmax, "rule"
            hi_why = f"operator rule {rid} allows {hw['readings']} up to {fmt_num(rmax)}"
            rules_used.append(rid)
        if rid not in rules_used and (rmin is not None or rmax is not None):
            rules_used.append(rid)  # the rule's band lies inside the range already: recorded for traceability
    return {
        "lo": float(lo), "hi": float(hi), "lo_source": lo_src, "hi_source": hi_src, "lo_why": lo_why, "hi_why": hi_why,
        "hint": kind, "q01": q01, "q99": q99, "span": float(span), "tolerance": float(tol), "rules": sorted(set(rules_used)),
    }


def range_words(pr: dict[str, Any]) -> str:
    """One plain sentence on where a plausible range comes from."""
    if pr["lo_why"] == pr["hi_why"]:
        return f"The range comes from the data: {pr['lo_why']}."
    return f"Lower bound: {pr['lo_why']}. Upper bound: {pr['hi_why']}."


def plausible_ranges(catalog: Iterable[Any], stats: dict[str, dict[str, Any]], rules: Iterable[Any] = (), wording: str = "sensor") -> dict[str, dict[str, Any]]:
    """alias -> plausible range for every numeric signal with a usable spread."""
    lims = rule_limits(rules)
    out: dict[str, dict[str, Any]] = {}
    for s in catalog:
        if getattr(s, "role", "") == "constant":
            continue
        pr = plausible_range(stats.get(s.alias, {}) or {}, getattr(s, "fingerprint", None), getattr(s, "role", "unknown"), lims.get(s.alias), wording=wording)
        if pr is not None:
            out[s.alias] = pr
    return out
