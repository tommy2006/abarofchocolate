"""Data-quality scores (0..1) per category from checks.jsonl + trust.jsonl.

score_category = 1 - min(1, penalty / critical_signal_fraction), where penalty is the severity-weighted share of
(batch, signal) cells with a warn (weight 0.4) or fail (weight 1.0) in that category; batch-level checks (duplicates,
gaps, ...) count as half a batch. overall = 0.7 * mean(category scores) + 0.3 * mean trust score.

``compute_dq_scores`` is pure so the assessor can evaluate what-if scenarios (drop a signal, a group, a row range,
duplicates) without touching the workspace; ``dq_scores`` adds evidence.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from ..contracts import CheckResult, TrustVerdict
from ..quality._common import load_catalog, numeric_signals
from ..quality.trust import compute_trust

CATEGORIES = ["completeness", "validity", "consistency", "timeliness"]
STATUS_W = {"fail": 1.0, "warn": 0.4}


def _keep(c: CheckResult, exclude_signals: set[str], exclude_batches: set[str], exclude_types: set[str], exclude_rows: Optional[tuple[int, int]]) -> bool:
    if c.status == "pass" or c.category not in CATEGORIES:
        return False
    if c.batch_id in exclude_batches or c.check_type in exclude_types:
        return False
    if c.signals and all(s in exclude_signals for s in c.signals):
        return False
    if exclude_rows and c.row_start is not None and c.row_end is not None and c.row_start >= exclude_rows[0] and c.row_end <= exclude_rows[1]:
        return False
    return True


def compute_dq_scores(checks: Iterable[CheckResult], verdicts: Iterable[TrustVerdict], n_batches: int, n_signals: int, settings: Any, exclude_signals: Iterable[str] = (), exclude_batches: Iterable[str] = (), exclude_types: Iterable[str] = (), exclude_rows: Optional[tuple[int, int]] = None) -> dict[str, Any]:
    ex_s, ex_b, ex_t = set(exclude_signals), set(exclude_batches), set(exclude_types)
    checks = list(checks)
    n_batches = max(1, int(n_batches) - len(ex_b))
    n_sig = max(1, int(n_signals) - len(ex_s))
    pen = {c: 0.0 for c in CATEGORIES}
    counts = {c: {"n_fail": 0, "n_warn": 0, "signals": set()} for c in CATEGORIES}
    kept: list[CheckResult] = []
    for c in checks:
        if not _keep(c, ex_s, ex_b, ex_t, exclude_rows):
            continue
        kept.append(c)
        w = float(c.severity) * STATUS_W.get(c.status, 0.0)
        if c.signals:
            pen[c.category] += w / (n_batches * n_sig)
            counts[c.category]["signals"].update(s for s in c.signals if s not in ex_s)
        else:
            pen[c.category] += 0.5 * w / n_batches
        counts[c.category]["n_fail" if c.status == "fail" else "n_warn"] += 1
    crit = max(float(settings.quality.critical_signal_fraction), 1e-6)
    scores = {c: round(max(0.0, 1.0 - min(1.0, pen[c] / crit)), 4) for c in CATEGORIES}
    # trust: recompute per batch when something is excluded (a dropped signal no longer hurts)
    trust_scores: list[float] = []
    n_untrusted = 0
    verdicts = [v for v in verdicts if v.batch_id not in ex_b]
    if ex_s or ex_t or exclude_rows:
        by_batch: dict[str, list[CheckResult]] = {}
        for c in kept:
            by_batch.setdefault(c.batch_id, []).append(c)
        for v in verdicts:
            t = compute_trust(settings, v.batch_id, by_batch.get(v.batch_id, []), n_signals=n_sig)
            trust_scores.append(t["trust_score"])
            n_untrusted += 0 if t["trusted"] else 1
    else:
        trust_scores = [float(v.trust_score) for v in verdicts]
        n_untrusted = sum(0 if v.trusted else 1 for v in verdicts)
    mean_trust = sum(trust_scores) / len(trust_scores) if trust_scores else 1.0
    overall = round(0.7 * (sum(scores.values()) / len(scores)) + 0.3 * mean_trust, 4)
    return {**scores, "overall": overall, "mean_trust": round(mean_trust, 4), "n_untrusted_batches": n_untrusted, "n_batches": n_batches, "n_signals": n_sig, "penalties": {c: round(pen[c], 5) for c in CATEGORIES}, "counts": {c: {"n_fail": counts[c]["n_fail"], "n_warn": counts[c]["n_warn"], "n_signals": len(counts[c]["signals"])} for c in CATEGORIES}}


def worst_signals(checks: Iterable[CheckResult], verdicts: Iterable[TrustVerdict], top: int = 8) -> list[dict[str, Any]]:
    """Signals ranked by how often and how badly they fail (for recommendations and the UI)."""
    agg: dict[str, dict[str, Any]] = {}
    n_batches = 0
    for v in verdicts:
        n_batches += 1
        for s in v.untrusted_signals:
            agg.setdefault(s, {"signal": s, "untrusted_batches": 0, "severity_sum": 0.0, "types": set(), "n_checks": 0})["untrusted_batches"] += 1
    for c in checks:
        if c.status == "pass" or c.category == "rule":
            continue
        for s in c.signals:
            d = agg.setdefault(s, {"signal": s, "untrusted_batches": 0, "severity_sum": 0.0, "types": set(), "n_checks": 0})
            d["severity_sum"] += c.severity * STATUS_W.get(c.status, 0.0)
            d["types"].add(c.check_type)
            d["n_checks"] += 1
    out = []
    for d in agg.values():
        d["types"] = sorted(d["types"])
        d["untrusted_fraction"] = round(d["untrusted_batches"] / max(1, n_batches), 4)
        d["severity_sum"] = round(d["severity_sum"], 3)
        out.append(d)
    out.sort(key=lambda d: (-d["untrusted_batches"], -d["severity_sum"]))
    return out[:top]


def dq_scores(ws: Any, settings: Any) -> dict[str, Any]:
    checks = ws.checks()
    verdicts = ws.trust()
    batches = ws.read_json("batches", []) or []
    catalog = load_catalog(ws)
    n_signals = len(numeric_signals(catalog)) or max(1, len({s for c in checks for s in c.signals}))
    n_batches = len(batches) or len({c.batch_id for c in checks}) or 1
    res = compute_dq_scores(checks, verdicts, n_batches, n_signals, settings)
    ev_ids: list[str] = []
    details: dict[str, Any] = {}
    for cat in CATEGORIES:
        cnt = res["counts"][cat]
        stmts = sorted((c for c in checks if c.category == cat and c.status != "pass"), key=lambda c: -c.severity)[:5]
        top = [{"check_id": c.check_id, "statement": c.statement, "severity": c.severity, "batch_id": c.batch_id, "signals": c.signals} for c in stmts]
        if cnt["n_fail"] or cnt["n_warn"]:
            st = f"{cat.capitalize()} score {res[cat]:.2f}: {cnt['n_fail']} failing and {cnt['n_warn']} warning checks over {n_batches} batches, {cnt['n_signals']} signal(s) involved; worst: {stmts[0].statement}" if stmts else f"{cat.capitalize()} score {res[cat]:.2f}"
        else:
            st = f"{cat.capitalize()} score {res[cat]:.2f}: no problems found in {n_batches} batches x {n_signals} signals"
        ev = ws.evidence.add("dq_score", st, signals=sorted({s for c in stmts for s in c.signals})[:20], values={"category": cat, "score": res[cat], "penalty": res["penalties"][cat], **cnt}, computed_by="assessor.scores.dq_scores", n_samples=n_batches)
        ev_ids.append(ev.id)
        details[cat] = {"score": res[cat], "statement": st, "evidence_id": ev.id, "top": top, **cnt}
    ev = ws.evidence.add("dq_score", f"Overall data-quality score {res['overall']:.2f} (mean trust {res['mean_trust']:.2f}, {res['n_untrusted_batches']} of {n_batches} batches untrusted)", values={k: res[k] for k in ("overall", "mean_trust", "n_untrusted_batches", "n_batches", "n_signals")}, computed_by="assessor.scores.dq_scores", n_samples=n_batches)
    ev_ids.append(ev.id)
    res["details"] = details
    res["worst_signals"] = worst_signals(checks, verdicts)
    res["evidence_ids"] = ev_ids
    ws.log.record("system:assessor", "dq_scores", "assessor", "dq_scores", {k: res[k] for k in CATEGORIES + ["overall", "mean_trust", "n_untrusted_batches"]}, ev_ids)
    return res
