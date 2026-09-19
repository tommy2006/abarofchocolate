"""Agent C diagnose tests: diagnoses are explained step by step, cite evidence, are critiqued, and accept
human overrides that become labelled examples."""
from __future__ import annotations

import pytest

from tests.test_c_common import detect_run


@pytest.fixture(scope="module")
def run():
    ws, truth, df, summary = detect_run(False)
    from tpm.diagnose import run_diagnose

    dsum = run_diagnose(ws, ws.settings, {"options": {"no_llm": True}})  # template-only keeps the suite fast; LLM path covered by test_llm_enhancement
    return ws, truth, df, dsum


def test_diagnoses_written(run):
    ws, truth, df, dsum = run
    diags = ws.diagnoses()
    assert diags
    assert dsum["n_diagnoses"] == len(diags)
    groups_with_events = {f.group_id for f in ws.flags() if f.kind in ("anomaly", "drift")}
    # the aggregated diagnosis of isolated readings (when present) spans groups and is not an event diagnosis
    assert {d.group_id for d in diags if d.fault_type != "isolated suspicious readings"} == groups_with_events
    assert sum(1 for d in diags if d.fault_type == "isolated suspicious readings") <= 1


def test_diagnosis_steps_and_evidence(run):
    ws, truth, df, dsum = run
    for d in ws.diagnoses():
        assert len(d.steps) >= 3, d.id
        assert d.evidence_ids, d.id
        for eid in d.evidence_ids:
            assert ws.evidence.get(eid) is not None, eid
        assert d.ranked_signals
        assert all(s.explanation for s in d.ranked_signals)
        assert d.fault_type
        assert d.cause_class in ("process", "sensor", "data", "mixed", "unknown")
        assert d.assumptions
        assert any("normal" in s.lower() for s in d.steps)
        assert any("check" in s.lower() for s in d.steps)
        assert 0 <= d.confidence <= 1


def test_sensor_fault_diagnosis(run):
    ws, truth, df, dsum = run
    alias = truth["alias"]
    stuck = [str(int(g) + 1) for g, info in truth["groups"].items() if info["fault"] == "stuck_sensor"]
    ds = [d for d in ws.diagnoses() if d.group_id in stuck and d.cause_class in ("sensor", "data")]
    assert ds
    d = ds[0]
    assert alias["level_s"] in d.fault_type or d.ranked_signals[0].signal == alias["level_s"]
    assert any("sensor" in s.lower() for s in d.steps)


def test_critique_runs_and_adjusts(run):
    ws, truth, df, dsum = run
    diags = ws.diagnoses()
    assert all(d.critique is not None for d in diags)
    for d in diags:
        c = d.critique
        assert c.verdict in ("supported", "weakened", "rejected")
        assert c.checks
        assert c.objections
        assert all("EV-" in o or "(no evidence)" in o for o in c.objections)
        assert c.adjusted_confidence is not None
        assert abs(d.confidence - c.adjusted_confidence) < 1e-6
        # verdict follows the weighted failed-check rule
        failed = sum(ch["weight"] for ch in c.checks if not ch["passed"])
        total = sum(ch["weight"] for ch in c.checks) or 1.0
        if failed / total >= 0.25:
            assert c.verdict in ("weakened", "rejected"), (d.id, c.checks)
        if any(not ch["passed"] and ch["name"] == "top_signal_trusted" for ch in c.checks):
            assert c.verdict == "rejected"
    # the fixture contains data-quality events, so at least one diagnosis must carry a failed check
    assert any(not ch["passed"] for d in diags for ch in d.critique.checks)


def test_apply_override_stores_human_label(run):
    ws, truth, df, dsum = run
    from tpm.contracts import HumanDecision
    from tpm.diagnose import apply_override

    d = ws.diagnoses()[0]
    res = apply_override(ws, ws.settings, HumanDecision(actor_name="bo", role="engineer", action="override", object_type="diagnosis", object_id=d.id, note="known feed valve issue", new_value={"fault_type": "feed valve sticking", "cause_class": "process"}))
    assert res["found"] and res.get("human_label_stored")
    d2 = [x for x in ws.diagnoses() if x.id == d.id][0]
    assert d2.human_status == "overridden"
    assert d2.fault_type == "feed valve sticking"
    labels = ws.read_jsonl("human_labels.jsonl")
    assert labels and labels[-1]["diagnosis_id"] == d.id
    # a later diagnose run reuses the human label for the same signature
    from tpm.diagnose import run_diagnose

    run_diagnose(ws, ws.settings, {"options": {"no_llm": True}})
    again = [x for x in ws.diagnoses() if x.group_id == d.group_id]
    assert any("feed valve sticking" in x.fault_type for x in again)


def test_diagnose_flags_streaming(run):
    ws, truth, df, dsum = run
    from tpm.diagnose import diagnose_flags

    flags = [f for f in ws.flags() if f.kind in ("anomaly", "drift")][:2]
    n_before = len(ws.diagnoses())
    out = diagnose_flags(ws, ws.settings, flags, use_llm=False)
    assert out
    assert len(ws.diagnoses()) == n_before + len(out)
    assert all(o.critique is not None for o in out)


def test_llm_enhancement_when_available(run):
    """When a local model is reachable the narrative/critique are labelled with their source; otherwise the
    template path must still produce a complete diagnosis."""
    ws, truth, df, dsum = run
    from tpm.diagnose import diagnose_flags
    from tpm.llm import available

    flags = [f for f in ws.flags() if f.kind in ("anomaly", "drift")][:1]
    out = diagnose_flags(ws, ws.settings, flags, use_llm=True)
    assert out and len(out[0].steps) >= 3
    d = out[0]
    if available().get("local") or available().get("external"):
        assert d.narrative_source != "template" or d.critique.source != "template"
        assert d.critique.objections
    else:
        assert d.narrative_source == "template" and d.critique.source == "template"
