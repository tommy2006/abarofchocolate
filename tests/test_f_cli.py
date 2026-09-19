"""Agent F: `python -m tpm` command line. Runs the real entry point in a subprocess with an isolated workspace."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str], workspace: Path, timeout: int = 240, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TPM_WORKSPACE"] = str(workspace)
    env["PYTHONIOENCODING"] = "utf-8"
    for var in ("TPM_SMTP_HOST", "TPM_SMTP_USER", "TPM_SMTP_PASSWORD", "TPM_SMTP_FROM", "TPM_PROFILE"):
        env.pop(var, None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, "-m", "tpm", *args], cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


@pytest.fixture(scope="module")
def samples():
    sys.path.insert(0, str(ROOT))
    from scripts.make_samples import make_all

    return make_all(ROOT / "samples")


def test_help_and_parse_opt():
    from tpm.cli import _parse_opt, build_parser

    assert build_parser().format_help().count("run") >= 1
    assert _parse_opt("has_header=false") == ("has_header", False)
    assert _parse_opt("delimiter=;") == ("delimiter", ";")
    assert _parse_opt("group_columns=a,b") == ("group_columns", ["a", "b"])
    assert _parse_opt("n=3") == ("n", 3) and _parse_opt("x=1.5") == ("x", 1.5)
    assert _parse_opt('opts={"a":1}') == ("opts", {"a": 1})
    with pytest.raises(Exception):
        _parse_opt("novalue")


def test_doctor_runs(tmp_path):
    r = _run(["doctor"], tmp_path / "ws")
    assert r.returncode in (0, 1), r.stderr
    assert "doctor" in r.stdout and "Python" in r.stdout and "problem(s)" in r.stdout
    assert "report e-mail not configured" in r.stdout  # the test environment never sees the developer's .env


def test_doctor_checks_the_mail_settings(monkeypatch):
    import socket

    from tpm.cli import _check_email
    from tpm.config import load_settings

    s = load_settings()
    msgs: list[tuple[str, str]] = []
    ok = lambda m: msgs.append(("ok", m))  # noqa: E731
    warn = lambda m, fix="": msgs.append(("warn", m))  # noqa: E731
    _check_email(s, ok, warn)
    assert len(msgs) == 1 and msgs[0][0] == "warn" and "not configured" in msgs[0][1]
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)  # never accept()ed: the probes stay queued, so the queue must hold all of them (Windows refuses otherwise)
    port = srv.getsockname()[1]
    try:
        monkeypatch.setenv("TPM_SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("TPM_SMTP_PORT", str(port))
        msgs.clear()
        _check_email(s, ok, warn)
        assert [k for k, _ in msgs] == ["warn", "ok"] and "sender" in msgs[0][1] and "reachable" in msgs[1][1]
        monkeypatch.setenv("TPM_SMTP_FROM", "onboarding@resend.dev")
        msgs.clear()
        _check_email(s, ok, warn)
        assert msgs == [("ok", f"report e-mail: 127.0.0.1:{port} reachable, sender onboarding@resend.dev")]
    finally:
        srv.close()
    monkeypatch.setenv("TPM_SMTP_PORT", str(port))  # nothing listens there any more
    msgs.clear()
    _check_email(s, ok, warn)
    assert msgs[-1][0] == "warn" and "not reachable" in msgs[-1][1]


def test_models_runs(tmp_path):
    r = _run(["models"], tmp_path / "ws")
    assert r.returncode == 0, r.stderr
    assert "Ollama" in r.stdout and "External model" in r.stdout


def test_run_ingest_stage_on_sample(tmp_path, samples):
    ws = tmp_path / "ws"
    r = _run(["run", str(samples["process"]), "--stages", "ingest", "--run-id", "t_ingest", "--no-llm", "--quiet"], ws)
    status_file = ws / "t_ingest" / "status.json"
    assert status_file.exists(), r.stdout + r.stderr
    st = json.loads(status_file.read_text(encoding="utf-8"))
    assert st["state"] == "done", st
    assert r.returncode == 0, r.stdout + r.stderr
    stages = {s["stage"]: s["state"] for s in st["stages"]}
    assert stages["ingest"] in ("done", "skipped")
    assert stages["report"] == "skipped"
    assert "Workspace:" in r.stdout and "t_ingest" in r.stdout


def test_run_rejects_missing_input_and_unknown_stage(tmp_path):
    r = _run(["run", "does_not_exist.csv"], tmp_path / "ws")
    assert r.returncode == 1 and "not found" in r.stderr
    r = _run(["run", "samples/demo_process.csv", "--stages", "bogus"], tmp_path / "ws")
    assert r.returncode == 1 and "unknown stage" in r.stderr


def test_report_export_verify_email_on_fake_workspace(tmp_path):
    from tests.fixtures.fake_workspace_f import build_fake_workspace

    ws_root = tmp_path / "ws"
    ws = build_fake_workspace(ws_root, run_id="fake_cli")
    ws.close()

    r = _run(["report", "fake_cli", "--lang", "all", "--no-llm"], ws_root)
    assert r.returncode == 0, r.stderr
    for lang in ("en", "fi", "sv"):
        assert (ws_root / "fake_cli" / f"report_{lang}.html").exists()

    r = _run(["report", "latest", "--lang", "fi", "--no-llm", "--out", str(tmp_path / "custom.html")], ws_root)
    assert r.returncode == 0 and (tmp_path / "custom.html").exists()

    r = _run(["verify-log", "fake_cli"], ws_root)
    assert r.returncode == 0 and "OK" in r.stdout

    r = _run(["export", "fake_cli", "--out", str(tmp_path / "exp")], ws_root)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "exp" / "fake_cli_export.zip").exists()
    assert "decision_log.jsonl" in r.stdout and "report_en.html" in r.stdout

    r = _run(["list"], ws_root)
    assert r.returncode == 0 and "fake_cli" in r.stdout

    r = _run(["email", "fake_cli", "--to", "a@b.c"], ws_root)
    assert r.returncode == 1 and "TPM_SMTP_HOST" in r.stderr

    r = _run(["verify-log", "nope"], ws_root)
    assert r.returncode == 1 and "not found" in r.stderr


def test_rules_file_is_loaded_into_workspace(tmp_path, samples):
    ws = tmp_path / "ws"
    rules = tmp_path / "rules.txt"
    rules.write_text("# comment\nS03 must stay between 100 and 140.\n\nS07 must not change by more than 5 per sample.\n", encoding="utf-8")
    r = _run(["run", str(samples["process"]), "--stages", "ingest", "--run-id", "t_rules", "--rules", str(rules), "--no-llm", "--quiet"], ws)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Loaded 2 rule(s)" in r.stdout
    meta = json.loads((ws / "t_rules" / "meta.json").read_text(encoding="utf-8"))
    assert meta["options"]["rules"] == ["S03 must stay between 100 and 140.", "S07 must not change by more than 5 per sample."]
    assert meta["options"]["rules_file"].endswith("rules.txt")
    # compiled once, by the quality stage (not in --stages here): no uncompiled drafts that would later be duplicated
    assert "quality stage is not in --stages" in r.stdout
    assert not (ws / "t_rules" / "rules.json").exists()


def test_samples_exist_and_are_small(samples):
    for k, p in samples.items():
        assert p.exists() and p.stat().st_size < 3_000_000, k
    head = samples["headerless"].read_text(encoding="utf-8").splitlines()[0]
    assert "," not in head and len(head.split()) >= 10  # whitespace separated, numeric
    rec = samples["records"].read_text(encoding="utf-8").splitlines()[0]
    assert rec.startswith("order_id,created_at")
