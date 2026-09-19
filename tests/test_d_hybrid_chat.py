"""Hybrid chat: the tool agent on the external model (spec section 4). AnthropicProvider.chat is a scripted stub (a
sequence of tool calls, then a final answer), the key is fake, the local model is a stub too: no network, no Ollama.

The run is a real pipeline pass over the shared synthetic generator with distinctive column names, label values,
timestamps and file name, so that a hit in an outgoing message cannot be a coincidence. Asserted: nothing raw is in any
message that reaches the provider; the sql tool is not offered; stats / series are the specified reductions while the UI
still gets the full series; a tool result the guard refuses is withheld and the turn goes on; an external failure restarts
the turn on the local agent; the call cap and the run budget hold; no-egress never touches the external provider."""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fixtures.synth import make_synthetic  # noqa: E402
from tpm.config import load_settings  # noqa: E402
from tpm.llm import agent as agent_mod  # noqa: E402
from tpm.llm import ledger, router  # noqa: E402
from tpm.llm.providers import AnthropicProvider, OllamaProvider, ProviderError  # noqa: E402
from tpm.workspace import Workspace  # noqa: E402

RENAME = {
    "run": "CampaignNo", "sample": "SampleIdx", "timestamp": "LoggedAt", "fault_label": "FaultClass",
    "flow_a": "FT101_FeedFlow", "press_r": "PT204_ReactorPress", "temp_r": "TT310_ReactorTemp", "level_s": "LT415_SepLevel",
    "flow_b": "FT102_RecycleFlow", "temp_s": "TT320_SepTemp", "valve_1": "XV501_FeedValve", "valve_2": "XV502_PurgeValve",
    "comp_a": "AT601_CompA", "const_c": "KC700_Const", "derived_sum": "FY103_TotalFlow", "power_c": "JT800_CompPower",
}
LABELS = {"normal": "NominalOp", "step": "ZetaTrip", "ramp": "QuorumLeak", "stuck_sensor": "FrozenProbeX", "corr_break": "LoopDecoupled", "oscillation": "HuntingValve", "noise_burst": "StaticBurst"}
FILE_NAME = "plant7_secret_campaign.csv"
PRESS = "PT204_ReactorPress"


@pytest.fixture(scope="module")
def finished_run(tmp_path_factory):
    from tpm.pipeline import run_pipeline

    tmp = tmp_path_factory.mktemp("hybrid_chat")
    s = load_settings()
    s.workspace_dir = str(tmp / "ws")
    s.detect.time_budget_s = 90
    s.detect.n_folds = 3
    s.detect.use_autoencoder = False
    df, _ = make_synthetic(n_groups=8, n_samples=240, seed=5, with_timestamp=True, with_labels=True)
    df["fault_label"] = df["fault_label"].map(LABELS)
    df = df.rename(columns=RENAME)
    src = tmp / FILE_NAME
    df.to_csv(src, index=False)
    st = run_pipeline(str(src), run_id="chat_src", settings=s, stages=["ingest", "profile", "quality", "detect", "diagnose"], options={"no_llm": True, "skip_llm": True, "use_llm": False, "report_llm": False})
    assert st.state == "done", [(x.stage, x.error) for x in st.stages if x.state == "failed"]
    ws = Workspace(run_id="chat_src", settings=s)
    assert ws.flags() and ws.diagnoses()
    return tmp, df, ws


def _run_copy(finished_run, name: str, profile: str = "hybrid"):
    tmp, df, src_ws = finished_run
    root = tmp / name
    shutil.copytree(src_ws.dir, root / "chat_run", ignore=shutil.ignore_patterns("decision_log.sqlite*", "duck_tmp"))
    settings = load_settings(profile=profile)
    settings.workspace_dir = str(root)
    return Workspace("chat_run", settings=settings, root=root), settings, df


