"""Point anomalies, the headline list of suspicious rows, and the narrative gates.

A dataset whose only abnormalities are isolated single readings must produce point findings and ONE aggregated
diagnosis, with no onset / pattern / propagation analysis; a propagation step needs a learned relation; a
"recurring pattern" needs events that share their lead signals."""
from __future__ import annotations

import collections
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fixtures.synth import make_synthetic  # noqa: E402
from tpm.config import load_settings  # noqa: E402
from tpm.contracts import Flag, SignalContribution  # noqa: E402
from tpm.workspace import Workspace  # noqa: E402


@pytest.fixture(scope="module")
def points_run(tmp_path_factory):
    from scipy.ndimage import median_filter

    from tpm.pipeline import run_pipeline

    tmp = tmp_path_factory.mktemp("points")
    s = load_settings()
    s.workspace_dir = str(tmp / "ws")
    s.detect.time_budget_s = 120
    s.detect.n_folds = 3
    df, _ = make_synthetic(n_groups=8, n_samples=300, seed=21, fault_fraction=0.0, dq_issues=False)
    rng = np.random.default_rng(1)
    injected: dict[int, str] = {}
    for c in ("temp_r", "press_r", "flow_a", "power_c", "temp_s"):
        x = df[c].to_numpy(float)
        sig = 1.4826 * np.median(np.abs(x - median_filter(x, size=7, mode="mirror")))
        for v in rng.choice(np.arange(60, 2340), size=3, replace=False):
            v = int(v)
            if 20 < v % 300 < 280:
                df.loc[v, c] += rng.choice([-1, 1]) * 25 * sig
                injected[v] = c
    p = tmp / "points.csv"
    df.to_csv(p, index=False)
    st = run_pipeline(str(p), run_id="points", settings=s, stages=["ingest", "profile", "quality", "detect", "diagnose"], options={"no_llm": True, "skip_llm": True})
    assert st.state == "done", [(x.stage, x.error) for x in st.stages if x.state == "failed"]
    return Workspace(run_id="points", settings=s), injected


def test_single_readings_become_point_findings_not_events(points_run):
    ws, injected = points_run
    kinds = collections.Counter(f.kind for f in ws.flags())
    assert kinds["point"] >= 5, kinds
    # one bad reading keeps rolling features high for a window: that echo must not be reported as an event
    assert not any(kinds[k] for k in ("anomaly", "drift", "changepoint", "cascade")), kinds
    meta = ws.read_json("detect_meta")["events"]["points"]
    assert meta["point_dominated"] and meta["n_sustained_stretches"] == 0
    pts = [f for f in ws.flags() if f.kind == "point"]
    assert all(f.row_end - f.row_start + 1 <= 2 for f in pts)
    assert all(f.signals_ranked and f.evidence_ids and f.likely_cause_class == "unknown" for f in pts)
    assert all("glitch" in f.statement and "manipulation" in f.statement for f in pts)
    assert not any(abs(f.row_start - r) > 1 for f in pts for r in [min(injected, key=lambda q: abs(q - f.row_start))]), "a point finding away from every injected reading"


def test_one_headline_list_combines_checks_and_detectors(points_run):
    ws, injected = points_run
    su = ws.read_json("suspicious_rows.json")
    assert su["n_rows"] >= len(injected) - 1
    listed = [r["row"] for r in su["rows"]]
    assert sum(1 for r in injected if any(abs(x - r) <= 1 for x in listed)) >= len(injected) - 1
    assert "glitch" in su["wording"] and "manipulation" in su["wording"] and "cannot tell" in su["wording"]
    assert su["regime"]["point_dominated"]
    sources = collections.Counter(s for r in su["rows"] for s in r["sources"])
    assert sources["local_spike"] >= 5 and sources["detector"] >= 5, sources
    assert any(len(r["sources"]) > 1 for r in su["rows"]), "a reading caught by several checks must be listed once"
    assert len(listed) == len(set(listed))
    for r in su["rows"]:
        assert r["signals"] and r["statement"] and ("glitch" in r["statement"].lower())
        assert all(set(sg) >= {"signal", "deviation", "direction", "explanation"} for sg in r["signals"])


