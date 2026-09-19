"""Round 5 B, chat controls: several chats per run (chat_id), stop a turn in progress (stop flag checked by
tpm.llm.agent.run_agent between steps), clear / delete a chat's history, and the drawer's client-side wiring.

    .venv\\Scripts\\python.exe -m pytest tests/test_e_chat_controls.py -q
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tpm.llm import agent as agent_mod
from tpm.llm.providers import OllamaProvider
from tpm.llm.sandbox import make_demo_workspace

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "tpm" / "api" / "static"


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    from tpm.config import load_settings

    s = load_settings(profile="no-egress")
    ws = make_demo_workspace(s, root=tmp_path_factory.mktemp("ws"), run_id="chat_ctl", n_groups=4, n_samples=120)
    return ws, s


def _no_ollama(monkeypatch):
    monkeypatch.setattr(OllamaProvider, "is_available", lambda self: False)
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: [])


def _fake_model(monkeypatch, replies):
    """A local model that answers from a list; each call pops the next reply (a dict = the JSON action)."""
    monkeypatch.setattr(OllamaProvider, "is_available", lambda self: True)
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: ["gemma3:4b"])
    monkeypatch.setattr(OllamaProvider, "capabilities", lambda self, m: [])
    calls = []

    def fake_chat(self, messages, schema=None, max_tokens=None, model=None, temperature=None):
        calls.append(messages[-1]["content"][:80])
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if callable(reply):
            reply = reply()
        return json.dumps(reply), reply, 0
    monkeypatch.setattr(OllamaProvider, "chat", fake_chat)
    return calls


# ------------------------------------------------------------------------------------------ agent: chat ids
def test_turns_are_tagged_with_the_chat_id_and_old_turns_count_as_default(demo, monkeypatch):
    ws, s = demo
    _no_ollama(monkeypatch)
    ws.rewrite_jsonl("chat", [{"ts": "2026-01-01T00:00:00", "role": "user", "content": "old question"}])   # from before chats existed
    out = agent_mod.chat(ws, s, "Why was FLAG-000001 raised?", context={"flag_id": "FLAG-000001"}, actor="tester", chat_id="c-abc", client_turn_id="CHAT-client1")
    assert out["chat_id"] == "c-abc" and out["turn_id"] == "CHAT-client1" and out["answer"]
    mine = agent_mod.chat_history(ws, chat_id="c-abc")
    assert [m["role"] for m in mine] == ["user", "assistant"] and all(m["turn_id"] == "CHAT-client1" for m in mine)
    old = agent_mod.chat_history(ws, chat_id="default")
    assert old and old[0]["content"] == "old question" and agent_mod.turn_chat_id(old[0]) == "default"
    assert len(agent_mod.chat_history(ws)) == 3
    # ids that are not safe as keys / file content fall back
    assert agent_mod.clean_chat_id("../x") == "default" and agent_mod.clean_turn_id("bad id") is None and agent_mod.clean_turn_id("CHAT-1:2") == "CHAT-1:2"


def test_clear_chat_removes_only_that_chats_turns(demo, monkeypatch):
    ws, s = demo
    _no_ollama(monkeypatch)
    agent_mod.chat(ws, s, "first", chat_id="keep")
    agent_mod.chat(ws, s, "second", chat_id="drop")
    before = len(agent_mod.chat_history(ws, limit=1000))
    assert agent_mod.clear_chat(ws, "drop") == 2
    after = agent_mod.chat_history(ws, limit=1000)
    assert len(after) == before - 2 and not [m for m in after if agent_mod.turn_chat_id(m) == "drop"]
    assert [m for m in after if agent_mod.turn_chat_id(m) == "keep"]
    assert agent_mod.clear_chat(ws, "drop") == 0


# ------------------------------------------------------------------------------------------ agent: stop flag
def test_a_stop_requested_before_the_first_model_call_ends_the_turn_as_stopped(demo, monkeypatch):
    ws, s = demo
    calls = _fake_model(monkeypatch, [{"action": "final", "answer": "This answer must never be produced.", "citations": []}])
    agent_mod.request_stop("CHAT-stop-early")
    out = agent_mod.chat(ws, s, "tell me everything", chat_id="c-stop", client_turn_id="CHAT-stop-early", max_steps=3)
    assert out["stopped"] is True and out["status"] == "stopped" and out["source"] == "stopped" and out["answer"] == ""
    assert calls == [], "no model call may be made after Stop"
    turns = agent_mod.chat_history(ws, chat_id="c-stop")
    assert turns[-1]["role"] == "assistant" and turns[-1]["status"] == "stopped" and turns[-1]["stopped"] is True
    assert not agent_mod.is_stopped("CHAT-stop-early")          # the flag is dropped when the turn ends


def test_a_stop_during_a_tool_step_is_noticed_before_the_next_model_call(demo, monkeypatch):
    ws, s = demo
    turn = "CHAT-stop-mid"

    def tool_call_then_stop():
        agent_mod.request_stop(turn)                              # the person presses Stop while the model works
        return {"thought": "look", "action": "get_flag", "args": {"id": "FLAG-000001"}}
    calls = _fake_model(monkeypatch, [tool_call_then_stop, {"action": "final", "answer": "never", "citations": []}])
    out = agent_mod.chat(ws, s, "Which signal contributed most?", context={"flag_id": "FLAG-000001"}, chat_id="c-stop", client_turn_id=turn, max_steps=4)
    assert out["stopped"] is True and len(calls) == 1
    assert any(x.get("error") == "stopped by the operator" for x in out["tool_trace"])
    # request_stop by chat id flags every running turn of that chat; nothing is running now
    assert agent_mod.request_stop(None, "c-stop") == []
    assert agent_mod.request_stop("CHAT-x", "c-stop") == ["CHAT-x"]
    agent_mod._turn_end("CHAT-x")


def test_run_agent_stop_callable_is_honoured_by_the_loop(demo, monkeypatch):
    ws, s = demo
    calls = _fake_model(monkeypatch, [{"action": "final", "answer": "x" * 40, "citations": []}])
    tb = agent_mod.Toolbox(ws, s)
    loaded = agent_mod.load_context(ws, s, {}, "hi", tb)
    out = agent_mod.run_agent(ws, s, "hi", loaded, tb, stop=lambda: True, max_steps=2)
    assert out["stopped"] is True and out["incomplete"] is True and calls == []
    out2 = agent_mod.run_agent(ws, s, "hi", loaded, tb, stop=lambda: False, max_steps=2)
    assert out2["answer"] == "x" * 40 and "stopped" not in out2


# ------------------------------------------------------------------------------------------ API routes
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient

    from tpm.api.server import create_app
    from tpm.config import load_settings

    base = tmp_path_factory.mktemp("chat_api")
    app = create_app(workspace_dir=base / "workspace")
    s = load_settings(profile="no-egress")
    make_demo_workspace(s, root=base / "workspace", run_id="run_chat", n_groups=4, n_samples=120)
    with TestClient(app) as c:
        yield c
    app.state.live.shutdown()


def test_chat_routes_keep_chats_apart_stop_and_clear(client, monkeypatch):
    _no_ollama(monkeypatch)
    r = client.post("/api/runs/run_chat/chat", json={"message": "Why?", "context": {"flag_id": "FLAG-000001"}, "chat_id": "c-one", "client_turn_id": "CHAT-t1", "actor": "Ada", "role": "operator"})
    assert r.status_code == 200, r.text
    a = r.json()["answer"]
    assert a["chat_id"] == "c-one" and a["turn_id"] == "CHAT-t1" and a["message"] and "series" in a
    r2 = client.post("/api/runs/run_chat/chat", json={"message": "What now?", "chat_id": "c-two"})
    assert r2.status_code == 200 and r2.json()["answer"]["chat_id"] == "c-two"
    g = client.get("/api/runs/run_chat/chat", params={"chat_id": "c-one"}).json()
    assert [m["role"] for m in g["items"]] == ["user", "assistant"] and all(m["chat_id"] == "c-one" for m in g["items"])
    assert {c["chat_id"] for c in g["chats"]} == {"c-one", "c-two"}
    assert next(c for c in g["chats"] if c["chat_id"] == "c-one")["first_question"] == "Why?"
    assert len(client.get("/api/runs/run_chat/chat").json()["items"]) == 4
    # stop: a turn id that is not running is still flagged (the turn may not have started yet) and the decision is logged
    st = client.post("/api/runs/run_chat/chat/stop", json={"turn_id": "CHAT-later", "chat_id": "c-one", "actor": "Ada"})
    assert st.status_code == 200 and st.json()["stopped"] == ["CHAT-later"]
    agent_mod._turn_end("CHAT-later")
    assert client.post("/api/runs/run_chat/chat/stop", json={}).status_code == 400
    # a stopped turn is answered as stopped, not with a template answer
    agent_mod.request_stop("CHAT-t9")
    r9 = client.post("/api/runs/run_chat/chat", json={"message": "slow question", "chat_id": "c-one", "client_turn_id": "CHAT-t9"})
    assert r9.status_code == 200 and r9.json()["stopped"] is True and r9.json()["answer"]["status"] == "stopped"
    # clear one chat: its turns go, the other chat stays, logged as a human decision
    c = client.post("/api/runs/run_chat/chat/clear", json={"chat_id": "c-one", "actor": "Ada", "role": "operator"})
    assert c.status_code == 200 and c.json()["removed"] == 4
    left = client.get("/api/runs/run_chat/chat").json()
    assert {m["chat_id"] for m in left["items"]} == {"c-two"} and [x["chat_id"] for x in left["chats"]] == ["c-two"]
    d = client.delete("/api/runs/run_chat/chat", params={"chat_id": "c-two", "delete": "true", "actor": "Ada"})
    assert d.status_code == 200 and d.json()["removed"] == 2
    assert client.get("/api/runs/run_chat/chat").json()["items"] == []
    log = client.get("/api/runs/run_chat/log").json()
    entries = log.get("entries") or log.get("items") or []
    actions = {e.get("action") for e in entries}
    assert {"chat_stop", "chat_cleared", "chat_deleted"} <= actions, actions


# ------------------------------------------------------------------------------------------ drawer wiring
def test_drawer_has_chat_list_context_chip_stop_and_all_its_texts():
    js = (STATIC / "js" / "chat.js").read_text(encoding="utf-8")
    for needle in ("AbortController", "/chat/stop", "/chat/clear", "client_turn_id", "chat_id", "chat.contextChanged", "chat.contextCleared", "newChat(", "deleteChat(", "clearHistory(", "export function stop(", "export function setChatContext"):
        assert needle in js, needle
    used = set(re.findall(r"\bt\('(chat\.[\w.]*\w)'", js))
    for lang in ("en", "fi", "sv"):
        d = json.loads((STATIC / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        missing = [k for k in used if k not in d]
        assert not missing, f"{lang}: {missing}"
        assert "{ctx}" in d["chat.contextChanged"] and d["chat.stop"].strip()


# ------------------------------------------------------------------------------------------ QA round 5
def test_stop_of_a_named_turn_leaves_the_next_question_of_that_chat_running(client):
    """Stop with a turn id flags only that turn: the next question of the same chat, asked right after Stop, can reach
    the server first (both requests race) and must not be stopped by the chat id the Stop request carries too."""
    agent_mod._turn_begin("CHAT-next", "c-race")                           # the next question is already running
    try:
        st = client.post("/api/runs/run_chat/chat/stop", json={"turn_id": "CHAT-old", "chat_id": "c-race", "actor": "Ada"})
        assert st.status_code == 200 and st.json()["stopped"] == ["CHAT-old"]
        assert not agent_mod.is_stopped("CHAT-next")
        # without a turn id every running turn of the chat stops (what Clear history / Delete chat do)
        st2 = client.post("/api/runs/run_chat/chat/stop", json={"chat_id": "c-race", "actor": "Ada"})
        assert st2.json()["stopped"] == ["CHAT-next"] and agent_mod.is_stopped("CHAT-next")
    finally:
        agent_mod._turn_end("CHAT-next")
        agent_mod._turn_end("CHAT-old")


def test_drawer_keeps_one_context_line_each_chats_draft_and_every_answer_after_its_question():
    js = (STATIC / "js" / "chat.js").read_text(encoding="utf-8")
    for needle in ("function byTurn(", "c.messages.pop()", "inputEl.value = draft", "if (!b && was) inputEl.focus()", "chat.run || state.run",
                   "ctx.object_id || ctx.diagnosis_id", "function sysText(", "setChatContext(null, { cleared: true })"):
        assert needle in js, needle