def _scripted(monkeypatch, script, seen, fail_at: int | None = None):
    """AnthropicProvider.chat answers with script[i] on its i-th call (the last entry repeats); fail_at raises instead."""

    def fake(self, messages, schema=None, max_tokens=None, model=None):
        seen.append({"model": model or self.cfg.model, "messages": [dict(m) for m in messages]})
        if fail_at is not None and len(seen) >= fail_at:
            raise ProviderError("anthropic APIConnectionError: down")
        step = script[min(len(seen) - 1, len(script) - 1)]
        self.last_usage = {"input_tokens": 100, "output_tokens": 20}
        return json.dumps(step), step, 4

    def no_client(self):
        raise AssertionError("the network client must never be built in this test")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    monkeypatch.setattr(AnthropicProvider, "chat", fake)
    monkeypatch.setattr(AnthropicProvider, "_client", no_client)


def _no_ollama(monkeypatch):
    monkeypatch.setattr(OllamaProvider, "is_available", lambda self: False)
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: [])


def _local_model(monkeypatch, step, seen):
    def fake(self, messages, schema=None, max_tokens=None, model=None, temperature=None):
        seen.append([dict(m) for m in messages])
        return json.dumps(step), step, 3

    monkeypatch.setattr(OllamaProvider, "is_available", lambda self: True)
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: ["gemma3:4b"])
    monkeypatch.setattr(OllamaProvider, "capabilities", lambda self, m: [])
    monkeypatch.setattr(OllamaProvider, "chat", fake)


def _alias(ws: Workspace, column: str) -> str:
    return ws.schema().signal_alias[column]


# ---------------------------------------------------------------------------------------------- raw-data scan
_NUM_RE = re.compile(r"(?<![\w.])[-+]?\d+\.\d+(?:[eE][-+]?\d+)?(?![\w])")


def _raw_numbers(df: pd.DataFrame) -> dict[int, set[float]]:
    vals = [df[c].to_numpy(dtype="float64") for c in df.columns if pd.api.types.is_float_dtype(df[c])]
    allv = np.unique(np.concatenate([v[np.isfinite(v)] for v in vals]))
    return {d: set(np.round(allv, d).tolist()) for d in range(0, 9)}


def _sig_digits(tok: str) -> int:
    return len(re.split(r"[eE]", tok.lstrip("+-"))[0].replace(".", "").strip("0"))


def _scan(text: str, df: pd.DataFrame, by_dec: dict[int, set[float]]) -> list[str]:
    hits = []
    low = text.lower()
    hits += [f"column name {c}" for c in df.columns if c.lower() in low]
    hits += [f"label value {v}" for v in LABELS.values() if v.lower() in low]
    hits += [f"file name {t}" for t in (FILE_NAME, Path(FILE_NAME).stem) if t.lower() in low]
    if re.search(r"\d{4}-\d{2}-\d{2}", text) or re.search(r"(?<![\d:])\d{1,2}:\d{2}:\d{2}", text):
        hits.append("date / time")
    for m in _NUM_RE.finditer(text):
        tok = m.group(0)
        if _sig_digits(tok) > 3:
            hits.append(f"number with more than 3 significant digits {tok}")
        if _sig_digits(tok) >= 5 and round(float(tok), min(8, len(re.split(r"[eE]", tok.split(".")[1])[0]))) in by_dec[min(8, len(re.split(r"[eE]", tok.split(".")[1])[0]))]:
            hits.append(f"raw cell value {tok}")
    return hits


def _sent_text(seen: list[dict]) -> str:
    return "\n".join(str(m.get("content", "")) for call in seen for m in call["messages"])


