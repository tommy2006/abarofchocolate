"""Completeness audit of the decision log: for every kind of object a run produced, how many exist in the run's
artifacts and how many have an entry of their own in the hash-chained decision log, and, for anything that has none,
which objects and why. `python -m tpm verify-log <run>` prints it under the chain check.

    res = audit(ws)            # {"rows": [...], "complete": bool, "n_entries": n, "seconds": s}
    for line in format_audit(res): print(line)

What is excluded on purpose is said once, here and in docs/DATAFLOW.md: raw readings never go into the log (entries
carry ids, status, signals, rows, scores and evidence ids; the readings stay in dataset.parquet), and evidence items
are cited by id from the entries of the objects they support instead of being logged one by one.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from typing import Any, Callable, Iterable, Optional

OLD_FLAG_CAP = 2000  # the detect stage logged at most this many flags before round 6 (2026-09-19)
_EXAMPLES = 5


def _jsonl(ws: Any, name: str) -> list[dict[str, Any]]:
    try:
        return [x for x in ws.read_jsonl(name) if isinstance(x, dict)]
    except Exception:
        return []


def _json_list(ws: Any, name: str) -> list[dict[str, Any]]:
    try:
        v = ws.read_json(name, []) or []
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []
    except Exception:
        return []


def _log_index(ws: Any) -> dict[str, Any]:
    """One pass over the log: object ids per (object_type, action), evidence ids cited, and the payloads of the few
    summary entries the reasons refer to. No other payload is parsed."""
    by_type: dict[str, set[str]] = {}
    by_action: dict[tuple[str, str], set[str]] = {}
    cited: set[str] = set()
    summaries: dict[str, list[dict[str, Any]]] = {}
    n = 0
    for otype, action, oid, ev, payload in ws.log.scan(("hypotheses", "stage_summary", "stage")):
        n += 1
        by_type.setdefault(otype, set()).add(oid)
        by_action.setdefault((otype, action), set()).add(oid)
        if ev and ev != "[]":
            try:
                cited.update(json.loads(ev))
            except Exception:
                pass
        if payload:
            try:
                summaries.setdefault(f"{action}:{oid}", []).append(json.loads(payload))
            except Exception:
                pass
    return {"by_type": by_type, "by_action": by_action, "cited": cited, "summaries": summaries, "n": n}


def _row(kind: str, label: str, source: str, ids: list[str], logged: set[str], reasons: Callable[[list[str]], list[dict[str, Any]]], note: str = "") -> dict[str, Any]:
    present = list(dict.fromkeys(i for i in ids if i and i != "None"))  # distinct objects (a file may repeat a row)
    missing = [i for i in present if i not in logged]
    n_logged = len(present) - len(missing)
    return {"object": kind, "label": label, "source": source, "n_artifacts": len(present), "n_logged": n_logged, "n_missing": len(missing), "complete": not missing, "excluded": reasons(missing) if missing else [], "note": note}


def _group(reason: str, ids: list[str]) -> dict[str, Any]:
    return {"n": len(ids), "reason": reason, "examples": ids[:_EXAMPLES]}


def _latest(summaries: dict[str, list[dict[str, Any]]], key: str) -> dict[str, Any]:
    v = summaries.get(key) or []
    return v[-1] if v else {}


def audit(ws: Any) -> dict[str, Any]:
    """Per object type: objects in the artifacts vs objects with an entry of their own in the decision log, and the
    reason for every gap. Read-only; a few hundred milliseconds on a run with thousands of flags and diagnoses."""
    t0 = time.perf_counter()
    idx = _log_index(ws)
    by_type, by_action, summaries = idx["by_type"], idx["by_action"], idx["summaries"]
    rows: list[dict[str, Any]] = []

    # ---- flags
    flags = _jsonl(ws, "flags")
    detect_summary = _latest(summaries, "stage_summary:detect")
    logged_flags = by_action.get(("flag", "flag"), set())

    def flag_reasons(missing: list[str]) -> list[dict[str, Any]]:
        if "n_flags_logged" not in detect_summary and len(logged_flags) == OLD_FLAG_CAP and len(flags) > OLD_FLAG_CAP:
            return [_group(f"made by an earlier version whose detect stage logged at most {OLD_FLAG_CAP:,} flags (the cap was removed on 2026-09-19); these flags are in flags.jsonl and the report, but have no entry of their own", missing)]
        return [_group("no entry of their own: written outside the detect stage, or by an earlier version of the app", missing)]

    rows.append(_row("flag", "flags", "flags.jsonl", [str(f.get("id")) for f in flags], logged_flags, flag_reasons))

    # ---- data-quality checks (baseline checks and rule checks)
    checks = _jsonl(ws, "checks")
    quality_done = [p for p in summaries.get("stage:quality", []) if p.get("state") == "done"]
    logged_checks = by_action.get(("check", "check"), set())

    def check_reasons(missing: list[str]) -> list[dict[str, Any]]:
        if not logged_checks and not any("n_checks_logged" in p for p in quality_done):
            return [_group("made by an earlier version that logged one summary entry per batch (its trust verdict), not one entry per check (per-check entries since 2026-09-19); every check is in checks.jsonl", missing)]
        return [_group("no entry of their own: written outside the quality stage (for example by an older live monitor), or by an earlier version", missing)]

    rows.append(_row("check", "data-quality checks", "checks.jsonl", [str(c.get("check_id")) for c in checks], logged_checks, check_reasons))

    # ---- trust verdicts (one per batch)
    trust = _jsonl(ws, "trust")
    rows.append(_row("trust", "trust verdicts (one per batch)", "trust.jsonl", sorted({str(v.get("batch_id")) for v in trust}), by_action.get(("batch", "trust"), set()), lambda m: [_group("no trust entry for these batches (verdicts written outside the quality stage)", m)]))

    # ---- diagnoses and their critiques
    diags = _jsonl(ws, "diagnoses")
    rows.append(_row("diagnosis", "diagnoses", "diagnoses.jsonl", [str(d.get("id")) for d in diags], by_action.get(("diagnosis", "diagnosis"), set()), lambda m: [_group("no entry of their own: added outside the diagnose stage", m)]))
    rows.append(_row("critique", "critiques (devil's advocate)", "diagnoses.jsonl", [str(d.get("id")) for d in diags if d.get("critique")], by_action.get(("diagnosis", "critique"), set()), lambda m: [_group("critique stored with the diagnosis without an entry of its own", m)]))

    # ---- inferences (claims about the data)
    infs = _jsonl(ws, "inferences")
    inf_by_id = {str(i.get("id")): i for i in infs}
    hyp = _latest(summaries, f"hypotheses:{getattr(ws, 'run_id', '')}") or next(iter(v[-1] for k, v in summaries.items() if k.startswith("hypotheses:") and v), {})

    def inference_reasons(missing: list[str]) -> list[dict[str, Any]]:
        heur, human, other = [], [], []
        for i in missing:
            inf = inf_by_id.get(i) or {}
            claim = str(inf.get("claim") or "")
            if inf.get("stage") == "profile" and str(inf.get("source") or "code") == "code" and re.match(r"^(instrument hypothesis|unit operation hypothesis)", claim):
                heur.append(i)
            elif str(inf.get("source") or "") == "human":
                human.append(i)
            else:
                other.append(i)
        out = []
        if heur:
            n_h = hyp.get("n_hypotheses")
            out.append(_group("code-written instrument / unit-operation guesses of the profile stage (one or two per signal): counted in its single 'hypotheses' entry" + (f" (n_hypotheses={n_h})" if n_h is not None else "") + " instead of one entry each; each is in inferences.jsonl with its evidence and shown in the signal catalog", heur))
        if human:
            out.append(_group("written when a person changed a signal or a rule: the person's decision is the log entry (on the signal / rule), the inference records its effect", human))
        if other:
            stages = Counter(str((inf_by_id.get(i) or {}).get("stage") or "?") for i in other)
            out.append(_group("no entry of their own (registered by " + ", ".join(f"stage {s} x{n}" for s, n in stages.most_common()) + "; stages log such claims at their end since 2026-09-19)", other))
        return out

    rows.append(_row("inference", "inferences (claims)", "inferences.jsonl", list(inf_by_id), by_type.get("inference", set()), inference_reasons))

    # ---- patterns, rules, egress calls, chat turns
    rows.append(_row("pattern", "fault patterns", "patterns.json", [str(p.get("id")) for p in _json_list(ws, "patterns")], by_type.get("pattern", set()), lambda m: [_group("no entry of their own", m)]))
    rules = _json_list(ws, "rules")
    rule_by_id = {str(r.get("id")): r for r in rules}

    def rule_reasons(missing: list[str]) -> list[dict[str, Any]]:
        raw = [i for i in missing if not (rule_by_id.get(i) or {}).get("compiled")]
        other = [i for i in missing if i not in raw]
        out = []
        if raw:
            out.append(_group("rule texts written into rules.json before the run started (`tpm run --rules`) and never compiled: the quality stage compiles the same file into rules of its own, and those are logged", raw))
        if other:
            out.append(_group("compiled rules without an entry of their own (added outside the quality stage)", other))
        return out

    rows.append(_row("rule", "operating rules", "rules.json", list(rule_by_id), by_type.get("rule", set()), rule_reasons))
    rows.append(_row("egress", "model calls (egress ledger)", "egress_ledger.jsonl", [str(r.get("id")) for r in _jsonl(ws, "egress_ledger")], by_action.get(("egress", "egress"), set()), lambda m: [_group("in the ledger file without a log entry (the log write failed, or the record was copied in)", m)]))
    chat = _jsonl(ws, "chat")
    turns = sorted({str(c.get("turn_id")) for c in chat if c.get("turn_id") and c.get("role") == "assistant"})
    rows.append(_row("chat", "answered chat turns", "chat.jsonl", turns, by_action.get(("chat", "chat"), set()), lambda m: [_group("answer stored without a log entry", m)]))

    # ---- evidence: cited by id, not logged one by one (by design)
    evidence = _jsonl(ws, "evidence")
    cited_log = idx["cited"]
    cited_art: set[str] = set()
    for rows_ in (flags, checks, diags, infs, trust):
        for x in rows_:
            cited_art.update(str(e) for e in (x.get("evidence_ids") or []))
    for name in ("signals", "patterns"):
        for x in _json_list(ws, name):
            cited_art.update(str(e) for e in (x.get("evidence_ids") or []))
    ev_ids = [str(e.get("id")) for e in evidence]
    n_log = sum(1 for e in ev_ids if e in cited_log)
    n_art = sum(1 for e in ev_ids if e not in cited_log and e in cited_art)
    uncited = [e for e in evidence if str(e.get("id")) not in cited_log and str(e.get("id")) not in cited_art]
    kinds = Counter(str(e.get("kind")) for e in uncited)
    ev_note = (f"not logged one by one, by design: evidence items are the facts log entries cite by id. {n_log:,} of {len(ev_ids):,} are cited by a log entry, "
               f"{n_art:,} only by an artifact (flags, checks, diagnoses, signal catalog)" + (f", {len(uncited):,} by neither (" + ", ".join(f"{k} x{v}" for k, v in kinds.most_common(6)) + "): statements behind a summary entry (relations, format detection, fold thresholds, trust scores recomputed after rule checks), or left by an earlier pass of a stage that was run again (the evidence registry is append-only)" if uncited else ""))
    rows.append({"object": "evidence", "label": "evidence items", "source": "evidence.jsonl", "n_artifacts": len(ev_ids), "n_logged": None, "n_missing": None, "complete": True, "excluded": [], "note": ev_note, "n_cited_by_log": n_log, "n_cited_by_artifacts_only": n_art, "n_uncited": len(uncited)})

    rows = [r for r in rows if r["n_artifacts"] or r["object"] in ("flag", "check")]
    gaps = [r for r in rows if r["n_missing"]]
    return {
        "run_id": getattr(ws, "run_id", ""),
        "n_entries": idx["n"],
        "rows": rows,
        "complete": not gaps,
        "n_objects": int(sum(r["n_artifacts"] for r in rows if r["n_logged"] is not None)),
        "n_logged": int(sum(r["n_logged"] for r in rows if r["n_logged"] is not None)),
        "n_missing": int(sum(r["n_missing"] for r in gaps)),
        "never_in_the_log": "raw readings: entries carry ids, status, signals (aliases), row ranges, scores and evidence ids; the readings stay in dataset.parquet",
        "seconds": round(time.perf_counter() - t0, 3),
    }


def format_audit(res: dict[str, Any]) -> list[str]:
    """Plain console lines for `tpm verify-log`."""
    out = ["", "Completeness: objects in the run's artifacts vs objects with an entry of their own in the decision log"]
    out.append(f"  {'object':<32} {'in artifacts':>12} {'logged':>8} {'missing':>8}")
    for r in res.get("rows") or []:
        if r["n_logged"] is None:
            out.append(f"  {r['label']:<32} {r['n_artifacts']:>12,} {'-':>8} {'-':>8}  {r['note']}")
            continue
        state = "complete" if r["complete"] else "see below"
        out.append(f"  {r['label']:<32} {r['n_artifacts']:>12,} {r['n_logged']:>8,} {r['n_missing']:>8,}  {state}")
    gaps = [r for r in (res.get("rows") or []) if r.get("n_missing")]
    if gaps:
        out.append("Not logged one by one, and why:")
        for r in gaps:
            for g in r["excluded"]:
                ex = ", ".join(g["examples"]) + (", ..." if g["n"] > len(g["examples"]) else "")
                out.append(f"  - {r['label']}: {g['n']:,} ({ex}): {g['reason']}")
    else:
        out.append("Every object listed above has an entry of its own.")
    out.append(f"Never in the log, on purpose: {res.get('never_in_the_log')}.")
    out.append(f"(audit of {res.get('n_entries', 0):,} entries in {res.get('seconds', 0):.2f} s)")
    return out


def completeness_statement(res: dict[str, Any]) -> str:
    """One sentence for reports / the UI."""
    if res.get("complete"):
        return f"All {res.get('n_objects', 0):,} objects of this run (flags, checks, verdicts, diagnoses, critiques, inferences, model calls) have an entry of their own in the decision log; evidence items are cited by id and raw readings are never logged."
    parts = [f"{g['n']:,} {r['label']}" for r in res.get("rows") or [] if r.get("n_missing") for g in r["excluded"]]
    return f"{res.get('n_logged', 0):,} of {res.get('n_objects', 0):,} objects have an entry of their own in the decision log; not logged one by one: " + "; ".join(parts) + " (reasons: `python -m tpm verify-log`)."


def iter_missing(res: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for r in res.get("rows") or []:
        for g in r.get("excluded") or []:
            yield r["object"], g


def audit_safe(ws: Any) -> Optional[dict[str, Any]]:
    try:
        return audit(ws)
    except Exception:
        return None
