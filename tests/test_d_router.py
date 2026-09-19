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


# ---------------------------------------------------------------------------------------------- hybrid: limited external calls
def _ext_ok(data=None, usage=None, sleep_s=0.0, seen=None):
    """A stand-in for AnthropicProvider.chat: no network, records what it was given."""
    import time as _time

    def fake(self, messages, schema=None, max_tokens=None, model=None):
        if seen is not None:
            seen.append({"messages": messages, "model": model, "max_tokens": max_tokens})
        if sleep_s:
            _time.sleep(sleep_s)
        if usage:
            self.last_usage = usage
        d = data if data is not None else {"verdict": "supported", "objections": [], "adjusted_confidence": 0.8, "reasoning": "ok"}
        return json.dumps(d), d, int(sleep_s * 1000) or 7

    return fake


def _boom(self, *a, **k):
    raise AssertionError("the external provider must not be called")


@pytest.mark.parametrize("how", ["env", "model_by_task", "settings", "patterns_emptied"])
def test_fable_and_mythos_models_are_refused(monkeypatch, ws, how):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    if how == "env":
        monkeypatch.setenv("TPM_EXTERNAL_MODEL", "claude-fable-5-1")
    s = _settings("hybrid")
    if how == "model_by_task":
        s.external_llm.model_by_task = {"critique": "claude-mythos-5-1"}
    elif how == "settings":
        s.external_llm.model = "Claude-FABLE-5"
    elif how == "patterns_emptied":  # the two families stay blocked even when the config lists are emptied
        s.external_llm.model, s.external_llm.blocked_model_patterns, s.external_llm.allowed_model_patterns = "claude-mythos-5-1", [], []
    monkeypatch.setattr(AnthropicProvider, "chat", _boom)
    monkeypatch.setattr(AnthropicProvider, "_client", _boom)
    _no_ollama(monkeypatch)
    assert s.external_llm.model_allowed(s.external_model_for("critique"))[0] is False
    res = router.complete("critique", {"diagnosis": {"id": "DIAG-000001", "summary": "x"}}, purpose="test", ws=ws, settings=s)
    assert res.ok is False and res.source == "template" and "blocked" in res.error
    recs = ledger.read(ws)
    assert recs[0].route == "external" and recs[0].guard_result == "unavailable" and recs[0].ok is False and "30 days" in recs[0].guard_reason
    assert recs[0].payload_preview == "" and recs[1].route == "local" and recs[1].guard_result == "fallback"
    info = router.available(s)
    assert info["external_model_blocked_reason"] and info["external_models_allowed"] == ["claude-sonnet-5", "claude-opus-5"]
    if how != "model_by_task":
        assert info["external"] is False and "blocked" in info["external_unavailable_reason"]
        assert router.external_ready("critique", ws, s)[0] is False
    else:
        assert info["external"] is True and router.external_ready("sensor_hypotheses", ws, s)[0] is True and router.external_ready("critique", ws, s)[0] is False


def test_provider_itself_refuses_a_blocked_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    monkeypatch.setattr(AnthropicProvider, "_client", _boom)
    p = AnthropicProvider(_settings("hybrid"))
    for model in ("claude-fable-5-1", "claude-mythos-5-1", "gpt-4o"):
        with pytest.raises(ProviderError):
            p.chat([{"role": "user", "content": "hi"}], model=model)
    assert p.cfg.model_allowed("claude-sonnet-5") == (True, "ok") and p.cfg.model_allowed("claude-opus-5")[0]


