"""Hybrid profile, stage call sites (docs/HYBRID_SPEC.md section 5): the concurrent narrative + critique path of the
diagnose stage, guard-clean payloads, and `tpm bench-llm --dry-run`.

No network, no Ollama, no API key: AnthropicProvider.chat is a stub that sleeps and answers from the payload it was
given, the network client raises if anyone builds it, and the local model is reported as not running."""
from __future__ import annotations

import json
import re
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_c_common import detect_run  # noqa: E402
from tpm.config import load_settings  # noqa: E402
from tpm.contracts import SignalDescriptor  # noqa: E402
from tpm.llm import guard, ledger  # noqa: E402
from tpm.llm.providers import AnthropicProvider, OllamaProvider, ProviderError  # noqa: E402
from tpm.workspace import Workspace  # noqa: E402

STUB_SLEEP_S = 0.2


def _copy_run(tmp_path: Path, name: str, profile: str = "hybrid"):
    """A private copy of the shared detect run (flags, evidence, patterns; no diagnoses yet) under its own settings."""
    src, _, _, _ = detect_run(False)
    root = tmp_path / name
    shutil.copytree(src.dir, root / "run", ignore=shutil.ignore_patterns("decision_log.sqlite*", "duck_tmp", "diagnoses.jsonl", "egress_ledger.jsonl", "human_labels.jsonl"))
    settings = load_settings(profile=profile)
    settings.workspace_dir = str(root)
    settings.detect.window = src.settings.detect.window
    return Workspace("run", settings=settings, root=root), settings


def _stub_external(monkeypatch, fail: bool = False) -> list[dict]:
    """AnthropicProvider.chat -> sleeps, then answers deterministically from the diagnosis id and the evidence ids found
    in the message (so a reply can be traced back to its diagnosis). Returns the list of calls it received."""
    calls: list[dict] = []
    lock = threading.Lock()

    def chat(self, messages, schema=None, max_tokens=None, model=None):
        text = "\n".join(str(m.get("content", "")) for m in messages)
        diag = (re.findall(r'"id": "(DIAG-\d+)"', text) or ["DIAG-?"])[0]
        evs = re.findall(r'"id": "(EV-\d+)"', text)
        task = "critique" if "devil's advocate" in text else "diagnosis_narrative"
        with lock:
            calls.append({"task": task, "diag": diag, "thread": threading.current_thread().name, "model": model})
        time.sleep(STUB_SLEEP_S)
        if fail:
            raise ProviderError("stub: the external endpoint is down")
        self.last_usage = {"input_tokens": 100, "output_tokens": 20}
        if task == "critique":
            obj = {"verdict": "weakened", "objections": [{"text": f"model objection to {diag}", "evidence_ids": evs[:1]}], "adjusted_confidence": 0.5, "reasoning": "stub"}
        else:
            obj = {"summary": f"model summary of {diag}", "steps": [f"model step for {diag}"], "uncertainty": [f"model doubt about {diag}"], "confidence": 0.5, "evidence_ids": evs[:2]}
        return json.dumps(obj), obj, int(STUB_SLEEP_S * 1000)

    def no_client(self):
        raise AssertionError("the network client must never be built in this test")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    monkeypatch.setattr(AnthropicProvider, "chat", chat)
    monkeypatch.setattr(AnthropicProvider, "_client", no_client)
    monkeypatch.setattr(OllamaProvider, "is_available", lambda self: False)
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: [])
    return calls


def _comparable(d) -> dict:
    return d.model_dump(exclude={"created_at"})


def _diagnose_records(ws) -> list[tuple[str, str, str]]:
    return [(e.action, e.object_id, e.actor.split(":")[0]) for e in ws.log.entries(object_type="diagnosis")]


