"""Round 6 (agent E): the egress guard proven on a run, not just described.

* Tennessee-Eastman style headers (xmeas_1 .. xmeas_41, xmv_1 .. xmv_11, faultNumber, simulationRun, sample): every
  externally routed task, the chat (questions that name the headers) and rule text, in the eu-hosted profile (strict
  guard, fake EU endpoint) and in hybrid, with AnthropicProvider.chat replaced by a recorder. None of the header strings
  may reach the provider, nor the ledger previews of what would have been sent.
* `tpm guard-demo`: a real payload before / after the guard, an operator question, a deliberately unsafe payload that is
  blocked and never sent, ledger records that no count of real calls includes, guard_demo.json, the report block;
  --send sends the SAFE payload once through the normal router and nothing else.
* Local calls in audit mode: nothing removed, the ledger says what the guard would have removed and why local calls may
  see more.
* Raw rows written as text, and payloads stripped to empty skeletons, never leave.
No network, no Ollama, no API key: providers are stubs."""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tpm.config import load_settings  # noqa: E402
from tpm.llm import guard, guard_demo, ledger, router  # noqa: E402
from tpm.llm.providers import AnthropicProvider, OllamaProvider, ProviderError  # noqa: E402
from tpm.workspace import Workspace  # noqa: E402

FILE_NAME = "te_style_secret_plant.csv"
XMEAS = [f"xmeas_{i}" for i in range(1, 42)]
XMV = [f"xmv_{i}" for i in range(1, 12)]
HEADERS = ["faultNumber", "simulationRun", "sample"] + XMEAS + XMV
EXTERNAL_TASKS = {"sensor_hypotheses", "rule_compile", "diagnosis_narrative", "critique", "report_narrative", "why_chat", "assessor_chat"}


def make_te_like(n_runs: int = 6, n_samples: int = 200, seed: int = 11) -> pd.DataFrame:
    """Tennessee-Eastman style layout: faultNumber, simulationRun, sample (1..n per run), 41 measured and 11
    manipulated variables driven by three latent processes; the second half of the runs gets a fault from sample 100
    (a step on a few measured and one manipulated variable)."""
    rng = np.random.default_rng(seed)
    frames = []
    levels = rng.uniform(0.2, 4000.0, len(XMEAS) + len(XMV))
    gains = rng.normal(0.0, 1.0, (len(XMEAS) + len(XMV), 3))
    for run in range(1, n_runs + 1):
        faulty = run > n_runs // 2
        latent = np.zeros((n_samples, 3))
        for t in range(1, n_samples):
            latent[t] = 0.9 * latent[t - 1] + rng.normal(0.0, 0.3, 3)
        x = levels * (1.0 + 0.01 * (latent @ gains.T)) + rng.normal(0.0, 0.002, (n_samples, len(levels))) * levels
        if faulty:
            x[100:, [0, 8, 20]] *= 1.08
            x[100:, len(XMEAS) + 9] *= 0.9
        df = pd.DataFrame(x, columns=XMEAS + XMV)
        df.insert(0, "sample", np.arange(1, n_samples + 1))
        df.insert(0, "simulationRun", float(run))
        df.insert(0, "faultNumber", np.where((np.arange(n_samples) >= 100) & faulty, 1.0, 0.0))
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


@pytest.fixture(scope="module")
def te_run(tmp_path_factory):
    from tpm.pipeline import run_pipeline

    tmp = tmp_path_factory.mktemp("te_guard")
    s = load_settings()
    s.workspace_dir = str(tmp / "ws")
    s.detect.time_budget_s = 60
    s.detect.n_folds = 3
    s.detect.use_autoencoder = False
    df = make_te_like()
    src = tmp / FILE_NAME
    df.to_csv(src, index=False)
    st = run_pipeline(str(src), run_id="te_src", settings=s, stages=["ingest", "profile", "quality", "detect", "diagnose"], options={"no_llm": True, "skip_llm": True, "use_llm": False, "report_llm": False})
    assert st.state == "done", [(x.stage, x.error) for x in st.stages if x.state == "failed"]
    ws = Workspace(run_id="te_src", settings=s)
    assert ws.diagnoses() and ws.flags(), "the TE-style run must produce flags and diagnoses"
    assert set(ws.schema().signal_alias) >= set(XMEAS[:5])
    return tmp, df, ws