def test_eu_hosted_needs_a_non_anthropic_endpoint(monkeypatch, ws):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    monkeypatch.setattr(AnthropicProvider, "chat", _boom)
    _no_ollama(monkeypatch)
    for base_url in (None, "", "https://api.anthropic.com", "https://eu.api.anthropic.com/v1"):
        s = _settings("eu-hosted")
        s.external_llm.base_url = base_url
        assert "base_url" in (s.external_block_reason() or "")
        res = router.complete("critique", {"diagnosis": {"id": "DIAG-000001"}}, purpose="test", ws=ws, settings=s)
        assert res.source == "template"
        assert router.available(s)["external"] is False and "EU" in router.available(s)["external_unavailable_reason"]
    assert all(r.guard_result in ("unavailable", "fallback") for r in ledger.read(ws))
    s = _settings("eu-hosted")
    s.external_llm.base_url = "https://bedrock-mantle.eu-north-1.api.aws/anthropic"
    monkeypatch.setattr(AnthropicProvider, "chat", _ext_ok())
    res = router.complete("critique", {"diagnosis": {"id": "DIAG-000001", "summary": "x"}}, purpose="test", ws=ws, settings=s)
    assert res.ok and res.route == "external" and router.available(s)["external"] is True
    # hybrid is the first-party profile: no endpoint needed
    assert _settings("hybrid").external_block_reason() is None


def test_budget_exhaustion_falls_back_to_local(monkeypatch, ws):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    s = _settings("hybrid")
    s.external_llm.max_calls_per_run = 2
    calls = []
    monkeypatch.setattr(AnthropicProvider, "chat", _ext_ok(seen=calls, usage={"input_tokens": 100, "output_tokens": 40}))
    _no_ollama(monkeypatch)
    payload = {"diagnosis": {"id": "DIAG-000001", "summary": "x"}}
    routes = [router.complete("critique", payload, purpose=f"call {i}", ws=ws, settings=s).route for i in range(4)]
    assert routes == ["external", "external", "none", "none"] and len(calls) == 2
    recs = ledger.read(ws)
    budget = [r for r in recs if r.guard_result == "budget"]
    assert len(budget) == 2 and all(r.route == "external" and not r.ok and "max_calls_per_run" in r.guard_reason for r in budget)
    assert [r.guard_result for r in recs if r.route == "local"] == ["fallback", "fallback"]
    u = ledger.usage(ws, s)
    assert u["external_ok"] == 2 and u["budget_left_calls"] == 0 and u["budget_refused"] == 2
    assert router.external_ready("critique", ws, s) == (False, router._budget(ws, s)[1])
    # the output-token budget works the same way
    s2 = _settings("hybrid")
    s2.external_llm.max_output_tokens_per_run = 50
    assert router._budget(ws, s2)[0] is False and "output-token" in router._budget(ws, s2)[1]
    assert "budget" in ledger.data_flow_statement(ws, s)


def test_ledger_usage_tokens_latency_and_sanitizer(monkeypatch, ws):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    s = _settings("hybrid")
    monkeypatch.setattr(AnthropicProvider, "chat", _ext_ok(usage={"input_tokens": 1200, "output_tokens": 300}))
    monkeypatch.setattr(OllamaProvider, "is_available", lambda self: True)
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: ["gemma3:4b"])
    monkeypatch.setattr(OllamaProvider, "capabilities", lambda self, m: [])
    monkeypatch.setattr(OllamaProvider, "chat", lambda self, messages, schema=None, max_tokens=None, model=None, temperature=None: ('{"roles": []}', {"roles": []}, 3000))
    payload = {"diagnosis": {"id": "DIAG-000001", "summary": "press_r rose to 2715.4837", "observed": 2715.4837}, "evidence": [{"id": "EV-000001", "statement": "x", "n_samples": 3}]}
    res = router.complete("critique", payload, purpose="t", ws=ws, settings=s)
    router.complete("critique", payload, purpose="t", ws=ws, settings=s)
    router.complete("column_roles", {"signals": [{"id": "S01"}]}, purpose="t", ws=ws, settings=s)  # local by routing
    rec = [r for r in ledger.read(ws) if r.id == res.ledger_id][0]
    assert rec.input_tokens == 1200 and rec.output_tokens == 300
    assert rec.sanitizer["keys_dropped"] == 1 and rec.sanitizer["items_dropped"] == 1 and rec.sanitizer["names_aliased"] == 1 and rec.sanitizer["notes"]
    assert "2715.4837" not in rec.payload_preview and "press_r" not in rec.payload_preview and "S02 rose to 2720" in rec.payload_preview
    u = ledger.usage(ws, s)
    assert u["external_calls"] == 2 and u["external_ok"] == 2 and u["input_tokens"] == 2400 and u["output_tokens"] == 600 and u["blocked"] == 0
    assert u["budget_left_calls"] == s.external_llm.max_calls_per_run - 2 and u["avg_latency_ms_external"] == 7 and u["avg_latency_ms_local"] == 3000
    assert u["by_task"]["critique"]["external_ok"] == 2 and u["by_task"]["column_roles"]["local_ok"] == 1 and u["by_task"]["critique"]["output_tokens"] == 600
    from tpm import llm

    assert llm.usage(ws, s)["external_ok"] == 2


