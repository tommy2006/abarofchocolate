"""Router / ledger / providers tests (agent D). All model calls are monkeypatched: no Ollama, no network."""
from __future__ import annotations

import json

import pytest

from tpm.contracts import LLMResult
from tpm.llm import ledger, prompts, router
from tpm.llm.providers import AnthropicProvider, OllamaProvider, ProviderError, extract_json, validate_schema
from tpm.llm.sandbox import catalog_payload, make_demo_workspace


@pytest.fixture
def ws(tmp_path):
    from tpm.config import load_settings

    s = load_settings(profile="no-egress")
    return make_demo_workspace(s, root=tmp_path / "ws", run_id="router_demo", n_groups=3, n_samples=100)


def _settings(profile: str):
    from tpm.config import load_settings

    return load_settings(profile=profile)


def _no_ollama(monkeypatch):
    monkeypatch.setattr(OllamaProvider, "is_available", lambda self: False)
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: [])


def test_no_egress_profile_never_calls_anthropic(monkeypatch, ws):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    s = _settings("no-egress")
    assert s.external_llm.api_key == "sk-fake-key"

    def boom(self, *a, **k):
        raise AssertionError("external provider must never be called in no-egress")

    monkeypatch.setattr(AnthropicProvider, "chat", boom)
    monkeypatch.setattr(AnthropicProvider, "_client", boom)
    _no_ollama(monkeypatch)
    for task in prompts.TASKS:
        assert s.route_for(task) == "local"
        res = router.complete(task, catalog_payload(ws), purpose="test", ws=ws, settings=s)
        assert res.source == "template" and res.ok is False
    recs = ledger.read(ws)
    assert recs and all(r.route == "local" for r in recs)


