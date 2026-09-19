"""Plain-language explanations for non-specialists, one per view, built deterministically from the run's
artifacts (template first). An optional local-model pass can reword or translate the text; the template is
always kept and shown as the source when the model is unavailable. Also normalises the sensor-understanding
artifact into the shape the UI and report expect (summary / assumptions / uncertain / hypotheses / signals)."""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

VIEWS = ("understanding", "quality", "monitor", "diagnoses", "assessor", "dataflow")

ROLE_WORDS = {
    "continuous_measured": "continuously varying measurements (typical for flows, pressures, temperatures or levels)",
    "actuator_like": "settings that move in steps within a fixed range (typical for valve positions or setpoints)",
    "held_sampled": "values that update only every few samples (typical for lab analysers or slow instruments)",
    "constant": "constant values that carry no information",
    "derived_redundant": "signals that are almost exact copies or sums of other signals",
    "counter": "counters",
    "timestamp": "time stamps",
    "categorical": "category codes",
    "text": "free text",
    "identifier": "identifiers",
    "unknown": "signals whose role could not be determined",
}
ROLE_ONE = {
    "continuous_measured": "a continuously varying measurement, such as a flow, pressure, temperature or level",
    "actuator_like": "a setting that moves in steps inside a fixed range, such as a valve position or a setpoint",
    "held_sampled": "a value that is refreshed only every few samples, such as a laboratory analyser",
    "constant": "a value that never changes in this data",
    "derived_redundant": "a copy or combination of other signals rather than an independent measurement",
    "counter": "a counter, not a measurement",
    "timestamp": "a time stamp",
    "categorical": "a category code",
    "text": "free text",
    "identifier": "an identifier",
    "unknown": "a signal whose role could not be determined",
}
CAUSE_WORDS = {"process": "process changes", "sensor": "suspected faulty instruments", "data": "data problems", "mixed": "mixed causes", "unknown": "cases that could not be settled"}


def _read(ws, name: str, default: Any = None) -> Any:
    try:
        p = ws.dir / name
        if not p.exists():
            return default
        if name.endswith(".jsonl"):
            out = []
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
            return out
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _conf_words(c: Optional[float]) -> str:
    if c is None:
        return "with unknown confidence"
    if c >= 0.85:
        return "with high confidence"
    if c >= 0.65:
        return "with fair confidence"
    if c >= 0.45:
        return "with moderate confidence"
    return "with low confidence"


def _n(x: Any) -> str:
    try:
        return f"{int(x):,}"
    except Exception:
        return str(x)


def signal_plain(sig: dict[str, Any]) -> str:
    """One or two sentences about a signal for a non-specialist."""
    role = sig.get("human_role_override") or sig.get("structural_role") or "unknown"
    named = f" (named \"{sig['display_name']}\"{' in ' + sig['display_unit'] if sig.get('display_unit') else ''} by an operator)" if sig.get("display_name") else ""
    base = f"{sig['id']}{named} behaves like {ROLE_ONE.get(role, ROLE_ONE['unknown'])}"
    c = sig.get("structural_confidence")
    s = base + (f" ({_conf_words(c)}, {c:.0%})." if isinstance(c, (int, float)) else ".")
    fp = sig.get("fingerprint") or {}
    bits = []
    if isinstance(fp.get("missing_rate"), (int, float)) and fp["missing_rate"] > 0.001:
        bits.append(f"{fp['missing_rate']:.1%} of its values are missing")
    if isinstance(fp.get("stuck_fraction"), (int, float)) and fp["stuck_fraction"] > 0.3 and role not in ("held_sampled", "constant", "actuator_like"):
        bits.append(f"it repeats the same value {fp['stuck_fraction']:.0%} of the time, which may mean it is stale")
    if isinstance(fp.get("noise_level"), (int, float)):
        nl = fp["noise_level"]
        bits.append("it is a noisy signal" if nl > 0.7 else ("it is a smooth signal" if nl < 0.15 else "its noise level is ordinary"))
    if bits:
        s += " " + "; ".join(bits[:2]).capitalize() + "."
    inst = sig.get("instrument_hypothesis")
    if inst:
        ic = sig.get("instrument_confidence")
        s += f" The system guesses it may be a {inst} sensor ({_conf_words(ic)}); this is a hypothesis to confirm, not a fact."
    rel = sig.get("related_signals") or []
    if rel:
        r0 = rel[0]
        lag = r0.get("lag")
        s += f" It moves together with {r0.get('signal')}" + (f", about {abs(int(lag))} samples {'later' if lag and lag > 0 else 'earlier'}" if lag else "") + "."
    if sig.get("excluded"):
        s += f" It is not used for fault detection ({sig.get('excluded_reason') or 'excluded'})."
    return s


