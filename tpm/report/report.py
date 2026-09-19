"""HTML report generation (agent F).

    run_report(ws, settings, ctx)            pipeline stage: writes report_<lang>.html
    generate_report(ws, settings, lang)      any language; returns the path (waits a bounded time for the model summary)
    ensure_report(ws, settings, lang)        what the API calls: cached per language, regenerated when artifacts change,
                                             never waits for the language model (it is added when it arrives)
    report_status(ws, lang)                  state of the report on disk: fresh / stale, model summary ready / pending / none
    collect(ws, settings, lang)              the template context (also useful for the API)

Template-first: every sentence is composed from the i18n dictionaries and the template report is always written
first. The optional "model-written summary" (tpm.llm.complete("report_narrative")) runs in a worker thread with a
time budget; its JSON is parsed into prose (lead paragraph, short sections, open points, references) and cached per
language in report_llm_<lang>.json. JSON is never printed: when the reply cannot be parsed the section is omitted.
Every artifact is optional: missing ones render as "not available in this run". Very large runs are capped to the
most severe rows, with the totals stated, so the HTML stays small.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Optional

from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape

from ..config import Settings, get_settings
from ..contracts import now_iso
from ..workspace import Workspace, dumps
from . import charts
from .i18n import Translator, available_languages, normalize_lang
from .prose import clean_text, detector_label, parse_narrative, strip_lead, whole_sentences

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
REPORT_VERSION = "4"  # bump when the template or the context changes shape: cached reports are then regenerated
MAX_SIGNALS = 400
MAX_FLAGS = 300
MAX_DIAGNOSES = 100
MAX_DIAG_CARDS = 30  # full cards for the most severe; the rest of the top MAX_DIAGNOSES as compact table rows
MAX_CHECK_ROWS = 300
MAX_UNTRUSTED = 100
MAX_LOG_APPENDIX = 300
MAX_TIMELINES = 24
MAX_SUSPICIOUS = 25  # rows of the headline "suspicious rows" list (HTML, PDF and deck show the same 25)
MAX_POINT_FLAGS = 25
TRUST_SERIES_POINTS = 80
TIMELINE_POINTS = 160
LLM_MAX_TOKENS = 1600  # the narrative JSON needs ~600-1000 tokens; 700 cut it mid-sentence
LLM_WAIT_S = 75.0  # blocking callers (pipeline stage, CLI) wait at most this long for the model summary
LLM_RETRY_S = 600.0  # after a failed model call, do not try again for this long (unless the artifacts change)
# artifacts whose change makes a cached report stale / a cached model summary stale
_REPORT_INPUTS = ("meta", "status", "schema", "signals", "relations", "domain", "evidence", "inferences", "checks", "trust", "batches", "rules", "scores", "flags", "patterns", "baseline", "detect_meta", "evaluation", "diagnoses", "assessor", "egress_ledger", "suspicious_rows.json", "group_scores.json")
_NARRATIVE_INPUTS = ("schema", "signals", "checks", "trust", "flags", "patterns", "diagnoses")


# ----------------------------------------------------------------------------- helpers
def report_path(ws: Workspace, lang: str) -> Path:
    return ws.dir / f"report_{normalize_lang(lang)}.html"


def _dump(obj: Any) -> Any:
    """pydantic -> dict, recursively json-safe."""
    return json.loads(dumps(obj))


def _read_typed(loader, raw_reader) -> list[dict[str, Any]]:
    """Try the typed workspace loader; fall back to the raw JSON when another agent's shape differs."""
    try:
        return [_dump(x) for x in loader()]
    except Exception:
        try:
            raw = raw_reader()
            return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []
        except Exception:
            return []


def _num(v: Any, digits: int = 2) -> str:
    try:
        if v is None:
            return ""
        if isinstance(v, bool):
            return str(v)
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return ""
        if abs(f) >= 1000 or (abs(f) < 0.01 and f != 0):
            return f"{f:.3g}"
        return f"{f:.{digits}f}".rstrip("0").rstrip(".") if not float(f).is_integer() else str(int(f))
    except Exception:
        return str(v)


def _pct(v: Any) -> str:
    try:
        return f"{100 * float(v):.0f} %"
    except Exception:
        return ""


def _thousands(v: Any, lang: str = "en") -> str:
    """12345 -> '12,345' (en) / '12 345' with a no-break space (fi, sv)."""
    try:
        out = f"{int(v):,}"
    except Exception:
        return "" if v is None else str(v)
    return out if lang == "en" else out.replace(",", "\u00a0")


def _short(s: Any, n: int = 140) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _ts(s: Any) -> str:
    s = "" if s is None else str(s)
    return s.replace("T", " ")[:19]


def _scalars(d: Any) -> list[tuple[str, Any]]:
    if not isinstance(d, dict):
        return []
    out = []
    for k, v in d.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out.append((str(k), v))
        elif isinstance(v, list) and all(isinstance(x, (str, int, float, bool)) for x in v) and len(v) <= 12:
            out.append((str(k), ", ".join(_num(x) if isinstance(x, float) else str(x) for x in v)))
    return out


def _natural_key(s: str) -> list[Any]:
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", str(s))]


def _plain_explainer():
    """tpm.api.evidence_plain.explain(evidence: dict) -> str, when that module exists (it is optional)."""
    try:
        from ..api.evidence_plain import explain  # type: ignore

        return explain if callable(explain) else None
    except Exception:  # ImportError, or a half-written module: the report must still render
        return None


def _same_text(a: str, b: str) -> bool:
    norm = lambda x: re.sub(r"[^a-z0-9]+", " ", (x or "").lower()).strip()  # noqa: E731
    return norm(a) == norm(b)


def _ev_item(ev: dict[str, Any], explain) -> dict[str, Any]:
    """One evidence statement for the template: the plain sentence first (when available), the technical one under it."""
    tech = clean_text(ev.get("statement"))
    plain = ""
    if explain is not None:
        try:
            plain = str(explain(ev) or "").strip()
        except Exception:
            plain = ""
    if plain and _same_text(plain, tech):
        plain = ""
    return {"id": ev.get("id"), "plain": plain, "technical": tech}


def _ev_items(ids: Any, evidence: dict[str, dict[str, Any]], explain, limit: int) -> list[dict[str, Any]]:
    out = []
    for e in ids or []:
        if e in evidence and len(out) < limit:
            out.append(_ev_item(evidence[e], explain))
    return out


