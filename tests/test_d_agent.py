"""Local tool-agent + LocalIndex tests (agent D). Runs without Ollama; the model path is monkeypatched."""
from __future__ import annotations

import json

import pytest

from tpm.llm import agent as agent_mod
from tpm.llm.embeddings import LocalIndex
from tpm.llm.providers import OllamaProvider
from tpm.llm.sandbox import make_demo_workspace


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    from tpm.config import load_settings

    s = load_settings(profile="no-egress")
    ws = make_demo_workspace(s, root=tmp_path_factory.mktemp("ws"), run_id="agent_demo", n_groups=4, n_samples=120)
    return ws, s


def _no_ollama(monkeypatch):
    monkeypatch.setattr(OllamaProvider, "is_available", lambda self: False)
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: [])


def test_toolbox_sql_is_read_only_and_limited(demo):
    ws, s = demo
    tb = agent_mod.Toolbox(ws, s)
    out = tb.call("sql", {"query": "SELECT __group__, count(*) AS n FROM dataset GROUP BY 1 ORDER BY 1"})
    assert out["columns"] == ["__group__", "n"] and out["n_rows"] == 4
    big = tb.call("sql", {"query": "SELECT * FROM dataset"})
    assert big["n_rows"] <= agent_mod.SQL_LIMIT and "LIMIT" in big["sql"]
    for bad in ("DELETE FROM dataset", "SELECT 1; DROP TABLE x", "COPY dataset TO 'x.csv'", "SELECT * FROM read_csv('secret.csv')", "CREATE TABLE t AS SELECT 1", "SET memory_limit='1GB'"):
        assert "error" in tb.call("sql", {"query": bad}), bad
    assert "error" in tb.call("nonexistent", {})


def test_toolbox_stats_series_objects(demo):
    ws, s = demo
    tb = agent_mod.Toolbox(ws, s)
    st = tb.call("stats", {"signal": "S02", "group_id": "1"})
    assert st["n"] == 120 and st["column"] == "press_r" and st["q05"] <= st["median"] <= st["q95"]
    ser = tb.call("series", {"signal": "S02", "row_start": 0, "row_end": 479, "max_points": 40})
    assert ser["n_raw"] == 480 and ser["bucket"] == 12 and len(tb.last_series["points"]) == 40 and ser["local_only"]
    assert tb.call("get_flag", {"id": "FLAG-000001"})["evidence"]
    assert tb.call("get_diagnosis", {"id": "DIAG-000001"})["steps"]
    assert "error" in tb.call("get_flag", {"id": "FLAG-999999"})
    assert tb.call("list_checks", {"batch_id": "B0001"})["n"] == 1
    d = tb.call("describe_signal", {"id": "S01"})
    assert d["id"] == "S01" and d["evidence"] and "fingerprint" in d
    assert "error" in tb.call("assessor_evaluate", {"action_text": "drop S05"}) or "result" in tb.call("assessor_evaluate", {"action_text": "drop S05"})


def test_agent_answers_deterministically_without_model(demo, monkeypatch):
    ws, s = demo
    _no_ollama(monkeypatch)
    out = agent_mod.chat(ws, s, "Why was FLAG-000001 raised?", context={"flag_id": "FLAG-000001"}, actor="tester")
    assert out["source"] == "template"
    assert "FLAG-000001" in out["answer"] and "DIAG-000001" in out["answer"]
    assert out["citations"] and any(c.startswith("EV-") for c in out["citations"])
    assert all(c in out["answer"] for c in out["citations"] if c.startswith("EV-"))
    assert out["suggested_followups"]
    assert out["series"] and out["series"]["points"] and out["series"]["local_only"]
    turns = ws.read_jsonl("chat")
    assert turns[-1]["role"] == "assistant" and turns[-2]["role"] == "user" and turns[-2]["actor"] == "human:tester(operator)"
    assert ws.log.entries(action="chat")
    # ids in the message are picked up without a context dict; other languages work
    out2 = agent_mod.chat(ws, s, "Kerro signaalista S02", language="fi")
    assert out2["source"] == "template" and "S02" in out2["answer"] and "Signaali" in out2["answer"]
    out3 = agent_mod.chat(ws, s, "What is going on?", language="sv")
    assert out3["answer"] and out3["suggested_followups"]


def test_agent_json_action_loop_with_fake_model(demo, monkeypatch):
    ws, s = demo
    monkeypatch.setattr(OllamaProvider, "is_available", lambda self: True)
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: ["gemma3:4b"])
    monkeypatch.setattr(OllamaProvider, "capabilities", lambda self, m: [])
    script = [
        {"thought": "look at the flag", "action": "get_flag", "args": {"id": "FLAG-000001"}},
        {"thought": "stats of the top signal", "action": "stats", "args": {"signal": "S01", "group_id": "2"}},
        {"thought": "repeat", "action": "stats", "args": {"signal": "S01", "group_id": "2"}},
        {"thought": "done", "action": "final", "answer": "S01 contributed most (52%) [EV-000026]; its mean in group 2 differs from the dataset.", "citations": ["EV-000026", "EV-BOGUS"], "confidence": 0.7, "suggested_followups": ["Was batch B0001 trusted?"]},
    ]
    seen_msgs = []

    def fake_chat(self, messages, schema=None, max_tokens=None, model=None, temperature=None):
        seen_msgs.append(list(messages))  # copy: the agent mutates its message list
        step = script[min(len(seen_msgs) - 1, len(script) - 1)]
        return json.dumps(step), step, 3

    monkeypatch.setattr(OllamaProvider, "chat", fake_chat)
    out = agent_mod.chat(ws, s, "Which signal contributed most to FLAG-000001?", context={"flag_id": "FLAG-000001"}, max_steps=5)
    assert out["source"] == "llm-local:gemma3:4b"
    assert out["citations"] == ["EV-000026"]  # bogus id filtered out
    assert [t["tool"] for t in out["tool_trace"]] == ["get_flag", "stats", "stats"]
    assert out["tool_trace"][1]["ok"] and not out["tool_trace"][2]["ok"]  # repeated identical call rejected
    assert "Tool result for get_flag" in seen_msgs[1][-1]["content"]
    assert out["suggested_followups"] == ["Was batch B0001 trusted?"]
    assert ws.log.entries(action="tool_call")
    from tpm.llm import ledger

    assert any(r.task == "why_chat" and r.route == "local" for r in ledger.read(ws))


