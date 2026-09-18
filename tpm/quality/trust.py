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


def _exposure(c: CheckResult, batch_rows: Optional[int]) -> float:
    """Share of the batch rows a signal-level check actually affects (1.0 when unknown = whole batch)."""
    v = c.values or {}
    for k in ("fraction", "stuck_fraction", "missing_rate"):
        if isinstance(v.get(k), (int, float)):
            return float(min(1.0, max(0.0, v[k])))
    ev = v.get("events")
    if ev and batch_rows:
        try:
            span = sum(int(b) - int(a) + 1 for a, b in ev)
            return float(min(1.0, max(0.0, span / batch_rows)))
        except Exception:
            pass
    if isinstance(v.get("n"), (int, float)) and batch_rows:
        return float(min(1.0, max(0.0, v["n"] / batch_rows)))
    return 1.0


def _effective_severity(c: CheckResult, batch_rows: Optional[int]) -> float:
    """Severity weighted by exposure: a problem confined to a few rows of a big batch weighs little at batch level."""
    return float(c.severity) * (0.15 + 0.85 * _exposure(c, batch_rows))


LOCAL_MIN_SEVERITY = 0.35  # below this effective severity a failing signal check is row-scoped, not batch-wide
BATCH_MIN_EXPOSURE = 0.25  # a failing check touching >= this share of the batch rows is batch-wide regardless (smaller shares stay row-scoped)
MAX_LOCAL_ENTRIES = 400


def _batch_rows(ws: Any, batch_id: str) -> Optional[int]:
    try:
        b = ws.read_json("batches", None)
        items = b.get("batches") if isinstance(b, dict) else b
        for it in items or []:
            if str(it.get("batch_id")) == str(batch_id):
                return max(1, int(it.get("row_end", 0)) - int(it.get("row_start", 0)))
    except Exception:
        return None
    return None


def _local_entries(c: CheckResult) -> list[dict[str, Any]]:
    ev = (c.values or {}).get("events") or ([] if c.row_start is None else [(c.row_start, c.row_end if c.row_end is not None else c.row_start)])
    out = []
    for s in c.signals:
        for a, b in list(ev)[:20]:
            out.append({"signal": s, "row_start": int(a), "row_end": int(b), "check_type": c.check_type, "severity": round(float(c.severity), 3), "check_id": c.check_id})
    return out


def compute_trust(settings: Any, batch_id: str, checks: Iterable[CheckResult], n_signals: Optional[int] = None, n_rows: Optional[int] = None) -> dict[str, Any]:
    """Pure trust computation (no evidence, no persistence): the assessor uses it for what-if scenarios.
    Failing signal checks are weighted by the share of the batch they affect; problems confined to a few rows
    are row-scoped (local_untrusted) instead of marking the signal unreliable for the whole batch."""
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
                expo = _exposure(c, n_rows)
                eff = _effective_severity(c, n_rows)
                batch_wide = expo >= BATCH_MIN_EXPOSURE or eff >= LOCAL_MIN_SEVERITY
                for s in c.signals:
                    if batch_wide:
                        sig_sev[s] = max(sig_sev.get(s, 0.0), max(eff, float(c.severity) * (0.5 + 0.5 * expo)))
                    else:
                        warn_sigs.add(s)
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


def trust_verdict(ws: Any, settings: Any, batch_id: str, checks: Iterable[CheckResult], n_signals: Optional[int] = None, persist: bool = True, n_rows: Optional[int] = None) -> TrustVerdict:
    q = settings.quality
    checks = [c for c in checks if c.batch_id == batch_id]
    if n_rows is None:
        n_rows = _batch_rows(ws, batch_id)
    sig_sev: dict[str, float] = {}
    sig_why: dict[str, list[str]] = {}
    warn_sigs: set[str] = set()
    batch_sev = 0.0
    batch_reasons: list[str] = []
    local: list[dict[str, Any]] = []
    for c in checks:
        if not counts_for_trust(c):
            continue
        is_batch = c.check_type in BATCH_LEVEL or not c.signals
        if c.status == "fail":
            if is_batch:
                batch_sev = max(batch_sev, c.severity)
                batch_reasons.append(c.statement)
            else:
                expo = _exposure(c, n_rows)
                eff = _effective_severity(c, n_rows)
                batch_wide = expo >= BATCH_MIN_EXPOSURE or eff >= LOCAL_MIN_SEVERITY
                local.extend(_local_entries(c))
                for s in c.signals:
                    if batch_wide:
                        sig_sev[s] = max(sig_sev.get(s, 0.0), max(eff, float(c.severity) * (0.5 + 0.5 * expo)))
                        sig_why.setdefault(s, []).append(_why(c))
                    else:
                        warn_sigs.add(s)
        else:
            if not is_batch:
                warn_sigs.update(c.signals)
    local.sort(key=lambda e: -(e["row_end"] - e["row_start"]))
    local = local[:MAX_LOCAL_ENTRIES]
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
    elif local:
        statement = f"Data in batch {batch_id} is usable overall."
    else:
        statement = f"Data in batch {batch_id} passed all baseline checks."
    if untrusted and batch_reasons:
        statement += " Also: " + batch_reasons[0]
    check_ids = [c.check_id for c in checks if counts_for_trust(c)]
    ev_ids = [e for c in checks if counts_for_trust(c) for e in c.evidence_ids]
    ev = ws.evidence.add("trust", f"Batch {batch_id}: trust score {score:.2f}, {n_aff}/{n_signals} signals unreliable, batch-level severity {batch_sev:.2f}", signals=untrusted[:20], values={"trust_score": round(score, 4), "share_weighted": round(share, 4), "n_signals": n_signals, "n_untrusted": n_aff, "batch_severity": round(batch_sev, 3), "signal_penalty": round(signal_penalty, 4), "batch_penalty": round(batch_penalty, 4), "warn_penalty": round(warn_penalty, 4)}, computed_by="quality.trust.trust_verdict", batch_id=batch_id)
    if local and not untrusted:
        n_loc_sig = len({e["signal"] for e in local})
        statement += f" {n_loc_sig} signal(s) are unreliable only in specific rows (e.g. {local[0]['signal']} rows {local[0]['row_start']}-{local[0]['row_end']}: {local[0]['check_type'].replace('_', ' ')}); detection treats them as data problems there, not elsewhere."
    verdict = TrustVerdict(batch_id=batch_id, trusted=trusted, trust_score=round(score, 4), untrusted_signals=untrusted, local_untrusted=local, n_rows=n_rows, reasons=reasons[:30], check_ids=check_ids, statement=statement)
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