def _copy(te_run, name: str, profile: str):
    tmp, df, src_ws = te_run
    root = tmp / name
    shutil.copytree(src_ws.dir, root / "te_run", ignore=shutil.ignore_patterns("decision_log.sqlite*", "duck_tmp"))
    settings = load_settings(profile=profile)
    settings.workspace_dir = str(root)
    if profile == "eu-hosted":
        settings.external_llm.base_url = "https://eu.example-llm.invalid/v1"  # fake EU endpoint; the network client is never built
    return Workspace("te_run", settings=settings, root=root), settings, df


def _recorder(monkeypatch, sent: list[dict]) -> None:
    """AnthropicProvider.chat records every message list. Agent steps get a final answer (so a chat turn completes),
    a diagnosis narrative gets a valid reply (so --send completes), everything else is refused after recording."""

    def fake(self, messages, schema=None, max_tokens=None, model=None):
        sent.append({"model": model or self.cfg.model, "messages": [dict(m) for m in messages], "schema": schema})
        props = (schema or {}).get("properties") or {}
        if "action" in props:
            step = {"thought": "done", "action": "final", "answer": "S01 moved before S51; see the flag.", "citations": [], "confidence": 0.5, "suggested_followups": []}
            self.last_usage = {"input_tokens": 50, "output_tokens": 10}
            return json.dumps(step), step, 3
        if "summary" in props and "steps" in props:
            reply = {"summary": "The strongest deviation starts in S01.", "steps": ["S01 rose first."], "uncertainty": [], "confidence": 0.5, "evidence_ids": []}
            self.last_usage = {"input_tokens": 80, "output_tokens": 20}
            return json.dumps(reply), reply, 5
        raise ProviderError("recorded, not answered")

    def no_client(self):
        raise AssertionError("the network client must never be built in this test")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    monkeypatch.setattr(AnthropicProvider, "chat", fake)
    monkeypatch.setattr(AnthropicProvider, "_client", no_client)
    monkeypatch.setattr(OllamaProvider, "is_available", lambda self: False)
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: [])


def _header_hits(text: str) -> list[str]:
    """Any trace of a Tennessee-Eastman header. 'sample' is an ordinary English word ("30 samples"); as a header it would
    appear as a JSON string value of its own ("order_column": "sample")."""
    low = text.lower()
    hits = [w for w in ("xmeas", "xmv", "faultnumber", "simulationrun") if w in low]
    if re.search(r'"sample"(?!\s*:)', text):
        hits.append('"sample" as a value')
    if FILE_NAME.lower() in low or Path(FILE_NAME).stem.lower() in low:
        hits.append("file name")
    return hits