# ----------------------------------------------------------------------------- collection
def _score_timelines(ws: Workspace, schema: Optional[dict[str, Any]], flags: list[dict[str, Any]], detect_meta: Optional[dict[str, Any]], t: Translator) -> Optional[dict[str, Any]]:
    """Per-group downsampled score series from scores.parquet via DuckDB. None when unavailable."""
    if not ws.exists("scores"):
        return None
    try:
        con = ws.duckdb()
        try:
            con = con.cursor()  # the connection is shared with API handlers; a cursor is safe to use from this thread
        except Exception:
            pass
        cols = [r[0] for r in con.execute("DESCRIBE scores").fetchall()]
        lc = {c.lower(): c for c in cols}
        score_col = next((lc[c] for c in ("score", "ensemble", "ensemble_score", "anomaly_score", "oof_score", "ensemble_raw") if c in lc), None)
        if score_col is None:
            score_col = next((c for c in cols if "score" in c.lower() and "threshold" not in c.lower()), None)
        if score_col is None:
            return None
        group_col = None
        for cand in ([schema.get("group_column")] if schema else []) + ["__group__", "group_id", "group", "run"]:
            if cand and cand in cols:
                group_col = cand
                break
        row_col = next((lc[c] for c in ("row", "row_index", "__row__", "row_id", "idx", "index") if c in lc), None)
        q = lambda c: '"' + c.replace('"', '""') + '"'  # noqa: E731
        g_expr = f"CAST({q(group_col)} AS VARCHAR)" if group_col else "'all'"
        r_expr = q(row_col) if row_col else "(row_number() OVER ()) - 1"
        sql = f"""
            WITH s AS (
              SELECT {g_expr} AS g, {r_expr} AS r, CAST({q(score_col)} AS DOUBLE) AS sc FROM scores
            ), k AS (
              SELECT g, r, sc, row_number() OVER (PARTITION BY g ORDER BY r) - 1 AS k, COUNT(*) OVER (PARTITION BY g) AS n FROM s
            )
            SELECT g, CAST(FLOOR(k * {TIMELINE_POINTS} / n) AS INTEGER) AS b, MAX(sc), MIN(r), MAX(r), MAX(n)
            FROM k GROUP BY g, b ORDER BY g, b
        """
        rows = con.execute(sql).fetchall()
        threshold = None
        if isinstance(detect_meta, dict):
            for container in (detect_meta, detect_meta.get("scoring"), detect_meta.get("final_model"), detect_meta.get("events")):
                if isinstance(container, dict):
                    th = container.get("threshold")
                    if isinstance(th, (int, float)) and not isinstance(th, bool):
                        threshold = float(th)
                        break
        if threshold is None:
            ths = sorted(float(f["threshold"]) for f in flags if isinstance(f.get("threshold"), (int, float)))
            if ths:
                threshold = ths[len(ths) // 2]
        if threshold is None and "threshold" in lc:
            try:
                threshold = float(con.execute(f"SELECT median({q(lc['threshold'])}) FROM scores").fetchone()[0])
            except Exception:
                threshold = None
        series: dict[str, list[tuple[int, float, int, int]]] = OrderedDict()
        for g, b, sc, r0, r1, n in rows:
            series.setdefault(str(g), []).append((int(b), float(sc) if sc is not None else 0.0, int(r0), int(r1)))
        flagged_groups = Counter(str(f.get("group_id")) for f in flags if f.get("group_id") is not None)
        order = sorted(series.keys(), key=lambda g: (-(flagged_groups.get(g, 0)), _natural_key(g)))
        keep = order[:MAX_TIMELINES]
        timelines = []
        for g in sorted(keep, key=_natural_key):
            pts = series[g]
            vals = [p[1] for p in pts]
            spans = []
            for f in flags:
                if str(f.get("group_id")) != g and group_col:
                    continue
                fs, fe = f.get("row_start"), f.get("row_end")
                if fs is None or fe is None:
                    continue
                idx = [i for i, p in enumerate(pts) if p[3] >= fs and p[2] <= fe]
                if idx:
                    spans.append((idx[0], idx[-1]))
            label = f"{t('group')} {g}" if group_col else t("s3_timelines")
            svg = charts.sparkline(vals, threshold=threshold, flagged=spans, label=label, x_labels=(str(pts[0][2]), str(pts[-1][3])))
            timelines.append({"group": g, "svg": svg, "n_flags": flagged_groups.get(g, 0), "max": max(vals) if vals else 0.0, "label": label, "values": [round(v, 4) for v in vals], "spans": spans, "x_labels": (str(pts[0][2]), str(pts[-1][3]))})
        return {"timelines": timelines, "threshold": threshold, "score_col": score_col, "group_col": group_col, "n_groups": len(series), "shown": len(timelines)}
    except Exception as e:  # never break the report
        return {"timelines": [], "error": str(e)[:200], "threshold": None, "n_groups": 0, "shown": 0}


def _ledger_summary(ws: Workspace, ledger: list[dict[str, Any]]) -> dict[str, Any]:
    own = {
        "n_local": sum(1 for r in ledger if r.get("route") == "local"),
        "n_external": sum(1 for r in ledger if r.get("route") == "external" and r.get("guard_result") in ("allowed", None, "")),
        "n_blocked": sum(1 for r in ledger if r.get("guard_result") == "blocked"),
        "n_fallback": sum(1 for r in ledger if r.get("guard_result") == "fallback"),
        "bytes_external": sum(int(r.get("payload_bytes") or 0) for r in ledger if r.get("route") == "external" and r.get("guard_result") == "allowed"),
        "models": sorted({str(r.get("model")) for r in ledger if r.get("model")}),
        "tasks": dict(Counter(str(r.get("task")) for r in ledger)),
    }
    try:
        from ..llm import ledger as _ledger  # agent D

        ext = _ledger.summary(ws)
        if isinstance(ext, dict):
            own["external_summary"] = _scalars(ext)
    except Exception:
        pass
    return own


def _dataflow_statement(ws: Workspace, settings: Settings, t: Translator, summ: dict[str, Any], prof) -> str:
    try:
        from ..llm import ledger as _ledger  # agent D

        try:
            s = _ledger.data_flow_statement(ws, settings, language=t.lang)
        except TypeError:
            s = _ledger.data_flow_statement(ws, settings)
        if isinstance(s, str) and s.strip():
            return s
    except Exception:
        pass
    if summ["n_external"] == 0 and summ["n_blocked"] == 0:
        return t("statement_no_egress", profile=settings.profile, n_local=summ["n_local"], local_model=settings.local_llm.model)
    return t("statement_external", profile=settings.profile, n_external=summ["n_external"], external_model=settings.external_llm.model, provider=settings.external_llm.provider, bytes=summ["bytes_external"], n_blocked=summ["n_blocked"], n_local=summ["n_local"], local_model=settings.local_llm.model)


def _signal_rows(signals: list[dict[str, Any]], evidence: dict[str, dict[str, Any]], inferences: list[dict[str, Any]], t: Translator, explain=None) -> list[dict[str, Any]]:
    by_subject: dict[str, list[dict[str, Any]]] = {}
    for inf in inferences:
        by_subject.setdefault(str(inf.get("subject")), []).append(inf)
    rows = []
    for s in signals[:MAX_SIGNALS]:
        sid = str(s.get("id"))
        ev_ids = list(s.get("evidence_ids") or [])
        ev_stmts = _ev_items(ev_ids, evidence, explain, 6)
        seen = {x["technical"] for x in ev_stmts}
        hyps = []
        for inf in by_subject.get(sid, [])[:6]:
            hyps.append({"claim": inf.get("claim", ""), "status": inf.get("status", ""), "confidence": inf.get("confidence"), "reasoning": inf.get("reasoning", ""), "alternatives": inf.get("alternatives") or [], "source": inf.get("source", "code"), "id": inf.get("id"), "human_status": inf.get("human_status")})
            for e in inf.get("evidence_ids") or []:
                if e in evidence and len(ev_stmts) < 8:
                    item = _ev_item(evidence[e], explain)
                    if item["technical"] not in seen:
                        seen.add(item["technical"])
                        ev_stmts.append(item)
        unc = []
        sc = float(s.get("structural_confidence") or 0.0)
        if sc < 0.6:
            unc.append(t("unc_low_structural"))
        if s.get("instrument_hypothesis"):
            unc.append(t("unc_instrument", conf=_pct(s.get("instrument_confidence") or 0.0)))
        alts = sorted({a for h in hyps for a in (h["alternatives"] or [])})
        if alts:
            unc.append(t("unc_alternatives", alts=", ".join(str(a) for a in alts[:5])))
        for h in hyps:
            if h["status"] in ("assumed", "uncertain"):
                unc.append(t("unc_assumed", status=h["status"]))
                break
        if not unc:
            unc.append(t("unc_none"))
        fp = s.get("fingerprint") or {}
        fp_short = {k: fp[k] for k in ("count", "mean", "std", "min", "max", "missing_fraction", "stuck_fraction", "noise_level", "autocorr_1", "dominant_period", "quantization_step") if k in fp}
        rows.append({
            "id": sid, "source_column": s.get("source_column"), "dtype": s.get("dtype"), "role": s.get("structural_role"), "role_label": t.role(s.get("structural_role")),
            "structural_confidence": sc, "instrument": s.get("instrument_hypothesis"), "instrument_confidence": s.get("instrument_confidence"), "unit_op": s.get("unit_operation_hypothesis"), "unit_op_conf": s.get("unit_operation_confidence"),
            "units": s.get("units_hypothesis"), "cluster": s.get("cluster_id"), "related": [f"{r.get('signal')} (r={_num(r.get('r'))}, lag {r.get('lag')})" for r in (s.get("related_signals") or [])[:5]],
            "evidence": ev_stmts, "evidence_ids": ev_ids[:8], "hypotheses": hyps, "uncertain": unc, "excluded": bool(s.get("excluded")), "excluded_reason": s.get("excluded_reason"), "human_role_override": s.get("human_role_override"), "fingerprint": fp_short, "confidence": s.get("confidence"),
        })
    return rows


def _human_rows(ws: Workspace, log_entries: list[dict[str, Any]], signals: list[dict[str, Any]], flags: list[dict[str, Any]], diags: list[dict[str, Any]], patterns: list[dict[str, Any]], rules: list[dict[str, Any]], inferences: list[dict[str, Any]], t: Translator) -> list[dict[str, Any]]:
    idx = {"signal": {s.get("id"): s for s in signals}, "flag": {f.get("id"): f for f in flags}, "diagnosis": {d.get("id"): d for d in diags}, "pattern": {p.get("id"): p for p in patterns}, "rule": {r.get("id"): r for r in rules}, "inference": {i.get("id"): i for i in inferences}}
    rows = []
    for e in log_entries:
        actor = str(e.get("actor", ""))
        if not actor.startswith("human:"):
            continue
        m = re.match(r"human:(?P<name>[^(]*)\((?P<role>[^)]*)\)", actor)
        name, role = (m.group("name"), m.group("role")) if m else (actor[6:], "")
        otype, oid = str(e.get("object_type")), str(e.get("object_id"))
        obj = idx.get(otype, {}).get(oid)
        payload = e.get("payload") or {}
        before = ""
        if obj:
            if otype == "signal":
                before = f"{t('structural_role')}: {t.role(obj.get('structural_role'))}" + (f"; {t('instrument_hypothesis')}: {obj.get('instrument_hypothesis')}" if obj.get("instrument_hypothesis") else "")
            elif otype == "diagnosis":
                before = f"{obj.get('fault_type')} ({t('confidence')} {_pct(obj.get('confidence'))})"
            elif otype == "flag":
                before = _short(obj.get("statement"), 90)
            elif otype == "pattern":
                before = obj.get("name") or t("unnamed")
            elif otype == "rule":
                before = f"{t('rule_status')}: {obj.get('status')}"
            elif otype == "inference":
                before = _short(obj.get("claim"), 90)
        after = ""
        nv = payload.get("new_value")
        if isinstance(nv, dict) and nv:
            after = "; ".join(f"{k}: {v}" for k, v in nv.items())
        elif nv not in (None, ""):
            after = str(nv)
        else:
            after = t.act(e.get("action"))
        rows.append({"seq": e.get("seq"), "ts": _ts(e.get("ts")), "actor": name, "role": role, "action": e.get("action"), "action_label": t.act(e.get("action")), "object_type": otype, "object_id": oid, "note": payload.get("note") or "", "before": before, "after": after})
    return rows


def _trust_series(trust: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trust score per batch for a chart: every batch when there are few, else the LOWEST score of each of
    TRUST_SERIES_POINTS consecutive buckets (so an untrusted batch is never averaged away)."""
    pts = []
    for x in trust:
        try:
            pts.append((str(x.get("batch_id") or ""), float(x.get("trust_score")), bool(x.get("trusted", True))))
        except (TypeError, ValueError):
            continue
    if len(pts) <= TRUST_SERIES_POINTS:
        return [{"label": b, "score": round(sc, 4), "trusted": tr, "n": 1} for b, sc, tr in pts]
    out = []
    n = len(pts)
    for k in range(TRUST_SERIES_POINTS):
        chunk = pts[k * n // TRUST_SERIES_POINTS : (k + 1) * n // TRUST_SERIES_POINTS]
        if chunk:
            worst = min(chunk, key=lambda c: c[1])
            out.append({"label": chunk[0][0], "score": round(worst[1], 4), "trusted": all(c[2] for c in chunk), "n": len(chunk)})
    return out


def _group_summary(ws: Workspace, flags: list[dict[str, Any]], schema: Optional[dict[str, Any]]) -> dict[str, Any]:
    """How many groups crossed the alert threshold (group_scores.json of the detect stage; falls back to the flags)."""
    gs = ws.read_json("group_scores.json")
    flagged = {str(f.get("group_id")) for f in flags if f.get("group_id") is not None}
    n_groups = int((schema or {}).get("n_groups") or 0)
    if isinstance(gs, dict) and isinstance(gs.get("groups"), list):
        groups = [g for g in gs["groups"] if isinstance(g, dict)]
        th = gs.get("threshold")
        n_over = gs.get("n_groups_over_threshold")
        if not isinstance(n_over, int):
            n_over = sum(1 for g in groups if isinstance(g.get("max_score"), (int, float)) and isinstance(th, (int, float)) and g["max_score"] >= th)
        top = sorted((g for g in groups if isinstance(g.get("max_score"), (int, float))), key=lambda g: -float(g["max_score"]))[:10]
        return {"n_groups": int(gs.get("n_groups") or len(groups) or n_groups), "n_over": int(n_over), "threshold": th if isinstance(th, (int, float)) else None, "top": [{"group": str(g.get("group")), "max_score": round(float(g["max_score"]), 3), "top_signal": g.get("top_signal"), "flagged_fraction": g.get("flagged_fraction")} for g in top], "source": "group_scores"}
    return {"n_groups": n_groups or len(flagged), "n_over": len(flagged), "threshold": None, "top": [], "source": "flags"}


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÅÄÖ])")
_ONSET_STEP_RE = re.compile(r"^\s*(?:When it started|How it propagated|Onset|Propagation)\b", re.I)


def _dir_label(t: Translator, d: Any) -> str:
    d = str(d or "")
    return t(f"dir_{d}") if d and t.has(f"dir_{d}") else d


def _row_text(a: Any, b: Any) -> str:
    if a is None:
        return ""
    return str(a) if b is None or b == a else f"{a}–{b}"


def _suspicious(ws: Workspace, point_flags: list[dict[str, Any]], evidence: dict[str, dict[str, Any]], explain, t: Translator) -> Optional[dict[str, Any]]:
    """The headline list of suspicious rows (suspicious_rows.json of the detect stage) plus the flags of kind "point"
    (isolated readings). None when the run has neither: the section is then omitted everywhere."""
    raw = ws.read_json("suspicious_rows.json")
    raw = raw if isinstance(raw, dict) else None
    if raw is None and not point_flags:
        return None
    rows = []
    for r in ((raw or {}).get("rows") or [])[:MAX_SUSPICIOUS]:
        if not isinstance(r, dict):
            continue
        sigs = [{**x, "direction_label": _dir_label(t, x.get("direction"))} for x in (r.get("signals") or []) if isinstance(x, dict)]
        sig_text = [f"{x.get('signal')} ({', '.join(p for p in (_num(x.get('deviation'), 1), _dir_label(t, x.get('direction'))) if p)})" for x in sigs[:4]]
        sources = [str(x) for x in (r.get("sources") or [])]
        rows.append({
            "row": r.get("row"), "row_end": r.get("row_end"), "row_text": _row_text(r.get("row"), r.get("row_end")), "group_id": r.get("group_id"), "batch_id": r.get("batch_id"),
            "signals": sigs[:4], "signals_text": sig_text, "sources": sources, "sources_text": ", ".join(t(f"susp_source_{x}") if t.has(f"susp_source_{x}") else x.replace("_", " ") for x in sources),
            "strength": r.get("strength"), "flag_ids": list(r.get("flag_ids") or [])[:4], "check_ids": list(r.get("check_ids") or [])[:4], "evidence_ids": list(r.get("evidence_ids") or [])[:4],
            "statement": clean_text(r.get("statement")), "explanation": clean_text(next((x.get("explanation") for x in sigs if x.get("explanation")), "")),
            "ev": _ev_items(r.get("evidence_ids"), evidence, explain, 1),
        })
    # the closing sentence every row repeats ("Glitch or manipulation: ...") is shown once, prominently, as the wording
    tails = {_SENTENCE_SPLIT_RE.split(r["statement"])[-1] for r in rows if r["statement"]}
    if len(rows) > 1 and len(tails) == 1:
        tail = tails.pop()
        for r in rows:
            if r["statement"] != tail:
                r["statement"] = r["statement"][: -len(tail)].rstrip()
    for r in rows:
        if r["explanation"] and r["explanation"].rstrip(".") in r["statement"]:
            r["explanation"] = ""  # already part of the statement
    regime = (raw or {}).get("regime") if isinstance((raw or {}).get("regime"), dict) else None
    regime_text = ""
    if regime:
        regime_text = t("susp_regime", n_points=regime.get("n_point_stretches", 0), n_sustained=regime.get("n_sustained_stretches", 0), share=_pct(regime.get("share_points") or 0))
        if regime.get("point_dominated"):
            regime_text = t("susp_point_dominated") + " " + regime_text
    pf = []
    for f in sorted(point_flags, key=lambda f: -float(f.get("severity") or 0))[:MAX_POINT_FLAGS]:
        pf.append({"id": f.get("id"), "group_id": f.get("group_id"), "batch_id": f.get("batch_id"), "row_text": _row_text(f.get("row_start"), f.get("row_end")), "score": f.get("score"), "threshold": f.get("threshold"), "severity": f.get("severity"), "confidence": f.get("confidence"), "cause_label": t.cause(f.get("likely_cause_class")), "human_status": f.get("human_status"),
                   "signals": [f"{s.get('signal')} ({_pct(s.get('contribution'))}{', ' + str(s.get('direction')) if s.get('direction') else ''})" for s in (f.get("signals_ranked") or [])[:3]], "statement": clean_text(f.get("statement")), "evidence_ids": list(f.get("evidence_ids") or [])[:4], "ev": _ev_items(f.get("evidence_ids"), evidence, explain, 1)})
    n_rows = (raw or {}).get("n_rows")
    return {
        "available": raw is not None, "headline": clean_text((raw or {}).get("headline")), "wording": clean_text((raw or {}).get("wording")) or t("susp_wording_default"),
        "regime": regime, "regime_text": regime_text, "n_rows": n_rows if isinstance(n_rows, int) else len(rows), "n_listed": (raw or {}).get("n_listed"), "cap": (raw or {}).get("cap"), "rows": rows, "shown": len(rows),
        "point_flags": pf, "n_point_flags": len(point_flags),
    }


def collect(ws: Workspace, settings: Optional[Settings] = None, lang: str = "en", use_llm: bool = False, ctx: Optional[dict[str, Any]] = None, narrative: Optional[dict[str, Any]] = None, llm_state: str = "none") -> dict[str, Any]:
    """The template context. `narrative` is a parsed model summary (see _narrative_context); with use_llm=True and no
    narrative given, the model is asked here, time-boxed (kept for callers that use collect() directly)."""
    settings = settings or ws.settings or get_settings()
    lang = normalize_lang(lang)
    t = Translator(lang)
    explain = _plain_explainer()
    meta = ws.read_json("meta", {}) or {}
    status = _dump(ws.status())
    schema = ws.read_json("schema") or None
    signals = _read_typed(ws.signals, lambda: ws.read_json("signals", []))
    relations = ws.read_json("relations")
    domain = ws.read_json("domain")
    evidence = {e.id: _dump(e) for e in ws.evidence.all()}
    inferences = [_dump(i) for i in ws.inferences.all()]
    checks = _read_typed(ws.checks, lambda: ws.read_jsonl("checks"))
    trust = _read_typed(ws.trust, lambda: ws.read_jsonl("trust"))
    batches = ws.read_json("batches")
    rules = _read_typed(ws.rules, lambda: ws.read_json("rules", []))
    flags = _read_typed(ws.flags, lambda: ws.read_jsonl("flags"))
    patterns = _read_typed(ws.patterns, lambda: ws.read_json("patterns", []))
    baseline = ws.read_json("baseline")
    detect_meta = ws.read_json("detect_meta")
    evaluation = ws.read_json("evaluation")
    diags = _read_typed(ws.diagnoses, lambda: ws.read_jsonl("diagnoses"))
    assessor = ws.read_json("assessor")
    ledger = [x for x in ws.read_jsonl("egress_ledger") if isinstance(x, dict)]
    try:
        log_entries = [_dump(e) for e in ws.log.entries(limit=10_000_000)]
        chain = ws.log.verify_chain()
    except Exception as e:
        log_entries, chain = [], {"ok": False, "checked": 0, "first_bad_seq": None, "error": str(e)}

    prof = settings.active_profile
    n_batches = 0
    if isinstance(batches, dict):
        n_batches = len(batches.get("batches") or []) or int(batches.get("n_batches") or 0)
    elif isinstance(batches, list):
        n_batches = len(batches)
    if not n_batches:
        n_batches = len({c.get("batch_id") for c in checks}) or len(trust)

    # ---- stages
    stages = []
    for s in status.get("stages") or []:
        secs = None
        try:
            from datetime import datetime

            if s.get("started_at") and s.get("finished_at"):
                secs = (datetime.fromisoformat(s["finished_at"]) - datetime.fromisoformat(s["started_at"])).total_seconds()
        except Exception:
            secs = None
        stages.append({"stage": s.get("stage"), "state": s.get("state"), "message": _short(s.get("message"), 160), "seconds": _num(secs, 1) if secs is not None else ""})

    # ---- quality
    st_counts = Counter(c.get("status") for c in checks)
    cats: dict[str, Counter] = OrderedDict()
    for cat in ("completeness", "validity", "consistency", "timeliness", "rule"):
        cats[cat] = Counter()
    for c in checks:
        cats.setdefault(str(c.get("category")), Counter())[str(c.get("status"))] += 1
    by_cat = [(t.cat(k), dict(v)) for k, v in cats.items() if sum(v.values()) > 0]
    failed = sorted([c for c in checks if c.get("status") in ("fail", "warn")], key=lambda c: (c.get("status") != "fail", -float(c.get("severity") or 0)))[:MAX_CHECK_ROWS]
    untrusted = [x for x in trust if not x.get("trusted", True)]
    rule_rows = []
    checks_by_rule = Counter(str(c.get("rule_id")) for c in checks if c.get("rule_id"))
    for r in rules:
        comp = r.get("compiled")
        rule_rows.append({"id": r.get("id"), "text": r.get("text"), "status": r.get("status"), "compiled": _short(json.dumps(comp, ensure_ascii=False), 220) if comp else "", "explanation_text": clean_text(r.get("compile_explanation")), "compile_source": r.get("compile_source"), "compile_confidence": r.get("compile_confidence"), "explanation": r.get("compile_explanation"), "n_checks": checks_by_rule.get(str(r.get("id")), 0)})
    quality = {"n_checks": len(checks), "n_pass": st_counts.get("pass", 0), "n_warn": st_counts.get("warn", 0), "n_fail": st_counts.get("fail", 0), "by_category": by_cat, "svg": charts.stacked_bars(by_cat, labels={"pass": t("pass"), "warn": t("warn"), "fail": t("fail")}), "failed": failed, "n_failed_total": sum(1 for c in checks if c.get("status") in ("fail", "warn")), "rules": rule_rows, "trust": trust[:MAX_CHECK_ROWS], "n_trust_total": len(trust), "trust_series": _trust_series(trust), "untrusted": sorted(untrusted, key=lambda x: float(x.get("trust_score") or 0))[:MAX_UNTRUSTED], "n_untrusted_total": len(untrusted), "threshold": settings.quality.trust_fail_threshold, "n_batches": n_batches}

    # ---- detect
    tl = _score_timelines(ws, schema, flags, detect_meta, t)
    flag_rows = []
    det_cache: dict[str, dict[str, Any]] = {}
    # isolated readings (kind "point") belong to the suspicious-rows list, not to the sustained events
    point_flags = [f for f in flags if str(f.get("kind")) == "point"]
    point_ids = {f.get("id") for f in point_flags}
    sustained = [f for f in flags if str(f.get("kind")) != "point"] if point_flags else flags
    for f in sorted(sustained, key=lambda f: -float(f.get("severity") or 0))[:MAX_FLAGS]:
        det_key = str(f.get("detector") or "")
        if det_key not in det_cache:
            det_cache[det_key] = detector_label(det_key, t)
        flag_rows.append({**f, "statement": clean_text(f.get("statement")), "kind_label": t.kind(f.get("kind")), "cause_label": t.cause(f.get("likely_cause_class")), "det": det_cache[det_key], "signals": [f"{s.get('signal')} ({_pct(s.get('contribution'))}{', ' + str(s.get('direction')) if s.get('direction') else ''})" for s in (f.get("signals_ranked") or [])[:5]], "ev": _ev_items(f.get("evidence_ids"), evidence, explain, 1 if len(sustained) > MAX_FLAGS else 3)})
    flags_by_kind = [(t.kind(k), n) for k, n in Counter(str(f.get("kind")) for f in sustained).most_common(8)]
    flags_by_cause = [(t.cause(k), n) for k, n in Counter(str(f.get("likely_cause_class")) for f in sustained if f.get("likely_cause_class")).most_common(8)]
    suspicious = _suspicious(ws, point_flags, evidence, explain, t)
    detect = {"flags_by_kind": flags_by_kind, "flags_by_cause": flags_by_cause, "detector_legend": [d for d in det_cache.values() if d["full"] and d["short"] != d["full"]], "baseline": baseline, "baseline_items": _scalars(baseline), "baseline_assumptions": (baseline or {}).get("assumptions") if isinstance(baseline, dict) else None, "detect_meta": detect_meta, "detect_items": _scalars(detect_meta), "timelines": tl, "flags": flag_rows, "n_flags": len(sustained), "n_point_flags": len(point_flags), "group_summary": _group_summary(ws, sustained, schema), "patterns": [{**p, "name_label": p.get("name") or t("unnamed"), "signature_text": _short(json.dumps(p.get("signature"), ensure_ascii=False), 200)} for p in patterns], "threshold": (tl or {}).get("threshold")}

    # ---- diagnoses
    diag_rows = []
    flag_sev = {f.get("id"): float(f.get("severity") or 0) for f in flags}
    diag_sev = {id(d): max([flag_sev.get(i, 0.0) for i in (d.get("flag_ids") or [])] or [0.0]) for d in diags}
    diags_sorted = sorted(diags, key=lambda d: (-diag_sev[id(d)], -float(d.get("confidence") or 0))) if len(diags) > MAX_DIAGNOSES else diags
    for i_d, d in enumerate(diags_sorted[:MAX_DIAGNOSES]):
        if i_d >= MAX_DIAG_CARDS:  # compact row: what, where, how sure, and the first whole sentences of the summary
            crit_c = d.get("critique") or None
            diag_rows.append({"id": d.get("id"), "compact": True, "group_id": d.get("group_id"), "pattern_id": d.get("pattern_id"), "flag_ids": (d.get("flag_ids") or [])[:6], "fault_type": d.get("fault_type"), "cause_class": d.get("cause_class"), "cause_label": t.cause(d.get("cause_class")), "confidence": d.get("confidence"), "severity": diag_sev[id(d)], "critique": {"verdict": crit_c.get("verdict")} if crit_c else None, "verdict_label": t.verdict(crit_c.get("verdict")) if crit_c else "", "human_status": d.get("human_status"), "summary": whole_sentences(clean_text(d.get("summary")), 260) or _short(clean_text(d.get("summary")), 260)})
            continue
        ranked = d.get("ranked_signals") or []
        svg = charts.hbars([(str(s.get("signal")), float(s.get("contribution") or 0), f"{_pct(s.get('contribution'))}{' ' + str(s.get('direction')) if s.get('direction') else ''}") for s in ranked[:8]])
        crit = d.get("critique") or None
        if crit:
            crit = {**crit, "objections": [x for x in (clean_text(o) for o in (crit.get("objections") or [])) if x]}
        # a diagnosis that only rests on isolated readings has no onset, pattern or propagation worth printing
        point_only = bool(d.get("flag_ids")) and all(i in point_ids for i in d["flag_ids"])
        steps = [x for x in (clean_text(x) for x in (d.get("steps") or [])) if x]
        severity = diag_sev[id(d)]
        if point_only:
            d = {**d, "propagation": [], "pattern_id": None}
            steps = [x for x in steps if not _ONSET_STEP_RE.match(x)]
        diag_rows.append({**d, "compact": False, "point_only": point_only, "summary": clean_text(d.get("summary")), "steps": steps, "uncertainty": [x for x in (clean_text(x) for x in (d.get("uncertainty") or [])) if x], "cause_label": t.cause(d.get("cause_class")), "severity": severity, "ranked_svg": svg, "ranked": ranked[:8], "critique": crit, "verdict_label": t.verdict(crit.get("verdict")) if crit else "", "ev": _ev_items(d.get("evidence_ids"), evidence, explain, 5)})
    crit_counts = Counter((d.get("critique") or {}).get("verdict") for d in diags)

    # ---- humans / log
    human = _human_rows(ws, log_entries, signals, flags, diags, patterns, rules, inferences, t)
    by_action = Counter(str(e.get("action")) for e in log_entries).most_common(20)
    by_actor = Counter(str(e.get("actor")).split("(")[0] for e in log_entries).most_common(20)
    chain_text = t("chain_ok", n=chain.get("checked", 0)) if chain.get("ok") else t("chain_bad", seq=chain.get("first_bad_seq"), n=chain.get("checked", 0))
    appendix = [{"seq": e.get("seq"), "ts": _ts(e.get("ts")), "actor": e.get("actor"), "action": e.get("action"), "object": f"{e.get('object_type')} {e.get('object_id')}", "payload": _short(json.dumps(e.get("payload"), ensure_ascii=False), 160), "evidence": ", ".join(e.get("evidence_ids") or [])[:80], "hash": str(e.get("hash"))[:12]} for e in log_entries[:MAX_LOG_APPENDIX]]
    log = {"total": len(log_entries), "by_action": by_action, "by_actor": by_actor, "chain": chain, "chain_text": chain_text, "appendix": appendix, "shown": len(appendix)}

    # ---- dataflow
    summ = _ledger_summary(ws, ledger)
    statement = _dataflow_statement(ws, settings, t, summ, prof)
    g = settings.guard
    dataflow = {
        "profile": settings.profile, "description": prof.description, "allow_external": prof.allow_external, "guard_strict": prof.guard_strict, "local_model": settings.local_llm.model, "local_provider": settings.local_llm.provider, "external_model": settings.external_llm.model, "external_provider": settings.external_llm.provider, "external_base_url": settings.external_llm.base_url or "",
        "routing": [(k, settings.route_for(k)) for k in (prof.routing or {}).keys()],
        "svg": charts.dataflow_diagram(settings.profile, prof.allow_external, settings.local_llm.model, settings.external_llm.model, summ["n_local"], summ["n_external"], summ["n_blocked"], t),
        "ledger": [{**r, "purpose": _short(r.get("purpose"), 90), "artifact_types": ", ".join(r.get("artifact_types") or [])} for r in ledger[:200]], "n_ledger": len(ledger), "summary": summ, "statement": statement,
        "guard_items": t("s8_guard_items", min_n=g.min_aggregate_n, max_series=g.max_series_points, max_vals=g.max_numeric_values_per_payload, max_bytes=g.max_payload_bytes),
    }

    # ---- dataset / overview
    n_excluded = sum(1 for s in signals if s.get("excluded")) + (len(schema.get("label_columns") or []) + len(schema.get("meta_columns") or []) if schema else 0)
    dataset = None
    if schema:
        sp = schema.get("sample_period_seconds")
        dataset = {**schema, "sample_period_text": (f"{_num(sp)} s" if sp else t("s1_sample_units")), "domain_items": [(k, _pct(v)) for k, v in (schema.get("domain_likelihood") or {}).items()], "candidates": schema.get("grouping_candidates") or [], "inferences": [i for i in inferences if i.get("subject") == "dataset" or i.get("stage") == "ingest"][:12]}
    domain_items = []
    if isinstance(domain, dict):
        domain_items = _scalars(domain) + [(k, _pct(v)) for k, v in (domain.get("likelihood") or {}).items()] if isinstance(domain.get("likelihood"), dict) else _scalars(domain)
    overview = []
    if schema:
        overview.append(t("overview_dataset", rows=_thousands(schema.get("n_rows") or 0, lang), cols=schema.get("n_cols"), source=Path(str(schema.get("source_path") or meta.get("source_path") or "")).name, format=schema.get("format"), n_signals=len(schema.get("signal_columns") or signals), n_excluded=n_excluded, groups=schema.get("n_groups"), method=schema.get("grouping_method"), batches=n_batches))
    else:
        overview.append(t("overview_dataset_missing"))
    if checks:
        overview.append(t("overview_quality", n_checks=len(checks), n_pass=quality["n_pass"], n_warn=quality["n_warn"], n_fail=quality["n_fail"], n_untrusted=len(untrusted), n_batches=n_batches))
    if flags or patterns:
        overview.append(t("overview_detect", n_flags=len(sustained), n_groups_flagged=len({f.get("group_id") for f in sustained}), n_patterns=len(patterns)))
    if suspicious and (suspicious["rows"] or suspicious["point_flags"]):
        overview.append(suspicious["headline"] or t("overview_suspicious", n=suspicious["n_rows"] or suspicious["n_point_flags"]))
    if diags:
        mean_conf = sum(float(d.get("confidence") or 0) for d in diags) / max(1, len(diags))
        overview.append(t("overview_diag", n_diag=len(diags), mean_conf=_pct(mean_conf), n_weak=crit_counts.get("weakened", 0), n_rej=crit_counts.get("rejected", 0)))
    if human:
        overview.append(t("overview_human", n_human=len(human)))
    if summ["n_external"] or summ["n_blocked"]:
        overview.append(t("overview_egress", n_external=summ["n_external"], profile=settings.profile, n_blocked=summ["n_blocked"]))
    else:
        overview.append(t("overview_egress_none", profile=settings.profile))
    overview.append(t("overview_chain", n=len(log_entries)))

    # ---- evaluation / assessor
    eval_ctx = None
    if isinstance(evaluation, dict) and evaluation:
        nested = {k: v for k, v in evaluation.items() if isinstance(v, dict) and v and all(isinstance(x, dict) for x in v.values())}
        tables = []
        for k, v in list(nested.items())[:3]:
            cols = sorted({c for row in v.values() for c in row.keys()})[:8]
            tables.append({"name": k, "cols": cols, "rows": [(gk, [row.get(c) for c in cols]) for gk, row in list(v.items())[:60]]})
        eval_ctx = {"scalars": _scalars(evaluation), "tables": tables}
    assess_ctx = None
    if isinstance(assessor, dict) and assessor:
        lc = assessor.get("learning_curve")
        pts = []
        if isinstance(lc, list):
            for p in lc:
                if isinstance(p, dict) and "fraction" in p and "score" in p:
                    pts.append((p["fraction"], p["score"]))
                elif isinstance(p, (list, tuple)) and len(p) >= 2:
                    pts.append((p[0], p[1]))
        recs = assessor.get("recommendations") or assessor.get("actions") or []
        if not pts and isinstance(assessor.get("fitness"), dict):
            for pnt in assessor["fitness"].get("curve") or []:
                if isinstance(pnt, dict) and pnt.get("fraction") is not None and (pnt.get("primary") if pnt.get("primary") is not None else pnt.get("score")) is not None:
                    pts.append((pnt["fraction"], pnt.get("primary") if pnt.get("primary") is not None else pnt.get("score")))
        rec_rows = []
        for r in recs[:12]:
            if not isinstance(r, dict):
                rec_rows.append({"action": str(r), "text": "", "gain": None, "evidence": ""})
                continue
            act = r.get("action")
            if isinstance(act, dict):
                params = ", ".join(f"{k} {v if not isinstance(v, (list, tuple)) else ', '.join(str(x) for x in v)}" for k, v in (act.get("params") or {}).items() if v not in (None, "", [], {}))
                act_label = str(act.get("type") or "").replace("_", " ") + (f" ({params})" if params else "")
            else:
                act_label = str(act or r.get("title") or "")
            gain = r.get("expected_gain")
            if gain is None and isinstance(r.get("expected_effect"), dict):
                gain = r["expected_effect"].get("estimated_gain")
                if gain is None:
                    gain = ((r["expected_effect"].get("dq_scores") or {}).get("overall") or {}).get("delta")
            ev = r.get("evidence") or r.get("reason") or ", ".join(str(x) for x in (r.get("evidence_ids") or [])[:6])
            rec_rows.append({"id": r.get("id"), "action": act_label, "text": strip_lead(r.get("text") or r.get("rationale") or ""), "gain": gain, "evidence": ev})
        verdicts = []
        for key, label in (("more_data_verdict", "assessor_more_data"), ("less_data_verdict", "assessor_less_data")):
            v = assessor.get(key)
            if isinstance(v, dict):
                wh = v.get("would_help")
                verdicts.append({"question": t(label), "answer": t("yes") if wh is True else (t("no") if wh is False else t("unclear")), "state": "pass" if wh is True else ("muted" if wh is False else "warn"), "why": strip_lead(re.sub(r"^\s*Removing bad data helps\s*:\s*", "", str(v.get("why") or ""))), "gain": v.get("estimated_gain")})
        summary_text = re.sub(r"\b(?:More|Less) data:\s*(?:yes|no|unclear)\.\s*", "", clean_text(assessor.get("summary") or "")) if verdicts else clean_text(assessor.get("summary") or "")
        assess_ctx = {"scalars": _scalars(assessor), "summary": summary_text, "verdicts": verdicts, "curve_svg": charts.line_chart(pts, x_label=t("learning_curve")) if pts else "", "curve_points": [(float(a), float(b)) for a, b in pts if isinstance(a, (int, float)) and isinstance(b, (int, float))], "recommendations": rec_rows}

    # ---- optional model-written summary
    anchors = {str(f.get("id")) for f in flag_rows} | {str(d.get("id")) for d in diag_rows}
    known_ids = set(evidence) | {str(x.get("id")) for x in flags} | {str(x.get("id")) for x in diags} | {str(x.get("check_id")) for x in checks} | {str(x.get("id")) for x in inferences} | {str(x.get("id")) for x in rules} | {str(x.get("id")) for x in patterns}
    llm_payload = _narrative_payload(lang, overview, flag_rows, diag_rows, quality, n_flags_total=len(sustained), n_diagnoses_total=len(diags))
    if narrative is None and use_llm:
        budget = LLM_WAIT_S
        if ctx and ctx.get("t_start") and ctx.get("time_budget_s"):
            budget = min(budget, max(0.0, float(ctx["time_budget_s"]) - (time.time() - float(ctx["t_start"]))))
        narrative = _ask_model(ws, settings, lang, llm_payload, wait_s=budget)
    llm = _narrative_context(narrative, known_ids, anchors)
    if llm is not None:
        llm_state = "ready"
    elif llm_state == "ready":
        llm_state = "none"

    context = {
        "t": t, "lang": lang, "lang_name": t("lang_name"), "languages": available_languages(), "generated_at": _ts(now_iso()), "run_id": ws.run_id, "meta": meta, "status": status, "source_name": Path(str(meta.get("source_path") or status.get("source_path") or "")).name or t("unknown"), "source_path": meta.get("source_path") or status.get("source_path") or "",
        "stages": stages, "overview": overview, "schema": schema, "dataset": dataset, "domain_items": domain_items, "signals": _signal_rows(signals, evidence, inferences, t, explain), "n_signals_total": len(signals), "relations_count": (len(relations.get("pairs") or []) if isinstance(relations, dict) else (len(relations) if isinstance(relations, list) else 0)),
        "quality": quality, "detect": detect, "suspicious": suspicious, "diagnoses": diag_rows, "n_diag_total": len(diags), "human": human, "log": log, "dataflow": dataflow, "evaluation": eval_ctx, "assessor": assess_ctx, "llm": llm, "llm_state": llm_state, "llm_payload": llm_payload, "has_plain_evidence": explain is not None,
        "caps": {"flags": (len(flag_rows), len(sustained)), "diagnoses": (len(diag_rows), len(diags)), "untrusted": (min(len(untrusted), MAX_UNTRUSTED), len(untrusted))},
        "fmt": {"num": _num, "pct": _pct, "short": _short, "ts": _ts},
    }
    # Signals a person renamed read "possibly broken (S44)" in every sentence of the report (HTML, PDF, deck);
    # ids, anchors and the payload for the model keep the bare alias.
    from ..naming import expand, operator_names

    names = operator_names(ws)
    if names:
        keep = {"t", "fmt", "llm_payload", "meta"}
        context = {k: (v if k in keep else expand(v, names)) for k, v in context.items()}
        for row in context.get("signals") or []:
            if isinstance(row, dict) and row.get("id") in names:
                row["display_name"] = names[row["id"]]
    return context


# ----------------------------------------------------------------------------- model-written summary
_LANGUAGE_NAMES = {"en": "English", "fi": "Finnish (suomi)", "sv": "Swedish (svenska)"}


def _r3(v: Any) -> Any:
    try:
        return round(float(v), 3) if v is not None and not isinstance(v, bool) else v
    except Exception:
        return v


def _narrative_payload(lang: str, overview: list[str], flags: list[dict[str, Any]], diags: list[dict[str, Any]], quality: dict[str, Any], n_flags_total: Optional[int] = None, n_diagnoses_total: Optional[int] = None) -> dict[str, Any]:
    """Derived artifacts only, under keys the egress guard knows (report_sections / flags / diagnoses / meta)."""
    name = _LANGUAGE_NAMES.get(lang, "English")
    return {
        "language": lang,
        "instructions": (
            f"Write for a plant operator, in {name}: the headings and every sentence must be in {name}. Keep it short: an executive "
            "summary of 2 to 4 sentences, then at most 4 sections of 2 to 4 sentences each, then at most 3 uncertainty sentences. "
            "Use complete sentences in plain prose: no lists, no markdown and no JSON inside the text fields. Keep every number exactly "
            "as given and cite only complete ids that appear in the artifacts (never a placeholder such as CHK-...). The flags and "
            "diagnoses listed are only the most severe few; the totals are in report_sections, so never present the length of a list as the total."
        ),
        "report_sections": {"overview": overview[:4], "quality": {k: quality[k] for k in ("n_checks", "n_pass", "n_warn", "n_fail", "n_batches") if k in quality}, "untrusted_batches": quality.get("n_untrusted_total", 0), "n_flags_total": n_flags_total if n_flags_total is not None else len(flags), "n_diagnoses_total": n_diagnoses_total if n_diagnoses_total is not None else len(diags)},
        "flags": [{"id": f.get("id"), "kind": f.get("kind"), "group_id": f.get("group_id"), "severity": _r3(f.get("severity")), "statement": whole_sentences(f.get("statement"), 320) or _short(f.get("statement"), 320), "signals": f.get("signals")} for f in flags[:8]],
        "diagnoses": [{"id": d.get("id"), "fault_type": d.get("fault_type"), "cause_class": d.get("cause_class"), "confidence": _r3(d.get("confidence")), "summary": whole_sentences(d.get("summary"), 320) or _short(d.get("summary"), 320), "verdict": (d.get("critique") or {}).get("verdict")} for d in diags[:8]],
    }


def _call_model(ws: Workspace, settings: Settings, lang: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One report_narrative call -> {"status": "ok", "narrative", "source", "model", "route"} or {"status": "failed", "error"}.
    Never raises. The reply is parsed here so that only prose is ever stored."""
    try:
        from ..llm import complete

        res = complete("report_narrative", payload, purpose=f"report: model-written summary ({lang})", ws=ws, settings=settings, language=lang, max_tokens=LLM_MAX_TOKENS)
        data, text = getattr(res, "data", None), (getattr(res, "text", "") or "")
        if not (getattr(res, "ok", False) or data or text.strip()):
            return {"status": "failed", "error": str(getattr(res, "error", "") or "no model available")[:300]}
        parsed = parse_narrative(data, text)
        if parsed is None:
            return {"status": "failed", "error": "the model reply could not be read as a summary"}
        return {"status": "ok", "narrative": parsed, "source": getattr(res, "source", "") or "llm", "model": getattr(res, "model", "") or "", "route": getattr(res, "route", "") or "", "latency_ms": getattr(res, "latency_ms", None)}
    except Exception as e:  # the report never depends on the model
        return {"status": "failed", "error": str(e)[:300]}


def _ask_model(ws: Workspace, settings: Settings, lang: str, payload: dict[str, Any], wait_s: float) -> Optional[dict[str, Any]]:
    """Time-boxed model call in a worker thread; None when it is not back within wait_s (the worker is abandoned)."""
    if wait_s <= 0:
        return None
    box: dict[str, Any] = {}
    th = threading.Thread(target=lambda: box.update(_call_model(ws, settings, lang, payload)), name=f"tpm-report-llm-{lang}", daemon=True)
    th.start()
    th.join(wait_s)
    return box if box.get("status") == "ok" else None


def _narrative_context(narrative: Optional[dict[str, Any]], known_ids: set[str], anchors: set[str]) -> Optional[dict[str, Any]]:
    """Stored narrative -> template context; references are limited to ids that exist in this run (a model must not
    invent evidence) and link to the row / card when it is rendered in this report."""
    if not isinstance(narrative, dict) or narrative.get("status") != "ok" or not isinstance(narrative.get("narrative"), dict):
        return None
    n = narrative["narrative"]
    ref = lambda i: {"id": i, "anchor": i in anchors}  # noqa: E731
    sections = [{"heading": s.get("heading") or "", "paragraphs": list(s.get("paragraphs") or []), "refs": [ref(i) for i in (s.get("refs") or []) if i in known_ids]} for s in (n.get("sections") or []) if s.get("paragraphs")]
    summary = list(n.get("summary") or [])
    if not summary and not sections:
        return None
    return {"summary": summary, "sections": sections, "uncertainty": list(n.get("uncertainty") or []), "confidence": n.get("confidence"), "truncated": bool(n.get("truncated")), "source": narrative.get("source") or "llm", "model": narrative.get("model") or "", "route": narrative.get("route") or "", "generated_at": _ts(narrative.get("ts") or "")}


# ----------------------------------------------------------------------------- cache: fingerprints, stored narrative, jobs
_locks_guard = threading.Lock()
_run_locks: dict[str, threading.RLock] = {}
_jobs: dict[str, dict[str, Any]] = {}


def _run_lock(ws: Workspace) -> threading.RLock:
    key = str(ws.dir)
    with _locks_guard:
        if key not in _run_locks:
            _run_locks[key] = threading.RLock()
        return _run_locks[key]


def _fingerprint(ws: Workspace, names: tuple[str, ...], extra: str = "") -> str:
    h = hashlib.sha1(f"v{REPORT_VERSION}|{extra}".encode("utf-8"))
    for name in names:
        try:
            st = ws.path(name).stat()
            h.update(f"|{name}:{st.st_size}:{st.st_mtime_ns}".encode("utf-8"))
        except OSError:
            h.update(f"|{name}:-".encode("utf-8"))
    return h.hexdigest()[:16]


def _code_stamp() -> str:
    """Size + mtime of the template, the dictionaries and this package: a cached report written by older code is stale."""
    parts = []
    for f in [TEMPLATE_DIR / "report.html.j2", *sorted((Path(__file__).resolve().parent / "i18n").glob("*.json")), *sorted(Path(__file__).resolve().parent.glob("*.py"))]:
        try:
            st = f.stat()
            parts.append(f"{f.name}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            pass
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]


def report_fingerprint(ws: Workspace) -> str:
    """Changes when any artifact the report shows changes, when a human records a decision, or when the report code /
    template / dictionaries change."""
    try:
        n_human = len(ws.log.entries(actor_prefix="human:", limit=1_000_000))
    except Exception:
        n_human = -1
    return _fingerprint(ws, _REPORT_INPUTS, extra=f"human={n_human}|code={_code_stamp()}")


def narrative_fingerprint(ws: Workspace) -> str:
    """Changes when what the model summary talks about changes (findings), not on every log / ledger entry."""
    return _fingerprint(ws, _NARRATIVE_INPUTS)


def narrative_path(ws: Workspace, lang: str) -> Path:
    return ws.dir / f"report_llm_{normalize_lang(lang)}.json"


def _load_narrative(ws: Workspace, lang: str) -> Optional[dict[str, Any]]:
    """The stored model summary (or stored failure) for this language, if it matches the current findings."""
    try:
        with open(narrative_path(ws, lang), "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if not isinstance(d, dict) or d.get("fingerprint") != narrative_fingerprint(ws) or d.get("version") != REPORT_VERSION:
        return None
    if d.get("status") != "ok":
        try:
            if time.time() - float(d.get("epoch") or 0) > LLM_RETRY_S:
                return None  # old failure: worth another try
        except Exception:
            return None
    return d


def _store_narrative(ws: Workspace, lang: str, fingerprint: str, result: dict[str, Any]) -> None:
    try:
        rec = {**result, "fingerprint": fingerprint, "version": REPORT_VERSION, "lang": normalize_lang(lang), "ts": now_iso(), "epoch": time.time()}
        _atomic_write(narrative_path(ws, lang), json.dumps(rec, ensure_ascii=False, indent=1))
    except Exception:
        pass


def _atomic_write(out: Path, text: str) -> None:
    """Unique temp name (two writers never share it) + replace with retries (Windows refuses while a reader has it open)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    for i in range(60):
        try:
            tmp.replace(out)
            return
        except PermissionError:
            time.sleep(0.02 + 0.01 * i)
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


_META_RE = re.compile(r'<meta name="tpm-report" content="([^"]*)"')


def _read_meta(path: Path) -> dict[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(2048)
    except OSError:
        return {}
    m = _META_RE.search(head)
    if not m:
        return {}
    return dict(kv.split("=", 1) for kv in m.group(1).split(";") if "=" in kv)


def _llm_enabled(use_llm: Optional[bool]) -> bool:
    if os.environ.get("TPM_REPORT_LLM", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    return bool(use_llm)


def _job_key(ws: Workspace, lang: str) -> str:
    return f"{ws.dir}|{normalize_lang(lang)}"


def _job_running(ws: Workspace, lang: str) -> bool:
    with _locks_guard:
        j = _jobs.get(_job_key(ws, lang))
    return bool(j and j["thread"].is_alive())


def _start_job(ws: Workspace, settings: Settings, lang: str, payload: dict[str, Any], rerender: bool, out: Optional[Path] = None) -> dict[str, Any]:
    """Ask the model in a daemon thread (one per run and language). The result is stored in report_llm_<lang>.json;
    with rerender=True the report is rendered again when the summary arrives (API use). Never raises."""
    key = _job_key(ws, lang)
    with _locks_guard:
        j = _jobs.get(key)
        if j and j["thread"].is_alive():
            j["rerender"] = j["rerender"] or rerender
            return j
        job: dict[str, Any] = {"done": threading.Event(), "rerender": rerender, "started": time.time(), "result": None, "out": out}

        def work() -> None:
            try:
                fp = narrative_fingerprint(ws)
                result = _call_model(ws, settings, lang, payload)
                job["result"] = result
                _store_narrative(ws, lang, fp, result)
                if job["rerender"]:
                    _render_to_disk(ws, settings, lang, use_llm=True, out_path=job["out"])
            except Exception:
                pass
            finally:
                job["done"].set()

        job["thread"] = threading.Thread(target=work, name=f"tpm-report-llm-{normalize_lang(lang)}", daemon=True)
        _jobs[key] = job
        job["thread"].start()
        return job


# ----------------------------------------------------------------------------- rendering
_env: Optional[Environment] = None


def _environment() -> Environment:
    global _env
    if _env is None:
        _env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html", "j2"]), undefined=ChainableUndefined, trim_blocks=True, lstrip_blocks=True)
        _env.filters["num"] = _num
        _env.filters["pct"] = _pct
        _env.filters["short"] = _short
        _env.filters["ts"] = _ts
        _env.filters["thousands"] = _thousands
    return _env


def render_html(context: dict[str, Any]) -> str:
    return _environment().get_template("report.html.j2").render(**context)


def _render_to_disk(ws: Workspace, settings: Settings, lang: str, use_llm: bool, out_path: Optional[str | Path] = None, log: bool = True, pending: bool = False) -> dict[str, Any]:
    """Collect + render + write, under the run lock. The stored model summary is included when it matches the current
    findings; `pending` marks a report whose summary is still being written. Returns the context."""
    with _run_lock(ws):
        fp = report_fingerprint(ws)
        narrative = _load_narrative(ws, lang) if use_llm else None
        state = "pending" if (pending and not (narrative and narrative.get("status") == "ok")) else "none"
        context = collect(ws, settings, lang, use_llm=False, narrative=narrative, llm_state=state)
        caps = context["caps"]
        context["report_meta"] = f"v={REPORT_VERSION};fp={fp};llm={context['llm_state']};flags={caps['flags'][0]}/{caps['flags'][1]};diagnoses={caps['diagnoses'][0]}/{caps['diagnoses'][1]}"
        html = render_html(context)
        out = Path(out_path) if out_path else report_path(ws, lang)
        _atomic_write(out, html)
        if log:
            try:
                ws.log.record("system:report", "report", "report", out.name, {"lang": lang, "bytes": out.stat().st_size, "llm_summary": bool(context.get("llm")), "flags_shown": caps["flags"][0], "flags_total": caps["flags"][1], "diagnoses_shown": caps["diagnoses"][0], "diagnoses_total": caps["diagnoses"][1]})
            except Exception:
                pass
        context["out_path"] = out
        return context


def generate_report(ws: Workspace, settings: Optional[Settings] = None, lang: str = "en", use_llm: bool = True, out_path: Optional[str | Path] = None, ctx: Optional[dict[str, Any]] = None, llm_wait_s: Optional[float] = None) -> Path:
    """Write report_<lang>.html and return its path. The template report is written first; the model summary is then
    awaited for at most llm_wait_s seconds (default LLM_WAIT_S, limited by the pipeline time budget) and the report is
    rendered once more when it arrives. With llm_wait_s=0 the summary is added later by the worker (API use)."""
    settings = settings or ws.settings or get_settings()
    lang = normalize_lang(lang)
    use_llm = _llm_enabled(use_llm)
    wait = LLM_WAIT_S if llm_wait_s is None else max(0.0, float(llm_wait_s))
    if ctx and ctx.get("t_start") and ctx.get("time_budget_s"):
        wait = min(wait, max(0.0, 0.95 * float(ctx["time_budget_s"]) - (time.time() - float(ctx["t_start"]))))
    progress = (ctx or {}).get("progress")
    need_model = use_llm and _load_narrative(ws, lang) is None
    context = _render_to_disk(ws, settings, lang, use_llm, out_path, pending=need_model)
    out = context["out_path"]
    if not need_model:
        return out
    blocking = wait > 0
    job = _start_job(ws, settings, lang, context["llm_payload"], rerender=not blocking, out=Path(out_path) if out_path else None)
    if not blocking:
        return out
    if callable(progress):
        try:
            progress(0.6, f"report written; waiting up to {wait:.0f} s for the model-written summary ({lang})")
        except Exception:
            pass
    job["done"].wait(wait)
    # arrived: include it; not arrived or failed: the report states that no model summary is included
    _render_to_disk(ws, settings, lang, use_llm, out_path, log=bool(job["result"] and job["result"].get("status") == "ok"))
    return out


def report_status(ws: Workspace, lang: str = "en") -> dict[str, Any]:
    """{"exists", "fresh", "llm": ready|pending|none, "bytes", "generated_at", "flags", "diagnoses"} for report_<lang>.html."""
    lang = normalize_lang(lang)
    p = report_path(ws, lang)
    if not p.exists():
        return {"lang": lang, "exists": False, "fresh": False, "llm": "none", "bytes": 0}
    meta = _read_meta(p)
    st = p.stat()
    llm = meta.get("llm", "none")
    if llm == "pending" and not _job_running(ws, lang):
        llm = "stalled"  # rendered while a worker was running that no longer exists (server restart)
    return {"lang": lang, "exists": True, "fresh": bool(meta) and meta.get("v") == REPORT_VERSION and meta.get("fp") == report_fingerprint(ws), "llm": llm, "bytes": st.st_size, "generated_at": _ts(time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(st.st_mtime))) + " UTC", "flags": meta.get("flags", ""), "diagnoses": meta.get("diagnoses", "")}


def ensure_report(ws: Workspace, settings: Optional[Settings] = None, lang: str = "en", use_llm: bool = True, force: bool = False, ask_model: bool = True) -> dict[str, Any]:
    """API entry point. Returns report_status() plus "path", "regenerated" and "seconds"; never waits for the model.

    - cached per language: a fresh report_<lang>.html is served as it is;
    - regenerated (template first, a few seconds at most) when it is missing, when an artifact changed, when a human
      recorded a decision, or when the report code changed (REPORT_VERSION);
    - the model summary is requested in a worker thread and the report is rendered again when it arrives; until then
      the status says llm="pending". A failed model call is remembered for LLM_RETRY_S seconds;
    - ask_model=False uses a stored summary but never starts the model (e-mail, export)."""
    settings = settings or ws.settings or get_settings()
    lang = normalize_lang(lang)
    use_llm = _llm_enabled(use_llm)
    t0 = time.time()
    regenerated = False
    with _run_lock(ws):
        if force and use_llm and ask_model and not _job_running(ws, lang):
            try:
                narrative_path(ws, lang).unlink()
            except OSError:
                pass
        st = report_status(ws, lang)
        stored = _load_narrative(ws, lang) if use_llm else None
        have = bool(stored and stored.get("status") == "ok")
        want_model = use_llm and ask_model and stored is None  # nothing stored (or an old failure): ask the model
        running = _job_running(ws, lang)
        stale = force or not st["exists"] or not st["fresh"]
        # rendered without the summary although one is stored now (or the other way round after new findings)
        mismatch = use_llm and st["exists"] and ((have and st["llm"] != "ready") or (not have and st["llm"] == "ready") or st["llm"] == "stalled" or (st["llm"] == "pending" and not want_model and not running))
        if stale or mismatch:
            context = _render_to_disk(ws, settings, lang, use_llm, pending=want_model or running)
            regenerated = True
            if want_model and not running:
                _start_job(ws, settings, lang, context["llm_payload"], rerender=True)
        elif want_model and not running:
            context = collect(ws, settings, lang, use_llm=False)
            _start_job(ws, settings, lang, context["llm_payload"], rerender=True)
        st = report_status(ws, lang)
    if use_llm and st["llm"] != "ready" and _job_running(ws, lang):
        st["llm"] = "pending"
    elif st["llm"] in ("pending", "stalled"):
        st["llm"] = "none"
    return {**st, "path": str(report_path(ws, lang)), "regenerated": regenerated, "seconds": round(time.time() - t0, 2)}


def run_report(ws: Workspace, settings: Settings, ctx: dict[str, Any]) -> dict[str, Any]:
    """Pipeline stage entry point (ARCHITECTURE.md section 3)."""
    options = (ctx or {}).get("options") or {}
    lang = normalize_lang(options.get("language") or settings.report.default_language)
    use_llm = bool(options.get("report_llm", True))
    progress = (ctx or {}).get("progress")
    if callable(progress):
        progress(0.1, f"rendering report ({lang})")
    out = generate_report(ws, settings, lang, use_llm=use_llm, ctx=ctx)
    if callable(progress):
        progress(1.0, "report written")
    llm = _read_meta(out).get("llm", "none")
    return {"message": f"report written: {out.name}" + (" (with model-written summary)" if llm == "ready" else ""), "path": str(out), "lang": lang, "llm_summary": llm == "ready"}