def test_complete_many_runs_external_jobs_concurrently(monkeypatch, ws):
    import time

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    s = _settings("hybrid")
    monkeypatch.setattr(AnthropicProvider, "chat", _ext_ok(sleep_s=0.5, usage={"input_tokens": 10, "output_tokens": 5}))
    _no_ollama(monkeypatch)
    jobs = [{"task": "critique", "payload": {"diagnosis": {"id": f"DIAG-{i:06d}", "summary": "x"}}, "purpose": f"job {i}"} for i in range(6)]
    jobs.append({"task": "column_roles", "payload": {"signals": [{"id": "S01"}]}, "purpose": "local job"})  # local route: sequential
    jobs.append({"purpose": "broken job"})
    t0 = time.time()
    out = router.complete_many(jobs, ws=ws, settings=s)
    took = time.time() - t0
    assert [r.route for r in out] == ["external"] * 6 + ["none", "none"] and all(r.ok for r in out[:6]) and not out[7].ok
    assert took < 1.6, f"6 external jobs of 0.5 s took {took:.2f} s: they did not run in parallel"
    recs = ledger.read(ws)
    ids = [r.id for r in recs]
    assert len(ids) == len(set(ids)) == 7 and sorted(r.purpose for r in recs if r.route == "external") == [f"job {i}" for i in range(6)]
    assert ws.log.verify_chain()["ok"] and len(ws.log.entries(action="egress")) == 7
    assert ledger.usage(ws, s)["output_tokens"] == 30
    # one worker: the same jobs take their turn
    t0 = time.time()
    router.complete_many(jobs[:3], ws=ws, settings=s, max_parallel=1)
    assert time.time() - t0 >= 1.4
    # a budget smaller than the batch is respected under concurrency (slots are booked before sending)
    s.external_llm.max_calls_per_run = 9 + 2
    out = router.complete_many(jobs[:6], ws=ws, settings=s)
    assert sum(1 for r in out if r.route == "external") == 2 and ledger.usage(ws, s)["external_ok"] == 11


def test_agent_chat_routes_sanitises_and_falls_back(monkeypatch, ws):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    s = _settings("hybrid")
    assert s.route_for("why_chat") == "external" and _settings("no-egress").route_for("why_chat") == "local"
    seen = []
    step = {"thought": "done", "action": "final", "answer": "S02 rose first.", "citations": ["EV-000001"]}
    monkeypatch.setattr(AnthropicProvider, "chat", _ext_ok(data=step, seen=seen, usage={"input_tokens": 50, "output_tokens": 20}))
    local_calls = []

    def fake_local(messages, **kw):
        local_calls.append(messages)
        return LLMResult(text="{}", data={"action": "final"}, source="llm-local:x", model="x", route="local", ok=True)

    monkeypatch.setattr(router, "local_chat", fake_local)
    msgs = [{"role": "system", "content": "You answer about signals. Dataset columns: S01, S02."},
            {"role": "user", "content": "Question: why did press_r reach 2715.4837 on 2026-01-03T04:12:30?"}]
    parts = [{"context": {"flag": {"id": "FLAG-000001", "statement": "press_r shifted", "observed": 2715.4837}}}]
    res = router.agent_chat(msgs, task="why_chat", purpose="chat", ws=ws, settings=s, schema=prompts.AGENT_STEP_SCHEMA, max_tokens=900, payload_parts=parts)
    assert res.ok and res.route == "external" and res.source == "llm-external:claude-sonnet-5" and res.data["action"] == "final"
    sent = seen[0]["messages"][1]["content"]
    assert sent == "Question: why did S02 reach 2720 on [time]?" and seen[0]["max_tokens"] >= 900 and not local_calls
    rec = ledger.read(ws)[-1]
    assert rec.guard_result == "allowed" and rec.output_tokens == 20 and "press_r" not in rec.payload_preview and rec.sanitizer["names_aliased"] >= 1
    # a structured part the guard refuses -> the call runs on the local model with the original messages
    res2 = router.agent_chat(msgs, task="why_chat", purpose="chat", ws=ws, settings=s, payload_parts=[{"tool_result": [float(i) for i in range(300)]}])
    assert res2.route == "local" and len(local_calls) == 1 and local_calls[0] == msgs and len(seen) == 1
    assert [r.guard_result for r in ledger.read(ws)][-1] == "blocked"
    # local profile: identical to local_chat
    router.agent_chat(msgs, task="why_chat", purpose="chat", ws=ws, settings=_settings("no-egress"))
    assert len(local_calls) == 2 and len(seen) == 1

    # an external failure mid-turn is reported, not silently mixed with a local call
    def failing(self, messages, schema=None, max_tokens=None, model=None):
        raise ProviderError("anthropic APIConnectionError: down")

    monkeypatch.setattr(AnthropicProvider, "chat", failing)
    res3 = router.agent_chat(msgs, task="why_chat", purpose="chat", ws=ws, settings=s, payload_parts=parts)
    assert res3.ok is False and res3.route == "external" and "down" in res3.error and len(local_calls) == 2