def _exercise(ws: Workspace, settings, df: pd.DataFrame) -> None:
    from tpm.api.plain import plain_for
    from tpm.diagnose import _load_context
    from tpm.diagnose.critique import critique_diagnosis
    from tpm.diagnose.diagnosis import add_llm_narrative
    from tpm.llm import agent as agent_mod
    from tpm.profile.roles import llm_hypotheses
    from tpm.quality.rules import compile_rule
    from tpm.report.report import generate_report

    schema = ws.schema()
    llm_hypotheses(ws, settings, ws.signals(), ws.read_json("relations", {}) or {}, dict(schema.domain_likelihood or {}), f"operator hint: xmeas_1 is the A feed, xmv_10 the reactor cooling valve, faultNumber marks the faults, simulationRun the campaign; file {FILE_NAME}")
    for text in ("xmeas_1 must stay below 0.4 when xmv_10 is above 40", "xmeas_9 and xmeas_21 ought to move together unless faultNumber is 1 in simulationRun 4"):
        compile_rule(ws, settings, text, persist=False)
    c = _load_context(ws, settings)
    flags_by_id = {f.id: f for f in ws.flags()}
    for d in ws.diagnoses()[:2]:
        add_llm_narrative(ws, settings, d, "en")
        critique_diagnosis(ws, settings, d, flags_by_id, c["baseline"], c["patterns"], int(settings.detect.window), "en", use_llm=True)
    generate_report(ws, settings, "en", use_llm=True, out_path=ws.dir / "te_guard_report_en.html", llm_wait_s=20)
    for view in ("understanding", "diagnoses", "dataflow"):
        plain_for(ws, settings, view, "en", enhance=True)
    flag = ws.flags()[0]
    raw = float(df["xmeas_1"].iloc[150])
    agent_mod.chat(ws, settings, f"Why did xmeas_1 reach {raw!r} before xmv_10 moved, while faultNumber was 1 in simulationRun 4 (sample 150, file {FILE_NAME})?", context={"flag_id": flag.id}, history=[{"role": "user", "content": "what is xmeas_9?"}, {"role": "assistant", "content": "xmeas_9 (S09) is a temperature-like signal."}])
    agent_mod.chat(ws, settings, "Can I drop xmeas_22 and xmv_5 from simulationRun 2?", task="assessor_chat")


@pytest.mark.parametrize("profile", ["eu-hosted", "hybrid"])
def test_te_headers_never_reach_the_provider(monkeypatch, te_run, profile):
    ws, s, df = _copy(te_run, f"headers_{profile}", profile)
    assert s.active_profile.guard_strict is (profile == "eu-hosted")
    assert s.external_block_reason("diagnosis_narrative") is None, "the eu-hosted route must be usable with the fake EU endpoint"
    sent: list[dict] = []
    _recorder(monkeypatch, sent)
    _exercise(ws, s, df)

    ext = [r for r in ledger.read(ws) if r.route == "external"]
    assert EXTERNAL_TASKS <= {r.task for r in ext}, f"every external task must be attempted: {sorted({r.task for r in ext})}"
    assert not [(r.task, r.guard_reason) for r in ext if r.guard_result not in ("allowed",)], "no external task may be stopped before sending"
    assert len(sent) >= len(EXTERNAL_TASKS)
    problems = []
    for i, call in enumerate(sent):
        text = "\n".join(str(m.get("content", "")) for m in call["messages"])
        problems += [f"call {i}: {h}" for h in _header_hits(text)]
    for r in ext:
        problems += [f"ledger preview {r.id} ({r.task}): {h}" for h in _header_hits(r.payload_preview)]
    assert not problems, "Tennessee-Eastman headers in outgoing text:\n" + "\n".join(sorted(set(problems))[:30])
    # the scan is not vacuous: questions, rule text and hints did name the headers, and the aliases went out instead
    everything = "\n".join(str(m.get("content", "")) for call in sent for m in call["messages"])
    assert "S01" in everything and ("[column]" in everything) and "[file]" in everything
    assert any("xmeas" in (json.dumps(x, default=str).lower()) for x in (ws.read_json("schema"), ws.read_json("signals")))


