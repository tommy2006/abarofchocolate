"""HTML report generation (agent F).

    run_report(ws, settings, ctx)            pipeline stage: writes report_<lang>.html
    generate_report(ws, settings, lang)      any language; returns the path
    collect(ws, settings, lang)              the template context (also useful for the API)

Template-first: every sentence is composed from the i18n dictionaries. If tpm.llm.complete("report_narrative")
returns ok=True, its text is added as a clearly labelled "model-written summary". Every artifact is optional:
missing ones render as "not available in this run".
"""
from __future__ import annotations

import json
import math
import re
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

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
MAX_SIGNALS = 400
MAX_FLAGS = 200
MAX_DIAGNOSES = 60
MAX_CHECK_ROWS = 300
MAX_LOG_APPENDIX = 1500
MAX_TIMELINES = 24
TIMELINE_POINTS = 160


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


# ----------------------------------------------------------------------------- collection
def _score_timelines(ws: Workspace, schema: Optional[dict[str, Any]], flags: list[dict[str, Any]], detect_meta: Optional[dict[str, Any]], t: Translator) -> Optional[dict[str, Any]]:
    """Per-group downsampled score series from scores.parquet via DuckDB. None when unavailable."""
    if not ws.exists("scores"):
        return None
    try:
        con = ws.duckdb()
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
            timelines.append({"group": g, "svg": svg, "n_flags": flagged_groups.get(g, 0), "max": max(vals) if vals else 0.0})
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


def _signal_rows(signals: list[dict[str, Any]], evidence: dict[str, dict[str, Any]], inferences: list[dict[str, Any]], t: Translator) -> list[dict[str, Any]]:
    by_subject: dict[str, list[dict[str, Any]]] = {}
    for inf in inferences:
        by_subject.setdefault(str(inf.get("subject")), []).append(inf)
    rows = []
    for s in signals[:MAX_SIGNALS]:
        sid = str(s.get("id"))
        ev_ids = list(s.get("evidence_ids") or [])
        ev_stmts = [evidence[e]["statement"] for e in ev_ids if e in evidence][:6]
        hyps = []
        for inf in by_subject.get(sid, [])[:6]:
            hyps.append({"claim": inf.get("claim", ""), "status": inf.get("status", ""), "confidence": inf.get("confidence"), "reasoning": inf.get("reasoning", ""), "alternatives": inf.get("alternatives") or [], "source": inf.get("source", "code"), "id": inf.get("id"), "human_status": inf.get("human_status")})
            for e in inf.get("evidence_ids") or []:
                if e in evidence and evidence[e]["statement"] not in ev_stmts and len(ev_stmts) < 8:
                    ev_stmts.append(evidence[e]["statement"])
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