# ---------------------------------------------------------------------------------------------- the external turn
def test_external_turn_sends_no_raw_data_and_names_come_back(monkeypatch, finished_run):
    ws, s, df = _run_copy(finished_run, "turn")
    s.external_llm.max_calls_per_chat_turn = 10
    flag = ws.flags()[0]
    diag = ws.diagnoses()[0]
    press = _alias(ws, PRESS)
    raw_value = float(df[PRESS].iloc[7])
    raw_time = str(df["LoggedAt"].iloc[7])
    assert re.search(r"\d{4}-\d{2}-\d{2}", raw_time) and len(repr(raw_value)) > 8
    script = [
        {"thought": "the flag", "action": "get_flag", "args": {"id": flag.id}},
        {"thought": "raw rows would help", "action": "sql", "args": {"query": f'SELECT "{PRESS}" FROM dataset'}},
        {"thought": "the diagnosis", "action": "get_diagnosis", "args": {"id": diag.id}},
        {"thought": "catalog", "action": "describe_signal", "args": {"id": press}},
        {"thought": "aggregates", "action": "stats", "args": {"signal": press}},
        {"thought": "shape", "action": "series", "args": {"signal": press, "row_start": 0, "row_end": 1199}},
        {"thought": "checks", "action": "list_checks", "args": {}},
        {"thought": "search", "action": "search", "args": {"query": "reactor pressure shifted"}},
        {"thought": "done", "action": "final", "answer": f"{press} moved first and the others followed [{flag.evidence_ids[0] if flag.evidence_ids else flag.id}]. Later {press} settled.", "citations": list(flag.evidence_ids[:1]), "confidence": 0.7, "suggested_followups": [f"Show {press} around the flag"]},
    ]
    seen: list[dict] = []
    _scripted(monkeypatch, script, seen)
    _no_ollama(monkeypatch)
    history = [{"role": "user", "content": f"what is {PRESS}?"}, {"role": "assistant", "content": f"{press} ({PRESS}) is a pressure-like signal, logged at {raw_time}."}]
    question = f"Why did {PRESS} reach {raw_value!r} at {raw_time} while FaultClass was {LABELS['step']} in {FILE_NAME}? See {flag.id}."
    out = agent_mod.chat(ws, s, question, context={"flag_id": flag.id}, history=history)

    assert out["route"] == "external" and out["source"] == "llm-external:claude-sonnet-5"
    assert out["external_calls"] == len(seen) == len(script)
    assert [t["tool"] for t in out["tool_trace"]] == [st["action"] for st in script[:-1]]
    assert all(t.get("route") == "external" for t in out["tool_trace"])
    # names come back locally, on the first mention only; followups keep the alias (they become the next question)
    assert out["answer"].startswith(f"{press} ({PRESS}) moved first") and out["answer"].count(PRESS) == 1
    assert out["suggested_followups"] == [f"Show {press} around the flag"]
    assert ws.read_jsonl("chat")[-1]["route"] == "external"

    # nothing raw in anything that reached the provider, nor in the ledger previews of it
    by_dec = _raw_numbers(df)
    text = _sent_text(seen)
    problems = _scan(text, df, by_dec)
    recs = [r for r in ledger.read(ws) if r.route == "external"]
    assert len(recs) == len(seen) and all(r.guard_result == "allowed" and r.ok and r.task == "why_chat" for r in recs)
    for r in recs:
        problems += [f"ledger preview {r.id}: {h}" for h in _scan(r.payload_preview, df, by_dec)]
    assert not problems, "raw data in outgoing chat text:\n" + "\n".join(sorted(set(problems))[:40])
    assert "[time]" in text and "[file]" in text and "[value]" in text and f"Why did {press} reach" in text
    assert f"{press} ({press})" not in text  # the expanded answer in the history went back as the plain alias

    # the sql tool is not offered, and asking for it anyway runs nothing
    system = seen[0]["messages"][0]["content"]
    assert "- sql(" not in system and "sql" not in system.lower() and "- stats(" in system and "never see rows" in system
    sql_step = out["tool_trace"][1]
    assert sql_step["tool"] == "sql" and not sql_step["ok"] and "unknown tool" in sql_step["summary"]

    # stats / series are reductions; single readings and the UI series never leave
    for key in ('"min"', '"max"', '"points"', '"column"', '"source_column"'):
        assert key not in text, key
    stats_msg = next(c["messages"][-1]["content"] for c in seen if c["messages"][-1]["content"].startswith("Tool result for stats"))
    stats = json.loads(stats_msg[len("Tool result for stats: "):stats_msg.index("\n(")])
    assert set(stats) == {"n", "mean", "std", "q05", "median", "q95", "n_null", "row_start", "row_end", "signal", "group_id", "n_samples"} and stats["signal"] == press
    series_msg = next(c["messages"][-1]["content"] for c in seen if c["messages"][-1]["content"].startswith("Tool result for series"))
    coarse = json.loads(series_msg[len("Tool result for series: "):series_msg.index("\n(")])
    assert 2 <= len(coarse["bucket_mean"]) <= s.guard.max_series_points and coarse["bucket_rows"] >= s.guard.min_aggregate_n
    assert len(coarse["bucket_mean"]) == len(coarse["bucket_row_start"]) == len(coarse["bucket_mean_in_std"])
    full = out["series"]
    assert full and full["local_only"] and full["column"] == PRESS and len(full["points"]) > 10 * len(coarse["bucket_mean"])
    assert full["columns"] == ["row", "mean", "min", "max"]