def understanding_normalized(ws) -> dict[str, Any]:
    """Shape used by the UI/report: summary, assumptions, uncertain, hypotheses, signals (with plain text)."""
    U = _read(ws, "understanding.json", {}) or {}
    schema = _read(ws, "schema.json", {}) or {}
    signals = _read(ws, "signals.json", []) or []
    baseline = _read(ws, "baseline.json", {}) or {}
    domain = _read(ws, "domain.json", {}) or {}
    infs = []
    try:
        infs = [i.model_dump() for i in ws.inferences.all()]
    except Exception:
        pass
    ds = U.get("dataset") or {}
    n_rows = ds.get("n_rows") or schema.get("n_rows")
    n_groups = ds.get("n_groups") or schema.get("n_groups") or 1
    roles = Counter((s.get("human_role_override") or s.get("structural_role") or "unknown") for s in signals) if signals else Counter(U.get("roles_count") or {})
    if not signals and U.get("roles_count"):
        roles = Counter(U["roles_count"])
    n_sig = len(signals) or ds.get("n_signals") or len(schema.get("signal_columns") or [])
    period = ds.get("sample_period_seconds") or schema.get("sample_period_seconds")
    method = ds.get("grouping_method") or schema.get("grouping_method") or "none"
    rel = U.get("relations_summary") or {}
    n_clusters = rel.get("clusters") if isinstance(rel.get("clusters"), int) else len(rel.get("clusters") or [])
    lk = ds.get("domain_likelihood") or domain.get("domain_likelihood") or schema.get("domain_likelihood") or {}
    dom = max(lk.items(), key=lambda kv: kv[1])[0].replace("_", " ") if lk else "unknown"

    parts = [f"This run analysed {_n(n_rows)} rows of {n_sig} signals" + (f", organised into {_n(n_groups)} groups (runs or segments) that the system found by {method.replace('_', ' ')}" if n_groups and n_groups > 1 else "") + "."]
    if period:
        parts.append(f"Samples are about {period:g} seconds apart.")
    else:
        parts.append("No time stamps were found, so time is counted in samples.")
    role_bits = [f"{v} are {ROLE_WORDS.get(k, k)}" for k, v in roles.most_common(5) if v]
    if role_bits:
        parts.append("Of the signals, " + "; ".join(role_bits) + ".")
    if n_clusters:
        parts.append(f"The signals form {n_clusters} clusters of related signals that tend to move together, often with a time delay, which is how the system guesses which ones belong to the same part of the process.")
    parts.append(f"The data looks most like a {dom} ({lk.get(max(lk, key=lk.get), 0):.0%} likelihood)." if lk else "The kind of data (sensor stream or business records) could not be determined.")
    parts.append("The system does not know the engineering units or the physical meaning of any signal; instrument and unit-operation names shown are hypotheses with a confidence, based on how each signal behaves and what it correlates with.")
    summary = " ".join(parts)

    assumptions = list(schema.get("assumptions") or [])
    for a in baseline.get("assumptions") or []:
        if a not in assumptions:
            assumptions.append(a)
    uncertain = list(U.get("unknowns") or [])
    for i in infs:
        if i.get("status") == "uncertain" and i.get("subject") == "dataset" and i.get("claim") not in uncertain:
            uncertain.append(i["claim"])
    low_inst = [s["id"] for s in signals if s.get("instrument_hypothesis") and (s.get("instrument_confidence") or 0) < 0.5]
    if low_inst:
        uncertain.append(f"Instrument guesses for {len(low_inst)} signals have confidence below 50 % ({', '.join(low_inst[:8])}{'…' if len(low_inst) > 8 else ''}).")
    hyps = [{"inference_id": i["id"], "subject": i["subject"], "claim": i["claim"], "confidence": i.get("confidence"), "status": i.get("status"), "human_status": i.get("human_status")} for i in infs if i.get("subject") == "dataset"]
    sig_plain = {s["id"]: signal_plain(s) for s in signals}
    U_signals = U.get("signals") or []
    for entry in U_signals:
        entry.setdefault("plain", sig_plain.get(entry.get("id") or entry.get("alias"), ""))
    clusters = None
    if isinstance(rel.get("clusters"), dict):
        clusters = rel["clusters"]
    out = dict(U)
    out.update({"summary": summary, "assumptions": assumptions, "uncertain": uncertain, "hypotheses": hyps, "signal_plain": sig_plain, "signals": U_signals or [{"id": s["id"], "role": s.get("structural_role"), "confidence": s.get("confidence"), "plain": sig_plain[s["id"]]} for s in signals], "inference_ids": [h["inference_id"] for h in hyps], "normalized": True})
    if clusters and "clusters" not in out:
        out["clusters"] = clusters
    return out