def test_hybrid_external_blocked_then_local_then_template(monkeypatch, ws):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    s = _settings("hybrid")
    assert s.route_for("sensor_hypotheses") == "external"
    calls = []

    def fake_ext_chat(self, messages, schema=None, max_tokens=None, model=None):
        calls.append(messages)
        raise AssertionError("must not be called: guard should block")

    monkeypatch.setattr(AnthropicProvider, "chat", fake_ext_chat)
    _no_ollama(monkeypatch)
    raw = {"signals": [{"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 5.0} for _ in range(5)]}
    res = router.complete("sensor_hypotheses", raw, purpose="test", ws=ws, settings=s)
    assert res.ok is False and res.source == "template"
    assert not calls
    recs = ledger.read(ws)
    assert recs[0].route == "external" and recs[0].guard_result == "blocked" and "row-like" in recs[0].guard_reason
    assert recs[1].route == "local" and recs[1].guard_result == "fallback" and recs[1].ok is False
    assert ws.log.entries(action="egress")


def test_hybrid_external_success_records_ledger(monkeypatch, ws):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    s = _settings("hybrid")
    seen = {}

    def fake_ext_chat(self, messages, schema=None, max_tokens=None, model=None):
        seen["messages"] = messages
        seen["schema"] = schema
        data = {"hypotheses": [{"signal": "S01", "instrument_hypothesis": "flow", "instrument_confidence": 0.4, "status": "assumed", "reasoning": "noisy continuous", "evidence_ids": ["EV-000001"]}], "process_hypothesis": "unknown", "process_confidence": 0.2, "uncertainty": ["no units"]}
        return json.dumps(data), data, 12

    monkeypatch.setattr(AnthropicProvider, "chat", fake_ext_chat)
    payload = catalog_payload(ws)
    res = router.complete("sensor_hypotheses", payload, purpose="test", ws=ws, settings=s, language="sv")
    assert res.ok and res.route == "external" and res.source.startswith("llm-external:")
    assert res.data["hypotheses"][0]["signal"] == "S01"
    assert seen["schema"] == prompts.schema_for("sensor_hypotheses")
    assert "SVENSKA" in seen["messages"][0]["content"]
    rec = [r for r in ledger.read(ws) if r.id == res.ledger_id][0]
    assert rec.route == "external" and rec.guard_result == "allowed" and rec.payload_preview and rec.payload_hash and rec.response_hash
    assert rec.payload_bytes > 0 and "signal_catalog" in rec.artifact_types
    summ = ledger.summary(ws)
    assert summ["n_external_sent"] == 1
    stmt = ledger.data_flow_statement(ws, s)
    assert "sent to the external model" in stmt


def test_local_fallback_to_template_when_ollama_down(monkeypatch, ws):
    s = _settings("no-egress")
    _no_ollama(monkeypatch)
    res = router.complete("diagnosis_narrative", {"diagnosis": {"id": "DIAG-000001", "summary": "x"}}, purpose="test", ws=ws, settings=s)
    assert res.ok is False and res.source == "template" and res.route == "none"
    assert "ollama" in (res.error or "").lower()
    recs = ledger.read(ws)
    assert len(recs) == 1 and recs[0].route == "local" and recs[0].ok is False and recs[0].task == "diagnosis_narrative"
    entries = ws.log.entries(action="egress")
    assert entries and entries[-1].actor.startswith("llm:local:")
    assert ws.log.verify_chain()["ok"]
    assert router.available(s)["local"] is False and router.available(s)["mode"] == "template"


def test_local_success_with_fake_ollama(monkeypatch, ws):
    s = _settings("no-egress")
    monkeypatch.setattr(OllamaProvider, "is_available", lambda self: True)
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: ["gemma3:4b"])
    monkeypatch.setattr(OllamaProvider, "capabilities", lambda self, m: [])
    n = {"calls": 0}

    def fake_chat(self, messages, schema=None, max_tokens=None, model=None, temperature=None):
        n["calls"] += 1
        if n["calls"] == 1:
            return "Sure! ```json\n{\"verdict\": \"weakened\", \"objections\": \"not a list\"}\n```", None, 5
        data = {"verdict": "weakened", "objections": [{"text": "single event", "evidence_ids": ["EV-000001"]}], "adjusted_confidence": 0.5, "reasoning": "ok"}
        return json.dumps(data), data, 7

    monkeypatch.setattr(OllamaProvider, "chat", fake_chat)
    res = router.complete("critique", {"diagnosis": {"id": "DIAG-000001"}}, purpose="test", ws=ws, settings=s)
    assert res.ok and res.source == "llm-local:gemma3:4b" and res.model == "gemma3:4b"
    assert n["calls"] == 2  # one repair round
    assert res.data["verdict"] == "weakened"
    assert ledger.read(ws)[-1].guard_result == "n/a"


def test_entry_point_never_raises(monkeypatch, ws):
    from tpm import llm

    def explode(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(router, "complete", explode)
    res = llm.complete("critique", {}, purpose="x", ws=ws)
    assert isinstance(res, LLMResult) and res.ok is False and "boom" in res.error


def test_in_memory_ledger_without_ws(monkeypatch):
    s = _settings("no-egress")
    _no_ollama(monkeypatch)
    ledger.clear_memory()
    res = router.complete("critique", {"diagnosis": {}}, purpose="x", ws=None, settings=s)
    assert res.ok is False
    recs = ledger.memory_records()
    assert recs and recs[-1].task == "critique"


def test_json_helpers():
    assert extract_json("text before {\"a\": 1} after") == {"a": 1}
    assert extract_json("<think>hmm</think>```json\n[1,2]\n```") == [1, 2]
    assert extract_json("no json") is None
    sch = prompts.schema_for("critique")
    assert validate_schema({"verdict": "supported", "objections": [], "adjusted_confidence": 0.9, "reasoning": "x"}, sch) == []
    errs = validate_schema({"verdict": "maybe", "objections": [], "adjusted_confidence": 2}, sch)
    assert any("enum" in e for e in errs) and any("maximum" in e for e in errs) and any("required" in e for e in errs)


def test_prompts_render_all_tasks_and_languages():
    payload = {"signals": [{"id": "S01"}], "rule_text": "S01 < 3", "question": "why?", "history": [{"role": "user", "content": "hi"}]}
    for task in prompts.TASKS:
        for lang in ("en", "fi", "sv"):
            sys_t, user_t = prompts.render(task, payload, language=lang)
            assert "cite" in sys_t.lower() or "IDs" in sys_t
            assert "S01" in user_t
            if lang == "fi":
                assert "SUOMEKSI" in sys_t
            if lang == "sv":
                assert "SVENSKA" in sys_t
            if prompts.schema_for(task):
                assert "Schema:" in sys_t
    sys_t, _ = prompts.render("unknown_task", {"evidence": []})
    assert "unknown_task" in sys_t


def test_pick_model_prefers_configured_then_fallbacks(monkeypatch):
    s = _settings("no-egress")
    s.local_llm.model = "gemma4:e4b-it-qat"
    s.local_llm.fallback_models = ["qwen3:8b", "llama3:8b", "gemma3:4b"]
    p = OllamaProvider(s)
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: ["llama3:8b", "gemma3:4b"])
    assert p.pick_model() == "llama3:8b"
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: ["gemma4:e4b-it-qat", "gemma3:4b"])
    assert p.pick_model() == "gemma4:e4b-it-qat"
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: ["other:latest"])
    assert p.pick_model() is None
    assert "ollama pull gemma4:e4b-it-qat" in p.pull_commands()
    assert "ollama pull nomic-embed-text" in p.pull_commands()


def test_anthropic_provider_without_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = _settings("hybrid")
    p = AnthropicProvider(s)
    assert not p.is_available()
    with pytest.raises(ProviderError):
        p.chat([{"role": "user", "content": "hi"}])
