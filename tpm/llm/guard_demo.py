"""`python -m tpm guard-demo --run <run> [--profile hybrid|eu-hosted] [--send]`: prove the egress guard on a real run
instead of describing it.

    result = run_demo(ws, settings, profile="eu-hosted", send=False)   # writes guard_demo.json + ledger records
    print("\n".join(format_demo(result)))                              # plain words, English
    ctx = report_context(ws, "fi")                                     # the report's data-flow block, en / fi / sv

1. SAFE: a real derived payload of the run -- the narrative payload the diagnose stage builds for the strongest
   diagnosis (the signal catalog when the run has no diagnoses) -- goes through guard.check exactly as router.complete
   would send it. The result says what was aliased, rounded, replaced and removed, and whether it could leave.
2. QUESTION: an operator question that names original columns, the source file and a label column goes through the
   same guard: the before / after shows the headers replaced by aliases.
3. UNSAFE: a deliberately unsafe payload is built from the run's real rows: raw records under the original column
   headers, full-precision readings, data time stamps, label values and the source file name, as records, as a
   numeric series and written out as text. The guard must block it or strip it to nothing; the result gives the
   reason layer by layer, and what the last check (the invariant) alone would have found. It is NEVER sent.
All three are written to the run's egress ledger (guard_result "demo_allowed" / "demo_blocked", purpose "guard
demonstration: ..."): they are shown, not sent, and no count of real model calls includes them. With send=True (off by
default) the SAFE payload is afterwards sent once through the normal router (guard, budget, ledger), for the lead to
run once with the real key. guard_demo.json in the run directory holds the result for the report and the Data-flow page.
"""
from __future__ import annotations

import json
import math
import random
import re
import time
from pathlib import Path
from typing import Any, Optional

from ..config import Settings
from ..contracts import now_iso
from . import guard as guard_mod
from . import ledger as ledger_mod

DEMO_FILE = "guard_demo.json"
PURPOSE = "guard demonstration"
UNSAFE_TASK = "guard_demo"
N_ROWS = 3  # raw records in the unsafe payload
N_SERIES = 50  # readings per signal in its numeric-series part
_EXAMPLES = 4


# ----------------------------------------------------------------------------------------------
# inputs: a real payload, an operator question, raw rows
# ----------------------------------------------------------------------------------------------


def _top_diagnosis(ws: Any) -> Optional[Any]:
    """The strongest event diagnosis (the diagnose stage writes the strongest first); the aggregate of isolated
    readings only when there is nothing else. Reads diagnoses.jsonl line by line and stops at the first hit."""
    from ..contracts import Diagnosis

    p = ws.path("diagnoses")
    if not p.exists():
        return None
    first = None
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                d = Diagnosis(**json.loads(line))
            except Exception:
                continue
            if d.fault_type != "isolated suspicious readings":
                return d
            first = first or d
    return first


