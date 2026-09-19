"""Agent B: rule schema, executor, template parser, compile/lifecycle and evaluation over batches."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.synth import make_synthetic
from tests.test_b_helpers import alias_map, build_workspace, make_settings
from tpm.config import ROOT
from tpm.contracts import HumanDecision
from tpm.quality import apply_override, compile_rule
from tpm.quality._common import SignalInfo
from tpm.quality.rules import RULE_TYPES, add_rules_from_file, explain_rule, load_rules_file, parse_rule_text, parse_rule_text_detailed, run_active_rules, run_rule, validate_compiled
from tpm.quality.trust import trust_verdict

CATALOG = [SignalInfo(alias=f"S{i:02d}", column=f"c{i}") for i in range(1, 13)]
EXAMPLE = ROOT / "config" / "rules.example.md"


def _frame(n: int = 200, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({f"S{i:02d}": rng.normal(100, 1, n) for i in range(1, 6)})
    df["__row__"] = np.arange(n)
    return df


# ------------------------------------------------------------------ executor: every rule type
def test_range_rule():
    df = _frame()
    df.loc[10:12, "S01"] = 150.0
    df.loc[50, "S01"] = np.nan
    v = run_rule({"type": "range", "signal": "S01", "min": 90, "max": 110}, df)
    assert v == [{"row_start": 10, "row_end": 12, "n": 3, "signal": "S01", "value_min": 150.0, "value_max": 150.0, "detail": "outside allowed range"}]


def test_rate_of_change_rule():
    df = _frame()
    df.loc[20, "S02"] = 130.0
    v = run_rule({"type": "rate_of_change", "signal": "S02", "max_abs_delta": 5, "per_samples": 1}, df)
    assert [(e["row_start"], e["row_end"]) for e in v] == [(20, 21)]  # jump up then back
    v2 = run_rule({"type": "rate_of_change", "signal": "S02", "max_abs_delta": 5, "per_samples": 3}, df)
    assert v2 and v2[0]["row_start"] == 20


def test_acceleration_rule():
    n = 100
    x = np.linspace(0, 10, n)  # constant slope: zero acceleration
    df = pd.DataFrame({"S01": x, "__row__": np.arange(n)})
    assert run_rule({"type": "acceleration", "signal": "S01", "max_abs_second_diff": 0.01}, df) == []
    df.loc[40, "S01"] += 5.0  # a spike is a large second difference
    v = run_rule({"type": "acceleration", "signal": "S01", "max_abs_second_diff": 1.0}, df)
    assert v and v[0]["row_start"] == 40 and v[-1]["row_end"] == 42


def test_duration_rule_variants():
    df = _frame()
    df.loc[30:60, "S03"] = 120.0
    v = run_rule({"type": "duration", "signal": "S03", "condition": {"op": "above", "value": 110}, "min_samples": 20}, df)
    assert v and (v[0]["row_start"], v[0]["row_end"]) == (30, 60)
    assert run_rule({"type": "duration", "signal": "S03", "condition": {"op": "above", "value": 110}, "min_samples": 40}, df) == []
    df.loc[100:115, "S03"] = np.nan
    v = run_rule({"type": "duration", "signal": "S03", "condition": {"op": "missing"}, "min_samples": 10}, df)
    assert v and v[0]["row_start"] == 100
    v = run_rule({"type": "duration", "signal": "S03", "condition": {"op": "constant"}, "min_samples": 25}, df)
    assert v and v[0]["row_start"] == 30
    v = run_rule({"type": "duration", "signal": "S03", "condition": {"op": "between", "min": 119, "max": 121}, "min_samples": 5}, df)
    assert v and v[0]["row_start"] == 30


def test_cross_signal_rule():
    df = _frame()
    df.loc[10:14, "S01"] = 200.0  # condition holds
    df.loc[12:13, "S02"] = 50.0  # consequence fails on two of those rows
    v = run_rule({"type": "cross_signal", "if": {"signal": "S01", "op": ">", "value": 150}, "then": {"signal": "S02", "op": ">=", "value": 90}}, df)
    assert [(e["row_start"], e["row_end"]) for e in v] == [(12, 13)]


def test_rolling_stat_rule():
    df = _frame(seed=2)
    df.loc[100:160, "S04"] = df.loc[100:160, "S04"] + np.random.default_rng(1).normal(0, 20, 61)
    v = run_rule({"type": "rolling_stat", "signal": "S04", "stat": "std", "window": 20, "op": "<", "value": 5}, df)
    assert v and v[0]["row_start"] >= 100 and v[-1]["row_end"] <= 180
    assert run_rule({"type": "rolling_stat", "signal": "S04", "stat": "mean", "window": 20, "op": ">", "value": 50}, df) == []


def test_missing_and_stuck_rules():
    df = _frame()
    df.loc[20:35, "S05"] = np.nan
    df.loc[80:120, "S05"] = 101.0
    v = run_rule({"type": "missing", "signal": "S05", "max_consecutive": 10}, df)
    assert [(e["row_start"], e["row_end"]) for e in v] == [(20, 35)]
    assert run_rule({"type": "missing", "signal": "S05", "max_consecutive": 16}, df) == []
    v = run_rule({"type": "stuck", "signal": "S05", "max_constant_samples": 30}, df)
    assert [(e["row_start"], e["row_end"]) for e in v] == [(80, 120)]
    assert run_rule({"type": "stuck", "signal": "S05", "max_constant_samples": 41}, df) == []


def test_drift_rule():
    n = 300
    x = np.concatenate([np.zeros(150), np.linspace(0, 30, 150)])
    df = pd.DataFrame({"S01": x, "__row__": np.arange(n)})
    v = run_rule({"type": "drift", "signal": "S01", "window": 50, "max_abs_change": 8}, df)
    assert v and v[0]["row_start"] > 150
    assert run_rule({"type": "drift", "signal": "S01", "window": 50, "max_abs_change": 40}, df) == []


def test_group_scope_and_boundaries():
    df = _frame(100)
    df["__group__"] = ["a"] * 50 + ["b"] * 50
    df.loc[:49, "S01"] = 100.0
    df.loc[50:, "S01"] = 140.0  # level jump across the group boundary is not a rate violation
    assert run_rule({"type": "rate_of_change", "signal": "S01", "max_abs_delta": 5}, df) == []
    df.loc[60, "S01"] = 150.0
    v = run_rule({"type": "range", "signal": "S01", "max": 120, "group": "a"}, df)
    assert v == []  # 140/150 are in group b
    v = run_rule({"type": "range", "signal": "S01", "max": 120, "group": "b"}, df)
    assert len(v) == 1 and (v[0]["row_start"], v[0]["row_end"], v[0]["value_max"]) == (50, 99, 150.0)


def test_validation_rejects_bad_specs():
    ok, errs, _ = validate_compiled({"type": "range", "signal": "S01"})
    assert not ok and "min" in errs[0]
    ok, errs, _ = validate_compiled({"type": "python", "code": "import os"})
    assert not ok
    ok, errs, _ = validate_compiled({"type": "range", "signal": "S01", "max": 1, "exec": "x"})
    assert not ok and "unknown fields" in errs[0]
    ok, errs, _ = validate_compiled({"type": "range", "signal": "S99", "max": 1}, {"S01"})
    assert not ok and "catalog" in errs[0]
    ok, _, norm = validate_compiled({"type": "rate_of_change", "signal": "S01", "max_abs_delta": "5"})
    assert ok and norm["per_samples"] == 1 and norm["max_abs_delta"] == 5.0
    for t in RULE_TYPES:
        assert t in RULE_TYPES


# ------------------------------------------------------------------ template parser
def test_every_example_rule_compiles_with_template_parser():
    lines = load_rules_file(EXAMPLE)
    assert len(lines) == 8
    types = []
    for line in lines:
        r = parse_rule_text_detailed(line, CATALOG)
        assert r.spec is not None, f"{line!r}: {r.error}"
        assert r.confidence >= 0.9 and r.explanation
        types.append(r.spec["type"])
    assert set(types) == set(RULE_TYPES) - {"duration"}  # the example file has no duration rule


@pytest.mark.parametrize("text,expected", [
    ("S03 must not exceed 140", {"type": "range", "signal": "S03", "max": 140.0}),
    ("S03 must be above 45.5", {"type": "range", "signal": "S03", "min": 45.5}),
    ("S3 should stay below 9.", {"type": "range", "signal": "S03", "max": 9.0}),
    ("S03 must not be negative", {"type": "range", "signal": "S03", "min": 0.0}),
    ("S03 >= 5", {"type": "range", "signal": "S03", "min": 5.0}),
    ("10 <= S03 <= 20", {"type": "range", "signal": "S03", "min": 10.0, "max": 20.0}),
    ("S03 must be at least 45 and at most 90", {"type": "range", "signal": "S03", "min": 45.0, "max": 90.0}),
    ("S07 must not change by more than 5 per 10 samples", {"type": "rate_of_change", "signal": "S07", "max_abs_delta": 5.0, "per_samples": 10}),
    ("The acceleration of S02 must not exceed 3", {"type": "acceleration", "signal": "S02", "max_abs_second_diff": 3.0}),
    ("If S09 > 90 then S01 > 45", {"type": "cross_signal", "if": {"signal": "S09", "op": ">", "value": 90.0}, "then": {"signal": "S01", "op": ">", "value": 45.0}}),
    ("Whenever S09 is at least 90, S01 must not be below 45", {"type": "cross_signal", "if": {"signal": "S09", "op": ">=", "value": 90.0}, "then": {"signal": "S01", "op": ">=", "value": 45.0}}),
    ("S05 must not have more than 10 missing values in a row", {"type": "missing", "signal": "S05", "max_consecutive": 10}),
    ("S12 must not be frozen for more than 30 samples", {"type": "stuck", "signal": "S12", "max_constant_samples": 30}),
    ("The rolling 60-sample mean of S04 must be above 8", {"type": "rolling_stat", "signal": "S04", "stat": "mean", "window": 60, "op": ">", "value": 8.0}),
    ("S10 must not change by more than 15 within any window of 200 samples", {"type": "drift", "signal": "S10", "window": 200, "max_abs_change": 15.0}),
    ("S03 must not be above 100 for more than 20 samples", {"type": "duration", "signal": "S03", "condition": {"op": "above", "value": 100.0}, "min_samples": 21}),
    ("S01 must stay between 100 and 140 in group 12", {"type": "range", "signal": "S01", "min": 100.0, "max": 140.0, "group": "12"}),
])
def test_parser_variants(text, expected):
    assert parse_rule_text(text, CATALOG) == expected


def test_parser_rejects_unknown_and_arbitrary():
    assert parse_rule_text("S99 must stay between 1 and 2", CATALOG) is None
    assert parse_rule_text("import os; os.system('rm -rf /')", CATALOG) is None
    assert parse_rule_text("keep the reactor warm", CATALOG) is None


def test_role_names_only_when_allowed():
    cat = [SignalInfo(alias="S01", column="c1", instrument="pressure", unit_operation="reactor"), SignalInfo(alias="S02", column="c2", instrument="pressure", unit_operation="separator"), SignalInfo(alias="S03", column="c3", instrument="temperature", unit_operation="reactor")]
    assert parse_rule_text("reactor temperature must stay below 130", cat, allow_role_names=False) is None
    r = parse_rule_text_detailed("reactor temperature must stay below 130", cat, allow_role_names=True)
    assert r.spec == {"type": "range", "signal": "S03", "max": 130.0} and r.confidence < 0.9
    amb = parse_rule_text_detailed("pressure must stay below 130", cat, allow_role_names=True)
    assert amb.spec is None and amb.candidates and set(amb.candidates["pressure"]) == {"S01", "S02"}


def test_explanations_are_plain_language():
    for line in load_rules_file(EXAMPLE):
        spec = parse_rule_text(line, CATALOG)
        text = explain_rule(spec)
        assert spec["type"] != "cross_signal" or "whenever" in text.lower()
        assert text.endswith(".") and len(text) > 20


# ------------------------------------------------------------------ compile + lifecycle + batch evaluation
@pytest.fixture
def rule_ws(tmp_path):
    settings = make_settings(tmp_path)
    df, truth = make_synthetic(n_groups=6, n_samples=200, seed=2, dq_issues=False)
    ws = build_workspace(tmp_path, settings, df, truth, run_id="run_rules")
    return ws, settings, df, alias_map(df, truth["signal_columns"])


def test_compile_rule_template_and_fallback(rule_ws):
    ws, settings, _, _ = rule_ws
    r = compile_rule(ws, settings, "S03 must stay between 100 and 140.")
    assert r.id == "RULE-001" and r.status == "draft" and r.compile_source == "template" and r.compiled["type"] == "range"
    assert r.compile_confidence >= 0.9 and r.inference_ids and ws.inferences.get(r.inference_ids[0]).evidence_ids
    assert ws.rules()[0].id == "RULE-001"
    # not parseable and no LLM available -> draft without compiled form, with an explanation
    r2 = compile_rule(ws, settings, "keep the reactor gently warm at all times")
    assert r2.id == "RULE-002" and r2.compiled is None and r2.status == "draft" and "Could not compile" in r2.compile_explanation
    assert len(ws.rules()) == 2
    assert any(e.action == "rule" for e in ws.log.entries(object_type="rule"))


def test_compile_rule_uses_llm_when_available(rule_ws, monkeypatch):
    ws, settings, _, _ = rule_ws
    import tpm.llm as llm
    from tpm.contracts import LLMResult

    def fake_complete(task, payload, **kw):
        assert task == "rule_compile" and "signal_catalog" in payload
        assert "schema" not in payload and kw.get("schema"), "the closed rule schema travels through schema=, not in the payload (egress guard)"
        return LLMResult(text="", data={"type": "range", "signal": "S02", "max": 2800}, source="llm-local:test", route="local", ok=True)

    monkeypatch.setattr(llm, "complete", fake_complete)
    r = compile_rule(ws, settings, "keep S02 comfortably under 2800 at all times please")
    assert r.compiled == {"type": "range", "signal": "S02", "max": 2800.0} and r.compile_source == "llm-local:test" and 0 < r.compile_confidence < 0.9

    def bad_complete(task, payload, **kw):
        return LLMResult(text='{"type": "python", "code": "print(1)"}', source="llm-local:test", route="local", ok=True)

    monkeypatch.setattr(llm, "complete", bad_complete)
    r2 = compile_rule(ws, settings, "do something clever with S02")
    assert r2.compiled is None and "closed schema" in r2.compile_explanation


def test_rule_lifecycle_and_evaluation(rule_ws):
    ws, settings, df, aliases = rule_ws
    a_flow = aliases["flow_a"]
    r = compile_rule(ws, settings, f"{a_flow} must not exceed 105.")
    dec = HumanDecision(actor_name="ann", role="engineer", action="approve", object_type="rule", object_id=r.id)
    eff = apply_override(ws, settings, dec)
    assert eff["status"] == "active" and ws.rules()[0].status == "active"
    settings.rules.auto_activate_on_approve = False
    r2 = compile_rule(ws, settings, f"{a_flow} must not drop below 20.")
    assert apply_override(ws, settings, HumanDecision(actor_name="ann", role="engineer", action="approve", object_type="rule", object_id=r2.id))["status"] == "approved"
    assert apply_override(ws, settings, HumanDecision(actor_name="bob", role="reviewer", action="reject", object_type="rule", object_id=r2.id))["status"] == "rejected"
    r3 = compile_rule(ws, settings, "nonsense that cannot compile at all")
    eff = apply_override(ws, settings, HumanDecision(actor_name="ann", role="engineer", action="approve", object_type="rule", object_id=r3.id))
    assert "error" in eff
    eff = apply_override(ws, settings, HumanDecision(actor_name="ann", role="engineer", action="edit", object_type="rule", object_id=r3.id, new_value={"text": f"{a_flow} must not change by more than 50 per sample"}))
    assert eff["status"] == "draft" and eff["rule"]["compiled"]["type"] == "rate_of_change"
    eff = apply_override(ws, settings, HumanDecision(actor_name="ann", role="engineer", action="edit", object_type="rule", object_id=r3.id, new_value={"compiled": {"type": "stuck", "signal": a_flow, "max_constant_samples": 10}}))
    assert eff["rule"]["compile_source"] == "human"
    assert apply_override(ws, settings, HumanDecision(actor_name="ann", role="engineer", action="retire", object_type="rule", object_id=r.id))["status"] == "retired"
    # activate again and evaluate over batches
    apply_override(ws, settings, HumanDecision(actor_name="ann", role="engineer", action="activate", object_type="rule", object_id=r.id))
    from tpm.quality.batches import define_batches

    batches = define_batches(ws, settings)
    checks = run_active_rules(ws, settings, batches)
    assert checks and all(c.category == "rule" and c.rule_id == r.id and c.check_type == f"rule:{r.id}" for c in checks)
    fails = [c for c in checks if c.status == "fail"]
    assert fails, "flow_a exceeds 105 regularly in the synthetic data"
    assert all(c.evidence_ids and c.row_start is not None and c.values["n_violating"] > 0 for c in fails)
    assert all(c.row_start >= 0 for c in fails)
    # violations really are above the limit
    b = next(b for b in batches if b["batch_id"] == fails[0].batch_id)
    seg = df["flow_a"].iloc[fails[0].row_start : fails[0].row_end + 1]
    assert seg.max() > 105
    assert any(c.rule_id == r.id for c in ws.checks())
    human = ws.log.entries(actor_prefix="human:ann")
    assert human and any(e.action.startswith("rule_") for e in human)


def test_operating_rule_violations_do_not_lower_trust(rule_ws):
    """A process rule being broken is a process condition for detection, not a reason to distrust the sensor;
    only data-quality-like rule types (missing, stuck) affect trust."""
    from tpm.quality.trust import compute_trust

    ws, settings, df, aliases = rule_ws
    a = aliases["flow_a"]
    frame = df.iloc[:400].copy()
    frame["__row__"] = np.arange(400)
    frame.loc[100:200, "flow_a"] = 500.0  # violates a range rule
    frame.loc[250:330, "flow_a"] = float(frame.loc[250, "flow_a"])  # violates a stuck rule
    from tpm.contracts import Rule
    from tpm.quality.rules import rule_check_result

    r_range = Rule(id="RULE-901", text=f"{a} must not exceed 200", status="active", compiled={"type": "range", "signal": a, "max": 200})
    r_stuck = Rule(id="RULE-902", text=f"{a} must not stay constant for more than 30 samples", status="active", compiled={"type": "stuck", "signal": a, "max_constant_samples": 30})
    colmap = {a: "flow_a"}
    c_range = rule_check_result(ws, r_range, run_rule(r_range, frame, colmap), "BX", 400)
    c_stuck = rule_check_result(ws, r_stuck, run_rule(r_stuck, frame, colmap), "BX", 400)
    assert c_range.status == "fail" and c_stuck.status == "fail"
    assert c_range.values["rule_type"] == "range" and c_stuck.values["rule_type"] == "stuck"
    t_none = compute_trust(settings, "BX", [], n_signals=12)
    t_range = compute_trust(settings, "BX", [c_range], n_signals=12)
    t_stuck = compute_trust(settings, "BX", [c_stuck], n_signals=12)
    assert t_range["trust_score"] == t_none["trust_score"] == 1.0 and t_range["untrusted_signals"] == []
    assert t_stuck["trust_score"] < 1.0 and t_stuck["untrusted_signals"] == [a]
    v = trust_verdict(ws, settings, "BX", [c_range, c_stuck], n_signals=12, persist=False)
    assert v.untrusted_signals == [a] and c_stuck.check_id in v.check_ids and c_range.check_id not in v.check_ids


def test_add_rules_from_file_and_run_quality_option(tmp_path):
    settings = make_settings(tmp_path)
    df, truth = make_synthetic(n_groups=6, n_samples=200, seed=4, dq_issues=False)
    ws = build_workspace(tmp_path, settings, df, truth, run_id="run_file")
    rules = add_rules_from_file(ws, settings, EXAMPLE)
    assert len(rules) == 8 and all(r.compiled for r in rules) and all(r.status == "draft" for r in rules)
    from tpm.quality import run_quality

    ws2 = build_workspace(tmp_path, settings, df, truth, run_id="run_file2")
    summary = run_quality(ws2, settings, {"options": {"rules_file": str(EXAMPLE)}})
    assert summary["n_rules_loaded"] == 8 and summary["n_rule_checks"] > 0
    assert all(r.status == "active" for r in ws2.rules())
    ws3 = build_workspace(tmp_path, settings, df, truth, run_id="run_file3")
    summary3 = run_quality(ws3, settings, {"options": {}})
    assert summary3["n_rules_loaded"] == 0 and summary3["n_rule_checks"] == 0 and not ws3.rules()


def test_load_rules_file_formats(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text("rules:\n  - S01 must stay between 1 and 2\n  - text: S02 must not exceed 5\n", encoding="utf-8")
    assert load_rules_file(p) == ["S01 must stay between 1 and 2", "S02 must not exceed 5"]
    p2 = tmp_path / "rules.txt"
    p2.write_text("# comment\n\n- S01 must stay between 1 and 2\n1. S02 must not exceed 5\nThis paragraph explains the rules and must be ignored.\n", encoding="utf-8")
    assert load_rules_file(p2) == ["S01 must stay between 1 and 2", "S02 must not exceed 5"]
