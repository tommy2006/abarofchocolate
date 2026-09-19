"""Nothing raw leaves: every externally routed model task of a finished run is exercised in the hybrid and the eu-hosted
profile with the external provider's chat (AnthropicProvider in hybrid, OpenAICompatProvider in eu-hosted) replaced by
a recorder, and every captured message is scanned for raw data.

The run is a real pipeline pass over the shared synthetic generator, with distinctive column names, category / label
values, timestamps and file name, so that a hit in an outgoing message cannot be a coincidence. Asserted:
  * every external task was really SENT (none silently blocked by the guard and answered locally);
  * no raw cell value at full precision, no original column name, no category / label value, no data timestamp and no
    source file name appears in anything that was sent, nor in the ledger previews of what was sent.
No network, no Ollama, no API key: the provider and the local model are stubs."""
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

from tests.fixtures.synth import make_synthetic  # noqa: E402
from tpm.config import load_settings  # noqa: E402
from tpm.llm import ledger  # noqa: E402
from tpm.llm.providers import AnthropicProvider, OllamaProvider, OpenAICompatProvider, ProviderError  # noqa: E402
from tpm.workspace import Workspace  # noqa: E402

RENAME = {
    "run": "CampaignNo", "sample": "SampleIdx", "timestamp": "LoggedAt", "fault_label": "FaultClass",
    "flow_a": "FT101_FeedFlow", "press_r": "PT204_ReactorPress", "temp_r": "TT310_ReactorTemp", "level_s": "LT415_SepLevel",
    "flow_b": "FT102_RecycleFlow", "temp_s": "TT320_SepTemp", "valve_1": "XV501_FeedValve", "valve_2": "XV502_PurgeValve",
    "comp_a": "AT601_CompA", "const_c": "KC700_Const", "derived_sum": "FY103_TotalFlow", "power_c": "JT800_CompPower",
}
LABELS = {"normal": "NominalOp", "step": "ZetaTrip", "ramp": "QuorumLeak", "stuck_sensor": "FrozenProbeX", "corr_break": "LoopDecoupled", "oscillation": "HuntingValve", "noise_burst": "StaticBurst"}
LINES = ["LineNorth7", "KettleSouth3"]
FILE_NAME = "plant7_secret_campaign.csv"
EXTERNAL_TASKS = {"sensor_hypotheses", "rule_compile", "diagnosis_narrative", "critique", "report_narrative"}


@pytest.fixture(scope="module")
def finished_run(tmp_path_factory):
    from tpm.pipeline import run_pipeline

    tmp = tmp_path_factory.mktemp("egress")
    s = load_settings()
    s.workspace_dir = str(tmp / "ws")
    s.detect.time_budget_s = 90
    s.detect.n_folds = 3
    s.detect.use_autoencoder = False
    df, _ = make_synthetic(n_groups=8, n_samples=240, seed=5, with_timestamp=True, with_labels=True)
    df["fault_label"] = df["fault_label"].map(LABELS)
    df["LineName"] = np.where(df["run"] % 2 == 0, LINES[0], LINES[1])
    df = df.rename(columns=RENAME)
    src = tmp / FILE_NAME
    df.to_csv(src, index=False)
    st = run_pipeline(str(src), run_id="egress_src", settings=s, stages=["ingest", "profile", "quality", "detect", "diagnose"], options={"no_llm": True, "skip_llm": True, "use_llm": False, "report_llm": False})
    assert st.state == "done", [(x.stage, x.error) for x in st.stages if x.state == "failed"]
    ws = Workspace(run_id="egress_src", settings=s)
    assert ws.diagnoses(), "the synthetic run must produce diagnoses, or the narrative tasks are not exercised"
    return tmp, df, ws


def _exercise(ws: Workspace, settings) -> None:
    """Every call site that routes external in the hybrid / eu-hosted profile (the same walk as the 2026-09-19 dry run)."""
    from tpm.api.plain import plain_for
    from tpm.diagnose import _load_context
    from tpm.diagnose.critique import critique_diagnosis
    from tpm.diagnose.diagnosis import add_llm_narrative
    from tpm.profile.roles import llm_hypotheses
    from tpm.quality.rules import compile_rule
    from tpm.report.report import generate_report

    schema = ws.schema()
    descriptors = ws.signals()
    rel = ws.read_json("relations", {}) or {}
    domain_ll = dict(schema.domain_likelihood or {})
    llm_hypotheses(ws, settings, descriptors, rel, domain_ll, None)
    llm_hypotheses(ws, settings, descriptors, rel, domain_ll, f"operator hint: pulp mill, {LINES[0]} near the cooler, file {FILE_NAME}")
    a, b = schema.signal_columns[0], schema.signal_columns[1]
    for text in (f"when {a} is unusually high the {b} should come down soon after", f"{a} and {b} ought to move together unless {LABELS['step']} is active"):
        compile_rule(ws, settings, text, persist=False)
    c = _load_context(ws, settings)
    flags_by_id = {f.id: f for f in ws.flags()}
    for d in ws.diagnoses()[:3]:
        add_llm_narrative(ws, settings, d, "en")
        critique_diagnosis(ws, settings, d, flags_by_id, c["baseline"], c["patterns"], int(settings.detect.window), "en", use_llm=True)
    generate_report(ws, settings, "en", use_llm=True, out_path=ws.dir / "egress_report_en.html", llm_wait_s=20)
    for view in ("understanding", "quality", "monitor", "diagnoses", "assessor", "dataflow"):
        plain_for(ws, settings, view, "en", enhance=True)


# ---------------------------------------------------------------------------------------------- raw-data scan
_NUM_RE = re.compile(r"(?<![\w.])[-+]?\d+\.\d+(?:[eE][-+]?\d+)?(?![\w])")