# ------------------------------------------------------------------------------------------------
# per-view explanations
# ------------------------------------------------------------------------------------------------


def _plain_understanding(ws) -> list[str]:
    U = understanding_normalized(ws)
    paras = [U["summary"]]
    if U.get("assumptions"):
        paras.append("What the system assumed: " + " ".join(U["assumptions"][:4]))
    if U.get("uncertain"):
        paras.append("What remains uncertain: " + " ".join(U["uncertain"][:4]))
    paras.append("How to use this page: click a signal to see the evidence behind its role, the signals it moves with, and the hypotheses; you can correct a role, and your correction is logged and used downstream.")
    return paras


def _plain_quality(ws) -> list[str]:
    trust = _read(ws, "trust.jsonl", []) or []
    checks = _read(ws, "checks.jsonl", []) or []
    if not trust and not checks:
        return ["Data-quality checks have not run yet for this run."]
    n_b = len(trust)
    bad = [t for t in trust if not t.get("trusted")]
    mean_trust = sum(t.get("trust_score", 0) for t in trust) / max(1, n_b)
    fails = Counter(c["check_type"] for c in checks if c.get("status") == "fail")
    warns = Counter(c["check_type"] for c in checks if c.get("status") == "warn")
    local_n = sum(len(t.get("local_untrusted") or []) for t in trust)
    words = {"stuck": "frozen or stale values", "missing": "missing values", "dropout": "signals with no values at all", "out_of_range": "values far outside the usual range", "impossible_value": "impossible values", "unit_shift": "sudden changes of scale (unit or decimal-point errors)", "duplicate_rows": "exact duplicate rows", "duplicate_key": "duplicate keys", "gap": "gaps in time", "out_of_order": "time stamps out of order", "saturation": "signals stuck at their limit", "quantization_change": "changes of measurement precision", "sign_violation": "unexpected negative values", "stale": "signals that stopped updating", "redundancy_violation": "copies that no longer agree with their source", "empty_rows": "empty rows"}
    p1 = f"The data was checked in {n_b} batches before any fault reasoning. Average trust is {mean_trust:.0%}."
    if bad:
        p1 += f" {len(bad)} batch(es) cannot be trusted: {bad[0].get('statement', '')[:220]}"
    else:
        p1 += " No batch had to be rejected as untrustworthy."
    if local_n:
        p1 += f" In addition, {local_n} short stretches were marked unreliable for a single signal; detection treats those rows as data problems and does not blame the process for them."
    top = [f"{words.get(k, k.replace('_', ' '))} ({v})" for k, v in (fails + warns).most_common(4)]
    p2 = ("The most common findings were " + ", ".join(top) + ".") if top else "No data-quality problems were found."
    p3 = "Why this matters: a frozen or missing sensor and a real process upset can look alike in a chart. These checks separate the two, so an alarm is not raised on bad data and a real fault is not hidden by it. Operating rules you type in plain language become extra checks here."
    return [p1, p2, p3]


