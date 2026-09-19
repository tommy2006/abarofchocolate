"""Local model management: the app works with whatever Ollama models a machine has, can pull more, and can get
Ollama onto a machine without it. Everything here runs against a fake Ollama (no network)."""
import json

import pytest
import yaml

from tpm.llm import models as mm

GB = 1_000_000_000


def _tag(name, size_gb, params, caps, family="llama"):
    return {"name": name, "size": int(size_gb * GB), "digest": "sha-" + name, "details": {"parameter_size": params, "quantization_level": "Q4_K_M", "family": family}, "capabilities": caps}


LAPTOP = {"ram_gb": 16.0, "ram_free_gb": 8.0, "gpu": {"name": "test gpu", "vram_gb": 8.0}, "disk_free_gb": 100.0}


class FakeOllama:
    """Replaces httpx in tpm.llm.models: /api/version, /api/tags, /api/show and a scripted /api/pull stream."""

    def __init__(self, tags=None, running=True, pull_lines=None, pull_status=200):
        self.tags = tags or []
        self.running = running
        self.pull_lines = pull_lines or []
        self.pull_status = pull_status
        self.pulled = []

    class _Resp:
        def __init__(self, status=200, data=None, lines=None):
            self.status_code = status
            self._data = data or {}
            self._lines = lines or []

        def json(self):
            return self._data

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def read(self):
            return json.dumps(self._data).encode()

        def iter_lines(self):
            yield from self._lines

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def get(self, url, **kw):
        if not self.running:
            raise mm.httpx.ConnectError("refused")
        if url.endswith("/api/version"):
            return self._Resp(200, {"version": "0.0-test"})
        if url.endswith("/api/tags"):
            return self._Resp(200, {"models": self.tags})
        return self._Resp(404)

    def post(self, url, json=None, **kw):
        return self._Resp(200, {"capabilities": [], "details": {}})

    def stream(self, method, url, json=None, **kw):
        if not self.running:
            raise mm.httpx.ConnectError("refused")
        self.pulled.append(json["model"])
        return self._Resp(self.pull_status, {"error": "nope"}, self.pull_lines)


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        f = FakeOllama(**kw)
        monkeypatch.setattr(mm.httpx, "get", f.get)
        monkeypatch.setattr(mm.httpx, "post", f.post)
        monkeypatch.setattr(mm.httpx, "stream", f.stream)
        monkeypatch.setattr(mm, "machine_cached", lambda max_age_s=60.0: dict(LAPTOP))
        mm._SHOW_CACHE.clear()
        return f

    return install


def test_classify_separates_search_models_from_chat_models():
    assert mm.classify("nomic-embed-text:latest", ["embedding"]) == "embedding"
    assert mm.classify("mystery-embed-v2", []) == "embedding"  # older Ollama reports no capabilities
    assert mm.classify("some-bert", [], family="nomic-bert") == "embedding"
    assert mm.classify("llama3:8b", ["completion"]) == "chat"
    assert mm.classify("anything:new", []) == "chat"


def test_rank_prefers_the_biggest_model_that_fits(tmp_settings):
    models = [
        {"name": "tiny:1b", "kind": "chat", "size_gb": 0.8, "parameters_b": 1.0, "capabilities": ["completion"]},
        {"name": "good:8b", "kind": "chat", "size_gb": 4.7, "parameters_b": 8.0, "capabilities": ["completion", "tools"]},
        {"name": "huge:70b", "kind": "chat", "size_gb": 40.0, "parameters_b": 70.0, "capabilities": ["completion"]},
        {"name": "embed", "kind": "embedding", "size_gb": 0.3, "parameters_b": 0.1, "capabilities": ["embedding"]},
    ]
    ranked = mm.rank_chat(models, LAPTOP)
    assert [m["name"] for m in ranked] == ["good:8b", "tiny:1b", "huge:70b"]
    assert ranked[0]["fit"] == "fast" and ranked[-1]["fit"] == "too_big"
    assert all(m["why"] for m in ranked)


def test_model_names_are_validated_before_they_reach_ollama():
    for ok in ("qwen3:4b", "gemma4:e4b-it-qat", "library/llama3.2:3b", "hf.co/org/model:Q4_K_M", "nomic-embed-text"):
        assert mm.valid_name(ok), ok
    for bad in ("", "a b", "x;rm -rf /", "../etc", "name:tag:extra", "$(whoami)", "x" * 200, "-flag"):
        assert not mm.valid_name(bad), bad


