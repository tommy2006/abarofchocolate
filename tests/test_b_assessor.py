"""Agent B: data-quality assessor (scores, coverage, learning curve, action parsing/evaluation, apply)."""
from __future__ import annotations

import numpy as np
import pytest

from tests.fixtures.synth import make_synthetic
from tests.test_b_helpers import alias_map, build_workspace, make_settings
from tpm.contracts import HumanDecision, LLMResult
from tpm.quality import run_quality


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """The assessor must work fully without a model; tests never talk to Ollama."""
    import tpm.llm as llm

    monkeypatch.setattr(llm, "complete", lambda *a, **k: LLMResult(text="", data=None, source="template", route="none", ok=False, error="stubbed"))


@pytest.fixture(scope="module")
def assessed(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("a")
    settings = make_settings(tmp)
    df, truth = make_synthetic(n_groups=24, n_samples=400, seed=1)
    ws = build_workspace(tmp, settings, df, truth)
    run_quality(ws, settings, {"options": {}})
    return {"ws": ws, "settings": settings, "df": df, "truth": truth, "aliases": alias_map(df, truth["signal_columns"]), "tmp": tmp}


def test_dq_scores(assessed):
    from tpm.assessor.scores import compute_dq_scores, dq_scores

    ws, settings = assessed["ws"], assessed["settings"]
    sc = dq_scores(ws, settings)
    for k in ("completeness", "validity", "consistency", "timeliness", "overall", "mean_trust"):
        assert 0.0 <= sc[k] <= 1.0
    assert sc["timeliness"] == 1.0  # no timestamps in this fixture
    assert sc["consistency"] < 1.0 and sc["validity"] < 1.0 and sc["completeness"] < 1.0
    assert sc["evidence_ids"] and all(ws.evidence.get(e) for e in sc["evidence_ids"])
    assert sc["details"]["consistency"]["top"] and sc["worst_signals"]
    frozen = assessed["aliases"]["flow_a"]
    alt = compute_dq_scores(ws.checks(), ws.trust(), sc["n_batches"], sc["n_signals"], settings, exclude_signals=[frozen])
    assert alt["consistency"] >= sc["consistency"] and alt["mean_trust"] >= sc["mean_trust"]
    alt2 = compute_dq_scores(ws.checks(), ws.trust(), sc["n_batches"], sc["n_signals"], settings, exclude_types=["duplicate_rows", "duplicate_key"])
    assert alt2["consistency"] > sc["consistency"]


def test_regime_coverage(assessed):
    from tpm.assessor.coverage import regime_coverage

    ws, settings, al = assessed["ws"], assessed["settings"], assessed["aliases"]
    cov = regime_coverage(ws, settings)
    assert cov["unit"]["kind"] == "group" and cov["n_units"] == 24
    assert set(cov["unit_regime"]) == {str(g) for g in range(1, 25)}
    assert cov["k"] >= 1 and 0.0 <= cov["balance"] <= 1.0 and cov["coverage_score"] is not None
    assert sum(r["n_units"] for r in cov["regimes"]) == 24
    assert cov["findings"] and cov["evidence_ids"] and ws.evidence.get(cov["evidence_ids"][0])
    sig = cov["signals"]
    assert al["valve_1"] not in sig["near_constant"] and al["valve_2"] not in sig["near_constant"]
    assert al["const_c"] not in cov["scaler"]["features"][0]  # constant signal is excluded from fingerprints
    assert cov["scaler"]["features"] and len(cov["centroids"]) == cov["k"]


def test_learning_curve_with_stubbed_fit_score_subset(assessed, monkeypatch):
    import tpm.detect as detect
    from tpm.assessor.fitness import learning_curve

    ws, settings = assessed["ws"], assessed["settings"]
    calls = []

    def stub(ws_, settings_, train_groups, eval_groups, time_budget_s=60.0):
        assert all(isinstance(g, str) for g in train_groups + eval_groups)
        assert not set(train_groups) & set(eval_groups) and eval_groups
        calls.append((len(train_groups), len(eval_groups), time_budget_s))
        n = len(train_groups)
        return {"threshold_cv_mean": 1.0 / (n + 1), "detector_agreement": 0.5 + 0.02 * n, "flagged_fraction": 0.05, "n_train_groups": n, "n_eval_groups": len(eval_groups)}

    monkeypatch.setattr(detect, "fit_score_subset", stub)
    lc = learning_curve(ws, settings, time_budget_s=20)
    assert lc["available"] and lc["estimators"] == ["detect.fit_score_subset"] and calls
    fr = [p["fraction"] for p in lc["curve"]]
    assert fr == sorted(settings.assessor.learning_curve_fractions)
    ys = [p["primary"] for p in lc["curve"]]
    assert all(0 <= y <= 1 for y in ys) and ys == sorted(ys)  # the stub improves with data
    assert lc["primary_metric"] == "stability" and lc["fitness_score"] == ys[-1]
    assert lc["slope"] is not None and lc["would_help_more_data"] in (True, False, None)
    assert lc["n_eval_units"] >= 1 and lc["n_train_pool"] + lc["n_eval_units"] == 24
    assert lc["evidence_ids"] and ws.evidence.get(lc["evidence_ids"][0]).values["primary"] == ys
    assert all(c[2] > 0 for c in calls)  # a time budget is always passed


def test_learning_curve_fallback_estimator(assessed):
    from tpm.assessor.fitness import fallback_fit_score_subset, learning_curve

    ws, settings = assessed["ws"], assessed["settings"]
    lc = learning_curve(ws, settings, time_budget_s=15, use_detect=False)
    assert lc["available"] and lc["estimators"] == ["fallback_pca"] and len(lc["curve"]) == 3
    assert all(p["primary"] is not None and 0 <= p["primary"] <= 1 for p in lc["curve"])
    assert lc["seconds"] < 15
    res = fallback_fit_score_subset(ws, settings, [str(g) for g in range(1, 13)], ["20", "21"], time_budget_s=5)
    assert res["estimator"] == "fallback_pca" and res["n_train_rows"] > 0 and 0 <= res["flagged_fraction"] <= 1 and res["stability"] is not None


def test_learning_curve_with_labels(tmp_path):
    from tpm.assessor.fitness import fallback_fit_score_subset

    settings = make_settings(tmp_path)
    df, truth = make_synthetic(n_groups=10, n_samples=200, seed=3, with_labels=True, dq_issues=False)
    ws = build_workspace(tmp_path, settings, df, truth, run_id="run_lab")
    res = fallback_fit_score_subset(ws, settings, [str(g) for g in range(1, 8)], ["8", "9", "10"], time_budget_s=5)
    assert res.get("auroc") is None or 0.0 <= res["auroc"] <= 1.0


@pytest.mark.parametrize("text,typ,params", [
    ("would dropping S05 improve quality?", "drop_signal", {"signals": ["S05"]}),
    ("what if we add 20 more runs?", "add_more_like", {"n_units": 20, "unit": "run"}),
    ("should we remove duplicate rows?", "drop_duplicates", {}),
    ("Can we exclude S3 and S07 from the data?", "drop_signal", {"signals": ["S03", "S07"]}),
    ("drop run 12", "drop_group", {"group_ids": ["12"]}),
    ("remove rows 100-250", "drop_range", {"row_start": 100, "row_end": 250}),
    ("what about using only every 3rd sample?", "downsample", {"factor": 3}),
    ("would more data help?", "add_more_like", {"n_units": None, "unit": "unit"}),
    ("remove regime R2", "drop_regime", {"regime_id": "R2"}),
    ("please add the file extra_runs.parquet", "add_file", {"path": "extra_runs.parquet"}),
])
def test_parse_action_templates(text, typ, params):
    from tpm.assessor.actions import parse_action

    a = parse_action(text, use_llm=False)
    assert a is not None and a["type"] == typ and a["params"] == params and a["source"] == "template"


def test_parse_action_rejects_non_actions():
    from tpm.assessor.actions import normalize_action, parse_action

    assert parse_action("what is the weather like?", use_llm=False) is None
    assert normalize_action({"type": "add_file", "params": {"signal_alias": "S13"}}) is None
    assert normalize_action({"type": "drop_signal", "params": {"signals": []}}) is None
    assert normalize_action({"type": "python", "params": {}}) is None
    assert normalize_action({"type": "drop_range", "params": {"row_start": 10, "row_end": 5}}) is None


def test_evaluate_actions_follow_evidence(assessed):
    from tpm.assessor.actions import evaluate_action

    ws, settings, al, truth = assessed["ws"], assessed["settings"], assessed["aliases"], assessed["truth"]
    # duplicates exist -> recommend
    e = evaluate_action(ws, settings, {"type": "drop_duplicates", "params": {}}, time_budget_s=5)
    assert e["recommendation"] == "recommend" and e["expected_effect"]["rows_removed"] == 5
    assert e["expected_effect"]["dq_scores"]["consistency"]["delta"] > 0 and e["evidence_ids"]
    # a clean signal -> advise against
    e = evaluate_action(ws, settings, {"type": "drop_signal", "params": {"signals": [al["temp_r"]]}}, time_budget_s=5)
    assert e["recommendation"] == "advise_against" and "No" in e["rationale"]
    # a signal with a frozen block -> evidence supports dropping (recommend or neutral, never advise_against)
    e = evaluate_action(ws, settings, {"type": "drop_signal", "params": {"signals": [al["flow_a"]]}}, time_budget_s=5)
    assert e["recommendation"] in ("recommend", "neutral") and "data-quality problems" in e["rationale"]
    assert e["expected_effect"]["dq_scores"]["consistency"]["delta"] >= 0
    # the row range of the frozen block -> recommend
    inj = next(d for d in truth["dq"] if d["type"] == "frozen_block")
    e = evaluate_action(ws, settings, {"type": "drop_range", "params": {"row_start": inj["row_start"] - 5, "row_end": inj["row_end"] + 5}}, time_budget_s=5)
    assert e["recommendation"] == "recommend"
    e = evaluate_action(ws, settings, {"type": "drop_range", "params": {"row_start": 10, "row_end": 20}}, time_budget_s=5)
    assert e["recommendation"] == "advise_against"
    # more data -> a verdict backed by the learning curve
    e = evaluate_action(ws, settings, {"type": "add_more_like", "params": {"n_units": 20, "unit": "run"}}, time_budget_s=10)
    assert e["recommendation"] in ("recommend", "neutral", "advise_against") and e["expected_effect"]["fitness"] is not None
    assert e["evidence_ids"] and any(ws.evidence.get(i).kind == "learning_curve" for i in e["evidence_ids"] if ws.evidence.get(i))
    # unknown signal / unknown action
    e = evaluate_action(ws, settings, {"type": "drop_signal", "params": {"signals": ["S99"]}}, time_budget_s=2)
    assert e["recommendation"] == "neutral"
    e = evaluate_action(ws, settings, {"type": "teleport", "params": {}}, time_budget_s=2)
    assert e["recommendation"] == "neutral"


def test_run_assess_and_ask(assessed):
    from tpm.assessor import ask, run_assess

    ws, settings = assessed["ws"], assessed["settings"]
    summary = run_assess(ws, settings, {"options": {}, "progress": lambda f, m="": None})
    a = ws.read_json("assessor")
    for k in ("dq_scores", "coverage", "fitness", "combined_score", "recommendations", "more_data_verdict", "less_data_verdict"):
        assert k in a
    assert 0.0 <= a["combined_score"] <= 1.0 and summary["combined_score"] == a["combined_score"]
    assert a["fitness"]["available"] and a["fitness"]["curve"]
    assert a["more_data_verdict"]["would_help"] in (True, False, None) and a["more_data_verdict"]["why"]
    assert a["less_data_verdict"]["would_help"] is True  # duplicates exist
    assert any(r["action"]["type"] == "drop_duplicates" for r in a["recommendations"])
    assert all(r["evidence_ids"] and r["text"] and r["id"].startswith("REC-") for r in a["recommendations"])
    assert all(e["recommendation"] in ("recommend", "neutral", "advise_against") for e in a["evaluations"])
    assert a["seconds"] <= settings.assessor.experiment_time_budget_s * 1.5 + 5
    r = ask(ws, settings, "should we remove duplicate rows?", actor="human:ann(operator)")
    assert r["answer"].startswith("Yes") and r["action"]["type"] == "drop_duplicates" and r["evidence_ids"] and "Evidence:" in r["answer"]
    assert r["answer_source"] == "template"
    r = ask(ws, settings, "would dropping S05 improve quality?", actor="human:ann(operator)")
    assert r["action"]["type"] == "drop_signal" and r["answer"].split(".")[0] in ("No", "Unclear", "Yes")
    r = ask(ws, settings, "what if we add 20 more runs?", actor="human:ann(operator)")
    assert r["action"]["type"] == "add_more_like" and r["evaluation"]["expected_effect"]["fitness"]
    r = ask(ws, settings, "what is the weather like?", actor="human:ann(operator)")
    assert r["action"] is None and "evaluate" in r["answer"]
    chat = ws.read_jsonl("chat")
    assert len(chat) >= 4 and chat[-1]["channel"] == "assessor"
    assert any(e.actor == "human:ann(operator)" and e.action == "assessor_question" for e in ws.log.entries(object_type="assessor"))


def test_ask_uses_llm_answer_field_and_strips_fake_ids(assessed, monkeypatch):
    import tpm.llm as llm
    from tpm.assessor import ask

    ws, settings = assessed["ws"], assessed["settings"]

    def fake(task, payload, **kw):
        if task == "assessor_chat" and "template_answer" in payload:
            return LLMResult(text='{"answer": "Yes, remove the 5 duplicate rows (see EV-999999 and CHK-999999).", "citations": ["FLAG-000003"]}', source="llm-local:test", route="local", ok=True)
        return LLMResult(text="", ok=False, source="template", route="none", error="n/a")

    monkeypatch.setattr(llm, "complete", fake)
    r = ask(ws, settings, "should we remove duplicate rows?", actor="human:bob(engineer)")
    assert r["answer_source"] == "llm-local:test" and "EV-999999" not in r["answer"] and "FLAG-000003" not in r["answer"]
    assert r["answer"].startswith("Yes") and "Evidence:" in r["answer"]


def test_assess_new_file_and_apply(assessed):
    from tpm.assessor import apply_override, assess_new_file, ask

    ws, settings, tmp, al = assessed["ws"], assessed["settings"], assessed["tmp"], assessed["aliases"]
    df2, _ = make_synthetic(n_groups=3, n_samples=200, seed=77, dq_issues=False)
    p = tmp / "more.csv"
    df2.to_csv(p, index=False)
    nf = assess_new_file(ws, settings, p, time_budget_s=10)
    assert nf["exists"] and nf["compatible"] and nf["compatibility"]["how"] == "name" and nf["n_rows"] == 600
    assert nf["coverage_gain"]["available"] and nf["coverage_gain"]["units_new"] >= 1 and 0 <= nf["coverage_gain"]["novel_share"] <= 1
    assert nf["fitness_gain"]["available"] and nf["recommendation"] in ("recommend", "neutral") and nf["evidence_ids"]
    bad = tmp / "bad.csv"
    bad.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    nb = assess_new_file(ws, settings, bad)
    assert not nb["compatible"] and nb["recommendation"] == "advise_against"
    r = ask(ws, settings, f"what if we add the file {p}?")
    assert r["action"]["type"] == "add_file" and r["evaluation"]["recommendation"] == nf["recommendation"]
    # apply only through a human decision
    n_before = len(assessed["df"])
    eff = apply_override(ws, settings, HumanDecision(actor_name="ann", role="engineer", action="apply_assessor_action", object_type="assessor", object_id="x", new_value={"action": {"type": "drop_duplicates", "params": {}}}))
    assert eff["applied"] and eff["n_rows_before"] == n_before and eff["n_rows_after"] == n_before - 5
    assert ws.path("dataset_curated.parquet").exists() and ws.read_json("curation.json")["current"]["n_rows_after"] == n_before - 5
    eff = apply_override(ws, settings, HumanDecision(actor_name="ann", role="engineer", action="apply_assessor_action", object_type="assessor", object_id="x", new_value={"action": {"type": "drop_signal", "params": {"signals": [al["const_c"]]}}}))
    assert eff["applied"] and eff["columns_dropped"] == ["const_c"] and eff["n_rows_after"] == n_before
    eff = apply_override(ws, settings, HumanDecision(actor_name="ann", role="engineer", action="apply_assessor_action", object_type="assessor", object_id="x", new_value={"action": {"type": "add_file", "params": {"path": str(p)}}}))
    assert eff["applied"] and eff["n_rows_after"] == n_before + 600
    eff = apply_override(ws, settings, HumanDecision(actor_name="ann", role="engineer", action="apply_assessor_action", object_type="assessor", object_id="x", new_value={"action": {"type": "drop_group", "params": {"group_ids": ["1", "2"]}}}))
    assert eff["applied"] and eff["n_rows_after"] == n_before - 800
    eff = apply_override(ws, settings, HumanDecision(actor_name="ann", role="engineer", action="apply_assessor_action", object_type="assessor", object_id="x", new_value={"action": {"type": "downsample", "params": {"factor": 4}}}))
    assert eff["applied"] and abs(eff["n_rows_after"] - n_before / 4) <= 1
    # a recommendation id from assessor.json also works; dismiss and missing actions are handled
    rec = next(r for r in ws.read_json("assessor")["recommendations"] if r["action"]["type"] == "drop_duplicates")
    eff = apply_override(ws, settings, HumanDecision(actor_name="ann", role="engineer", action="approve", object_type="assessor", object_id=rec["id"]))
    assert eff["applied"] and ws.read_json("assessor")["recommendations"][0].get("status") in ("applied", None)
    assert not apply_override(ws, settings, HumanDecision(actor_name="ann", role="engineer", action="dismiss", object_type="assessor", object_id=rec["id"]))["applied"]
    assert "error" in apply_override(ws, settings, HumanDecision(actor_name="ann", role="engineer", action="approve", object_type="assessor", object_id="REC-999"))
    human = [e for e in ws.log.entries(actor_prefix="human:ann") if e.action == "apply_assessor_action"]
    assert len(human) >= 5 and ws.log.verify_chain()["ok"]


def test_pipeline_apply_decision_routes_to_assessor_and_quality(assessed):
    from tpm.pipeline import apply_decision
    from tpm.quality import compile_rule

    ws, settings = assessed["ws"], assessed["settings"]
    r = compile_rule(ws, settings, "S03 must stay between 100 and 140.")
    out = apply_decision(ws, settings, HumanDecision(actor_name="ann", role="engineer", action="approve", object_type="rule", object_id=r.id))
    assert out["effect"]["status"] == "active"
    out = apply_decision(ws, settings, HumanDecision(actor_name="ann", role="engineer", action="apply_assessor_action", object_type="assessor", object_id="x", new_value={"action": {"type": "drop_duplicates", "params": {}}}))
    assert out["effect"]["applied"]


# ------------------------------------------------------------------------------------------------
# missing-value tokens and non-finite values (DuckDB: stddev_samp raises 'out of range' on NaN / inf)
# ------------------------------------------------------------------------------------------------
def test_pipeline_reaches_assess_on_headerless_whitespace_file_with_nan_tokens(tmp_path):
    """A headerless whitespace file that writes missing values as the token 'NaN' must give NULLs in
    dataset.parquet and run through ingest -> profile -> quality -> assess."""
    from tpm.pipeline import run_pipeline
    from tpm.workspace import Workspace

    settings = make_settings(tmp_path)
    df, _ = make_synthetic(n_groups=8, n_samples=200, seed=11, dq_issues=False)
    num = df.select_dtypes(include=[np.number])
    toks = num.astype(object).to_numpy()
    rng = np.random.default_rng(3)
    floats = [j for j, c in enumerate(num.columns) if np.issubdtype(num[c].dtype, np.floating)]
    holes = [(int(i), int(j)) for i, j in zip(rng.integers(0, len(num), 60), rng.choice(floats, 60))]
    for i, j in holes:
        toks[i, j] = "NaN"
    p = tmp_path / "headerless_nan.dat"
    p.write_text("\n".join(" ".join(str(v) for v in row) for row in toks) + "\n", encoding="utf-8")
    st = run_pipeline(str(p), run_id="nan_tokens", stages=["ingest", "profile", "quality", "assess"], settings=settings, options={"use_llm": False, "no_llm": True, "skip_llm": True, "report_llm": False})
    states = {s.stage: (s.state, s.message) for s in st.stages}
    for stage in ("ingest", "profile", "quality", "assess"):
        assert states[stage][0] == "done", states
    ws = Workspace(run_id="nan_tokens", settings=settings)
    try:
        con = ws.duckdb()
        fl = [r[0] for r in con.execute("DESCRIBE dataset").fetchall() if r[1] in ("FLOAT", "DOUBLE")]
        n_bad = con.execute("SELECT " + " + ".join(f'count(*) FILTER (WHERE isnan("{c}") OR isinf("{c}"))' for c in fl) + " FROM dataset").fetchone()[0]
        n_null = con.execute("SELECT " + " + ".join(f'(count(*) - count("{c}"))' for c in fl) + " FROM dataset").fetchone()[0]
        assert n_bad == 0 and n_null == len(set(holes))
        a = ws.read_json("assessor")
        assert a["coverage"]["n_units"] >= 1 and 0.0 <= a["combined_score"] <= 1.0
    finally:
        ws.close()


def test_assessor_aggregates_tolerate_non_finite_values(tmp_path):
    """Defensive: a dataset.parquet written before ingest stored NaN / inf as NULL, and a new file that is read
    directly, must not break the per-unit mean / std aggregates."""
    import duckdb

    from tpm.assessor import assess_new_file
    from tpm.assessor.coverage import regime_coverage

    settings = make_settings(tmp_path)
    df, truth = make_synthetic(n_groups=8, n_samples=200, seed=2, dq_issues=False)
    ws = build_workspace(tmp_path, settings, df, truth, run_id="run_nonfinite")
    ds = ws.path("dataset")
    dirty = ds.with_name("dirty.parquet")
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * REPLACE (CASE WHEN __row__ % 50 = 7 THEN 'nan'::DOUBLE WHEN __row__ % 50 = 9 THEN 'inf'::DOUBLE WHEN __row__ % 50 = 11 THEN '-inf'::DOUBLE ELSE flow_a END AS flow_a) FROM read_parquet('{ds.as_posix()}')) TO '{dirty.as_posix()}' (FORMAT PARQUET)")
    con.close()
    dirty.replace(ds)
    try:
        assert ws.duckdb().execute("SELECT count(*) FROM dataset WHERE isnan(flow_a) OR isinf(flow_a)").fetchone()[0] == 3 * len(df) // 50
        cov = regime_coverage(ws, settings)
        assert cov["n_units"] == 8 and cov["findings"]
        df2, _ = make_synthetic(n_groups=2, n_samples=200, seed=78, dq_issues=False)
        new = df2.astype({"flow_a": object})
        new.loc[::40, "flow_a"] = "nan"
        new.loc[5::40, "flow_a"] = "inf"
        p = tmp_path / "more_nonfinite.csv"
        new.to_csv(p, index=False)
        nf = assess_new_file(ws, settings, p, time_budget_s=5)
        assert nf["exists"] and nf["compatible"] and nf["coverage_gain"]["available"], nf
    finally:
        ws.close()