def collect(ws: Workspace, settings: Optional[Settings] = None, lang: str = "en", use_llm: bool = False, ctx: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    settings = settings or ws.settings or get_settings()
    lang = normalize_lang(lang)
    t = Translator(lang)
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
        rule_rows.append({"id": r.get("id"), "text": r.get("text"), "status": r.get("status"), "compiled": _short(json.dumps(comp, ensure_ascii=False), 220) if comp else "", "compile_source": r.get("compile_source"), "compile_confidence": r.get("compile_confidence"), "explanation": r.get("compile_explanation"), "n_checks": checks_by_rule.get(str(r.get("id")), 0)})
    quality = {"n_checks": len(checks), "n_pass": st_counts.get("pass", 0), "n_warn": st_counts.get("warn", 0), "n_fail": st_counts.get("fail", 0), "by_category": by_cat, "svg": charts.stacked_bars(by_cat, labels={"pass": t("pass"), "warn": t("warn"), "fail": t("fail")}), "failed": failed, "n_failed_total": sum(1 for c in checks if c.get("status") in ("fail", "warn")), "rules": rule_rows, "trust": trust[:MAX_CHECK_ROWS], "untrusted": untrusted, "threshold": settings.quality.trust_fail_threshold, "n_batches": n_batches}

    # ---- detect
    tl = _score_timelines(ws, schema, flags, detect_meta, t)
    flag_rows = []
    for f in sorted(flags, key=lambda f: -float(f.get("severity") or 0))[:MAX_FLAGS]:
        flag_rows.append({**f, "kind_label": t.kind(f.get("kind")), "cause_label": t.cause(f.get("likely_cause_class")), "signals": [f"{s.get('signal')} ({_pct(s.get('contribution'))}{', ' + str(s.get('direction')) if s.get('direction') else ''})" for s in (f.get("signals_ranked") or [])[:5]], "ev": [evidence[e]["statement"] for e in (f.get("evidence_ids") or []) if e in evidence][:3]})
    detect = {"baseline": baseline, "baseline_items": _scalars(baseline), "baseline_assumptions": (baseline or {}).get("assumptions") if isinstance(baseline, dict) else None, "detect_meta": detect_meta, "detect_items": _scalars(detect_meta), "timelines": tl, "flags": flag_rows, "n_flags": len(flags), "patterns": [{**p, "name_label": p.get("name") or t("unnamed"), "signature_text": _short(json.dumps(p.get("signature"), ensure_ascii=False), 200)} for p in patterns], "threshold": (tl or {}).get("threshold")}

    # ---- diagnoses
    diag_rows = []
    for d in diags[:MAX_DIAGNOSES]:
        ranked = d.get("ranked_signals") or []
        svg = charts.hbars([(str(s.get("signal")), float(s.get("contribution") or 0), f"{_pct(s.get('contribution'))}{' ' + str(s.get('direction')) if s.get('direction') else ''}") for s in ranked[:8]])
        crit = d.get("critique") or None
        diag_rows.append({**d, "cause_label": t.cause(d.get("cause_class")), "ranked_svg": svg, "ranked": ranked[:8], "critique": crit, "verdict_label": t.verdict(crit.get("verdict")) if crit else "", "ev": [evidence[e]["statement"] for e in (d.get("evidence_ids") or []) if e in evidence][:5]})
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
        overview.append(t("overview_dataset", rows=f"{int(schema.get('n_rows') or 0):,}", cols=schema.get("n_cols"), source=Path(str(schema.get("source_path") or meta.get("source_path") or "")).name, format=schema.get("format"), n_signals=len(schema.get("signal_columns") or signals), n_excluded=n_excluded, groups=schema.get("n_groups"), method=schema.get("grouping_method"), batches=n_batches))
    else:
        overview.append(t("overview_dataset_missing"))
    if checks:
        overview.append(t("overview_quality", n_checks=len(checks), n_pass=quality["n_pass"], n_warn=quality["n_warn"], n_fail=quality["n_fail"], n_untrusted=len(untrusted), n_batches=n_batches))
    if flags or patterns:
        overview.append(t("overview_detect", n_flags=len(flags), n_groups_flagged=len({f.get("group_id") for f in flags}), n_patterns=len(patterns)))
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
        assess_ctx = {"scalars": _scalars(assessor), "summary": assessor.get("summary") or "", "curve_svg": charts.line_chart(pts, x_label=t("learning_curve")) if pts else "", "recommendations": [r if isinstance(r, dict) else {"action": str(r)} for r in recs][:12]}

    # ---- optional model-written summary
    llm = None
    if use_llm:
        llm = _llm_summary(ws, settings, lang, overview, flag_rows, diag_rows, quality, ctx)

    return {
        "t": t, "lang": lang, "lang_name": t("lang_name"), "languages": available_languages(), "generated_at": _ts(now_iso()), "run_id": ws.run_id, "meta": meta, "status": status, "source_name": Path(str(meta.get("source_path") or status.get("source_path") or "")).name or t("unknown"), "source_path": meta.get("source_path") or status.get("source_path") or "",
        "stages": stages, "overview": overview, "schema": schema, "dataset": dataset, "domain_items": domain_items, "signals": _signal_rows(signals, evidence, inferences, t), "n_signals_total": len(signals), "relations_count": (len(relations.get("pairs") or []) if isinstance(relations, dict) else (len(relations) if isinstance(relations, list) else 0)),
        "quality": quality, "detect": detect, "diagnoses": diag_rows, "n_diag_total": len(diags), "human": human, "log": log, "dataflow": dataflow, "evaluation": eval_ctx, "assessor": assess_ctx, "llm": llm,
        "fmt": {"num": _num, "pct": _pct, "short": _short, "ts": _ts},
    }


def _llm_summary(ws: Workspace, settings: Settings, lang: str, overview: list[str], flags: list[dict[str, Any]], diags: list[dict[str, Any]], quality: dict[str, Any], ctx: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Ask the LLM layer for a narrative built from derived artifacts only; None when unavailable."""
    if ctx and ctx.get("t_start") and ctx.get("time_budget_s"):
        if time.time() - float(ctx["t_start"]) > 0.9 * float(ctx["time_budget_s"]):
            return None
    try:
        from ..llm import complete

        payload = {
            "language": lang,
            "overview": overview,
            "quality": {k: quality[k] for k in ("n_checks", "n_pass", "n_warn", "n_fail")},
            "top_flags": [{"id": f.get("id"), "kind": f.get("kind"), "group": f.get("group_id"), "statement": _short(f.get("statement"), 200), "signals": f.get("signals")} for f in flags[:8]],
            "diagnoses": [{"id": d.get("id"), "fault_type": d.get("fault_type"), "cause_class": d.get("cause_class"), "confidence": d.get("confidence"), "summary": _short(d.get("summary"), 240), "verdict": (d.get("critique") or {}).get("verdict")} for d in diags[:8]],
        }
        res = complete("report_narrative", payload, purpose="report: model-written summary", ws=ws, settings=settings, language=lang, max_tokens=700)
        if getattr(res, "ok", False) and (getattr(res, "text", "") or "").strip():
            return {"text": res.text.strip(), "source": res.source or "llm", "model": getattr(res, "model", ""), "route": getattr(res, "route", "")}
    except Exception:
        return None
    return None


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
    return _env


def render_html(context: dict[str, Any]) -> str:
    return _environment().get_template("report.html.j2").render(**context)


def generate_report(ws: Workspace, settings: Optional[Settings] = None, lang: str = "en", use_llm: bool = True, out_path: Optional[str | Path] = None, ctx: Optional[dict[str, Any]] = None) -> Path:
    settings = settings or ws.settings or get_settings()
    lang = normalize_lang(lang)
    context = collect(ws, settings, lang, use_llm=use_llm, ctx=ctx)
    html = render_html(context)
    out = Path(out_path) if out_path else report_path(ws, lang)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    tmp.replace(out)
    try:
        ws.log.record("system:report", "report", "report", out.name, {"lang": lang, "bytes": out.stat().st_size, "llm_summary": bool(context.get("llm"))})
    except Exception:
        pass
    return out


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
    return {"message": f"report written: {out.name}", "path": str(out), "lang": lang}
