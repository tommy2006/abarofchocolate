"""Egress ledger: every model call (local and external, attempted or completed) becomes an EgressRecord in
workspace/<run>/egress_ledger.jsonl and a hash-chained decision-log entry (action "egress").
With ws=None an in-memory ledger is kept so tests and ad-hoc calls still have a trail.

Records of `tpm guard-demo` carry guard_result "demo_allowed" / "demo_blocked": the guard's verdict on a payload that
was shown, never sent. They are kept out of every count of real model calls (summary, usage, budget, statement).

narrative_coverage(ws) says who wrote the explanations of a run: how many diagnoses / critiques / report summaries a
model wrote (local or external, which model) and how many come from the evidence-based templates, and why."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter
from typing import Any, Optional

from ..config import Settings
from ..contracts import EgressRecord, now_iso

_MEM: list[EgressRecord] = []
_MEM_MAX = 1000
_lock = threading.RLock()
_counters: dict[str, int] = {}
DEMO_RESULTS = ("demo_allowed", "demo_blocked")  # guard demonstration: shown, never sent


def is_demo(r: Any) -> bool:
    g = r.get("guard_result") if isinstance(r, dict) else getattr(r, "guard_result", None)
    return str(g or "") in DEMO_RESULTS


def sha256_of(obj: Any) -> str:
    if isinstance(obj, (bytes, bytearray)):
        data = bytes(obj)
    elif isinstance(obj, str):
        data = obj.encode("utf-8")
    else:
        data = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _ws_key(ws: Any) -> str:
    return str(getattr(ws, "dir", "")) if ws is not None else "__memory__"


def next_id(ws: Any = None) -> str:
    key = _ws_key(ws)
    with _lock:
        if key not in _counters:
            n = 0
            if ws is not None:
                try:
                    for rec in ws.read_jsonl("egress_ledger"):
                        try:
                            n = max(n, int(str(rec.get("id", "EGR-0")).split("-")[-1]))
                        except Exception:
                            continue
                except Exception:
                    n = 0
            else:
                n = len(_MEM)
            _counters[key] = n
        _counters[key] += 1
        return f"EGR-{_counters[key]:06d}"


def record(ws: Any, rec: EgressRecord) -> EgressRecord:
    """Persist one record (jsonl + decision log) or keep it in memory when ws is None. Never raises."""
    if not rec.id:
        rec.id = next_id(ws)
    if not rec.ts:
        rec.ts = now_iso()
    with _lock:
        _MEM.append(rec)
        if len(_MEM) > _MEM_MAX:
            del _MEM[: len(_MEM) - _MEM_MAX]
    if ws is None:
        return rec
    try:
        with _lock:  # several threads (complete_many, chat turns) and Workspace objects append to the same file
            ws.append_jsonl("egress_ledger", rec.model_dump())
    except Exception:
        pass
    try:
        actor = f"llm:{rec.route}:{rec.model or 'none'}"
        payload = rec.model_dump(exclude={"payload_preview"})
        payload["payload_preview"] = rec.payload_preview[:120] if rec.payload_preview else ""
        ws.log.record(actor, "egress", "egress", rec.id, payload)
    except Exception:
        pass
    return rec


def read(ws: Any = None) -> list[EgressRecord]:
    if ws is None:
        with _lock:
            return list(_MEM)
    out: list[EgressRecord] = []
    try:
        for d in ws.read_jsonl("egress_ledger"):
            try:
                out.append(EgressRecord(**d))
            except Exception:
                continue
    except Exception:
        pass
    return out


def memory_records() -> list[EgressRecord]:
    with _lock:
        return list(_MEM)


def clear_memory() -> None:
    with _lock:
        _MEM.clear()
        _counters.pop("__memory__", None)


def summary(ws: Any = None, last_n: int = 20) -> dict[str, Any]:
    """Counts of the real model calls of a run (guard-demonstration records are listed apart under "demo")."""
    all_recs = read(ws)
    recs = [r for r in all_recs if not is_demo(r)]
    demo = [r for r in all_recs if is_demo(r)]
    by = lambda attr: dict(Counter(getattr(r, attr) or "" for r in recs))  # noqa: E731
    ext = [r for r in recs if r.route == "external"]
    return {
        "n_demo": len(demo),
        "demo": [{"id": r.id, "task": r.task, "guard_result": r.guard_result, "guard_reason": r.guard_reason, "purpose": r.purpose} for r in demo[-6:]],
        "n_records": len(recs),
        "n_external": len(ext),
        "n_external_sent": len([r for r in ext if r.guard_result == "allowed" and r.ok]),
        "n_blocked": len([r for r in recs if r.guard_result == "blocked"]),
        "n_fallback": len([r for r in recs if r.guard_result == "fallback"]),
        "n_failed": len([r for r in recs if not r.ok]),
        "by_route": by("route"),
        "by_model": by("model"),
        "by_task": by("task"),
        "by_guard_result": by("guard_result"),
        "total_payload_bytes": int(sum(r.payload_bytes for r in recs)),
        "external_payload_bytes": int(sum(r.payload_bytes for r in ext if r.guard_result == "allowed" and r.ok)),
        "total_latency_ms": int(sum(r.latency_ms or 0 for r in recs)),
        "last": [r.model_dump() for r in recs[-last_n:]],
    }


def _avg(values: list[int]) -> Optional[int]:
    return int(sum(values) / len(values)) if values else None


def usage(ws: Any = None, settings: Optional[Settings] = None) -> dict[str, Any]:
    """External-model use of one run, for the budget and the Data-flow view: calls, tokens, blocks, what is left of
    the per-run call budget, and average latency per route (overall and per task). Guard demonstrations are not calls."""
    recs = [r for r in read(ws) if not is_demo(r)]
    if settings is None:
        settings = getattr(ws, "settings", None)
    ext = [r for r in recs if r.route == "external"]
    ext_ok = [r for r in ext if r.ok and r.guard_result == "allowed"]
    local_ok = [r for r in recs if r.route == "local" and r.ok]
    by_task: dict[str, dict[str, Any]] = {}
    for task in sorted({r.task for r in recs}):
        t_ext = [r for r in ext_ok if r.task == task]
        t_loc = [r for r in local_ok if r.task == task]
        by_task[task] = {
            "external_ok": len(t_ext),
            "local_ok": len(t_loc),
            "blocked": len([r for r in ext if r.task == task and r.guard_result == "blocked"]),
            "input_tokens": int(sum(r.input_tokens for r in t_ext)),
            "output_tokens": int(sum(r.output_tokens for r in t_ext)),
            "avg_latency_ms_external": _avg([r.latency_ms for r in t_ext if r.latency_ms is not None]),
            "avg_latency_ms_local": _avg([r.latency_ms for r in t_loc if r.latency_ms is not None]),
        }
    out_tokens = int(sum(r.output_tokens for r in ext))
    cap_calls = int(settings.external_llm.max_calls_per_run) if settings is not None else None
    cap_tokens = int(settings.external_llm.max_output_tokens_per_run) if settings is not None else None
    return {
        "external_calls": len(ext),
        "external_ok": len(ext_ok),
        "input_tokens": int(sum(r.input_tokens for r in ext)),
        "output_tokens": out_tokens,
        "blocked": len([r for r in ext if r.guard_result == "blocked"]),
        "budget_refused": len([r for r in ext if r.guard_result == "budget"]),
        "max_calls_per_run": cap_calls,
        "budget_left_calls": max(0, cap_calls - len(ext_ok)) if cap_calls is not None else None,
        "max_output_tokens_per_run": cap_tokens,
        "budget_left_output_tokens": max(0, cap_tokens - out_tokens) if cap_tokens is not None else None,
        "avg_latency_ms_external": _avg([r.latency_ms for r in ext_ok if r.latency_ms is not None]),
        "avg_latency_ms_local": _avg([r.latency_ms for r in local_ok if r.latency_ms is not None]),
        "by_task": by_task,
    }


def data_flow_statement(ws: Any, settings: Settings, language: str = "en", extras: bool = True) -> str:
    """Plain-language 'what left the operator environment, to which model, and why' for the Data-flow record. With
    extras=True (the UI) it ends with who wrote the explanations (narrative_coverage) and, when `tpm guard-demo` ran,
    what the demonstration showed; the report renders those two in blocks of their own and passes extras=False."""
    s = summary(ws, last_n=0)
    all_recs = read(ws)
    recs = [r for r in all_recs if not is_demo(r)]
    extras = _statement_extras(ws, settings, all_recs, language) if extras else []
    prof = settings.active_profile
    lines: list[str] = []
    lines.append(f"Profile: {settings.profile} ({'external model allowed through the egress guard' if prof.allow_external else 'no network model calls allowed'}).")
    lines.append(f"Local model configured: {settings.local_llm.model} via {settings.local_llm.provider} at {settings.local_llm.base_url} (data stays on this machine).")
    used_local = Counter(r.model for r in recs if r.route == "local" and r.ok and r.model)
    if used_local:
        used = ", ".join(f"{m} x{n}" for m, n in used_local.most_common())
        note = "" if settings.local_llm.model in used_local else f" (configured model not pulled; fell back to the first available model)"
        lines.append(f"Local model actually used in this run: {used}{note}.")
    if prof.allow_external:
        ext = settings.external_llm
        ep = ext.base_url or "provider default endpoint"
        where = "; ".join(x for x in (f"run by {ext.operator}" if ext.operator else "", f"located in {ext.location}" if ext.location else "") if x)
        lines.append(f"External model: {ext.model} via {ext.provider} ({ep}{'; ' + where if where else ''}).")
    lines.append("")
    if not recs:
        lines.append("No language-model calls have been made in this run. Nothing left the operator environment.")
        return "\n".join(lines + extras)
    lines.append(f"Model calls recorded: {s['n_records']} (local: {s['by_route'].get('local', 0)}, external attempts: {s['n_external']}).")
    if s["n_external_sent"]:
        tasks = Counter(r.task for r in recs if r.route == "external" and r.guard_result == "allowed" and r.ok)
        types = Counter(t for r in recs if r.route == "external" and r.guard_result == "allowed" and r.ok for t in r.artifact_types)
        lines.append(
            f"{s['n_external_sent']} payload(s), {s['external_payload_bytes']} bytes in total, were sent to the external model. "
            f"Tasks: {', '.join(f'{k} x{v}' for k, v in tasks.items())}. Artifact types: {', '.join(f'{k} x{v}' for k, v in types.items()) or 'n/a'}. "
            "Each payload passed the egress guard: derived artifacts only (aliases, aggregates, statements, evidence IDs), no raw rows, no long series, no categorical values; "
            f"numbers rounded to {settings.guard.external_sig_digits} significant digits, dates, column names, file names and single readings removed."
        )
        tok_in, tok_out = int(sum(r.input_tokens for r in recs if r.route == "external")), int(sum(r.output_tokens for r in recs if r.route == "external"))
        if tok_in or tok_out:
            lines.append(f"Tokens reported by the external provider: {tok_in} in, {tok_out} out.")
    else:
        lines.append("No payload was sent to an external model. Nothing left the operator environment.")
    if s["n_blocked"]:
        reasons = Counter(r.guard_reason.split(" at ")[0] for r in recs if r.guard_result == "blocked")
        lines.append(f"The guard blocked {s['n_blocked']} payload(s): " + "; ".join(f"{k} (x{v})" for k, v in reasons.items()) + ". Those tasks were answered locally or by a template.")
    n_budget = len([r for r in recs if r.guard_result == "budget"])
    if n_budget:
        lines.append(f"{n_budget} call(s) were not sent because the external budget of this run was used up (external_llm.max_calls_per_run / max_output_tokens_per_run); they ran locally.")
    if s["n_fallback"]:
        lines.append(f"{s['n_fallback']} call(s) ran on the local model as a fallback after a block or an external failure.")
    if s["n_failed"]:
        lines.append(f"{s['n_failed']} call(s) failed (model unavailable or error) and were answered by code templates.")
    lines.append("")
    lines.append(local_why(recs))
    return "\n".join(lines + extras)


LOCAL_MAY_SEE_MORE = (
    "Why local calls may see more: the local model runs on this machine and nothing it reads leaves it, so the guard removes "
    "nothing from its prompts, and the chat's local tools may read exact rows to answer 'why this row?'."
)
_AUDIT_ON = " The guard still checks every local call in audit mode: its ledger record says what would have been removed had the call gone out."
_AUDIT_OLD = " (This run's local calls were recorded before the guard's audit mode existed, so their records do not say what it would have removed.)"
_WHY_TAIL = (" The external model only receives derived artifacts that passed the guard, so that it can write hypotheses and "
             "explanations on top of them, citing evidence IDs. Every call, including blocked and failed ones, is in "
             "egress_ledger.jsonl and in the hash-chained decision log.")


def local_why(recs: list[EgressRecord]) -> str:
    """Why local calls may see more, and whether this run's local records carry the guard's audit."""
    local = [r for r in recs if r.route == "local" and r.guard_result == "n/a"]
    audited = [r for r in local if (r.sanitizer or {}).get("mode") == "audit"]
    return LOCAL_MAY_SEE_MORE + (_AUDIT_OLD if local and not audited else _AUDIT_ON) + _WHY_TAIL


# ----------------------------------------------------------------------------------------------
# who wrote the explanations: model or template, and why
# ----------------------------------------------------------------------------------------------

_SOURCE_RE = re.compile(r"llm-(local|external):([^+]+)")

_COVERAGE_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "none": "This run has no diagnoses, so no explanation was written.",
        "all": "All {n_total} diagnoses have a model-written explanation ({models}).",
        "head": "{n_model} of {n_total} diagnoses have a model-written explanation{models_paren}; the other {n_template} use the evidence-based template, {why}; the template states the same findings with their evidence IDs, and the chat explains any diagnosis on request.",
        "head_zero": "None of the {n_total} diagnoses has a model-written explanation; all use the evidence-based template, {why}; the template states the same findings with their evidence IDs, and the chat explains any diagnosis on request.",
        "why_local": "by design: a local model needs about {sec} s per explanation, so the diagnose stage spends at most half of its time budget ({allowance} s here) on the strongest diagnoses first",
        "why_local_nosec": "by design: the diagnose stage lets the local model explain the strongest diagnoses first and stops when half of its time budget ({allowance} s here) is used",
        "why_external": "by design: the external model explains at most {cap} diagnoses per run (external_llm.max_narratives_per_run), the strongest first",
        "why_no_llm": "because this run was made with language models switched off (--no-llm)",
        "why_no_model": "because no language model was reachable when the run was made (Ollama not running or no model pulled)",
        "why_generic": "by design: a model explains the strongest diagnoses first while the stage's model time allowance lasts",
        "local": "local model {m}", "external": "external model {m}",
        "critiques": "{n_model} of {n_total} critiques (devil's advocate) were written with a model; the others are the code checks of the template.",
        "reports": "Report summary: {items}.", "report_model": "{lang}: {m}", "report_template": "{lang}: template text only (no model summary)",
    },
    "fi": {
        "none": "Tässä ajossa ei ole diagnooseja, joten selityksiä ei kirjoitettu.",
        "all": "Kaikilla {n_total} diagnoosilla on kielimallin kirjoittama selitys ({models}).",
        "head": "{n_model} diagnoosilla {n_total}:stä on kielimallin kirjoittama selitys{models_paren}; loput {n_template} käyttävät näyttöön perustuvaa valmispohjaa, {why}; pohja kertoo samat havainnot näyttötunnisteineen, ja keskustelu selittää minkä tahansa diagnoosin pyynnöstä.",
        "head_zero": "Yhdelläkään {n_total} diagnoosista ei ole kielimallin kirjoittamaa selitystä; kaikki käyttävät näyttöön perustuvaa valmispohjaa, {why}; pohja kertoo samat havainnot näyttötunnisteineen, ja keskustelu selittää minkä tahansa diagnoosin pyynnöstä.",
        "why_local": "tarkoituksella: paikallinen malli tarvitsee noin {sec} s selitystä kohden, joten diagnoosivaihe käyttää enintään puolet aikabudjetistaan (tässä {allowance} s) vahvimpiin diagnooseihin ensin",
        "why_local_nosec": "tarkoituksella: diagnoosivaihe antaa paikallisen mallin selittää vahvimmat diagnoosit ensin ja lopettaa, kun puolet sen aikabudjetista (tässä {allowance} s) on käytetty",
        "why_external": "tarkoituksella: ulkoinen malli selittää enintään {cap} diagnoosia ajoa kohden (external_llm.max_narratives_per_run), vahvimmat ensin",
        "why_no_llm": "koska ajo tehtiin kielimallit pois päältä (--no-llm)",
        "why_no_model": "koska yhtään kielimallia ei tavoitettu ajon aikana (Ollama ei käynnissä tai mallia ei ole ladattu)",
        "why_generic": "tarkoituksella: malli selittää vahvimmat diagnoosit ensin niin kauan kuin vaiheen malliaikaa riittää",
        "local": "paikallinen malli {m}", "external": "ulkoinen malli {m}",
        "critiques": "{n_model}/{n_total} kriittisestä arviosta kirjoitettiin kielimallilla; muut ovat valmispohjan koodilla tehtyjä tarkistuksia.",
        "reports": "Raportin yhteenveto: {items}.", "report_model": "{lang}: {m}", "report_template": "{lang}: vain valmispohjan teksti (ei mallin yhteenvetoa)",
    },
    "sv": {
        "none": "Den här körningen har inga diagnoser, så ingen förklaring skrevs.",
        "all": "Alla {n_total} diagnoser har en förklaring skriven av en språkmodell ({models}).",
        "head": "{n_model} av {n_total} diagnoser har en förklaring skriven av en språkmodell{models_paren}; övriga {n_template} använder den evidensbaserade mallen, {why}; mallen anger samma fynd med sina evidens-id:n, och chatten förklarar vilken diagnos som helst på begäran.",
        "head_zero": "Ingen av de {n_total} diagnoserna har en förklaring skriven av en språkmodell; alla använder den evidensbaserade mallen, {why}; mallen anger samma fynd med sina evidens-id:n, och chatten förklarar vilken diagnos som helst på begäran.",
        "why_local": "avsiktligt: en lokal modell behöver cirka {sec} s per förklaring, så diagnossteget lägger högst halva sin tidsbudget ({allowance} s här) på de starkaste diagnoserna först",
        "why_local_nosec": "avsiktligt: diagnossteget låter den lokala modellen förklara de starkaste diagnoserna först och slutar när halva tidsbudgeten ({allowance} s här) är förbrukad",
        "why_external": "avsiktligt: den externa modellen förklarar högst {cap} diagnoser per körning (external_llm.max_narratives_per_run), de starkaste först",
        "why_no_llm": "eftersom körningen gjordes med språkmodellerna avstängda (--no-llm)",
        "why_no_model": "eftersom ingen språkmodell gick att nå när körningen gjordes (Ollama körs inte eller ingen modell är hämtad)",
        "why_generic": "avsiktligt: en modell förklarar de starkaste diagnoserna först så länge stegets modelltid räcker",
        "local": "lokal modell {m}", "external": "extern modell {m}",
        "critiques": "{n_model} av {n_total} kritiska granskningar skrevs med en språkmodell; övriga är mallens kodkontroller.",
        "reports": "Rapportsammanfattning: {items}.", "report_model": "{lang}: {m}", "report_template": "{lang}: endast malltext (ingen modellsammanfattning)",
    },
}


def _model_of(source: Any) -> Optional[tuple[str, str]]:
    """'llm-local:gemma4:e4b-it-qat+template' -> ('local', 'gemma4:e4b-it-qat'); template / human / code -> None."""
    m = _SOURCE_RE.search(str(source or ""))
    return (m.group(1), m.group(2).strip()) if m else None


def _tally(sources: list[Any]) -> dict[str, Any]:
    by: Counter = Counter()
    for s in sources:
        mm = _model_of(s)
        if mm:
            by[f"{mm[0]}:{mm[1]}"] += 1
    n_model = int(sum(by.values()))
    return {"total": len(sources), "model": n_model, "template": len(sources) - n_model, "by_model": dict(by)}


def _fmt_int(n: Any, lang: str) -> str:
    try:
        out = f"{int(n):,}"
    except Exception:
        return str(n)
    return out if lang == "en" else out.replace(",", " ")


_COV_CACHE: dict[str, tuple[Any, dict[str, Any]]] = {}


def _coverage_stamp(ws: Any) -> Any:
    parts = []
    try:
        for p in [ws.path("diagnoses"), ws.path("egress_ledger"), ws.path("meta"), *sorted(ws.dir.glob("report_*"))]:
            try:
                st = p.stat()
                parts.append((p.name, st.st_mtime_ns, st.st_size))
            except OSError:
                parts.append((getattr(p, "name", str(p)), None, None))
    except Exception:
        return None
    return tuple(parts)


def narrative_coverage(ws: Any, settings: Optional[Settings] = None) -> dict[str, Any]:
    """Who wrote the explanations of a run: diagnoses, critiques and report summaries written by a model (local or
    external, which model) vs by the evidence-based templates, and why the rest are templates:
      reason = none | all | no_llm | no_model | external_cap | local_budget | generic
    plus "sentence" (English, one plain sentence) and "details" (critiques, report summaries). coverage_sentence(cov,
    lang) renders the sentence in en / fi / sv. Reads diagnoses.jsonl, report_llm_<lang>.json, the ledger and the
    diagnose stage's note in the decision log; cached until one of those files changes. Never raises."""
    s = settings or getattr(ws, "settings", None)
    key = f"{getattr(ws, 'dir', '')}|{getattr(getattr(s, 'external_llm', None), 'max_narratives_per_run', '')}"
    stamp = _coverage_stamp(ws)
    hit = _COV_CACHE.get(key)
    if stamp is not None and hit and hit[0] == stamp:
        return json.loads(json.dumps(hit[1]))
    cov = _narrative_coverage(ws, settings)
    if stamp is not None:
        _COV_CACHE[key] = (stamp, cov)
    return json.loads(json.dumps(cov))