def real_payload(ws: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """(task, payload, about) of a real derived payload of the run, built by the stages' own builders."""
    d = _top_diagnosis(ws)
    if d is not None:
        from ..diagnose.diagnosis import narrative_payload

        return "diagnosis_narrative", narrative_payload(ws, d), {"kind": "diagnosis", "id": d.id, "flag_ids": list(d.flag_ids[:3])}
    schema = ws.schema()
    descriptors = ws.signals()
    if schema is not None and descriptors:
        from ..profile.roles import build_llm_payload

        return "sensor_hypotheses", build_llm_payload(descriptors, ws.read_json("relations", {}) or {}, dict(schema.domain_likelihood or {}), None), {"kind": "catalog", "id": "signal catalog", "flag_ids": []}
    raise ValueError("the run has neither diagnoses nor a signal catalog yet: run the pipeline first")


def _schema(ws: Any) -> dict[str, Any]:
    try:
        return ws.read_json("schema", {}) or {}
    except Exception:
        return {}


def _headers(sch: dict[str, Any]) -> list[str]:
    """The original column headers of the file (nothing when the file had no header row)."""
    if not sch.get("had_header", True):
        return []
    return [c for c in (sch.get("columns") or []) if isinstance(c, str) and not c.startswith("__")]


def _file_name(ws: Any, sch: dict[str, Any]) -> tuple[str, str]:
    src = str(sch.get("source_path") or (ws.read_json("meta", {}) or {}).get("source_path") or "")
    return src, (Path(src).name if src else "")


def operator_question(ws: Any, about: dict[str, Any]) -> Optional[str]:
    """A question an operator could type, naming two original signal headers, a label column and the source file."""
    sch = _schema(ws)
    headers = _headers(sch)
    if not headers:
        return None
    sig = [c for c in (sch.get("signal_columns") or []) if c in headers] or headers
    names = sig[:1] + sig[-1:] if len(sig) > 1 else sig[:1]
    label = next(iter(sch.get("label_columns") or []), None)
    _, fname = _file_name(ws, sch)
    where = about.get("id") if about.get("kind") == "diagnosis" else "the last run"
    q = f"Why did {names[0]} rise before {names[-1]} in {where}"
    if label:
        q += f" while {label} was set"
    q += "?"
    if fname:
        q += f" The data is from {fname}."
    return q


def _fetch_rows(ws: Any, row_start: Optional[int], n: int) -> tuple[list[dict[str, Any]], bool]:
    """(rows, real): n consecutive rows of dataset.parquet from row_start (the rows of the demonstrated diagnosis), with
    their original headers; ([], False) when the file cannot be read."""
    if not ws.exists("dataset"):
        return [], False
    try:
        con = ws.duckdb()
        try:
            con = con.cursor()
        except Exception:
            pass
        cols = [r[0] for r in con.execute("DESCRIBE dataset").fetchall()]
        start = max(0, int(row_start or 0))
        if "__row__" in cols:  # a range on the row number: the parquet statistics skip every other row group
            q = f"SELECT * FROM dataset WHERE __row__ BETWEEN {start} AND {start + int(n) - 1}"
        else:
            q = f"SELECT * FROM dataset LIMIT {int(n)} OFFSET {start}"
        cur = con.execute(q)
        names = [d[0] for d in cur.description]
        rows = sorted((dict(zip(names, r)) for r in cur.fetchall()), key=lambda r: r.get("__row__") or 0)
        if not rows:
            rows = [dict(zip(names, r)) for r in con.execute(f"SELECT * FROM dataset LIMIT {int(n)}").fetchall()]
        return rows, bool(rows)
    except Exception:
        return [], False


def _plain_value(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:
            return str(v)
    return v


def unsafe_payload(ws: Any, row_start: Optional[int] = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(payload, what) of a deliberately unsafe payload built from the run's real rows. `what` describes it in counts
    only; the readings themselves are never copied into guard_demo.json, the ledger or the report."""
    sch = _schema(ws)
    headers = _headers(sch)
    signal_cols = [c for c in (sch.get("signal_columns") or []) if isinstance(c, str)]
    label_cols = [c for c in (sch.get("label_columns") or []) if isinstance(c, str)]
    time_col = sch.get("time_column")
    source_path, fname = _file_name(ws, sch)
    rows, real = _fetch_rows(ws, row_start, N_SERIES)
    if not rows:  # no dataset.parquet in this copy: made-up readings under the real headers
        rng = random.Random(7)
        cols = headers or [f"col_{i + 1}" for i in range(12)]
        rows = [{c: rng.uniform(0.0, 5000.0) for c in cols} for _ in range(N_SERIES)]
    records = [{k: _plain_value(v) for k, v in r.items() if not str(k).startswith("__")} for r in rows[:N_ROWS]]
    first_row = rows[0].get("__row__", row_start or 0)
    if time_col and all(time_col in r and r[time_col] is not None for r in rows[:N_ROWS]):
        stamps = [str(_plain_value(r[time_col])) for r in rows[:N_ROWS]]
        synthetic_stamps = False
    else:  # the dataset has no time column: data-like stamps built from the row numbers
        stamps = [f"2026-01-03T{(int(first_row) + i) // 3600 % 24:02d}:{(int(first_row) + i) // 60 % 60:02d}:{(int(first_row) + i) % 60:02d}" for i in range(N_ROWS)]
        synthetic_stamps = True
    numeric_cols = [c for c in (records[0] if records else {}) if isinstance(records[0].get(c), float)]
    labels = {c: [r.get(c) for r in records] for c in label_cols if records and c in records[0]}
    as_text = []
    for i, rec in enumerate(records):
        pairs = ", ".join(f"{c}={rec[c]!r}" for c in numeric_cols[:24])
        text = f"row {int(first_row) + i}: {pairs}; logged at {stamps[i]} in {fname or 'the source file'}"
        lab = ", ".join(f"{c}={rec.get(c)!r}" for c in label_cols[:2] if c in rec)
        if lab:
            text += f"; labels {lab}"
        as_text.append(text)
    series = [{"source_column": c, "values": [_plain_value(r.get(c)) for r in rows if isinstance(_plain_value(r.get(c)), float)]} for c in signal_cols[:3] if rows and c in rows[0]]
    payload = {
        "purpose": f"{PURPOSE}: deliberately unsafe test payload, never sent",
        "rows": records,
        "batch": records,
        "tool_result": {"rows": records, "timestamp": stamps, "source_path": source_path or fname, "file": fname, "labels": labels},
        "signals": series,
        "evidence": [{"statement": t} for t in as_text],
        "question": "Explain these raw readings: " + (as_text[0] if as_text else ""),
    }
    n_readings = sum(1 for r in records for v in r.values() if isinstance(v, float))
    what = {
        "n_rows": len(records), "n_headers": len(records[0]) if records else 0, "n_readings": n_readings, "n_stamps": len(stamps), "n_labels": len(labels),
        "file_name": fname, "real_rows": real, "synthetic_stamps": synthetic_stamps, "n_series_points": sum(len(s["values"]) for s in series),
        "row_start": int(first_row) if isinstance(first_row, (int, float)) else None,
        "headers_example": list(records[0])[:6] if records else [],
    }
    return payload, what


# ----------------------------------------------------------------------------------------------
# before / after
# ----------------------------------------------------------------------------------------------


def _name_pattern(name: str) -> re.Pattern:
    return re.compile(guard_mod._LB + re.escape(name) + guard_mod._LA, re.IGNORECASE)


def _aliased(original_text: str, amap: dict[str, str]) -> list[dict[str, Any]]:
    """Original names that occur in the payload, with their alias and how often they occur."""
    out = []
    low = original_text.lower()
    for name, alias in sorted(amap.items(), key=lambda kv: (-len(kv[0]), kv[0])):
        if not name or name.lower() not in low:
            continue
        n = len(_name_pattern(name).findall(original_text))
        if n:
            out.append({"original": name, "alias": alias, "count": n})
    out.sort(key=lambda d: -d["count"])
    return out


def _rounded_examples(node: Any, digits: int, out: list[dict[str, str]], limit: int = _EXAMPLES) -> None:
    """Numbers the guard rounds: float fields first, then decimals inside sentences (time stamps are not numbers: the
    guard replaces them as a whole)."""
    if len(out) >= limit:
        return
    if isinstance(node, dict):
        for v in node.values():
            _rounded_examples(v, digits, out, limit)
    elif isinstance(node, list):
        for v in node:
            _rounded_examples(v, digits, out, limit)
    elif isinstance(node, float) and not isinstance(node, bool) and math.isfinite(node):
        r = guard_mod.round_sig(node, digits)
        if r != node and not any(e["before"] == repr(node) for e in out):
            out.append({"before": repr(node), "after": guard_mod._format_sig(node, digits)})
    elif isinstance(node, str) and "." in node:
        s = node
        for pat in (guard_mod._ISO_TOKEN_RE, guard_mod._DATE_TOKEN_RE, guard_mod._CLOCK_TOKEN_RE):
            s = pat.sub(" ", s)
        for m in guard_mod._DECIMAL_TOKEN_RE.finditer(s):
            tok = m.group(0)
            if len(out) < limit and guard_mod._token_sig_digits(tok) > digits and not any(e["before"] == tok for e in out):
                out.append({"before": tok, "after": guard_mod._format_sig(float(tok), digits)})


def _headers_found(text: str, headers: list[str]) -> list[str]:
    """Original headers that appear in `text`. A generic header word ('sample', 'time') counts only where it is used
    as a column name (a JSON string of its own), not as an English word inside a sentence."""
    found = []
    for h in headers:
        if h.lower() in guard_mod.GENERIC_COLUMN_WORDS:
            if re.search('"' + re.escape(h) + r'"(?!\s*:)', text):  # a JSON string value, not a key such as "source":
                found.append(h)
        elif _name_pattern(h).search(text):
            found.append(h)
    return found


def _before_after(payload: dict[str, Any], g: Any, settings: Settings, headers: list[str]) -> dict[str, Any]:
    plain = guard_mod.to_plain(payload)
    before_txt = json.dumps(plain, ensure_ascii=False, default=str)
    after_txt = json.dumps(g.sanitized_payload, ensure_ascii=False, default=str) if g.allowed else ""
    digits = int(settings.guard.external_sig_digits)
    rounded: list[dict[str, str]] = []
    _rounded_examples(plain, digits, rounded)
    counts = {k: int(v) for k, v in (g.sanitizer or {}).items() if isinstance(v, int) and not isinstance(v, bool)}
    removed = [n for n in g.notes if n.startswith("dropped") or n.startswith("invariant")]
    return {
        "verdict": "allowed" if g.allowed else "blocked",
        "reason": g.reason,
        "artifact_types": g.artifact_types,
        "bytes_before": guard_mod.payload_bytes(plain),
        "bytes_after": guard_mod.payload_bytes(g.sanitized_payload) if g.allowed else 0,
        "counts": counts,
        "aliased": _aliased(before_txt, g.alias_map or {}),
        "rounded_examples": rounded,
        "n_rounded": counts.get("floats_rounded", 0) + counts.get("numbers_in_strings_rounded", 0),
        "removed": removed[:12],
        "n_removed_notes": len(removed),
        "headers_in_output": _headers_found(after_txt, headers) if g.allowed else [],
        "preview_before": before_txt[:600],
        "preview_after": after_txt[:600],
    }


# ----------------------------------------------------------------------------------------------
# the demonstration
# ----------------------------------------------------------------------------------------------


def demo_settings(settings: Settings, profile: Optional[str] = None) -> Settings:
    """The profile the demonstration speaks for: --profile, else the active one when it allows external models, else
    hybrid. The guard itself needs neither a key nor an endpoint."""
    if profile:
        if profile not in settings.profiles:
            raise ValueError(f"unknown profile '{profile}'; known: {', '.join(settings.profiles)}")
        return settings.with_profile(profile)
    return settings if settings.active_profile.allow_external else settings.with_profile("hybrid")


def run_demo(ws: Any, settings: Settings, profile: Optional[str] = None, send: bool = False, language: str = "en") -> dict[str, Any]:
    """Run the demonstration on a run workspace; returns the result (also written to guard_demo.json)."""
    from . import router as router_mod

    t0 = time.perf_counter()
    s = demo_settings(settings, profile)
    prof = s.active_profile
    strict = bool(prof.guard_strict)
    sch = _schema(ws)
    headers = _headers(sch)
    task, payload, about = real_payload(ws)
    provider, model = s.external_llm.provider, s.external_model_for(task)

    # 1. the real payload
    g = guard_mod.check(payload, s, strict=strict, ws=ws)
    safe = _before_after(payload, g, s, headers)
    safe.update({"task": task, "about": about})
    rec = router_mod._record(ws, task=task, purpose=f"{PURPOSE}: real payload ({task} of {about['id']}) as the stage builds it, cleaned by the guard; shown, not sent", route="external", provider=provider, model=model, payload=g.sanitized_payload if g.allowed else payload, artifact_types=g.artifact_types, guard_result="demo_allowed" if g.allowed else "demo_blocked", guard_reason=g.reason, preview=g.allowed, sanitizer=router_mod._sanitizer_record(g))
    safe["ledger_id"] = rec.id

    # 2. an operator question naming original columns
    question = None
    q_text = operator_question(ws, about)
    if q_text:
        gq = guard_mod.check({"question": q_text}, s, strict=strict, ws=ws)
        q_after = str((gq.sanitized_payload or {}).get("question") or "")
        question = {"verdict": "allowed" if gq.allowed else "blocked", "reason": gq.reason, "before": q_text, "after": q_after, "counts": {k: int(v) for k, v in (gq.sanitizer or {}).items() if isinstance(v, int) and not isinstance(v, bool)}, "aliased": _aliased(q_text, gq.alias_map or {}), "headers_in_output": _headers_found(json.dumps(q_after), headers)}
        rq = router_mod._record(ws, task="why_chat", purpose=f"{PURPOSE}: operator question naming original columns, cleaned by the guard; shown, not sent", route="external", provider=provider, model=s.external_model_for("why_chat"), payload=gq.sanitized_payload if gq.allowed else {"question": "(withheld)"}, artifact_types=gq.artifact_types, guard_result="demo_allowed" if gq.allowed else "demo_blocked", guard_reason=gq.reason, preview=gq.allowed, sanitizer=router_mod._sanitizer_record(gq))
        question["ledger_id"] = rq.id

    # 3. the deliberately unsafe payload: checked, recorded, NEVER sent
    row0 = None
    if about.get("flag_ids"):
        fl = next((f for f in ws.read_jsonl("flags") if f.get("id") in about["flag_ids"]), None)
        row0 = (fl or {}).get("row_start")
    bad, what = unsafe_payload(ws, row0)
    gb = guard_mod.check(bad, s, strict=strict, ws=ws)
    amap = guard_mod.alias_map(ws, bad)
    _, file_tokens = guard_mod._names_from_ws(ws)
    raw_findings = guard_mod.verify_invariant(guard_mod.to_plain(bad), amap, guard_mod.data_vocabulary(ws) + file_tokens + ([what["file_name"]] if what.get("file_name") else []), s.guard)
    invariant_raw = list(dict.fromkeys(raw_findings))  # the invariant stops listing after 20 findings
    leaked = _headers_found(json.dumps(gb.sanitized_payload, ensure_ascii=False), headers) if gb.allowed else []
    unsafe = {
        "verdict": "allowed" if gb.allowed else "blocked", "reason": gb.reason, "what": what,
        "layers": [n for n in gb.notes if not n.startswith("alias map")][:14], "counts": {k: int(v) for k, v in (gb.sanitizer or {}).items() if isinstance(v, int) and not isinstance(v, bool)},
        "invariant_on_raw": invariant_raw[:10], "n_invariant_on_raw": len(raw_findings), "invariant_capped": len(raw_findings) >= 20,
        "bytes_before": guard_mod.payload_bytes(bad), "bytes_after": guard_mod.payload_bytes(gb.sanitized_payload) if gb.allowed else 0,
        "headers_in_output": leaked, "sent": False,
    }
    rb = router_mod._record(ws, task=UNSAFE_TASK, purpose=f"{PURPOSE}: deliberately unsafe test payload (raw rows under the original headers, full-precision readings, time stamps, label values, file name); never sent", route="external", provider=provider, model=model, payload=bad, artifact_types=gb.artifact_types or ["unknown"], guard_result="demo_allowed" if gb.allowed else "demo_blocked", guard_reason=gb.reason, preview=False, sanitizer=router_mod._sanitizer_record(gb))
    unsafe["ledger_id"] = rb.id

    # 4. optionally: send the SAFE payload once through the normal router
    sent: dict[str, Any] = {"requested": bool(send), "sent": False}
    if send:
        ready, why = router_mod.external_ready(task, ws, s)
        if not ready:
            sent["reason"] = f"not sent: the external route is not usable ({why})"
        elif not g.allowed:
            sent["reason"] = "not sent: the guard blocked the payload"
        else:
            res = router_mod.complete(task, payload, purpose=f"{PURPOSE}: real payload of {about['id']} sent once (--send)", ws=ws, settings=s, language=language)
            sent.update({"sent": res.route == "external", "route": res.route, "source": res.source, "ok": bool(res.ok), "ledger_id": res.ledger_id, "error": res.error, "latency_ms": res.latency_ms})
            if res.route != "external":
                sent["reason"] = f"the external call did not complete; the router answered on the {res.route} route ({(res.error or '')[:200]})"

    try:
        cov = ledger_mod.narrative_coverage(ws, s)
    except Exception:
        cov = None
    headers_check = {
        "n_headers": len(headers), "generic": [h for h in headers if h.lower() in guard_mod.GENERIC_COLUMN_WORDS],
        "found": sorted(set(safe["headers_in_output"]) | set((question or {}).get("headers_in_output") or []) | set(leaked)),
        "examples": headers if len(headers) <= 5 else headers[:4] + ["...", headers[-1]],
    }
    result = {
        "version": 1, "created_at": now_iso(), "run_id": getattr(ws, "run_id", ""), "profile": s.profile, "strict": strict,
        "external_model": s.external_llm.model, "endpoint": s.external_llm.base_url or "provider default endpoint",
        "external_block_reason": s.external_block_reason(task), "digits": int(s.guard.external_sig_digits),
        "safe": safe, "question": question, "unsafe": unsafe, "send": sent, "headers_check": headers_check,
        "coverage": {k: cov.get(k) for k in ("diagnoses", "critiques", "report_summaries", "reason", "sentence", "details")} if cov else None,
    }
    result["seconds"] = round(time.perf_counter() - t0, 2)
    result["statement"] = statement(result)
    ws.write_json(DEMO_FILE, result)
    return result


# ----------------------------------------------------------------------------------------------
# plain words: console (English), statement, report block (en / fi / sv)
# ----------------------------------------------------------------------------------------------

_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "title": "Egress guard demonstration",
        "intro": "Made on {created} (profile {profile}, {strict}; the Data flow page or `python -m tpm guard-demo`). {sent_note} The unsafe payload is never sent.",
        "strict": "strict guard", "nonstrict": "non-strict guard",
        "not_sent": "Nothing was sent.", "was_sent": "The real payload was then sent once (--send): {detail}.",
        "src_diagnosis": "the narrative payload of {id} (the strongest diagnosis), as the diagnose stage builds it",
        "src_catalog": "the signal catalog the profile stage sends for role hypotheses",
        "safe_head": "1. A real payload of this run: {source}.",
        "verdict_allowed": "Verdict: allowed, {before} bytes before and {after} bytes after the guard.",
        "verdict_blocked": "Verdict: blocked ({reason}).",
        "names": "Column names: {n} mention(s) of {k} original header(s) replaced by aliases, e.g. {examples}.",
        "names_none": "Column names: the payload names no original column (the stages already use aliases).",
        "numbers": "Numbers: {n} rounded to {d} significant digits, e.g. {examples}.",
        "numbers_none": "Numbers: none needed rounding.",
        "replaced": "Replaced: {times} date / time stamp(s), {values} text / label value(s), {files} file name(s).",
        "removed": "Removed: {n} field(s) or item(s): {reasons}.",
        "removed_none": "Removed: nothing.",
        "question_head": "2. An operator question that names original columns:",
        "before": "Before", "after": "After",
        "unsafe_head": "3. A deliberately unsafe test payload: {what}.",
        "what": "{n_rows} raw rows under {n_headers} original column headers, {n_readings} readings at full precision, a numeric series of {n_series} readings, {n_stamps} data time stamps{synthetic}{labels}{file}, as records and written out as text",
        "what_labels": ", the values of {n} label column(s)", "what_file": " and the source file name",
        "made_up": " (made-up readings under the real headers: dataset.parquet is not available here)",
        "synthetic": " (built from the row numbers: the dataset has no time column)",
        "unsafe_blocked": "The guard blocked it: {reason}.",
        "unsafe_allowed": "WARNING: the guard let part of it through: {reason}.",
        "layers": "Layer by layer",
        "invariant": "The last check alone would also have stopped it: {n} finding(s), e.g. {examples}.",
        "headers_ok": "None of the {n} original column headers of this dataset ({examples}) appears in anything that would be sent.",
        "headers_generic": " Generic header words ({generic}) are removed wherever a field names columns; inside sentences they are ordinary words.",
        "headers_bad": "Original headers found in what would be sent: {found}.",
        "headers_none": "The file had no header row: there are no original names to hide.",
        "ledger": "Ledger records of the demonstration: {ids}.",
    },
    "fi": {
        "title": "Tietovirran vartijan (egress guard) esittely",
        "intro": "Tehty {created} (profiili {profile}, {strict}; Tietovirta-sivu tai `python -m tpm guard-demo`). {sent_note} Vaarallista testikuormaa ei lähetetä koskaan.",
        "strict": "tiukka vartija", "nonstrict": "ei-tiukka vartija",
        "not_sent": "Mitään ei lähetetty.", "was_sent": "Todellinen kuorma lähetettiin sen jälkeen kerran (--send): {detail}.",
        "src_diagnosis": "diagnoosin {id} (vahvin diagnoosi) selityskuorma sellaisena kuin diagnoosivaihe sen rakentaa",
        "src_catalog": "signaaliluettelo, jonka profilointivaihe lähettää roolihypoteeseja varten",
        "safe_head": "1. Tämän ajon todellinen kuorma: {source}.",
        "verdict_allowed": "Tulos: sallittu, {before} tavua ennen ja {after} tavua vartijan jälkeen.",
        "verdict_blocked": "Tulos: estetty ({reason}).",
        "names": "Sarakenimet: {n} mainintaa {k} alkuperäisestä otsikosta korvattiin aliaksilla, esim. {examples}.",
        "names_none": "Sarakenimet: kuorma ei mainitse alkuperäisiä sarakkeita (vaiheet käyttävät jo aliaksia).",
        "numbers": "Luvut: {n} pyöristettiin {d} merkitsevään numeroon, esim. {examples}.",
        "numbers_none": "Luvut: mitään ei tarvinnut pyöristää.",
        "replaced": "Korvattu: {times} päivämäärää tai aikaleimaa, {values} teksti- tai luokka-arvoa, {files} tiedostonimeä.",
        "removed": "Poistettu: {n} kenttää tai alkiota: {reasons}.",
        "removed_none": "Poistettu: ei mitään.",
        "question_head": "2. Käyttäjän kysymys, jossa mainitaan alkuperäisiä sarakkeita:",
        "before": "Ennen", "after": "Jälkeen",
        "unsafe_head": "3. Tarkoituksella vaarallinen testikuorma: {what}.",
        "what": "{n_rows} raakariviä {n_headers} alkuperäisen sarakeotsikon alla, {n_readings} lukemaa täydellä tarkkuudella, {n_series} lukeman numerosarja, {n_stamps} aineiston aikaleimaa{synthetic}{labels}{file}, tietueina ja tekstiksi kirjoitettuina",
        "what_labels": ", {n} luokkasarakkeen arvot", "what_file": " ja lähdetiedoston nimi",
        "made_up": " (keksittyjä lukemia oikeiden otsikoiden alla: dataset.parquet ei ole tässä saatavilla)",
        "synthetic": " (muodostettu rivinumeroista: aineistossa ei ole aikasaraketta)",
        "unsafe_blocked": "Vartija esti sen: {reason}.",
        "unsafe_allowed": "VAROITUS: vartija päästi osan läpi: {reason}.",
        "layers": "Vaihe vaiheelta",
        "invariant": "Viimeinen tarkistus yksinään olisi myös pysäyttänyt sen: {n} havaintoa, esim. {examples}.",
        "headers_ok": "Yksikään tämän aineiston {n} alkuperäisestä sarakeotsikosta ({examples}) ei esiinny missään, mitä lähetettäisiin.",
        "headers_generic": " Yleiset otsikkosanat ({generic}) poistetaan kaikkialta, missä kenttä nimeää sarakkeita; lauseissa ne ovat tavallisia sanoja.",
        "headers_bad": "Lähetettävästä löytyi alkuperäisiä otsikoita: {found}.",
        "headers_none": "Tiedostossa ei ollut otsikkoriviä: piilotettavia alkuperäisiä nimiä ei ole.",
        "ledger": "Esittelyn kirjaukset tietovirtalokissa: {ids}.",
    },
    "sv": {
        "title": "Demonstration av utflödesvakten (egress guard)",
        "intro": "Gjord {created} (profil {profile}, {strict}; sidan Dataflöde eller `python -m tpm guard-demo`). {sent_note} Den osäkra testnyttolasten skickas aldrig.",
        "strict": "strikt vakt", "nonstrict": "icke-strikt vakt",
        "not_sent": "Inget skickades.", "was_sent": "Den verkliga nyttolasten skickades sedan en gång (--send): {detail}.",
        "src_diagnosis": "förklaringsnyttolasten för {id} (den starkaste diagnosen), så som diagnossteget bygger den",
        "src_catalog": "signalkatalogen som profileringssteget skickar för rollhypoteser",
        "safe_head": "1. En verklig nyttolast från den här körningen: {source}.",
        "verdict_allowed": "Utfall: tillåten, {before} byte före och {after} byte efter vakten.",
        "verdict_blocked": "Utfall: blockerad ({reason}).",
        "names": "Kolumnnamn: {n} förekomst(er) av {k} ursprungliga rubrik(er) ersattes med alias, t.ex. {examples}.",
        "names_none": "Kolumnnamn: nyttolasten nämner inga ursprungliga kolumner (stegen använder redan alias).",
        "numbers": "Tal: {n} avrundades till {d} värdesiffror, t.ex. {examples}.",
        "numbers_none": "Tal: inget behövde avrundas.",
        "replaced": "Ersatt: {times} datum eller tidsstämplar, {values} text- eller etikettvärden, {files} filnamn.",
        "removed": "Borttaget: {n} fält eller poster: {reasons}.",
        "removed_none": "Borttaget: inget.",
        "question_head": "2. En operatörsfråga som nämner ursprungliga kolumner:",
        "before": "Före", "after": "Efter",
        "unsafe_head": "3. En avsiktligt osäker testnyttolast: {what}.",
        "what": "{n_rows} råa rader under {n_headers} ursprungliga kolumnrubriker, {n_readings} avläsningar med full precision, en talserie med {n_series} avläsningar, {n_stamps} tidsstämplar ur datan{synthetic}{labels}{file}, som poster och utskrivet som text",
        "what_labels": ", värdena i {n} etikettkolumn(er)", "what_file": " och källfilens namn",
        "made_up": " (påhittade avläsningar under de verkliga rubrikerna: dataset.parquet finns inte här)",
        "synthetic": " (byggda av radnumren: datamängden har ingen tidskolumn)",
        "unsafe_blocked": "Vakten blockerade den: {reason}.",
        "unsafe_allowed": "VARNING: vakten släppte igenom en del: {reason}.",
        "layers": "Steg för steg",
        "invariant": "Den sista kontrollen ensam hade också stoppat den: {n} fynd, t.ex. {examples}.",
        "headers_ok": "Ingen av datamängdens {n} ursprungliga kolumnrubriker ({examples}) förekommer i något som skulle skickas.",
        "headers_generic": " Allmänna rubrikord ({generic}) tas bort överallt där ett fält namnger kolumner; i meningar är de vanliga ord.",
        "headers_bad": "Ursprungliga rubriker i det som skulle skickas: {found}.",
        "headers_none": "Filen hade ingen rubrikrad: det finns inga ursprungliga namn att dölja.",
        "ledger": "Demonstrationens poster i utflödesloggen: {ids}.",
    },
}


def _fmt(n: Any, lang: str) -> str:
    return ledger_mod._fmt_int(n, lang)


def _safe_lines(safe: dict[str, Any], tx: dict[str, str], lang: str, digits: int) -> list[str]:
    about = safe.get("about") or {}
    src = tx["src_diagnosis"].format(id=about.get("id")) if about.get("kind") == "diagnosis" else tx["src_catalog"]
    lines = [tx["safe_head"].format(source=src)]
    if safe.get("verdict") == "allowed":
        lines.append(tx["verdict_allowed"].format(before=_fmt(safe.get("bytes_before"), lang), after=_fmt(safe.get("bytes_after"), lang)))
    else:
        lines.append(tx["verdict_blocked"].format(reason=safe.get("reason")))
    al = safe.get("aliased") or []
    if al:
        ex = ", ".join(f"{a['original']} -> {a['alias']}" for a in al[:_EXAMPLES]) + (", ..." if len(al) > _EXAMPLES else "")
        lines.append(tx["names"].format(n=_fmt(sum(a["count"] for a in al), lang), k=len(al), examples=ex))
    else:
        lines.append(tx["names_none"])
    if safe.get("n_rounded"):
        ex = ", ".join(f"{e['before']} -> {e['after']}" for e in (safe.get("rounded_examples") or [])[:3])
        lines.append(tx["numbers"].format(n=_fmt(safe["n_rounded"], lang), d=digits, examples=ex))
    else:
        lines.append(tx["numbers_none"])
    c = safe.get("counts") or {}
    lines.append(tx["replaced"].format(times=c.get("times_redacted", 0), values=c.get("values_redacted", 0), files=c.get("files_redacted", 0)))
    n_rm = c.get("keys_dropped", 0) + c.get("fields_dropped", 0) + c.get("items_dropped", 0) + c.get("human_notes_dropped", 0)
    if n_rm:
        lines.append(tx["removed"].format(n=n_rm, reasons="; ".join((safe.get("removed") or [])[:4])))
    else:
        lines.append(tx["removed_none"])
    return lines


def _unsafe_lines(unsafe: dict[str, Any], tx: dict[str, str], lang: str) -> list[str]:
    w = unsafe.get("what") or {}
    labels = tx["what_labels"].format(n=w["n_labels"]) if w.get("n_labels") else ""
    what = tx["what"].format(n_rows=w.get("n_rows", 0), n_headers=w.get("n_headers", 0), n_readings=_fmt(w.get("n_readings", 0), lang), n_series=w.get("n_series_points", 0), n_stamps=w.get("n_stamps", 0), synthetic=tx["synthetic"] if w.get("synthetic_stamps") else "", labels=labels, file=tx["what_file"] if w.get("file_name") else "")
    if not w.get("real_rows", True):
        what += tx["made_up"]
    lines = [tx["unsafe_head"].format(what=what)]
    lines.append((tx["unsafe_blocked"] if unsafe.get("verdict") == "blocked" else tx["unsafe_allowed"]).format(reason=unsafe.get("reason")))
    if unsafe.get("n_invariant_on_raw"):
        n = unsafe["n_invariant_on_raw"]
        lines.append(tx["invariant"].format(n=f"{n}+" if unsafe.get("invariant_capped") else n, examples="; ".join((unsafe.get("invariant_on_raw") or [])[:3])))
    return lines


def _header_line(result: dict[str, Any], tx: dict[str, str]) -> str:
    hc = result.get("headers_check") or {}
    if not hc.get("n_headers"):
        return tx["headers_none"]
    if hc.get("found"):
        return tx["headers_bad"].format(found=", ".join(hc["found"]))
    line = tx["headers_ok"].format(n=hc["n_headers"], examples=", ".join(hc.get("examples") or []))
    if hc.get("generic"):
        line += tx["headers_generic"].format(generic=", ".join(hc["generic"]))
    return line


def _sent_note(result: dict[str, Any], tx: dict[str, str]) -> str:
    snd = result.get("send") or {}
    if snd.get("sent"):
        return tx["was_sent"].format(detail=f"{snd.get('source')}, ledger {snd.get('ledger_id')}")
    if snd.get("requested"):
        return tx["not_sent"] + " (" + str(snd.get("reason") or "") + ")"
    return tx["not_sent"]


def statement(result: dict[str, Any]) -> str:
    """Two or three plain English sentences for the data-flow statement (UI and report)."""
    safe, unsafe, q = result.get("safe") or {}, result.get("unsafe") or {}, result.get("question") or {}
    c = safe.get("counts") or {}
    n_names = sum(a["count"] for a in safe.get("aliased") or [])
    n_rm = c.get("keys_dropped", 0) + c.get("fields_dropped", 0) + c.get("items_dropped", 0)
    about = safe.get("about") or {}
    parts = [f"Guard demonstration (python -m tpm guard-demo, profile {result.get('profile')}, {'strict' if result.get('strict') else 'non-strict'} guard, {str(result.get('created_at'))[:16].replace('T', ' ')}): "
             f"the real {safe.get('task')} payload of {about.get('id')} was cleaned ({n_names} column-name mention(s) aliased, {safe.get('n_rounded', 0)} number(s) rounded to {result.get('digits')} significant digits, {n_rm} field(s) removed) and would be {safe.get('verdict')}"]
    if q:
        parts.append(f"an operator question naming original columns became '{q.get('after')}'")
    w = unsafe.get("what") or {}
    parts.append(f"a deliberately unsafe payload ({w.get('n_rows')} raw rows under {w.get('n_headers')} original headers, full-precision readings, time stamps, label values, the file name) was {unsafe.get('verdict')}: {str(unsafe.get('reason'))[:180]}")
    hc = result.get("headers_check") or {}
    tail = f" None of the {hc.get('n_headers')} original column headers appears in what would be sent." if hc.get("n_headers") and not hc.get("found") else ""
    snd = result.get("send") or {}
    sent = f" The real payload was then sent once with --send ({snd.get('source')}, {snd.get('ledger_id')})." if snd.get("sent") else " Nothing of the demonstration was sent."
    return "; ".join(parts) + "." + tail + sent


def format_demo(result: dict[str, Any], ws_dir: Optional[Path] = None) -> list[str]:
    """Console lines, English."""
    tx = _TEXT["en"]
    out = [f"{tx['title']} on run {result.get('run_id')}: profile {result.get('profile')} ({tx['strict'] if result.get('strict') else tx['nonstrict']}), external model {result.get('external_model')} at {result.get('endpoint')}"]
    if result.get("external_block_reason"):
        out.append(f"  (the external route itself is not usable right now: {result['external_block_reason']}; the guard works the same)")
    out.append(_sent_note(result, tx) + " The unsafe payload is never sent.")
    out.append("")
    safe = result.get("safe") or {}
    out += [("  " if i else "") + line for i, line in enumerate(_safe_lines(safe, tx, "en", int(result.get("digits") or 3)))]
    out.append(f"  {tx['before']} ({safe.get('bytes_before')} bytes, first 300 characters): {str(safe.get('preview_before'))[:300]}")
    if safe.get("preview_after"):
        out.append(f"  {tx['after']}  ({safe.get('bytes_after')} bytes, first 300 characters): {str(safe.get('preview_after'))[:300]}")
    q = result.get("question")
    if q:
        out += ["", tx["question_head"], f"  {tx['before']}: {q.get('before')}", f"  {tx['after']}:  {q.get('after')}", f"  Verdict: {q.get('verdict')} ({q.get('reason')})"]
    unsafe = result.get("unsafe") or {}
    ul = _unsafe_lines(unsafe, tx, "en")
    out += ["", ul[0], "  " + ul[1]]
    if unsafe.get("layers"):
        out.append(f"  {tx['layers']}:")
        out += [f"    - {n}" for n in unsafe["layers"]]
    out += ["  " + x for x in ul[2:]]
    out += ["", _header_line(result, tx)]
    ids = [x for x in ((safe or {}).get("ledger_id"), (q or {}).get("ledger_id"), unsafe.get("ledger_id"), (result.get("send") or {}).get("ledger_id")) if x]
    out.append(tx["ledger"].format(ids=", ".join(ids)))
    cov = result.get("coverage") or {}
    if cov.get("sentence"):
        out += ["", "Who wrote the explanations: " + cov["sentence"]] + ["  " + d for d in cov.get("details") or []]
    if ws_dir is not None:
        out += ["", f"Written: {Path(ws_dir) / DEMO_FILE}  ({result.get('seconds')} s)"]
    return out


def load(ws: Any) -> Optional[dict[str, Any]]:
    try:
        d = ws.read_json(DEMO_FILE, None)
        return d if isinstance(d, dict) and d.get("safe") else None
    except Exception:
        return None


def report_context(ws: Any, lang: str = "en") -> Optional[dict[str, Any]]:
    """The report's data-flow block for the last demonstration of the run, in en / fi / sv; None when none was made.
    {"title", "intro", "safe": [lines], "question": {"head", "before", "after", labels} | None, "unsafe": [lines],
    "layers": [...], "layers_label", "headers", "ledger", "unsafe_ok"}"""
    result = load(ws)
    if result is None:
        return None
    tx = _TEXT.get(lang) or _TEXT["en"]
    unsafe = result.get("unsafe") or {}
    ul = _unsafe_lines(unsafe, tx, lang)
    q = result.get("question")
    ids = [x for x in ((result.get("safe") or {}).get("ledger_id"), (q or {}).get("ledger_id"), unsafe.get("ledger_id"), (result.get("send") or {}).get("ledger_id")) if x]
    return {
        "title": tx["title"],
        "intro": tx["intro"].format(created=str(result.get("created_at"))[:16].replace("T", " "), profile=result.get("profile"), strict=tx["strict"] if result.get("strict") else tx["nonstrict"], sent_note=_sent_note(result, tx)),
        "safe": _safe_lines(result.get("safe") or {}, tx, lang, int(result.get("digits") or 3)),
        "question": {"head": tx["question_head"], "before_label": tx["before"], "after_label": tx["after"], "before": q.get("before"), "after": q.get("after")} if q else None,
        "unsafe": ul,
        "unsafe_ok": unsafe.get("verdict") == "blocked",
        "layers_label": tx["layers"],
        "layers": list(unsafe.get("layers") or [])[:10],
        "headers": _header_line(result, tx),
        "ledger": tx["ledger"].format(ids=", ".join(ids)),
    }