def test_choice_no_longer_depends_on_one_particular_model(tmp_settings, fake):
    s = tmp_settings
    # nothing the app was configured for is installed: it picks the best installed chat model by itself
    fake(tags=[_tag("llama3.2:3b", 2.0, "3.2B", ["completion"]), _tag("phi9:14b", 9.0, "14B", ["completion"]), _tag("all-minilm:latest", 0.05, "23M", ["embedding"], "bert")])
    s.local_llm.model, s.local_llm.fallback_models, s.local_llm.embedding_model = "not-installed:1b", [], "also-missing"
    c = mm.choose(s)
    assert c["chat"] == "llama3.2:3b" and c["chat_source"] == "auto"  # the 14B one only fits slowly
    assert c["embedding"] == "all-minilm:latest" and c["embedding_source"] == "auto"
    # the configured default wins when it is there
    fake(tags=[_tag("llama3.2:3b", 2.0, "3.2B", ["completion"]), _tag("not-installed:1b", 0.8, "1B", ["completion"])])
    assert mm.choose(s)["chat_source"] == "configured"
    # a fallback is used before the automatic choice
    s.local_llm.fallback_models = ["llama3.2:3b"]
    fake(tags=[_tag("llama3.2:3b", 2.0, "3.2B", ["completion"]), _tag("zeta:7b", 4.0, "7B", ["completion"])])
    assert mm.choose(s)["chat"] == "llama3.2:3b" and mm.choose(s)["chat_source"] == "fallback"
    # nothing installed at all
    fake(tags=[])
    c = mm.choose(s)
    assert c["chat"] is None and c["embedding"] is None


def test_auto_select_can_be_switched_off(tmp_settings, fake):
    fake(tags=[_tag("llama3.2:3b", 2.0, "3.2B", ["completion"])])
    tmp_settings.local_llm.model, tmp_settings.local_llm.fallback_models, tmp_settings.local_llm.auto_select = "missing:1b", [], False
    assert mm.choose(tmp_settings)["chat"] is None


def test_overview_names_the_next_step(tmp_settings, fake, monkeypatch):
    s = tmp_settings
    monkeypatch.setattr(mm, "ollama_binary", lambda: None)
    fake(running=False)
    assert mm.overview(s)["next_step"] == "install_ollama"
    monkeypatch.setattr(mm, "ollama_binary", lambda: "C:/fake/ollama.exe")
    assert mm.overview(s)["next_step"] == "start_ollama"
    fake(tags=[_tag("nomic-embed-text:latest", 0.27, "137M", ["embedding"], "nomic-bert")])
    assert mm.overview(s)["next_step"] == "pull_chat_model"
    fake(tags=[_tag("llama3:8b", 4.7, "8B", ["completion"])])
    o = mm.overview(s)
    assert o["next_step"] == "pull_embedding_model" and o["selected"]["chat"] == "llama3:8b"
    fake(tags=[_tag("llama3:8b", 4.7, "8B", ["completion"]), _tag("nomic-embed-text:latest", 0.27, "137M", ["embedding"], "nomic-bert")])
    o = mm.overview(s)
    assert o["next_step"] == "ready"
    assert {m["name"]: m["in_use"] for m in o["models"]} == {"llama3:8b": True, "nomic-embed-text:latest": True}
    assert any(x["name"] == "nomic-embed-text" and x["installed"] for x in o["suggested"])


def test_user_choice_is_saved_and_beats_the_env_pin(tmp_path, tmp_settings, fake, monkeypatch):
    from tpm import config

    fake(tags=[_tag("llama3:8b", 4.7, "8B", ["completion"]), _tag("qwen3:4b", 2.6, "4B", ["completion", "tools"]), _tag("nomic-embed-text:latest", 0.27, "137M", ["embedding"], "nomic-bert")])
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump({"profile": "no-egress", "local_llm": {"model": "llama3:8b"}}), encoding="utf-8")
    with pytest.raises(ValueError):
        mm.select("chat", "never-pulled:1b", tmp_settings, path)
    with pytest.raises(ValueError):
        mm.select("chat", "nomic-embed-text", tmp_settings, path)  # a search model cannot answer questions
    with pytest.raises(ValueError):
        mm.select("embedding", "qwen3:4b", tmp_settings, path)
    mm.select("chat", "qwen3:4b", tmp_settings, path)
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))["local_llm"]
    assert saved["model"] == "qwen3:4b" and saved["model_selected_by"] == "user"
    # .env pins TPM_LOCAL_MODEL on the team's machines: a choice made in the UI must survive a restart anyway
    monkeypatch.setenv("TPM_LOCAL_MODEL", "gemma4:e4b-it-qat")
    assert config.load_settings(path).local_llm.model == "qwen3:4b"
    mm.select("chat", "auto", tmp_settings, path)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["local_llm"]["model_selected_by"] == "config"
    assert config.load_settings(path).local_llm.model == "gemma4:e4b-it-qat"  # back to config / env
    config.reload_settings()


def _wait(job_id, timeout=5.0):
    import time

    t0 = time.time()
    while time.time() - t0 < timeout:
        j = mm.PULLS.get(job_id)
        if j["state"] not in ("queued", "running"):
            return j
        time.sleep(0.02)
    raise AssertionError("pull did not finish")


