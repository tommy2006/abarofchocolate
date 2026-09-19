"""Trust verdict per batch (decision 28): continue but flag, lower confidence, say it in plain language.

trust_score in [0, 1]:
    signal_penalty = 0.7 * min(1, (sum of max fail severity per signal / n_signals) / critical_signal_fraction)
    record_penalty = (1 - trust_fail_threshold) * min(1, record_share / RECORD_BATCH_SHARE)
    batch_penalty  = 0.3 * max severity of the other batch-level fails (time gaps, ordering, repeated timestamps)
    warn_penalty   = up to 0.1 for warnings
    trust_score    = 1 - signal_penalty - record_penalty - batch_penalty - warn_penalty
trusted is False when trust_score < settings.quality.trust_fail_threshold, and always when record_share reaches
RECORD_BATCH_SHARE; the statement then starts with "This data cannot be trusted as a whole".

Single-signal problems (a frozen, missing or implausible signal) are exposure-weighted: a failing check touching
>= BATCH_MIN_EXPOSURE (25 %) of the batch rows, or with an effective severity >= LOCAL_MIN_SEVERITY, marks the signal
unreliable for the whole batch (untrusted_signals, detection excludes it); smaller problems are row-scoped
(local_untrusted: the signal is unreliable only in those rows). A single bad signal keeps the batch trusted.

Record-level problems (round 6, review item 10) hit whole rows, i.e. every signal of the row at once: exact duplicate
rows, repeated (group, order) keys, rows frozen or missing in many signals at once (the grouped frozen_block /
missing_block checks) and empty rows. record_share = share of the batch rows they cover (sum over the findings,
capped at 1).
* Rows are untrusted for every signal (row-scoped: TrustVerdict.untrusted_rows) for a common-mode block or empty rows
  always, for duplicates / repeated keys from DUP_ROW_SCOPE_MIN = 2 % of the batch. Below 2 % a few repeated records
  (a retransmission) do not make the rows around them unreliable; from 2 % the logger or export copies records
  systematically and the copies are not independent measurements.
* The whole batch is untrusted from RECORD_BATCH_SHARE = 10 %: one row in ten is not a real, independent measurement,
  so every statistic, baseline and alarm computed on the batch is biased by at least that much. This is lower than
  BATCH_MIN_EXPOSURE (25 %, one signal) because a record-level problem hits all signals of a row at once.
* Members of a frozen / missing block covering >= BATCH_MIN_EXPOSURE of the batch are untrusted signals for the whole
  batch too (as their own stuck / missing check would have made them); smaller blocks are row-scoped per member.

Operating-rule violations (category "rule") are process conditions, not data problems: they never lower trust,
except rules whose type is itself a data-quality check ("missing", "stuck"). Otherwise detection would be told to
ignore exactly the signal that is misbehaving. "not_testable" checks count neither way.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from ..contracts import CheckResult, TrustVerdict
from ._common import WORDING, is_problem

BATCH_LEVEL = {"duplicate_rows", "duplicate_key", "gap", "out_of_order", "duplicate_timestamp", "irregular_sampling", "empty_rows"}
RECORD_LEVEL = {"duplicate_rows", "duplicate_key", "frozen_block", "missing_block", "empty_rows"}
UNTRUST_TYPES = {"missing", "dropout", "out_of_range", "plausibility", "impossible_value", "unit_shift", "stuck", "saturation", "sign_violation", "relation_break", "stale", "quantization_change", "quantization_block", "frozen_block", "missing_block"}
DQ_RULE_TYPES = {"missing", "stuck"}

LOCAL_MIN_SEVERITY = 0.35  # below this effective severity a failing signal check is row-scoped, not batch-wide
BATCH_MIN_EXPOSURE = 0.25  # a failing check touching >= this share of the batch rows is batch-wide regardless (smaller shares stay row-scoped)
DUP_ROW_SCOPE_MIN = 0.02  # duplicated / repeated-key rows are untrusted (row-scoped) from this share of the batch on
RECORD_BATCH_SHARE = 0.10  # record-level problems covering this share of the batch make the whole batch untrusted
# the record penalty reaches (1 - threshold) at RECORD_BATCH_SHARE: record-level problems alone put the score exactly
# on the threshold there (and the batch is untrusted from that share on); below it they only lower the score
MAX_LOCAL_ENTRIES = 400
MAX_ROW_RANGES = 200  # untrusted_rows kept per batch (largest first)
MAX_BLOCK_LOCAL = 12  # blocks of a grouped finding expanded into per-signal local_untrusted entries (largest first)


def counts_for_trust(c: CheckResult) -> bool:
    """Data-quality findings count; operating rules only when their type is a data-quality kind; passes and
    not-testable checks never."""
    if not is_problem(c.status):
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
    if t == "plausibility":
        return f"{v.get('n', '?')} readings outside its plausible range"
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
    if t in ("quantization_change", "quantization_block"):
        return "changed resolution"
    if t == "frozen_block":
        return "frozen together with other signals (logging problem)"
    if t == "missing_block":
        return "missing together with other signals (logging gap)"
    if t.startswith("rule:"):
        return f"violates {c.rule_id}"
    return t.replace("_", " ")


def _record_reason(c: CheckResult, share: float) -> str:
    v = c.values or {}
    t = c.check_type
    w = WORDING.get(str(v.get("wording") or "sensor"), WORDING["sensor"])
    if t == "duplicate_rows":
        return f"{share:.1%} of its rows are exact copies of earlier rows"
    if t == "duplicate_key":
        return f"{share:.1%} of its rows repeat an existing (group, order) key"
    if t == "frozen_block":
        return f"{share:.1%} of its rows are frozen in {v.get('signals_per_block_median') or len(c.signals)} {w['signals']} at once ({w['record_problem']})"
    if t == "missing_block":
        return f"{share:.1%} of its rows are missing in {v.get('signals_per_row_typical') or len(c.signals)} {w['signals']} at once ({w['record_gap']})"
    if t == "empty_rows":
        return f"{share:.1%} of its rows have no values at all"
    return f"{share:.1%} of its rows: {t.replace('_', ' ')}"


def _exposure(c: CheckResult, batch_rows: Optional[int]) -> float:
    """Share of the batch rows a check actually affects (1.0 when unknown = whole batch)."""
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


def _events(c: CheckResult) -> list[tuple[int, int]]:
    ev = (c.values or {}).get("events") or []
    out = []
    for e in ev:
        try:
            out.append((int(e[0]), int(e[1])))
        except (TypeError, ValueError, IndexError):
            continue
    if not out and c.row_start is not None:
        out.append((int(c.row_start), int(c.row_end if c.row_end is not None else c.row_start)))
    return out


def _assess(settings: Any, batch_id: str, checks: Iterable[CheckResult], n_signals: Optional[int], n_rows: Optional[int]) -> dict[str, Any]:
    """Everything the verdict needs, without evidence or persistence (shared by compute_trust and trust_verdict)."""
    q = settings.quality
    checks = [c for c in checks if c.batch_id == batch_id]
    sig_sev: dict[str, float] = {}
    sig_why: dict[str, list[str]] = {}
    warn_sigs: set[str] = set()
    batch_sev = 0.0
    batch_reasons: list[str] = []
    local: list[dict[str, Any]] = []
    warn_types: list[str] = []
    record_share = 0.0
    row_scoped_share = 0.0
    record_reasons: list[str] = []
    row_ranges: list[dict[str, Any]] = []
    for c in checks:
        if not counts_for_trust(c):
            continue
        t = c.check_type
        if t in RECORD_LEVEL:
            share = _exposure(c, n_rows)
            rf = (c.values or {}).get("record_fraction")
            own = float(min(share, max(0.0, rf))) if isinstance(rf, (int, float)) else share  # rows another record-level finding already counts are counted once
            record_share += own
            scoped = t not in ("duplicate_rows", "duplicate_key") or share >= DUP_ROW_SCOPE_MIN
            record_reasons.append(_record_reason(c, share) + ("" if scoped else " (too few to distrust the rows around them)"))
            if scoped:
                row_scoped_share += own
                for a, b in _events(c):
                    row_ranges.append({"row_start": a, "row_end": b, "check_type": t, "check_id": c.check_id})
                if c.signals:  # grouped block: its members are unreliable in those rows (or batch-wide when it is large)
                    members = (c.values or {}).get("members") or {}
                    evs = sorted(_events(c), key=lambda e: -(e[1] - e[0]))[:MAX_BLOCK_LOCAL]
                    for s in c.signals:
                        m = members.get(s) if isinstance(members, dict) else None
                        s_expo = min(1.0, float(m.get("rows", 0)) / n_rows) if (isinstance(m, dict) and n_rows) else share
                        if s_expo >= BATCH_MIN_EXPOSURE:
                            sig_sev[s] = max(sig_sev.get(s, 0.0), float(c.severity) * (0.5 + 0.5 * s_expo))
                            sig_why.setdefault(s, []).append(_why(c))
                        else:
                            local.extend({"signal": s, "row_start": a, "row_end": b, "check_type": t, "severity": round(float(c.severity), 3), "check_id": c.check_id} for a, b in evs)
            continue
        is_batch = t in BATCH_LEVEL or not c.signals
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
            warn_types.append(t)
            if not is_batch:
                warn_sigs.update(c.signals)
    record_share = min(1.0, record_share)
    row_scoped_share = min(1.0, row_scoped_share)
    if n_signals is None:
        n_signals = max(1, len({s for c in checks for s in c.signals}))
    n_signals = max(1, int(n_signals))
    share = sum(sig_sev.values()) / n_signals
    signal_penalty = 0.7 * min(1.0, share / max(float(q.critical_signal_fraction), 1e-6))
    record_penalty = (1.0 - float(q.trust_fail_threshold)) * min(1.0, record_share / RECORD_BATCH_SHARE)
    batch_penalty = 0.3 * min(1.0, batch_sev)
    warn_penalty = min(0.1, 0.1 * len(warn_sigs - set(sig_sev)) / n_signals)
    score = max(0.0, min(1.0, 1.0 - signal_penalty - record_penalty - batch_penalty - warn_penalty))
    thr = float(q.trust_fail_threshold)
    as_whole = record_share >= RECORD_BATCH_SHARE
    if as_whole and score >= thr:
        score = max(0.0, thr - 0.05)  # keep the score consistent with the verdict under any configured threshold
    trusted = score >= thr and not as_whole
    local.sort(key=lambda e: -(e["row_end"] - e["row_start"]))
    row_ranges.sort(key=lambda e: -(e["row_end"] - e["row_start"]))
    return {"trust_score": score, "trusted": trusted, "as_whole": as_whole, "sig_sev": sig_sev, "sig_why": sig_why, "warn_sigs": warn_sigs, "batch_sev": batch_sev, "batch_reasons": batch_reasons,
            "local": local[:MAX_LOCAL_ENTRIES], "warn_types": warn_types, "record_share": record_share, "row_scoped_share": row_scoped_share, "record_reasons": record_reasons, "row_ranges": row_ranges[:MAX_ROW_RANGES],
            "n_signals": n_signals, "share": share, "signal_penalty": signal_penalty, "record_penalty": record_penalty, "batch_penalty": batch_penalty, "warn_penalty": warn_penalty, "checks": checks}


def compute_trust(settings: Any, batch_id: str, checks: Iterable[CheckResult], n_signals: Optional[int] = None, n_rows: Optional[int] = None) -> dict[str, Any]:
    """Pure trust computation (no evidence, no persistence): the assessor uses it for what-if scenarios.
    Failing signal checks are weighted by the share of the batch they affect; problems confined to a few rows
    are row-scoped (local_untrusted) instead of marking the signal unreliable for the whole batch; record-level
    problems (duplicates, common-mode blocks, empty rows) count by the share of rows they cover."""
    a = _assess(settings, batch_id, checks, n_signals, n_rows)
    sig_sev = a["sig_sev"]
    return {"trust_score": a["trust_score"], "trusted": a["trusted"], "untrusted_signals": sorted(sig_sev, key=lambda s: (-sig_sev[s], s)), "n_signals": a["n_signals"], "record_share": round(a["record_share"], 6)}


def trust_verdict(ws: Any, settings: Any, batch_id: str, checks: Iterable[CheckResult], n_signals: Optional[int] = None, persist: bool = True, n_rows: Optional[int] = None) -> TrustVerdict:
    if n_rows is None:
        n_rows = _batch_rows(ws, batch_id)
    a = _assess(settings, batch_id, checks, n_signals, n_rows)
    checks = a["checks"]
    sig_sev, sig_why, local = a["sig_sev"], a["sig_why"], a["local"]
    score, trusted, n_signals = a["trust_score"], a["trusted"], a["n_signals"]
    batch_reasons, record_reasons = a["batch_reasons"], a["record_reasons"]
    untrusted = sorted(sig_sev, key=lambda s: (-sig_sev[s], s))
    reasons: list[str] = []
    for s in untrusted:
        reasons.append(f"{s}: " + "; ".join(dict.fromkeys(sig_why.get(s, []))))
    reasons.extend(record_reasons)
    reasons.extend(batch_reasons)
    n_aff = len(untrusted)
    listed = ", ".join(f"{s} ({'; '.join(dict.fromkeys(sig_why.get(s, [])))})" for s in untrusted[:6])
    more = f" and {n_aff - 6} more" if n_aff > 6 else ""
    if not trusted:
        parts = list(record_reasons)
        if untrusted:
            parts.append(f"{n_aff} of {n_signals} signals are unreliable, e.g. {listed}{more}")
        if batch_reasons:
            parts.append(batch_reasons[0].rstrip("."))
        fallback = not parts
        if fallback:
            parts.append(f"its trust score {score:.2f} is below the threshold {float(settings.quality.trust_fail_threshold):.2f}")
        statement = f"This data cannot be trusted as a whole: in batch {batch_id}, " + "; ".join(parts) + "."
        if a["as_whole"]:
            statement += f" {a['record_share']:.0%} of the rows are not real, independent measurements, so statistics and alarms computed on this batch are unreliable."
        elif not fallback:  # no single reason is decisive on its own: say that the combination is
            statement += f" Together these problems bring its trust score to {score:.2f}, below the threshold of {float(settings.quality.trust_fail_threshold):.2f}."
    else:
        if untrusted:
            statement = f"Data in batch {batch_id} is usable but cannot be trusted for signals {listed}{more}; these are excluded or down-weighted in detection."
        elif record_reasons and a["row_ranges"]:
            statement = f"Data in batch {batch_id} is usable only in part: " + "; ".join(record_reasons) + ". Those rows are untrusted for every signal and are not used as evidence."
        elif batch_reasons:
            statement = f"Data in batch {batch_id} has structural problems: " + batch_reasons[0]
        elif record_reasons:
            statement = f"Data in batch {batch_id} is usable: " + "; ".join(record_reasons) + "."
        elif local:
            statement = f"Data in batch {batch_id} is usable overall."
        elif a["warn_types"]:
            wt = sorted(set(a["warn_types"]))
            statement = f"Data in batch {batch_id} passed the baseline checks with {len(a['warn_types'])} warning(s) ({', '.join(x.replace('_', ' ') for x in wt[:4])})."
        else:
            statement = f"Data in batch {batch_id} passed all baseline checks."
        if untrusted and (record_reasons or batch_reasons):
            statement += " Also: " + (record_reasons[0] if record_reasons else batch_reasons[0])
    check_ids = [c.check_id for c in checks if counts_for_trust(c)]
    ev_ids = [e for c in checks if counts_for_trust(c) for e in c.evidence_ids]
    ev = ws.evidence.add("trust", f"Batch {batch_id}: trust score {score:.2f}, {n_aff}/{n_signals} signals unreliable, {a['record_share']:.1%} of the rows with record-level problems, batch-level severity {a['batch_sev']:.2f}", signals=untrusted[:20], values={"trust_score": round(score, 4), "share_weighted": round(a["share"], 4), "n_signals": n_signals, "n_untrusted": n_aff, "batch_severity": round(a["batch_sev"], 3), "record_share": round(a["record_share"], 4), "untrusted_row_share": round(a["row_scoped_share"], 4), "signal_penalty": round(a["signal_penalty"], 4), "record_penalty": round(a["record_penalty"], 4), "batch_penalty": round(a["batch_penalty"], 4), "warn_penalty": round(a["warn_penalty"], 4), "as_whole": a["as_whole"]}, computed_by="quality.trust.trust_verdict", batch_id=batch_id)
    if local and not untrusted and trusted:
        n_loc_sig = len({e["signal"] for e in local})
        statement += f" {n_loc_sig} signal(s) are unreliable only in specific rows (e.g. {local[0]['signal']} rows {local[0]['row_start']}-{local[0]['row_end']}: {local[0]['check_type'].replace('_', ' ')}); detection treats them as data problems there, not elsewhere."
    verdict = TrustVerdict(batch_id=batch_id, trusted=trusted, trust_score=round(score, 4), untrusted_signals=untrusted, local_untrusted=local, untrusted_rows=a["row_ranges"], untrusted_row_share=round(a["row_scoped_share"], 6), n_rows=n_rows, reasons=reasons[:30], check_ids=check_ids, statement=statement)
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


PROBLEM_WORDS = {
    "duplicate_rows": "duplicated rows", "duplicate_key": "repeated (group, order) keys", "frozen_block": "rows frozen in many signals at once",
    "missing_block": "rows missing in many signals at once", "empty_rows": "empty rows", "stuck": "values frozen for a long time", "missing": "missing values",
    "dropout": "signals without any data", "plausibility": "implausible values", "out_of_range": "values far outside the usual range",
    "unit_shift": "changes of scale", "impossible_value": "impossible values", "saturation": "values stuck at a limit",
    "quantization_change": "changes of resolution", "quantization_block": "changes of resolution in many signals at once", "gap": "gaps in time",
    "out_of_order": "timestamps out of order", "duplicate_timestamp": "repeated timestamps", "irregular_sampling": "irregular sampling",
    "stale": "signals that stopped updating", "relation_break": "redundant signals that no longer agree", "local_spike": "single odd values",
}


def dominant_problem(checks: Iterable[CheckResult], batch_ids: Optional[set[str]] = None) -> str:
    """Plain words for the data problem that weighs most (failing checks, severity x covered share), optionally only
    in the given batches."""
    weight: dict[str, float] = {}
    for c in checks:
        if c.status != "fail" or not counts_for_trust(c) or (batch_ids is not None and c.batch_id not in batch_ids):
            continue
        key = "rule" if c.check_type.startswith("rule") else c.check_type
        v = c.values or {}
        # weight = severity x share of the rows the finding covers: a few odd readings do not outweigh 5 % copied rows
        frac = next((float(v[k]) for k in ("record_fraction", "fraction", "stuck_fraction", "missing_rate") if isinstance(v.get(k), (int, float))), 0.01)
        weight[key] = weight.get(key, 0.0) + float(c.severity) * max(frac, 1e-6)
    if not weight:
        return ""
    top = max(weight, key=weight.get)
    return PROBLEM_WORDS.get(top, "operating-rule violations" if top == "rule" else top.replace("_", " "))


def run_summary(verdicts: Iterable[TrustVerdict], batches: Optional[list[dict[str, Any]]] = None, n_fail: int = 0, n_warn: int = 0, n_not_testable: int = 0, n_checks: int = 0, not_testable_categories: Iterable[str] = (), top_problem: str = "") -> dict[str, Any]:
    """Run-level verdict in plain words (quality_summary.json; read by the report and the UI). Never says "fine"
    when failing checks exist: the verdict is one of untrusted | partly_untrusted | usable_with_problems | clean."""
    verdicts = list(verdicts)
    rows = {str(b.get("batch_id")): int(b.get("n_rows") or 0) for b in (batches or [])}
    total = sum(rows.values()) or sum(int(v.n_rows or 0) for v in verdicts) or 0
    bad = [v for v in verdicts if not v.trusted]
    n_b = len(verdicts)

    def nrows(v: TrustVerdict) -> int:
        return rows.get(v.batch_id) or int(v.n_rows or 0)

    bad_rows = sum(nrows(v) for v in bad)
    bad_share = bad_rows / total if total else (len(bad) / n_b if n_b else 0.0)
    # rows untrusted inside trusted batches: record-level row scope (all signals)
    scoped_rows = sum(float(v.untrusted_row_share or 0.0) * nrows(v) for v in verdicts if v.trusted)
    scoped_share = scoped_rows / total if total else 0.0
    local_sigs = {e.get("signal") for v in verdicts if v.trusted for e in (v.local_untrusted or []) if isinstance(e, dict)}
    top = top_problem
    nt = sorted(set(not_testable_categories))
    nt_txt = f" {', '.join(c.capitalize() for c in nt)} could not be tested." if nt else ""
    if bad and bad_share >= 0.5:
        verdict = "untrusted"
        statement = f"This data cannot be trusted as a whole: {len(bad)} of {n_b} batches ({bad_share:.0%} of the rows) failed the trust check" + (f", mainly because of {top}" if top else "") + f". {n_fail} checks failed and {n_warn} warned.{nt_txt}"
    elif bad:
        verdict = "partly_untrusted"
        statement = f"Parts of this data cannot be trusted: {len(bad)} of {n_b} batches ({bad_share:.0%} of the rows) failed the trust check" + (f", mainly because of {top}" if top else "") + f"; in the other batches {scoped_share:.1%} of the rows are untrusted. {n_fail} checks failed and {n_warn} warned.{nt_txt}"
    elif n_fail or scoped_share > 0 or local_sigs:
        verdict = "usable_with_problems"
        parts = [f"{n_fail} checks failed and {n_warn} warned"]
        if scoped_share > 0:
            parts.append(f"{scoped_share:.1%} of the rows are untrusted for every signal (duplicated, frozen or missing records)")
        if local_sigs:
            parts.append(f"{len(local_sigs)} signal(s) are unreliable in specific rows")
        statement = f"This data is usable, but not clean: every batch passed the trust check, yet " + "; ".join(parts) + f". Those rows and signals are set aside, not treated as process behaviour.{nt_txt}"
    elif n_b:
        verdict = "clean"
        statement = f"This data passed the baseline checks: all {n_b} batches can be trusted" + (f" ({n_warn} minor warnings)" if n_warn else "") + f".{nt_txt}"
    else:
        verdict = "clean"
        statement = f"No batch was checked.{nt_txt}"
    return {"verdict": verdict, "statement": statement, "n_batches": n_b, "n_untrusted": len(bad), "untrusted_batch_row_share": round(bad_share, 6), "row_scoped_share": round(scoped_share, 6),
            "top_problem": top, "n_checks": n_checks, "n_fail": n_fail, "n_warn": n_warn, "n_not_testable": n_not_testable, "not_testable_categories": nt}
