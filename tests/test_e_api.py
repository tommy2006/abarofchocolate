"""Agent E: FastAPI server against the fake workspace (tests/fixtures/fake_workspace.py).

    .venv\\Scripts\\python.exe -m pytest tests/test_e_* -q
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUN = "run_test_synth"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient

    from tests.fixtures.fake_workspace import build_fake_workspace
    from tpm.api.server import create_app
    from tpm.config import load_settings

    base = tmp_path_factory.mktemp("e_api")
    settings_path = base / "settings.yaml"
    shutil.copy(ROOT / "config" / "settings.yaml", settings_path)
    s = load_settings(settings_path)
    s.workspace_dir = str(base / "workspace")
    ws = build_fake_workspace(s, run_id=RUN, n_groups=8, n_samples=120)
    ws.close()
    app = create_app(settings_path=settings_path, workspace_dir=base / "workspace")
    with TestClient(app) as c:
        yield c


ARTIFACT_ROUTES = {
    "schema": ["available", "n_rows", "signal_alias"],
    "signals": ["available", "signals", "n"],
    "relations": ["available", "correlation", "clusters"],
    "domain": ["available", "likelihood"],
    "understanding": ["available", "summary", "assumptions", "uncertain"],
    "inferences": ["available", "items"],
    "batches": ["available", "batches"],
    "checks": ["available", "items", "summary"],
    "trust": ["available", "items", "untrusted", "overall"],
    "rules": ["available", "rules"],
    "flags": ["available", "items", "kinds", "groups"],
    "patterns": ["available", "patterns"],
    "baseline": ["available", "candidates"],
    "detect_meta": ["available", "detectors"],
    "evaluation": ["available"],
    "diagnoses": ["available", "items"],
    "assessor": ["available", "score", "recommendations"],
    "egress": ["available", "ledger", "summary", "statement"],
    "log": ["available", "items", "count_total"],
    "chat": ["available", "items"],
    "stream/status": ["available", "pushed"],
    "log/verify": ["ok", "checked"],
}


def test_index_and_static(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>Trustworthy Process Monitor</title>" in r.text
    for p in ["/static/app.js", "/static/styles.css", "/static/js/core.js", "/static/i18n/en.json", "/static/i18n/fi.json", "/static/i18n/sv.json"]:
        assert client.get(p).status_code == 200, p
    r = client.get("/static/vendor/plotly.min.js")
    assert r.status_code == 200 and len(r.content) > 100_000


def test_health_settings_runs(client):
    assert client.get("/api/health").json()["ok"] is True
    s = client.get("/api/settings").json()
    for k in ("profile", "profiles", "models", "local_model", "languages", "external_calls", "external_route_exists"):
        assert k in s
    assert set(s["languages"]) >= {"en", "fi", "sv"}
    runs = client.get("/api/runs").json()["runs"]
    assert any(r["run_id"] == RUN for r in runs)
    st = client.get(f"/api/runs/{RUN}/status").json()
    assert st["state"] == "done" and len(st["stages"]) == 7 and st["artifacts"]["flags"] is True
    assert client.get("/api/runs/does_not_exist/status").status_code == 404


@pytest.mark.parametrize("route", sorted(ARTIFACT_ROUTES))
def test_every_get_returns_200_with_keys(client, route):
    r = client.get(f"/api/runs/{RUN}/{route}")
    assert r.status_code == 200, route
    d = r.json()
    for k in ARTIFACT_ROUTES[route]:
        assert k in d, f"{route}: missing {k}"


def test_filters_and_evidence(client):
    d = client.get(f"/api/runs/{RUN}/flags", params={"kind": "anomaly,drift", "min_severity": 0.5}).json()
    assert d["n"] > 0 and all(f["kind"] in ("anomaly", "drift") for f in d["items"])
    d = client.get(f"/api/runs/{RUN}/checks", params={"status": "fail"}).json()
    assert d["n"] > 0 and all(c["status"] == "fail" for c in d["items"])
    ids = d["items"][0]["evidence_ids"]
    e = client.get(f"/api/runs/{RUN}/evidence", params={"ids": ",".join(ids + ["EV-999999"])}).json()
    assert e["n"] == len(ids) and e["missing"] == ["EV-999999"]
    assert e["items"][0]["statement"]


def test_scores_and_series_downsampled(client):
    d = client.get(f"/api/runs/{RUN}/scores", params={"max_points": 50, "detectors": "true"}).json()
    assert d["available"] and 0 < len(d["rows"]) <= 51 and d["bucket"] > 1
    assert d["threshold_value"] is not None and d["signals"] and d["detectors"]
    assert len(d["contrib"][d["signals"][0]]) == len(d["rows"])
    g = client.get(f"/api/runs/{RUN}/scores", params={"group": "2", "max_points": 40}).json()
    assert g["available"] and g["n_rows"] < d["n_rows"]
    s = client.get(f"/api/runs/{RUN}/series", params={"signals": "S01,S02,flow_a,nope", "row_start": 10, "row_end": 500, "max_points": 30}).json()
    assert s["available"] and set(s["series"]) == {"S01", "S02", "flow_a"} and len(s["rows"]) <= 31
    assert s["time"][0] is not None


def test_decisions_log_and_verify(client):
    before = client.get(f"/api/runs/{RUN}/log").json()["count_total"]
    r = client.post(f"/api/runs/{RUN}/decisions", json={"actor_name": "Olli", "role": "operator", "action": "accept", "object_type": "flag", "object_id": "FLAG-000001", "note": "agree"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["log_seq"] > before
    flag = next(f for f in client.get(f"/api/runs/{RUN}/flags").json()["items"] if f["id"] == "FLAG-000001")
    assert flag["human_status"] == "accepted"
    r = client.post(f"/api/runs/{RUN}/decisions", json={"actor_name": "Maija", "role": "engineer", "action": "name_pattern", "object_type": "pattern", "object_id": "PATTERN-A", "new_value": {"name": "Feed surge"}})
    assert r.status_code == 200
    pats = client.get(f"/api/runs/{RUN}/patterns").json()["patterns"]
    assert next(p for p in pats if p["id"] == "PATTERN-A")["name"] == "Feed surge"
    r = client.post(f"/api/runs/{RUN}/decisions", json={"actor_name": "Maija", "role": "engineer", "action": "set_role", "object_type": "signal", "object_id": "S01", "new_value": {"role": "actuator_like"}})
    assert r.status_code == 200
    sig = next(s for s in client.get(f"/api/runs/{RUN}/signals").json()["signals"] if s["id"] == "S01")
    assert sig["human_role_override"] == "actuator_like"
    assert client.post(f"/api/runs/{RUN}/decisions", json={"actor_name": "x", "role": "boss", "action": "accept", "object_type": "flag", "object_id": "F"}).status_code == 422
    log = client.get(f"/api/runs/{RUN}/log", params={"actor": "human:", "action": "accept"}).json()
    assert log["n"] >= 1 and log["items"][-1]["object_id"] == "FLAG-000001"
    v = client.get(f"/api/runs/{RUN}/log/verify").json()
    assert v["ok"] is True and v["checked"] == client.get(f"/api/runs/{RUN}/log").json()["count_total"]
    r = client.get(f"/api/runs/{RUN}/log/export")
    assert r.status_code == 200 and r.content.count(b"\n") >= v["checked"]


def test_rules_compile_501_or_draft(client):
    r = client.post(f"/api/runs/{RUN}/rules", json={"text": "S03 must stay between 100 and 140."})
    if r.status_code == 501:
        assert r.json()["unavailable"] == "tpm.quality.compile_rule"
    else:
        assert r.status_code == 200
        rule = r.json()["rule"]
        assert rule["id"].startswith("RULE-") and rule["status"] in ("draft", "approved", "active")
        assert rule["id"] in {x["id"] for x in client.get(f"/api/runs/{RUN}/rules").json()["rules"]}
    r = client.post(f"/api/runs/{RUN}/rules/RULE-002/approve", json={"actor_name": "Maija", "role": "engineer"})
    assert r.status_code == 200 and r.json()["rule"]["status"] in ("approved", "active")
    r = client.post(f"/api/runs/{RUN}/rules/RULE-002/reject", json={"actor_name": "Maija", "role": "engineer"})
    assert r.status_code == 200 and r.json()["rule"]["status"] == "rejected"
    r = client.post(f"/api/runs/{RUN}/rules/upload", files={"file": ("rules.md", b"# comment\nS03 must stay between 100 and 140.\nS07 must not change by more than 5 per sample.\n")})
    assert r.status_code == 200 and r.json()["n"] == 2
    r = client.post(f"/api/runs/{RUN}/rules/run")
    assert r.status_code in (200, 501)


def test_chat_template_or_model(client):
    ctx = {"object_type": "flag", "object_id": "FLAG-000001", "flag_id": "FLAG-000001"}
    for q in ["why?", "which sensor?", "is it a broken sensor?", "what should I do?"]:
        r = client.post(f"/api/runs/{RUN}/chat", json={"message": q, "context": ctx, "history": [], "actor": "Olli", "role": "operator"})
        assert r.status_code == 200, q
        a = r.json()["answer"]
        assert a["message"] and a["source"]
    r = client.post(f"/api/runs/{RUN}/chat", json={"message": "what is going on?", "context": {}})
    assert r.status_code == 200 and r.json()["answer"]["message"]
    hist = client.get(f"/api/runs/{RUN}/chat").json()
    assert hist["n"] >= 2 and all("message" in m for m in hist["items"])
    assert client.post(f"/api/runs/{RUN}/chat", json={"message": ""}).status_code == 400


def test_assessor_endpoints(client):
    r = client.post(f"/api/runs/{RUN}/assessor/ask", json={"question": "would adding more data help?"})
    assert r.status_code in (200, 501)
    if r.status_code == 200:
        assert r.json()["answer"]["text"]
    r = client.post(f"/api/runs/{RUN}/assessor/apply", json={"action": "REC-002", "actor_name": "Maija", "role": "engineer"})
    assert r.status_code == 200 and r.json()["ok"]
    r = client.post(f"/api/runs/{RUN}/assessor/upload", files={"file": ("cand.csv", b"a,b\n1,2\n")})
    assert r.status_code in (200, 501)


def test_stream_push_and_replay(client):
    r = client.post(f"/api/runs/{RUN}/stream/push", json={"rows": [{"flow_a": 100.0, "press_r": 2700.0}, {"flow_a": 101.0, "press_r": 2701.0}], "batch_id": "PUSH-T1"})
    assert r.status_code == 200 and r.json()["n_rows"] == 2
    r = client.post(f"/api/runs/{RUN}/stream/replay", json={"speed": 100, "max_batches": 2})
    assert r.status_code == 200
    for _ in range(100):
        st = client.get(f"/api/runs/{RUN}/stream/status").json()
        if st["replay"] and st["replay"]["state"] in ("done", "failed", "stopped"):
            break
        time.sleep(0.1)
    assert st["replay"]["state"] == "done", st["replay"]
    assert st["pushed"] >= 3


def test_report_and_email(client):
    r = client.get(f"/api/runs/{RUN}/report", params={"lang": "en"})
    assert r.status_code == 200 and "<html" in r.text.lower()
    r = client.get(f"/api/runs/{RUN}/report", params={"lang": "sv"})
    assert r.status_code in (200, 501)
    r = client.post(f"/api/runs/{RUN}/report/email", json={"to": "someone@example.com", "lang": "fi"})
    assert r.status_code in (200, 500, 501)  # 500 when SMTP is not configured (clear message)
    assert client.post(f"/api/runs/{RUN}/report/email", json={"to": "nope"}).status_code == 400


def test_email_route_passes_the_attachment_choice(client, monkeypatch):
    import tpm.report

    calls = []

    def fake_email_report(ws, settings, to, lang, **kw):
        calls.append({"to": to, "lang": lang, **kw})
        return {"sent": True, "to": [to], "attachments": ["report_fi.html"] + [f"report_fi.{x}" for x in ("pdf", "pptx") if kw.get(f"attach_{x}")]}

    monkeypatch.setattr(tpm.report, "email_report", fake_email_report)
    r = client.post(f"/api/runs/{RUN}/report/email", json={"to": "me@example.com", "lang": "fi", "attach_pdf": True, "attach_pptx": True})
    assert r.status_code == 200 and r.json()["result"]["attachments"] == ["report_fi.html", "report_fi.pdf", "report_fi.pptx"]
    assert calls[-1] == {"to": "me@example.com", "lang": "fi", "attach_pdf": True, "attach_pptx": True}
    client.post(f"/api/runs/{RUN}/report/email", json={"to": "me@example.com", "lang": "fi"})
    assert calls[-1]["attach_pdf"] is False and calls[-1]["attach_pptx"] is False  # an old client still gets the HTML only


def test_sse_yields_an_event(client):
    with client.stream("GET", f"/api/runs/{RUN}/events", params={"max_events": 1}) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes())
    assert body.startswith(b"event: status\ndata: ")
    payload = json.loads(body.split(b"data: ", 1)[1].split(b"\n", 1)[0])
    assert payload["run_id"] == RUN


def test_settings_profile_toggle(client):
    r = client.put("/api/settings", json={"profile": "hybrid"})
    assert r.status_code == 200 and r.json()["profile"] == "hybrid" and r.json()["allow_external"] is True
    assert client.get("/api/settings").json()["profile"] == "hybrid"
    assert client.put("/api/settings", json={"profile": "bogus"}).status_code == 400
    r = client.put("/api/settings", json={"profile": "no-egress"})
    assert r.json()["allow_external"] is False


def test_create_and_delete_run(client, tmp_path):
    src = tmp_path / "tiny.csv"
    src.write_text("a,b,c\n" + "\n".join(f"{i},{i * 2},{i % 3}" for i in range(50)), encoding="utf-8")
    r = client.post("/api/runs", json={"path": str(src), "has_header": True, "language": "fi"})
    assert r.status_code == 202
    rid = r.json()["run_id"]
    for _ in range(600):  # the real pipeline may run when stages exist; wait for it to settle
        st = client.get(f"/api/runs/{rid}/status").json()
        if st["state"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert st["state"] in ("done", "failed") and len(st["stages"]) == 7
    r = client.post("/api/runs", files={"file": ("up.csv", b"x,y\n1,2\n3,4\n")}, data={"has_header": "true"})
    assert r.status_code == 202
    rid2 = r.json()["run_id"]
    for _ in range(600):
        if client.get(f"/api/runs/{rid2}/status").json()["state"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert client.post("/api/runs", json={"path": "C:/definitely/not/here.csv"}).status_code == 400
    for x in (rid, rid2):
        d = client.delete(f"/api/runs/{x}")
        assert d.status_code in (200, 409), d.text
    assert client.get(f"/api/runs/{rid}/status").status_code in (200, 404)


def test_missing_artifacts_are_graceful(client, tmp_path):
    """A run directory with only status.json must answer available:false everywhere, never 500."""
    from tpm.api.server import create_app
    from tpm.contracts import RunStatus
    from tpm.workspace import Workspace
    from tpm.config import load_settings
    from fastapi.testclient import TestClient

    s = load_settings()
    s.workspace_dir = str(tmp_path / "ws2")
    ws = Workspace(run_id="empty_run", settings=s)
    ws.set_status(RunStatus(run_id="empty_run", source_path="x", profile="no-egress", state="failed"))
    ws.close()
    app = create_app(workspace_dir=tmp_path / "ws2")
    with TestClient(app) as c:
        for route in ARTIFACT_ROUTES:
            r = c.get(f"/api/runs/empty_run/{route}")
            assert r.status_code == 200, route
        assert c.get("/api/runs/empty_run/scores").json()["available"] is False
        assert c.get("/api/runs/empty_run/series").json()["available"] is False
        r = c.post("/api/runs/empty_run/chat", json={"message": "hello"})
        assert r.status_code == 200 and r.json()["answer"]["message"]
        assert c.post("/api/runs/empty_run/stream/replay", json={}).status_code == 409
        assert c.get("/api/runs/empty_run/report", params={"lang": "en"}).status_code in (200, 500, 501)


def test_settings_say_where_this_computer_keeps_its_keys(client):
    """Nobody should have to be told the path out loud: the app names the file it reads keys from, the variable the
    active profile needs, and whether the file is there."""
    d = client.get("/api/settings").json()
    assert d["keys_file"].endswith(".env") and isinstance(d["keys_file_exists"], bool)
    assert d["key_variable"], "the active profile's key variable is named"
    from tpm.config import keys_file

    assert d["keys_file"] == str(keys_file())


def test_the_keys_folder_opens_only_from_this_computer(client, monkeypatch):
    """The button opens the folder for the person sitting at the machine; a request from anywhere else is refused and
    the key file itself is never read or sent."""
    from starlette.testclient import TestClient

    from tpm.config import keys_file

    opened = []
    monkeypatch.setattr("os.startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: opened.append(a), raising=False)
    here = TestClient(client.app, client=("127.0.0.1", 40404))
    r = here.post("/api/keys-folder/open", json={})
    assert r.status_code == 200 and r.json()["ok"]
    assert r.json() == {"ok": True, "folder": str(keys_file().parent)}, "only the folder comes back, never the file"
    assert opened, "the folder was handed to the file manager"
    elsewhere = TestClient(client.app, client=("10.0.0.7", 40404))
    assert elsewhere.post("/api/keys-folder/open", json={}).status_code == 403


def test_every_key_the_app_can_use_is_named_with_where_to_get_it(client, monkeypatch):
    """A judge who wants the full experience must not have to guess: the app names every key it can use, the variable
    it reads it from, whether it is there, and where one comes from - and never the value itself."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value-of-mine")
    monkeypatch.delenv("TPM_EU_API_KEY", raising=False)
    monkeypatch.delenv("TPM_SMTP_HOST", raising=False)
    keys = client.get("/api/settings").json()["keys"]
    by_var = {k["variable"]: k for k in keys}
    assert {"ANTHROPIC_API_KEY", "TPM_EU_API_KEY", "TPM_SMTP_PASSWORD"} <= set(by_var), "both external routes and e-mail"
    assert by_var["ANTHROPIC_API_KEY"]["set"] is True and by_var["TPM_EU_API_KEY"]["set"] is False
    assert by_var["TPM_SMTP_PASSWORD"]["set"] is False, "e-mail needs a server as well as a key"
    for k in keys:
        assert k["url"].startswith("https://"), f"{k['variable']} says where a key comes from"
    assert "sk-secret-value-of-mine" not in json.dumps(keys), "presence is reported, never the value"
