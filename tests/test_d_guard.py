"""Egress guard tests (agent D). Everything here runs without Ollama or an API key.

Guard v2 (docs/HYBRID_SPEC.md section 2) fails closed per FIELD: what looks like raw data is dropped and the rest of the
payload still leaves; a payload is blocked only when nothing useful is left, it is too large, or the invariant fails."""
from __future__ import annotations

import json

import pytest

from tpm.llm import guard
from tpm.llm.sandbox import catalog_payload, diagnosis_payload, make_demo_workspace


@pytest.fixture(scope="module")
def demo_ws(tmp_path_factory):
    from tpm.config import load_settings

    s = load_settings(profile="no-egress")
    return make_demo_workspace(s, root=tmp_path_factory.mktemp("ws"), run_id="guard_demo", n_groups=4, n_samples=120)


@pytest.fixture
def settings():
    from tpm.config import load_settings

    return load_settings(profile="hybrid")


def _evidence(n_samples=500):
    return [{"id": "EV-000001", "kind": "correlation", "signals": ["S01", "S02"], "statement": "S01 and S02 correlate r=0.92 at lag 0", "n_samples": n_samples}]


def _rows():
    return [{"flow_a": 100.1 + i, "press_r": 2700.0 + i, "temp_r": 120.0, "level_s": 50.0, "flow_b": 40.0, "valve_1": 55.0} for i in range(10)]


def test_raw_rows_never_leave(settings):
    # next to a good artifact the row block is dropped on its own ...
    g = guard.check({"signals": _rows(), "evidence": _evidence()}, settings, strict=False)
    assert g.allowed and "signals" not in g.sanitized_payload and g.sanitized_payload["evidence"]
    assert any("row-like" in n for n in g.notes) and g.sanitizer["fields_dropped"] == 1
    # ... and a payload made of rows only has nothing left to send
    g2 = guard.check({"signals": _rows()}, settings, strict=True)
    assert not g2.allowed and "row-like" in g2.reason and g2.sanitized_payload == {}


def test_long_series_never_leaves(settings):
    ev = {"id": "EV-000002", "kind": "distribution", "statement": "profile", "n_samples": 500, "values": {"curve": [float(i) for i in range(500)], "mean": 1.5}}
    g = guard.check({"evidence": [ev]}, settings, strict=False)
    assert g.allowed and "curve" not in g.sanitized_payload["evidence"][0]["values"]
    assert g.sanitized_payload["evidence"][0]["values"]["mean"] == 1.5
    assert any("series of 500 points" in n for n in g.notes)
    g2 = guard.check({"evidence": [float(i) for i in range(500)]}, settings, strict=False)
    assert not g2.allowed and "series" in g2.reason and "500" in g2.reason


def test_small_aggregates_are_dropped_in_every_mode(settings):
    payload = {"evidence": _evidence(500) + [{"id": "EV-000003", "kind": "distribution", "signals": ["S03"], "statement": "S03 mean 1.2", "n_samples": 5}]}
    for strict in (True, False):  # strict mode used to block the whole payload: a false block in the 2026-09-19 dry run
        g = guard.check(payload, settings, strict=strict)
        assert g.allowed, g.reason
        assert [e["id"] for e in g.sanitized_payload["evidence"]] == ["EV-000001"]
        assert g.stats["items_dropped"] == 1 and any("n_samples=5" in n for n in g.notes)
    g2 = guard.check({"evidence": payload["evidence"][1:]}, settings, strict=True)
    assert not g2.allowed and "n_samples=5" in g2.reason


