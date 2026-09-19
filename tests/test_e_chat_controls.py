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


# ------------------------------------------------------------------------------------------ finishing pass
def test_clear_in_one_run_never_stops_a_chat_of_the_same_name_in_another_run(client):
    """Every run's first chat is "default": Clear / Delete / Stop by chat id in run B must leave run A's answer alone."""
    agent_mod._turn_begin("CHAT-runA1", "default", "run_a")
    agent_mod._turn_begin("CHAT-runB1", "default", "run_chat")
    try:
        c = client.post("/api/runs/run_chat/chat/clear", json={"chat_id": "default", "actor": "Bob"})
        assert c.status_code == 200
        assert agent_mod.is_stopped("CHAT-runB1") and not agent_mod.is_stopped("CHAT-runA1")
        assert agent_mod.request_stop(None, "default", "run_other") == []
    finally:
        agent_mod._turn_end("CHAT-runA1")
        agent_mod._turn_end("CHAT-runB1")


def test_an_invalid_chat_id_is_refused_instead_of_clearing_the_default_chat(client, monkeypatch):
    _no_ollama(monkeypatch)
    client.post("/api/runs/run_chat/chat", json={"message": "kept in the default chat", "chat_id": "default"})
    n = len(client.get("/api/runs/run_chat/chat", params={"chat_id": "default"}).json()["items"])
    assert n >= 2
    for bad in ("chat two", "../x", "x" * 100, ""):
        assert client.post("/api/runs/run_chat/chat/clear", json={"chat_id": bad, "delete": True}).status_code == 400, bad
    assert client.delete("/api/runs/run_chat/chat", params={"chat_id": "x" * 100}).status_code == 400
    assert client.post("/api/runs/run_chat/chat/stop", json={"chat_id": "chat two"}).status_code == 400
    assert len(client.get("/api/runs/run_chat/chat", params={"chat_id": "default"}).json()["items"]) == n


def test_clear_keeps_a_turn_of_another_chat_saved_while_it_runs(tmp_path):
    """Read, filter and rewrite happen under the workspace lock: an answer of another chat appended at that moment
    (another tab) is not lost."""
    import threading

    from tpm.api import fallback
    from tpm.workspace import Workspace

    ws = Workspace(run_id="clear_race", root=tmp_path)
    ws.rewrite_jsonl("chat", [{"chat_id": "keep", "role": "user", "content": "q"}, {"chat_id": "drop", "role": "user", "content": "x"}])
    real_read = Workspace.read_jsonl
    appended = threading.Event()

    def slow_read(self, artifact):
        rows = real_read(self, artifact)
        th = threading.Thread(target=lambda: (self.append_jsonl("chat", {"chat_id": "keep", "role": "assistant", "content": "a"}), appended.set()))
        th.start()
        th.join(0.3)                      # the append waits for the lock the clear holds
        return rows

    Workspace.read_jsonl = slow_read
    try:
        for fn in (fallback.clear_chat, agent_mod.clear_chat):
            appended.clear()
            ws.append_jsonl("chat", {"chat_id": "drop", "role": "user", "content": "y"})
            assert fn(ws, "drop") >= 1
            assert appended.wait(5)
    finally:
        Workspace.read_jsonl = real_read
    rows = ws.read_jsonl("chat")
    assert [r["role"] for r in rows if r["chat_id"] == "keep"] == ["user", "assistant", "assistant"] and not [r for r in rows if r["chat_id"] == "drop"]


def test_a_turn_stopped_by_clear_history_is_not_saved_back_into_the_cleared_chat(demo, monkeypatch):
    ws, s = demo
    turn = "CHAT-late"

    def clear_while_the_model_works():
        agent_mod.clear_chat(ws, "c-late")
        agent_mod.discard_running("c-late", ws.run_id)            # Clear history pressed while the model works
        return {"action": "final", "answer": "an answer nobody wants any more", "citations": []}
    _fake_model(monkeypatch, [clear_while_the_model_works])
    out = agent_mod.chat(ws, s, "a slow question", chat_id="c-late", client_turn_id=turn, max_steps=2)
    assert out["stopped"] is True
    assert agent_mod.chat_history(ws, chat_id="c-late") == [], "the cleared chat stays empty"
    assert turn not in agent_mod._DISCARD and not agent_mod.is_stopped(turn)


def test_the_answers_follow_up_questions_are_saved_with_it(demo, monkeypatch):
    ws, s = demo
    _no_ollama(monkeypatch)
    out = agent_mod.chat(ws, s, "Why was FLAG-000001 raised?", context={"flag_id": "FLAG-000001"}, chat_id="c-fu")
    saved = agent_mod.chat_history(ws, chat_id="c-fu")[-1]
    assert out["suggested_followups"] and saved["followups"] == out["suggested_followups"]


@pytest.mark.parametrize("first_piece_after", [0.1, 5.0])
def test_stop_ends_the_local_model_call_itself_and_closes_the_connection(first_piece_after):
    """Stop during a model step: the local model is asked in streaming mode inside a chat turn, and the connection is
    dropped as soon as Stop is pressed, while it writes (0.1 s) or while it still reads the prompt (5 s). Ollama stops
    generating when its client goes away, so a question asked right after Stop does not wait behind the old step."""
    import http.server
    import threading
    import time

    from tpm.config import load_settings
    from tpm.llm import providers

    seen = {"disconnected": threading.Event(), "body": None}

    class Slow(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # quiet
            pass

        def do_POST(self):
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            time.sleep(first_piece_after)
            try:
                for i in range(100):
                    line = (json.dumps({"message": {"content": f"word{i} "}, "done": False}) + "\n").encode()
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                    self.wfile.flush()
                    time.sleep(0.2)
            except OSError:
                seen["disconnected"].set()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        s = load_settings(profile="no-egress")
        s.local_llm.base_url = f"http://127.0.0.1:{srv.server_address[1]}"
        p = providers.OllamaProvider(s)
        p.capabilities = lambda model: []
        t0 = time.time()
        with providers.cancellable(lambda: time.time() - t0 > 0.6):
            with pytest.raises(providers.ProviderError, match=providers.STOPPED):
                p.chat([{"role": "user", "content": "hi"}], model="m")
        assert time.time() - t0 < 3.0, "the call ended soon after Stop"
        assert seen["body"]["stream"] is True
        assert seen["disconnected"].wait(8), "the model server saw the client go away"
    finally:
        srv.shutdown()


def test_a_streamed_model_step_that_is_not_stopped_returns_the_whole_answer():
    import http.server
    import threading

    from tpm.config import load_settings
    from tpm.llm import providers

    pieces = ['{"action": "final", ', '"answer": "All ', 'good."}']

    class Quick(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = "".join(json.dumps({"message": {"content": c}, "done": False}) + "\n" for c in pieces) + json.dumps({"message": {"content": ""}, "done": True}) + "\n"
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quick)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        s = load_settings(profile="no-egress")
        s.local_llm.base_url = f"http://127.0.0.1:{srv.server_address[1]}"
        p = providers.OllamaProvider(s)
        p.capabilities = lambda model: []
        with providers.cancellable(lambda: False):
            text, parsed, _ = p.chat([{"role": "user", "content": "hi"}], schema={"type": "object"}, model="m")
        assert text == "".join(pieces) and parsed == {"action": "final", "answer": "All good."}
    finally:
        srv.shutdown()
