"""Egress guard tests (agent D). Everything here runs without Ollama or an API key."""
from __future__ import annotations

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


def test_blocks_raw_rows(settings):
    rows = [{"flow_a": 100.1 + i, "press_r": 2700.0 + i, "temp_r": 120.0, "level_s": 50.0, "flow_b": 40.0, "valve_1": 55.0} for i in range(10)]
    g = guard.check({"signals": rows, "evidence": _evidence()}, settings, strict=False)
    assert not g.allowed
    assert "row-like" in g.reason


def test_blocks_long_series(settings):
    g = guard.check({"evidence": [{"id": "EV-000002", "kind": "distribution", "statement": "series", "n_samples": 500, "values": {"series": [float(i) for i in range(500)]}}]}, settings, strict=False)
    assert not g.allowed
    assert "series" in g.reason and "500" in g.reason


def test_blocks_small_aggregate_in_strict_and_drops_in_non_strict(settings):
    payload = {"evidence": _evidence(500) + [{"id": "EV-000003", "kind": "distribution", "signals": ["S03"], "statement": "S03 mean 1.2", "n_samples": 5}]}
    g = guard.check(payload, settings, strict=True)
    assert not g.allowed and "n_samples=5" in g.reason
    g2 = guard.check(payload, settings, strict=False)
    assert g2.allowed
    assert len(g2.sanitized_payload["evidence"]) == 1
    assert g2.stats["items_dropped"] == 1 and any("dropped" in n for n in g2.notes)


def test_blocks_free_text_record_values(settings):
    payload = {"checks": [{"check_id": "CHK-000001", "check_type": "categorical", "category": "validity", "status": "warn", "statement": "most common value", "values": {"most_common": "ACME Corporation Ltd, contact John Smith"}}]}
    g = guard.check(payload, settings, strict=False)
    assert not g.allowed
    assert "free-text" in g.reason
    # a categorical record under an unknown top-level key is blocked in strict mode too
    g2 = guard.check({"records": [{"customer": "Jane Doe", "comment": "called about invoice"}]}, settings, strict=True)
    assert not g2.allowed


def test_blocks_too_many_numbers_and_too_large(settings):
    s = settings.model_copy(deep=True)
    s.guard.max_numeric_values_per_payload = 50
    ev = [{"id": f"EV-{i:06d}", "kind": "distribution", "statement": "x", "n_samples": 100, "values": {"mean": 1.0, "std": 2.0, "min": 0.0, "max": 3.0}} for i in range(20)]
    g = guard.check({"evidence": ev}, s, strict=False)
    assert not g.allowed and "numeric values" in g.reason
    s2 = settings.model_copy(deep=True)
    s2.guard.max_payload_bytes = 500
    g2 = guard.check({"evidence": _evidence() * 10}, s2, strict=False)
    assert not g2.allowed and "bytes" in g2.reason


def test_allows_catalog_relations_evidence(demo_ws, settings):
    payload = catalog_payload(demo_ws)
    g = guard.check(payload, settings, strict=False)
    assert g.allowed, g.reason
    assert set(g.artifact_types) >= {"signal_catalog", "relations", "evidence"}
    assert g.stats["payload_bytes"] > 0
    g2 = guard.check(diagnosis_payload(demo_ws), settings, strict=True)
    assert g2.allowed, g2.reason


def test_strict_mode_aliases_names(demo_ws, settings):
    payload = catalog_payload(demo_ws)
    payload["evidence"].append({"id": "EV-999999", "kind": "name_hint", "signals": ["S01"], "statement": "column named flow_a", "n_samples": 100})
    payload["rule_text"] = "flow_a must stay below 130 and press_r above 2600"
    payload["flags"] = [{"id": "FLAG-000009", "kind": "anomaly", "row_start": 0, "row_end": 10, "severity": 0.5, "score": 1.0, "detector": "x", "statement": "flow_a jumped", "human_note": "Ask Matti about pump 3"}]
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
    # non-strict keeps names (allow_column_names is true in config)
    g2 = guard.check(payload, settings, strict=False)
    assert g2.allowed and "flow_a" in str(g2.sanitized_payload)


def test_unknown_key_blocked_in_strict_dropped_otherwise(settings):
    payload = {"evidence": _evidence(), "raw_sample": {"x": 1}}
    assert not guard.check(payload, settings, strict=True).allowed
    g = guard.check(payload, settings, strict=False)
    assert g.allowed and "raw_sample" not in g.sanitized_payload


def test_nested_contract_objects_and_numpy_are_walked(settings):
    import numpy as np

    from tpm.contracts import Evidence

    # a pydantic object hiding a 500-point series inside `values`, plus numpy scalars: must still be caught
    ev = Evidence(id="EV-000010", kind="distribution", statement="hidden series", n_samples=500, values={"series": np.arange(500).tolist()})
    g = guard.check({"evidence": [ev]}, settings, strict=False)
    assert not g.allowed and "series" in g.reason
    ok = guard.check({"evidence": [Evidence(id="EV-000011", kind="distribution", statement="fine", n_samples=np.int64(500), values={"mean": np.float64(1.5), "nan": float("nan")})]}, settings, strict=True)
    assert ok.allowed and ok.sanitized_payload["evidence"][0]["values"]["nan"] is None


def test_explain_mentions_thresholds(settings):
    txt = guard.explain(settings)
    assert "20 points" in txt and f"{settings.guard.max_numeric_values_per_payload} numbers" in txt and "30 samples" in txt
    from tpm.config import load_settings

    txt2 = guard.explain(load_settings(profile="no-egress"))
    assert "nothing leaves" in txt2.lower()
