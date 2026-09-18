"""Agent B: baseline data-quality checks, batches and trust verdicts on the synthetic fixture."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.synth import make_synthetic
from tests.test_b_helpers import alias_map, batch_of_row, build_workspace, make_settings
from tpm.quality import check_batch, run_quality
from tpm.quality.batches import define_batches
from tpm.quality.checks import _quality_context, run_checks_for_batch
from tpm.quality.trust import trust_verdict


@pytest.fixture(scope="module")
def quality_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("q")
    settings = make_settings(tmp)
    df, truth = make_synthetic(n_groups=24, n_samples=400, seed=1)
    ws = build_workspace(tmp, settings, df, truth)
    summary = run_quality(ws, settings, {"options": {}, "progress": lambda f, m="": None})
    return {"ws": ws, "settings": settings, "df": df, "truth": truth, "summary": summary, "aliases": alias_map(df, truth["signal_columns"])}


def _checks_for(qr, check_type: str, signal: str | None = None):
    out = []
    for c in qr["ws"].checks():
        if c.check_type != check_type:
            continue
        if signal is not None and signal not in c.signals:
            continue
        out.append(c)
    return out


def _overlaps(c, row_start: int, row_end: int) -> bool:
    return c.row_start is not None and c.row_end is not None and c.row_start <= row_end and c.row_end >= row_start


def test_batches_cover_all_rows(quality_run):
    ws = quality_run["ws"]
    batches = ws.read_json("batches")
    n_rows = len(quality_run["df"])
    assert batches[0]["row_start"] == 0 and batches[-1]["row_end"] == n_rows  # half-open, same shape as ingest.stream
    for a, b in zip(batches, batches[1:]):
        assert b["row_start"] == a["row_end"]
    assert all(b["batch_id"].startswith("B") and b["group_ids"] and b["n_rows"] == b["row_end"] - b["row_start"] for b in batches)
    assert quality_run["summary"]["n_batches"] == len(batches)


def test_existing_batches_json_is_reused_in_either_convention(tmp_path):
    settings = make_settings(tmp_path)
    df, truth = make_synthetic(n_groups=4, n_samples=100, seed=9, dq_issues=False)
    ws = build_workspace(tmp_path, settings, df, truth, run_id="run_reuse")
    ws.write_json("batches", [{"batch_id": "B0001", "row_start": 0, "row_end": 200, "n_rows": 200, "group_ids": ["1"]}, {"batch_id": "B0002", "row_start": 200, "row_end": 400, "n_rows": 200, "group_ids": ["2", "3"]}])
    b = define_batches(ws, settings)
    assert [(x["row_start"], x["row_end"]) for x in b] == [(0, 200), (200, 400)]
    ws.write_json("batches", [{"batch_id": "B0001", "row_start": 0, "row_end": 199, "n_rows": 200}, {"batch_id": "B0002", "row_start": 200, "row_end": 399, "n_rows": 200}])
    b = define_batches(ws, settings)
    assert [(x["row_start"], x["row_end"]) for x in b] == [(0, 200), (200, 400)]


def test_batches_by_time_window(tmp_path):
    settings = make_settings(tmp_path)
    settings.batch.window_seconds = 180 * 50  # 50 samples of 180 s
    df, truth = make_synthetic(n_groups=4, n_samples=200, seed=3, with_timestamp=True, dq_issues=False)
    ws = build_workspace(tmp_path, settings, df, truth, run_id="run_time")
    batches = define_batches(ws, settings)
    assert len(batches) >= 10
    assert batches[0]["time_start"] is not None and batches[0]["n_rows"] >= settings.batch.min_rows
    assert batches[-1]["row_end"] == len(df) and batches[0]["method"] == "time_window"


@pytest.mark.parametrize("dq_type,check_type", [("missing_block", "missing"), ("spike_out_of_range", "out_of_range"), ("frozen_block", "stuck"), ("unit_shift", "unit_shift"), ("duplicate_rows", "duplicate_rows")])
def test_injected_dq_issues_are_detected(quality_run, dq_type, check_type):
    inj = next(d for d in quality_run["truth"]["dq"] if d["type"] == dq_type)
    signal = quality_run["aliases"].get(inj["signal"]) if inj["signal"] else None
    hits = [c for c in _checks_for(quality_run, check_type, signal) if _overlaps(c, inj["row_start"], inj["row_end"])]
    assert hits, f"{dq_type} on {signal} rows {inj['row_start']}-{inj['row_end']} not detected"
    c = hits[0]
    assert c.status in ("warn", "fail") and 0 < c.severity <= 1
    assert c.evidence_ids and quality_run["ws"].evidence.get(c.evidence_ids[0]) is not None
    assert c.category in ("completeness", "validity", "consistency", "timeliness")
    assert c.statement and c.batch_id


def test_held_sampled_signal_is_not_stuck(quality_run):
    held = quality_run["aliases"]["comp_a"]
    assert not _checks_for(quality_run, "stuck", held)
    const = quality_run["aliases"]["const_c"]
    assert not _checks_for(quality_run, "stuck", const)
    actuators = [quality_run["aliases"]["valve_1"], quality_run["aliases"]["valve_2"]]
    for a in actuators:
        assert not _checks_for(quality_run, "stuck", a)


def test_trust_verdict_flips_for_affected_batch(quality_run):
    ws = quality_run["ws"]
    batches = ws.read_json("batches")
    verdicts = {v.batch_id: v for v in ws.trust()}
    assert set(verdicts) == {b["batch_id"] for b in batches}
    inj = next(d for d in quality_run["truth"]["dq"] if d["type"] == "frozen_block")
    b = batch_of_row(batches, inj["row_start"])
    v = verdicts[b["batch_id"]]
    signal = quality_run["aliases"][inj["signal"]]
    # the affected signal is marked either batch-wide or row-scoped (a frozen block covering a small share of
    # the batch is a local problem: detection treats it as a data issue in those rows only)
    local = [e for e in v.local_untrusted if e["signal"] == signal and e["row_start"] <= inj["row_end"] and e["row_end"] >= inj["row_start"]]
    assert signal in v.untrusted_signals or local
    assert signal in v.statement and ("cannot be trusted" in v.statement or "unreliable only in specific rows" in v.statement)
    others = [x.trust_score for k, x in verdicts.items() if k != b["batch_id"]]
    assert v.trust_score <= max(others)
    clean = [x for x in verdicts.values() if not x.untrusted_signals and not x.reasons]
    assert clean and all(x.trust_score >= 0.95 and x.trusted for x in clean)


def test_batch_becomes_untrusted_when_many_signals_fail(tmp_path):
    settings = make_settings(tmp_path)
    df, truth = make_synthetic(n_groups=6, n_samples=200, seed=5, dq_issues=False)
    # freeze most continuous signals in the second half of the data
    n = len(df)
    for col in ("flow_a", "press_r", "temp_r", "level_s", "flow_b", "temp_s", "power_c"):
        df.loc[n // 2 :, col] = float(df.loc[n // 2, col])
    ws = build_workspace(tmp_path, settings, df, truth, run_id="run_untrusted")
    summary = run_quality(ws, settings, {"options": {}})
    assert summary["untrusted_batches"], "batches with most signals frozen must be untrusted"
    verdicts = {x.batch_id: x for x in ws.trust()}
    bad = [verdicts[b] for b in summary["untrusted_batches"]]
    assert all(not v.trusted and v.trust_score < settings.quality.trust_fail_threshold for v in bad)
    # the batch where the freeze starts mid-way may be row-scoped; fully frozen batches mark the signals batch-wide
    assert max(len(v.untrusted_signals) for v in bad) >= 5


def test_timeliness_checks_with_timestamps(tmp_path):
    settings = make_settings(tmp_path)
    df, truth = make_synthetic(n_groups=6, n_samples=200, seed=7, with_timestamp=True)
    ws = build_workspace(tmp_path, settings, df, truth, run_id="run_ts")
    run_quality(ws, settings, {"options": {}})
    gaps = [c for c in ws.checks() if c.check_type == "gap"]
    assert gaps, "a 45-minute jump in a 3-minute series must be reported as a gap"
    inj = next(d for d in truth["dq"] if d["type"] == "timestamp_gap")
    assert any(_overlaps(c, inj["row_start"] - 1, inj["row_start"]) for c in gaps)


def test_check_batch_stream_path(quality_run):
    ws, settings, df = quality_run["ws"], quality_run["settings"], quality_run["df"]
    aliases = quality_run["aliases"]
    sub = df.iloc[:600].copy()
    # stream frame with alias columns and a fresh frozen block plus a dropout
    sub = sub.rename(columns=aliases)
    sub.loc[100:250, aliases["temp_r"]] = float(sub.loc[100, aliases["temp_r"]])
    sub[aliases["press_r"]] = np.nan
    checks, verdict = check_batch(ws, settings, sub, "STREAM-1")
    types = {(c.check_type, tuple(c.signals)) for c in checks}
    assert ("stuck", (aliases["temp_r"],)) in types
    assert ("dropout", (aliases["press_r"],)) in types
    assert verdict.batch_id == "STREAM-1" and aliases["press_r"] in verdict.untrusted_signals
    assert any(v.batch_id == "STREAM-1" for v in ws.trust())
    ids = [c.check_id for c in checks]
    assert len(ids) == len(set(ids)) and all(i.startswith("CHK-") for i in ids)


def test_check_batch_without_catalog(tmp_path):
    """Stream frame arriving before any profile artifacts: aliases are derived from the frame."""
    settings = make_settings(tmp_path)
    from tpm.workspace import Workspace

    ws = Workspace(run_id="run_bare", settings=settings)
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"S01": rng.normal(size=300), "S02": rng.normal(size=300)})
    df.loc[50:120, "S02"] = 1.5
    checks, verdict = check_batch(ws, settings, df, "B-bare")
    assert any(c.check_type == "stuck" and c.signals == ["S02"] for c in checks)
    assert 0.0 <= verdict.trust_score <= 1.0


def test_run_quality_summary_and_log(quality_run):
    s = quality_run["summary"]
    assert s["n_checks"] > 0 and s["n_fail"] > 0 and s["seconds"] >= 0
    ws = quality_run["ws"]
    entries = ws.log.entries(actor_prefix="system:quality")
    assert any(e.action == "trust" for e in entries) and any(e.action == "stage" for e in entries)
    assert ws.log.verify_chain()["ok"]
    # no relation break in clean batches: the derived signal keeps its formula
    assert all(c.category != "rule" for c in ws.checks())


def test_time_budget_stride(tmp_path):
    settings = make_settings(tmp_path)
    df, truth = make_synthetic(n_groups=8, n_samples=300, seed=11)
    ws = build_workspace(tmp_path, settings, df, truth, run_id="run_stride")
    batches = define_batches(ws, settings)
    qctx = _quality_context(ws, settings)
    checks = run_checks_for_batch(ws, settings, batches[0], qctx, stride=3)
    assert checks and all("EV-" in e for c in checks for e in c.evidence_ids)
    v = trust_verdict(ws, settings, batches[0]["batch_id"], checks, n_signals=12, persist=False)
    assert 0.0 <= v.trust_score <= 1.0