def test_free_text_record_values_never_leave(settings):
    payload = {"evidence": _evidence(), "checks": [{"check_id": "CHK-000001", "check_type": "categorical", "category": "validity", "status": "warn", "statement": "most common value", "values": {"most_common": "ACME Corporation Ltd, contact John Smith", "share": 0.5}}]}
    g = guard.check(payload, settings, strict=False)
    assert g.allowed
    assert "ACME" not in json.dumps(g.sanitized_payload) and g.sanitized_payload["checks"][0]["values"] == {"share": 0.5}
    assert any("free-text" in n for n in g.notes)
    # a categorical record under an unknown top-level key is blocked in strict mode too
    g2 = guard.check({"records": [{"customer": "Jane Doe", "comment": "called about invoice"}]}, settings, strict=True)
    assert not g2.allowed
    # free text as a dict key (value counts keyed by record values)
    g3 = guard.check({"evidence": _evidence(), "checks": [{"check_id": "CHK-000002", "statement": "counts", "values": {"counts": {"Jane Doe": 3, "John Smith": 4}}}]}, settings, strict=False)
    assert g3.allowed and "Jane" not in json.dumps(g3.sanitized_payload)


def test_blocks_too_many_numbers_and_too_large(settings):
    s = settings.model_copy(deep=True)
    s.guard.max_numeric_values_per_payload = 50
    ev = [{"id": f"EV-{i:06d}", "kind": "distribution", "statement": "x", "n_samples": 100, "values": {"mean": 1.0, "std": 2.0, "q05": 0.0, "q95": 3.0}} for i in range(20)]
    g = guard.check({"evidence": ev}, s, strict=False)
    assert not g.allowed and "numeric values" in g.reason
    s2 = settings.model_copy(deep=True)
    s2.guard.max_payload_bytes = 500
    g2 = guard.check({"evidence": _evidence() * 10}, s2, strict=False)
    assert not g2.allowed and "bytes" in g2.reason


def test_allows_catalog_relations_evidence(demo_ws, settings):
    payload = catalog_payload(demo_ws)
    g = guard.check(payload, settings, strict=False, ws=demo_ws)
    assert g.allowed, g.reason
    assert set(g.artifact_types) >= {"signal_catalog", "relations", "evidence"}
    assert g.stats["payload_bytes"] > 0
    g2 = guard.check(diagnosis_payload(demo_ws), settings, strict=True, ws=demo_ws)
    assert g2.allowed, g2.reason


def test_names_are_aliased_in_every_external_profile(demo_ws, settings):
    payload = catalog_payload(demo_ws)
    payload["evidence"].append({"id": "EV-999999", "kind": "name_hint", "signals": ["S01"], "statement": "column named flow_a", "n_samples": 100})
    payload["rule_text"] = "flow_a must stay below 130 and press_r above 2600"
    payload["flags"] = [{"id": "FLAG-000009", "kind": "anomaly", "row_start": 0, "row_end": 10, "severity": 0.5, "score": 1.0, "detector": "x", "statement": "flow_a jumped, PRESS_R followed", "human_note": "Ask Matti about pump 3"}]
    originals = list(demo_ws.schema().signal_alias.keys())
    g = guard.check(payload, settings, strict=True)
    assert g.allowed, g.reason
    text = str(g.sanitized_payload)
    for name in originals:
        assert name not in text, name
    assert "S01 must stay below 130" in g.sanitized_payload["rule_text"]
    assert "source_column" not in g.sanitized_payload["signals"][0]
    assert "signal_alias" not in g.sanitized_payload["schema_summary"]
    assert all(e["kind"] != "name_hint" for e in g.sanitized_payload["evidence"])
    assert "human_note" not in g.sanitized_payload["flags"][0]
    assert "Matti" not in text
    assert g.alias_map["flow_a"] == "S01" and g.sanitizer["names_aliased"] >= 3
    # hybrid (non-strict) aliases too: original names never leave, in any external profile; human notes stay
    g2 = guard.check(payload, settings, strict=False)
    assert g2.allowed and not any(name in str(g2.sanitized_payload) for name in originals)
    assert "press_r" not in str(g2.sanitized_payload).lower()  # other spellings of a name are caught as well
    assert g2.sanitized_payload["flags"][0]["human_note"].startswith("Ask Matti")
    # the legacy switch: only with aliasing turned off in the settings may a non-strict payload keep names
    s = settings.model_copy(deep=True)
    s.guard.alias_names_external = False
    assert "flow_a" in str(guard.check(payload, s, strict=False).sanitized_payload)