def _plain_monitor(ws) -> list[str]:
    meta = _read(ws, "detect_meta.json", {}) or {}
    base = _read(ws, "baseline.json", {}) or {}
    gs = _read(ws, "group_scores.json", {}) or {}
    flags = _read(ws, "flags.jsonl", []) or []
    if not meta:
        return ["Drift and anomaly detection has not run yet for this run."]
    ev = meta.get("events") or {}
    n_groups = ev.get("n_groups") or gs.get("n_groups") or 1
    over = ev.get("n_groups_over_threshold") if ev.get("n_groups_over_threshold") is not None else gs.get("n_groups_over_threshold")
    dets = meta.get("detectors_used") or []
    strat = str(base.get("strategy", "")).replace("_", " ")
    conf = base.get("confidence")
    p1 = f"The monitor first learned what normal operation looks like from the data itself (no labels were used; strategy '{strat}', {_conf_words(conf)}), then scored every row on how far it departs from that normal pattern, using {len(dets)} complementary detectors ({', '.join(dets)}). Each group was scored by models that never saw that group, so the scores are honest."
    kinds = Counter(f.get("kind") for f in flags)
    causes = Counter(f.get("likely_cause_class") for f in flags)
    p2 = f"{_n(over)} of {_n(n_groups)} groups went above the threshold somewhere; the strongest {len([f for f in flags if f.get('kind') in ('anomaly', 'drift')])} events are listed as flags" + (f" ({kinds.get('changepoint', 0)} with a located onset, {kinds.get('drift', 0)} gradual drifts, {kinds.get('cascade', 0)} cascades)" if flags else "") + "."
    if causes:
        p2 += " Of the flags, " + ", ".join(f"{v} point to {CAUSE_WORDS.get(k, k)}" for k, v in causes.most_common(4)) + "."
    susp = _read(ws, "suspicious_rows.json", {}) or {}
    if susp.get("n_rows"):
        p2 += " " + str(susp.get("headline", "")) + " " + str(susp.get("wording", ""))
    p3 = "How to read the chart: the line is the deviation score; 1.0 is the largest deviation still seen in normal operation, so 3x means three times that. Click a flag to see which signals are responsible and to ask why in plain language."
    return [p1, p2, p3]


def _plain_diagnoses(ws) -> list[str]:
    diags = _read(ws, "diagnoses.jsonl", []) or []
    if not diags:
        return ["No diagnoses yet for this run."]
    causes = Counter(d.get("cause_class") for d in diags)
    p1 = f"{len(diags)} diagnoses were produced: " + ", ".join(f"{v} {CAUSE_WORDS.get(k, k)}" for k, v in causes.most_common()) + "."
    top = sorted(diags, key=lambda d: -(d.get("confidence") or 0) * (1.0 if d.get("cause_class") == "process" else 0.9))[:3]
    lines = []
    for d in top:
        sig = ", ".join(r.get("signal") for r in (d.get("ranked_signals") or [])[:2])
        lines.append(f"{d['id']}: {d.get('fault_type')} (group {d.get('group_id')}, signals {sig}, {_conf_words(d.get('confidence'))})")
    p2 = "Most confident: " + "; ".join(lines) + "." if lines else ""
    crit = Counter((d.get("critique") or {}).get("verdict") for d in diags)
    p3 = f"Every diagnosis was challenged before being shown: {crit.get('supported', 0)} held up, {crit.get('weakened', 0)} were weakened, {crit.get('rejected', 0)} rejected. Each one lists the steps of its reasoning, what it assumed, what stays uncertain, and the evidence it relies on. You can accept, question or override any of them; your decision is logged."
    return [p for p in (p1, p2, p3) if p]


