"""Round 6 (agent E): the decision log is complete, or says what it leaves out and why.

* DecisionLog.record_many: one transaction per chunk, the hash chain stays linear and verifiable with concurrent
  writers (two connections, several threads), a failed chunk leaves nothing behind, thousands of entries in well under a
  second.
* The quality stage logs every data-quality check and the detect stage every flag (the old 2,000-flag cap is gone), one
  entry each, with ids / status / signals / rows but no readings.
* The completeness audit (`python -m tpm verify-log`): per object type, objects in the artifacts vs objects logged, and
  the reason for every gap, including runs made by an earlier version.
* narrative_coverage: who wrote the explanations (model or template) and why, in one sentence, in three languages."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tpm.config import load_settings  # noqa: E402
from tpm.log.completeness import audit, completeness_statement, format_audit  # noqa: E402
from tpm.log.decision_log import DecisionLog  # noqa: E402
from tpm.log.stage_log import check_entry, flag_entry, log_flags, log_stage_inferences  # noqa: E402
from tpm.workspace import Workspace  # noqa: E402


# ---------------------------------------------------------------------------------------------- record_many
def test_record_many_is_one_chain_and_fast(tmp_path):
    log = DecisionLog(tmp_path / "log.sqlite")
    first = log.record("system:test", "note", "dataset", "x", {"n": 1})
    items = [("system:detect", "flag", "flag", f"FLAG-{i:06d}", {"kind": "anomaly", "row_start": i, "row_end": i + 5, "score": 1.5 + i / 1000}, [f"EV-{i:06d}"]) for i in range(12_000)]
    items[3] = {"actor": "system:quality", "action": "check", "object_type": "check", "object_id": 7, "payload": {"status": "fail"}}  # dict form, int id
    t0 = time.perf_counter()
    out = log.record_many(items, chunk_size=5000)  # three transactions
    seconds = time.perf_counter() - t0
    last = log.record("system:test", "note", "dataset", "y")
    assert len(out) == 12_000 and log.count() == 12_002
    assert out[0].prev_hash == first.hash and last.prev_hash == out[-1].hash
    assert all(out[i].prev_hash == out[i - 1].hash for i in range(1, len(out)))
    assert [e.seq for e in out] == list(range(first.seq + 1, first.seq + 12_001))
    assert out[3].object_id == "7" and log.entries(object_id="7")[0].payload == {"status": "fail"}
    assert log.verify_chain() == {"ok": True, "checked": 12_002, "first_bad_seq": None}
    assert seconds < 5.0, f"record_many of 12,000 entries took {seconds:.1f} s"
    assert log.logged_ids("flag", "flag") >= {"FLAG-000000", "FLAG-011999"}
    with pytest.raises(TypeError):
        log.record_many([("only", "three", "fields")])
    log.close()


def test_record_many_with_concurrent_writers_keeps_the_chain_linear(tmp_path):
    path = tmp_path / "log.sqlite"
    a, b = DecisionLog(path), DecisionLog(path)  # two connections, like the API and the pipeline
    batches: list[list] = []
    errors: list[BaseException] = []

    def bulk(log: DecisionLog, tag: str) -> None:
        try:
            for k in range(15):
                batches.append(log.record_many([(f"system:{tag}", "flag", "flag", f"{tag}-{k}-{i}", {"i": i}) for i in range(200)]))
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    def singles(log: DecisionLog) -> None:
        try:
            for i in range(150):
                log.record("human:ui", "note", "flag", f"single-{i}", {"i": i})
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=bulk, args=(a, "A")), threading.Thread(target=bulk, args=(b, "B")), threading.Thread(target=singles, args=(a,)), threading.Thread(target=singles, args=(b,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert a.count() == 2 * 15 * 200 + 2 * 150
    assert a.verify_chain()["ok"] and b.verify_chain()["ok"]
    for batch in batches:  # one transaction each: nobody wrote in between
        assert [e.seq for e in batch] == list(range(batch[0].seq, batch[0].seq + len(batch)))
    a.close()
    b.close()


def test_a_failed_chunk_leaves_nothing_behind(tmp_path):
    log = DecisionLog(tmp_path / "log.sqlite")
    log.record("system:test", "note", "dataset", "x")
    bad = [("system:detect", "flag", "flag", "FLAG-1", {"ok": 1}), ("system:detect", "flag", "flag", "FLAG-2", {1: "int key", "b": "str key"})]  # cannot be canonicalised
    with pytest.raises(TypeError):
        log.record_many(bad)
    assert log.count() == 1 and log.verify_chain()["ok"]
    log.close()


def test_stage_entries_carry_ids_status_rows_but_no_readings():
    from tpm.contracts import CheckResult, Flag, SignalContribution

    c = CheckResult(check_id="CHK-000001", check_type="stuck", category="consistency", signals=["S01"], batch_id="B00001", status="fail", severity=0.5578193, statement="S01 is frozen at 2715.4837 for 367 samples", evidence_ids=["EV-000291"], values={"value": 2715.4837, "longest_run": 367}, row_start=10, row_end=376)
    actor, action, otype, oid, payload, ev = check_entry(c)
    assert (action, otype, oid, ev) == ("check", "check", "CHK-000001", ["EV-000291"])
    assert payload["status"] == "fail" and payload["signals"] == ["S01"] and payload["row_start"] == 10 and payload["severity"] == 0.558
    assert "values" not in payload and "statement" not in payload and "2715" not in json.dumps(payload)
    f = Flag(id="FLAG-000009", kind="anomaly", group_id="G1", row_start=5, row_end=9, severity=0.7, score=3.14159, detector="ensemble", statement="S01 rose to 2715.4837", signals_ranked=[SignalContribution(signal="S01", contribution=0.6)], evidence_ids=["EV-1"])
    _, action, otype, oid, payload, ev = flag_entry(f)
    assert (action, oid, payload["top_signals"], payload["score"]) == ("flag", "FLAG-000009", ["S01"], 3.1416) and "statement" not in payload
    assert flag_entry(f.model_dump())[4] == payload  # dicts (flags.jsonl rows) give the same entry


# ---------------------------------------------------------------------------------------------- stages log everything
@pytest.fixture(scope="module")
def small_run(tmp_path_factory):
    from tests.fixtures.synth import make_synthetic
    from tpm.pipeline import run_pipeline

    tmp = tmp_path_factory.mktemp("logcomp")
    s = load_settings()
    s.workspace_dir = str(tmp / "ws")
    s.detect.time_budget_s = 60
    s.detect.n_folds = 3
    s.detect.use_autoencoder = False
    df, _ = make_synthetic(n_groups=8, n_samples=200, seed=3)
    src = tmp / "plant.csv"
    df.to_csv(src, index=False)
    st = run_pipeline(str(src), run_id="logcomp", settings=s, stages=["ingest", "profile", "quality", "detect", "diagnose"], options={"no_llm": True, "skip_llm": True, "use_llm": False, "report_llm": False})
    assert st.state == "done", [(x.stage, x.error) for x in st.stages if x.state == "failed"]
    return tmp, s, Workspace(run_id="logcomp", settings=s)


def test_quality_and_detect_log_every_check_and_flag(small_run):
    tmp, s, ws = small_run
    flags, checks = ws.read_jsonl("flags"), ws.read_jsonl("checks")
    assert flags and checks
    assert {f["id"] for f in flags} == ws.log.logged_ids("flag", "flag")
    assert {c["check_id"] for c in checks} == ws.log.logged_ids("check", "check")
    entry = ws.log.entries(action="check", limit=1)[0]
    assert set(entry.payload) >= {"check_type", "status", "signals", "batch_id", "row_start", "row_end"} and "values" not in entry.payload and "statement" not in entry.payload
    det = ws.log.entries(action="stage_summary", object_id="detect")[-1].payload
    assert det["n_flags_logged"] == det["n_flags"] == len(flags) and det["log_seconds"] < 5
    qual = [e.payload for e in ws.log.entries(action="stage", object_id="quality") if e.payload.get("state") == "done" and "n_checks_logged" in e.payload][-1]
    assert qual["n_checks_logged"] == len(checks)
    assert ws.log.verify_chain()["ok"]


def test_completeness_audit_of_a_new_run(small_run):
    tmp, s, ws = small_run
    res = audit(ws)
    rows = {r["object"]: r for r in res["rows"]}
    for kind in ("flag", "check", "trust", "diagnosis", "critique", "pattern"):
        if kind in rows:
            assert rows[kind]["complete"], (kind, rows[kind])
    assert rows["flag"]["n_artifacts"] == rows["flag"]["n_logged"] > 0
    # what is still not logged one by one is named, with its reason (the profile stage's per-signal guesses)
    for kind, r in rows.items():
        for g in r["excluded"]:
            assert g["reason"] and g["n"] and g["examples"], (kind, g)
            assert kind == "inference" and "profile stage" in g["reason"], (kind, g)
    ev = rows["evidence"]
    assert ev["n_logged"] is None and "by design" in ev["note"] and ev["n_cited_by_log"] > 0
    text = "\n".join(format_audit(res))
    assert "Completeness" in text and "Never in the log, on purpose: raw readings" in text
    assert "decision log" in completeness_statement(res)


def test_verify_log_cli_prints_the_audit(small_run):
    tmp, s, ws = small_run
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "TPM_PROFILE")}
    env.update({"TPM_WORKSPACE": s.workspace_dir, "PYTHONIOENCODING": "utf-8", "TPM_NO_DOTENV": "1"})
    r = subprocess.run([sys.executable, "-m", "tpm", "verify-log", "logcomp"], cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("OK: hash chain intact") and "Completeness" in r.stdout and "flags" in r.stdout and "data-quality checks" in r.stdout
    r2 = subprocess.run([sys.executable, "-m", "tpm", "verify-log", "logcomp", "--json"], cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
    data = json.loads(r2.stdout)
    assert data["chain"]["ok"] and {r["object"] for r in data["completeness"]["rows"]} >= {"flag", "check", "evidence"}


# ---------------------------------------------------------------------------------------------- runs of an earlier version
def test_audit_names_the_gaps_of_an_earlier_version(tmp_path):
    s = load_settings()
    ws = Workspace(run_id="old", settings=s, root=tmp_path)
    flags = [{"id": f"FLAG-{i:06d}", "kind": "anomaly", "row_start": i, "row_end": i + 1, "severity": 0.5, "score": 1.2, "detector": "x", "statement": "s", "evidence_ids": [f"EV-{i:06d}"]} for i in range(1, 2501)]
    ws.rewrite_jsonl("flags", flags)
    ws.rewrite_jsonl("checks", [{"check_id": f"CHK-{i:06d}", "check_type": "stuck", "category": "consistency", "batch_id": "B00001", "status": "warn", "statement": "s"} for i in range(1, 41)])
    ws.log.record("system:quality", "stage", "stage", "quality", {"state": "done", "n_checks": 40})  # old stage: no per-check entries
    t0 = time.perf_counter()
    log_flags(ws, flags[:2000])  # the old cap
    assert time.perf_counter() - t0 < 2.0
    ws.log.record("system:detect", "stage_summary", "dataset", "detect", {"n_flags": 2500})
    inf_a = ws.inferences.add("S01", "instrument hypothesis: flow-like (fast, noisy)", status="uncertain", stage="profile")
    inf_b = ws.inferences.add("dataset", "Most deviations are isolated readings", stage="detect")
    ws.log.record("system:profile", "hypotheses", "dataset", ws.run_id, {"n_hypotheses": 1})
    ws.evidence.add("correlation", "S01 and S02 correlate r=0.9", signals=["S01", "S02"], n_samples=500)
    res = audit(ws)
    rows = {r["object"]: r for r in res["rows"]}
    assert (rows["flag"]["n_artifacts"], rows["flag"]["n_logged"], rows["flag"]["n_missing"]) == (2500, 2000, 500)
    assert "at most 2,000 flags" in rows["flag"]["excluded"][0]["reason"] and rows["flag"]["excluded"][0]["examples"][0] == "FLAG-002001"
    assert rows["check"]["n_logged"] == 0 and "one summary entry per batch" in rows["check"]["excluded"][0]["reason"]
    reasons = {g["examples"][0]: g["reason"] for g in rows["inference"]["excluded"]}
    assert "'hypotheses' entry (n_hypotheses=1)" in reasons[inf_a.id] and "stage detect x1" in reasons[inf_b.id]
    assert not res["complete"] and "not logged one by one" in completeness_statement(res)
    # a stage that ends with log_stage_inferences closes its own gap
    assert log_stage_inferences(ws, "detect") == 1 and log_stage_inferences(ws, "detect") == 0
    assert inf_b.id in ws.log.logged_ids("inference")
    ws.close()


# ---------------------------------------------------------------------------------------------- who wrote the explanations
def _diag(i: int, source: str, crit: str = "template") -> dict:
    return {"id": f"DIAG-{i:06d}", "fault_type": "x", "cause_class": "process", "summary": "s", "narrative_source": source, "critique": {"verdict": "supported", "source": crit}}


def test_narrative_coverage_says_who_wrote_what_and_why(tmp_path):
    from tpm.llm import ledger

    s = load_settings()
    ws = Workspace(run_id="cov", settings=s, root=tmp_path)
    diags = [_diag(i, "llm-local:gemma4:e4b-it-qat+template", "llm-local:gemma4:e4b-it-qat+template") for i in range(1, 4)] + [_diag(i, "template") for i in range(4, 3858)]
    ws.rewrite_jsonl("diagnoses", diags)
    ws.log.record("system:diagnose", "note", "dataset", "diagnose", {"note": "LLM narrative/critique applied to the 3 strongest of 3857 diagnoses (LLM time 139s of a 90s allowance); the rest are template-only"})
    (ws.dir / "report_en.html").write_text("<html></html>", encoding="utf-8")
    (ws.dir / "report_llm_en.json").write_text(json.dumps({"status": "ok", "source": "llm-local:gemma4:e4b-it-qat"}), encoding="utf-8")
    (ws.dir / "report_fi.html").write_text("<html></html>", encoding="utf-8")
    cov = ledger.narrative_coverage(ws, s)
    assert cov["diagnoses"] == {"total": 3857, "model": 3, "template": 3854, "by_model": {"local:gemma4:e4b-it-qat": 3}}
    assert cov["critiques"]["model"] == 3 and cov["reason"] == "local_budget" and cov["model_time_allowance_s"] == 90.0
    assert cov["report_summaries"]["by_language"] == {"en": "llm-local:gemma4:e4b-it-qat", "fi": "template"}
    assert cov["sentence"].startswith("3 of 3,857 diagnoses have a model-written explanation (local model gemma4:e4b-it-qat); the other 3,854 use the evidence-based template, by design:")
    assert "90 s here" in cov["sentence"] and cov["sentence"].count(".") <= 3
    assert any("fi: template text only" in d for d in cov["details"])
    assert ledger.coverage_sentence(cov, "fi").startswith("3 diagnoosilla 3 857:stä") and ledger.coverage_sentence(cov, "sv").startswith("3 av 3 857 diagnoser")
    assert "Who wrote the explanations: 3 of 3,857" in ledger.data_flow_statement(ws, s)

    # external route: the per-run cap is the reason; no model at all: --no-llm or nothing reachable
    ws.rewrite_jsonl("diagnoses", [_diag(i, "llm-external:claude-sonnet-5+template") for i in range(1, 13)] + [_diag(i, "template") for i in range(13, 101)])
    cov2 = ledger.narrative_coverage(ws, s)
    assert cov2["reason"] == "external_cap" and "at most 12 diagnoses per run" in cov2["sentence"] and "external model claude-sonnet-5" in cov2["sentence"]
    ws.rewrite_jsonl("diagnoses", [_diag(i, "template") for i in range(1, 11)])
    ws.write_json("meta", {"options": {"no_llm": True}})
    cov3 = ledger.narrative_coverage(ws, s)
    assert cov3["reason"] == "no_llm" and cov3["sentence"].startswith("None of the 10 diagnoses") and "--no-llm" in cov3["sentence"]
    ws.close()