def test_names_come_from_the_run_even_when_the_payload_has_no_alias_map(demo_ws, settings):
    g = guard.check({"question": "why did press_r rise before temp_r in run 3?", "evidence": _evidence()}, settings, strict=False, ws=demo_ws)
    assert g.allowed and g.sanitized_payload["question"] == "why did S02 rise before S03 in run 3?"


def test_unknown_top_level_key_is_dropped_never_sent(settings):
    payload = {"evidence": _evidence(), "raw_sample": {"x": 1}}
    for strict in (True, False):
        g = guard.check(payload, settings, strict=strict)
        assert g.allowed and "raw_sample" not in g.sanitized_payload
        assert any("raw_sample" in n for n in g.notes)
    assert not guard.check({"raw_sample": {"x": 1}}, settings, strict=True).allowed


def test_nested_contract_objects_and_numpy_are_walked(settings):
    import numpy as np

    from tpm.contracts import Evidence

    # a pydantic object hiding a 500-point series inside `values`, plus numpy scalars: must still be caught
    ev = Evidence(id="EV-000010", kind="distribution", statement="hidden series", n_samples=500, values={"trace": np.arange(500).tolist()})
    g = guard.check({"evidence": [ev]}, settings, strict=False)
    assert g.allowed and "trace" not in g.sanitized_payload["evidence"][0]["values"] and any("series" in n for n in g.notes)
    ok = guard.check({"evidence": [Evidence(id="EV-000011", kind="distribution", statement="fine", n_samples=np.int64(500), values={"mean": np.float64(1.5), "nan": float("nan")})]}, settings, strict=True)
    assert ok.allowed and ok.sanitized_payload["evidence"][0]["values"]["nan"] is None


def test_floats_are_rounded_also_inside_strings(settings):
    ev = {"id": "EV-000020", "kind": "distribution", "signals": ["S01"], "n_samples": 800, "statement": "S01 has mean 2715.4837 and std 0.0123456 (r=-0.98765, 52.37% of rows, 1.2345e-05 drift) see EV-000123, S05, B12, DIAG-3, v1.2.3, rows 1200-1350",
          "values": {"mean": 2715.4837, "std": 0.0123456, "share": 0.5, "count": 1200, "row": 1234567.0, "big": 99999.0, "small": 1.23456e-9, "ok": True, "quantiles": [1.23456, 2.0, 3.98765]}}
    g = guard.check({"evidence": [ev]}, settings, strict=False)
    assert g.allowed, g.reason
    out = g.sanitized_payload["evidence"][0]
    assert out["values"] == {"mean": 2720.0, "std": 0.0123, "share": 0.5, "count": 1200, "row": 1234567, "big": 100000.0, "small": 1.23e-9, "ok": True, "quantiles": [1.23, 2.0, 3.99]}
    assert out["statement"] == "S01 has mean 2720 and std 0.0123 (r=-0.988, 52.4% of rows, 0.0000123 drift) see EV-000123, S05, B12, DIAG-3, v1.2.3, rows 1200-1350"
    assert g.sanitizer["floats_rounded"] == 6 and g.sanitizer["numbers_in_strings_rounded"] == 5
    s = settings.model_copy(deep=True)
    s.guard.external_sig_digits = 2
    assert guard.check({"evidence": [ev]}, s, strict=False).sanitized_payload["evidence"][0]["values"]["mean"] == 2700.0


def test_iso_times_are_redacted(settings):
    flag = {"id": "FLAG-000001", "kind": "anomaly", "row_start": 1200, "row_end": 1350, "created_at": "2026-09-19T10:15:00.123456+00:00",
            "statement": "Deviation from 2026-01-03 04:12:30 until 2026-01-03T05:00Z (rows 1200-1350), first seen 03.01.2026 04:12 at 04:12:30"}
    g = guard.check({"flags": [flag]}, settings, strict=False)
    assert g.allowed, g.reason
    out = g.sanitized_payload["flags"][0]
    assert out["created_at"] == "[time]"
    assert out["statement"] == "Deviation from [time] until [time] (rows 1200-1350), first seen [time] at [time]"
    assert out["row_start"] == 1200 and g.sanitizer["times_redacted"] == 5