def test_points_get_one_diagnosis_without_onset_pattern_or_propagation(points_run):
    ws, _ = points_run
    dg = ws.diagnoses()
    assert [d.fault_type for d in dg] == ["isolated suspicious readings"]
    d = dg[0]
    assert d.cause_class == "unknown" and d.propagation == [] and d.pattern_id is None
    text = " ".join(d.steps) + d.summary
    assert "glitch" in text and "manipulation" in text
    assert "no onset" in text.lower()
    assert ws.patterns() == []
    assert ws.exists("propagation"), "detect writes the propagation artifact even when it is empty"
    assert not (ws.read_json("propagation", {}) or {})


def _flag(fid: str, group: str, sigs: list[tuple[str, float, int]]) -> Flag:
    return Flag(id=fid, kind="anomaly", group_id=group, row_start=10, row_end=60, severity=0.5, score=3.0, detector="t", statement="s", signals_ranked=[SignalContribution(signal=a, contribution=c, direction="up", lag=lag) for a, c, lag in sigs], evidence_ids=["EV-000001"])


def test_propagation_step_needs_a_learned_relation():
    from tpm.detect.cascade import chain_for_flag

    inputs = SimpleNamespace(relations={"pairs": [{"a": "S01", "b": "S02", "r": 0.85, "lag": 2}], "corr": {}}, signals=[])
    f = _flag("FLAG-000001", "1", [("S01", 0.5, 0), ("S02", 0.3, 2), ("S03", 0.2, 6)])
    steps = chain_for_flag(inputs, f, window=20)
    assert [(s.from_signal, s.to_signal) for s in steps] == [("S01", "S02")], "S02->S03 has no learned relation and must not appear"
    # nothing learned at all -> no chain, so the narrative cannot mention propagation
    assert chain_for_flag(SimpleNamespace(relations={"pairs": [], "corr": {}}, signals=[]), f, window=20) == []
    # a learned lead/lag that contradicts the observed order is not a propagation claim either
    contra = SimpleNamespace(relations={"pairs": [{"a": "S01", "b": "S02", "r": 0.9, "lag": -15}], "corr": {}}, signals=[])
    assert chain_for_flag(contra, _flag("FLAG-000002", "1", [("S01", 0.6, 0), ("S02", 0.4, 2)]), window=20) == []


def test_recurring_pattern_needs_shared_lead_signals(tmp_path):
    from tpm.detect.patterns import build_patterns

    s = load_settings()
    s.workspace_dir = str(tmp_path / "ws")
    s.detect.pattern_min_events = 3
    aliases = [f"S{i:02d}" for i in range(1, 13)]
    # every event led by a different signal: clustering may still group them, but it is not a recurring pattern
    ws = Workspace(run_id="scattered", settings=s)
    scattered = [_flag(f"FLAG-{i:06d}", str(i), [(aliases[i], 0.7, 0), (aliases[(i + 5) % 12], 0.2, 1), (aliases[(i + 9) % 12], 0.1, 2)]) for i in range(8)]
    pats, assign, meta = build_patterns(ws, s, scattered, aliases)
    assert pats == [] and assign == {}, [p.description for p in pats]
    # events that share their lead signals ARE a recurring pattern
    ws2 = Workspace(run_id="shared", settings=s)
    shared = [_flag(f"FLAG-{i:06d}", str(i), [("S03", 0.6 + 0.01 * i, 0), ("S07", 0.3, 2), ("S09", 0.1, 4)]) for i in range(4)] + [_flag(f"FLAG-{i + 10:06d}", str(i + 10), [("S11", 0.7, 0), ("S02", 0.3, 3)]) for i in range(4)]
    pats2, assign2, _ = build_patterns(ws2, s, shared, aliases)
    assert len(pats2) >= 1 and len(assign2) >= 3
    ws.close()
    ws2.close()
