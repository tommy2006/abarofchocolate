"""profile stage: dataset + schema -> ``signals.json`` (the signal catalog, list[SignalDescriptor]),
``relations.json``, ``domain.json``, ``understanding.json`` (code-generated sensor understanding report),
evidence, inferences and log entries.

    from tpm.profile import run_profile, apply_override
    summary = run_profile(ws, settings, ctx)

Only ``schema.signal_columns`` are profiled: label and metadata columns never enter the catalog.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from ..contracts import SignalDescriptor
from .fingerprints import compute_fingerprints, load_dynamics_sample
from .relations import compute_relations
from .roles import ACTOR, MANIPULATED_MIN_SCORE, STAGE, apply_override, heuristic_hypotheses, llm_hypotheses, llm_unit_operations, manipulated_evidence, structural_role, check_hypotheses  # noqa: F401

__all__ = ["run_profile", "apply_override", "write_catalog", "build_understanding"]

ROLE_TEXT = {
    "continuous_measured": "a continuously varying measurement",
    "actuator_like": "a step-like manipulated variable (actuator-like, manipulated_evidence, MANIPULATED_MIN_SCORE)",
    "held_sampled": "a sample-and-hold measurement (updated every few samples)",
    "constant": "constant",
    "derived_redundant": "a derived / redundant signal (function of other signals)",
    "counter": "a monotone counter",
    "timestamp": "a timestamp",
    "categorical": "a discrete-coded signal",
    "text": "free text",
    "identifier": "an identifier",
    "unknown": "of unknown structure",
}


def _progress(ctx: dict[str, Any]):
    fn = ctx.get("progress")

    def p(f: float, m: str = "") -> None:
        if fn:
            try:
                fn(float(max(0.0, min(1.0, f))), m)
            except Exception:
                pass

    return p


def _fmt(v: Any, nd: int = 3) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v):.{nd}g}"
    except (TypeError, ValueError):
        return str(v)


def signal_narrative(d: SignalDescriptor, relations: dict[str, Any]) -> str:
    fp = d.fingerprint
    parts = [f"{d.id} is {ROLE_TEXT.get(d.structural_role, d.structural_role)} (confidence {d.structural_confidence:.0%})."]
    if d.human_role_override:
        parts.append(f"An operator set the role to {d.human_role_override}.")
    if fp.get("count"):
        parts.append(f"Mean {_fmt(fp.get('mean'))}, std {_fmt(fp.get('std'))}, range [{_fmt(fp.get('min'))}, {_fmt(fp.get('max'))}], missing {float(fp.get('missing_rate') or 0):.1%}.")
    if d.structural_role not in ("constant",) and fp.get("autocorr_lag1") is not None:
        parts.append(f"Lag-1 autocorrelation {_fmt(fp.get('autocorr_lag1'), 2)}, noise level {_fmt(fp.get('noise_level'), 2)}, {float(fp.get('stuck_fraction') or 0):.0%} unchanged steps.")
    if d.structural_role == "held_sampled":
        parts.append(f"Values are held for about {_fmt(fp.get('hold_period'), 2)} samples between updates.")
    if d.structural_role == "actuator_like":
        parts.append(f"Steps are held for about {_fmt(fp.get('hold_period'), 2)} samples; " + ("bounded to 0-100." if fp.get("range_0_100") else "unbounded."))
    if fp.get("dominant_period"):
        parts.append(f"A dominant oscillation period of ~{_fmt(fp.get('dominant_period'), 3)} samples is visible.")
    if d.related_signals:
        r0 = d.related_signals[0]
        lag_txt = f", leading by {r0['lag']} samples" if r0.get("lag", 0) > 0 and r0.get("leads") else (f", lagging by {r0['lag']} samples" if r0.get("lag", 0) > 0 else "")
        parts.append(f"Strongest partner: {r0['signal']} (r={r0['r']:+.2f}{lag_txt}).")
    if d.cluster_id:
        parts.append(f"Member of cluster {d.cluster_id}.")
    if d.instrument_hypothesis:
        parts.append(f"Hypothesis: {d.instrument_hypothesis} (confidence {d.instrument_confidence:.0%}, not a fact).")
    if d.unit_operation_hypothesis:
        parts.append(f"Unit-operation hypothesis: {d.unit_operation_hypothesis} ({d.unit_operation_confidence:.0%}).")
    if d.excluded:
        parts.append(f"Excluded from detection ({d.excluded_reason}).")
    return " ".join(parts)


def build_understanding(ws, descriptors: list[SignalDescriptor], relations: dict[str, Any], domain: dict[str, Any], schema, llm_info: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Deterministic 'sensor understanding report' structure (template first)."""
    sig_entries = []
    for d in descriptors:
        stmts = []
        for eid in d.evidence_ids[:12]:
            e = ws.evidence.get(eid)
            if e:
                stmts.append({"id": e.id, "kind": e.kind, "statement": e.statement})
        hyps = []
        for iid in d.inference_ids:
            inf = ws.inferences.get(iid)
            if inf and ("hypothesis" in inf.claim):
                hyps.append({"inference_id": inf.id, "claim": inf.claim, "confidence": inf.confidence, "source": inf.source, "status": inf.status, "human_status": inf.human_status})
        uncertainty = []
        if d.structural_confidence < 0.6:
            uncertainty.append(f"structural role is uncertain ({d.structural_confidence:.0%})")
        if d.fingerprint.get("missing_rate") and d.fingerprint["missing_rate"] > 0.05:
            uncertainty.append(f"{d.fingerprint['missing_rate']:.0%} missing values")
        if d.instrument_hypothesis:
            uncertainty.append("instrument type is a hypothesis, not a fact")
        if d.structural_role == "derived_redundant":
            uncertainty.append("which member of the redundant set is derived cannot be decided from data alone")
        sig_entries.append({"id": d.id, "alias": d.id, "source_column": d.source_column, "column_index": d.column_index, "role": d.structural_role, "confidence": d.structural_confidence, "excluded": d.excluded, "cluster_id": d.cluster_id, "evidence": stmts, "hypotheses": hyps, "uncertainty": uncertainty, "narrative": signal_narrative(d, relations)})
    unknowns = list(schema.assumptions)
    if not schema.time_column:
        unknowns.append("physical sample period (working in sample units)")
    if max(domain.get("domain_likelihood", {}).values() or [0]) < 0.6:
        unknowns.append("data domain is ambiguous; instrument hypotheses are suppressed or weak")
    unknowns.append("engineering units of every signal")
    unknowns.append("which process each cluster corresponds to")
    roles_count: dict[str, int] = {}
    for d in descriptors:
        roles_count[d.structural_role] = roles_count.get(d.structural_role, 0) + 1
    return {
        "generated_by": "template",
        "llm": llm_info or {"ok": False},
        "dataset": {"n_rows": schema.n_rows, "n_signals": len(descriptors), "n_groups": schema.n_groups, "grouping_method": schema.grouping_method, "time_column": schema.time_column, "sample_period_seconds": schema.sample_period_seconds, "domain_likelihood": domain.get("domain_likelihood"), "domain_explanation": domain.get("explanation"), "assumptions": schema.assumptions, "label_columns_excluded": schema.label_columns, "meta_columns_excluded": schema.meta_columns},
        "roles_count": roles_count,
        "signals": sig_entries,
        "relations_summary": {"n_pairs": len(relations.get("pairs", [])), "clusters": relations.get("clusters", {}), "leaders": relations.get("leaders", {}), "redundant": [r["signal"] for r in relations.get("redundancy", []) if r.get("derived")]},
        "unknowns": unknowns,
    }


