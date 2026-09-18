"""Agent A: profile stage tests (fingerprints, relations, structural roles, hypotheses, overrides, LLM fallback)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.fixtures.synth import make_synthetic
from tpm.contracts import HumanDecision, LLMResult
from tpm.ingest import run_ingest
from tpm.profile import apply_override, run_profile
from tpm.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
TE_HEAD = ROOT / "workspace" / "samples" / "te_head.csv"


@pytest.fixture(scope="module")
def module_settings(tmp_path_factory):
    from tpm.config import load_settings

    s = load_settings()
    s.workspace_dir = str(tmp_path_factory.mktemp("workspace"))
    return s


def _run(settings, run_id, tmp_dir, options=None, **synth_kwargs):
    kw = {"n_groups": 12, "n_samples": 400, "seed": 1}
    kw.update(synth_kwargs)
    df, truth = make_synthetic(**kw)
    p = Path(tmp_dir) / f"{run_id}.csv"
    df.to_csv(p, index=False)
    ws = Workspace(run_id=run_id, settings=settings)
    opts = {"skip_llm": True, **(options or {})}
    run_ingest(ws, settings, {"source_path": str(p), "options": opts, "progress": lambda f, m: None})
    t0 = time.time()
    res = run_profile(ws, settings, {"source_path": str(p), "options": opts, "progress": lambda f, m: None, "t_start": t0, "time_budget_s": 600})
    return ws, res, truth


@pytest.fixture(scope="module")
def profiled(module_settings, tmp_path_factory):
    ws, res, truth = _run(module_settings, "prof_main", tmp_path_factory.mktemp("data"))
    yield ws, res, truth
    ws.close()


def _alias_map(ws):
    schema = ws.schema()
    return schema, schema.signal_alias, {a: c for c, a in schema.signal_alias.items()}


# ------------------------------------------------------------------------------------------------
def test_structural_roles_match_truth(profiled):
    ws, res, truth = profiled
    schema, alias, rev = _alias_map(ws)
    got = {rev[d.id]: d.structural_role for d in ws.signals()}
    for col, role in truth["signal_roles"].items():
        if col in ("run", "sample", "timestamp", "fault_label"):
            continue
        assert got.get(col) == role, (col, got.get(col), role)
    # counter and identifier are recognised structurally at schema level (they are not signals)
    assert schema.order_column == "sample"
    assert "run" in schema.meta_columns and "run" not in schema.signal_columns
    consts = [d for d in ws.signals() if d.structural_role == "constant"]
    assert consts and all(d.excluded and d.excluded_reason == "constant" for d in consts)


def test_lagged_relations_within_one_sample(profiled):
    ws, res, truth = profiled
    _, alias, _ = _alias_map(ws)
    rel = ws.read_json("relations")
    pairs = {(p["a"], p["b"]): p for p in rel["pairs"]}
    for t in truth["relations"]:
        a, b = alias[t["a"]], alias[t["b"]]
        p = pairs.get((a, b)) or pairs.get((b, a))
        assert p is not None, f"pair {t} not among the related pairs"
        if t["lag"] > 0:
            assert p["a"] == a and p["b"] == b, f"lead/lag direction wrong for {t}: {p}"
        assert abs(p["lag"] - t["lag"]) <= 1, (t, p["lag"])
        assert p["evidence_ids"] and all(ws.evidence.get(e) for e in p["evidence_ids"])
    assert rel["corr"]["signals"] == rel["signals"] and len(rel["corr"]["matrix"]) == len(rel["signals"])
    assert rel["clusters"] and all(k.startswith("C") for k in rel["clusters"])
    assert any(len(v) for v in rel["leaders"].values())


def test_redundant_signal_detected(profiled):
    ws, res, truth = profiled
    _, alias, _ = _alias_map(ws)
    rel = ws.read_json("relations")
    derived = {r["signal"] for r in rel["redundancy"] if r["derived"]}
    assert alias["derived_sum"] in derived
    d = next(d for d in ws.signals() if d.id == alias["derived_sum"])
    assert d.structural_role == "derived_redundant"
    inf = ws.inferences.get(d.inference_ids[0])
    assert inf.alternatives, "the ambiguity of which member is derived must be stated"


def test_fingerprints_are_aggregates_only(profiled):
    ws, res, truth = profiled
    for d in ws.signals():
        fp = d.fingerprint
        for k, v in fp.items():
            if isinstance(v, (list, tuple)):
                assert len(v) <= 10, f"{d.id}.{k} looks like raw data"
            if isinstance(v, dict):
                assert all(not isinstance(x, (list, tuple)) or len(x) <= 50 for x in v.values())
        assert fp["count"] > 0 and fp["n_samples_dynamics"] > 0
        for key in ("mean", "std", "min", "max", "q50", "n_unique", "missing_rate", "autocorr_lag1", "noise_level", "stuck_fraction", "hold_period", "quantization_step", "distribution_shape", "boundedness"):
            assert key in fp, key
        assert 0.0 <= d.structural_confidence <= 1.0
        assert all(ws.evidence.get(e) is not None for e in d.evidence_ids)
        assert all(ws.inferences.get(i) is not None for i in d.inference_ids)
    assert res["roles"]["continuous_measured"] >= 6


def test_catalog_never_contains_labels(module_settings, tmp_path):
    ws, res, truth = _run(module_settings, "prof_labels", tmp_path, n_groups=6, n_samples=200, with_labels=True, with_timestamp=True)
    try:
        ids = {d.source_column for d in ws.signals()}
        assert "fault_label" not in ids and "timestamp" not in ids and "run" not in ids and "sample" not in ids
        rel = ws.read_json("relations")
        assert set(rel["signals"]) == {d.id for d in ws.signals()}
    finally:
        ws.close()


def test_domain_and_understanding_reports(profiled):
    ws, res, truth = profiled
    dom = ws.read_json("domain")
    assert abs(sum(dom["domain_likelihood"].values()) - 1.0) < 0.01
    assert dom["domain_likelihood"]["sensor_stream"] >= 0.5 and dom["explanation"] and dom["evidence_ids"]
    und = ws.read_json("understanding.json")
    assert und["generated_by"] == "template" and und["unknowns"]
    assert len(und["signals"]) == len(ws.signals())
    for s in und["signals"]:
        assert s["narrative"].startswith(s["id"]) and s["evidence"]
    hyp_sigs = [d for d in ws.signals() if d.instrument_hypothesis]
    assert hyp_sigs and all(d.instrument_confidence <= 0.5 for d in hyp_sigs)  # heuristics stay hypotheses
    assert any(i.status == "uncertain" and "hypothesis" in i.claim for i in ws.inferences.all())


def test_hypotheses_gated_by_domain(module_settings, tmp_path):
    import numpy as np
    import pandas as pd

    from tpm.ingest import ingest_dataframe

    rng = np.random.default_rng(0)
    n = 600
    df = pd.DataFrame({"customer": [f"C{rng.integers(0, 50):03d}" for _ in range(n)], "product": rng.choice(["a", "b", "c", "d"], n), "amount": rng.exponential(50, n).round(2), "qty": rng.integers(1, 9, n), "note": [f"order {i} ref {rng.integers(1e6)}" for i in range(n)]})
    ws = Workspace(run_id="prof_records", settings=module_settings)
    try:
        ingest_dataframe(ws, module_settings, df)
        res = run_profile(ws, module_settings, {"source_path": "df", "options": {"skip_llm": True}, "progress": lambda f, m: None})
        dom = ws.read_json("domain")
        assert dom["domain_likelihood"]["sensor_stream"] < 0.5 and not dom["hypotheses_enabled"]
        assert all(d.instrument_hypothesis is None for d in ws.signals())
    finally:
        ws.close()


def test_signal_override_rewrites_catalog(profiled, module_settings):
    ws, res, truth = profiled
    target = next(d for d in ws.signals() if d.structural_role == "continuous_measured")
    d = HumanDecision(actor_name="bob", role="operator", action="set_role", object_type="signal", object_id=target.id, note="it is a valve", new_value={"structural_role": "actuator_like", "instrument_hypothesis": "valve"})
    eff = apply_override(ws, module_settings, d)
    assert eff["updated"]
    after = next(x for x in ws.signals() if x.id == target.id)
    assert after.structural_role == "actuator_like" and after.human_role_override == "actuator_like" and after.instrument_hypothesis == "valve"
    assert ws.inferences.get(target.inference_ids[0]).human_status == "overridden"
    und = ws.read_json("understanding.json")
    assert next(s for s in und["signals"] if s["id"] == target.id)["role"] == "actuator_like"
    inf_id = after.inference_ids[0]
    d2 = HumanDecision(actor_name="bob", role="operator", action="question", object_type="inference", object_id=inf_id, note="not sure")
    apply_override(ws, module_settings, d2)
    assert ws.inferences.get(inf_id).human_status == "questioned"


def test_llm_enhancement_is_optional_and_capped(module_settings, tmp_path, monkeypatch):
    import tpm.llm as llm_mod
    import tpm.profile.roles as roles_mod

    monkeypatch.setattr(llm_mod, "complete", lambda *a, **k: LLMResult(ok=False, source="template", error="no model"))
    ws, res, truth = _run(module_settings, "prof_nollm", tmp_path, n_groups=4, n_samples=200, options={"skip_llm": False})
    try:
        assert res["llm"]["ok"] is False and res["n_signals"] > 0
    finally:
        ws.close()

    def fake_complete(task, payload, **kw):
        sigs = [s["signal"] for s in payload["signals"]][:3]
        data = {"hypotheses": [{"signal": sigs[0], "instrument": "flow", "unit_operation": "feed", "confidence": 0.99, "reasoning": "noisy", "evidence_ids": ["EV-999999"]}, {"signal": "S99", "instrument": "x", "confidence": 0.5}, {"signal": sigs[1], "instrument": "", "confidence": 0.5}]}
        return LLMResult(ok=True, data=data, text="", source="llm-local:test-model", model="test-model", route="local", ledger_id="EGR-000001")

    monkeypatch.setattr(llm_mod, "complete", fake_complete)
    ws, res, truth = _run(module_settings, "prof_fakellm", tmp_path, n_groups=4, n_samples=200, options={"skip_llm": False})
    try:
        assert res["llm"]["ok"] and res["llm"]["n_accepted"] == 1 and res["llm"]["source"] == "llm-local:test-model"
        d = next(x for x in ws.signals() if x.instrument_hypothesis == "flow")
        assert d.instrument_confidence <= 0.6 and d.unit_operation_hypothesis == "feed"
        inf = [ws.inferences.get(i) for i in d.inference_ids if ws.inferences.get(i).source == "llm-local:test-model"]
        assert inf and inf[0].status == "uncertain" and inf[0].evidence_ids and all(ws.evidence.get(e) for e in inf[0].evidence_ids)
        assert ws.log.entries(actor_prefix="llm:local")
    finally:
        ws.close()


@pytest.mark.skipif(not TE_HEAD.exists(), reason="workspace/samples/te_head.csv not present")
def test_te_head_profile_smoke(module_settings):
    ws = Workspace(run_id="prof_te_head", settings=module_settings)
    try:
        t0 = time.time()
        run_ingest(ws, module_settings, {"source_path": str(TE_HEAD), "options": {}, "progress": lambda f, m: None})
        t1 = time.time()
        res = run_profile(ws, module_settings, {"source_path": str(TE_HEAD), "options": {"skip_llm": True}, "progress": lambda f, m: None, "t_start": t0, "time_budget_s": 600})
        t2 = time.time()
        print(f"\nte_head: ingest {t1 - t0:.2f}s, profile {t2 - t1:.2f}s, roles={res['roles']}, clusters={res['n_clusters']}")
        assert res["n_signals"] >= 10 and res["n_pairs"] > 0
        assert (t2 - t0) < 120
    finally:
        ws.close()