def _narrative_coverage(ws: Any, settings: Optional[Settings] = None) -> dict[str, Any]:
    settings = settings or getattr(ws, "settings", None)
    try:
        diags = [d for d in ws.read_jsonl("diagnoses") if isinstance(d, dict)]
    except Exception:
        diags = []
    diag = _tally([d.get("narrative_source") for d in diags])
    crit = _tally([(d.get("critique") or {}).get("source") for d in diags if isinstance(d.get("critique"), dict)])
    reports: dict[str, Optional[str]] = {}
    try:
        for p in sorted(ws.dir.glob("report_*.html")):
            reports[p.stem.split("_", 1)[1]] = None
        for p in sorted(ws.dir.glob("report_llm_*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            lang = p.stem.rsplit("_", 1)[-1]
            if isinstance(d, dict) and d.get("status") == "ok":
                reports[lang] = str(d.get("source") or "llm")
            else:
                reports.setdefault(lang, None)
    except Exception:
        pass
    rep = {"total": len(reports), "model": sum(1 for v in reports.values() if v), "by_language": {k: (v or "template") for k, v in sorted(reports.items())}}

    # why the rest are templates
    recs = [r for r in read(ws) if not is_demo(r)] if ws is not None else []
    narr = [r for r in recs if r.task in ("diagnosis_narrative", "critique")]
    opts = {}
    try:
        opts = (ws.read_json("meta", {}) or {}).get("options") or {}
    except Exception:
        pass
    note = ""
    try:
        notes = [e.payload.get("note", "") for e in ws.log.entries(action="note", object_id="diagnose", limit=100_000)]
        note = next((n for n in reversed(notes) if "LLM narrative" in n), "")
    except Exception:
        pass
    m_allow = re.search(r"of a (\d+(?:\.\d+)?)s allowance", note)
    m_time = re.search(r"LLM time (\d+(?:\.\d+)?)s", note)
    allowance = float(m_allow.group(1)) if m_allow else None
    llm_time = float(m_time.group(1)) if m_time else None
    routes = {k.split(":", 1)[0] for k in diag["by_model"]}
    cap = int(getattr(getattr(settings, "external_llm", None), "max_narratives_per_run", 12) or 12) if settings is not None else 12
    sec = None
    if llm_time and diag["model"]:
        sec = llm_time / diag["model"]
    else:
        lat = [r.latency_ms for r in narr if r.route == "local" and r.ok and r.latency_ms]
        if lat:
            sec = 2 * (sum(lat) / len(lat)) / 1000.0  # narrative + critique per diagnosis
    if not diags:
        reason = "none"
    elif diag["model"] == diag["total"]:
        reason = "all"
    elif diag["model"] == 0 and (opts.get("no_llm") or opts.get("use_llm") is False):
        reason = "no_llm"
    elif diag["model"] == 0 and narr and not any(r.ok for r in narr) and any(("reachable" in (r.error or "")) or ("pulled" in (r.error or "")) for r in narr):
        reason = "no_model"
    elif "external" in routes:
        reason = "external_cap"
    elif "local" in routes or allowance is not None:
        reason = "local_budget"
    else:
        reason = "generic"
    cov = {
        "diagnoses": diag, "critiques": crit, "report_summaries": rep, "reason": reason,
        "local_seconds_per_explanation": round(sec, 1) if sec else None, "model_time_allowance_s": allowance, "external_cap": cap,
        "stage_note": note or None,
    }
    cov["sentence"] = coverage_sentence(cov, "en")
    cov["details"] = coverage_details(cov, "en")
    return cov


def _models_text(by_model: dict[str, int], tx: dict[str, str]) -> str:
    out = []
    for key in sorted(by_model, key=lambda k: -by_model[k]):
        route, model = key.split(":", 1)
        out.append(tx.get(route, "{m}").format(m=model))
    return ", ".join(out)


def coverage_sentence(cov: dict[str, Any], lang: str = "en") -> str:
    """One plain sentence: how many diagnoses have a model-written explanation, and why the others use the template."""
    tx = _COVERAGE_TEXT.get(lang) or _COVERAGE_TEXT["en"]
    d = cov.get("diagnoses") or {}
    reason = cov.get("reason")
    n_total, n_model = int(d.get("total") or 0), int(d.get("model") or 0)
    models = _models_text(d.get("by_model") or {}, tx)
    if reason == "none" or not n_total:
        return tx["none"]
    if reason == "all":
        return tx["all"].format(n_total=_fmt_int(n_total, lang), models=models)
    sec, allowance = cov.get("local_seconds_per_explanation"), cov.get("model_time_allowance_s")
    if reason == "no_llm":
        why = tx["why_no_llm"]
    elif reason == "no_model":
        why = tx["why_no_model"]
    elif reason == "external_cap":
        why = tx["why_external"].format(cap=cov.get("external_cap") or 12)
    elif reason == "local_budget" and allowance is not None and sec:
        why = tx["why_local"].format(sec=f"{sec:.0f}", allowance=f"{allowance:.0f}")
    elif reason == "local_budget" and allowance is not None:
        why = tx["why_local_nosec"].format(allowance=f"{allowance:.0f}")
    else:
        why = tx["why_generic"]
    if n_model == 0:
        return tx["head_zero"].format(n_total=_fmt_int(n_total, lang), why=why)
    return tx["head"].format(n_model=_fmt_int(n_model, lang), n_total=_fmt_int(n_total, lang), n_template=_fmt_int(n_total - n_model, lang), models_paren=f" ({models})" if models else "", why=why)


def coverage_details(cov: dict[str, Any], lang: str = "en") -> list[str]:
    """Critiques and report summaries, one short sentence each."""
    tx = _COVERAGE_TEXT.get(lang) or _COVERAGE_TEXT["en"]
    out = []
    c = cov.get("critiques") or {}
    if c.get("total"):
        out.append(tx["critiques"].format(n_model=_fmt_int(c.get("model", 0), lang), n_total=_fmt_int(c["total"], lang)))
    r = (cov.get("report_summaries") or {}).get("by_language") or {}
    if r:
        items = []
        for lg, src in r.items():
            mm = _model_of(src)
            items.append(tx["report_model"].format(lang=lg, m=tx.get(mm[0], "{m}").format(m=mm[1])) if mm else tx["report_template"].format(lang=lg))
        out.append(tx["reports"].format(items="; ".join(items)))
    return out


# ----------------------------------------------------------------------------------------------
# guard demonstration, as the data-flow statement tells it
# ----------------------------------------------------------------------------------------------


def demo_statement(ws: Any, recs: Optional[list[EgressRecord]] = None) -> Optional[str]:
    """One or two sentences on the last `tpm guard-demo` of the run (from guard_demo.json, else from the ledger)."""
    demo = None
    try:
        demo = ws.read_json("guard_demo.json", None) if ws is not None else None
    except Exception:
        demo = None
    if isinstance(demo, dict) and demo.get("statement"):
        return str(demo["statement"])
    recs = [r for r in (recs if recs is not None else read(ws)) if is_demo(r)]
    if not recs:
        return None
    allowed = [r for r in recs if r.guard_result == "demo_allowed"]
    blocked = [r for r in recs if r.guard_result == "demo_blocked"]
    parts = []
    if allowed:
        parts.append(f"{len(allowed)} real payload(s) of this run were cleaned by the guard and would have been allowed")
    if blocked:
        parts.append(f"{len(blocked)} deliberately unsafe test payload(s) were blocked ({blocked[-1].guard_reason[:160]})")
    return "Guard demonstration (python -m tpm guard-demo): " + "; ".join(parts) + ". Nothing of the demonstration was sent."


def _statement_extras(ws: Any, settings: Settings, all_recs: list[EgressRecord], language: str) -> list[str]:
    out: list[str] = []
    try:
        cov = narrative_coverage(ws, settings)
        if (cov.get("diagnoses") or {}).get("total"):
            out += ["", "Who wrote the explanations: " + coverage_sentence(cov, "en")]
    except Exception:
        pass
    try:
        demo = demo_statement(ws, all_recs)
        if demo:
            out += ["", demo]
    except Exception:
        pass
    return out