def test_concurrent_path_matches_the_sequential_path_and_is_faster(monkeypatch, tmp_path):
    from tpm import diagnose
    from tpm.diagnose import run_diagnose

    calls = _stub_external(monkeypatch)
    ws_par, s_par = _copy_run(tmp_path, "par")
    ws_seq, s_seq = _copy_run(tmp_path, "seq")
    for s in (s_par, s_seq):
        s.external_llm.max_narratives_per_run = 1000
        s.external_llm.max_parallel = 6

    t0 = time.time()
    run_diagnose(ws_par, s_par, {})
    t_par = time.time() - t0
    n_par_calls = len(calls)
    assert len({c["thread"] for c in calls}) > 1 and all(c["thread"].startswith("tpm-diagnose-llm") for c in calls), "the model calls must run in the worker pool"

    monkeypatch.setattr(diagnose, "_external_route_ready", lambda ws, settings: False)  # same stub, today's one-by-one loop
    t0 = time.time()
    run_diagnose(ws_seq, s_seq, {})
    t_seq = time.time() - t0

    par, seq = ws_par.diagnoses(), ws_seq.diagnoses()
    events = [d for d in par if d.fault_type != "isolated suspicious readings"]
    assert len(events) >= 6, "the fixture must give the pool something to do"
    assert [d.id for d in par] == [d.id for d in seq]
    assert [_comparable(d) for d in par] == [_comparable(d) for d in seq], "same ids, fields, narrative, critique and confidence on both paths"
    # every reply landed on the diagnosis it was asked for
    for d in events:
        assert d.narrative_source == "llm-external:claude-sonnet-5+template"
        assert d.summary.endswith(f"model summary of {d.id}") and f"Model explanation: model step for {d.id}" in d.steps
        assert d.critique.source == "llm-external:claude-sonnet-5+template" and d.critique.objections[0].startswith(f"[llm-external:claude-sonnet-5] model objection to {d.id}")
    assert n_par_calls == len(calls) - n_par_calls == 2 * len(events)
    # the same decision-log records per diagnosis, in the same order (narrative, critique, diagnosis), and an intact chain
    assert _diagnose_records(ws_par) == _diagnose_records(ws_seq)
    assert ws_par.log.verify_chain()["ok"]
    ext = [r for r in ledger.read(ws_par) if r.route == "external"]
    assert len(ext) == 2 * len(events) and all(r.ok and r.guard_result == "allowed" for r in ext)
    assert len({r.id for r in ext}) == len(ext)
    # 2 * len(events) sleeping calls one after the other against six at a time
    assert t_par < 0.6 * t_seq, f"concurrent {t_par:.2f}s, sequential {t_seq:.2f}s"
    ws_par.close()
    ws_seq.close()


def test_max_narratives_per_run_is_respected(monkeypatch, tmp_path):
    from tpm.diagnose import run_diagnose

    calls = _stub_external(monkeypatch)
    ws, s = _copy_run(tmp_path, "cap")
    s.external_llm.max_narratives_per_run = 2
    summary = run_diagnose(ws, s, {})
    diags = ws.diagnoses()
    events = [d for d in diags if d.fault_type != "isolated suspicious readings"]
    with_model = [d for d in events if d.narrative_source != "template"]
    assert [d.id for d in with_model] == [d.id for d in events[:2]], "the strongest diagnoses get the model narrative"
    assert sorted(c["task"] for c in calls) == ["critique", "critique", "diagnosis_narrative", "diagnosis_narrative"]
    assert {c["diag"] for c in calls} == {d.id for d in with_model}
    assert all(d.critique is not None and d.critique.objections for d in diags)
    assert all(d.critique.source == "template" and d.narrative_source == "template" for d in events[2:])
    assert summary["n_diagnoses"] == len(diags)
    notes = [e.payload.get("note", "") for e in ws.log.entries(action="note")]
    assert any("2 strongest" in n and "external model" in n for n in notes)
    ws.close()


def test_local_route_keeps_the_sequential_loop(monkeypatch, tmp_path):
    from tpm import diagnose
    from tpm.diagnose import run_diagnose

    calls = _stub_external(monkeypatch)
    monkeypatch.setattr(diagnose, "_model_replies", lambda *a, **k: pytest.fail("the concurrent path must not run on the local route"))
    ws, s = _copy_run(tmp_path, "local", profile="no-egress")
    run_diagnose(ws, s, {})
    diags = ws.diagnoses()
    assert diags and not calls
    assert all(d.narrative_source == "template" and d.critique.source == "template" for d in diags)
    assert not [r for r in ledger.read(ws) if r.route == "external"]
    ws.close()