def write_catalog(ws, descriptors: list[SignalDescriptor]) -> None:
    ws.write_json("signals", [d.model_dump() for d in descriptors])
    schema = ws.schema()
    relations = ws.read_json("relations", {}) or {}
    domain = ws.read_json("domain", {}) or {}
    if schema is not None:
        und = ws.read_json("understanding.json", None)
        ws.write_json("understanding.json", build_understanding(ws, descriptors, relations, domain, schema, llm_info=(und or {}).get("llm")))


def run_profile(ws, settings, ctx: dict[str, Any]) -> dict[str, Any]:
    t0 = time.time()
    progress = _progress(ctx)
    options = dict(ctx.get("options") or {})
    schema = ws.schema()
    if schema is None:
        raise RuntimeError("schema.json missing: run ingest first")
    signal_cols = list(schema.signal_columns)
    aliases = [schema.signal_alias.get(c, f"S{i + 1:02d}") for i, c in enumerate(signal_cols)]
    col_index = {c: i for i, c in enumerate(schema.columns)}
    ptypes = {r[0]: r[1] for r in ws.duckdb().execute("DESCRIBE dataset").fetchall()}
    if not signal_cols:
        ws.write_json("signals", [])
        ws.write_json("relations", {"signals": [], "pairs": [], "clusters": {}, "redundancy": [], "leaders": {}})
        ws.write_json("domain", {"domain_likelihood": schema.domain_likelihood, "explanation": "no signal columns", "evidence_ids": []})
        return {"n_signals": 0, "message": "no signal columns to profile"}

    # ---------------- sample + fingerprints
    progress(0.02, "loading profile subsample")
    sample = load_dynamics_sample(ws, settings, signal_cols)
    e_s = ws.evidence.add("sampling", f"profile subsample: {sample['description']['n_rows']} rows from {sample['description']['n_chunks']} {sample['description']['method'].replace('_', ' ')} ({sample['description']['fraction']:.1%} of {schema.n_rows} rows)", values=sample["description"], computed_by="profile.fingerprints.load_dynamics_sample", n_samples=sample["description"]["n_rows"])
    progress(0.05, "fingerprints")
    fps = compute_fingerprints(ws, settings, signal_cols, sample, progress=lambda f, m: progress(0.05 + 0.4 * f, m))

    # ---------------- relations
    progress(0.45, "relations")
    fps_by_alias = {schema.signal_alias.get(c, c): fps[c] for c in signal_cols}
    rel = compute_relations(ws, settings, aliases, sample, {c: fps[c] for c in signal_cols}, max_lag=int(options.get("max_lag", 30)), progress=lambda f, m: progress(0.45 + 0.25 * f, m))
    rel["alias_to_column"] = {a: c for a, c in zip(aliases, signal_cols)} if not settings.ingest.blind_mode else {}
    red_by_alias = {r["signal"]: r for r in rel.get("redundancy", [])}
    cluster_of = {m: cid for cid, members in rel.get("clusters", {}).items() for m in members}
    related: dict[str, list[dict[str, Any]]] = {a: [] for a in aliases}
    for p in rel.get("pairs", []):
        related[p["a"]].append({"signal": p["b"], "r": p["r"], "lag": p["lag"], "leads": p["lag"] > 0})
        related[p["b"]].append({"signal": p["a"], "r": p["r"], "lag": p["lag"], "leads": False})
    for a in related:
        related[a].sort(key=lambda x: -abs(x["r"]))

    # ---------------- structural roles
    progress(0.72, "structural roles")
    descriptors: list[SignalDescriptor] = []
    role_counts: dict[str, int] = {}
    derived_desc = {x.get("column"): x.get("description") for x in (ws.read_json("derived_signals.json") or []) if isinstance(x, dict)}
    for c, a in zip(signal_cols, aliases):
        fp = fps[c]
        role, conf, reasoning, alts = structural_role(fp, red_by_alias.get(a))
        # manipulated vs measured: a continuous signal that behaves like a valve position / controller output is an
        # actuator. Its value pinned at a limit is then a saturated actuator (a process symptom), not a dead sensor.
        m_score, m_reasons = manipulated_evidence(a, fp, rel)
        fp["manipulated"] = {"score": m_score, "reasons": m_reasons}
        if c in derived_desc:  # a rate derived from an event log: a measurement of how often something happens
            role, conf, reasoning, alts = "continuous_measured", 0.7, f"derived from the event log: {derived_desc[c]}", []
        elif role == "continuous_measured" and m_score >= MANIPULATED_MIN_SCORE:
            role, conf = "actuator_like", round(min(0.85, 0.35 + 0.5 * m_score), 3)
            reasoning = "behaves like a manipulated variable (valve position / controller output): " + "; ".join(m_reasons)
            alts = ["a measured percentage (for example a level in %)"]
        ev_fp = ws.evidence.add("distribution", f"{a}: n={fp.get('count')}, missing {float(fp.get('missing_rate') or 0):.1%}, mean {_fmt(fp.get('mean'))}, std {_fmt(fp.get('std'))}, range [{_fmt(fp.get('min'))}, {_fmt(fp.get('max'))}], {fp.get('n_unique')} distinct, shape {fp.get('distribution_shape')}", signals=[a], values={k: v for k, v in fp.items() if k != "sampling"}, computed_by="profile.fingerprints", n_samples=int(fp.get("count") or 0))
        ev_ids = [ev_fp.id]
        if fp.get("n_samples_dynamics"):
            ev_dyn = ws.evidence.add("dynamics", f"{a}: lag-1 autocorrelation {_fmt(fp.get('autocorr_lag1'), 2)}, noise level {_fmt(fp.get('noise_level'), 2)}, {float(fp.get('stuck_fraction') or 0):.0%} unchanged steps, hold period {_fmt(fp.get('hold_period'), 2)} samples (regularity {_fmt(fp.get('hold_regularity'), 2)}), quantization step {_fmt(fp.get('quantization_step'))}" + (f", dominant period {_fmt(fp.get('dominant_period'), 3)}" if fp.get("dominant_period") else ""), signals=[a], values={k: fp.get(k) for k in ("autocorr_lag1", "autocorr_lag5", "noise_level", "stuck_fraction", "hold_period", "hold_regularity", "level_autocorr", "quantization_step", "jump_ratio", "large_jump_share", "dominant_period", "trend_strength")}, computed_by="profile.fingerprints.dynamics", n_samples=int(fp.get("n_samples_dynamics") or 0))
            ev_ids.append(ev_dyn.id)
            if (fp.get("stuck_fraction") or 0) >= 0.4:
                ev_st = ws.evidence.add("stuck", f"{a}: {float(fp['stuck_fraction']):.0%} of consecutive samples are identical (runs of ~{_fmt(fp.get('hold_period'), 2)})", signals=[a], values={"stuck_fraction": fp.get("stuck_fraction"), "hold_period": fp.get("hold_period"), "hold_regularity": fp.get("hold_regularity")}, computed_by="profile.fingerprints.dynamics", n_samples=int(fp.get("n_samples_dynamics") or 0))
                ev_ids.append(ev_st.id)
        if a in red_by_alias:
            ev_ids.append(red_by_alias[a]["evidence_id"])
        if schema.had_header and not settings.ingest.blind_mode:
            pass  # names are never used for roles; kept as source_column only
        status = "inferred" if conf >= 0.6 else "uncertain"
        inf = ws.inferences.add(a, f"structural role: {role}", status=status, confidence=round(conf, 3), evidence_ids=ev_ids, reasoning=reasoning, source="code", stage=STAGE, alternatives=alts)
        ws.log.record(ACTOR, "inference", "inference", inf.id, {"subject": a, "claim": inf.claim, "confidence": inf.confidence, "status": status}, evidence_ids=ev_ids)
        excluded = role in ("constant",) or (fp.get("count") or 0) == 0
        d = SignalDescriptor(id=a, source_column=c if schema.had_header else None, column_index=int(col_index.get(c, -1)), dtype=str(ptypes.get(c, "")), structural_role=role, structural_confidence=round(conf, 3), cluster_id=cluster_of.get(a), related_signals=related.get(a, [])[:8], fingerprint=fp, confidence=round(conf, 3), inference_ids=[inf.id], evidence_ids=ev_ids, excluded=excluded, excluded_reason=("constant" if role == "constant" else ("all missing" if (fp.get("count") or 0) == 0 else None)))
        if c in derived_desc:  # a derived event-log signal reads as what it is: "share of level = ERROR in the last 50 rows"
            d.display_name = derived_desc[c][:80]
        descriptors.append(d)
        role_counts[role] = role_counts.get(role, 0) + 1

    # ---------------- domain + hypotheses
    progress(0.8, "hypotheses")
    domain_ll = dict(schema.domain_likelihood or {})
    dom_ev = [e.id for e in ws.evidence.all() if e.kind == "domain"]
    dom_inf = [i for i in ws.inferences.all() if i.subject == "dataset" and "looks like" in i.claim]
    domain = {"domain_likelihood": domain_ll, "explanation": dom_inf[-1].reasoning if dom_inf else "", "evidence_ids": dom_ev[-1:], "inference_ids": [dom_inf[-1].id] if dom_inf else [], "hypotheses_enabled": domain_ll.get("sensor_stream", 0.0) >= 0.5}
    ws.write_json("domain", domain)
    n_hyp = 0
    if domain["hypotheses_enabled"]:
        for d in descriptors:
            if d.excluded:
                continue
            for h in heuristic_hypotheses(d, rel):
                inf = ws.inferences.add(d.id, f"{h['kind'].replace('_', ' ')} hypothesis: {h['value']}", status="uncertain", confidence=round(h["confidence"], 3), evidence_ids=d.evidence_ids[:3], reasoning=h["reasoning"], source="code", stage=STAGE)
                d.inference_ids.append(inf.id)
                n_hyp += 1
                if h["kind"] == "instrument" and h["confidence"] > d.instrument_confidence:
                    d.instrument_hypothesis, d.instrument_confidence = h["value"], round(h["confidence"], 3)
                elif h["kind"] == "unit_operation" and h["confidence"] > d.unit_operation_confidence:
                    d.unit_operation_hypothesis, d.unit_operation_confidence = h["value"], round(h["confidence"], 3)
        ws.log.record(ACTOR, "hypotheses", "dataset", ws.run_id, {"n_hypotheses": n_hyp, "layer": "heuristic", "sensor_stream": domain_ll.get("sensor_stream")})
    else:
        ws.log.record(ACTOR, "hypotheses_skipped", "dataset", ws.run_id, {"reason": "domain does not look like a sensor stream", "domain_likelihood": domain_ll})
    llm_info: dict[str, Any] = {"ok": False, "source": "template", "n_accepted": 0}
    budget_left = float(ctx.get("time_budget_s", settings.time_budget_s)) - (time.time() - float(ctx.get("t_start", t0)))
    # optional enhancement: a local model may need two calls of up to local_llm.timeout_s; only when the budget allows
    llm_reserve = 2 * float(getattr(settings.local_llm, "timeout_s", 180)) + 60
    if domain["hypotheses_enabled"] and not options.get("skip_llm") and budget_left > llm_reserve:
        progress(0.85, "asking the language model for hypotheses (optional)")
        llm_info = llm_hypotheses(ws, settings, descriptors, rel, domain_ll, options.get("domain_hint"))
        budget_left = float(ctx.get("time_budget_s", settings.time_budget_s)) - (time.time() - float(ctx.get("t_start", t0)))
        if budget_left > llm_reserve:
            progress(0.9, "asking the language model to name the process units (optional)")
            llm_info["unit_operations"] = llm_unit_operations(ws, settings, descriptors, rel, options.get("domain_hint"))
    if domain["hypotheses_enabled"]:
        n_tests = check_hypotheses(ws, descriptors, rel)
        ws.log.record(ACTOR, "hypothesis_tests", "dataset", ws.run_id, {"n_tested": n_tests})

    # ---------------- artifacts
    progress(0.95, "writing catalog")
    ws.write_json("relations", rel)
    ws.write_json("signals", [d.model_dump() for d in descriptors])
    und = build_understanding(ws, descriptors, rel, domain, schema, llm_info=llm_info)
    ws.write_json("understanding.json", und)
    summary = {"n_signals": len(descriptors), "n_excluded": sum(1 for d in descriptors if d.excluded), "roles": role_counts, "n_clusters": len(rel.get("clusters", {})), "n_pairs": len(rel.get("pairs", [])), "n_redundant": sum(1 for r in rel.get("redundancy", []) if r.get("derived")), "n_hypotheses": n_hyp, "llm": llm_info, "domain_likelihood": domain_ll, "sampling": sample["description"], "seconds": round(time.time() - t0, 2)}
    summary["message"] = f"{len(descriptors)} signals profiled: " + ", ".join(f"{k}={v}" for k, v in sorted(role_counts.items(), key=lambda kv: -kv[1])) + f"; {summary['n_clusters']} clusters, {summary['n_pairs']} related pairs"
    try:  # every hypothesis and hypothesis test of this stage gets its own decision-log entry (one transaction)
        from ..log.stage_log import log_stage_inferences

        summary["n_inferences_logged"] = log_stage_inferences(ws, "profile")
    except Exception:
        pass
    ws.log.record(ACTOR, "catalog", "dataset", ws.run_id, {k: v for k, v in summary.items() if k != "message"}, evidence_ids=[e_s.id])
    progress(1.0, summary["message"])
    return summary
