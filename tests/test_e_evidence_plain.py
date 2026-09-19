"""Agent E: plain-language evidence (tpm/api/evidence_plain.py) and resolution of every citable id through
GET /api/runs/{run}/evidence?ids=...

    .venv\\Scripts\\python.exe -m pytest tests/test_e_evidence_plain.py -q
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tpm.api.evidence_plain import KINDS, dejargon, explain, explain_object

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "tpm" / "api" / "static"
RUN = "run_test_evidence"
DEMO = ROOT / "workspace" / "demo_cli" / "evidence.jsonl"

JARGON = ("{", "}", "AUROC", "auroc", "sigma", "σ", "r=", "R2=", "Spearman", "autocorrelation", "None", "nan", "unknown share")


def assert_plain(text: str, ev: dict) -> None:
    assert isinstance(text, str) and len(text.strip()) >= 25, f"too short for {ev.get('kind')}: {text!r}"
    for j in JARGON:
        assert j not in text, f"{j!r} leaked into the plain text of kind {ev.get('kind')}: {text!r}"
    assert not re.search(r"\br\s*=", text), text
    assert text.strip()[-1] in ".!?…", text
    assert "  " not in text, text
    assert "[llm-" not in text and not re.search(r"\[\s*['\"]", text), text  # no source tags, no list reprs (dict reprs: no braces above)
    for s in ev.get("signals") or []:  # ids stay verbatim so the UI can link them
        if re.fullmatch(r"S\d{2,3}", str(s)) and s in (ev.get("statement") or "") and len(ev.get("signals") or []) <= 2:
            assert s in text, f"{s} lost from {text!r}"


# --------------------------------------------------------------------------------- real evidence
def _demo_items() -> list[dict]:
    if not DEMO.exists():
        return []
    out: dict[tuple, dict] = {}
    for line in DEMO.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        e = json.loads(line)
        out.setdefault((e["kind"], tuple(sorted((e.get("values") or {}).keys()))), e)  # one per kind AND values shape
    return list(out.values())


@pytest.mark.skipif(not DEMO.exists(), reason="workspace/demo_cli is not present")
def test_every_kind_of_the_demo_run_is_explained():
    items = _demo_items()
    kinds = {e["kind"] for e in items}
    assert len(kinds) >= 20, kinds
    for e in items:
        assert_plain(explain(e), e)
    generic = sorted(k for k in kinds if k not in KINDS and not k.startswith("rule"))
    assert not generic, f"kinds of the demo run without their own template: {generic}"


@pytest.mark.skipif(not DEMO.exists(), reason="workspace/demo_cli is not present")
def test_known_demo_sentences_keep_numbers_and_ids():
    by_kind: dict[str, list[dict]] = {}
    for e in _demo_items():
        by_kind.setdefault(e["kind"], []).append(e)
    contrib = next(e for e in by_kind["contribution"] if "shares" in e["values"])
    text = explain(contrib)
    top = next(iter(contrib["values"]["shares"]))
    assert top in text and "more unusual than anything seen in normal operation" in text
    m = re.search(r"Rows (\d+)-(\d+)", contrib["statement"])
    assert m and m.group(1) in text and m.group(2) in text
    corr = next(e for e in by_kind["correlation"] if "r" in e["values"])
    text = explain(corr)
    assert all(s in text for s in corr["signals"]) and "in step" in text and f"{abs(corr['values']['r']) * 100:.0f} %" in text


# --------------------------------------------------------------------------------- synthetic items
SYNTHETIC = [
    {"kind": "correlation", "statement": "S07 and S13 correlate r=0.92 at lag 0", "signals": ["S07", "S13"], "values": {"r": 0.92, "lag": 0}},
    {"kind": "correlation", "statement": "S02 and S04 correlate r=-0.81 (Spearman -0.79) at lag 0", "signals": ["S02", "S04"], "values": {"r": -0.81, "spearman": -0.79, "n": 5000}},
    {"kind": "correlation", "statement": "S05 loses its correlation with S04 (r 0.71 -> 0.04) while every other pair keeps its structure", "signals": ["S05", "S04"], "values": {"r_before": 0.71, "r_after": 0.04}},
    {"kind": "lag", "statement": "S07 leads S13 by 3 samples (cross-correlation +0.88 on levels, prominence 2.1)", "signals": ["S07", "S13"], "values": {"lag": 3, "r_at_lag": 0.88, "method": "level"}},
    {"kind": "lag", "statement": "Onset order in group 1: S01 (row 77) -> S02 (+2) -> S03 (+4)", "signals": ["S01", "S02", "S03"], "values": {"lags": [0, 2, 4]}, "group_id": "1"},
    {"kind": "stuck", "statement": "S06 is frozen at 12.5 for 80 samples in batch B00003 (rows 400-479): looks like a dead or stale sensor, not a process change", "signals": ["S06"], "batch_id": "B00003", "values": {"longest_run": 80, "value": 12.5, "threshold": 30, "stuck_fraction": 0.4, "n_runs": 1, "events": [[400, 479]]}},
    {"kind": "stuck", "statement": "S07: 96% of consecutive samples are identical (runs of ~25)", "signals": ["S07"], "values": {"stuck_fraction": 0.96, "hold_period": 25.0, "hold_regularity": 1.0}},
    {"kind": "contribution", "statement": "Rows 979-1026 (group 3): ensemble 2.3x threshold; contributions S06 69% (stuck), S03 6% (up). Detector agreement 50%.", "signals": ["S06", "S03"], "group_id": "3", "values": {"shares": {"S06": 0.69, "S03": 0.06}, "directions": {"S06": "stuck", "S03": "up"}, "mean_ensemble": 2.3, "peak_ensemble": 3.0}},
    {"kind": "segment", "statement": "Group G0506: ensemble score at or above threshold for rows 252520-252999 (480 rows), mean 6.36x, peak 7.63x; leading signal S51 in 60% of rows", "signals": ["S51", "S44"], "group_id": "G0506", "values": {"row_start": 252520, "row_end": 252999, "n_rows": 480, "mean_norm": 6.36, "peak": 7.63, "leading_signal_shares": {"S51": 0.6, "S44": 0.38}}},
    {"kind": "threshold", "statement": "Fold 0: thresholds calibrated on 1520 validation baseline rows of 4 group(s); pca=1.62, robust_z=1.82; ensemble threshold 1.44.", "values": {"fold": 0, "thresholds": {"pca": 1.62, "robust_z": 1.82}, "ens_threshold": 1.44, "n_fit_rows": 4353}, "n_samples": 1520},
    {"kind": "baseline_candidate", "statement": "Baseline candidate 'robust_covariance' keeps 60% of sampled rows; cross-group separation AUROC 0.94, generalization ratio 1.1, group coverage 100%, consensus with other candidates 0.7, score 0.83.", "values": {"name": "robust_covariance", "fraction": 0.6, "separation_auroc": 0.94, "generalization_ratio": 1.1, "coverage": 1.0, "consensus": 0.7, "score": 0.83}},
    {"kind": "baseline", "statement": "Baseline strategy 'robust_covariance' selected 7595 of 8005 sampled rows (95%) spanning 20 of 20 groups.", "values": {"strategy": "robust_covariance", "fraction": 0.95, "n_groups": 20, "candidate_scores": {"robust_covariance": 0.83, "consensus_of_modes": 0.75}}},
    {"kind": "changepoint", "statement": "Onset at row 979 (group 3), abrupt, second-order change; first signals to move: S06 (lag 0, stuck), S11 (lag +15, up).", "signals": ["S06", "S11"], "group_id": "3", "values": {"row": 979, "kind": "abrupt", "order": "second", "rows_to_80pct": 0, "first_signals": [{"signal": "S06", "lag": 0, "direction": "stuck"}, {"signal": "S11", "lag": 15, "direction": "up"}]}},
    {"kind": "onset", "statement": "Group 1: ensemble score crosses the threshold 1.0 at row 77 and stays above it in 62.0 % of the remaining rows", "group_id": "1", "values": {"onset_row": 77, "peak": 3.3}},
    {"kind": "cascade", "statement": "Sequential failure in group 12, rows 4405-4483: C01 (S01, S09) at lag +0 -> single:S08 (S08) at lag +35.", "signals": ["S01", "S09", "S08"], "group_id": "12", "values": {"chain": [{"from_signal": "S01", "to_signal": "S09", "lag": 0, "strength": 0.9}, {"from_signal": "S09", "to_signal": "S08", "lag": 35, "strength": 0.0}], "gaps": [35], "parent_flag": "FLAG-000007"}},
    {"kind": "propagation", "statement": "S01 -> S02 (+4)", "signals": ["S01", "S02"], "values": {"chain": [{"from_signal": "S01", "to_signal": "S02", "lag": 4}]}},
    {"kind": "pattern", "statement": "PATTERN-A: 4 events in 4 group(s); leading signals S06 (stuck, 24%); dominant cause class data.", "signals": ["S06"], "values": {"n_events": 4, "groups": ["3", "7"], "mean_shares": {"S06": 0.24}, "directions": {"S06": "stuck"}, "lag_order": [["S06", 0.0], ["S09", 5.5]], "cause_classes": {"data": 3, "process": 1}}},
    {"kind": "cause", "statement": "Cause class 'sensor' for rows 10-40 (group 2): S04 is frozen at a constant value while its correlated peers keep moving", "signals": ["S04"], "group_id": "2", "values": {"cause": "sensor", "confidence": 0.8, "untrusted": []}},
    {"kind": "grouping", "statement": "grouping candidate key_columns; columns=run; n_groups=20; contiguity=1.00 -> score 1.00", "values": {"method": "key_columns", "columns": ["run"], "n_groups": 20, "score": 1.0, "median_len": 400.0, "min_len": 400.0, "max_len": 405.0}},
    {"kind": "format", "statement": "delimiter ',' chosen: 15 columns in 100% of sampled lines", "values": {"delimiter": ",", "n_cols": 15, "consistency": 1.0, "scores": {",": 2.9, ";": 0.0}}},
    {"kind": "format", "statement": "header present: first row 0% numeric tokens vs 93% in data rows", "values": {"numeric_fraction_row0": 0.0, "numeric_fraction_data": 0.93, "has_header": True}},
    {"kind": "label_detection", "statement": "1 label-like, 3 metadata, 12 signal columns (structure-based)", "values": {"label_columns": ["fault"], "meta_columns": ["run", "sample", "ts"], "reasons": {"run": "group key column"}}},
    {"kind": "sampling", "statement": "profile subsample: 8005 rows from 20 whole groups (100.0% of 8005 rows)", "values": {"method": "whole_groups", "n_chunks": 20, "n_rows": 8005, "total_rows": 8005, "fraction": 1.0}},
    {"kind": "distribution", "statement": "S01: n=8005, missing 0.0%, mean 101, std 8.22, range [74.6, 130], 7776 distinct, shape unimodal", "signals": ["S01"], "values": {"count": 8005, "missing_rate": 0.0, "mean": 100.5, "std": 8.22, "min": 74.6, "max": 130.0, "n_unique": 7776, "distribution_shape": "unimodal"}},
    {"kind": "fingerprint", "statement": "S02: mean 5, std 1", "signals": ["S02"], "values": {"mean": 5.0, "std": 1.0, "min": 1.0, "max": 9.0, "count": 100}},
    {"kind": "dynamics", "statement": "S01: lag-1 autocorrelation 0.93, noise level 0.37, 0% unchanged steps", "signals": ["S01"], "values": {"autocorr_lag1": 0.93, "noise_level": 0.37, "stuck_fraction": 0.0, "hold_period": 1.0}},
    {"kind": "role", "statement": "S07 structural role actuator_like", "signals": ["S07"], "values": {"role": "actuator_like", "confidence": 0.8}},
    {"kind": "redundancy", "statement": "S11 is reproduced by S01 + S05 (R2=1.00000, 2 regressor(s)) -> likely derived", "signals": ["S11", "S01", "S05"], "values": {"signal": "S11", "partners": ["S01", "S05"], "r2": 1.0, "derived": True}},
    {"kind": "cluster", "statement": "cluster C01: S01, S02, S03 move together (average |r| >= 0.5)", "signals": ["S01", "S02", "S03"], "values": {"members": ["S01", "S02", "S03"]}},
    {"kind": "domain", "statement": "73% of columns are continuous numeric, median lag-1 autocorrelation 0.92, 0% text/categorical columns", "values": {"share_continuous": 0.73, "median_autocorr": 0.92, "share_text": 0.0, "regularity": 1.0, "raw": {"sensor_stream": 0.85, "business_records": 0.02}}},
    {"kind": "out_of_range", "statement": "S02 has 1 values beyond 6 robust sigma of its usual range in batch B00022 (max 3388 sigma; e.g. row 2640: 1e+05)", "signals": ["S02"], "batch_id": "B00022", "values": {"n": 1, "fraction": 0.01, "max_robust_z": 3388.2, "events": [[2640, 2640]]}},
    {"kind": "range", "statement": "All values within 6 robust standard deviations", "batch_id": "B00001", "values": {"max_robust_z": 3.2}},
    {"kind": "missing", "statement": "S06 is missing in 61.0% of batch B00008, longest gap 61 samples (rows 960-1020)", "signals": ["S06"], "batch_id": "B00008", "values": {"missing_rate": 0.61, "n_missing": 61, "longest_missing": 61, "events": [[960, 1020]]}},
    {"kind": "duplicates", "statement": "5 exact duplicate rows in batch B00056 (2.70%; first at rows 6805-6809)", "batch_id": "B00056", "values": {"n": 5, "fraction": 0.027, "events": [[6805, 6809]]}},
    {"kind": "unit_shift", "statement": "S12 changes scale by x10^3 in batch B00047 (rows 5760-5784): likely a unit or decimal-point change", "signals": ["S12"], "batch_id": "B00047", "values": {"k": [3], "events": [[5760, 5784]]}},
    {"kind": "gap", "statement": "1 timestamp gap(s) in batch B00019; largest 92.4 min = 277x the usual period of 20 s (rows 1987-1988)", "batch_id": "B00019", "values": {"n": 1, "max_gap_s": 5544, "period_s": 20, "events": [[1987, 1988]]}},
    {"kind": "timeliness", "statement": "Timestamps advance by 180 s throughout", "batch_id": "B00001", "values": {"max_gap_s": 180}},
    {"kind": "rule_violation", "statement": "RULE-001 violated in batch B00004: 12 samples in 2 episode(s) (above 125); first at rows 300-305; values 126..131. Rule: S03 must stay between 100 and 125.", "signals": ["S03"], "batch_id": "B00004", "values": {"rule_id": "RULE-001", "n_violating": 12, "n_episodes": 2, "episodes": [[300, 305], [340, 345]]}},
    {"kind": "rule_pass", "statement": "RULE-001 holds in batch B00001 (180 samples checked). Rule: S03 must stay between 100 and 125.", "signals": ["S03"], "batch_id": "B00001", "values": {"rule_id": "RULE-001", "n_rows": 180}},
    {"kind": "rule:RULE-001", "statement": "RULE-001: S03 outside [100, 125] in 7 rows", "signals": ["S03"], "batch_id": "B00002", "values": {"violations": 7}},
    {"kind": "trust", "statement": "Batch B00008: trust score 0.79, 1/11 signals unreliable, batch-level severity 0.00", "signals": ["S06"], "batch_id": "B00008", "values": {"trust_score": 0.79, "n_signals": 11, "n_untrusted": 1, "batch_severity": 0.0}},
    {"kind": "learning_curve", "statement": "Learning curve (stability): 10% of 16 groups -> 0.738, 100% of 16 groups -> 0.896; slope at the end +0.102 per full dataset (+/-0.099); no plateau reached", "values": {"fractions": [0.1, 0.4, 1.0], "primary": [0.738, 0.82, 0.896], "slope": 0.102, "slope_uncertainty": 0.099, "diminishing_returns_fraction": None, "estimated_gain_more_data": 0.05}},
    {"kind": "coverage", "statement": "Only 2 of 12 groups cover the low-load regime (S07 below 40); the other regimes have 4 to 6 groups each", "values": {"low_load_groups": 2}},
    {"kind": "regime_coverage", "statement": "3 operating regimes found among 20 groups (silhouette 0.34); shares R1 90%, R2 5%, R3 5%; balance 0.36.", "values": {"k": 3, "silhouette": 0.34, "n_units": 20, "unit_kind": "group", "shares": [0.9, 0.05, 0.05], "balance": 0.36, "thin": ["R2", "R3"]}},
    {"kind": "dq_score", "statement": "Timeliness score 0.74: 17 failing and 0 warning checks over 66 batches, 0 signal(s) involved; worst: 1 timestamps go backwards in batch B00004 (first at row 400)", "values": {"category": "timeliness", "score": 0.74, "n_fail": 17, "n_warn": 0, "n_signals": 0}},
    {"kind": "dq_score", "statement": "Overall data-quality score 0.93 (mean trust 0.94, 0 of 66 batches untrusted)", "values": {"overall": 0.93, "mean_trust": 0.94, "n_untrusted_batches": 0, "n_batches": 66, "n_signals": 11}},
]


@pytest.mark.parametrize("ev", SYNTHETIC, ids=lambda e: e["kind"] + ":" + ",".join(sorted(e.get("values", {})))[:40])
def test_synthetic_kinds(ev):
    assert_plain(explain(ev), ev)


def test_the_examples_of_the_brief_read_as_intended():
    corr = explain(SYNTHETIC[0])
    assert "S07 and S13 rise and fall together almost perfectly (92 % in step)" in corr and "same part of the process" in corr
    lag = explain(SYNTHETIC[3])
    assert "S13 follows S07 about 3 samples later" in lag and "S07 influences S13" in lag
    stuck = explain(SYNTHETIC[5])
    assert "S06 showed exactly the same value (12.5) for 80 readings in a row" in stuck and "sensor or its connection" in stuck and "B00003" in stuck
    contrib = explain(SYNTHETIC[7])
    assert "Between rows 979 and 1026 (group 3) the process looked 2.3 times more unusual than anything seen in normal operation" in contrib
    assert "S06 alone explains 69 % of that" in contrib
    assert "largest deviation still seen in normal operation" in explain(SYNTHETIC[9])
    cand = explain(SYNTHETIC[10])
    assert "0.94 (1.0 = perfectly" in cand and "60 %" in cand
    oor = explain(SYNTHETIC[30])
    assert "times its normal spread" in oor and "3,388" in oor and "B00022" in oor


def test_unknown_kind_and_degenerate_inputs_fall_back_gracefully():
    ev = {"kind": "brand_new_kind", "statement": "S07 and S13 correlate r=0.92 (Spearman 0.9) with AUROC 0.94, 7 robust sigma above; details {'a': 1, 'b': [1, 2]} [llm-local:x] {\"text\": \"t\"}", "signals": ["S07", "S13"], "values": {"weird": {"nested": [1, 2, 3]}}}
    text = explain(ev)
    assert_plain(text, ev)
    assert "S07" in text and "S13" in text and "92 % in step" in text and "times its normal spread" in text
    for ev in ({}, {"kind": "stuck"}, {"kind": "correlation", "values": {"r": "not a number"}}, {"kind": None, "statement": None, "values": None, "signals": None}, {"kind": "contribution", "values": {"shares": {}}}, {"kind": "cascade", "values": {"chain": ["junk"]}}, {"kind": "learning_curve", "values": {"fractions": [0.1], "primary": []}}):
        out = explain(ev)
        assert isinstance(out, str) and out.strip() and "{" not in out
    # every registered kind survives an item that has nothing but its kind
    for k in KINDS:
        out = explain({"kind": k, "statement": "", "values": {}, "signals": []})
        assert isinstance(out, str) and out.strip() and "{" not in out, k


def test_pydantic_evidence_objects_are_accepted_and_language_is_reserved():
    from tpm.contracts import Evidence

    e = Evidence(id="EV-000001", kind="correlation", signals=["S01", "S02"], statement="S01 and S02 correlate r=0.61 at lag 0", values={"r": 0.61, "lag": 0})
    assert explain(e) == explain(e.model_dump()) == explain(e, lang="fi")
    assert "61 % in step" in explain(e)
    assert "in step" in dejargon("S01 and S02 correlate r=0.61") and "r=" not in dejargon("r=0.61")


def test_objects_other_than_evidence_have_plain_text():
    flag = {"id": "FLAG-000001", "kind": "anomaly", "group_id": "3", "row_start": 979, "row_end": 1026, "score": 2.3, "threshold": 1.0, "likely_cause_class": "data", "confidence": 0.78, "statement": "Anomaly in group 3", "signals_ranked": [{"signal": "S06", "contribution": 0.69, "direction": "stuck"}]}
    text = explain_object("flag", flag)
    assert "FLAG-000001" in text and "2.3 times" in text and "S06 alone explains 69 %" in text
    diag = {"id": "DIAG-000005", "group_id": "3", "fault_type": "sensor fault on S06", "cause_class": "sensor", "confidence": 0.83, "summary": "x\n\n[llm-local:m] {\"summary\": \"y\"}", "ranked_signals": [{"signal": "S06", "contribution": 0.69, "direction": "stuck"}], "critique": {"verdict": "supported"}}
    text = explain_object("diagnosis", diag)
    assert "DIAG-000005" in text and "faulty instrument" in text and "S06" in text and "{" not in text


# --------------------------------------------------------------------------------- API
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient

    from tests.fixtures.fake_workspace import build_fake_workspace
    from tpm.api.server import create_app
    from tpm.config import load_settings

    base = tmp_path_factory.mktemp("e_evidence")
    settings_path = base / "settings.yaml"
    shutil.copy(ROOT / "config" / "settings.yaml", settings_path)
    s = load_settings(settings_path)
    s.workspace_dir = str(base / "workspace")
    ws = build_fake_workspace(s, run_id=RUN, n_groups=8, n_samples=120)
    ws.close()
    app = create_app(settings_path=settings_path, workspace_dir=base / "workspace")
    with TestClient(app) as c:
        yield c


def test_evidence_list_and_ids_carry_plain(client):
    d = client.get(f"/api/runs/{RUN}/evidence", params={"limit": 5000}).json()
    assert d["n"] > 20 and len(d["items"]) == d["n"]
    for e in d["items"]:
        assert e["ref_type"] == "evidence"
        assert_plain(e["plain"], e)
    ids = [e["id"] for e in d["items"][:4]]
    got = client.get(f"/api/runs/{RUN}/evidence", params={"ids": ",".join(ids)}).json()
    assert got["missing"] == [] and [e["id"] for e in got["items"]] == ids
    assert all(e["plain"] and e["statement"] and e["ref_type"] == "evidence" for e in got["items"])
    by_kind = client.get(f"/api/runs/{RUN}/evidence", params={"kind": "correlation", "limit": 3}).json()
    assert by_kind["items"] and all("in step" in e["plain"] or "link" in e["plain"] for e in by_kind["items"])


def test_any_cited_id_resolves_to_an_evidence_like_item(client):
    diag = client.get(f"/api/runs/{RUN}/diagnoses").json()["items"][0]
    flag = client.get(f"/api/runs/{RUN}/flags").json()["items"][0]
    check = client.get(f"/api/runs/{RUN}/checks", params={"status": "fail"}).json()["items"][0]
    inf = next(i for i in client.get(f"/api/runs/{RUN}/inferences").json()["items"] if i["evidence_ids"])
    ev_id = flag["evidence_ids"][0]
    wanted = [diag["id"], flag["id"], check["check_id"], inf["id"], "RULE-001", "PATTERN-A", "EGR-000002", ev_id, "DIAG-999999", "EV-999999", "NOT-AN-ID"]
    d = client.get(f"/api/runs/{RUN}/evidence", params={"ids": ",".join(wanted)}).json()
    assert d["missing"] == ["DIAG-999999", "EV-999999", "NOT-AN-ID"]
    items = {e["id"]: e for e in d["items"]}
    assert list(items) == wanted[:8] and d["n"] == 8
    expect = {diag["id"]: "diagnosis", flag["id"]: "flag", check["check_id"]: "check", inf["id"]: "inference", "RULE-001": "rule", "PATTERN-A": "pattern", "EGR-000002": "egress"}
    for oid, kind in expect.items():
        it = items[oid]
        assert it["kind"] == kind and it["ref_type"] == kind, it
        assert it["statement"].strip(), it
        assert_plain(it["plain"], {"kind": kind})
        assert isinstance(it["values"], dict) and isinstance(it["signals"], list) and isinstance(it["evidence_ids"], list)
        assert it["open"]["view"] in ("diagnoses", "monitor", "quality", "understanding", "dataflow")
        assert len(json.dumps(it)) < 6000, f"{oid}: the item must stay compact"
    assert items[ev_id]["ref_type"] == "evidence" and items[ev_id]["plain"]
    # content: diagnosis -> summary + cause/confidence/top signals and its own evidence to drill into
    it = items[diag["id"]]
    assert it["evidence_ids"] == diag["evidence_ids"] and it["group_id"] == diag["group_id"]
    assert it["values"]["cause"] == diag["cause_class"] and it["values"]["fault_type"] == diag["fault_type"] and diag["id"] in it["plain"]
    assert it["open"] == {"view": "diagnoses", "params": {"diag": diag["id"]}}
    it = items[flag["id"]]
    assert it["evidence_ids"] == flag["evidence_ids"] and it["batch_id"] == flag["batch_id"] and it["open"]["params"] == {"flag": flag["id"]}
    it = items[check["check_id"]]
    assert it["values"]["status"] == "fail" and it["batch_id"] == check["batch_id"] and it["evidence_ids"] == check["evidence_ids"]
    assert it["open"]["params"] == {"batch": check["batch_id"], "check": check["check_id"]} and it["plain"].startswith("This check failed.")
    it = items[inf["id"]]
    assert it["values"]["status"] == inf["status"] and it["evidence_ids"] == inf["evidence_ids"] and inf["claim"][:20] in it["statement"]
    assert "guard" in items["EGR-000002"]["plain"].lower()
    # the nested evidence of a resolved object resolves too (drill-down)
    nested = client.get(f"/api/runs/{RUN}/evidence", params={"ids": ",".join(items[diag["id"]]["evidence_ids"])}).json()
    assert nested["missing"] == [] and nested["n"] == len(set(diag["evidence_ids"]))


def test_sloppy_ids_and_single_object_route(client):
    d = client.get(f"/api/runs/{RUN}/evidence", params={"ids": "diag-1, [FLAG-000001] ,EV-1"}).json()
    assert [e["id"] for e in d["items"]] == ["DIAG-000001", "FLAG-000001", "EV-000001"] and d["missing"] == []
    assert d["items"][0]["requested_id"] == "diag-1"
    r = client.get(f"/api/runs/{RUN}/object/DIAG-000001")
    assert r.status_code == 200 and r.json()["kind"] == "diagnosis" and r.json()["plain"]
    assert client.get(f"/api/runs/{RUN}/object/DIAG-424242").status_code == 404


def test_object_index_is_cached_until_the_file_changes(client, monkeypatch):
    import tpm.api.evidence_plain as ep

    calls: list[str] = []
    real = ep._load_records
    monkeypatch.setattr(ep, "_load_records", lambda p: (calls.append(p.name), real(p))[1])
    for _ in range(3):
        d = client.get(f"/api/runs/{RUN}/evidence", params={"ids": "DIAG-000001,DIAG-000002,FLAG-000002"}).json()
        assert d["n"] == 3
    assert calls.count("diagnoses.jsonl") <= 1 and calls.count("flags.jsonl") <= 1, calls  # 0 when an earlier test already indexed them
    # a decision rewrites diagnoses.jsonl -> the index follows the file
    body = {"actor_name": "Maija", "role": "engineer", "action": "accept", "object_type": "diagnosis", "object_id": "DIAG-000001", "note": "checked on site"}
    assert client.post(f"/api/runs/{RUN}/decisions", json=body).status_code == 200
    before = len(calls)
    it = client.get(f"/api/runs/{RUN}/evidence", params={"ids": "DIAG-000001"}).json()["items"][0]
    stored = next(x for x in client.get(f"/api/runs/{RUN}/diagnoses").json()["items"] if x["id"] == "DIAG-000001")
    if stored.get("human_status"):
        assert len(calls) == before + 1 and it["values"].get("human_status") == stored["human_status"]


# --------------------------------------------------------------------------------- UI assets
def test_ui_shows_plain_first_and_types_citations():
    core = (STATIC / "js" / "core.js").read_text(encoding="utf-8")
    chat = (STATIC / "js" / "chat.js").read_text(encoding="utf-8")
    for name in ("refTypeOfId", "citeLink", "evidenceItem", "evidenceList", "evidencePanel", "evChips", "showRefModal", "fetchEvidenceFull"):
        assert f"export function {name}" in core or f"export async function {name}" in core, name
    assert "e.plain" in core and "evidence.technical" in core and "evidence.behind" in core and "roleAllows('engineer')" in core
    assert "citations(" in chat and "evidence.sources" in chat and "evidencePanel" in chat
    for lang in ("en", "fi", "sv"):
        d = json.loads((STATIC / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        for k in ("evidence.technical", "evidence.behind", "evidence.sources", "evidence.missingAny"):
            assert d.get(k, "").strip(), f"{lang}:{k}"


@pytest.mark.parametrize("name", ["core.js", "chat.js"])
def test_touched_modules_parse_as_es_modules(name, tmp_path):
    """`node --check file.js` does not parse an ES module strictly (a broken string literal passes); a copy with
    the .mjs extension does."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    target = tmp_path / (Path(name).stem + ".mjs")
    shutil.copy(STATIC / "js" / name, target)
    r = subprocess.run([node, "--check", str(target)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr or r.stdout