def test_failing_external_route_stops_the_asking(monkeypatch, tmp_path):
    from tpm.diagnose import run_diagnose

    calls = _stub_external(monkeypatch, fail=True)
    ws, s = _copy_run(tmp_path, "down")
    s.external_llm.max_narratives_per_run = 1000
    s.external_llm.max_parallel = 3
    run_diagnose(ws, s, {})
    diags = ws.diagnoses()
    events = [d for d in diags if d.fault_type != "isolated suspicious readings"]
    assert len(events) >= 6
    # the first workers find the route down; nobody keeps sending two calls per diagnosis after that
    assert 0 < len(calls) <= 2 * s.external_llm.max_parallel < 2 * len(events)
    assert all(d.narrative_source == "template" and d.critique is not None and d.critique.objections for d in diags)
    ws.close()


# ---------------------------------------------------------------------------------------------- payload hygiene


def test_stage_payloads_are_guard_clean(monkeypatch, tmp_path):
    from tpm.contracts import LLMResult
    from tpm.diagnose.critique import objections_payload
    from tpm.profile.roles import build_llm_payload
    from tpm.quality import rules as rules_mod
    import tpm.llm as llm

    fp = {"count": 5000, "mean": 51.2345, "std": 9.87654, "min": 0.0, "max": 100.0, "q05": 33.3333, "q50": 50.1234, "q95": 68.8888, "missing_rate": 0.0, "n_unique": 4100, "autocorr_lag1": 0.98765, "noise_level": 0.12345, "stuck_fraction": 0.0, "hold_period": 1, "distribution_shape": "unimodal", "boundedness": "0-100", "integer_valued": False}
    descriptors = [SignalDescriptor(id=f"S{i:02d}", source_column=f"PT{i}_Secret", column_index=i, dtype="float32", structural_role="continuous_measured", structural_confidence=0.8, fingerprint=dict(fp), instrument_hypothesis="pressure / level-like (intermediate dynamics)") for i in (1, 2)]
    payload = build_llm_payload(descriptors, {"clusters": {"C01": ["S01", "S02"]}, "pairs": [{"a": "S01", "b": "S02", "r": 0.91234, "lag": 2}], "redundancy": []}, {"sensor_stream": 0.9}, None)
    sent_fp = payload["signals"][0]["fingerprint"]
    assert "min" not in sent_fp and "max" not in sent_fp
    assert sent_fp["q05"] == fp["q05"] and sent_fp["q95"] == fp["q95"] and sent_fp["n_samples"] == 5000
    assert "instructions" in payload and "instruction" not in payload
    settings = load_settings(profile="hybrid")
    g = guard.check(payload, settings)
    assert g.allowed, g.reason
    assert not g.sanitizer.get("keys_dropped") and not g.sanitizer.get("fields_dropped") and not g.sanitizer.get("items_dropped"), g.notes
    assert g.sanitized_payload["signals"][0]["fingerprint"]["boundedness"] == "range_0_100"
    assert "Secret" not in json.dumps(g.sanitized_payload)

    # critique: code-written task text travels under "instructions"
    ws, s = _copy_run(tmp_path, "payloads")
    from tpm.diagnose import run_diagnose

    run_diagnose(ws, s, {"options": {"no_llm": True}})
    d = ws.diagnoses()[0]
    crit = objections_payload(d, d.critique.checks, [])
    assert "instructions" in crit and "instruction" not in crit

    # rule_compile: the closed schema goes through schema=, never into the payload
    seen: list[dict] = []

    def fake_complete(task, payload, **kw):
        seen.append({"task": task, "payload": payload, "schema": kw.get("schema")})
        return LLMResult(ok=False, error="captured")

    monkeypatch.setattr(llm, "complete", fake_complete)
    rules_mod.compile_rule(ws, s, "the reactor ought to behave itself most of the time", persist=False)
    assert seen and seen[0]["task"] == "rule_compile"
    rule_payload = seen[0]["payload"]
    assert "$schema" not in json.dumps(rule_payload) and "schema" not in rule_payload
    assert seen[0]["schema"] is rules_mod.RULE_JSON_SCHEMA
    assert all("min" not in sig and "max" not in sig for sig in rule_payload["signal_catalog"])
    g = guard.check(rule_payload, s, ws=ws)
    assert g.allowed and not g.sanitizer.get("keys_dropped"), g.notes
    ws.close()


# ---------------------------------------------------------------------------------------------- tpm bench-llm