def _raw_numbers(df: pd.DataFrame) -> dict[int, set[float]]:
    """Every float cell of the dataset, indexed by number of decimals, for exact look-ups of decimal tokens."""
    vals = []
    for col in df.columns:
        if pd.api.types.is_float_dtype(df[col]):
            v = df[col].to_numpy(dtype="float64")
            vals.append(v[np.isfinite(v)])
    allv = np.unique(np.concatenate(vals))
    return {d: set(np.round(allv, d).tolist()) for d in range(0, 9)}


def _sig_digits(tok: str) -> int:
    """Significant digits of a decimal token; 2700.0 has two (JSON writes the rounded float 2.7e3 that way)."""
    return len(re.split(r"[eE]", tok.lstrip("+-"))[0].replace(".", "").strip("0"))


def _scan(text: str, df: pd.DataFrame, by_dec: dict[int, set[float]]) -> list[str]:
    hits = []
    low = text.lower()
    for name in df.columns:
        if name.lower() in low:
            hits.append(f"column name {name}")
    for value in list(LABELS.values()) + LINES:
        if value.lower() in low:
            hits.append(f"category value {value}")
    for token in (FILE_NAME, Path(FILE_NAME).stem):
        if token.lower() in low:
            hits.append(f"file name {token}")
    if re.search(r"\d{4}-\d{2}-\d{2}", text) or re.search(r"(?<![\d:])\d{1,2}:\d{2}:\d{2}", text):
        hits.append("date / time")
    for m in _NUM_RE.finditer(text):
        tok = m.group(0)
        if _sig_digits(tok) > 3:
            hits.append(f"number with more than 3 significant digits {tok}")
        if _sig_digits(tok) < 5:  # a 3-digit rounded aggregate may coincide with a rounded cell; full precision may not
            continue
        dec = min(8, len(re.split(r"[eE]", tok.split(".")[1])[0]))
        if round(float(tok), dec) in by_dec[dec]:
            hits.append(f"raw cell value {tok}")
    return hits


@pytest.mark.parametrize("profile", ["hybrid", "eu-hosted"])
def test_external_tasks_are_sent_and_carry_no_raw_data(monkeypatch, finished_run, profile):
    tmp, df, src_ws = finished_run
    root = tmp / f"copy_{profile}"
    shutil.copytree(src_ws.dir, root / "egress_run", ignore=shutil.ignore_patterns("decision_log.sqlite*", "duck_tmp"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
    monkeypatch.setenv("TPM_EU_API_KEY", "eu-fake-key")
    settings = load_settings(profile=profile)
    settings.workspace_dir = str(root)
    if profile == "eu-hosted":
        settings.external_llm.base_url = "http://127.0.0.1:9/eu"  # a closed local port: nothing could leave even without the stub
        settings.active_profile.eu_hosts.append("127.0.0.1")
    ws = Workspace("egress_run", settings=settings, root=root)
    sent: list[dict] = []

    def capture(self, messages, schema=None, max_tokens=None, model=None):
        sent.append({"model": model or self.cfg.model, "messages": messages, "provider": type(self).__name__})
        raise ProviderError("captured, not sent")

    def no_client(self):
        raise AssertionError("the network client must never be built in this test")

    for cls in (AnthropicProvider, OpenAICompatProvider):  # hybrid sends through the first, eu-hosted through the second
        monkeypatch.setattr(cls, "chat", capture)
        monkeypatch.setattr(cls, "_client", no_client)
    monkeypatch.setattr(OllamaProvider, "is_available", lambda self: False)
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: [])

    _exercise(ws, settings)

    recs = ledger.read(ws)
    ext = [r for r in recs if r.route == "external"]
    blocked = [(r.task, r.guard_result, r.guard_reason) for r in ext if r.guard_result != "allowed"]
    assert not blocked, f"external tasks were stopped before sending: {blocked}"
    assert {r.task for r in ext} == EXTERNAL_TASKS, "every external task must reach the provider"
    assert len(sent) == len(ext) >= 12
    assert all(s["model"] == settings.external_llm.model for s in sent)
    assert {s["provider"] for s in sent} == {"OpenAICompatProvider" if profile == "eu-hosted" else "AnthropicProvider"}

    by_dec = _raw_numbers(df)
    problems = []
    for i, s in enumerate(sent):
        text = "\n".join(str(m.get("content", "")) for m in s["messages"])
        problems += [f"message {i}: {h}" for h in _scan(text, df, by_dec)]
    for r in ext:
        problems += [f"ledger preview {r.id} ({r.task}): {h}" for h in _scan(r.payload_preview, df, by_dec)]
        assert r.sanitizer, f"{r.id}: the ledger must say what the sanitiser changed"
    assert not problems, "raw data in outgoing text:\n" + "\n".join(sorted(set(problems))[:40])

    # the scan itself works: the same artifacts WITHOUT the guard do contain raw material
    raw_text = json.dumps([d.model_dump() for d in ws.signals()], default=str) + json.dumps(ws.read_json("schema"), default=str)
    assert any(h.startswith("column name") for h in _scan(raw_text, df, by_dec))
    totals = {k: sum(int(r.sanitizer.get(k) or 0) for r in ext) for k in ("floats_rounded", "keys_dropped", "names_aliased")}
    assert totals["floats_rounded"] and totals["names_aliased"], f"the sanitiser was expected to round and alias on a real run: {totals}"
    # spec section 5: the call sites no longer put min / max, $schema or `evaluation` into a payload, so the guard has no
    # key to remove (the key drops themselves are covered by test_d_guard)
    assert totals["keys_dropped"] == 0, f"a stage payload carries a key the guard removes; give the model an aggregate instead: {totals}"