def _plain_assessor(ws) -> list[str]:
    a = _read(ws, "assessor.json", {}) or {}
    if not a:
        return ["The data assessor has not run yet."]
    cs = a.get("combined_score")
    fit = (a.get("fitness") or {}).get("fitness_score")
    cov = (a.get("coverage") or {}).get("balance")
    dq = (a.get("dq_scores") or {}).get("overall")
    p1 = f"Overall the dataset scores {cs:.0%} of 1" if isinstance(cs, (int, float)) else "The dataset's overall score is not available"
    comps = []
    if isinstance(fit, (int, float)):
        comps.append(f"how well the detectors can learn from it ({fit:.0%})")
    if isinstance(cov, (int, float)):
        comps.append(f"how evenly the operating conditions are represented ({cov:.0%})")
    if isinstance(dq, (int, float)):
        comps.append(f"basic data quality ({dq:.0%})")
    if comps:
        p1 += ", combining " + ", ".join(comps) + "."
    else:
        p1 += "."
    more = a.get("more_data_verdict") or {}
    less = a.get("less_data_verdict") or {}
    def _verdict(v: dict, yes: str, no: str, unclear: str) -> str:
        import re as _re

        why = _re.sub(r"^\s*(yes|no|unclear)\s*[.,:;!-]*\s*", "", str(v.get("why") or ""), flags=_re.I).strip()
        why = (why[0].lower() + why[1:]) if why and not why[:2].isupper() else why
        head = yes if v.get("would_help") else (no if v.get("would_help") is False else unclear)
        return (head + (": " + why if why else ".")).strip()

    p2 = _verdict(more, "Adding more data of the same kind would probably help", "Adding more data of the same kind would probably not help", "It is unclear whether more data would help")
    p3 = _verdict(less, "Removing some of the data would probably help", "Removing data would probably not help", "It is unclear whether removing data would help")
    recs = a.get("recommendations") or []
    p4 = ("Recommendations: " + " ".join(f"({i + 1}) {r.get('text')}" for i, r in enumerate(recs[:3]))) if recs else ""
    p5 = "Ask in your own words below (for example: would dropping S05 improve quality? what if we add 20 more runs?). Nothing is changed until you approve it."
    return [p for p in (p1, p2, p3, p4, p5) if p]


def _plain_dataflow(ws, settings) -> list[str]:
    try:
        from ..llm.ledger import data_flow_statement, summary

        st = data_flow_statement(ws, settings)
        sm = summary(ws, last_n=0)
    except Exception as e:
        return [f"Data-flow record unavailable: {e}"]
    prof = settings.profile
    p1 = ("Everything in this run stayed on this machine: the profile is 'no-egress', so no network model was called." if prof == "no-egress" else f"The active profile is '{prof}': raw data never leaves the machine; only derived summaries that pass the egress guard may be sent to the external model.")
    p2 = f"Model calls recorded: {sm.get('n_records', 0)} (local {sm.get('by_route', {}).get('local', 0)}, external {sm.get('n_external', 0)}, blocked by the guard {sm.get('n_blocked', 0)})."
    return [p1, p2, st]