def test_system_override_is_sanitised_for_the_external_route(monkeypatch, ws):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    seen = []
    monkeypatch.setattr(AnthropicProvider, "chat", _ext_ok(seen=seen))
    router.complete("critique", {"diagnosis": {"id": "DIAG-000001", "summary": "x"}}, purpose="t", ws=ws, settings=_settings("hybrid"), system="Focus on press_r since 2026-01-03T04:12:30.")
    assert seen[0]["messages"][0]["content"] == "Focus on S02 since [time]."


def test_anthropic_request_shape(monkeypatch):
    """Pooled client, merged turns, effort, usage; the SDK client is a stub (no network)."""
    from types import SimpleNamespace

    from tpm.llm import providers

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    sent = []

    class FakeMessages:
        def create(self, **kwargs):
            sent.append(kwargs)
            block = SimpleNamespace(type="tool_use", name="emit_result", input={"answer": "ok"})
            return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=11, output_tokens=5), stop_reason="tool_use")

    built = []

    class FakeAnthropic:
        def __init__(self, **kwargs):
            built.append(kwargs)
            self.messages = FakeMessages()

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(providers, "_EXT_CLIENTS", {})
    s = _settings("hybrid")
    p = AnthropicProvider(s)
    msgs = [{"role": "system", "content": "sys"}, {"role": "assistant", "content": "stale"}, {"role": "user", "content": "a"}, {"role": "user", "content": "b"}, {"role": "assistant", "content": "c"}, {"role": "user", "content": "d"}]
    text, parsed, _ = p.chat(msgs, schema={"type": "object", "properties": {"answer": {"type": "string"}}}, max_tokens=500)
    assert parsed == {"answer": "ok"} and p.last_usage == {"input_tokens": 11, "output_tokens": 5}
    req = sent[0]
    assert req["model"] == "claude-sonnet-5" and req["system"] == "sys" and req["output_config"] == {"effort": "low"}
    assert req["messages"] == [{"role": "user", "content": "a\n\nb"}, {"role": "assistant", "content": "c"}, {"role": "user", "content": "d"}]
    assert "temperature" not in req and "thinking" not in req and req["tool_choice"] == {"type": "tool", "name": "emit_result"}
    AnthropicProvider(s).chat([{"role": "user", "content": "again"}])
    assert len(built) == 1 and built[0]["timeout"] == 60.0, "one pooled client per (endpoint, key, timeout)"
    # an EU (Bedrock-hosted) endpoint: forced tool call needs thinking off
    s.external_llm.base_url = "https://bedrock-mantle.eu-north-1.api.aws/anthropic"
    AnthropicProvider(s).chat([{"role": "user", "content": "x"}], schema={"type": "object"})
    assert len(built) == 2 and sent[-1]["thinking"] == {"type": "disabled"}