def test_bench_llm_dry_run_writes_the_benchmark(monkeypatch, tmp_path, capsys):
    from tpm import cli
    from tpm.llm import bench
    from tests.fixtures.fake_workspace_f import build_fake_workspace

    root = tmp_path / "ws"
    ws = build_fake_workspace(root, run_id="bench_run")
    ledger_before = (ws.dir / "egress_ledger.jsonl").read_bytes() if (ws.dir / "egress_ledger.jsonl").exists() else b""
    ws.close()
    files_before = sorted(p.name for p in (root / "bench_run").iterdir())
    saved = {name: AnthropicProvider.__dict__[name] for name in ("chat", "is_available", "_client")}

    rc = cli.main(["--workspace", str(root), "bench-llm", "--run", "bench_run", "--dry-run", "--profile", "hybrid", "--n", "2"])
    out = capsys.readouterr().out
    assert rc == 0, out
    path = root / "bench_run" / bench.DRY_RUN_FILE
    assert path.exists() and not (root / "bench_run" / bench.BENCH_FILE).exists(), "stub timings never land in the file the UI shows"
    res = json.loads(path.read_text(encoding="utf-8"))
    assert res["dry_run"] is True and res["run_id"] == "bench_run" and res["generated_at"] and res["machine"]["cpu_count"]
    rows = {(r["task"], r["route"]): r for r in res["tasks"]}
    assert set(rows) == {(t, r) for t in bench.BENCH_TASKS for r in ("local", "external")}
    for r in rows.values():
        assert set(r) >= {"task", "route", "model", "n", "ok", "median_s", "mean_s"}
        assert r["n"] == r["ok"] == 2 and r["median_s"] is not None and r["mean_s"] is not None
    assert rows[("critique", "external")]["model"] == "claude-sonnet-5" and rows[("critique", "local")]["model"] == "stub-local"
    turns = {t["route"]: t for t in res["chat"]["turns"]}
    assert turns["local"]["ok"] and turns["external"]["ok"] and turns["external"]["external_calls"] >= 1
    assert set(res["speedup"]["per_task"]) == set(bench.BENCH_TASKS) and res["speedup"]["all_tasks"] > 0
    assert res["concurrent"]["jobs"] == len(bench.BENCH_TASKS) and res["concurrent"]["ok"] == len(bench.BENCH_TASKS)
    assert "median s" in out and "claude-sonnet-5" in out
    # the Data-flow view (GET /api/runs/{id}/llm/usage) folds the per-route rows into one line per task
    from tpm.api.server import _benchmark_rows

    folded = {r["task"]: r for r in _benchmark_rows(res)}
    assert set(folded) == set(bench.BENCH_TASKS) and res["n"] == 2 and res["external_model"] == "claude-sonnet-5" and res["local_model"] == "stub-local"
    assert all(r["local_s"] is not None and r["external_s"] is not None and r["local_ok"] == r["external_ok"] == 2 for r in folded.values())
    # a dry run leaves the run itself alone and puts the real providers back
    assert ((root / "bench_run" / "egress_ledger.jsonl").read_bytes() if (root / "bench_run" / "egress_ledger.jsonl").exists() else b"") == ledger_before
    assert sorted(p.name for p in (root / "bench_run").iterdir() if p.name != bench.DRY_RUN_FILE and not p.name.startswith("decision_log.sqlite")) == [n for n in files_before if not n.startswith("decision_log.sqlite")]
    assert all(AnthropicProvider.__dict__[name] is fn for name, fn in saved.items())


def test_bench_llm_does_not_open_a_closed_route(monkeypatch, tmp_path):
    from tpm.llm import bench
    from tests.fixtures.fake_workspace_f import build_fake_workspace

    root = tmp_path / "ws"
    ws = build_fake_workspace(root, run_id="bench_closed")
    settings = load_settings(profile="no-egress")
    settings.workspace_dir = str(root)
    res = bench.run_benchmark(ws, settings, tasks=["critique"], n=1, chat=False, dry_run=True)
    ext = [r for r in res["tasks"] if r["route"] == "external"]
    assert ext and all(r["n"] == 0 and r["ok"] == 0 and "skipped" in r for r in ext)
    assert [r["ok"] for r in res["tasks"] if r["route"] == "local"] == [1]
    # the settings the caller passed in are untouched
    assert settings.profile == "no-egress" and settings.route_for("critique") == "local"
    forced = bench.forced_route(load_settings(profile="hybrid"), "local", ["critique"])
    assert forced.route_for("critique") == "local" and load_settings(profile="hybrid").route_for("critique") == "external"
    ws.close()
