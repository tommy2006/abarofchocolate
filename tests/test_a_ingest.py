"""Agent A: ingest stage tests (readers, schema inference, grouping, labels, time, batches, streaming, overrides)."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.synth import make_synthetic, write_variants
from tpm.contracts import HumanDecision
from tpm.ingest import apply_override, detect_format, ingest_dataframe, run_ingest
from tpm.ingest.stream import align_incoming, build_batches, iter_batches, read_rows, replay, watch_folder
from tpm.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
TE_HEAD = ROOT / "workspace" / "samples" / "te_head.csv"


def _ingest(settings, path, run_id, options=None):
    ws = Workspace(run_id=run_id, settings=settings)
    res = run_ingest(ws, settings, {"source_path": str(path), "options": options or {}, "progress": lambda f, m: None})
    return ws, res


@pytest.fixture(scope="module")
def variants(tmp_path_factory):
    df, truth = make_synthetic(n_groups=12, n_samples=200, seed=1, with_timestamp=True, with_labels=True)
    paths = write_variants(df, tmp_path_factory.mktemp("variants"), "synth")
    return df, truth, paths


@pytest.fixture(scope="module")
def module_settings(tmp_path_factory):
    from tpm.config import load_settings

    s = load_settings()
    s.workspace_dir = str(tmp_path_factory.mktemp("workspace"))
    return s


@pytest.fixture(scope="module")
def ingested(variants, module_settings):
    df, truth, paths = variants
    out = {}
    for k, p in paths.items():
        ws, res = _ingest(module_settings, p, f"ing_{k}")
        out[k] = (ws, res)
    yield out
    for ws, _ in out.values():
        ws.close()


# ------------------------------------------------------------------------------------------------
# readers
# ------------------------------------------------------------------------------------------------
def test_every_format_ingests_to_the_same_shape(variants, ingested):
    df, truth, paths = variants
    n_rows = {k: r["n_rows"] for k, (_, r) in ingested.items()}
    assert set(n_rows.values()) == {len(df)}, n_rows
    n_cols = {k: r["n_cols"] for k, (_, r) in ingested.items()}
    numeric_cols = df.select_dtypes(include=[np.number]).shape[1]
    for k, n in n_cols.items():
        assert n == (numeric_cols if k.startswith("dat") else df.shape[1]), (k, n)
    for k, (ws, _) in ingested.items():
        con = ws.duckdb()
        bad = con.execute("SELECT count(*) FROM (SELECT __row__, row_number() OVER () - 1 AS rn FROM dataset) WHERE __row__ <> rn").fetchone()[0]
        assert bad == 0, f"{k}: parquet not in file order"
        assert "__group__" in [r[0] for r in con.execute("DESCRIBE dataset").fetchall()]


def test_headerless_and_transposed_detection(variants):
    _, _, paths = variants
    f1 = detect_format(paths["dat_noheader"])
    assert f1["format"] == "whitespace" and f1["has_header"] is False and f1["transposed"] is False
    f2 = detect_format(paths["dat_transposed"])
    assert f2["has_header"] is False and f2["transposed"] is True
    assert f2["orientation_stats"]["autocorr_along_rows"] > f2["orientation_stats"]["autocorr_down_columns"]
    f3 = detect_format(paths["csv"])
    assert f3["format"] == "csv" and f3["delimiter"] == "," and f3["has_header"] is True
    f4 = detect_format(paths["tsv"])
    assert f4["delimiter"] == "\t" and f4["has_header"] is True


def test_transposed_matrix_matches_original(variants, ingested):
    df, _, _ = variants
    ws, _ = ingested["dat_transposed"]
    num = df.select_dtypes(include=[np.number])
    got = ws.duckdb().execute("SELECT * FROM dataset ORDER BY __row__ LIMIT 50").df()
    np.testing.assert_allclose(got["col_2"].to_numpy()[:50], num.iloc[:50, 2].to_numpy(), rtol=1e-4)


def test_decimal_comma_semicolon_and_nan_tokens(tmp_path, module_settings):
    p = tmp_path / "euro.csv"
    lines = ["a;b;c;d", *[f"{i},5;{i * 2},25;NA;x{i % 3}" if i % 7 else f"{i},5;-;{i};x{i % 3}" for i in range(400)]]
    p.write_text("\n".join(lines), encoding="latin-1")
    fmt = detect_format(p)
    assert fmt["delimiter"] == ";" and fmt["decimal"] == "," and fmt["has_header"] is True
    ws, res = _ingest(module_settings, p, "ing_euro")
    try:
        types = {r[0]: r[1] for r in ws.duckdb().execute("DESCRIBE dataset").fetchall()}
        assert types["a"] in ("FLOAT", "DOUBLE") and types["b"] in ("FLOAT", "DOUBLE")
        assert res["n_rows"] == 400
        n_null = ws.duckdb().execute("SELECT count(*) - count(b) FROM dataset").fetchone()[0]
        assert n_null > 0  # '-' tokens became NULL
    finally:
        ws.close()


def test_ingest_dataframe_path(module_settings):
    df, truth = make_synthetic(n_groups=4, n_samples=100, seed=3, dq_issues=False)
    ws = Workspace(run_id="ing_df", settings=module_settings)
    try:
        res = ingest_dataframe(ws, module_settings, df)
        assert res["n_rows"] == len(df) and res["n_groups"] == 4
    finally:
        ws.close()


# ------------------------------------------------------------------------------------------------
# schema inference
# ------------------------------------------------------------------------------------------------
def test_grouping_finds_the_runs(variants, ingested):
    _, truth, _ = variants
    for k, (ws, res) in ingested.items():
        schema = ws.schema()
        assert schema.n_groups == truth["n_groups"], (k, schema.grouping_method, schema.n_groups)
        assert schema.grouping_method == "key_columns", k
        assert schema.order_column is not None, k  # the sample counter
        methods = {c.method for c in schema.grouping_candidates}
        assert {"key_columns", "counter_reset", "none"} <= methods
        assert all(0.0 <= c.score <= 1.0 and c.rationale for c in schema.grouping_candidates)
        assert (ws.dir / "groups.json").exists()


def test_label_column_is_excluded_from_signals(variants, ingested):
    _, truth, _ = variants
    ws, _ = ingested["csv"]
    schema = ws.schema()
    assert "fault_label" in schema.label_columns
    assert "fault_label" not in schema.signal_columns and "fault_label" not in schema.signal_alias
    assert set(schema.signal_columns) == set(truth["signal_columns"])
    assert "run" in schema.meta_columns and "sample" in schema.meta_columns and "timestamp" in schema.meta_columns


def test_time_column_and_period(variants, ingested):
    ws, res = ingested["csv"]
    schema = ws.schema()
    assert schema.time_column == "timestamp"
    assert schema.sample_period_seconds == pytest.approx(180.0, rel=0.05)
    ws2, _ = ingested["jsonl"]  # timestamp arrives as an ISO string in JSONL and must be cast
    assert ws2.schema().time_column == "timestamp"
    types = {r[0]: r[1] for r in ws2.duckdb().execute("DESCRIBE dataset").fetchall()}
    assert types["timestamp"].startswith("TIMESTAMP")


def test_no_timestamp_is_a_recorded_assumption(variants, ingested):
    ws, _ = ingested["dat_noheader"]
    schema = ws.schema()
    assert schema.time_column is None and schema.sample_period_seconds is None
    assert any("sample units" in a for a in schema.assumptions)
    assumed = [i for i in ws.inferences.all() if i.status == "assumed" and "sample units" in i.claim]
    assert assumed and assumed[0].evidence_ids


def test_blind_aliases_and_evidence_first(variants, ingested):
    ws, _ = ingested["csv"]
    schema = ws.schema()
    aliases = list(schema.signal_alias.values())
    assert aliases == [f"S{i:02d}" for i in range(1, len(aliases) + 1)]
    kinds = {e.kind for e in ws.evidence.all()}
    assert {"format", "sampling", "typing", "time", "counter", "grouping", "label_detection", "domain"} <= kinds
    hints = [e for e in ws.evidence.all() if e.kind == "name_hint"]
    assert hints and all(e.values.get("weight", 0.1) <= 0.1 for e in hints if "weight" in e.values)
    for inf in ws.inferences.all():
        assert 0.0 <= inf.confidence <= 1.0 and inf.status in ("inferred", "assumed", "uncertain")
        assert all(ws.evidence.get(e) is not None for e in inf.evidence_ids)
    assert ws.log.count() > 0 and ws.log.verify_chain()["ok"]
    assert schema.domain_likelihood["sensor_stream"] > 0.5


def test_operator_options_override_inference(variants, module_settings):
    _, _, paths = variants
    ws, res = _ingest(module_settings, paths["csv"], "ing_opts", options={"group_columns": ["fault_label"], "domain_hint": "sensor"})
    try:
        schema = ws.schema()
        assert schema.group_columns == ["fault_label"] and schema.n_groups >= 2
        assert any("domain hint" in a for a in schema.assumptions)
    finally:
        ws.close()


def test_schema_override_regroups(variants, module_settings):
    _, truth, paths = variants
    ws, _ = _ingest(module_settings, paths["csv"], "ing_override")
    try:
        d = HumanDecision(actor_name="ann", role="engineer", action="override", object_type="schema", object_id="schema", new_value={"group_columns": []})
        eff = apply_override(ws, module_settings, d)
        assert eff["changed"]["n_groups"] == 1
        assert ws.duckdb().execute("SELECT count(DISTINCT __group__) FROM dataset").fetchone()[0] == 1
        d2 = HumanDecision(actor_name="ann", role="engineer", action="override", object_type="schema", object_id="schema", new_value={"group_columns": ["run"], "move": {"column": "fault_label", "to": "meta"}})
        eff2 = apply_override(ws, module_settings, d2)
        assert eff2["changed"]["n_groups"] == truth["n_groups"]
        schema = ws.schema()
        assert "fault_label" in schema.meta_columns and "fault_label" not in schema.label_columns
        assert ws.log.entries(action="schema_override_applied")
    finally:
        ws.close()


# ------------------------------------------------------------------------------------------------
# batches and streaming
# ------------------------------------------------------------------------------------------------
def test_batches_cover_all_rows_without_overlap(variants, ingested, module_settings):
    for k in ("csv", "dat_noheader"):
        ws, res = ingested[k]
        batches = build_batches(ws, module_settings, force=True)
        assert batches and batches[0]["row_start"] == 0
        for a, b in zip(batches, batches[1:]):
            assert a["row_end"] == b["row_start"]
        assert batches[-1]["row_end"] == res["n_rows"]
        assert all(b["n_rows"] == b["row_end"] - b["row_start"] for b in batches)
        if k == "csv":
            assert batches[0]["method"] == "time_window" and batches[0]["time_start"]
        assert list(iter_batches(ws, module_settings))[0][0] == "B00001"


def test_read_rows_and_replay(variants, ingested, module_settings):
    ws, res = ingested["csv"]
    df = read_rows(ws, 10, 20, columns=["run", "sample"])
    assert list(df["__row__"]) == list(range(10, 20)) and set(df.columns) == {"__row__", "run", "sample"}
    seen = []
    out = replay(ws, module_settings, callback=lambda d, bid, meta: seen.append((bid, len(d))), max_batches=3)
    assert len(out) == 3 and sum(n for _, n in seen) == sum(b["n_rows"] for b in build_batches(ws, module_settings)[:3])


def test_align_incoming_and_watch_folder(variants, ingested, module_settings, tmp_path):
    df, _, _ = variants
    ws, res = ingested["csv"]
    schema = ws.schema()
    incoming = df.head(30).drop(columns=["temp_s"]).rename(columns={"flow_a": "FLOW_A"})
    incoming["extra_col"] = 1
    aligned = align_incoming(ws, module_settings, incoming)
    assert list(aligned.columns) == schema.columns + ["__row__", "__group__"]
    assert aligned["temp_s"].isna().all() and aligned["flow_a"].notna().all()
    assert int(aligned["__row__"].iloc[0]) == schema.n_rows
    assert any(e.kind == "alignment" for e in ws.evidence.all())
    folder = tmp_path / "watch"
    folder.mkdir()
    df.iloc[30:60].to_csv(folder / "batch1.csv", index=False)
    got = []
    processed = watch_folder(ws, module_settings, folder, callback=lambda d, bid, meta: got.append((bid, len(d))), poll_s=0.05, max_iterations=3)
    assert len(processed) == 1 and got[0][1] == 30 and got[0][0].startswith("SB")
    # a second pass must not re-ingest the same file
    processed2 = watch_folder(ws, module_settings, folder, callback=lambda d, bid, meta: got.append((bid, len(d))), poll_s=0.05, max_iterations=2)
    assert processed2 == []


# ------------------------------------------------------------------------------------------------
# smoke test on a head sample of the big file (never the full file)
# ------------------------------------------------------------------------------------------------
@pytest.mark.skipif(not TE_HEAD.exists(), reason="workspace/samples/te_head.csv not present (create with: head -n 30001 te_process.csv > workspace/samples/te_head.csv)")
def test_te_head_smoke(module_settings):
    ws = Workspace(run_id="ing_te_head", settings=module_settings)
    try:
        t0 = time.time()
        res = run_ingest(ws, module_settings, {"source_path": str(TE_HEAD), "options": {}, "progress": lambda f, m: None})
        dt = time.time() - t0
        assert res["n_rows"] == 30000 and res["n_groups"] >= 2 and res["n_signals"] >= 10
        assert res["grouping_method"] in ("key_columns", "counter_reset")
        print(f"\nte_head ingest: {dt:.2f}s {res['message']}")
        assert dt < 60
    finally:
        ws.close()
