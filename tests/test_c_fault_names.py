"""Recurring patterns are named from the known-failure catalogue only when enough of a failure type's sensors lead
them; otherwise 'cannot name' (review item 9)."""
from tpm.diagnose.fault_names import name_patterns

CAT = [
    {"id": "F06", "name": "Fault 6: A feed loss - large, fast collapse", "columns": ["xmeas_1", "xmv_3", "xmeas_4"], "pattern": "collapse", "confidence": "documented"},
    {"id": "F07", "name": "Fault 7: C header pressure loss - mean shift", "columns": ["xmeas_4", "xmv_4"], "pattern": "mean_shift", "confidence": "documented"},
]
SIGNALS = [{"id": "S01", "source_column": "xmeas_1"}, {"id": "S04", "source_column": "xmeas_4"}, {"id": "S44", "source_column": "xmv_3"}, {"id": "S45", "source_column": "xmv_4"}, {"id": "S09", "source_column": "xmeas_9"}]


def test_named_when_the_failure_types_sensors_lead_the_pattern():
    pats = {"PATTERN-A": {"signature": {"ranked_signals": ["S44", "S01", "S09"], "directions": {"S44": "stuck", "S01": "stuck"}}}}
    h = name_patterns(pats, SIGNALS, CAT)["PATTERN-A"]
    assert h["named"] and h["id"] == "F06" and h["matched"] == 2 and "possibly Fault 6" in h["text"]


def test_cannot_name_when_it_does_not_fit_or_the_sensors_are_absent():
    pats = {"PATTERN-B": {"signature": {"ranked_signals": ["S45", "S09"], "directions": {}}}}
    h = name_patterns(pats, SIGNALS, CAT)["PATTERN-B"]
    assert not h["named"] and h["text"].startswith("cannot name") and "closest" in h["text"]
    other = [{"id": "S01", "source_column": "temperature"}, {"id": "S02", "source_column": "flow"}]
    h2 = name_patterns({"P": {"signature": {"ranked_signals": ["S01", "S02"]}}}, other, CAT)["P"]
    assert not h2["named"] and "does not have" in h2["text"]