@pytest.mark.parametrize("profile", ["eu-hosted", "hybrid"])
def test_guard_demo_on_te_style_run(monkeypatch, te_run, profile):
    ws, s, df = _copy(te_run, f"demo_{profile}", profile)
    sent: list[dict] = []
    _recorder(monkeypatch, sent)
    n_before = len(ledger.read(ws))
    res = guard_demo.run_demo(ws, s, profile=profile)
    assert not sent, "without --send nothing reaches the provider"

    safe, q, unsafe = res["safe"], res["question"], res["unsafe"]
    assert res["profile"] == profile and res["strict"] is (profile == "eu-hosted")
    assert safe["verdict"] == "allowed" and safe["task"] == "diagnosis_narrative" and safe["about"]["id"].startswith("DIAG-")
    assert safe["bytes_after"] > 0 and not safe["headers_in_output"]
    # the operator question names original headers and the file; all of them are gone after the guard
    named = [h for h in HEADERS if re.search(rf"(?<![A-Za-z0-9_]){h}(?![A-Za-z0-9_])", q["before"])]
    assert len(named) >= 2 and FILE_NAME in q["before"]
    assert q["verdict"] == "allowed" and not _header_hits(q["after"]) and re.search(r"\bS\d\d\b", q["after"]) and "[file]" in q["after"]
    assert {a["original"] for a in q["aliased"]} >= set(named)
    # the deliberately unsafe payload: real rows under the original headers, blocked with the reason, layer by layer
    w = unsafe["what"]
    assert w["real_rows"] and w["n_rows"] == 3 and w["n_headers"] >= len(HEADERS) and w["n_readings"] >= 150 and w["file_name"] == FILE_NAME
    assert unsafe["verdict"] == "blocked" and "nothing useful left" in unsafe["reason"] and unsafe["bytes_after"] == 0
    layers = "\n".join(unsafe["layers"])
    for expected in ("unknown top-level key 'rows'", "row-like structure", "numeric series of 50 points", "raw row written as text", "timestamp", "source_path"):
        assert expected in layers, expected
    assert unsafe["n_invariant_on_raw"] >= 10 and any("original column name" in v for v in unsafe["invariant_on_raw"])
    assert not res["headers_check"]["found"] and res["headers_check"]["n_headers"] >= len(HEADERS)

    # three demonstration records: shown, never sent, and no count of real calls includes them
    recs = ledger.read(ws)[n_before:]
    assert [r.guard_result for r in recs] == ["demo_allowed", "demo_allowed", "demo_blocked"]
    assert all(r.purpose.startswith("guard demonstration") and r.route == "external" for r in recs)
    assert recs[0].payload_preview and not _header_hits(recs[0].payload_preview) and recs[2].payload_preview == ""
    assert ledger.summary(ws)["n_external_sent"] == 0 and ledger.summary(ws)["n_blocked"] == 0 and ledger.summary(ws)["n_demo"] == 3
    assert ledger.usage(ws, s)["external_calls"] == 0 and router._budget(ws, s)[0]
    stmt = ledger.data_flow_statement(ws, s)
    assert "Guard demonstration" in stmt and "Nothing of the demonstration was sent" in stmt and "Nothing left the operator environment" in stmt
    assert ws.log.entries(action="egress", object_id=recs[2].id), "the demonstration is in the hash-chained decision log too"

    # guard_demo.json, the console text and the report block (three languages)
    on_disk = json.loads((ws.dir / guard_demo.DEMO_FILE).read_text(encoding="utf-8"))
    assert on_disk["unsafe"]["verdict"] == "blocked" and on_disk["safe"]["ledger_id"] == recs[0].id
    raw_cell = repr(float(df["xmeas_1"].iloc[0]))
    assert raw_cell not in json.dumps(on_disk), "no reading of the unsafe payload is copied into guard_demo.json"
    console = "\n".join(guard_demo.format_demo(res))
    assert "blocked" in console and "never sent" in console and "xmeas_1" in console and "S01" in console
    for lang, word in (("en", "Egress guard demonstration"), ("fi", "esittely"), ("sv", "Demonstration")):
        ctx = guard_demo.report_context(ws, lang)
        assert ctx and word in ctx["title"] and ctx["unsafe_ok"] and ctx["question"]["after"] == q["after"]


def test_guard_demo_send_sends_only_the_safe_payload_once(monkeypatch, te_run):
    ws, s, df = _copy(te_run, "demo_send", "eu-hosted")
    sent: list[dict] = []
    _recorder(monkeypatch, sent)
    res = guard_demo.run_demo(ws, s, send=True)
    assert res["send"]["sent"] and res["send"]["route"] == "external" and res["send"]["source"].startswith("llm-external:")
    assert len(sent) == 1, "only the safe payload is sent, once"
    text = "\n".join(str(m.get("content", "")) for m in sent[0]["messages"])
    assert res["safe"]["about"]["id"] in text and not _header_hits(text)
    rec = next(r for r in ledger.read(ws) if r.id == res["send"]["ledger_id"])
    assert rec.guard_result == "allowed" and rec.ok and rec.purpose.startswith("guard demonstration")
    assert ledger.summary(ws)["n_external_sent"] == 1  # the real call counts; the demonstration records do not
    assert "sent once with --send" in res["statement"]


