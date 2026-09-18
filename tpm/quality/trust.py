"""Trust verdict per batch (decision 28): continue but flag, lower confidence, say it in plain language.

trust_score in [0, 1]:
    signal_penalty = 0.7 * min(1, (sum of max fail severity per signal / n_signals) / critical_signal_fraction)
    batch_penalty  = 0.3 * max severity of batch-level fails (duplicates, gaps, ordering, empty rows)
    warn_penalty   = up to 0.1 for warnings
    trust_score    = 1 - signal_penalty - batch_penalty - warn_penalty
trusted is False when trust_score < settings.quality.trust_fail_threshold. A single bad signal keeps the batch
trusted but lists the signal in untrusted_signals so detection can exclude it.

Operating-rule violations (category "rule") are process conditions, not data problems: they never lower trust,
except rules whose type is itself a data-quality check ("missing", "stuck"). Otherwise detection would be told to
ignore exactly the signal that is misbehaving.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from ..contracts import CheckResult, TrustVerdict

BATCH_LEVEL = {"duplicate_rows", "duplicate_key", "gap", "out_of_order", "duplicate_timestamp", "irregular_sampling", "empty_rows"}
UNTRUST_TYPES = {"missing", "dropout", "out_of_range", "impossible_value", "unit_shift", "stuck", "saturation", "sign_violation", "relation_break", "stale", "quantization_change"}
DQ_RULE_TYPES = {"missing", "stuck"}


def counts_for_trust(c: CheckResult) -> bool:
    """Data-quality checks count; operating rules only when their type is a data-quality kind."""
    if c.status == "pass":
        return False
    if c.category == "rule":
        return (c.values or {}).get("rule_type") in DQ_RULE_TYPES
    return True


def _why(c: CheckResult) -> str:
    v = c.values or {}
    t = c.check_type
    if t == "stuck":
        return f"frozen for {v.get('longest_run', '?')} samples"
    if t == "dropout":
        return "no values at all"
    if t == "missing":
        return f"{float(v.get('missing_rate', 0)):.0%} missing"
    if t == "out_of_range":
        return f"{v.get('n', '?')} values far outside the usual range"
    if t == "unit_shift":
        return f"scale change x10^{v.get('k', ['?'])[0] if isinstance(v.get('k'), list) and v.get('k') else '?'}"
    if t == "impossible_value":
        return "impossible values"
    if t == "saturation":
        return f"clipped at its {v.get('at', 'limit')} for {v.get('longest_run', '?')} samples"
    if t == "sign_violation":
        return "unexpected negative values"
    if t == "relation_break":
        return "lost its usual relation to a redundant signal"
    if t == "stale":
        return f"not updated for {v.get('longest_run', '?')} samples"
    if t == "quantization_change":
        return "changed resolution"
    if t.startswith("rule:"):
        return f"violates {c.rule_id}"
    return t.replace("_", " ")


def compute_trust(settings: Any, batch_id: str, checks: Iterable[CheckResult], n_signals: Optional[int] = None) -> dict[str, Any]:
    """Pure trust computation (no evidence, no persistence): the assessor uses it for what-if scenarios."""
    q = settings.quality
    checks = [c for c in checks if c.batch_id == batch_id]
    sig_sev: dict[str, float] = {}
    warn_sigs: set[str] = set()
    batch_sev = 0.0
    for c in checks:
        if not counts_for_trust(c):
            continue
        is_batch = c.check_type in BATCH_LEVEL or not c.signals
        if c.status == "fail":
            if is_batch:
                batch_sev = max(batch_sev, c.severity)
            else:
                for s in c.signals:
                    sig_sev[s] = max(sig_sev.get(s, 0.0), c.severity)
        elif not is_batch:
            warn_sigs.update(c.signals)
    if n_signals is None:
        n_signals = max(1, len({s for c in checks for s in c.signals}))
    n_signals = max(1, int(n_signals))
    share = sum(sig_sev.values()) / n_signals
    signal_penalty = 0.7 * min(1.0, share / max(float(q.critical_signal_fraction), 1e-6))
    batch_penalty = 0.3 * min(1.0, batch_sev)
    warn_penalty = min(0.1, 0.1 * len(warn_sigs - set(sig_sev)) / n_signals)
    score = max(0.0, min(1.0, 1.0 - signal_penalty - batch_penalty - warn_penalty))
    return {"trust_score": score, "trusted": score >= float(q.trust_fail_threshold), "untrusted_signals": sorted(sig_sev, key=lambda s: (-sig_sev[s], s)), "n_signals": n_signals}


def trust_verdict(ws: Any, settings: Any, batch_id: str, checks: Iterable[CheckResult], n_signals: Optional[int] = None, persist: bool = True) -> TrustVerdict:
    q = settings.quality
    checks = [c for c in checks if c.batch_id == batch_id]
    sig_sev: dict[str, float] = {}
    sig_why: dict[str, list[str]] = {}
    warn_sigs: set[str] = set()
    batch_sev = 0.0
    batch_reasons: list[str] = []
    for c in checks:
        if not counts_for_trust(c):
            continue
        is_batch = c.check_type in BATCH_LEVEL or not c.signals
        if c.status == "fail":
            if is_batch:
                batch_sev = max(batch_sev, c.severity)
                batch_reasons.append(c.statement)
            else:
                for s in c.signals:
                    sig_sev[s] = max(sig_sev.get(s, 0.0), c.severity)
                    sig_why.setdefault(s, []).append(_why(c))
        else:
            if not is_batch:
                warn_sigs.update(c.signals)
    if n_signals is None:
        seen = {s for c in checks for s in c.signals}
        n_signals = max(1, len(seen))
    n_signals = max(1, int(n_signals))
    share = sum(sig_sev.values()) / n_signals
    signal_penalty = 0.7 * min(1.0, share / max(float(q.critical_signal_fraction), 1e-6))
    batch_penalty = 0.3 * min(1.0, batch_sev)
    warn_penalty = min(0.1, 0.1 * len(warn_sigs - set(sig_sev)) / n_signals)
    score = max(0.0, min(1.0, 1.0 - signal_penalty - batch_penalty - warn_penalty))
    trusted = score >= float(q.trust_fail_threshold)
    untrusted = sorted(sig_sev, key=lambda s: (-sig_sev[s], s))
    reasons: list[str] = []
    for s in untrusted:
        reasons.append(f"{s}: " + "; ".join(dict.fromkeys(sig_why.get(s, []))))
    reasons.extend(batch_reasons)
    n_aff = len(untrusted)
    if untrusted:
        listed = ", ".join(f"{s} ({'; '.join(dict.fromkeys(sig_why.get(s, [])))})" for s in untrusted[:6])
        more = f" and {n_aff - 6} more" if n_aff > 6 else ""
        if trusted:
            statement = f"Data in batch {batch_id} is usable but cannot be trusted for signals {listed}{more}; these are excluded or down-weighted in detection."
        else:
            statement = f"Data in batch {batch_id} cannot be trusted: {n_aff} of {n_signals} signals are unreliable, e.g. {listed}{more}."
    elif batch_reasons:
        statement = (f"Data in batch {batch_id} has structural problems: " if trusted else f"Data in batch {batch_id} cannot be trusted: ") + batch_reasons[0]
    else:
        statement = f"Data in batch {batch_id} passed all baseline checks."
    if untrusted and batch_reasons:
        statement += " Also: " + batch_reasons[0]
    check_ids = [c.check_id for c in checks if counts_for_trust(c)]
    ev_ids = [e for c in checks if counts_for_trust(c) for e in c.evidence_ids]
    ev = ws.evidence.add("trust", f"Batch {batch_id}: trust score {score:.2f}, {n_aff}/{n_signals} signals unreliable, batch-level severity {batch_sev:.2f}", signals=untrusted[:20], values={"trust_score": round(score, 4), "share_weighted": round(share, 4), "n_signals": n_signals, "n_untrusted": n_aff, "batch_severity": round(batch_sev, 3), "signal_penalty": round(signal_penalty, 4), "batch_penalty": round(batch_penalty, 4), "warn_penalty": round(warn_penalty, 4)}, computed_by="quality.trust.trust_verdict", batch_id=batch_id)
    verdict = TrustVerdict(batch_id=batch_id, trusted=trusted, trust_score=round(score, 4), untrusted_signals=untrusted, reasons=reasons[:30], check_ids=check_ids, statement=statement)
    if persist:
        ws.append_jsonl("trust", verdict)
        ws.log.record("system:quality", "trust", "batch", batch_id, {"trusted": trusted, "trust_score": round(score, 4), "untrusted_signals": untrusted[:20]}, [ev.id] + ev_ids[:30])
    return verdict


def signal_trust_summary(verdicts: Iterable[TrustVerdict]) -> dict[str, dict[str, Any]]:
    """Per-signal: in how many batches it was untrusted (helper for detection / assessor)."""
    out: dict[str, dict[str, Any]] = {}
    n = 0
    for v in verdicts:
        n += 1
        for s in v.untrusted_signals:
            d = out.setdefault(s, {"n_batches_untrusted": 0, "batches": []})
            d["n_batches_untrusted"] += 1
            if len(d["batches"]) < 50:
                d["batches"].append(v.batch_id)
    for d in out.values():
        d["fraction"] = d["n_batches_untrusted"] / max(1, n)
    return out