def test_refused_tool_result_is_withheld_and_the_turn_goes_on(monkeypatch, finished_run):
    ws, s, df = _run_copy(finished_run, "withheld")
    rows = df[[PRESS, "TT310_ReactorTemp", "FT101_FeedFlow", "LT415_SepLevel", "FT102_RecycleFlow"]].head(6).to_numpy().tolist()
    monkeypatch.setattr(agent_mod.Toolbox, "tool_list_checks", lambda self, **_: {"rows": rows})  # a tool that hands out raw rows
    script = [
        {"thought": "checks", "action": "list_checks", "args": {}},
        {"thought": "done", "action": "final", "answer": "The checks could not be shared, so I cannot tell from the data.", "citations": []},
    ]
    seen: list[dict] = []
    _scripted(monkeypatch, script, seen)
    _no_ollama(monkeypatch)
    out = agent_mod.chat(ws, s, "Which checks failed?")
    assert out["route"] == "external" and out["external_calls"] == 2 and out["answer"].startswith("The checks could not be shared")
    step = out["tool_trace"][0]
    assert step["tool"] == "list_checks" and step["withheld"] is True and step["ok"]
    told = seen[1]["messages"][-1]["content"]
    assert told.startswith('Tool result for list_checks: {"withheld": true, "reason": "')
    assert not _scan(_sent_text(seen), df, _raw_numbers(df))
    assert all(r.guard_result == "allowed" for r in ledger.read(ws) if r.route == "external")  # the turn itself was never blocked


def test_external_failure_restarts_the_turn_on_the_local_agent(monkeypatch, finished_run):
    ws, s, df = _run_copy(finished_run, "fallback")
    flag = ws.flags()[0]
    script = [{"thought": "the flag", "action": "get_flag", "args": {"id": flag.id}}]
    seen: list[dict] = []
    _scripted(monkeypatch, script, seen, fail_at=2)
    local_seen: list[list[dict]] = []
    _local_model(monkeypatch, {"thought": "done", "action": "final", "answer": f"{flag.id} was raised because several signals shifted together.", "citations": []}, local_seen)
    out = agent_mod.chat(ws, s, f"Why was {flag.id} raised?", context={"flag_id": flag.id})
    assert out["route"] == "local" and out["source"] == "llm-local:gemma3:4b" and out["external_calls"] == 2 == len(seen)
    assert out["answer"].startswith(f"{flag.id} was raised")
    # the local agent started the turn again with its own prompt and tools; nothing of the external conversation is reused
    assert len(local_seen) == 1 and len(local_seen[0]) == 2
    assert "- sql(" in local_seen[0][0]["content"] and PRESS in local_seen[0][0]["content"] and "Tool result" not in local_seen[0][-1]["content"]
    by_dec = _raw_numbers(df)
    assert not _scan(_sent_text(seen), df, by_dec)
    assert any(h.startswith("column name") for h in _scan(local_seen[0][0]["content"], df, by_dec))  # the scan itself works
    ext_steps = [t for t in out["tool_trace"] if t.get("route") == "external"]
    assert [t["tool"] for t in ext_steps] == ["get_flag", None] and "down" in ext_steps[-1]["error"]
    routes = [(r.route, r.ok) for r in ledger.read(ws)]
    assert routes == [("external", True), ("external", False), ("local", True)]

    # no local model either: the deterministic answer, and it says so
    _no_ollama(monkeypatch)
    out2 = agent_mod.chat(ws, s, f"Why was {flag.id} raised?", context={"flag_id": flag.id})
    assert out2["route"] == "none" and out2["source"] == "template" and out2["external_calls"] == 1
    assert flag.id in out2["answer"] and "external model did not finish" in out2["answer"]