def test_guard_demo_cli_on_a_copy(te_run, tmp_path):
    import os
    import subprocess

    tmp, df, src_ws = te_run
    root = tmp_path / "ws"
    shutil.copytree(src_ws.dir, root / "te_cli", ignore=shutil.ignore_patterns("decision_log.sqlite*", "duck_tmp"))
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "TPM_PROFILE")}
    env.update({"TPM_WORKSPACE": str(root), "PYTHONIOENCODING": "utf-8", "TPM_NO_DOTENV": "1"})
    r = subprocess.run([sys.executable, "-m", "tpm", "guard-demo", "--run", "te_cli", "--profile", "eu-hosted"], cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "The guard blocked it" in r.stdout and "xmeas_1" in r.stdout and "None of the" in r.stdout and "Who wrote the explanations" in r.stdout
    assert (root / "te_cli" / guard_demo.DEMO_FILE).exists()
    r2 = subprocess.run([sys.executable, "-m", "tpm", "models", "--run", "te_cli"], cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
    assert r2.returncode == 0 and "Explanations in run te_cli" in r2.stdout and "evidence-based template" in r2.stdout


# ---------------------------------------------------------------------------------------------- audit mode (local calls)
def _local_stub(monkeypatch, seen: list) -> None:
    def fake(self, messages, schema=None, max_tokens=None, model=None, temperature=None):
        seen.append([dict(m) for m in messages])
        data = {"summary": "x", "steps": [], "uncertainty": [], "confidence": 0.5, "evidence_ids": []}
        return json.dumps(data), data, 4

    monkeypatch.setattr(OllamaProvider, "is_available", lambda self: True)
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: ["gemma3:4b"])
    monkeypatch.setattr(OllamaProvider, "capabilities", lambda self, m: [])
    monkeypatch.setattr(OllamaProvider, "chat", fake)


def test_local_calls_are_audited_not_stripped(monkeypatch, te_run):
    ws, s, df = _copy(te_run, "audit", "no-egress")
    seen: list = []
    _local_stub(monkeypatch, seen)
    payload = {"diagnosis": {"id": "DIAG-000009", "summary": "xmeas_1 rose to 3674.123456 at 2026-01-03T10:00:00 in simulationRun 4", "confidence": 0.8123456}, "evidence": [{"id": "EV-000001", "statement": "xmeas_1 and xmv_10 correlate r=0.9234", "n_samples": 500}]}
    res = router.complete("diagnosis_narrative", payload, purpose="audit test", ws=ws, settings=s)
    assert res.route == "local" and res.ok
    sent_local = json.dumps(seen[0])
    assert "xmeas_1" in sent_local and "3674.123456" in sent_local, "audit mode removes nothing from a local call"
    rec = ledger.read(ws)[-1]
    assert rec.route == "local" and rec.guard_result == "n/a"
    assert "nothing leaves" in rec.guard_reason and "Audit only" in rec.guard_reason and "would have" in rec.guard_reason
    assert rec.sanitizer["mode"] == "audit" and rec.sanitizer["applied"] is False and rec.sanitizer["would_be"] == "allowed"
    assert rec.sanitizer["names_aliased"] >= 3 and rec.sanitizer["floats_rounded"] >= 1 and rec.sanitizer["times_redacted"] >= 1

    # the chat's local tool agent: exact rows may be read; the audit counts them
    msgs = [{"role": "system", "content": "Signals: S01 (xmeas_1), S51 (xmv_10)"}, {"role": "user", "content": "Tool result for sql: [{\"xmeas_1\": 0.25038, \"xmv_10\": 41.2573}] from " + FILE_NAME}]
    res2 = router.local_chat(msgs, task="why_chat", purpose="why this row?", ws=ws, settings=s)
    assert res2.route == "local"
    rec2 = ledger.read(ws)[-1]
    assert "why this row" in rec2.guard_reason and "exact rows" in rec2.guard_reason and rec2.sanitizer["sql_results"] == 1 and rec2.sanitizer["files_redacted"] == 1
    stmt = ledger.data_flow_statement(ws, s)
    assert "Why local calls may see more" in stmt and "audit mode" in stmt and "before the guard's audit mode existed" not in stmt


# ---------------------------------------------------------------------------------------------- guard: rows as text, skeletons
def test_raw_rows_written_as_text_never_leave():
    s = load_settings(profile="hybrid")
    row = "row 5: S01=0.25038, S02=3674.1, S03=4529.2, S04=9.2321, S05=26.889"
    g = guard.check({"evidence": [{"id": "EV-000001", "statement": row, "n_samples": 500}, {"id": "EV-000002", "statement": "S01 and S02 correlate r=0.92 at lag 0", "n_samples": 500}]}, s, strict=False)
    assert g.allowed and len(g.sanitized_payload["evidence"]) == 2
    assert "statement" not in g.sanitized_payload["evidence"][0] and g.sanitized_payload["evidence"][1]["statement"].startswith("S01 and S02")
    assert any("raw row written as text (5 signal=value pairs)" in n for n in g.notes)
    # original names count as signals too, and the invariant catches a row that slipped through
    amap = {f"xmeas_{i}": f"S{i:02d}" for i in range(1, 8)}
    v = guard.verify_invariant({"question": "xmeas_1=0.25, xmeas_2=3670, xmeas_3=4530, xmeas_4=9.23, xmeas_5=26.9"}, amap, [], s.guard)
    assert any("row-like text" in x for x in v)
    assert not guard.verify_invariant({"question": "S01=0.25 and S02=3670 moved; r=0.9, lag=3, n=500, p=0.01"}, amap, [], s.guard)
    # a payload stripped down to empty skeletons has nothing useful left
    g2 = guard.check({"signals": [{"source_column": "xmeas_1", "values": [float(i) for i in range(50)]}], "tool_result": {"rows": [[1.5] * 6]}}, s, strict=True)
    assert not g2.allowed and "nothing useful left" in g2.reason and g2.sanitized_payload == {}
    # rule text keeps its numbers and is not a row
    g3 = guard.check({"rule_text": "S01=100, S02=200, S03=300, S04=400, S05=500 are the limits"}, s, strict=False)
    assert g3.allowed and g3.sanitized_payload["rule_text"].startswith("S01=100")


def test_guard_demo_and_coverage_in_pdf_and_pptx(monkeypatch, te_run):
    """Round 6 follow-up: the PDF and PowerPoint exports carry the data-flow additions of the HTML report too."""
    pypdf = pytest.importorskip("pypdf")
    pptx = pytest.importorskip("pptx")
    from tpm.report.pdf import generate_pdf
    from tpm.report.pptx_export import generate_pptx

    ws, s, _df = _copy(te_run, "demo_exports", "hybrid")
    _recorder(monkeypatch, [])
    guard_demo.run_demo(ws, s, profile="hybrid")
    title = guard_demo.report_context(ws, "en")["title"]
    pdf_text = " ".join((p.extract_text() or "") for p in pypdf.PdfReader(str(generate_pdf(ws, s, "en"))).pages)
    pdf_text = re.sub(r"\s+", " ", pdf_text)
    assert title in pdf_text and "demo_blocked" in pdf_text
    assert "Who wrote the explanations" in pdf_text
    prs = pptx.Presentation(str(generate_pptx(ws, s, "en")))
    slides = [" ".join(sh.text_frame.text for sh in sl.shapes if sh.has_text_frame) for sl in prs.slides]
    assert any(title in t for t in slides), "the data-flow slide names the guard demonstration"
