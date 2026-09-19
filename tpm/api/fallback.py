"""Template-first fallbacks used by the API when a stage module is missing or a model is unreachable.

Everything here is deterministic code over workspace artifacts: chat answers built from flag /
diagnosis / signal evidence, generic human-decision effects on artifacts, a batch replay loop, a folder
watcher, column alignment for incoming batches, and the egress ledger summary + data-flow statement.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import Settings
from ..contracts import HumanDecision, now_iso
from ..workspace import Workspace

CAUSE_TEXT = {
    "process": "several related signals moved together and kept their usual relations, which is what a real process change looks like rather than a single faulty instrument",
    "sensor": "one signal broke its usual relation to its partners while the partners stayed consistent with each other, which points at the instrument rather than the process",
    "data": "the change is a data artefact (scale, duplicates or gaps) rather than a physical event",
    "mixed": "both a process disturbance and a shared instrument problem remain plausible",
    "unknown": "the evidence does not clearly favour a process or a sensor explanation",
}

ACTION_TEXT = {
    "process": "check the process upstream of the leading signal first (feed, setpoints, utilities) before touching the instruments",
    "sensor": "inspect the instrument behind the leading signal (wiring, transmitter, freeze) before acting on the process",
    "data": "fix the data path (units, duplicates, gaps) and re-run; do not act on the process",
    "mixed": "compare with the operator log; if nothing changed in the process, check the sensor wiring",
    "unknown": "gather more context before acting",
}

LANG_INTRO = {
    "en": "",
    "fi": "(Vastaus on koottu mallipohjasta englanniksi; kielimallia ei ole käytettävissä.) ",
    "sv": "(Svaret är sammanställt från en mall på engelska; ingen språkmodell tillgänglig.) ",
}


# ------------------------------------------------------------------------------------------ chat
def normalize_chat_result(res: Any) -> dict[str, Any]:
    """Accept LLMResult | dict | str from tpm.llm.agent.chat / tpm.assessor.ask and normalise."""
    if res is None:
        return {"text": "", "source": "template", "route": "none", "evidence_ids": []}
    if isinstance(res, str):
        return {"text": res, "source": "llm", "route": "local", "evidence_ids": _find_ids(res)}
    if hasattr(res, "model_dump"):
        d = res.model_dump()
    elif isinstance(res, dict):
        d = dict(res)
    else:
        return {"text": str(res), "source": "llm", "route": "local", "evidence_ids": []}
    text = d.get("text") or d.get("answer") or d.get("message") or ""
    data = d.get("data") if isinstance(d.get("data"), dict) else {}
    ev = d.get("evidence_ids") or d.get("citations") or data.get("evidence_ids") or _find_ids(text)
    source = d.get("source") or "llm"
    route = d.get("route") or ("none" if source == "template" else "external" if source.startswith("llm-external") else "local")
    model = d.get("model") or (source.split(":", 1)[1] if ":" in source else "")
    series = d.get("series") if isinstance(d.get("series"), dict) else None
    return {"text": text, "source": source, "route": route, "model": model, "evidence_ids": list(ev), "ledger_id": d.get("ledger_id"), "data": data or None, "followups": list(d.get("suggested_followups") or []), "confidence": d.get("confidence"),
            # which workspace objects the answer looked at (shown under "Show technical analyses"); names and ids only, no contents
            "tool_trace": [{k: st.get(k) for k in ("step", "tool", "args", "ok", "ms", "thought") if st.get(k) is not None} for st in (d.get("tool_trace") or []) if isinstance(st, dict)][:24],
            # the turn the agent persisted (the UI names it in advance so its Stop button can flag it) and its chat
            "turn_id": d.get("turn_id"), "chat_id": d.get("chat_id"), "stopped": bool(d.get("stopped")),
            # the downsampled series the agent looked at (local only; drawn as a small chart under the answer)
            "series": {"signal": series.get("signal"), "columns": series.get("columns"), "points": list(series.get("points") or [])[:400], "row_start": series.get("row_start"), "row_end": series.get("row_end")} if series and series.get("points") else None}


def _find_ids(text: str) -> list[str]:
    return sorted(set(re.findall(r"\b(?:EV|INF|CHK|FLAG|DIAG)-\d{6}\b", text or "")))


# ------------------------------------------------------------------------------- chat ids + stop
# The drawer keeps several chats per run (chat_id) and names a turn before it starts (client_turn_id) so its Stop
# button can flag the turn while the model works. The flag registry lives in tpm.llm.agent; these helpers keep the
# API usable when that module is missing.
DEFAULT_CHAT_ID = "default"
_TURN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")


def _agent_mod() -> Any:
    try:
        from ..llm import agent
        return agent
    except Exception:
        return None


def clean_turn_id(turn_id: Any) -> Optional[str]:
    s = str(turn_id or "").strip()
    return s if s and _TURN_ID_RE.match(s) else None


def clean_chat_id(chat_id: Any) -> str:
    s = str(chat_id or "").strip()
    return s if s and _TURN_ID_RE.match(s) else DEFAULT_CHAT_ID


def valid_chat_id(chat_id: Any) -> Optional[str]:
    """The chat id when it is a safe id, else None. Clear / Delete / Stop refuse an invalid id instead of falling back
    to "default" (which would silently clear the first chat)."""
    s = str(chat_id or "").strip()
    return s if s and _TURN_ID_RE.match(s) else None


def turn_chat_id(turn: dict[str, Any]) -> str:
    return str(turn.get("chat_id") or DEFAULT_CHAT_ID)


def request_stop(turn_id: Optional[str], chat_id: Optional[str], run_id: Optional[str] = None) -> list[str]:
    a = _agent_mod()
    return list(a.request_stop(turn_id, chat_id, run_id)) if a is not None else []


def discard_running(chat_id: str, run_id: Optional[str] = None) -> list[str]:
    """Stop the running turns of a chat that is being cleared or deleted, and save nothing more of them."""
    a = _agent_mod()
    return list(a.discard_running(chat_id, run_id)) if a is not None else []


def is_stopped(turn_id: Optional[str]) -> bool:
    a = _agent_mod()
    return bool(a.is_stopped(turn_id)) if a is not None else False


def clear_chat(ws: Workspace, chat_id: str) -> int:
    """Remove the persisted turns of one chat (under the workspace lock: a turn of another chat saved meanwhile stays).
    Returns how many were removed."""
    if not ws.exists("chat"):
        return 0
    return ws.filter_jsonl("chat", lambda t: turn_chat_id(t) != chat_id)


def _pct(x: Any) -> str:
    try:
        return f"{float(x) * 100:.0f} %"
    except Exception:
        return "?"


def _find(items: list[dict[str, Any]], key: str, value: Any) -> Optional[dict[str, Any]]:
    for it in items:
        if it.get(key) == value:
            return it
    return None


def _evidence_lines(ws: Workspace, ids: list[str], n: int = 4) -> list[str]:
    out = []
    for i in ids[:n]:
        e = ws.evidence.get(i)
        if e is not None:
            out.append(f"{e.id}: {e.statement}")
    return out


def template_chat_answer(ws: Workspace, settings: Settings, message: str, context: dict[str, Any], language: str = "en") -> dict[str, Any]:
    """Deterministic answer built from the artifacts behind the object in `context`."""
    m = (message or "").lower()
    flag_id = context.get("flag_id") or (context.get("object_id") if context.get("object_type") == "flag" else None)
    diag_id = context.get("diagnosis_id") or (context.get("object_id") if context.get("object_type") == "diagnosis" else None)
    signal_id = context.get("signal_id") or (context.get("object_id") if context.get("object_type") == "signal" else None)
    flags = ws.read_jsonl("flags") if ws.exists("flags") else []
    diags = ws.read_jsonl("diagnoses") if ws.exists("diagnoses") else []
    signals = ws.read_json("signals", []) or []
    intro = LANG_INTRO.get(language, "")
    ev_ids: list[str] = []
    parts: list[str] = []

    flag = _find(flags, "id", flag_id) if flag_id else None
    diag = _find(diags, "id", diag_id) if diag_id else None
    if flag and not diag:
        diag = next((d for d in diags if flag["id"] in (d.get("flag_ids") or [])), None)
    if diag and not flag and diag.get("flag_ids"):
        flag = _find(flags, "id", diag["flag_ids"][0])
    sig = _find(signals, "id", signal_id) if signal_id else None

    asks_why = any(k in m for k in ("why", "miksi", "varför", "how do you know", "evidence", "reason"))
    asks_which = any(k in m for k in ("which sensor", "which signal", "what signal", "where", "mikä", "vilken", "missä", "var "))
    asks_broken = any(k in m for k in ("broken", "faulty", "sensor fault", "rikki", "trasig", "instrument", "is it a sensor", "sensor or process", "process or sensor"))
    asks_action = any(k in m for k in ("what should", "what do i", "what to do", "action", "recommend", "mitä teen", "vad ska", "next step"))
    asks_conf = any(k in m for k in ("confidence", "sure", "certain", "uncertain", "luotettav", "säker", "how confident"))
    asks_trust = any(k in m for k in ("trust", "data quality", "reliable", "luotta", "lita"))
    asks_when = any(k in m for k in ("when", "start", "onset", "milloin", "när"))

    if flag:
        ranked = flag.get("signals_ranked") or []
        lead = ranked[0]["signal"] if ranked else (flag.get("signals_ranked") or [{}])[0].get("signal", "?")
        cause = flag.get("likely_cause_class", "unknown")
        ev_ids = list(flag.get("evidence_ids") or [])
        if asks_which:
            parts.append(f"The score of {flag['id']} is driven by " + ", ".join(f"{r['signal']} ({_pct(r.get('contribution'))}, {r.get('direction') or 'moved'})" for r in ranked[:4]) + f". {lead} leads.")
            if flag.get("group_id") is not None:
                parts.append(f"Location: group {flag['group_id']}, rows {flag.get('row_start')} to {flag.get('row_end')}" + (f", batch {flag.get('batch_id')}" if flag.get("batch_id") else "") + ".")
        elif asks_broken:
            parts.append(f"Most likely cause class for {flag['id']}: {cause}. In plain terms, {CAUSE_TEXT.get(cause, CAUSE_TEXT['unknown'])}.")
            if cause == "sensor":
                parts.append(f"So yes: {lead} behaves like a faulty or frozen instrument rather than a real process change.")
            elif cause == "process":
                parts.append(f"So no broken sensor is indicated: {lead} and its partners moved together.")
            else:
                parts.append("A broken sensor cannot be excluded; see the objections in the critique.")
        elif asks_action:
            parts.append(f"Suggested next step: {ACTION_TEXT.get(cause, ACTION_TEXT['unknown'])}. Leading signal: {lead}.")
            if diag and diag.get("steps"):
                parts.append("From the diagnosis: " + diag["steps"][-1])
        elif asks_conf:
            parts.append(f"Confidence in {flag['id']} is {_pct(flag.get('confidence'))} (severity {_pct(flag.get('severity'))}, score {flag.get('score')} against threshold {flag.get('threshold')}).")
            if diag:
                parts.append(f"The diagnosis {diag['id']} has confidence {_pct(diag.get('confidence'))}" + (f" after the critique ({diag['critique'].get('verdict')}: {'; '.join(diag['critique'].get('objections') or ['no objections'])})." if diag.get("critique") else "."))
                if diag.get("uncertainty"):
                    parts.append("What remains uncertain: " + "; ".join(diag["uncertainty"]) + ".")
        elif asks_trust:
            tc = flag.get("trust_context") or {}
            if tc:
                parts.append(f"Batch {tc.get('batch_id')} trust score is {tc.get('trust_score')} ({'trusted' if tc.get('trusted') else 'NOT trusted'})" + (f"; untrusted signals: {', '.join(tc.get('untrusted_signals') or [])}." if tc.get("untrusted_signals") else "."))
            else:
                parts.append("No trust verdict is attached to this flag.")
        elif asks_when:
            parts.append(f"{flag['id']} starts at row {flag.get('row_start')}" + (f" ({flag.get('time_start')})" if flag.get("time_start") else "") + f" and lasts until row {flag.get('row_end')}" + (f" ({flag.get('time_end')})" if flag.get("time_end") else "") + ".")
        else:  # why / default
            parts.append(flag.get("statement", ""))
            parts.append(f"Detector: {flag.get('detector')}. Score {flag.get('score')} vs threshold {flag.get('threshold')}; confidence {_pct(flag.get('confidence'))}; likely cause: {cause}.")
            lines = _evidence_lines(ws, ev_ids)
            if lines:
                parts.append("Evidence: " + " | ".join(lines))
        if diag and not asks_conf and not asks_action:
            parts.append(f"Diagnosis {diag['id']}: {diag.get('fault_type')} ({diag.get('cause_class')}), confidence {_pct(diag.get('confidence'))}.")
    elif diag:
        ev_ids = list(diag.get("evidence_ids") or [])
        if asks_action:
            parts.append(diag["steps"][-1] if diag.get("steps") else ACTION_TEXT.get(diag.get("cause_class", "unknown")))
        elif asks_conf:
            parts.append(f"Confidence {_pct(diag.get('confidence'))}. Uncertainty: " + "; ".join(diag.get("uncertainty") or ["none recorded"]) + ".")
        else:
            parts.append(diag.get("summary", ""))
            parts.extend(f"{i + 1}. {s}" for i, s in enumerate(diag.get("steps") or []))
    elif sig:
        ev_ids = list(sig.get("evidence_ids") or [])
        fp = sig.get("fingerprint") or {}
        parts.append(f"{sig['id']} is read as a {str(sig.get('structural_role')).replace('_', ' ')} signal (confidence {_pct(sig.get('structural_confidence'))})" + (f", possibly a {sig.get('instrument_hypothesis')} measurement (confidence {_pct(sig.get('instrument_confidence'))}; this is a hypothesis)" if sig.get("instrument_hypothesis") else "") + ".")
        if fp:
            parts.append(f"Fingerprint: mean {fp.get('mean')}, std {fp.get('std')}, range [{fp.get('min')}, {fp.get('max')}], missing {_pct(fp.get('missing_fraction', 0))}, stuck fraction {_pct(fp.get('stuck_fraction', 0))}.")
        rel = sig.get("related_signals") or []
        if rel:
            parts.append("Related: " + ", ".join(f"{r['signal']} (r={r.get('r')}, lag {r.get('lag')})" for r in rel[:4]) + ".")
        lines = _evidence_lines(ws, ev_ids)
        if lines:
            parts.append("Evidence: " + " | ".join(lines))
    else:
        sch = ws.read_json("schema") or {}
        trust = ws.read_jsonl("trust") if ws.exists("trust") else []
        untrusted = [t["batch_id"] for t in trust if not t.get("trusted", True)]
        parts.append(f"This run has {sch.get('n_rows', '?')} rows, {len(sch.get('signal_columns', []))} signals and {sch.get('n_groups', 1)} groups; {len(flags)} flags and {len(diags)} diagnoses.")
        if untrusted:
            parts.append(f"Batches that cannot be trusted: {', '.join(untrusted)}.")
        top = sorted(flags, key=lambda f: -float(f.get("severity", 0)))[:3]
        if top:
            parts.append("Most severe: " + " | ".join(f"{f['id']} ({f.get('kind')}, {_pct(f.get('severity'))}): {f.get('statement', '')[:120]}" for f in top))
        parts.append("Open a flag or a diagnosis to ask about it specifically.")
    text = intro + " ".join(p for p in parts if p)
    return {"text": text, "source": "template", "route": "none", "model": "", "evidence_ids": ev_ids[:8]}


def template_assessor_answer(ws: Workspace, question: str) -> dict[str, Any]:
    a = ws.read_json("assessor") or {}
    q = (question or "").lower()
    parts: list[str] = []
    ev: list[str] = []
    wmd = a.get("would_more_data_help") or {}
    if any(k in q for k in ("more data", "adding", "lisää dataa", "mer data", "help")) and wmd:
        parts.append(f"Would more data help? {wmd.get('answer', 'unknown')}. Expected gain {wmd.get('expected_gain')}, confidence {_pct(wmd.get('confidence'))}. {wmd.get('explanation', '')}")
        ev = list(wmd.get("evidence_ids") or [])
    elif any(k in q for k in ("coverage", "regime", "kattavuus", "täckning")):
        cov = a.get("coverage_by_regime") or []
        parts.append("Coverage by regime: " + "; ".join(f"{c.get('regime')}: {_pct(c.get('coverage'))} ({c.get('n_groups')} groups)" for c in cov) + ".")
    elif any(k in q for k in ("quality", "dq", "laatu", "kvalitet")):
        dq = a.get("dq_scores") or {}
        parts.append("Data-quality scores: " + ", ".join(f"{k} {_pct(v)}" for k, v in dq.items()) + ".")
    elif any(k in q for k in ("recommend", "suositus", "rekommend", "what should")):
        recs = a.get("recommendations") or []
        parts.append("Recommendations: " + " | ".join(f"{r.get('id')}: {r.get('action')} (gain {r.get('expected_gain')}, confidence {_pct(r.get('confidence'))})" for r in recs) + ".")
    else:
        parts.append(f"Assessor score {a.get('score')}: {a.get('verdict', '')}")
        comps = a.get("components") or {}
        if comps:
            parts.append("Components: " + ", ".join(f"{k.replace('_', ' ')} {_pct(v)}" for k, v in comps.items()) + ".")
        ev = list(a.get("evidence_ids") or [])
    if not a:
        parts = ["The assessor has not produced results for this run yet."]
    return {"text": " ".join(parts), "source": "template", "route": "none", "model": "", "evidence_ids": ev}


# ------------------------------------------------------------------------------------ decisions
def apply_generic_effect(ws: Workspace, settings: Settings, decision: HumanDecision) -> dict[str, Any]:
    """When the owning module has no apply_override yet, keep the artifacts consistent with the
    decision so the UI reflects it: human_status on flags/diagnoses, pattern names, role overrides,
    rule status, assessor recommendation status."""
    status_map = {"accept": "accepted", "question": "questioned", "override": "overridden", "dismiss": "dismissed"}
    ot, oid, act = decision.object_type, decision.object_id, decision.action
    out: dict[str, Any] = {"applied": False}
    try:
        if ot == "flag" and ws.exists("flags"):
            items = ws.read_jsonl("flags")
            for f in items:
                if f.get("id") == oid:
                    f["human_status"] = status_map.get(act, f.get("human_status"))
                    f["human_note"] = decision.note
                    if act == "override" and decision.new_value:
                        for k in ("likely_cause_class", "kind", "severity", "pattern_id"):
                            if k in decision.new_value:
                                f[k] = decision.new_value[k]
                    out = {"applied": True, "human_status": f["human_status"]}
            ws.rewrite_jsonl("flags", items)
        elif ot == "diagnosis" and ws.exists("diagnoses"):
            items = ws.read_jsonl("diagnoses")
            for d in items:
                if d.get("id") == oid:
                    d["human_status"] = status_map.get(act, d.get("human_status"))
                    d["human_note"] = decision.note
                    if act == "override" and decision.new_value:
                        for k in ("cause_class", "fault_type", "confidence"):
                            if k in decision.new_value:
                                d[k] = decision.new_value[k]
                    out = {"applied": True, "human_status": d["human_status"]}
            ws.rewrite_jsonl("diagnoses", items)
        elif ot == "pattern":
            items = ws.read_json("patterns", []) or []
            for p in items:
                if p.get("id") == oid:
                    if act == "name_pattern" and decision.new_value and decision.new_value.get("name"):
                        p["name"] = decision.new_value["name"]
                    if decision.note:
                        p["human_note"] = decision.note
                    p["human_status"] = status_map.get(act, p.get("human_status"))
                    out = {"applied": True, "name": p.get("name")}
            ws.write_json("patterns", items)
        elif ot == "signal":
            items = ws.read_json("signals", []) or []
            for s in items:
                if s.get("id") == oid:
                    if act in ("set_role", "override") and decision.new_value and decision.new_value.get("role"):
                        s["human_role_override"] = decision.new_value["role"]
                    s["human_status"] = status_map.get(act, s.get("human_status"))
                    s["human_note"] = decision.note
                    out = {"applied": True, "human_role_override": s.get("human_role_override")}
            ws.write_json("signals", items)
        elif ot == "rule":
            items = ws.read_json("rules", []) or []
            for r in items:
                if r.get("id") == oid:
                    if act in ("approve_rule", "accept"):
                        r["status"] = "active" if settings.rules.auto_activate_on_approve else "approved"
                    elif act in ("reject_rule", "dismiss"):
                        r["status"] = "rejected"
                    elif act == "override" and decision.new_value and decision.new_value.get("compiled"):
                        r["compiled"] = decision.new_value["compiled"]
                        r["compile_source"] = "human"
                    r["updated_at"] = now_iso()
                    out = {"applied": True, "status": r["status"]}
            ws.write_json("rules", items)
        elif ot == "assessor":
            a = ws.read_json("assessor") or {}
            for r in a.get("recommendations") or []:
                if r.get("id") == oid:
                    r["status"] = {"apply_assessor_action": "approved", "accept": "approved", "dismiss": "dismissed", "question": "questioned"}.get(act, r.get("status"))
                    r["human_note"] = decision.note
                    out = {"applied": True, "status": r["status"]}
            if a:
                ws.write_json("assessor", a)
        elif ot == "schema":
            sch = ws.read_json("schema") or {}
            if sch and decision.new_value:
                sch.setdefault("human_overrides", {}).update(decision.new_value)
                ws.write_json("schema", sch)
                out = {"applied": True}
    except Exception as e:
        out = {"applied": False, "error": str(e)}
    return out


# ------------------------------------------------------------------------------------ streaming
def align_incoming(ws: Workspace, df: Any) -> Any:
    """Map incoming columns onto the run's schema: original names -> aliases, drop unknown columns,
    add missing signal columns as NaN, coerce to numeric where possible."""
    import numpy as np
    import pandas as pd

    sch = ws.read_json("schema") or {}
    alias = sch.get("signal_alias", {}) or {}
    known = set(sch.get("columns") or [])
    rename = {c: alias[c] for c in df.columns if c in alias}
    if rename:
        df = df.rename(columns=rename)
    if alias and not any(c in df.columns for c in set(alias.values()) | set(alias.keys())):
        # header-less numeric frame: assign by position over the signal columns
        sigs = [alias[c] for c in sch.get("signal_columns", []) if c in alias]
        num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        if len(num_cols) >= len(sigs) and sigs:
            df = df.rename(columns=dict(zip(num_cols[-len(sigs):], sigs)))
    keep = [c for c in df.columns if c in known or c in alias.values()]
    if keep:
        df = df[keep]
    for a in alias.values():
        if a not in df.columns:
            df[a] = np.nan
        else:
            df[a] = pd.to_numeric(df[a], errors="coerce")
    return df


def replay(ws: Workspace, settings: Settings, ctl: dict[str, Any], on_batch: Callable[[Any, str], Any]) -> None:
    """Replay dataset.parquet batch by batch (batches.json when present, else windows of
    settings.batch.fallback_fraction rows), pacing 1/speed seconds between batches."""
    con = ws.duckdb()
    cols = [r[0] for r in con.execute("DESCRIBE dataset").fetchall()]
    rexpr = '"__row__"' if "__row__" in cols else "(row_number() OVER () - 1)"
    n = con.execute("SELECT COUNT(*) FROM dataset").fetchone()[0]
    b = ws.read_json("batches") or {}
    batches = b.get("batches") if isinstance(b, dict) else b
    if isinstance(b, list):  # tpm.ingest.stream format: row_end exclusive
        batches = [dict(x, row_end=int(x["row_end"]) - 1) for x in batches]
    if not batches:
        step = max(settings.batch.min_rows, int(n * settings.batch.fallback_fraction))
        batches = [{"batch_id": f"R{i + 1:04d}", "row_start": s, "row_end": min(n - 1, s + step - 1)} for i, s in enumerate(range(0, n, step))]
    delay = 1.0 / max(0.01, float(ctl.get("speed") or 1.0))
    for i, bt in enumerate(batches[: int(ctl.get("max_batches") or len(batches))]):
        if ctl.get("stop"):
            ctl["state"] = "stopped"
            return
        df = con.execute(f"SELECT * FROM (SELECT *, {rexpr} AS __r FROM dataset) WHERE __r BETWEEN ? AND ?", [bt["row_start"], bt["row_end"]]).df()
        df = df.drop(columns=["__r"], errors="ignore")
        bid = f"REPLAY-{bt['batch_id']}"
        try:
            on_batch(df, bid)
        except Exception as e:
            ctl.setdefault("errors", []).append(f"{bid}: {e}")
        ctl["done"] = i + 1
        ctl["last_batch"] = bid
        time.sleep(delay)


def watch_folder(ws: Workspace, ctl: dict[str, Any], on_batch: Callable[[Any, str], Any]) -> None:
    import pandas as pd

    folder = Path(ctl["folder"])
    seen: set[str] = set(ctl.get("seen") or [])
    for p in folder.iterdir():  # existing files are not replayed
        if p.is_file():
            seen.add(p.name)
    ctl["seen"] = sorted(seen)
    while not ctl.get("stop"):
        for p in sorted(folder.iterdir()):
            if not p.is_file() or p.name in seen or p.suffix.lower() not in (".csv", ".tsv", ".parquet", ".jsonl", ".json"):
                continue
            try:
                time.sleep(0.2)  # let the writer finish
                if p.suffix.lower() == ".parquet":
                    df = pd.read_parquet(p)
                elif p.suffix.lower() in (".jsonl", ".json"):
                    df = pd.read_json(p, lines=p.suffix.lower() == ".jsonl")
                else:
                    df = pd.read_csv(p, sep=None, engine="python")
                on_batch(df, f"WATCH-{p.stem}")
                ctl["last_file"] = p.name
                ctl["n_files"] = ctl.get("n_files", 0) + 1
            except Exception as e:
                ctl.setdefault("errors", []).append(f"{p.name}: {e}")
            seen.add(p.name)
            ctl["seen"] = sorted(seen)[-50:]
        time.sleep(float(ctl.get("poll_s") or 2.0))


# --------------------------------------------------------------------------------------- egress
def ledger_summary(ledger: list[dict[str, Any]]) -> dict[str, Any]:
    s: dict[str, Any] = {"n": len(ledger), "local": 0, "external": 0, "external_allowed": 0, "external_blocked": 0, "fallback": 0, "bytes_external": 0, "demo": 0, "by_task": {}, "models": sorted({r.get("model", "") for r in ledger if r.get("model")}), "last_ts": ledger[-1].get("ts") if ledger else None}
    for r in ledger:
        route = r.get("route")
        if str(r.get("guard_result") or "") in ("demo_allowed", "demo_blocked"):  # guard demonstration: shown, never sent
            s["demo"] += 1
            continue
        if route == "external":
            s["external"] += 1
            if r.get("guard_result") == "allowed" and r.get("ok", True):
                s["external_allowed"] += 1
                s["bytes_external"] += int(r.get("payload_bytes") or 0)
            else:
                s["external_blocked"] += 1
            if r.get("guard_result") == "fallback":
                s["fallback"] += 1
        else:
            s["local"] += 1
        t = s["by_task"].setdefault(r.get("task", "?"), {"local": 0, "external": 0})
        t["external" if route == "external" else "local"] += 1
    return s


def data_flow_statement(settings: Settings, summary: dict[str, Any]) -> str:
    prof = settings.active_profile
    name = settings.profile
    local = settings.local_llm.model
    ext = f"{settings.external_llm.provider} {settings.external_llm.model}"
    key = bool(settings.external_llm.api_key)
    if not prof.allow_external:
        return (f"Profile '{name}': nothing leaves this machine. Raw rows, statistics and every model prompt stay local; "
                f"the only model in use is the local one ({local}, Ollama at {settings.local_llm.base_url}). "
                f"External calls recorded in this run: {summary.get('external_allowed', 0)} sent, {summary.get('external_blocked', 0)} blocked by the guard.")
    guard = "strict" if prof.guard_strict else "standard"
    tasks = ", ".join(sorted(k for k, v in prof.routing.items() if v == "external")) or "none"
    where = f" at {settings.external_llm.base_url}" if settings.external_llm.base_url else ""
    return (f"Profile '{name}': raw rows never leave this machine. Tasks that see raw data ({', '.join(sorted(k for k, v in prof.routing.items() if v == 'local')) or 'none'}) run on the local model ({local}). "
            f"Derived artifacts only (aggregates over at least {settings.guard.min_aggregate_n} samples, no series longer than {settings.guard.max_series_points} points, no categorical values, no row-like structures) "
            f"may be sent to {ext}{where} for: {tasks}, after passing the {guard} egress guard. "
            f"{'An API key is configured, so an external route exists.' if key else 'No API key is configured, so no external route exists right now and every call falls back to the local model.'} "
            f"External calls recorded in this run: {summary.get('external_allowed', 0)} sent ({summary.get('bytes_external', 0)} bytes), {summary.get('external_blocked', 0)} blocked.")