def test_call_cap_per_turn_and_run_budget(monkeypatch, finished_run):
    ws, s, df = _run_copy(finished_run, "cap")
    s.external_llm.max_calls_per_chat_turn = 3
    ev = [e.id for e in ws.evidence.all()][:8]
    seen: list[dict] = []

    def never_done(self, messages, schema=None, max_tokens=None, model=None):
        seen.append({"messages": messages})
        step = {"thought": "more", "action": "get_evidence", "args": {"id": ev[len(seen) % len(ev)]}}
        return json.dumps(step), step, 2

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    monkeypatch.setattr(AnthropicProvider, "chat", never_done)
    _no_ollama(monkeypatch)
    out = agent_mod.chat(ws, s, "tell me everything")
    assert len(seen) == 3 == out["external_calls"] and out["route"] == "none" and out["source"] == "template"
    assert "at most 2 tool calls" in seen[0]["messages"][0]["content"]
    assert 'Respond now with action "final"' in seen[2]["messages"][-1]["content"]
    assert len([t for t in out["tool_trace"] if t.get("tool")]) == 2

    # run budget used up: the turn is local from the start, the external provider is not called again
    s.external_llm.max_calls_per_run = 3
    assert router.external_ready("why_chat", ws, s)[0] is False
    out2 = agent_mod.chat(ws, s, "and now?")
    assert len(seen) == 3 and out2["external_calls"] == 0 and out2["route"] == "none"


def test_external_calls_made_by_a_tool_count_toward_the_turn_cap(monkeypatch, finished_run):
    ws, s, df = _run_copy(finished_run, "nested")
    s.external_llm.max_calls_per_chat_turn = 4
    press = _alias(ws, PRESS)

    def assessor(ws_, settings_, action_text):  # like tpm.assessor.ask: two routed model calls of its own
        return [router.complete("assessor_chat", {"question": action_text, "template_answer": "Nothing is changed until you approve."}, purpose="nested", ws=ws_, settings=settings_).route for _ in range(2)]

    monkeypatch.setattr(agent_mod, "_assessor_fn", lambda: assessor)
    seen: list[dict] = []

    def fake(self, messages, schema=None, max_tokens=None, model=None):
        nested = "action" not in ((schema or {}).get("properties") or {})
        seen.append({"nested": nested, "messages": [dict(m) for m in messages]})
        if nested:
            step = {"answer": "Dropping it is safe.", "citations": [], "confidence": 0.6, "suggested_followups": []}
        elif len(seen) == 1:
            step = {"thought": "ask the assessor", "action": "assessor_evaluate", "args": {"action_text": f"drop {press}"}}
        else:
            step = {"thought": "done", "action": "final", "answer": f"Dropping {press} looks safe, nothing was applied.", "citations": []}
        return json.dumps(step), step, 2

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    monkeypatch.setattr(AnthropicProvider, "chat", fake)
    _no_ollama(monkeypatch)
    out = agent_mod.chat(ws, s, f"Can I drop {PRESS}?", task="why_chat")
    assert [c["nested"] for c in seen] == [False, True, True, False]
    assert out["route"] == "external" and out["external_calls"] == 4
    assert '(0 tool calls left.) Respond now with action "final"' in seen[-1]["messages"][-1]["content"]
    assert not _scan(_sent_text(seen), df, _raw_numbers(df))