def test_agent_step_cap_forces_final(demo, monkeypatch):
    ws, s = demo
    monkeypatch.setattr(OllamaProvider, "is_available", lambda self: True)
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: ["gemma3:4b"])
    monkeypatch.setattr(OllamaProvider, "capabilities", lambda self, m: [])
    n = {"i": 0}

    def fake_chat(self, messages, schema=None, max_tokens=None, model=None, temperature=None):
        n["i"] += 1
        if "No tool calls left" in messages[-1]["content"]:
            step = {"thought": "ok", "action": "final", "answer": "Best effort answer.", "citations": []}
        else:
            step = {"thought": "more", "action": "get_evidence", "args": {"id": f"EV-{n['i']:06d}"}}
        return json.dumps(step), step, 1

    monkeypatch.setattr(OllamaProvider, "chat", fake_chat)
    out = agent_mod.chat(ws, s, "tell me everything", max_steps=2)
    assert len([t for t in out["tool_trace"] if t.get("tool")]) == 2
    assert out["answer"] == "Best effort answer." and out["source"].startswith("llm-local")


def test_local_index_tfidf_fallback(demo, monkeypatch):
    ws, s = demo
    _no_ollama(monkeypatch)
    idx = LocalIndex(ws, s).build()
    assert idx.method == "tfidf" and idx.info()["n_items"] > 10
    hits = idx.search("signals correlate at lag 0", k=3)
    assert hits and hits[0]["type"] == "evidence" and "correlate" in hits[0]["text"]
    hits2 = idx.search("egress guard external model", k=3, types=["doc"])
    assert hits2 and all(h["type"] == "doc" for h in hits2)
    assert (ws.dir / "index" / "items.jsonl").exists() and (ws.dir / "index" / "meta.json").exists()
    idx2 = LocalIndex(ws, s).build()  # loads from disk, same signature
    assert idx2.info()["n_items"] == idx.info()["n_items"]


def test_public_chat_wrapper_never_raises(demo, monkeypatch):
    from tpm import llm

    ws, s = demo
    _no_ollama(monkeypatch)
    out = llm.chat(ws, s, "Why?", context={"diagnosis_id": "DIAG-000001"})
    assert out["answer"] and out["source"] == "template" and out["citations"]
    monkeypatch.setattr(agent_mod, "chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out2 = llm.chat(ws, s, "Why?")
    assert "boom" in out2.get("error", "") and out2["source"] == "template"


def test_run_summary_counts_what_the_pages_count(demo):
    """The chat used to deny shares the pages show ("73 % of the findings are process changes") because it could look
    at single findings but never count them. run_summary gives it the run's own totals, and they must match the
    artifacts the pages read."""
    ws, s = demo
    tb = agent_mod.Toolbox(ws, s)
    out = tb.run_summary()
    diags = ws.diagnoses()
    if diags:
        by = out["diagnoses"]["by_cause"]
        assert out["diagnoses"]["total"] == len(diags)
        for cause in {d.cause_class for d in diags}:
            n = sum(1 for d in diags if d.cause_class == cause)
            assert by[cause]["n"] == n and abs(by[cause]["share"] - n / len(diags)) < 1e-3, cause
        assert abs(sum(v["share"] for v in by.values()) - 1) < 0.01
    assert out["flags"]["total"] == len(ws.flags())
    assert out["checks"]["total"] == len(ws.checks())
    # the tool is offered to both models; the totals are also in the system prompt, so an answer cannot deny them
    assert "run_summary" in [t["name"] for t in agent_mod.tool_specs(s)]
    assert "run_summary" in [t["name"] for t in agent_mod.tool_specs(s, external=True)]
    facts = agent_mod._run_facts(tb)
    assert str(out["flags"]["total"]) in facts and (not diags or str(out["diagnoses"]["total"]) in facts)


def test_run_summary_leaves_no_reading_in_what_an_external_model_would_see(demo):
    """Counts and shares only: the guard of the hybrid profile passes them, and no raw reading rides along."""
    from tpm.config import load_settings
    from tpm.llm import guard as guard_mod

    ws, _ = demo
    hybrid = load_settings(profile="hybrid")
    out = agent_mod.Toolbox(ws, hybrid, external=True).run_summary()
    text = json.dumps(out, default=str)
    assert "row_start" not in text and "readings" not in text
    g = guard_mod.check({"tool_result": out}, hybrid, ws=ws)
    assert g.allowed, g.reason
    kept = g.sanitized_payload["tool_result"]
    assert kept.get("flags", {}).get("total") == out["flags"]["total"]
