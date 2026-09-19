"""eu-hosted profile: its own EU endpoint (Mistral Large 3 on the hackathon's Verda containers in Finland, OpenAI-compatible),
the list of EU-hosted services, and the OpenAI-compatible provider. The HTTP server is an httpx.MockTransport: nothing
leaves the machine."""
from __future__ import annotations

import json

import httpx
import pytest

from tpm.config import load_settings
from tpm.llm import ledger, router
from tpm.llm.providers import (AnthropicProvider, OllamaProvider, OpenAICompatProvider, ProviderError, TransientError,
                               external_provider)
from tpm.llm.sandbox import make_demo_workspace

VERDA = "https://containers.datacrunch.io/data-sovereignty-mistral-large-3/v1"
MISTRAL = "mistralai/Mistral-Large-3-675B-Instruct-2512-NVFP4"


@pytest.fixture
def ws(tmp_path):
    return make_demo_workspace(load_settings(profile="no-egress"), root=tmp_path / "ws", run_id="eu_demo", n_groups=3, n_samples=100)


def _server(monkeypatch, replies: list, seen: list):
    """OpenAICompatProvider talks to a fake server: each request is recorded, `replies` are served in order
    (an httpx.Response, or a dict sent as a 200 JSON body)."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url), "headers": dict(request.headers), "body": json.loads(request.content or b"{}")})
        r = replies.pop(0) if len(replies) > 1 else replies[0]
        return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(OpenAICompatProvider, "_client", lambda self: client)
    monkeypatch.setattr("tpm.llm.providers.time.sleep", lambda s: None)


def _answer(content: str, prompt_tokens: int = 120, completion_tokens: int = 30, finish: str = "stop") -> dict:
    return {"id": "x", "object": "chat.completion", "model": MISTRAL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


# ---------------------------------------------------------------------------------------------- settings


def test_eu_hosted_brings_its_own_endpoint_model_and_key(monkeypatch):
    hybrid = load_settings(profile="hybrid")
    assert (hybrid.external_llm.provider, hybrid.external_llm.model, hybrid.external_llm.base_url) == ("anthropic", "claude-sonnet-5", None)
    eu = hybrid.with_profile("eu-hosted")
    x = eu.external_llm
    assert (x.provider, x.base_url, x.model, x.api_key_env) == ("openai-compatible", VERDA, MISTRAL, "TPM_EU_API_KEY")
    assert x.location == "Finland (EU)" and "Verda" in x.operator and x.provider_label == "openai-compatible @ containers.datacrunch.io"
    assert eu.external_block_reason() is None, "the EU endpoint is configured; only the key is missing"
    assert eu.base_external_llm.model == "claude-sonnet-5", "the top-level block (hybrid's) is untouched"
    back = eu.with_profile("hybrid")
    assert (back.external_llm.provider, back.external_llm.model, back.external_llm.base_url) == ("anthropic", "claude-sonnet-5", None)
    assert isinstance(external_provider(eu), OpenAICompatProvider) and isinstance(external_provider(back), AnthropicProvider)


def test_eu_variables_only_change_the_eu_profile(monkeypatch):
    monkeypatch.setenv("TPM_EU_BASE_URL", "https://api.eu.mistral.ai/v1")
    monkeypatch.setenv("TPM_EU_MODEL", "mistral-medium-latest")
    assert load_settings(profile="eu-hosted").external_llm.base_url == "https://api.eu.mistral.ai/v1"
    assert load_settings(profile="eu-hosted").external_llm.model == "mistral-medium-latest"
    assert load_settings(profile="eu-hosted").external_block_reason() is None
    h = load_settings(profile="hybrid").external_llm
    assert h.base_url is None and h.model == "claude-sonnet-5"


@pytest.mark.parametrize("url,model,why", [
    (None, MISTRAL, "base_url"),
    ("https://api.anthropic.com", MISTRAL, "no EU-only processing"),
    ("https://api.mistral.ai/v1", MISTRAL, "not on the list of EU-hosted services"),        # Mistral's global endpoint
    ("https://bedrock-mantle.us-east-1.api.aws/anthropic", MISTRAL, "not on the list"),
    ("https://containers.datacrunch.io.evil.example/v1", MISTRAL, "not on the list"),        # look-alike host
    (VERDA, "global.mistral-large", "outside the EU"),
    (VERDA, "claude-sonnet-5", "not in the allowed families"),
    (VERDA, "mistral-fable-edition", "30 days"),
])
def test_eu_hosted_refuses_what_is_not_eu_hosted(url, model, why):
    s = load_settings(profile="eu-hosted")
    s.external_llm.base_url, s.external_llm.model = url, model
    assert why in (s.external_block_reason() or "")


def test_unknown_provider_is_refused():
    s = load_settings(profile="eu-hosted")
    s.external_llm.provider = "carrier-pigeon"
    assert "unknown" in s.external_block_reason()


# ---------------------------------------------------------------------------------------------- provider


def test_request_carries_only_the_bearer_key_and_the_messages(monkeypatch):
    monkeypatch.setenv("TPM_EU_API_KEY", "eu-test-key")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_should_never_leave")
    seen: list = []
    _server(monkeypatch, [_answer('{"summary": "S01 rose first.", "confidence": 0.6}')], seen)
    p = OpenAICompatProvider(load_settings(profile="eu-hosted"))
    schema = {"type": "object", "properties": {"summary": {"type": "string"}, "confidence": {"type": "number"}}, "required": ["summary"]}
    msgs = [{"role": "system", "content": "be brief"}, {"role": "assistant", "content": "dropped: a leading assistant turn"},
            {"role": "user", "content": "part 1"}, {"role": "user", "content": "part 2"}]
    text, parsed, latency = p.chat(msgs, schema=schema, max_tokens=500)
    assert parsed == {"summary": "S01 rose first.", "confidence": 0.6} and latency >= 0
    assert p.last_usage == {"input_tokens": 120, "output_tokens": 30}
    req = seen[0]
    assert req["url"] == VERDA + "/chat/completions"
    assert req["headers"]["authorization"] == "Bearer eu-test-key"
    assert not [h for h in req["headers"] if "anthropic" in h or "workspace" in h or "organization" in h]
    b = req["body"]
    assert b["model"] == MISTRAL and b["max_tokens"] == 500 and b["temperature"] == 0.2
    assert b["messages"] == [{"role": "system", "content": "be brief"}, {"role": "user", "content": "part 1\n\npart 2"}]
    assert b["response_format"]["type"] == "json_schema" and b["response_format"]["json_schema"]["schema"] == schema


def test_a_refused_response_format_is_retried_without_it(monkeypatch):
    monkeypatch.setenv("TPM_EU_API_KEY", "k")
    seen: list = []
    _server(monkeypatch, [httpx.Response(400, json={"message": "response_format json_schema is not supported"}), _answer('```json\n["S01", "S02"]\n```')], seen)
    p = OpenAICompatProvider(load_settings(profile="eu-hosted"))
    text, parsed, _ = p.chat([{"role": "user", "content": "rank"}], schema={"type": "array", "items": {"type": "string"}})
    assert parsed == ["S01", "S02"]
    assert "response_format" in seen[0]["body"] and "result" in seen[0]["body"]["response_format"]["json_schema"]["schema"]["properties"]
    assert "response_format" not in seen[1]["body"]


def test_wrapped_array_schema_is_unwrapped(monkeypatch):
    monkeypatch.setenv("TPM_EU_API_KEY", "k")
    _server(monkeypatch, [_answer('{"result": ["S03"]}')], [])
    _, parsed, _ = OpenAICompatProvider(load_settings(profile="eu-hosted")).chat([{"role": "user", "content": "x"}], schema={"type": "array"})
    assert parsed == ["S03"]


@pytest.mark.parametrize("response,exc,words", [
    (httpx.Response(401, json={"error": "bad key"}), ProviderError, "TPM_EU_API_KEY"),
    (httpx.Response(503, text="no replica ready"), TransientError, "503"),
    (httpx.Response(404, text="no such deployment"), ProviderError, "404"),
    (_answer("", finish="content_filter"), ProviderError, "content filter"),
])
def test_errors_are_provider_errors(monkeypatch, response, exc, words):
    monkeypatch.setenv("TPM_EU_API_KEY", "k")
    seen: list = []
    _server(monkeypatch, [response], seen)
    with pytest.raises(exc) as e:
        OpenAICompatProvider(load_settings(profile="eu-hosted")).chat([{"role": "user", "content": "x"}])
    assert words in str(e.value)
    assert len(seen) == (2 if isinstance(response, httpx.Response) and response.status_code == 503 else 1), "only a busy server is retried, once"


def test_no_key_or_blocked_model_never_calls(monkeypatch):
    seen: list = []
    _server(monkeypatch, [_answer("x")], seen)
    p = OpenAICompatProvider(load_settings(profile="eu-hosted"))
    assert p.is_available() is False
    with pytest.raises(ProviderError, match="TPM_EU_API_KEY"):
        p.chat([{"role": "user", "content": "x"}])
    monkeypatch.setenv("TPM_EU_API_KEY", "k")
    with pytest.raises(ProviderError, match="30 days"):
        p.chat([{"role": "user", "content": "x"}], model="mistral-fable")
    assert not seen


def test_anthropic_workspace_header_only_goes_to_anthropic(monkeypatch):
    import anthropic

    built: list = []

    class FakeClient:
        def __init__(self, **kw):
            built.append(kw)

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-k")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_1")
    s = load_settings(profile="hybrid")
    AnthropicProvider(s)._client()
    s.external_llm.base_url, s.external_llm.timeout_s = "https://bedrock-mantle.eu-north-1.api.aws/anthropic", 61
    AnthropicProvider(s)._client()
    assert built[0]["default_headers"] == {"anthropic-workspace-id": "wrkspc_1"}
    assert "default_headers" not in built[1]


# ---------------------------------------------------------------------------------------------- through the router


def test_router_sends_eu_hosted_calls_to_the_eu_endpoint_and_records_where(monkeypatch, ws):
    monkeypatch.setenv("TPM_EU_API_KEY", "k")
    monkeypatch.setattr(OllamaProvider, "is_available", lambda self: False)
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: [])
    seen: list = []
    reply = {"verdict": "supported", "objections": [], "alternative_explanations": [], "adjusted_confidence": 0.7, "reasoning": "S01 leads."}
    _server(monkeypatch, [_answer(json.dumps(reply))], seen)
    s = load_settings(profile="eu-hosted")
    assert router.available(s)["external"] is True and router.available(s)["external_location"] == "Finland (EU)"
    res = router.complete("critique", {"diagnosis": {"id": "DIAG-000001", "summary": "S01 rose first", "first": 12.3456789}}, purpose="test", ws=ws, settings=s)
    assert res.ok and res.route == "external" and res.model == MISTRAL and res.data["verdict"] == "supported", res.error
    assert len(seen) == 1, "a valid answer needs no repair round"
    rec = [r for r in ledger.read(ws) if r.route == "external"][-1]
    assert rec.provider == "openai-compatible @ containers.datacrunch.io" and rec.model == MISTRAL and rec.guard_result == "allowed"
    assert rec.input_tokens == 120 and rec.output_tokens == 30
    sent = json.dumps(seen[0]["body"]["messages"])
    assert "12.3456789" not in sent and "12.3" not in sent, "the guard ran before the call: the single reading never left"
    statement = ledger.data_flow_statement(ws, s, extras=False)
    assert "Finland (EU)" in statement and "containers.datacrunch.io" in statement and MISTRAL in statement
