"""Assessor actions: parse a natural-language question into a structured action, evaluate it against the
evidence (decision 40: evaluate anything, recommend only when the evidence supports it), assess an uploaded
file, and apply an approved action (decision 42: apply only after human approval, always logged).

Action shape: {"type": <ACTION_TYPES>, "params": {...}, "confidence": 0..1, "source": "template"|"llm-local:..."}
    drop_signal      {"signals": ["S05"]}
    drop_group       {"group_ids": ["12"]}
    drop_range       {"row_start": int, "row_end": int}         (inclusive rows)
    drop_duplicates  {}
    downsample       {"factor": int} or {"fraction": float}
    drop_regime      {"regime_id": "R2"}
    add_file         {"path": "..."}
    add_more_like    {"n_units": int|None, "unit": "runs"|"rows"|...}
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ..contracts import now_iso
from ..quality._common import GROUP_COL, ROW_COL, finite_sql, is_problem, load_catalog, numeric_signals, quote_ident, time_column_in
from .coverage import coverage_signals, project_units, regime_coverage, unit_definition
from .fitness import compare_with_without, learning_curve
from .scores import CATEGORIES, compute_dq_scores, worst_signals

ACTION_TYPES = ["drop_signal", "drop_group", "drop_range", "drop_duplicates", "downsample", "drop_regime", "add_file", "add_more_like"]
ACTION_SCHEMA = {"type": "object", "properties": {"type": {"enum": ACTION_TYPES}, "params": {"type": "object"}}, "required": ["type", "params"], "description": "Structured data-curation action. Signals are aliases like S05. Return {\"type\": null} when the question is not about adding or removing data."}
_DROP = r"(?:drop|dropping|dropped|remov\w*|exclud\w*|delet\w*|discard\w*|omit\w*|ignor\w*|leav\w* out|get rid of|without|cut\w*|take out|filter\w* out|disabl\w*|skip\w*)"
_SIG = r"\b[sS](\d{1,3})\b"


def _sig_list(text: str) -> list[str]:
    out = []
    for m in re.finditer(_SIG, text):
        a = f"S{int(m.group(1)):02d}"
        if a not in out:
            out.append(a)
    return out


def _canon(aliases: list[str], catalog: list[Any]) -> list[str]:
    known = {s.alias for s in catalog}
    by_num = {int(re.sub(r"\D", "", s.alias) or 0): s.alias for s in catalog if re.sub(r"\D", "", s.alias)}
    out = []
    for a in aliases:
        if a in known:
            out.append(a)
        else:
            n = int(re.sub(r"\D", "", a) or 0)
            out.append(by_num.get(n, a))
    return out


def parse_action(text: str, ws: Any = None, settings: Any = None, use_llm: bool = True) -> Optional[dict[str, Any]]:
    """Template patterns first; optional LLM (task 'assessor_chat', routed by the profile) when nothing matches."""
    t = " " + re.sub(r"\s+", " ", text.strip().lower()) + " "
    catalog = load_catalog(ws) if ws is not None else []
    sigs = _canon(_sig_list(text), catalog) if catalog else _sig_list(text)
    m_num = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:%|percent)\b", t)
    # add_file
    m = re.search(r"([\w\-./\\:~]+\.(?:csv|tsv|txt|dat|parquet|xlsx|xls|json|jsonl))", text, re.I)
    if m and re.search(r"\b(add|upload|includ|merg|append|import|use|load|attach|new file|this file|the file)", t):
        return normalize_action({"type": "add_file", "params": {"path": m.group(1)}, "confidence": 0.9, "source": "template"}, catalog)
    # duplicates
    if re.search(r"\bduplicat|\bdedup|\bdouble rows|\brepeated rows", t):
        return normalize_action({"type": "drop_duplicates", "params": {}, "confidence": 0.9, "source": "template"}, catalog)
    # downsample / less data
    m = re.search(r"\b(?:every|each)\s+(\d+)(?:st|nd|rd|th)?\s+(?:row|sample|record|line)", t) or re.search(r"\b(?:downsampl\w*|subsampl\w*|thin\w*|decimat\w*)\s+(?:by|to)?\s*(?:a factor of\s*)?(\d+)\b", t)
    if m:
        return normalize_action({"type": "downsample", "params": {"factor": int(m.group(1))}, "confidence": 0.85, "source": "template"}, catalog)
    if re.search(r"\bhalf (?:of )?the (?:data|rows|samples)|\bhalve\b", t):
        return normalize_action({"type": "downsample", "params": {"factor": 2}, "confidence": 0.8, "source": "template"}, catalog)
    if m_num and re.search(r"\b(?:only|just|keep|use|with|retain)\b", t) and not re.search(r"\b(?:more|add)\b", t):
        return normalize_action({"type": "downsample", "params": {"fraction": float(m_num.group(1)) / 100.0}, "confidence": 0.7, "source": "template"}, catalog)
    if re.search(r"\b(?:downsampl|subsampl|less data|fewer (?:rows|samples|records|runs|groups)|reduce the data|smaller dataset|shrink)", t):
        return normalize_action({"type": "downsample", "params": {"factor": 2}, "confidence": 0.6, "source": "template"}, catalog)
    # regime / group / range / signal drops
    m = re.search(rf"{_DROP}\s+(?:the\s+)?(?:operating\s+)?regime\s+(r?\d+)", t)
    if m:
        rid = m.group(1).upper()
        return normalize_action({"type": "drop_regime", "params": {"regime_id": rid if rid.startswith("R") else f"R{rid}"}, "confidence": 0.85, "source": "template"}, catalog)
    m = re.search(rf"{_DROP}\s+(?:the\s+)?(?:group|run|batch|unit|segment|lot|campaign)s?\s+((?:[\w\-]+)(?:\s*,\s*[\w\-]+|\s+and\s+[\w\-]+)*)", t)
    if m:
        ids = [g for g in re.split(r"\s*,\s*|\s+and\s+", m.group(1)) if g]
        return normalize_action({"type": "drop_group", "params": {"group_ids": ids}, "confidence": 0.85, "source": "template"}, catalog)
    m = re.search(rf"{_DROP}\s+(?:the\s+)?rows?\s+(\d+)\s*(?:-|to|through|\.\.|until)\s*(\d+)", t)
    if m:
        return normalize_action({"type": "drop_range", "params": {"row_start": int(m.group(1)), "row_end": int(m.group(2))}, "confidence": 0.85, "source": "template"}, catalog)
    m = re.search(rf"{_DROP}\s+(?:the\s+)?(?:first|initial)\s+(\d+)\s+rows?", t)
    if m:
        return normalize_action({"type": "drop_range", "params": {"row_start": 0, "row_end": int(m.group(1)) - 1}, "confidence": 0.8, "source": "template"}, catalog)
    m = re.search(rf"{_DROP}\s+(?:the\s+)?rows?\s+(?:before|prior to)\s+(?:row\s+)?(\d+)", t)
    if m:
        return normalize_action({"type": "drop_range", "params": {"row_start": 0, "row_end": int(m.group(1)) - 1}, "confidence": 0.8, "source": "template"}, catalog)
    if sigs and re.search(_DROP, t):
        return normalize_action({"type": "drop_signal", "params": {"signals": sigs}, "confidence": 0.9, "source": "template"}, catalog)
    # more data
    m = re.search(r"\b(?:add|adding|collect\w*|gather\w*|get|record\w*|with|obtain\w*|acquir\w*|includ\w*|another|extra|additional)\s+(?:\w+\s+)?(\d+)\s+(?:more\s+|additional\s+|extra\s+|new\s+)?(runs?|groups?|batches|batch|files?|days?|hours?|weeks?|rows?|samples?|records?|campaigns?|lots?|units?)\b", t) or re.search(r"\b(\d+)\s+(?:more|additional|extra|new)\s+(runs?|groups?|batches|batch|files?|days?|hours?|weeks?|rows?|samples?|records?|campaigns?|lots?|units?)\b", t)
    if m:
        return normalize_action({"type": "add_more_like", "params": {"n_units": int(m.group(1)), "unit": m.group(2).rstrip("s") if not m.group(2).endswith("ies") else m.group(2)}, "confidence": 0.85, "source": "template"}, catalog)
    if re.search(r"\b(?:more data|more (?:runs|groups|batches|samples|rows|records|files)|additional data|extra data|collect more|gather more|double the data|twice as much|larger dataset|bigger dataset|add data)\b", t):
        return normalize_action({"type": "add_more_like", "params": {"n_units": None, "unit": "unit"}, "confidence": 0.7, "source": "template"}, catalog)
    if sigs and re.search(r"\b(?:improve|better|worse|help|quality|useful|matter|important|need)\b", t) and re.search(r"\b(?:is|are|does|do|would|should)\b", t):
        return normalize_action({"type": "drop_signal", "params": {"signals": sigs}, "confidence": 0.5, "source": "template", "note": "interpreted as a question about the value of the signal"}, catalog)
    # optional LLM fallback (local model, or the external one behind the egress guard when the profile routes it there)
    if use_llm and ws is not None and settings is not None and settings.route_for("assessor_chat") in ("local", "external"):
        try:
            from ..llm import complete

            res = complete("assessor_chat", {"question": text, "action_types": ACTION_TYPES, "signal_aliases": [s.alias for s in catalog][:200], "instructions": "Map the question onto ONE structured action or {\"type\": null}."}, purpose="parse assessor question into a structured action", ws=ws, settings=settings, schema=ACTION_SCHEMA)
            if res.ok:
                data = res.data if isinstance(res.data, dict) else _json_in(res.text)
                if isinstance(data, dict) and isinstance(data.get("action"), dict):
                    data = data["action"]
                if isinstance(data, dict) and data.get("type") in ACTION_TYPES:
                    return normalize_action({"type": data["type"], "params": data.get("params") or {k: v for k, v in data.items() if k != "type"}, "confidence": 0.6, "source": res.source or "llm-local"}, catalog)
        except Exception:
            pass
    return None


def normalize_action(action: Any, catalog: Optional[list[Any]] = None) -> Optional[dict[str, Any]]:
    """Validate/normalise an action (template or LLM produced). Returns None when it is not a usable action."""
    if not isinstance(action, dict) or action.get("type") not in ACTION_TYPES:
        return None
    typ = action["type"]
    p = dict(action.get("params") or {})
    out: dict[str, Any] = {}
    if typ == "drop_signal":
        raw = p.get("signals") or p.get("signal") or p.get("signal_alias") or []
        raw = [raw] if isinstance(raw, str) else list(raw)
        sigs = [f"S{int(m.group(1)):02d}" for r in raw for m in [re.fullmatch(r"\s*[sS](\d{1,3})\s*", str(r))] if m]
        if not sigs:
            return None
        out["signals"] = _canon(sigs, catalog) if catalog else sigs
    elif typ == "drop_group":
        raw = p.get("group_ids") or p.get("group_id") or p.get("groups") or p.get("run") or []
        raw = [raw] if isinstance(raw, (str, int)) else list(raw)
        ids = [str(g).strip() for g in raw if str(g).strip()]
        if not ids:
            return None
        out["group_ids"] = ids
    elif typ == "drop_range":
        try:
            a, b = int(p.get("row_start", p.get("start"))), int(p.get("row_end", p.get("end")))
        except (TypeError, ValueError):
            return None
        if b < a:
            return None
        out["row_start"], out["row_end"] = a, b
    elif typ == "downsample":
        f, fr = p.get("factor"), p.get("fraction")
        try:
            if f is not None and int(f) >= 1:
                out["factor"] = int(f)
            elif fr is not None and 0 < float(fr) <= 1:
                out["fraction"] = float(fr)
            else:
                return None
        except (TypeError, ValueError):
            return None
    elif typ == "drop_regime":
        rid = str(p.get("regime_id") or p.get("regime") or "").strip().upper()
        if not re.fullmatch(r"R?\d+", rid):
            return None
        out["regime_id"] = rid if rid.startswith("R") else f"R{rid}"
    elif typ == "add_file":
        path = p.get("path") or p.get("file_path") or p.get("file")
        if not path or not re.search(r"\.(csv|tsv|txt|dat|parquet|xlsx|xls|json|jsonl)$", str(path), re.I):
            return None
        out["path"] = str(path)
    elif typ == "add_more_like":
        n = p.get("n_units", p.get("n"))
        try:
            out["n_units"] = int(n) if n is not None else None
        except (TypeError, ValueError):
            out["n_units"] = None
        out["unit"] = str(p.get("unit") or "unit")
    return {"type": typ, "params": out, "confidence": float(action.get("confidence", 0.5)), "source": str(action.get("source", "template")), **({"note": action["note"]} if action.get("note") else {})}


def _json_in(text: str) -> Optional[dict[str, Any]]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------- state
def _state(ws: Any, settings: Any, need_coverage: bool = True) -> dict[str, Any]:
    checks = ws.checks()
    verdicts = ws.trust()
    batches = ws.read_json("batches", []) or []
    catalog = load_catalog(ws)
    n_signals = len(numeric_signals(catalog)) or max(1, len({s for c in checks for s in c.signals}))
    n_batches = len(batches) or len({c.batch_id for c in checks}) or 1
    assessor = ws.read_json("assessor", None) or {}
    coverage = assessor.get("coverage")
    if need_coverage and (not coverage or "unit_regime" not in coverage) and ws.exists("dataset"):
        try:
            coverage = regime_coverage(ws, settings, catalog)
        except Exception as e:
            ws.log.record("system:assessor", "warning", "assessor", "coverage", {"error": str(e)[:300]})
            coverage = None
    return {"checks": checks, "verdicts": verdicts, "batches": batches, "catalog": catalog, "n_signals": n_signals, "n_batches": n_batches, "coverage": coverage, "fitness": assessor.get("fitness"), "dq": compute_dq_scores(checks, verdicts, n_batches, n_signals, settings)}


def _delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    keys = CATEGORIES + ["overall", "mean_trust"]
    out: dict[str, Any] = {}
    for k in keys:
        b, a = before.get(k), after.get(k)
        # a category that could not be tested (score None, e.g. timeliness without a time column) has no delta
        out[k] = {"before": b, "after": a, "delta": round(float(a) - float(b), 4) if (a is not None and b is not None) else 0.0}
    return out


def _fmt_delta(d: dict[str, Any]) -> str:
    parts = [f"{k} {v['before']:.2f} -> {v['after']:.2f}" for k, v in d.items() if v["delta"] and abs(v["delta"]) >= 0.005 and k != "mean_trust"]
    return "; ".join(parts) if parts else "no measurable change in the data-quality scores"


def _result(action: dict[str, Any], rec: str, rationale: str, effect: dict[str, Any], ev: list[str], confidence: float, **extra: Any) -> dict[str, Any]:
    return {"action": action, "recommendation": rec, "rationale": rationale, "expected_effect": effect, "evidence_ids": ev, "confidence": round(float(confidence), 3), **extra}


# ---------------------------------------------------------------- evaluation
def evaluate_action(ws: Any, settings: Any, action: dict[str, Any], time_budget_s: Optional[float] = None) -> dict[str, Any]:
    t0 = time.time()
    budget = float(time_budget_s if time_budget_s is not None else settings.assessor.experiment_time_budget_s)
    typ = action.get("type")
    params = action.get("params", {}) or {}
    st = _state(ws, settings, need_coverage=typ in ("drop_signal", "drop_group", "drop_regime", "add_more_like", "add_file", "downsample"))
    ev_ids: list[str] = []
    handler = {"drop_signal": _eval_drop_signal, "drop_group": _eval_drop_group, "drop_range": _eval_drop_range, "drop_duplicates": _eval_drop_duplicates, "downsample": _eval_downsample, "drop_regime": _eval_drop_regime, "add_more_like": _eval_add_more, "add_file": _eval_add_file}.get(typ)
    if handler is None:
        return _result(action, "neutral", f"Unknown action type {typ!r}; I can evaluate: {', '.join(ACTION_TYPES)}.", {}, [], 0.0)
    try:
        out = handler(ws, settings, action, params, st, budget, ev_ids)
    except Exception as e:  # never break the chat
        out = _result(action, "neutral", f"The evaluation failed ({e}); no recommendation.", {}, ev_ids, 0.0, error=str(e))
    out["seconds"] = round(time.time() - t0, 2)
    ev = ws.evidence.add("assessor_evaluation", f"Evaluated action {typ} {json.dumps(params, default=str)[:200]}: {out['recommendation']} - {out['rationale'][:300]}", values={"action": action, "recommendation": out["recommendation"], "expected_effect": out.get("expected_effect", {})}, computed_by="assessor.actions.evaluate_action")
    out["evidence_ids"] = list(dict.fromkeys(out.get("evidence_ids", []) + [ev.id]))
    ws.log.record("system:assessor", "evaluate_action", "assessor", typ or "unknown", {"params": params, "recommendation": out["recommendation"], "confidence": out["confidence"], "seconds": out["seconds"]}, out["evidence_ids"][:20])
    return out


def _eval_drop_signal(ws, settings, action, params, st, budget, ev_ids):
    sigs = _canon(list(params.get("signals") or []), st["catalog"])
    known = {s.alias for s in st["catalog"]}
    unknown = [s for s in sigs if s not in known]
    if unknown:
        return _result(action, "neutral", f"Unknown signal(s) {', '.join(unknown)}; the catalog has {len(known)} signals.", {}, [], 0.2)
    cov = st["coverage"] or {}
    low = set((cov.get("signals") or {}).get("low_information") or [])
    near_const = set((cov.get("signals") or {}).get("near_constant") or [])
    redundant = {p["b"]: p["a"] for p in (cov.get("signals") or {}).get("redundant_pairs") or []}
    redundant.update({p["a"]: p["b"] for p in (cov.get("signals") or {}).get("redundant_pairs") or []})
    worst = {w["signal"]: w for w in worst_signals(st["checks"], st["verdicts"], top=1000)}
    reasons, ev = [], []
    for s in sigs:
        w = worst.get(s)
        if w and (w["untrusted_batches"] > 0 or w["severity_sum"] > 0):
            reasons.append(f"{s} has data-quality problems ({', '.join(w['types'])}) in {w['untrusted_batches']} of {st['n_batches']} batches")
            ev.extend(c.evidence_ids[0] for c in st["checks"] if s in c.signals and is_problem(c.status) and c.evidence_ids)
        if s in near_const:
            reasons.append(f"{s} is (almost) constant and carries no information")
        elif s in redundant:
            reasons.append(f"{s} is redundant with {redundant[s]} (|r| >= 0.98)")
    ev.extend(cov.get("evidence_ids") or [])
    after = compute_dq_scores(st["checks"], st["verdicts"], st["n_batches"], st["n_signals"], settings, exclude_signals=sigs)
    eff = {"dq_scores": _delta(st["dq"], after), "signals_remaining": st["n_signals"] - len(sigs)}
    fit = None
    if budget > 4 and ws.exists("dataset"):
        try:
            fit = compare_with_without(ws, settings, sigs, cov, time_budget_s=min(budget * 0.6, 60))
        except Exception as e:
            fit = {"available": False, "reason": str(e)[:200]}
    eff["fitness"] = fit
    d_fit = fit.get("delta") if fit and fit.get("available") else None
    fit_txt = f"; model stability ({fit['primary_metric']}) {fit['before']:.3f} -> {fit['after']:.3f} without it" if d_fit is not None else ""
    if reasons and (d_fit is None or d_fit >= -0.02):
        return _result(action, "recommend", f"Yes: {'; '.join(reasons)}. Expected effect: {_fmt_delta(eff['dq_scores'])}{fit_txt}.", eff, ev, 0.75 if d_fit is not None else 0.6)
    if reasons:
        return _result(action, "neutral", f"Mixed: {'; '.join(reasons)}, but the detector gets less stable without it{fit_txt}. Consider excluding it only in the affected batches (the trust verdicts already do that).", eff, ev, 0.5)
    if d_fit is not None and d_fit > 0.02:
        return _result(action, "neutral", f"No data-quality issue was found on {', '.join(sigs)}, yet the model is slightly more stable without it{fit_txt}. Evidence is weak; keep the signal unless a domain reason exists.", eff, ev, 0.4)
    return _result(action, "advise_against", f"No: no data-quality issue was found on {', '.join(sigs)} in any of {st['n_batches']} batches and it is neither constant nor redundant; dropping it removes information without improving quality{fit_txt}.", eff, ev, 0.7)


def _eval_drop_group(ws, settings, action, params, st, budget, ev_ids):
    gids = [str(g) for g in params.get("group_ids") or []]
    batches = st["batches"]
    touched = [b for b in batches if any(g in (b.get("group_ids") or []) for g in gids)]
    if not touched:
        return _result(action, "neutral", f"Group(s) {', '.join(gids)} not found in the batch map ({len(batches)} batches).", {}, [], 0.2)
    bids = {b["batch_id"] for b in touched}
    verdicts = [v for v in st["verdicts"] if v.batch_id in bids]
    untrusted = [v for v in verdicts if not v.trusted]
    fails = [c for c in st["checks"] if c.batch_id in bids and c.status == "fail" and c.category != "rule"]
    ev = [v_e for c in fails for v_e in c.evidence_ids][:20]
    # batches shared with other groups cannot simply be excluded from the score: approximate by excluding batches whose groups are all dropped
    only = {b["batch_id"] for b in touched if set(b.get("group_ids") or []) <= set(gids)}
    after = compute_dq_scores(st["checks"], st["verdicts"], st["n_batches"], st["n_signals"], settings, exclude_batches=only)
    cov = st["coverage"] or {}
    regime_of = cov.get("unit_regime") or {}
    regimes = sorted({regime_of.get(g) for g in gids if regime_of.get(g)})
    thin = [r for r in regimes if r in (cov.get("thin_regimes") or [])]
    eff = {"dq_scores": _delta(st["dq"], after), "batches_affected": sorted(bids), "regimes": regimes}
    if untrusted or len(fails) >= 3:
        why = f"{len(untrusted)} of {len(verdicts)} batches covering group(s) {', '.join(gids)} are untrusted and {len(fails)} checks fail there"
        if thin:
            return _result(action, "neutral", f"Mixed: {why}, but the group is one of the few examples of regime {', '.join(thin)}; dropping it would leave that regime uncovered. Prefer excluding only the untrusted signals.", eff, ev, 0.5)
        return _result(action, "recommend", f"Yes: {why}. Expected effect: {_fmt_delta(eff['dq_scores'])}.", eff, ev, 0.7)
    txt = f"No: the batches covering group(s) {', '.join(gids)} are trusted ({len(fails)} failing checks)"
    if thin:
        txt += f" and the group belongs to the thin regime {', '.join(thin)}: it is valuable for coverage"
    return _result(action, "advise_against", txt + ".", eff, ev + list(cov.get("evidence_ids") or []), 0.7)


def _eval_drop_range(ws, settings, action, params, st, budget, ev_ids):
    a, b = int(params.get("row_start", 0)), int(params.get("row_end", -1))
    if b < a:
        return _result(action, "neutral", "The row range is empty.", {}, [], 0.2)
    inside = [c for c in st["checks"] if is_problem(c.status) and c.category != "rule" and c.row_start is not None and c.row_end is not None and c.row_start >= a and c.row_end <= b]
    overl = [c for c in st["checks"] if is_problem(c.status) and c.category != "rule" and c.row_start is not None and c.row_end is not None and not (c.row_end < a or c.row_start > b)]
    after = compute_dq_scores(st["checks"], st["verdicts"], st["n_batches"], st["n_signals"], settings, exclude_rows=(a, b))
    eff = {"dq_scores": _delta(st["dq"], after), "rows_removed": b - a + 1, "checks_inside": len(inside), "checks_overlapping": len(overl)}
    ev = [e for c in overl for e in c.evidence_ids][:20]
    fails = [c for c in inside if c.status == "fail"]
    if fails:
        return _result(action, "recommend", f"Yes: rows {a}-{b} contain {len(fails)} failing check(s) ({', '.join(sorted({c.check_type for c in fails}))}). Expected effect: {_fmt_delta(eff['dq_scores'])}.", eff, ev, 0.7)
    if overl:
        return _result(action, "neutral", f"Partly: {len(overl)} problem(s) overlap rows {a}-{b} but extend beyond them; dropping the range alone does not remove them. Consider the exact ranges in the checks.", eff, ev, 0.5)
    return _result(action, "advise_against", f"No: no data-quality problem was found in rows {a}-{b}; dropping {b - a + 1} rows loses data for nothing.", eff, ev, 0.7)


def _eval_drop_duplicates(ws, settings, action, params, st, budget, ev_ids):
    dups = [c for c in st["checks"] if c.check_type in ("duplicate_rows", "duplicate_key") and is_problem(c.status)]
    n = sum(int((c.values or {}).get("n", 0)) for c in dups if c.check_type == "duplicate_rows")
    total = sum(int(b.get("n_rows", 0)) for b in st["batches"]) or 1
    after = compute_dq_scores(st["checks"], st["verdicts"], st["n_batches"], st["n_signals"], settings, exclude_types=("duplicate_rows", "duplicate_key"))
    eff = {"dq_scores": _delta(st["dq"], after), "rows_removed": n, "fraction": round(n / total, 5), "batches": sorted({c.batch_id for c in dups})}
    ev = [e for c in dups for e in c.evidence_ids][:20]
    if n > 0:
        return _result(action, "recommend", f"Yes: {n} exact duplicate rows ({n / total:.2%}) were found in {len(eff['batches'])} batch(es); removing them is safe and lifts consistency from {st['dq']['consistency']:.2f} to {after['consistency']:.2f}.", eff, ev, 0.85)
    return _result(action, "neutral", "No duplicate rows were found, so there is nothing to remove.", eff, ev, 0.8)


def _eval_downsample(ws, settings, action, params, st, budget, ev_ids):
    factor = params.get("factor")
    frac = params.get("fraction")
    keep = (1.0 / float(factor)) if factor else (float(frac) if frac else 0.5)
    keep = max(0.01, min(1.0, keep))
    fit = st["fitness"]
    if not fit or not fit.get("available"):
        fit = learning_curve(ws, settings, coverage=st["coverage"], time_budget_s=min(budget, 60)) if ws.exists("dataset") else None
    ev = list((fit or {}).get("evidence_ids") or [])
    eff = {"kept_fraction": keep, "fitness": None}
    if not fit or not fit.get("available"):
        return _result(action, "neutral", "No learning curve is available, so the effect of using less data cannot be estimated.", eff, ev, 0.3)
    xs = [p["fraction"] for p in fit["curve"] if p["primary"] is not None]
    ys = [p["primary"] for p in fit["curve"] if p["primary"] is not None]
    at = float(np.interp(keep, xs, ys)) if len(xs) >= 2 else None
    full = ys[-1] if ys else None
    eff["fitness"] = {"primary_metric": fit["primary_metric"], "full": full, "at_kept_fraction": at, "diminishing_returns_fraction": fit.get("diminishing_returns_fraction")}
    if at is None or full is None:
        return _result(action, "neutral", "The learning curve has too few points to estimate the effect.", eff, ev, 0.3)
    loss = full - at
    if loss <= 0.01:
        return _result(action, "neutral", f"Keeping {keep:.0%} of the data costs little: {fit['primary_metric']} {full:.3f} -> {at:.3f}; the curve flattens from {fit.get('diminishing_returns_fraction') or xs[-1]:.0%}. Fine for faster iteration, but it does not improve quality by itself.", eff, ev, 0.6)
    return _result(action, "advise_against", f"No: keeping {keep:.0%} of the data would lower {fit['primary_metric']} from {full:.3f} to about {at:.3f}; the learning curve is still rising.", eff, ev, 0.7)


def _eval_drop_regime(ws, settings, action, params, st, budget, ev_ids):
    rid = str(params.get("regime_id", "")).upper()
    cov = st["coverage"] or {}
    reg = next((r for r in cov.get("regimes", []) if r["regime_id"] == rid), None)
    if reg is None:
        return _result(action, "neutral", f"Regime {rid} does not exist; known regimes: {', '.join(r['regime_id'] for r in cov.get('regimes', []))}.", {}, list(cov.get("evidence_ids") or []), 0.2)
    units = [u for u, r in (cov.get("unit_regime") or {}).items() if r == rid]
    bids = {b["batch_id"] for b in st["batches"] if cov.get("unit", {}).get("kind") == "group" and set(b.get("group_ids") or []) and set(b.get("group_ids") or []) <= set(units)}
    verdicts = [v for v in st["verdicts"] if v.batch_id in bids]
    untrusted = [v for v in verdicts if not v.trusted]
    after = compute_dq_scores(st["checks"], st["verdicts"], st["n_batches"], st["n_signals"], settings, exclude_batches=bids)
    eff = {"dq_scores": _delta(st["dq"], after), "units_removed": len(units), "share": reg["share"], "batches": sorted(bids)}
    ev = list(cov.get("evidence_ids") or [])
    if verdicts and len(untrusted) >= max(1, len(verdicts) // 2):
        return _result(action, "neutral", f"Regime {rid} ({reg['share']:.0%} of the {cov['unit']['kind']}s) is mostly untrusted data ({len(untrusted)}/{len(verdicts)} batches); dropping it removes bad data but also the only examples of that operating mode. Prefer fixing the data problems.", eff, ev, 0.5)
    return _result(action, "advise_against", f"No: regime {rid} covers {reg['share']:.0%} of the {cov['unit']['kind']}s{' and is already thin' if reg.get('thin') else ''}; removing it shrinks coverage and the model would not recognise that operating mode.", eff, ev, 0.75)


def _eval_add_more(ws, settings, action, params, st, budget, ev_ids):
    fit = st["fitness"]
    if not fit or not fit.get("available"):
        fit = learning_curve(ws, settings, coverage=st["coverage"], time_budget_s=min(budget, 90)) if ws.exists("dataset") else None
    cov = st["coverage"] or {}
    ev = list((fit or {}).get("evidence_ids") or []) + list(cov.get("evidence_ids") or [])
    n = params.get("n_units")
    pool = (fit or {}).get("n_train_pool") or cov.get("n_units") or 0
    unit_kind = (cov.get("unit") or {}).get("kind", "unit")
    if not fit or not fit.get("available") or fit.get("slope") is None:
        thin = cov.get("thin_regimes") or []
        why = f"more data from the thin regime(s) {', '.join(thin)} would improve coverage" if thin else "no learning curve could be computed"
        return _result(action, "neutral" if not thin else "recommend", f"{'Probably' if thin else 'Unclear'}: {why}.", {"fitness": None, "coverage": {"thin_regimes": thin}}, ev, 0.4)
    add_frac = (float(n) / pool) if (n and pool) else 0.5
    gain = float(max(-0.2, min(0.5, fit["slope"] * add_frac)))
    unc = float((fit.get("slope_uncertainty") or 0.0) * add_frac)
    full = fit.get("fitness_score")
    thin = cov.get("thin_regimes") or []
    eff = {"fitness": {"primary_metric": fit["primary_metric"], "now": full, "estimated_gain": round(gain, 4), "uncertainty": round(unc, 4), "added_fraction": round(add_frac, 3), "diminishing_returns_fraction": fit.get("diminishing_returns_fraction")}, "coverage": {"thin_regimes": thin}}
    what = f"{n} more {params.get('unit', unit_kind)}s" if n else "50% more data"
    if fit.get("would_help_more_data") is True and gain > unc:
        return _result(action, "recommend", f"Yes: the learning curve is still rising ({fit['primary_metric']} {full:.3f} at full data, slope {fit['slope']:+.3f}); {what} should add about {gain:+.3f} (+/-{unc:.3f}){' and should preferably come from the thin regime(s) ' + ', '.join(thin) if thin else ''}.", eff, ev, 0.7)
    if fit.get("would_help_more_data") is False:
        txt = f"Not much: the curve flattens from {fit['diminishing_returns_fraction']:.0%} of the current data" if fit.get("diminishing_returns_fraction") is not None else f"Not much: the slope at the end is only {fit['slope']:+.3f}"
        txt += f"; {what} of the same kind would add only about {gain:+.3f}."
        if thin:
            txt += f" Data from the thin regime(s) {', '.join(thin)} would still improve coverage."
            return _result(action, "neutral", txt, eff, ev, 0.6)
        return _result(action, "advise_against", txt + " Better invest in fixing the data-quality problems.", eff, ev, 0.65)
    return _result(action, "neutral", f"Unclear: the estimated gain of {what} is {gain:+.3f} with uncertainty +/-{unc:.3f}; run the assessment with a larger experiment budget for a firmer answer.", eff, ev, 0.4)


def _eval_add_file(ws, settings, action, params, st, budget, ev_ids):
    path = params.get("path")
    if not path:
        return _result(action, "neutral", "No file path given.", {}, [], 0.2)
    res = assess_new_file(ws, settings, path, time_budget_s=budget, state=st)
    return _result(action, res["recommendation"], res["rationale"], {"file": {k: v for k, v in res.items() if k not in ("rationale", "recommendation", "evidence_ids")}}, res.get("evidence_ids", []), res.get("confidence", 0.5))


# ---------------------------------------------------------------- new file
def _reader_sql(path: Path) -> Optional[str]:
    p = path.as_posix().replace("'", "''")
    suf = path.suffix.lower()
    if suf == ".parquet":
        return f"read_parquet('{p}')"
    if suf in (".csv", ".tsv", ".txt", ".dat"):
        return f"read_csv_auto('{p}', sample_size=20000, ignore_errors=true)"
    if suf in (".json", ".jsonl", ".ndjson"):
        return f"read_json_auto('{p}')"
    return None


def assess_new_file(ws: Any, settings: Any, path: str | Path, time_budget_s: Optional[float] = None, state: Optional[dict[str, Any]] = None, max_rows: int = 500_000) -> dict[str, Any]:
    """Quick profile of an uploaded file: schema compatibility, expected coverage gain (projection onto the run's
    regimes) and learning-curve-based fitness gain. Reads at most ``max_rows`` rows through DuckDB."""
    t0 = time.time()
    p = Path(path)
    if not p.exists():
        return {"path": str(path), "exists": False, "compatible": False, "recommendation": "neutral", "rationale": f"File {path} does not exist.", "evidence_ids": [], "confidence": 0.1}
    st = state or _state(ws, settings, need_coverage=True)
    con = ws.duckdb()
    schema = None
    try:
        schema = ws.schema()
    except Exception:
        schema = None
    src = _reader_sql(p)
    view = "__newfile"
    try:
        if src is None:
            import pandas as pd

            pdf = pd.read_excel(p, nrows=max_rows) if p.suffix.lower() in (".xlsx", ".xls") else pd.read_csv(p, nrows=max_rows)
            con.register(view, pdf)
        else:
            con.execute(f"CREATE OR REPLACE TEMP VIEW {view} AS SELECT * FROM {src} LIMIT {int(max_rows)}")
    except Exception as e:
        return {"path": str(p), "exists": True, "compatible": False, "recommendation": "advise_against", "rationale": f"The file could not be read ({str(e)[:150]}).", "evidence_ids": [], "confidence": 0.5}
    desc = con.execute(f"DESCRIBE {view}").fetchall()
    new_cols = [(r[0], str(r[1]).upper()) for r in desc]
    n_rows = int(con.execute(f"SELECT count(*) FROM {view}").fetchone()[0])
    numeric_new = [c for c, t in new_cols if any(k in t for k in ("INT", "FLOAT", "DOUBLE", "DECIMAL", "REAL"))]
    catalog = st["catalog"]
    sigs = coverage_signals(catalog)
    # column mapping: by name (case-insensitive, also source names), else by position among numeric columns
    lower = {c.lower(): c for c, _ in new_cols}
    mapping: dict[str, str] = {}
    for s in sigs:
        for cand in (s.column, s.source_column, s.alias):
            if cand and cand.lower() in lower:
                mapping[s.alias] = lower[cand.lower()]
                break
    by_name = len(mapping)
    if by_name < 0.5 * len(sigs) and len(numeric_new) >= len(sigs):
        ds_cols = [r[0] for r in con.execute("DESCRIBE dataset").fetchall() if r[0] not in (ROW_COL, GROUP_COL)]
        ds_numeric = [c for c in ds_cols]
        pos = {c: i for i, c in enumerate(ds_numeric)}
        mapping = {}
        for s in sigs:
            i = pos.get(s.column)
            if i is not None and i < len(numeric_new):
                mapping[s.alias] = numeric_new[i]
        how = "position"
    else:
        how = "name"
    compat_share = len(mapping) / max(1, len(sigs))
    compatible = compat_share >= 0.8
    # quick DQ profile of the file
    miss = {}
    if mapping:
        exprs = ", ".join(f"avg(CASE WHEN {quote_ident(c)} IS NULL THEN 1 ELSE 0 END)" for c in mapping.values())
        row = con.execute(f"SELECT {exprs} FROM {view}").fetchone()
        miss = {a: round(float(v or 0.0), 4) for a, v in zip(mapping.keys(), row)}
    high_missing = [a for a, m in miss.items() if m >= settings.quality.missing_fail]
    # coverage gain: windows of the file projected on the regimes
    cov = st["coverage"] or {}
    coverage_gain: dict[str, Any] = {"available": False}
    if compatible and cov.get("scaler") and cov.get("centroids"):
        unit = cov.get("unit") or unit_definition(ws)
        w = unit.get("window") or max(100, int(np.median(list((cov.get("unit_sizes") or {"x": 400}).values()))))
        feats = cov["scaler"]["features"]
        aggs = []
        for f in feats:
            alias = f.split(":")[0]
            c = mapping.get(alias)
            v = finite_sql(quote_ident(c)) if c else None  # the file is read directly, not through ingest: it may hold NaN / inf
            aggs.append(f"avg({v})" if c and f.endswith(":mean") else (f"stddev_samp({v})" if c else "NULL"))
        rows = con.execute(f"SELECT u, {', '.join(aggs)} FROM (SELECT *, floor((row_number() OVER () - 1) / {int(w)}) AS u FROM {view}) GROUP BY u ORDER BY u").fetchall()
        X = np.array([[float(v) if v is not None else np.nan for v in r[1:]] for r in rows], dtype="float64") if rows else np.zeros((0, len(feats)))
        if X.shape[0]:
            ridx, dist = project_units(cov, X)
            thr = cov.get("novelty_threshold")
            novel = float((dist > thr).mean()) if thr is not None and np.isfinite(dist).any() else 0.0
            counts: dict[str, int] = {}
            for r in ridx.tolist():
                rid = f"R{int(r) + 1}"
                counts[rid] = counts.get(rid, 0) + 1
            thin_hits = [r for r in (cov.get("thin_regimes") or []) if counts.get(r)]
            coverage_gain = {"available": True, "units_new": int(X.shape[0]), "window": int(w), "regime_counts": counts, "novel_share": round(novel, 4), "adds_to_thin_regimes": thin_hits}
    # fitness gain from the learning curve
    fit = st["fitness"]
    if compatible and (not fit or not fit.get("available")) and ws.exists("dataset"):
        fit = learning_curve(ws, settings, coverage=cov, time_budget_s=min(float(time_budget_s or settings.assessor.experiment_time_budget_s), 60))
    fitness_gain: dict[str, Any] = {"available": False}
    if fit and fit.get("available") and fit.get("slope") is not None:
        pool = fit.get("n_train_pool") or 1
        units_new = coverage_gain.get("units_new") or max(1, n_rows // max(1, int((cov.get("unit") or {}).get("window") or 400)))
        add_frac = units_new / max(1, pool)
        gain = float(max(-0.2, min(0.5, fit["slope"] * add_frac)))
        fitness_gain = {"available": True, "primary_metric": fit["primary_metric"], "now": fit.get("fitness_score"), "estimated_gain": round(gain, 4), "added_fraction": round(add_frac, 3), "curve_flat": fit.get("would_help_more_data") is False}
    # verdict
    ev_ids: list[str] = []
    if not compatible:
        rec, why, conf = "advise_against", f"The file maps only {len(mapping)} of {len(sigs)} run signals ({how}); it is not compatible with this run's schema.", 0.7
    else:
        parts = [f"{n_rows} rows, {len(mapping)}/{len(sigs)} signals matched by {how}"]
        if high_missing:
            parts.append(f"{len(high_missing)} signal(s) mostly missing ({', '.join(high_missing[:5])})")
        good = False
        if coverage_gain.get("available"):
            if coverage_gain["adds_to_thin_regimes"]:
                parts.append(f"adds data to thin regime(s) {', '.join(coverage_gain['adds_to_thin_regimes'])}")
                good = True
            if coverage_gain["novel_share"] >= 0.1:
                parts.append(f"{coverage_gain['novel_share']:.0%} of its windows look like operating conditions not seen so far")
                good = True
            else:
                parts.append("its windows fall into the regimes already covered")
        if fitness_gain.get("available"):
            parts.append(f"estimated {fitness_gain['primary_metric']} gain {fitness_gain['estimated_gain']:+.3f}")
            if fitness_gain["estimated_gain"] > 0.01:
                good = True
        if good and not high_missing:
            rec, conf = "recommend", 0.7
            why = "Yes: " + "; ".join(parts) + "."
        elif high_missing:
            rec, conf = "neutral", 0.5
            why = "With care: " + "; ".join(parts) + "."
        else:
            rec, conf = "neutral", 0.55
            why = "Little gain expected: " + "; ".join(parts) + "."
    ev = ws.evidence.add("new_file_profile", f"File {p.name}: {n_rows} rows, {len(new_cols)} columns, {len(mapping)}/{len(sigs)} signals mapped by {how}; coverage gain {json.dumps(coverage_gain, default=str)[:150]}; fitness gain {json.dumps(fitness_gain, default=str)[:120]}", values={"n_rows": n_rows, "n_cols": len(new_cols), "mapped": len(mapping), "how": how, "coverage_gain": coverage_gain, "fitness_gain": fitness_gain, "missing": miss}, computed_by="assessor.actions.assess_new_file", n_samples=n_rows)
    ev_ids.append(ev.id)
    try:
        con.execute(f"DROP VIEW IF EXISTS {view}")
    except Exception:
        try:
            con.unregister(view)
        except Exception:
            pass
    out = {"path": str(p), "exists": True, "n_rows": n_rows, "n_cols": len(new_cols), "columns": [c for c, _ in new_cols][:100], "compatible": compatible, "compatibility": {"how": how, "mapped": len(mapping), "of": len(sigs), "share": round(compat_share, 3), "mapping": mapping}, "missing_rates": miss, "coverage_gain": coverage_gain, "fitness_gain": fitness_gain, "recommendation": rec, "rationale": why, "confidence": conf, "evidence_ids": ev_ids, "seconds": round(time.time() - t0, 2)}
    ws.log.record("system:assessor", "assess_new_file", "assessor", p.name, {k: out[k] for k in ("n_rows", "compatible", "recommendation")}, ev_ids)
    return out


# ---------------------------------------------------------------- apply
def apply_action(ws: Any, settings: Any, action: dict[str, Any], actor: str = "human:unknown") -> dict[str, Any]:
    """Write dataset_curated.parquet (a filtered copy) + curation.json. Only called after human approval."""
    typ = action.get("type")
    params = action.get("params", {}) or {}
    con = ws.duckdb()
    catalog = load_catalog(ws)
    cols = [r[0] for r in con.execute("DESCRIBE dataset").fetchall()]
    n_before = int(con.execute("SELECT count(*) FROM dataset").fetchone()[0])
    out_path = ws.path("dataset_curated.parquet")
    select_cols = list(cols)
    where = None
    note = ""
    if typ == "drop_signal":
        sigs = set(_canon(list(params.get("signals") or []), catalog))
        drop = {s.column for s in catalog if s.alias in sigs}
        select_cols = [c for c in cols if c not in drop]
        note = f"dropped columns {sorted(drop)}"
    elif typ == "drop_group":
        gids = [str(g).replace("'", "''") for g in params.get("group_ids") or []]
        if GROUP_COL not in cols:
            return {"applied": False, "reason": "dataset has no __group__ column"}
        where = f"CAST({quote_ident(GROUP_COL)} AS VARCHAR) NOT IN ({', '.join(chr(39) + g + chr(39) for g in gids)})"
        note = f"dropped groups {gids}"
    elif typ == "drop_range":
        a, b = int(params.get("row_start", 0)), int(params.get("row_end", -1))
        where = f"NOT ({quote_ident(ROW_COL)} BETWEEN {a} AND {b})"
        note = f"dropped rows {a}-{b}"
    elif typ == "drop_duplicates":
        schema = None
        try:
            schema = ws.schema()
        except Exception:
            schema = None
        key = [s.column for s in numeric_signals(catalog) if s.column in cols]
        tcol = time_column_in(cols, schema)
        if tcol:
            key.append(tcol)
        if not key:
            return {"applied": False, "reason": "no signal columns to deduplicate on"}
        part = ", ".join(quote_ident(c) for c in key)
        sel = ", ".join(quote_ident(c) for c in cols)
        con.execute(f"COPY (SELECT {sel} FROM (SELECT *, row_number() OVER (PARTITION BY {part} ORDER BY {quote_ident(ROW_COL)}) AS __rn FROM dataset) WHERE __rn = 1 ORDER BY {quote_ident(ROW_COL)}) TO '{out_path.as_posix()}' (FORMAT PARQUET, COMPRESSION zstd)")
        return _finish(ws, action, actor, out_path, n_before, cols, cols, "removed exact duplicate rows (first occurrence kept)")
    elif typ == "downsample":
        factor = int(params.get("factor") or round(1.0 / float(params.get("fraction") or 0.5)))
        factor = max(1, factor)
        where = f"{quote_ident(ROW_COL)} % {factor} = 0"
        note = f"kept every {factor}. row"
    elif typ == "drop_regime":
        cov = (ws.read_json("assessor", None) or {}).get("coverage") or {}
        rid = str(params.get("regime_id", "")).upper()
        units = [u for u, r in (cov.get("unit_regime") or {}).items() if r == rid]
        if not units:
            return {"applied": False, "reason": f"regime {rid} unknown or assessor.json missing"}
        expr = (cov.get("unit") or {}).get("expr") or f"CAST({quote_ident(GROUP_COL)} AS VARCHAR)"
        where = f"{expr} NOT IN ({', '.join(chr(39) + u.replace(chr(39), chr(39) * 2) + chr(39) for u in units)})"
        note = f"dropped regime {rid} ({len(units)} units)"
    elif typ == "add_file":
        p = Path(params.get("path", ""))
        src = _reader_sql(p) if p.exists() else None
        if src is None:
            return {"applied": False, "reason": f"cannot read {p}"}
        prof = assess_new_file(ws, settings, p, time_budget_s=5)
        mapping = prof.get("compatibility", {}).get("mapping", {})
        if not prof.get("compatible"):
            return {"applied": False, "reason": "file is not compatible with the run schema", "profile": prof}
        alias_col = {s.alias: s.column for s in catalog}
        sel_new = []
        for c in cols:
            if c == ROW_COL:
                sel_new.append(f"row_number() OVER () - 1 + {n_before} AS {quote_ident(ROW_COL)}")
            elif c == GROUP_COL:
                sel_new.append(f"'new:{p.stem}' AS {quote_ident(GROUP_COL)}")
            else:
                alias = next((a for a, col in alias_col.items() if col == c), None)
                nc = mapping.get(alias) if alias else None
                sel_new.append(f"{quote_ident(nc)} AS {quote_ident(c)}" if nc else f"NULL AS {quote_ident(c)}")
        sel_old = ", ".join(quote_ident(c) for c in cols)
        con.execute(f"COPY (SELECT {sel_old} FROM dataset UNION ALL BY NAME SELECT {', '.join(sel_new)} FROM {src}) TO '{out_path.as_posix()}' (FORMAT PARQUET, COMPRESSION zstd)")
        return _finish(ws, action, actor, out_path, n_before, cols, cols, f"appended rows from {p.name} as group new:{p.stem}")
    else:
        return {"applied": False, "reason": f"action {typ} cannot be applied to the data (it is advice about collecting data)"}
    sel = ", ".join(quote_ident(c) for c in select_cols)
    con.execute(f"COPY (SELECT {sel} FROM dataset{' WHERE ' + where if where else ''} ORDER BY {quote_ident(ROW_COL)}) TO '{out_path.as_posix()}' (FORMAT PARQUET, COMPRESSION zstd)")
    return _finish(ws, action, actor, out_path, n_before, cols, select_cols, note)


def _finish(ws, action, actor, out_path, n_before, cols_before, cols_after, note) -> dict[str, Any]:
    con = ws.duckdb()
    n_after = int(con.execute(f"SELECT count(*) FROM read_parquet('{out_path.as_posix()}')").fetchone()[0])
    cur = ws.read_json("curation.json", {"actions": []}) or {"actions": []}
    entry = {"action": action, "note": note, "n_rows_before": n_before, "n_rows_after": n_after, "columns_dropped": [c for c in cols_before if c not in cols_after], "path": str(out_path), "applied_by": actor, "applied_at": now_iso()}
    cur["actions"].append(entry)
    cur["current"] = entry
    ws.write_json("curation.json", cur)
    ev = ws.evidence.add("curation", f"Applied {action.get('type')}: {note}; rows {n_before} -> {n_after}, columns {len(cols_before)} -> {len(cols_after)} (dataset_curated.parquet)", values={"n_rows_before": n_before, "n_rows_after": n_after, "columns_dropped": entry["columns_dropped"]}, computed_by="assessor.actions.apply_action", n_samples=n_after)
    ws.log.record(actor, "apply_assessor_action", "assessor", str(action.get("type")), {"params": action.get("params", {}), "n_rows_before": n_before, "n_rows_after": n_after, "columns_dropped": entry["columns_dropped"], "path": str(out_path)}, [ev.id])
    return {"applied": True, **entry, "evidence_ids": [ev.id]}