def test_dropped_keys_at_any_depth_but_rule_limits_stay(settings):
    payload = {
        "signals": [{"id": "S01", "structural_role": "continuous_measured", "fingerprint": {"mean": 1.5, "min": 0.123, "max": 9.87, "first": 1.1, "last": 2.2, "q05": 0.2, "q95": 9.1}, "source_path": "C:/data/plant7.csv"}],
        "flags": [{"id": "FLAG-000001", "kind": "anomaly", "statement": "x", "row_start": 5, "row_end": 9, "start_time": "2026-01-01T00:00:00", "observed": 17.25, "readings": [1.0, 2.0], "evaluation": {"f1": 0.9}, "label": "fault_3"}],
        "rules": [{"id": "RULE-001", "text": "S01 must stay between 100.25 and 2715.5", "status": "active", "compiled": {"type": "range", "signal": "S01", "min": 100.25, "max": 2715.5}}],
        "rule_text": "S01 must stay below 2715.5",
        "evaluation": {"f1": 0.91},
    }
    g = guard.check(payload, settings, strict=True)
    assert g.allowed, g.reason
    assert g.sanitized_payload["signals"][0]["fingerprint"] == {"mean": 1.5, "q05": 0.2, "q95": 9.1}
    assert "source_path" not in g.sanitized_payload["signals"][0]
    assert set(g.sanitized_payload["flags"][0]) == {"id", "kind", "statement", "row_start", "row_end"}
    assert "evaluation" not in g.sanitized_payload
    # limits the operator wrote into a rule are not data: kept, and kept exactly
    assert g.sanitized_payload["rules"][0]["compiled"] == {"type": "range", "signal": "S01", "min": 100.25, "max": 2715.5}
    assert "2715.5" in g.sanitized_payload["rules"][0]["text"] and g.sanitized_payload["rule_text"].endswith("2715.5")
    assert g.sanitizer["keys_dropped"] == 11 and any("min x1" in n and "evaluation x2" in n for n in g.notes)


def test_vocabulary_of_the_dataset_is_redacted(tmp_path, settings):
    import pandas as pd

    from tpm.workspace import Workspace

    ws = Workspace(run_id="vocab", settings=settings, root=tmp_path)
    df = pd.DataFrame({"press_r": [1.5, 2.5, 3.5, 4.5], "Line_Name": ["LineNorth7", "LineNorth7", "Kettle B-12", "ok"], "fault_kind": ["ZetaTrip", "normal", "QuorumLeak", "normal"], "batch_no": ["17", "18", "19", "20"]})
    df.to_parquet(ws.path("dataset"), index=False)
    ws.write_json("schema", {"dataset_id": "vocab", "source_path": "C:/plant/secret_mill_2026.csv", "format": "csv", "n_rows": 4, "n_cols": 4, "had_header": True, "columns": list(df.columns), "label_columns": ["fault_kind"], "meta_columns": ["Line_Name", "batch_no"], "signal_columns": ["press_r"], "signal_alias": {"press_r": "S01"}})
    vocab = guard.data_vocabulary(ws)
    assert set(vocab) == {"LineNorth7", "Kettle B-12", "ZetaTrip", "QuorumLeak"}  # short, numeric and structural words ("normal") left out
    assert (ws.dir / guard.VOCAB_FILE).exists() and guard.vocabulary_record(ws)["complete"]
    payload = {"flags": [{"id": "FLAG-000001", "kind": "anomaly", "group_id": "LineNorth7", "statement": "Group linenorth7 (Kettle B-12) shows a ZetaTrip_2 pattern on press_r; fault_kind and Line_Name agree; normal elsewhere. Source secret_mill_2026.csv"}], "evidence": _evidence()}
    g = guard.check(payload, settings, strict=False, ws=ws)
    assert g.allowed, g.reason
    out = g.sanitized_payload["flags"][0]
    assert out["group_id"] == "[value]"
    assert out["statement"] == "Group [value] ([value]) shows a [value]_2 pattern on S01; [column] and [column] agree; normal elsewhere. Source [file]"
    assert g.sanitizer["values_redacted"] == 4 and g.sanitizer["files_redacted"] == 1 and g.sanitizer["vocabulary_size"] == 4
    # without ws the guard cannot know the vocabulary: that step is skipped, names are not
    g2 = guard.check(payload, settings, strict=False)
    assert "ZetaTrip" in g2.sanitized_payload["flags"][0]["statement"]