def test_no_egress_never_touches_the_external_provider(monkeypatch, finished_run):
    ws, s, df = _run_copy(finished_run, "noegress", profile="no-egress")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")

    def boom(*a, **k):
        raise AssertionError("no-egress must never touch the external provider")

    for name in ("__init__", "chat", "_client", "is_available"):
        monkeypatch.setattr(AnthropicProvider, name, boom)
    monkeypatch.setattr(router, "agent_chat", boom)
    local_seen: list[list[dict]] = []
    _local_model(monkeypatch, {"thought": "done", "action": "final", "answer": "Several signals shifted together in that stretch of rows.", "citations": []}, local_seen)
    out = agent_mod.chat(ws, s, f"Why did {PRESS} move?")
    assert out["route"] == "local" and out["source"].startswith("llm-local:") and out["external_calls"] == 0
    assert "- sql(" in local_seen[0][0]["content"] and PRESS in local_seen[0][0]["content"]  # the local agent keeps raw access
    assert all(r.route == "local" for r in ledger.read(ws))


# ---------------------------------------------------------------------------------------------- the external toolbox
def test_external_toolbox_reductions(finished_run):
    ws, s, df = _run_copy(finished_run, "toolbox")
    press = _alias(ws, PRESS)
    local = agent_mod.Toolbox(ws, s)
    tb = local.external_view()
    assert "sql" in local.names and "sql" not in tb.names and "error" in tb.call("sql", {"query": "SELECT 1"})
    assert [t["name"] for t in agent_mod.tool_specs(s, external=True)] == tb.names

    st = tb.call("stats", {"signal": press, "row_start": 0, "row_end": 239})
    assert st["n"] == st["n_samples"] and "min" not in st and "max" not in st and "column" not in st and st["q05"] <= st["median"] <= st["q95"]
    assert {"min", "max", "column"} <= set(local.call("stats", {"signal": press}))  # the local agent still gets them
    short = tb.call("stats", {"signal": press, "row_start": 0, "row_end": s.guard.min_aggregate_n - 2})
    assert "too short" in short["error"]
    # only catalogued signals: label, id and time columns are not summarised for an external model
    for column in ("FaultClass", "SampleIdx", "LoggedAt"):
        assert "error" in tb.call("stats", {"signal": column}) and "error" in tb.call("series", {"signal": column, "row_start": 0, "row_end": 500})

    n = len(df)
    ser = tb.call("series", {"signal": press, "row_start": 0, "row_end": n - 1})
    assert len(ser["bucket_mean"]) <= s.guard.max_series_points and ser["bucket_rows"] == int(np.ceil(n / s.guard.max_series_points))
    assert "points" not in ser and "columns" not in ser and tb.last_series["n_raw"] == n and len(tb.last_series["points"]) > 100
    small = tb.call("series", {"signal": press, "row_start": 0, "row_end": 99})
    assert small["bucket_rows"] == s.guard.min_aggregate_n and len(small["bucket_mean"]) == 3 and "left out" in small["note"]  # 100 rows: 30 + 30 + 30, the last 10 dropped
    tiny = tb.call("series", {"signal": press, "row_start": 0, "row_end": 9})
    assert "bucket_mean" not in tiny and "too short" in tiny["note"] and len(tb.last_series["points"]) == 10


def test_alias_expansion_prefers_the_operator_display_name(finished_run):
    ws, s, df = _run_copy(finished_run, "labels")
    press, temp = _alias(ws, PRESS), _alias(ws, "TT310_ReactorTemp")
    signals = ws.read_json("signals", [])
    next(d for d in signals if d["id"] == press)["display_name"] = "Reactor pressure"
    ws.write_json("signals", signals)
    labels = agent_mod.alias_labels(agent_mod.Toolbox(ws, s))
    assert labels[press] == "Reactor pressure" and labels[temp] == "TT310_ReactorTemp"
    text = agent_mod.expand_aliases(f"{press} rose before {temp}; {press} stayed high. S999 and EV-000012 are untouched.", labels)
    assert text == f"{press} (Reactor pressure) rose before {temp} (TT310_ReactorTemp); {press} stayed high. S999 and EV-000012 are untouched."
    assert agent_mod.expand_aliases(text, labels) == text  # already expanded: not doubled
    assert agent_mod._collapse_aliases(text, labels).startswith(f"{press} rose before {temp};")