def test_pull_reports_progress_and_success(tmp_settings, fake):
    lines = [json.dumps(x) for x in (
        {"status": "pulling manifest"},
        {"status": "pulling aaa", "digest": "aaa", "total": 4 * GB, "completed": 1 * GB},
        {"status": "pulling aaa", "digest": "aaa", "total": 4 * GB, "completed": 4 * GB},
        {"status": "pulling bbb", "digest": "bbb", "total": 1 * GB, "completed": 1 * GB},
        {"status": "verifying sha256 digest"},
        {"status": "success"},
    )]
    f = fake(pull_lines=lines)
    job = mm.PULLS.start("qwen3:4b", tmp_settings)
    done = _wait(job["id"])
    assert f.pulled == ["qwen3:4b"]
    assert done["state"] == "done" and done["percent"] == 100.0 and done["total_gb"] == 5.0
    assert "_cancel" not in done  # internals never reach the API


def test_pull_failure_is_explained_in_plain_words(tmp_settings, fake):
    fake(pull_lines=[json.dumps({"error": "pull model manifest: file does not exist"})])
    done = _wait(mm.PULLS.start("no-such-model:9b", tmp_settings)["id"])
    assert done["state"] == "failed" and "does not know a model called no-such-model:9b" in done["error"]
    with pytest.raises(ValueError):
        mm.PULLS.start("bad name; rm", tmp_settings)


def test_installer_is_only_opened_with_a_valid_ollama_signature(monkeypatch, tmp_path):
    class R:
        def __init__(self, out):
            self.stdout = out

    monkeypatch.setattr(mm.subprocess, "run", lambda *a, **k: R("Valid|CN=Ollama Inc., O=Ollama Inc., C=US\n"))
    assert mm._signature_ok(tmp_path / "x.exe")[0] is True
    monkeypatch.setattr(mm.subprocess, "run", lambda *a, **k: R("NotSigned|\n"))
    assert mm._signature_ok(tmp_path / "x.exe")[0] is False
    monkeypatch.setattr(mm.subprocess, "run", lambda *a, **k: R("Valid|CN=Somebody Else Ltd\n"))
    ok, why = mm._signature_ok(tmp_path / "x.exe")
    assert ok is False and "someone else" in why


def test_models_routes(tmp_path, fake, monkeypatch):
    from fastapi.testclient import TestClient

    from tpm import config
    from tpm.api.server import create_app

    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text((config.ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    fake(tags=[_tag("llama3:8b", 4.7, "8B", ["completion"]), _tag("nomic-embed-text:latest", 0.27, "137M", ["embedding"], "nomic-bert")], pull_lines=[json.dumps({"status": "success"})])
    app = create_app(settings_path=settings_path, workspace_dir=tmp_path / "ws")
    with TestClient(app) as c:
        o = c.get("/api/models").json()
        assert o["next_step"] == "ready" and o["selected"]["chat"] == "llama3:8b"  # the configured default is absent: auto choice
        assert c.post("/api/models/select", json={"kind": "chat", "name": "ghost:1b"}).status_code == 400
        assert c.post("/api/models/select", json={"kind": "chat", "name": ""}).status_code == 400
        r = c.post("/api/models/select", json={"kind": "chat", "name": "llama3:8b"})
        assert r.status_code == 200 and r.json()["selected"]["chat_source"] == "user"
        assert c.post("/api/models/pull", json={"name": "bad name"}).status_code == 400
        job = c.post("/api/models/pull", json={"name": "gemma3:1b"}).json()
        assert _wait(job["id"])["state"] == "done"
        assert c.get(f"/api/models/pull/{job['id']}").json()["state"] == "done"
        assert c.get("/api/models/pull/PULL-unknown").status_code == 404
        fake(running=False)
        assert c.post("/api/models/pull", json={"name": "gemma3:1b"}).status_code == 409
    config.reload_settings()


def test_provider_works_with_whatever_is_installed(tmp_settings, fake):
    from tpm.llm.providers import OllamaProvider

    fake(tags=[_tag("other:latest", 2.0, "3B", ["completion"]), _tag("bge-m3:latest", 1.2, "567M", ["embedding"], "bert")])
    OllamaProvider._tags_cache.clear()
    OllamaProvider._auto_cache.clear()
    s = tmp_settings
    s.local_llm.model, s.local_llm.fallback_models = "gemma4:e4b-it-qat", []
    p = OllamaProvider(s)
    assert p.pick_model() == "other:latest"
    assert p.embedding_model() == "bge-m3:latest" and p.has_embedding_model()
    assert p.missing_models() == [] and p.pull_commands() == []
    s.local_llm.auto_select = False
    OllamaProvider._auto_cache.clear()
    p = OllamaProvider(s)
    assert p.pick_model() is None and not p.has_embedding_model()
    assert p.pull_commands() == ["ollama pull gemma4:e4b-it-qat", "ollama pull nomic-embed-text"]
    OllamaProvider._tags_cache.clear()
    OllamaProvider._auto_cache.clear()