def test_verify_invariant_catches_what_a_sanitiser_bug_would_let_through(settings):
    cfg = settings.guard
    amap = {"press_r": "S02", "fault_kind": "[column]"}
    vocab = ["ZetaTrip"]
    clean = {"flags": [{"id": "FLAG-000001", "statement": "S02 rose to 2720 at row 1200", "severity": 0.75, "quantiles": [1.0, 2.5]}], "rules": [{"compiled": {"type": "range", "signal": "S02", "max": 2715.5}}], "rule_text": "S02 below 2715.5"}
    assert guard.verify_invariant(clean, amap, vocab, cfg) == []
    dirty = {
        "flags": [{"id": "FLAG-000001", "statement": "press_r rose to 2715.4837 on 2026-01-03T04:12:30", "severity": 0.123456, "max": 17.0, "group_id": "ZetaTrip", "curve": [float(i) for i in range(40)]}],
    }
    v = "\n".join(guard.verify_invariant(dirty, amap, vocab, cfg))
    for expected in ("float with more than 3 significant digits", "inside a string", "date/time token", "original column name", "data vocabulary value", "list of 40 numbers", "dropped key 'max'"):
        assert expected in v, expected
    assert "2715" not in v and "ZetaTrip" not in v and "press_r" not in v  # violation messages never repeat the value
    # and check() blocks on it: simulate a sanitiser that lets a long float through
    import unittest.mock as mock

    with mock.patch.object(guard._Sanitizer, "_number", lambda self, x, key, in_rule: x):
        g = guard.check({"evidence": [{"id": "EV-000001", "statement": "x", "n_samples": 100, "values": {"mean": 2715.4837}}]}, settings, strict=False)
    assert not g.allowed and "invariant" in g.reason and g.sanitized_payload == {}


def test_dry_run_false_blocks_are_gone(settings):
    # every key below blocked a whole payload before guard v2 (dry run 2026-09-19)
    payload = {
        "signals": [{"signal": "S01", "structural_role": "continuous_measured", "heuristic_instrument": "flow-like (fast, noisy)", "fingerprint": {"mean": 1.0}}],
        "schema": {"$schema": "closed-rule-spec-v1", "description": "Exactly one object.", "oneOf": [{"type": "range", "required": ["signal"]}]},
        "template_parser_error": "no rule pattern matched: 'when S01 is unusually high the S02 should come down'",
        "candidates": {"reactor pressure": ["S02", "S07"]},
        "checks": [{"name": "single_event", "passed": False, "detail": "Only one event supports this diagnosis; 2 of 3 peers did not move."}],
        "instruction": "Act as a devil's advocate. Every objection must cite at least one evidence id.",
        "instructions": "Write for a plant operator, in English.",
        "evidence": _evidence() + [{"id": "EV-000009", "kind": "lag", "statement": "short window", "n_samples": 12}],
    }
    for strict in (True, False):
        g = guard.check(payload, settings, strict=strict)
        assert g.allowed, g.reason
        sp = g.sanitized_payload
        assert sp["signals"][0]["heuristic_instrument"].startswith("flow-like") and "$schema" not in sp["schema"]
        assert sp["template_parser_error"].startswith("no rule pattern") and sp["candidates"] == {"reactor pressure": ["S02", "S07"]}
        assert sp["checks"][0]["detail"].startswith("Only one event") and sp["instruction"] and sp["instructions"]
        assert [e["id"] for e in sp["evidence"]] == ["EV-000001"]
    # instructions alone are not worth a call
    assert not guard.check({"instruction": "Summarise.", "language": "en"}, settings, strict=False).allowed


