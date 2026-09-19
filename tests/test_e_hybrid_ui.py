"""Agent E, hybrid profile: API and Data-flow view of the external model use (docs/HYBRID_SPEC.md section 6).

* GET /api/runs/{id}/llm/usage against a temp workspace with a fake ledger (and a fake llm_benchmark.json);
* GET /api/llm/status and GET /api/settings name the external models the settings allow;
* PUT /api/settings accepts external_llm.model only when ExternalLLMConfig.model_allowed says yes (Fable refused);
* static assets: the card module parses as an ES module, its i18n keys exist in en / fi / sv, the card cannot
  overflow a narrow window, and its pure helpers give the right numbers (run under node).

No model is called: the local model server is pointed at a closed port and no API key is set.

    .venv\\Scripts\\python.exe -m pytest tests/test_e_hybrid_ui.py -q
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "tpm" / "api" / "static"
RUN = "run_test_hybrid"
EMPTY_RUN = "run_test_hybrid_empty"

SANITIZER = {"floats_rounded": 40, "numbers_in_strings_rounded": 2, "names_aliased": 7, "times_redacted": 3, "values_redacted": 1, "keys_dropped": 5, "vocabulary_size": 12, "notes": ["dropped keys: min x3"]}


def _fake_ledger(ws) -> None:
    """3 external calls that were sent, 1 blocked, 1 refused by the budget, 2 more local calls (the fixture already
    wrote one local call of 8400 ms and one blocked external call)."""
    from tpm.contracts import EgressRecord

    def rec(i: int, **kw) -> dict:
        base = dict(id=f"EGR-{i:06d}", task="diagnosis_narrative", purpose="test", route="external", provider="anthropic", model="claude-sonnet-5", artifact_types=["diagnosis"], payload_bytes=2048, payload_hash=hashlib.sha256(str(i).encode()).hexdigest(), guard_result="allowed", ok=True)
        base.update(kw)
        return EgressRecord(**base).model_dump()

    for i, ms in enumerate((2000, 3000, 4000), start=3):
        ws.append_jsonl("egress_ledger", rec(i, latency_ms=ms, input_tokens=1000, output_tokens=200, sanitizer=SANITIZER, payload_preview='{"diagnosis": {"signals": ["S01"], "score": 0.731}}'))
    ws.append_jsonl("egress_ledger", rec(6, task="critique", guard_result="blocked", guard_reason="egress invariant violated", ok=False, error="blocked by egress guard", sanitizer={"floats_rounded": 99}))
    ws.append_jsonl("egress_ledger", rec(7, task="why_chat", guard_result="budget", guard_reason="external call budget of this run is used up", ok=False, error="external budget exhausted"))
    for i, ms in ((8, 30000), (9, 21600)):
        ws.append_jsonl("egress_ledger", rec(i, route="local", provider="ollama", model="gemma4:e4b-it-qat", guard_result="n/a", latency_ms=ms))


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    from fastapi.testclient import TestClient

    from tests.fixtures.fake_workspace import build_fake_workspace
    from tpm.api.server import create_app
    from tpm.config import load_settings
    from tpm.contracts import RunStatus
    from tpm.workspace import Workspace

    base = tmp_path_factory.mktemp("e_hybrid")
    settings_path = base / "settings.yaml"
    text = (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    settings_path.write_text(text.replace("base_url: http://localhost:11434", "base_url: http://127.0.0.1:9"), encoding="utf-8")
    saved = os.environ.get("OLLAMA_HOST")
    os.environ["OLLAMA_HOST"] = "http://127.0.0.1:9"  # no local model server: status answers at once, nothing is called
    try:
        s = load_settings(settings_path)
        s.workspace_dir = str(base / "workspace")
        ws = build_fake_workspace(s, run_id=RUN, n_groups=6, n_samples=80)
        _fake_ledger(ws)
        ws.close()
        empty = Workspace(run_id=EMPTY_RUN, settings=s)
        empty.set_status(RunStatus(run_id=EMPTY_RUN, source_path="x", profile="no-egress", state="pending"))
        empty.close()
        app = create_app(settings_path=settings_path, workspace_dir=base / "workspace")
        with TestClient(app) as c:
            yield c, settings_path, base / "workspace"
    finally:
        if saved is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = saved


@pytest.fixture
def client(served):
    return served[0]


# ------------------------------------------------------------------------------------------ GET /llm/usage
def test_usage_route_counts_calls_tokens_latency_and_budget(client):
    r = client.get(f"/api/runs/{RUN}/llm/usage")
    assert r.status_code == 200, r.text
    d = r.json()
    for k in ("available", "profile", "allow_external", "external_model", "external_model_by_task", "external_models_allowed", "external_model_blocked_reason", "external_unavailable_reason", "usage", "caps", "sanitizer", "guard", "benchmark"):
        assert k in d, k
    assert d["available"] is True and d["profile"] == "no-egress" and d["allow_external"] is False
    u = d["usage"]
    assert u["external_ok"] == 3 and u["external_calls"] == 6, "3 sent + 2 blocked (one from the fixture) + 1 budget"
    assert u["blocked"] == 2 and u["budget_refused"] == 1
    assert u["input_tokens"] == 3000 and u["output_tokens"] == 600
    assert u["avg_latency_ms_external"] == 3000 and u["avg_latency_ms_local"] == 20000
    assert u["max_calls_per_run"] == d["caps"]["max_calls_per_run"] == 200 and u["budget_left_calls"] == 197
    assert u["by_task"]["diagnosis_narrative"]["external_ok"] == 3 and u["by_task"]["diagnosis_narrative"]["local_ok"] == 2
    assert set(d["caps"]) == {"max_calls_per_run", "max_calls_per_chat_turn", "max_output_tokens_per_run", "max_parallel", "max_narratives_per_run", "timeout_s"}
    assert d["guard"]["external_sig_digits"] == 3 and d["guard"]["alias_names_external"] is True
    assert d["benchmark"] is None


def test_usage_route_sums_what_the_guard_changed_in_sent_payloads_only(client):
    san = client.get(f"/api/runs/{RUN}/llm/usage").json()["sanitizer"]
    assert san["payloads"] == 3
    assert san["numbers_rounded"] == 3 * 42 and san["names_aliased"] == 3 * 7 and san["values_withheld"] == 3 * (3 + 1 + 5)
    assert san["detail"]["floats_rounded"] == 120, "the blocked payload (99 rounded floats) was never sent and is not counted"
    assert "vocabulary_size" not in san["detail"] and "notes" not in san["detail"]


def test_usage_route_on_a_run_without_model_calls_and_unknown_run(client):
    d = client.get(f"/api/runs/{EMPTY_RUN}/llm/usage").json()
    assert d["available"] is False and d["usage"]["external_ok"] == 0 and d["usage"]["budget_left_calls"] == 200
    assert d["usage"]["avg_latency_ms_external"] is None and d["sanitizer"] == {"payloads": 0, "numbers_rounded": 0, "names_aliased": 0, "values_withheld": 0, "detail": {}}
    assert client.get("/api/runs/no_such_run/llm/usage").status_code == 404
    assert client.get("/api/runs/..%2Fx/llm/usage").status_code in (400, 404)


@pytest.mark.parametrize("tasks", [
    [{"task": "diagnosis_narrative", "n": 3, "local": {"ok": 3, "avg_latency_ms": 31000}, "external": {"ok": 3, "avg_latency_ms": 2500}}, {"task": "critique", "n": 3, "local": {"avg_s": 26.0}, "external": None}],
    {"diagnosis_narrative": {"n": 3, "local": {"ok": 3, "mean_ms": 31000}, "external": {"ok": 3, "mean_ms": 2500}}, "critique": {"n": 3, "local": 26000}},
], ids=["list", "dict"])
def test_usage_route_shows_the_benchmark_when_present(served, tasks):
    client, _, workspace = served
    p = workspace / RUN / "llm_benchmark.json"
    p.write_text(json.dumps({"run_id": RUN, "created_at": "2026-09-19T16:00:00+03:00", "n": 3, "local_model": "gemma4:e4b-it-qat", "external_model": "claude-sonnet-5", "tasks": tasks}), encoding="utf-8")
    try:
        b = client.get(f"/api/runs/{RUN}/llm/usage").json()["benchmark"]
        assert b["n"] == 3 and b["external_model"] == "claude-sonnet-5" and b["local_model"] == "gemma4:e4b-it-qat"
        rows = {r["task"]: r for r in b["rows"]}
        assert rows["diagnosis_narrative"]["local_s"] == 31.0 and rows["diagnosis_narrative"]["external_s"] == 2.5 and rows["diagnosis_narrative"]["speedup"] == 12.4
        assert rows["critique"]["local_s"] == 26.0 and rows["critique"]["external_s"] is None and rows["critique"]["speedup"] is None
        p.write_text("not json", encoding="utf-8")
        assert client.get(f"/api/runs/{RUN}/llm/usage").json()["benchmark"] is None, "a broken benchmark file never breaks the view"
    finally:
        p.unlink()


# ------------------------------------------------------------------------------------------ status / settings
def test_status_and_settings_name_the_allowed_external_models(client):
    st = client.get("/api/llm/status")
    assert st.status_code == 200, st.text
    st = st.json()
    assert st["external_models_allowed"] == ["claude-sonnet-5", "claude-opus-5"] and st["external_model_blocked_reason"] is None
    assert st["profile"] == "no-egress" and st["allow_external"] is False and st["external"] is False
    assert "does not allow external models" in st["external_unavailable_reason"]
    s = client.get("/api/settings").json()
    assert s["external_models_allowed"] == ["claude-sonnet-5", "claude-opus-5"] and s["external_model_blocked_reason"] is None
    assert s["external_caps"]["max_calls_per_run"] == 200 and s["external_caps"]["max_calls_per_chat_turn"] == 6 and s["external_sig_digits"] == 3
    assert not any("fable" in m or "mythos" in m for m in s["external_models_allowed"])


def test_put_settings_accepts_an_allowed_external_model_and_keeps_the_file_readable(served):
    client, settings_path, _ = served
    r = client.put("/api/settings", json={"external_llm": {"model": "claude-opus-5"}})
    assert r.status_code == 200, r.text
    assert r.json()["external_model"] == "claude-opus-5" and r.json()["changed"] == {"external_llm": {"model": "claude-opus-5"}}
    assert client.get("/api/settings").json()["external_model"] == "claude-opus-5"
    text = settings_path.read_text(encoding="utf-8")
    assert re.search(r"(?m)^  model: claude-opus-5\s+# or claude-opus-5; never a Fable", text), "the line is edited in place: comments stay"
    assert "model: gemma4:e4b-it-qat" in text and "model_by_task: {}" in text, "the local model line and the neighbours are untouched"
    r = client.put("/api/settings", json={"external_model": "claude-sonnet-5"})  # flat form, like local_model
    assert r.status_code == 200 and client.get("/api/settings").json()["external_model"] == "claude-sonnet-5"
    from tpm.config import load_settings

    assert load_settings(settings_path).external_llm.model == "claude-sonnet-5"


@pytest.mark.parametrize("model, words", [
    ("claude-fable-5-1", "30 days"),
    ("Claude-FABLE-5", "30 days"),
    ("claude-mythos-5", "30 days"),
    ("gpt-4o", "allowed families"),
    ("", "no external model"),
])
def test_put_settings_refuses_a_model_that_is_not_allowed(served, model, words):
    client, settings_path, _ = served
    before = settings_path.read_text(encoding="utf-8")
    r = client.put("/api/settings", json={"external_llm": {"model": model}})
    assert r.status_code == 400, r.text
    assert words in r.json()["detail"], r.json()
    assert settings_path.read_text(encoding="utf-8") == before, "a refused model never reaches the settings file"
    assert client.get("/api/settings").json()["external_model"] == "claude-sonnet-5"


def test_put_settings_changes_only_the_model_id_of_external_llm(served):
    client, settings_path, _ = served
    before = settings_path.read_text(encoding="utf-8")
    for body in ({"external_llm": {"model": "claude-opus-5", "blocked_model_patterns": []}}, {"external_llm": {"base_url": "https://example.test"}}, {"external_llm": "claude-opus-5"}, {"external_llm": {"model": "claude-opus-5\n  api_key_env: X"}}):
        r = client.put("/api/settings", json=body)
        assert r.status_code == 400, (body, r.text)
    assert settings_path.read_text(encoding="utf-8") == before
    assert client.put("/api/settings", json={}).status_code == 400


def test_profile_switching_still_works_next_to_the_model(served):
    client, settings_path, _ = served
    r = client.put("/api/settings", json={"profile": "hybrid"})
    assert r.status_code == 200 and r.json()["profile"] == "hybrid" and r.json()["allow_external"] is True
    d = client.get(f"/api/runs/{RUN}/llm/usage").json()
    assert d["allow_external"] is True and d["external_unavailable_reason"].startswith("no API key"), "hybrid without a key: allowed, not usable"
    r = client.put("/api/settings", json={"profile": "eu-hosted", "external_llm": {"model": "claude-opus-5"}})
    # eu-hosted brings its own endpoint and model (Mistral Large 3 on Verda); the Claude choice is kept for hybrid
    assert r.status_code == 200 and r.json()["profile"] == "eu-hosted" and r.json()["external_model"].startswith("mistralai/")
    status = client.get("/api/llm/status").json()
    assert status["external_unavailable_reason"] == "no API key in env TPM_EU_API_KEY" and "containers.datacrunch.io" in status["external_base_url"]
    assert client.put("/api/settings", json={"profile": "bogus"}).status_code == 400
    r = client.put("/api/settings", json={"profile": "no-egress", "external_model": "claude-sonnet-5"})
    assert r.status_code == 200 and r.json()["allow_external"] is False
    text = settings_path.read_text(encoding="utf-8")
    assert re.search(r"(?m)^profile: no-egress\s+# default", text) and "# or claude-opus-5; never a Fable" in text, "both in-place edits keep the comments"


def test_a_blocked_model_from_the_environment_is_reported_and_the_ui_choice_wins(served, monkeypatch):
    client, _, _ = served
    monkeypatch.setenv("TPM_EXTERNAL_MODEL", "claude-fable-5-1")
    assert client.put("/api/settings", json={"profile": "hybrid"}).status_code == 200  # reloads the settings with the pinned model
    try:
        st = client.get("/api/llm/status").json()
        assert st["external_model"] == "claude-fable-5-1" and st["external"] is False
        assert "30 days" in st["external_model_blocked_reason"] and st["external_models_allowed"] == ["claude-sonnet-5", "claude-opus-5"]
        d = client.get(f"/api/runs/{RUN}/llm/usage").json()
        assert "30 days" in d["external_model_blocked_reason"] and "30 days" in d["external_unavailable_reason"]
        r = client.put("/api/settings", json={"external_llm": {"model": "claude-opus-5"}})
        assert r.status_code == 200 and os.environ["TPM_EXTERNAL_MODEL"] == "claude-opus-5", "the UI choice wins over the pinned variable for this process"
        assert client.get("/api/llm/status").json()["external_model_blocked_reason"] is None
    finally:
        monkeypatch.delenv("TPM_EXTERNAL_MODEL", raising=False)
        client.put("/api/settings", json={"profile": "no-egress", "external_model": "claude-sonnet-5"})


# ------------------------------------------------------------------------------------------ static assets
def _src(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _load(lang: str) -> dict[str, str]:
    return json.loads(_src(f"i18n/{lang}.json"))


@pytest.mark.parametrize("name", ["js/externaluse.js", "js/views/dataflow.js"])
def test_node_check_as_module(name, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    mjs = tmp_path / (Path(name).stem + ".mjs")
    mjs.write_text(_src(name), encoding="utf-8")
    r = subprocess.run([node, "--check", str(mjs)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr or r.stdout


def test_card_keys_exist_in_every_language_with_the_same_placeholders():
    en, fi, sv = _load("en"), _load("fi"), _load("sv")
    used = set(re.findall(r"'(flow\.ext\.[A-Za-z.]+)'", _src("js/externaluse.js") + _src("js/views/dataflow.js")))
    assert len(used) >= 40
    ph = lambda s: set(re.findall(r"\{(\w+)\}", s))  # noqa: E731
    for k in sorted(used):
        for d, lang in ((en, "en"), (fi, "fi"), (sv, "sv")):
            assert d.get(k, "").strip(), f"{lang}: {k} is missing"
        assert ph(en[k]) == ph(fi[k]) == ph(sv[k]), k
        assert fi[k] != en[k] or k in ("flow.ext.reason.other",), f"fi: {k} is not translated"
    assert sorted(k for k in en if k.startswith("flow.ext.") and k not in used) == [], "no dead keys"
    # the plain sentence: what leaves, what never leaves, which model, why Fable-class models are refused
    assert "{model}" in en["flow.ext.leadOn"] and "never leaves this machine" in en["flow.ext.leadOn"]
    assert "Fable" in en["flow.ext.leadOnMore"] and "30 days" in en["flow.ext.leadOnMore"] and "{digits}" in en["flow.ext.leadOnMore"]
    assert en["flow.ext.leadOff"].startswith("Nothing leaves this machine")
    assert "Fable" in fi["flow.ext.leadOnMore"] and "30" in fi["flow.ext.leadOnMore"] and "Fable" in sv["flow.ext.leadOnMore"] and "30" in sv["flow.ext.leadOnMore"]


def test_view_wires_the_card_first_and_the_ledger_shows_what_was_sent():
    view = _src("js/views/dataflow.js")
    assert "import { externalUseCard } from '../externaluse.js';" in view
    assert "page.insertBefore(ext.root, tech)" in view, "the card sits above the technical expander"
    assert "profileCards: cards" in view and "ext.reload(s)" in view, "the profile selector lives in the card; a profile switch repaints it"
    assert view.index("externalUseCard(") < view.index("section(t('flow.models'))") < view.index("section(t('flow.ledger')")
    assert "t('flow.ext.previewCol')" in view and "r.payload_preview" in view and "t('flow.ext.tokensCol')" in view
    card = _src("js/externaluse.js")
    assert "runApi('/llm/usage')" in card and "body: { external_llm: { model: id } }" in card
    assert "'claude-sonnet-5'" in card and "'claude-opus-5'" in card and "fable" not in card.lower().replace("fable-class", "")
    assert "briefSection: 'profile'" in card, "the summary card's 'choose what may leave' action still finds the profile selector"
    assert "\n'" not in card and '\n"' not in card


def test_card_fits_a_narrow_window():
    css = _src("styles.css")
    block = css[css.index(".sec.extuse"):]
    for cls in (".sec.extuse", ".extuse-lead", ".extuse-first", ".extuse-stats", ".extuse-stat", ".extuse-note"):
        assert cls in block, cls
    assert "repeat(auto-fit, minmax(min(140px, 100%), 1fr))" in block, "tiles wrap instead of overflowing"
    assert ".extuse .sec-head .chip { white-space: normal; }" in block
    rules = [ln for ln in block.splitlines() if ln.strip().startswith(".ext") or ln.strip().startswith(".sec.extuse")]
    assert rules and not any(re.search(r"(?<![-\w])(?:min-)?width:\s*\d{3,}px", ln) for ln in rules), "no fixed widths of 100 px or more"
    assert not any("white-space: nowrap" in ln for ln in rules)
    assert css.count("{") == css.count("}")


HARNESS = r"""
import { callSeconds, externalMode, unusableKey, usageFacts, factsFromSettings, modelLabel } from './externaluse.mjs';
const usage = { external_ok: 3, max_calls_per_run: 200, budget_left_calls: 197, input_tokens: 3000, output_tokens: 600, blocked: 2, budget_refused: 1, avg_latency_ms_local: 20000, avg_latency_ms_external: 3000, by_task: { why_chat: { external_ok: 3, local_ok: 1 } } };
const out = {};
out.seconds = [callSeconds(2140), callSeconds(31200), callSeconds(null), callSeconds(undefined), callSeconds(0)];
out.facts = usageFacts({ usage, caps: { max_calls_per_run: 200 } });
out.empty = usageFacts({ usage: {}, caps: { max_calls_per_run: 50 } });
out.nothing = usageFacts(null);
out.modes = [externalMode(null), externalMode({ allow_external: false, external_unavailable_reason: 'profile' }), externalMode({ allow_external: true }), externalMode({ allow_external: true, external_unavailable_reason: 'no API key in env ANTHROPIC_API_KEY' }), externalMode({ allow_external: true, external_model_blocked_reason: 'blocked' })];
out.reasons = [unusableKey({ external_unavailable_reason: 'no API key in env ANTHROPIC_API_KEY' }), unusableKey({ external_unavailable_reason: "profile 'eu-hosted' needs external_llm.base_url set to an EU-hosted endpoint" }), unusableKey({ external_unavailable_reason: 'x', external_model_blocked_reason: 'blocked' }), unusableKey({ external_unavailable_reason: 'something else' })];
out.fromSettings = factsFromSettings({ profile: 'no-egress', allow_external: false, external_model: 'claude-sonnet-5', external_models_allowed: ['claude-sonnet-5', 'claude-opus-5'], external_caps: { max_calls_per_run: 200 }, external_sig_digits: 3 });
out.older = factsFromSettings({ profile: 'no-egress' });
out.labels = [modelLabel('claude-sonnet-5'), modelLabel('claude-opus-5'), modelLabel('eu.anthropic.claude-sonnet-5-v1'), modelLabel(null)];
console.log(JSON.stringify(out));
"""


def test_card_numbers_and_modes_under_node(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    (tmp_path / "core.mjs").write_text(_src("js/core.js"), encoding="utf-8")
    (tmp_path / "externaluse.mjs").write_text(_src("js/externaluse.js").replace("from './core.js'", "from './core.mjs'"), encoding="utf-8")
    (tmp_path / "harness.mjs").write_text(HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(tmp_path / "harness.mjs")], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["seconds"] == ["2.1", "31", None, None, "0.0"]
    f = out["facts"]
    assert (f["used"], f["cap"], f["left"], f["tokensIn"], f["tokensOut"], f["blocked"], f["refused"]) == (3, 200, 197, 3000, 600, 2, 1)
    assert f["speedup"] == 6.7 and f["byTask"] == [{"task": "why_chat", "external_ok": 3, "local_ok": 1}]
    assert out["empty"]["used"] == 0 and out["empty"]["cap"] == 50 and out["empty"]["left"] == 50 and out["empty"]["speedup"] is None
    assert out["nothing"]["cap"] is None and out["nothing"]["left"] is None
    assert out["modes"] == ["off", "off", "on", "unusable", "unusable"], "no-egress is 'off' whatever else the server says"
    assert out["reasons"] == ["flow.ext.reason.key", "flow.ext.reason.endpoint", "flow.ext.reason.blocked", "flow.ext.reason.other"]
    assert out["fromSettings"]["allow_external"] is False and out["fromSettings"]["usage"] is None and out["fromSettings"]["guard"]["external_sig_digits"] == 3
    assert out["older"]["caps"] == {} and out["older"]["external_model_blocked_reason"] is None, "an older server without the new fields still renders"
    assert out["labels"] == ["Sonnet 5", "Opus 5", "eu.anthropic.claude-sonnet-5-v1", ""]
