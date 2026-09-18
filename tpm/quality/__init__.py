"""quality stage (agent B): batches, baseline data-quality checks, trust verdicts and operating rules.

Public functions (see docs/ARCHITECTURE.md):
    run_quality(ws, settings, ctx)                 -> summary dict; writes batches.json, checks.jsonl, trust.jsonl
    check_batch(ws, settings, batch_df, batch_id)  -> (list[CheckResult], TrustVerdict)   (stream path)
    compile_rule(ws, settings, text)               -> Rule (template grammar first, LLM optional)
    apply_override(ws, settings, decision)         -> rule lifecycle for HumanDecision(object_type="rule")
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from ..contracts import CheckResult, TrustVerdict
from ._common import load_catalog, numeric_signals, reset_check_ids
from .batches import define_batches, load_batch_frame
from .checks import check_batch as _check_batch
from .checks import _quality_context, run_checks_for_batch
from .rules import add_rules_from_file, apply_override, compile_rule, load_rules_file, parse_rule_text, run_active_rules, run_rule, run_rules_on_frame
from .trust import trust_verdict

__all__ = ["run_quality", "check_batch", "compile_rule", "apply_override", "define_batches", "trust_verdict", "run_rule", "parse_rule_text", "load_rules_file", "add_rules_from_file", "run_active_rules", "load_batch_frame"]


def check_batch(ws: Any, settings: Any, batch_df: Any, batch_id: str) -> tuple[list[CheckResult], TrustVerdict]:
    """Stream path: baseline checks + active rules + trust verdict for one in-memory batch."""
    checks, verdict = _check_batch(ws, settings, batch_df, batch_id)
    rule_checks = run_rules_on_frame(ws, settings, batch_df, batch_id)
    if rule_checks:
        checks = checks + rule_checks
        # rule failures also count for trust: rewrite the verdict once with everything
        verdict = trust_verdict(ws, settings, batch_id, checks, n_signals=len(numeric_signals(load_catalog(ws))) or None, persist=False)
        verdicts = [v for v in ws.read_jsonl("trust") if v.get("batch_id") != batch_id]
        verdicts.append(verdict.model_dump())
        ws.rewrite_jsonl("trust", verdicts)
    return checks, verdict


def run_quality(ws: Any, settings: Any, ctx: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Pipeline stage. Reruns start clean (checks.jsonl / trust.jsonl are rewritten)."""
    ctx = ctx or {}
    t0 = time.time()
    progress = ctx.get("progress") or (lambda f, m="": None)
    total_budget = float(ctx.get("time_budget_s") or settings.time_budget_s)
    budget = max(30.0, min(240.0, 0.2 * total_budget))
    deadline = t0 + budget
    ws.log.record("system:quality", "stage", "stage", "quality", {"state": "started", "budget_s": budget})
    if not ws.exists("dataset"):
        raise FileNotFoundError("dataset.parquet is missing; run ingest first")
    for art in ("checks", "trust"):
        if ws.exists(art):
            ws.path(art).unlink()
    reset_check_ids(ws)
    progress(0.02, "defining batches")
    batches = define_batches(ws, settings)
    qctx = _quality_context(ws, settings)
    n_signals = len(numeric_signals(qctx["catalog"])) or None
    total_rows = sum(int(b["n_rows"]) for b in batches) or 1
    done_rows = 0
    n_checks = n_fail = n_warn = 0
    untrusted: list[str] = []
    verdicts = []
    stride = 1
    for i, b in enumerate(batches):
        # time-budget guard: after the first batch, estimate throughput and subsample rows if we would overrun
        elapsed = time.time() - t0
        remaining_rows = total_rows - done_rows
        if i >= 1 and elapsed > 0 and done_rows > 0:
            rate = done_rows / elapsed
            affordable = max(1.0, (deadline - time.time()) * rate)
            if remaining_rows > affordable * 1.05:
                stride = max(stride, int(remaining_rows / affordable) + 1)
        checks = run_checks_for_batch(ws, settings, b, qctx, deadline=deadline, stride=stride)
        for c in checks:
            ws.append_jsonl("checks", c)
        v = trust_verdict(ws, settings, b["batch_id"], checks, n_signals=n_signals)
        verdicts.append(v)
        n_checks += len(checks)
        n_fail += sum(c.status == "fail" for c in checks)
        n_warn += sum(c.status == "warn" for c in checks)
        if not v.trusted:
            untrusted.append(b["batch_id"])
        done_rows += int(b["n_rows"])
        progress(0.05 + 0.75 * done_rows / total_rows, f"checked {b['batch_id']} ({len(checks)} checks, trust {v.trust_score:.2f})")
    # rules
    rules_file = (ctx.get("options") or {}).get("rules_file")
    n_rules_loaded = 0
    if rules_file:
        p = Path(rules_file)
        if p.exists():
            added = add_rules_from_file(ws, settings, p, author="file", auto_status="active")
            n_rules_loaded = len(added)
        else:
            ws.log.record("system:quality", "rules_loaded", "rules", str(rules_file), {"error": "file not found"})
    progress(0.82, "evaluating active rules")
    rule_checks = run_active_rules(ws, settings, batches, deadline=t0 + budget * 1.5)
    rule_fail_batches = {c.batch_id for c in rule_checks if c.status == "fail"}
    if rule_checks:
        n_checks += len(rule_checks)
        n_fail += sum(c.status == "fail" for c in rule_checks)
        # refresh trust for batches with rule failures
        all_checks = ws.checks()
        new_verdicts = []
        for v in verdicts:
            if v.batch_id in rule_fail_batches:
                v = trust_verdict(ws, settings, v.batch_id, [c for c in all_checks if c.batch_id == v.batch_id], n_signals=n_signals, persist=False)
            new_verdicts.append(v)
        verdicts = new_verdicts
        ws.rewrite_jsonl("trust", verdicts)
        untrusted = [v.batch_id for v in verdicts if not v.trusted]
    seconds = round(time.time() - t0, 2)
    summary = {"n_batches": len(batches), "n_checks": n_checks, "n_fail": n_fail, "n_warn": n_warn, "untrusted_batches": untrusted, "n_rules_loaded": n_rules_loaded, "n_rule_checks": len(rule_checks), "stride": stride, "seconds": seconds, "message": f"{len(batches)} batches, {n_checks} checks ({n_fail} fail, {n_warn} warn), {len(untrusted)} untrusted batches in {seconds}s"}
    ws.log.record("system:quality", "stage", "stage", "quality", {"state": "done", **{k: v for k, v in summary.items() if k != "message"}})
    progress(1.0, summary["message"])
    return summary
