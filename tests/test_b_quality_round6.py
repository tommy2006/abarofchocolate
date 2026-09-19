"""Agent B, round 6 (expert review items 10, 18, 19, 20, 7, 5): grouped common-mode findings, record-level trust,
plausible ranges, honest "not testable" timeliness, a confidence on every check, domain-aware wording.

All data here is synthetic and generic (no dataset-specific values): continuous noisy signals, a percentage-like
signal, a non-negative signal, injected record-level problems.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tests.test_b_helpers import build_workspace, make_settings
from tpm.quality import run_quality
from tpm.quality._common import check_confidence, common_mode_k, is_problem
from tpm.quality.checks import BatchAccumulator, _quality_context
from tpm.quality.grouping import cluster_blocks, explained_mask, find_common_blocks
from tpm.quality.plausibility import percentage_like, plausible_range
from tpm.quality.trust import DUP_ROW_SCOPE_MIN, RECORD_BATCH_SHARE, compute_trust


# ------------------------------------------------------------------ fixtures
def _frame(n_runs: int = 4, n: int = 500, n_sig: int = 10, seed: int = 0) -> pd.DataFrame:
    """Generic multi-run table: n_sig noisy continuous signals (AR(1) + noise, distinct levels), a percentage-like
    valve that sometimes sits at 100, a non-negative flow near zero."""
    rng = np.random.default_rng(seed)
    frames = []
    for r in range(n_runs):
        d = {"run": np.full(n, r + 1), "sample": np.arange(1, n + 1)}
        for j in range(n_sig):
            e = rng.normal(0, 1.0, n)
            x = np.zeros(n)
            for t in range(1, n):
                x[t] = 0.9 * x[t - 1] + e[t]
            d[f"sig_{j:02d}"] = 50.0 + 10.0 * j + 3.0 * x + rng.normal(0, 0.5, n)
        v = np.clip(55 + 50 * np.sin(np.arange(n) / 25.0 + r) + rng.normal(0, 3, n), 0, 100)  # a valve that is fully open now and then
        d["valve"] = v
        d["flow_nn"] = np.abs(rng.normal(0.5, 0.4, n))
        frames.append(pd.DataFrame(d))
    return pd.concat(frames, ignore_index=True)


def _ws(tmp_path, df: pd.DataFrame, run_id: str, one_batch: bool = True, domain: dict | None = None, roles: dict | None = None):
    settings = make_settings(tmp_path)
    sig_cols = [c for c in df.columns if c not in ("run", "sample", "timestamp")]
    truth = {"signal_roles": {c: (roles or {}).get(c, "continuous_measured") for c in sig_cols}}
    ws = build_workspace(tmp_path, settings, df, truth, run_id=run_id, with_relations=False)
    if one_batch:
        ws.write_json("batches", [{"batch_id": "B0001", "row_start": 0, "row_end": len(df), "n_rows": len(df), "group_ids": []}])
    if domain is not None:
        ws.write_json("domain", domain)
    return ws, settings


def _alias(df: pd.DataFrame, col: str) -> str:
    sig_cols = [c for c in df.columns if c not in ("run", "sample", "timestamp")]
    return f"S{sig_cols.index(col) + 1:02d}"


# ------------------------------------------------------------------ grouping (pure)
def test_common_mode_k():
    assert common_mode_k(52) == 3 and common_mode_k(125) == 3 and common_mode_k(30) == 3
    assert common_mode_k(12) == 2 and common_mode_k(5) == 2 and common_mode_k(1) == 2


def test_find_common_blocks_and_membership():
    iv = {f"S{i:02d}": [(100 + i, 199)] for i in range(1, 6)}  # 5 signals frozen at (almost) the same rows
    iv["S09"] = [(150, 160)]  # short run inside the block: a member (half of its own finding lies inside)
    iv["S10"] = [(500, 600)]  # alone elsewhere: not common mode
    blocks = find_common_blocks(iv, k=3, min_len=15)
    assert len(blocks) == 1
    b = blocks[0]
    assert b["row_start"] == 103 and b["row_end"] == 199
    assert set(b["members"]) == {"S01", "S02", "S03", "S04", "S05", "S09"}
    assert not find_common_blocks(iv, k=3, min_len=200)
    cl = cluster_blocks(blocks)
    assert len(cl) == 1 and cl[0]["n_rows"] == 97
    m = explained_mask([(100, 199), (300, 330)], blocks)
    assert m.tolist() == [True, False]


# ------------------------------------------------------------------ item 18: one frozen block, not 30 checks
def test_common_mode_freeze_is_one_grouped_check(tmp_path):
    df = _frame()
    frozen = [f"sig_{j:02d}" for j in range(8)]
    a, b = 1200, 1399  # 200 rows (10 % of the batch) frozen in 8 signals at once
    for c in frozen:
        df.loc[a:b, c] = float(df.loc[a, c])
    solo = "sig_09"  # an independent dead sensor elsewhere must still be its own stuck check
    df.loc[300:420, solo] = float(df.loc[300, solo])
    ws, settings = _ws(tmp_path, df, "run_cm")
    run_quality(ws, settings, {"options": {}})
    checks = ws.checks()
    blocks = [c for c in checks if c.check_type == "frozen_block"]
    assert len(blocks) == 1, [c.statement for c in blocks]
    fb = blocks[0]
    members = {_alias(df, c) for c in frozen}
    assert members <= set(fb.signals) and fb.category == "consistency"
    assert f"frozen in {len(members)} signals at once" in fb.statement and "not 8 broken sensors" in fb.statement
    assert fb.row_start == a and fb.row_end == b
    assert set(fb.values["members"]) >= members and all(fb.values["members"][m]["longest_run"] >= 150 for m in members)  # traceable
    stuck = [c for c in checks if c.check_type == "stuck"]
    assert all(not (set(c.signals) & members) for c in stuck), "per-signal stuck checks inside the block must not be reported again"
    assert any(c.signals == [_alias(df, solo)] for c in stuck)
    # the block is a record-level problem: its rows are untrusted for every signal, and at 10 % the batch as a whole
    v = ws.trust()[0]
    assert v.untrusted_rows and any(r["check_type"] == "frozen_block" and r["row_start"] == a for r in v.untrusted_rows)
    assert not v.trusted and v.statement.startswith("This data cannot be trusted as a whole")


def test_small_freeze_block_is_row_scoped_and_batch_stays_trusted(tmp_path):
    df = _frame()
    for c in ("sig_01", "sig_02", "sig_03", "sig_04"):
        df.loc[700:739, c] = float(df.loc[700, c])  # 40 rows = 2 % of the batch
    ws, settings = _ws(tmp_path, df, "run_cm_small")
    run_quality(ws, settings, {"options": {}})
    fb = [c for c in ws.checks() if c.check_type == "frozen_block"]
    assert len(fb) == 1
    v = ws.trust()[0]
    assert v.trusted and v.untrusted_rows and 0 < v.untrusted_row_share < RECORD_BATCH_SHARE
    assert "usable only in part" in v.statement
    assert {e["signal"] for e in v.local_untrusted} >= set(fb[0].signals)  # detection sees the members as data problems there


# ------------------------------------------------------------------ item 10: duplicates trip the trust verdict
@pytest.mark.parametrize("share,trusted,row_scoped", [(0.01, True, False), (0.05, True, True), (0.16, False, True)])
def test_duplicate_rows_share_drives_trust(tmp_path, share, trusted, row_scoped):
    df = _frame()
    n = len(df)
    k = int(round(share * n))
    src = df.iloc[100 : 100 + k].copy()
    df.iloc[n - k :, 2:] = src.iloc[:, 2:].to_numpy()  # the last k rows repeat earlier rows exactly (run/sample differ)
    ws, settings = _ws(tmp_path, df, f"run_dup_{int(share * 100)}")
    run_quality(ws, settings, {"options": {}})
    d = [c for c in ws.checks() if c.check_type == "duplicate_rows"]
    assert d and abs(d[0].values["fraction"] - k / n) < 0.002
    v = ws.trust()[0]
    assert v.trusted is trusted
    assert bool(v.untrusted_rows) is row_scoped
    if not trusted:
        assert v.statement.startswith("This data cannot be trusted as a whole") and "exact copies" in v.statement
        assert v.trust_score < settings.quality.trust_fail_threshold
    s = ws.read_json("quality_summary.json")
    assert s["verdict"] in (("untrusted",) if not trusted else ("usable_with_problems",))
    if not trusted:
        assert s["statement"].startswith("This data cannot be trusted as a whole")


def test_duplicates_are_found_across_read_chunks(tmp_path):
    df = _frame(n_runs=2, n=400)
    df.iloc[700:720, 2:] = df.iloc[10:30, 2:].to_numpy()
    ws, settings = _ws(tmp_path, df, "run_dup_chunks")
    qctx = _quality_context(ws, settings)
    acc = BatchAccumulator(ws, settings, "B0001", qctx["catalog"], qctx["stats"], schema=qctx["schema"], plausible=qctx["plausible"])
    data = pd.read_parquet(ws.path("dataset"))
    acc.update(data.iloc[:400])  # the originals are in the first chunk, the copies in the second
    acc.update(data.iloc[400:])
    assert acc.dup_rows == 20


def test_record_thresholds_are_consistent():
    assert 0 < DUP_ROW_SCOPE_MIN < RECORD_BATCH_SHARE <= 0.25


# ------------------------------------------------------------------ item 19: plausibility vs out_of_range
def test_plausible_range_derivation():
    st = {"q01": 0.0, "q99": 100.0, "min": -0.2, "max": 100.0, "scale": 20.0}
    assert percentage_like(st)
    pr = plausible_range(st)
    assert pr["hint"] == "percentage" and pr["lo"] < 0 < 100 < pr["hi"] <= 102
    temp = {"q01": 88.0, "q99": 96.0, "min": 80.0, "max": 100.3, "scale": 1.7}  # a temperature just below 100 is not a percentage
    assert not percentage_like(temp)
    spiky = {"q01": 0.0, "q99": 0.5, "min": -0.3, "max": 1e5, "scale": 0.1}  # outliers must not make a small signal "use the 0..100 scale"
    assert not percentage_like(spiky) and plausible_range(spiky)["hint"] == "non_negative"
    nn = plausible_range({"q01": 0.05, "q99": 1.0, "min": 0.0, "max": 1.2, "scale": 0.2})
    assert nn["hint"] == "non_negative" and -0.05 < nn["lo"] < 0 and "non-negative" in nn["lo_why"]
    far = plausible_range({"q01": 2600.0, "q99": 2800.0, "min": 2500.0, "max": 2900.0, "scale": 40.0})
    assert far["hint"] is None and far["lo"] == pytest.approx(2000.0) and far["hi"] == pytest.approx(3400.0)
    widened = plausible_range(nn and {"q01": 0.05, "q99": 1.0, "min": 0.0, "max": 1.2, "scale": 0.2}, limits=[(-5.0, None, "RULE-001")])
    assert widened["lo"] == -5.0 and widened["lo_source"] == "rule" and "RULE-001" in widened["rules"]


def test_plausibility_and_out_of_range_never_report_the_same_reading(tmp_path):
    df = _frame()
    s3, valve, flow = "sig_03", "valve", "flow_nn"
    med, mad = float(df[s3].median()), float((df[s3] - df[s3].median()).abs().median())
    df.loc[900, s3] = med + 9 * 1.4826 * mad  # unusual (9 robust sigma) but plausible
    df.loc[500, s3] = -9999.0  # a sentinel: implausible
    df.loc[1100:1102, valve] = 180.0  # a percentage above 100
    df.loc[1500, flow] = -3.0  # a non-negative quantity below 0
    ws, settings = _ws(tmp_path, df, "run_plaus")
    run_quality(ws, settings, {"options": {}})
    checks = ws.checks()
    pl = {c.signals[0]: c for c in checks if c.check_type == "plausibility"}
    oor = {c.signals[0]: c for c in checks if c.check_type == "out_of_range"}
    a3, av, af = _alias(df, s3), _alias(df, valve), _alias(df, flow)
    assert a3 in pl and av in pl and af in pl
    assert pl[av].values["hi_source"] == "percentage" and pl[af].values["lo_source"] == "non_negative"
    assert "plausible range" in pl[a3].statement and "Lower bound" in pl[av].statement or "range comes from" in pl[av].statement
    rows_pl = {r for c in pl.values() for a, b in c.values["events"] for r in range(a, b + 1)}
    rows_oor = {(c.signals[0], r) for c in oor.values() for a, b in c.values["events"] for r in range(a, b + 1)}
    assert not {(s, r) for s, r in rows_oor if any(r in range(a, b + 1) for c in pl.values() if c.signals[0] == s for a, b in c.values["events"])}
    assert 500 in rows_pl and (a3, 900) in rows_oor
    qs = ws.read_json("quality_stats.json")
    assert qs["plausible_ranges"][av]["hint"] == "percentage"


def test_operator_rule_limits_widen_the_plausible_range(tmp_path):
    from tpm.contracts import Rule

    df = _frame()
    df.loc[1500:1504, "flow_nn"] = -2.0
    ws, settings = _ws(tmp_path, df, "run_rule_limits")
    af = _alias(df, "flow_nn")
    ws.write_json("rules", [Rule(id="RULE-001", text=f"{af} must stay at or above -5", status="active", compiled={"type": "range", "signal": af, "min": -5.0})])
    run_quality(ws, settings, {"options": {}})
    assert not [c for c in ws.checks() if c.check_type == "plausibility" and c.signals == [af]], "the operator allows -2"
    assert ws.read_json("quality_stats.json")["plausible_ranges"][af]["lo_source"] == "rule"


# ------------------------------------------------------------------ item 20: timeliness not testable
def test_timeliness_without_time_column_is_not_testable(tmp_path):
    df = _frame(n_runs=2, n=300)
    ws, settings = _ws(tmp_path, df, "run_nt", one_batch=False)
    summary = run_quality(ws, settings, {"options": {}})
    checks = ws.checks()
    nt = [c for c in checks if c.category == "timeliness" and c.status == "not_testable"]
    n_batches = len(ws.read_json("batches"))
    assert len(nt) == n_batches and all(c.statement.startswith("Timeliness cannot be tested: the file has no time column") for c in nt)
    assert not [c for c in checks if c.check_type == "timeliness_ok"]
    assert not is_problem("not_testable") and summary["n_not_testable"] == n_batches
    assert all(c.check_id not in v.check_ids for v in ws.trust() for c in nt)  # neither pass nor fail for trust
    assert ws.read_json("quality_summary.json")["not_testable_categories"] == ["timeliness"]


def test_timeliness_with_time_column_is_tested(tmp_path):
    df = _frame(n_runs=2, n=300)
    df.insert(2, "timestamp", pd.Timestamp("2026-01-01") + pd.to_timedelta(np.arange(len(df)) * 180, unit="s"))  # the helper's schema says 180 s
    ws, settings = _ws(tmp_path, df, "run_ts6", one_batch=False)
    run_quality(ws, settings, {"options": {}})
    checks = ws.checks()
    assert not [c for c in checks if c.status == "not_testable"]
    assert [c for c in checks if c.check_type == "timeliness_ok"]


# ------------------------------------------------------------------ common-mode missing
def test_rows_missing_in_many_signals_are_one_missing_block(tmp_path):
    df = _frame()
    cols = [f"sig_{j:02d}" for j in range(6)]
    df.loc[800:879, cols] = np.nan  # 80 rows missing in 6 of 12 signals (a logger for part of the plant)
    ws, settings = _ws(tmp_path, df, "run_mb")
    run_quality(ws, settings, {"options": {}})
    checks = ws.checks()
    mb = [c for c in checks if c.check_type == "missing_block"]
    assert len(mb) == 1 and set(mb[0].signals) == {_alias(df, c) for c in cols}
    assert "missing in 6 signals at once" in mb[0].statement and mb[0].values["n_rows"] == 80
    assert not [c for c in checks if c.check_type == "missing" and set(c.signals) & set(mb[0].signals)]


# ------------------------------------------------------------------ item 7: a confidence on every check
def test_every_check_has_a_confidence(tmp_path):
    df = _frame()
    df.loc[500, "sig_03"] = -9999.0
    ws, settings = _ws(tmp_path, df, "run_conf", one_batch=False)
    run_quality(ws, settings, {"options": {}})
    for c in ws.checks():
        conf = c.values.get("confidence")
        assert isinstance(conf, float) and 0.0 <= conf <= 1.0, (c.check_type, conf)
        assert isinstance(c.values.get("confidence_basis"), str) and c.values["confidence_basis"]
        if c.status == "not_testable":
            assert conf == 0.0
    marginal, strong = check_confidence(5000, 1.02)[0], check_confidence(5000, 8.0)[0]
    few = check_confidence(8, 8.0)[0]
    assert marginal < 0.6 < strong and few < 0.3


# ------------------------------------------------------------------ item 5: wording follows the kind of data
def test_statements_follow_the_domain(tmp_path):
    df = _frame(n_runs=2, n=400)
    df.loc[100:199, "sig_02"] = float(df.loc[100, "sig_02"])
    ws, settings = _ws(tmp_path, df, "run_records", domain={"domain_likelihood": {"sensor_stream": 0.11, "business_records": 0.43}})
    run_quality(ws, settings, {"options": {}})
    st = [c for c in ws.checks() if c.check_type == "stuck"][0].statement
    assert "copied or default value" in st and "sensor" not in st
    ws2, settings2 = _ws(tmp_path, df, "run_sensor_words")  # no domain.json: sensor data
    run_quality(ws2, settings2, {"options": {}})
    st2 = [c for c in ws2.checks() if c.check_type == "stuck"][0].statement
    assert "dead or stale sensor" in st2


# ------------------------------------------------------------------ stuck share is no longer capped at 48 runs
def test_stuck_share_counts_every_run(tmp_path):
    df = _frame(n_runs=1, n=8000, n_sig=3)
    for start in range(0, 8000, 100):  # 80 frozen runs of 40 rows: 40 % of the signal
        df.loc[start : start + 39, "sig_01"] = float(df.loc[start, "sig_01"])
    ws, settings = _ws(tmp_path, df, "run_many_runs")
    run_quality(ws, settings, {"options": {}})
    st = [c for c in ws.checks() if c.check_type == "stuck" and c.signals == [_alias(df, "sig_01")]]
    assert st and st[0].values["n_runs"] >= 79 and st[0].values["stuck_fraction"] >= 0.39


def test_compute_trust_matches_verdict_for_record_level(tmp_path):
    df = _frame()
    k = int(0.12 * len(df))
    df.iloc[len(df) - k :, 2:] = df.iloc[50 : 50 + k, 2:].to_numpy()
    ws, settings = _ws(tmp_path, df, "run_ct")
    run_quality(ws, settings, {"options": {}})
    v = ws.trust()[0]
    t = compute_trust(settings, "B0001", ws.checks(), n_signals=12, n_rows=len(df))
    assert t["trusted"] is v.trusted is False and t["record_share"] >= RECORD_BATCH_SHARE
    # the assessor's "drop duplicates" what-if restores trust
    t2 = compute_trust(settings, "B0001", [c for c in ws.checks() if c.check_type not in ("duplicate_rows", "duplicate_key")], n_signals=12, n_rows=len(df))
    assert t2["trusted"]