def build_plain(ws, settings, view: str) -> dict[str, Any]:
    if view == "understanding":
        paras = _plain_understanding(ws)
    elif view == "quality":
        paras = _plain_quality(ws)
    elif view == "monitor":
        paras = _plain_monitor(ws)
    elif view == "diagnoses":
        paras = _plain_diagnoses(ws)
    elif view == "assessor":
        paras = _plain_assessor(ws)
    elif view == "dataflow":
        paras = _plain_dataflow(ws, settings)
    else:
        paras = []
    return {"view": view, "paragraphs": paras, "source": "template", "language": "en"}


LANG_NAMES = {"en": "English", "fi": "Finnish", "sv": "Swedish"}


def _artifact_stamp(ws) -> float:
    latest = 0.0
    for name in ("schema.json", "signals.json", "trust.jsonl", "flags.jsonl", "diagnoses.jsonl", "assessor.json", "egress_ledger.jsonl", "detect_meta.json"):
        p = ws.dir / name
        if p.exists():
            latest = max(latest, p.stat().st_mtime)
    return latest


def plain_for(ws, settings, view: str, lang: str = "en", enhance: bool = False) -> dict[str, Any]:
    """Cached per (view, lang, enhance). enhance=True asks the LOCAL model to reword (and translate when
    lang != en); the template is returned immediately when no model is reachable."""
    lang = (lang or "en").lower()[:2]
    cache = ws.dir / f"plain_{view}_{lang}_{'m' if enhance else 't'}.json"
    stamp = _artifact_stamp(ws)
    if cache.exists():
        try:
            d = json.loads(cache.read_text(encoding="utf-8"))
            if d.get("stamp") == stamp:
                return d
        except Exception:
            pass
    base = build_plain(ws, settings, view)
    out = dict(base)
    out["stamp"] = stamp
    if enhance and base["paragraphs"]:
        try:
            from ..llm import complete

            text = "\n\n".join(base["paragraphs"])
            payload = {"report_sections": [{"title": view, "text": text}], "instruction": f"Rewrite the text for a person with no data-science background, in {LANG_NAMES.get(lang, 'English')}. Keep every number, percentage and identifier (S07, FLAG-000001, B00003) exactly. Return plain prose paragraphs, no JSON, no headings, no bullet lists."}
            t0 = time.time()
            res = complete("report_narrative", payload, purpose=f"plain-language {view} ({lang})", ws=ws, settings=settings, language=lang, max_tokens=900)
            txt = ""
            if res.ok:
                d = res.data if isinstance(res.data, dict) else None
                if d is None and res.text and res.text.strip().startswith("{"):
                    try:
                        d = json.loads(res.text)
                    except Exception:
                        d = None
                if isinstance(d, dict):
                    # report_narrative answers {title, executive_summary, sections:[{heading, body}], uncertainty}
                    paras: list[str] = []
                    for k in ("executive_summary", "summary", "text", "narrative", "content"):
                        if isinstance(d.get(k), str) and d[k].strip():
                            paras.append(d[k].strip())
                            break
                    for sec in d.get("sections") or []:
                        if isinstance(sec, dict):
                            body = str(sec.get("body") or sec.get("text") or "").strip()
                            if body and body not in paras:
                                paras.append(body)
                    unc = d.get("uncertainty")
                    if isinstance(unc, list) and unc:
                        paras.append("What remains uncertain: " + " ".join(str(u) for u in unc[:4]))
                    txt = "\n\n".join(paras)
                if not txt and res.text and not res.text.strip().startswith("{"):
                    txt = res.text
            if txt.strip():
                out["paragraphs"] = [p.strip() for p in txt.strip().split("\n\n") if p.strip()]
                out["source"] = res.source
                out["language"] = lang
                out["seconds"] = round(time.time() - t0, 1)
                out["template_paragraphs"] = base["paragraphs"]
            else:
                out["note"] = "no local model available; showing the template text" + ("" if lang == "en" else " in English")
        except Exception as e:
            out["note"] = f"model rewording failed: {str(e)[:120]}"
    try:
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return out