def test_sanitize_text_and_verify_texts(demo_ws, settings):
    text = "Tool result for stats: press_r mean 2715.4837 between 2026-01-03T04:12:30 and row 1350 (EV-000012)"
    clean, counts = guard.sanitize_text(text, settings, ws=demo_ws)
    assert clean == "Tool result for stats: S02 mean 2720 between [time] and row 1350 (EV-000012)"
    assert counts["names_aliased"] == 1 and counts["times_redacted"] == 1 and counts["numbers_in_strings_rounded"] == 1
    assert guard.verify_texts([clean], settings, ws=demo_ws) == []
    assert len(guard.verify_texts([text], settings, ws=demo_ws)) == 3


def test_explain_mentions_thresholds(settings):
    txt = guard.explain(settings)
    assert "20 points" in txt and f"{settings.guard.max_numeric_values_per_payload} numbers" in txt and "30 samples" in txt
    assert "3 significant digits" in txt and "[time]" in txt and "[value]" in txt and "aliases" in txt
    from tpm.config import load_settings

    txt2 = guard.explain(load_settings(profile="no-egress"))
    assert "nothing leaves" in txt2.lower()
    txt3 = guard.explain(load_settings(profile="eu-hosted"))
    assert "NOT used" in txt3 and "base_url" in txt3


def test_narrow_table_rows_never_leave(demo_ws, settings):
    """Round 6: a table with only a few numeric columns next to many text columns (business records, logs). Its rows are
    rows however few numbers they carry: three records under the data's own column names are dropped, and a row written
    as text with a row number / time stamp / file name is dropped with three value pairs already."""
    recs = [{"order": f"A-{i}", "region": "north", "category": "x", "status": "open", "note": "ok", "flow_a": 100.1 + i, "press_r": 2700.0 + i, "temp_r": 120.0} for i in range(3)]
    g = guard.check({"batch": recs, "evidence": _evidence()}, settings, strict=False, ws=demo_ws)
    assert g.allowed and "batch" not in g.sanitized_payload and any("row-like structure" in n for n in g.notes)
    row_text = {"id": "EV-000002", "kind": "note", "signals": ["S01"], "statement": "row 12: flow_a=100.1, press_r=2700.5, temp_r=120.2", "n_samples": 500}
    stamped = {"id": "EV-000003", "kind": "note", "signals": ["S01"], "statement": "flow_a=100.1, press_r=2700.5, temp_r=120.2 logged at 2026-03-01T08:02:17", "n_samples": 500}
    shares = {"id": "EV-000004", "kind": "attribution", "signals": ["S01"], "statement": "share of the deviation: flow_a=0.31, press_r=0.22, temp_r=0.12", "n_samples": 500}
    g2 = guard.check({"evidence": [row_text, stamped, shares]}, settings, strict=False, ws=demo_ws)
    kept = [e.get("id") for e in g2.sanitized_payload.get("evidence", []) if e.get("statement")]
    assert kept == ["EV-000004"], g2.notes  # attribution shares without a row locator are aggregates and stay
    assert sum("a raw row written as text" in n for n in g2.notes) == 2
    # aggregates keyed by statistics (not by column names) are still allowed, whatever their number
    stats = [{"signal": f"S0{i}", "mean": 1.2 + i, "std": 0.3, "n": 500} for i in range(1, 4)]
    g3 = guard.check({"signals": stats, "evidence": _evidence()}, settings, strict=False, ws=demo_ws)
    assert g3.allowed and len(g3.sanitized_payload.get("signals") or []) == 3, g3.notes
