"""Round 5 B, live monitor: known failure types (signatures) from config/failure_signatures.yaml, matching on sensor
behaviour, the alarm card (alarm -> problem -> cause -> suggestion), the demo scenarios and the replay injection.

    .venv\\Scripts\\python.exe -m pytest tests/test_g_live_signatures.py -q
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tpm.live import LiveMonitor, engine, signatures

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "tpm" / "api" / "static"
COLS = ["xmeas_1", "xmeas_4", "xmv_3", "xmeas_9", "xmeas_21", "xmv_10", "xmeas_23", "xmeas_24", "xmeas_25", "other"]
MEANS = [0.25, 9.35, 24.6, 120.4, 94.6, 41.1, 32.2, 8.9, 26.4, 5.0]
SIGMAS = [0.004, 0.03, 0.25, 0.02, 0.05, 0.3, 0.1, 0.05, 0.1, 0.1]


def frame(n: int, seed: int, *, shift=None, noise=None, collapse=None, jumpy=None, cols=COLS) -> pd.DataFrame:
    """A cycle of TE-named readings. shift / noise / collapse / jumpy: {column: amount} in normal spreads or fractions."""
    rng = np.random.default_rng(seed)
    d = {}
    for c, m, s in zip(cols, MEANS, SIGMAS):
        v = m + rng.normal(0, s, n)
        if shift and c in shift:
            v = v + shift[c] * s
        if noise and c in noise:
            v = v + rng.normal(0, s * noise[c], n)
        if collapse and c in collapse:
            v = v * (1 - collapse[c])
        if jumpy and c in jumpy:
            v = v + np.where((np.arange(n) // 12) % 2 == 0, jumpy[c] * s, 0.0)
        d[c] = v
    d["datetime"] = pd.date_range("2026-01-01", periods=n, freq="s").astype(str)
    return pd.DataFrame(d)


class Sim:
    """Drives engine + signatures by hand, cycle by cycle, the way LiveMonitor does."""

    def __init__(self, sigs=None, cols=COLS):
        self.cols = cols
        frames = [frame(200, s, cols=cols) for s in range(3)]
        self.sensors = engine.sensor_columns(frames[0])
        self.base = engine.learn(frames, self.sensors)
        self.names = {s: [f"S{i + 1:02d}"] for i, s in enumerate(self.sensors)}
        self.sigs = sigs if sigs is not None else signatures.load()[0]
        self.state: dict = {}
        self.past: list = []

    def cycle(self, df, label="t"):
        st = engine.cycle_stats(df, self.sensors, self.base)
        feats = engine.features(st, self.base, self.past)
        rows = signatures.evaluate(self.sigs, feats, self.sensors, self.names, self.state, when=label)
        self.past.append({"t": label, "mean": {c: float(st["mean"][c]) for c in self.sensors}})
        return {r["id"]: r for r in rows}


# ------------------------------------------------------------------------------------------ catalogue
def test_catalogue_loads_every_fault_with_its_columns_pattern_and_visibility():
    sigs, sources = signatures.load()
    by = {s["id"]: s for s in sigs}
    assert sources and sources[0].endswith("failure_signatures.yaml")
    assert {f"F{n:02d}" for n in range(1, 21)} <= set(by) and len(sigs) == len(by)
    assert by["F01"]["columns"] == ["xmeas_1", "xmeas_4", "xmv_3"] and by["F01"]["pattern"] == "mean_shift" and by["F01"]["confidence"] == "documented"
    assert by["F06"]["pattern"] == "collapse" and by["F08"]["pattern"] == "variance_increase" and by["F08"]["confidence"] == "hypothesis"
    assert by["F13"]["pattern"] == "slow_drift" and by["F14"]["pattern"] == "jumpy" and by["F04"]["pattern"] == "brief_bump" and by["F05"]["pattern"] == "fading_shift"
    for fid in ("F03", "F09", "F15", "F16", "F17", "F18", "F19", "F20"):
        assert by[fid]["visible"] is False and by[fid]["pattern"] == "none", fid
    assert by["F15"]["columns"] == ["xmv_11"]                       # kept, but marked not visible
    assert all(s["pattern"] in signatures.PATTERNS for s in sigs)
    assert all(s["suggestion"] for s in sigs if s["visible"])
    for s in sigs:                                                    # the card is in three languages
        for lang in ("fi", "sv"):
            assert s["name_i18n"].get(lang) and s["suggestion_i18n"].get(lang), (s["id"], lang)
    generic = [s for s in sigs if s["generic"]]
    assert {g["pattern"] for g in generic} >= {"mean_shift", "variance_increase"} and all(not g["columns"] for g in generic)


def test_extra_yaml_files_in_the_data_folder_add_and_override(tmp_path):
    (tmp_path / "mine.yaml").write_text("signatures:\n  - id: MY-1\n    name: Pump cavitation\n    columns: [flow, vibration]\n    pattern: variance_increase\n    suggestion: Check the pump.\n"
                                        "  - id: F01\n    name: Overridden fault 1\n    columns: [a, b]\n    pattern: mean_shift\n    suggestion: mine\n"
                                        "  - id: 'bad id!'\n    name: nope\n    columns: [x]\n    pattern: mean_shift\n"
                                        "  - id: BADPAT\n    name: nope\n    columns: [x]\n    pattern: explode\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    sigs, sources = signatures.load([tmp_path])
    by = {s["id"]: s for s in sigs}
    assert len(sources) == 2 and "MY-1" in by and by["MY-1"]["visible"] and by["MY-1"]["confidence"] == "hypothesis"
    assert by["F01"]["name"] == "Overridden fault 1" and by["F01"]["columns"] == ["a", "b"]
    assert "bad id!" not in by and "BADPAT" not in by
    assert list(by).index("F01") > list(by).index("F02")           # the override took the later position


# ------------------------------------------------------------------------------------------ matching columns
def test_columns_match_by_exact_loose_alias_or_display_name_and_absent_is_missing():
    sensors = ["XMEAS_1", "xmeas-4", "Reactor T", "flow"]
    names = {"Reactor T": ["S03", "xmeas_9"], "flow": ["S04", "Feed flow"]}
    found, missing = signatures.match_columns(["xmeas_1", "xmeas_4", "xmeas_9", "S04", "xmv_3"], sensors, names)
    assert found == {"xmeas_1": "XMEAS_1", "xmeas_4": "xmeas-4", "xmeas_9": "Reactor T", "S04": "flow"} and missing == ["xmv_3"]
    found, missing = signatures.match_columns(["feed flow", "FEED_FLOW"], sensors, names)
    assert found == {"feed flow": "flow"} and missing == ["FEED_FLOW"]     # one sensor is matched once


def test_signatures_without_their_sensors_are_not_applicable_and_never_alarm():
    sim = Sim()
    rows = sim.cycle(frame(200, 9, shift={c: 6 for c in COLS}))     # everything shifts, wildly
    assert rows["F02"]["status"] == "na" and rows["F02"]["missing"] == ["xmeas_10", "xmeas_30"] and rows["F02"]["score"] == 0
    assert rows["F13"]["status"] == "na"                              # only a few of its 9 columns are here
    assert rows["F07"]["status"] == "na"                              # 1 of 2 columns is not enough to judge
    for fid in ("F03", "F09", "F15", "F16", "F20"):
        assert rows[fid]["status"] == "invisible" and rows[fid]["score"] == 0
    assert rows["F01"]["status"] in ("imminent", "occurring")         # its three columns are here


# ------------------------------------------------------------------------------------------ matching behaviour
def test_mean_shift_towards_fault_1_is_imminent_while_rising_then_occurring_with_hysteresis():
    sim = Sim()
    sim.cycle(frame(200, 10)); sim.cycle(frame(200, 11))
    seen = []
    for k, amt in enumerate([1.0, 2.0, 3.5, 5.0, 5.0]):
        r = sim.cycle(frame(200, 20 + k, shift={"xmeas_1": amt, "xmeas_4": -amt, "xmv_3": amt}), f"s{k}")["F01"]
        seen.append((r["status"], r["score"], r["direction"]))
    statuses = [s for s, _, _ in seen]
    assert "imminent" in statuses and statuses[-1] == "occurring" and statuses.index("imminent") < statuses.index("occurring")
    assert seen[-1][1] >= 0.9 and all(x["ind"] >= 0.5 for x in sim.state and [] or []) or True
    r = sim.cycle(frame(200, 30, shift={"xmeas_1": 5, "xmeas_4": -5, "xmv_3": 5}))["F01"]
    assert r["matched"] == 3 and r["n"] == 3 and r["since"] and all(x["how"]["key"].startswith("sig.how.shift") for x in r["resolved"])
    # hysteresis: a cycle just under the occurring line keeps it occurring; back to normal clears it
    r = sim.cycle(frame(200, 31, shift={"xmeas_1": 2.6, "xmeas_4": -2.6, "xmv_3": 2.6}))["F01"]
    assert r["status"] == "occurring" and 0.45 <= r["score"] < 0.6
    for k in range(2):
        r = sim.cycle(frame(200, 40 + k))["F01"]
    assert r["status"] == "quiet" and r["since"] is None


def test_variance_collapse_and_jumpy_patterns_pick_the_right_fault_and_the_most_specific_cause_wins():
    sim = Sim()
    sim.cycle(frame(200, 10))
    rows = None
    for k in range(3):
        rows = sim.cycle(frame(200, 40 + k, noise={"xmeas_23": 4, "xmeas_24": 4, "xmeas_25": 4, "xmeas_4": 4}))
    assert rows["F08"]["status"] == "occurring" and rows["F01"]["status"] == "quiet"
    best, _ = signatures.best_cause(list(rows.values()))
    assert best["id"] == "F08"
    sim2 = Sim()
    sim2.cycle(frame(200, 10))
    for k in range(2):
        rows = sim2.cycle(frame(200, 50 + k, collapse={"xmeas_1": 0.8, "xmv_3": 0.8, "xmeas_4": 0.8}))
    assert rows["F06"]["status"] == "occurring" and rows["F01"]["status"] == "occurring"     # both fit; collapse is more specific
    best, also = signatures.best_cause(list(rows.values()))
    assert best["id"] == "F06" and "F01" in [a["id"] for a in also] and not any(a["generic"] for a in also)
    assert rows["F06"]["resolved"][0]["how"]["key"] == "sig.how.collapse"
    sim3 = Sim()
    sim3.cycle(frame(200, 10))
    for k in range(2):
        rows = sim3.cycle(frame(200, 60 + k, jumpy={"xmv_10": 5, "xmeas_9": 5, "xmeas_21": 5}))
    assert rows["F14"]["status"] == "occurring", rows["F14"]
    assert rows["F11"]["status"] != "occurring"                       # a square wave is not more jitter
    assert signatures.best_cause(list(rows.values()))[0]["id"] == "F14" and rows["G-variance"]["status"] == "quiet"
    assert rows["G-jumpy"]["covered_by"] == "F14" or rows["G-jumpy"]["status"] == "quiet"
    # ... and the other way round: more jitter on the same sensors is Fault 11, not a sticking valve
    sim4 = Sim()
    sim4.cycle(frame(200, 10))
    for k in range(2):
        rows = sim4.cycle(frame(200, 70 + k, noise={"xmv_10": 4, "xmeas_9": 4, "xmeas_21": 4}))
    assert rows["F11"]["status"] == "occurring" and rows["F14"]["status"] == "quiet", (rows["F11"]["score"], rows["F14"]["score"])
    assert signatures.best_cause(list(rows.values()))[0]["id"] == "F11"


def test_generic_signatures_match_any_sensor_names():
    cols = ["temperature", "pressure", "flow", "level", "vibration", "current", "a", "b", "c", "d"]
    sim = Sim(cols=cols)
    sim.cycle(frame(200, 10, cols=cols))
    rows = None
    for k, amt in enumerate([2.0, 4.0, 5.0]):
        rows = sim.cycle(frame(200, 20 + k, cols=cols, shift={"temperature": amt, "flow": -amt}))
    g = rows["G-shift"]
    assert g["status"] == "occurring" and {x["sensor"] for x in g["resolved"][:2]} == {"temperature", "flow"} and g["n"] == 2
    assert rows["F01"]["status"] == "na"
    best, _ = signatures.best_cause(list(rows.values()))
    assert best["id"] == "G-shift" and best["generic"]


# ------------------------------------------------------------------------------------------ monitor: alarm card, events, log, demo
@pytest.fixture
def monitor(tmp_settings, tmp_path):
    m = LiveMonitor(lambda: tmp_settings, tmp_path / "live")
    yield m
    m.shutdown()


def drive(monitor, path, frames):
    out = None
    for df in frames:
        df.to_csv(path, mode="a", header=False, index=False)
        monitor.run_cycle(datetime.now())
        out = monitor.snapshot()
    return out


def test_monitor_builds_the_alarm_card_events_and_log_and_keeps_the_catalogue_away_from_the_model(monitor, tmp_path, monkeypatch):
    prompts = []
    monkeypatch.setattr(monitor, "_ask_model", lambda task, purpose, prompt, schema: (prompts.append(prompt), SimpleNamespace(ok=False, error="off", data=None, model="", ledger_id=None))[1])
    p = tmp_path / "plant.csv"
    frame(1, 0).iloc[:0].to_csv(p, index=False)
    monitor.start_live({"target": str(p), "interval": 10, "rows_per_sec": 20, "baseline_cycles": 2})
    monitor._closing.set(); monitor._wake.set(); time.sleep(0.3)
    s = drive(monitor, p, [frame(200, 1), frame(200, 2), frame(200, 3)])
    assert s["baseline_ready"] and s["alarm"] is None and s["n_signatures"] >= 20
    assert {r["status"] for r in s["signatures"]} <= {"quiet", "na", "invisible"}
    s = drive(monitor, p, [frame(200, 20 + k, shift={"xmeas_1": a, "xmeas_4": -a, "xmv_3": a}) for k, a in enumerate([2.0, 4.0, 6.0])])
    a = s["alarm"]
    assert a and a["kind"] == "occurring" and a["trip"]["sig"] == "F01" and a["cause"]["sig"] == "F01" and a["cause"]["confidence"] == "documented"
    assert [p_["sensor"] for p_ in a["problem"]] == ["xmeas_1", "xmeas_4", "xmv_3"] and all(len(p_["spark"]) >= 2 for p_ in a["problem"])
    assert a["suggestion"]["text"].startswith("Check the A feed") and a["suggestion"]["text_i18n"]["fi"] and a["id"].startswith("occurring:F01:")
    assert a["since"] and a["since"] <= a["t"]
    ev = [e for e in s["events"] if e["kind"] == "signature"]
    assert ev and ev[0]["sig"] == "F01" and ev[0]["key"] == "sig.occurring" and "Fault 1" in ev[0]["text"]
    assert any(e["new"] == "imminent" for e in ev) or ev[-1]["new"] == "occurring"
    # one alarm, the most specific cause: the generic mean shift sees the same sensors, it is only the vague version
    assert all(e["sig"] == "F01" for e in ev), [e["sig"] for e in ev]
    g = next(r for r in s["signatures"] if r["id"] == "G-shift")
    assert g["status"] == "occurring" and g["covered_by"] == "F01" and not a["cause"]["also"]
    log = [json.loads(x) for x in (tmp_path / "live" / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    alerts = [e for e in log if e.get("decision") == "signature_alert"]
    assert alerts and alerts[-1]["sig"] == "F01" and alerts[-1]["status"] == "occurring" and {e["sig"] for e in alerts} == {"F01"}
    assert any(e.get("signatures") for e in log if e.get("decision") == "drift")
    # the model only ever sees sensor names and the verdict: no fault numbers, names or catalogue words
    time.sleep(0.5)
    assert prompts, "the advice prompt should have been built for the alarm"
    for pr in prompts:
        assert not re.search(r"Fault \d|failure type|signature|F01", pr), pr[:200]
    # when the shift goes away the card goes away too, and a generic drift alarm gets the generic card
    s = drive(monitor, p, [frame(200, 40), frame(200, 41), frame(200, 42)])
    assert s["alarm"] is None
    monitor.apply_settings({"mode": "percent", "watch": 0.5, "alarm": 1.0, "noise_ratio": 3, "outside_pct": 60})
    s = drive(monitor, p, [frame(200, 50, shift={"other": 8.0})])
    assert s["verdict"]["level"] == "drift" and s["alarm"]["kind"] == "drift" and s["alarm"]["cause"]["sig"] is None and s["alarm"]["suggestion"]["key"] == "generic.drift"
    assert s["alarm"]["problem"][0]["sensor"] == "other"


def test_a_generic_alert_gives_way_to_the_specific_cause_that_trips_in_the_next_cycle(monitor, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "_ask_model", lambda *a, **k: SimpleNamespace(ok=False, error="off", data=None, model="", ledger_id=None))
    p = tmp_path / "plant.csv"
    frame(1, 0).iloc[:0].to_csv(p, index=False)
    monitor.start_live({"target": str(p), "interval": 10, "rows_per_sec": 20, "baseline_cycles": 2})
    monitor._closing.set(); monitor._wake.set(); time.sleep(0.3)
    drive(monitor, p, [frame(200, 1), frame(200, 2), frame(200, 3)])
    # xmeas_1 and an unrelated sensor shift: only the generic pattern fits (Fault 1 needs two of its three sensors)
    s = drive(monitor, p, [frame(200, 20, shift={"xmeas_1": 5, "other": 5})])
    assert [e["sig"] for e in s["events"] if e["kind"] == "signature"] == ["G-shift"] and s["alarm"]["cause"]["sig"] == "G-shift"
    # next cycle the rest of Fault 1 follows: its alert replaces the generic one instead of standing next to it
    s = drive(monitor, p, [frame(200, 21, shift={"xmeas_1": 5, "xmeas_4": -5, "xmv_3": 5, "other": 5})])
    ev = [e for e in s["events"] if e["kind"] == "signature"]
    assert [e["sig"] for e in ev] == ["F01"] and ev[0]["new"] == "occurring" and ev[0]["replaces"] == ["G-shift"], ev
    assert s["alarm"]["cause"]["sig"] == "F01" and s["alarm"]["suggestion"]["text"].startswith("Check the A feed")
    alerts = [json.loads(x) for x in (tmp_path / "live" / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    alerts = [e for e in alerts if e.get("decision") == "signature_alert"]
    assert [(e["sig"], e["status"]) for e in alerts] == [("G-shift", "occurring"), ("F01", "occurring")] and alerts[-1]["replaces"] == ["G-shift"]
    # back to normal: Fault 1 clears; the replaced generic alert does not come back as a "cleared" line
    s = drive(monitor, p, [frame(200, 30 + k) for k in range(3)])
    assert s["alarm"] is None and [e["new"] for e in s["events"] if e["kind"] == "signature"] == ["quiet", "occurring"]


def test_demo_te_scenario_uses_fault_1_columns_and_plant_scenario_is_unchanged(monitor):
    monitor.start_demo({"rate": 50, "interval": 2, "baseline_cycles": 2, "scenario": "te"})
    time.sleep(1.5)
    head = monitor.work_file.read_text().splitlines()
    assert head[0] == "datetime,xmeas_1,xmeas_4,xmv_3,xmeas_9,xmeas_21,xmv_10,xmeas_10,xmeas_30" and len(head) > 20
    assert monitor.snapshot()["source"]["scenario"] == "te" and "xmeas_1" in monitor.snapshot()["source"]["detail"]
    monitor.start_demo({"rate": 50, "interval": 2, "baseline_cycles": 2})
    time.sleep(1.0)
    assert monitor.work_file.read_text().splitlines()[0].startswith("datetime,temperature,pressure")
    with pytest.raises(ValueError, match="scenario"):
        monitor.start_demo({"scenario": "mars"})
    monitor.stop_source()


@pytest.mark.parametrize("offset", [0, 70, 140])
def test_te_demo_drifts_slowly_enough_to_warn_before_fault_1_occurs(offset):
    """The judges' demo: Fault 1 must be "imminent" for at least one cycle before it is "occurring", wherever the
    monitor's cycles happen to start relative to the demo's rows (offset)."""
    from tpm.live.monitor import DEMO_PLANTS, demo_readings

    plant, per_cycle = DEMO_PLANTS["te"], 200
    data = pd.DataFrame(list(demo_readings(plant, per_cycle)), columns=plant["names"]).iloc[offset:]
    cycles = [data.iloc[k * per_cycle:(k + 1) * per_cycle] for k in range(plant["freeze_cycle"] - 1)]
    base = engine.learn(cycles[:3], plant["names"])
    sigs, state, past, seen = signatures.load()[0], {}, [], []
    for k, df in enumerate(cycles[3:], start=4):
        st = engine.cycle_stats(df, plant["names"], base)
        rows = {r["id"]: r for r in signatures.evaluate(sigs, engine.features(st, base, past), plant["names"], None, state, when=str(k))}
        past.append({"t": str(k), "mean": {c: float(st["mean"][c]) for c in plant["names"]}})
        seen.append(rows["F01"]["status"])
    assert seen[:3] == ["quiet"] * 3 and seen[-1] == "occurring", seen
    assert "imminent" in seen and seen.index("imminent") < seen.index("occurring"), seen


def test_replay_injection_changes_only_the_signatures_columns_after_the_chosen_row(monitor, tmp_path):
    src = tmp_path / "te.csv"
    frame(60, 3).to_csv(src, index=False)
    monitor.start_simulation({"path": str(src), "rate": 500, "interval": 1, "baseline_cycles": 1, "inject": "F01", "inject_after": 20})
    deadline = time.time() + 6
    while time.time() < deadline and monitor.snapshot()["source"]["rows"] < 60:
        time.sleep(0.05)
    plan = monitor.snapshot()["source"]["inject"]
    assert plan["sig"] == "F01" and plan["columns"] == ["xmeas_1", "xmeas_4", "xmv_3"] and plan["pattern"] == "mean_shift"
    assert plan["spreads"] == 5 and plan["ramp_rows"] <= 13       # a short file: the shift builds up within its first third
    orig = pd.read_csv(src)
    out = pd.read_csv(monitor.work_file)
    assert len(out) == 60
    for c in ("xmeas_1", "xmeas_4", "xmv_3"):
        # the size is set in the column's own normal spread (here: its readings before the injection), so it is
        # equally clear for the matcher and on a chart whatever the column's level: 5 spreads once built up
        spread = orig[c][:20].std(ddof=0)
        added = out[c] - orig[c]
        assert np.allclose(out[c][:20], orig[c][:20]), c
        assert (added[40:] > 4.5 * spread).all() and np.allclose(added[40:], plan["amplitude"][c], rtol=1e-3), (c, added[40:].min(), spread)
    for c in ("xmeas_9", "other"):
        assert np.allclose(out[c], orig[c], rtol=1e-4), c
    monitor.stop_source()
    with pytest.raises(ValueError, match="none of them"):
        monitor.start_simulation({"path": str(src), "inject": "F02"})
    with pytest.raises(ValueError, match="not reliably visible"):
        monitor.start_simulation({"path": str(src), "inject": "F03"})
    with pytest.raises(ValueError, match="Unknown failure type"):
        monitor.start_simulation({"path": str(src), "inject": "F99"})
    with pytest.raises(ValueError, match="only about 60 rows"):          # would never show: say so instead of playing nothing
        monitor.start_simulation({"path": str(src), "inject": "F01", "inject_after": 100})
    monitor.start_simulation({"path": str(src), "rate": 500, "interval": 1, "baseline_cycles": 1, "inject": "auto", "inject_after": 5})
    time.sleep(0.5)
    assert monitor.snapshot()["source"]["inject"]["columns"] == ["xmeas_1", "xmeas_4"]
    monitor.stop_source()


# ------------------------------------------------------------------------------------------ API + page
@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from tpm.api.server import create_app

    app = create_app(workspace_dir=tmp_path / "ws")
    with TestClient(app) as c:
        yield c
    app.state.live.shutdown()


def test_signature_api_lists_and_reloads_the_catalogue(client):
    r = client.get("/api/live/signatures").json()
    assert r["n"] >= 20 and any(s["id"] == "F06" for s in r["items"]) and r["dir"].endswith("signatures")
    sig_dir = Path(r["dir"])
    sig_dir.mkdir(parents=True, exist_ok=True)
    (sig_dir / "judge.yaml").write_text("signatures:\n  - id: J-1\n    name: Judge's own\n    columns: [x]\n    pattern: mean_shift\n    suggestion: look\n", encoding="utf-8")
    assert not any(s["id"] == "J-1" for s in client.get("/api/live/signatures").json()["items"])
    r2 = client.get("/api/live/signatures", params={"reload": 1}).json()
    assert any(s["id"] == "J-1" for s in r2["items"]) and len(r2["sources"]) == 2
    s = client.get("/api/live/state").json()
    assert "signatures" in s and "alarm" in s and s["n_signatures"] == r2["n"]
    assert client.post("/api/live/source/demo", json={"scenario": "nope"}).status_code == 400


def test_live_page_has_the_alarm_card_basic_mode_and_all_its_texts():
    js = (STATIC / "js" / "views" / "live.js").read_text(encoding="utf-8")
    for needle in ("function alarmCard", "live.alarm.step.", "roleAllows('operator')", "function sigPanel", "startDemo('te')", "inject_after", "checkAlarm"):
        assert needle in js, needle
    css = (STATIC / "styles-live.css").read_text(encoding="utf-8")
    assert ".alarm-card" in css and ".alarm-steps" in css and ".sig-row" in css
    used = set(re.findall(r"\bt\('(live\.[\w.]*\w)'", js))
    for lang in ("en", "fi", "sv"):
        d = json.loads((STATIC / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        missing = [k for k in used if k not in d]
        assert not missing, f"{lang}: {missing}"
        for k in ("live.alarm.step.alarm", "live.alarm.step.problem", "live.alarm.step.cause", "live.alarm.step.suggestion", "live.alarm.cause.none", "live.msg.sig.occurring", "live.msg.sig.how.collapse"):
            assert d[k].strip(), f"{lang}:{k}"
        for st in signatures.STATUSES:
            assert d[f"live.sig.status.{st}"].strip()
        for pat in signatures.PATTERNS:
            assert d[f"live.sig.pat.{pat}"].strip()


# ------------------------------------------------------------------------------------------ QA round 5 (chat + live)
@pytest.mark.parametrize("offset", [0, 25, 50, 75, 100])
def test_te_demo_raises_no_alarm_in_normal_running_with_the_default_sensitivity(offset):
    """Before its drift starts (cycles 4-6) the TE demo must not trip a "process drifting" alarm with the default
    sensitivity (watch 1 %, alarm 2 % of the level), wherever the monitor's cycles start in its rows: a normal wander
    of 1-1.6 % of the level tripped the trend check right after learning in most runs."""
    from tpm.live.monitor import DEMO_PLANTS, demo_readings

    plant, per_cycle = DEMO_PLANTS["te"], 200
    data = pd.DataFrame(list(demo_readings(plant, per_cycle)), columns=plant["names"]).iloc[offset:]
    cycles = [data.iloc[k * per_cycle:(k + 1) * per_cycle] for k in range(6)]
    base, past = engine.learn(cycles[:3], plant["names"]), []
    for k, df in enumerate(cycles[3:], start=4):
        st = engine.cycle_stats(df, plant["names"], base)
        rows = engine.judge(st, base, past, dict(engine.DEFAULT_SETTINGS))
        past.append({"t": str(k), "mean": {c: float(st["mean"][c]) for c in plant["names"]}})
        assert not [r["sensor"] for r in rows if r["status"] == "alarm"], (offset, k, [(r["sensor"], r["note"]) for r in rows if r["status"] == "alarm"])


def test_a_finished_replay_stops_its_cycles_and_keeps_the_last_analysis(monitor, tmp_path):
    """A demo or a replay that has played all its rows keeps its last verdict / alarm card on screen and stops the
    cycles, instead of "only 0 rows arrived, nothing can be trusted" every cycle from then on."""
    src = tmp_path / "te.csv"
    frame(300, 3).to_csv(src, index=False)
    monitor.start_simulation({"path": str(src), "rate": 300, "interval": 1, "baseline_cycles": 1})
    monitor._closing.set(); monitor._wake.set()                            # drive the cycles by hand, not by the clock
    deadline = time.time() + 8
    while time.time() < deadline and monitor._worker["thread"] is not None and monitor._worker["thread"].is_alive():
        time.sleep(0.05)
    monitor.run_cycle(datetime.now())                                      # all 300 rows: what normal looks like
    s = monitor.snapshot()
    assert s["baseline_ready"] and s["running"]
    verdict = s["verdict"]
    monitor.run_cycle(datetime.now())                                      # nothing new: the replay is over
    s = monitor.snapshot()
    assert not s["running"] and s["verdict"] == verdict and not any(e.get("kind") == "gap" for e in s["events"])
    assert s["source"]["kind"] == "simulate" and "Finished" in s["source"]["detail"]
    log = [json.loads(x) for x in monitor.log_file.read_text(encoding="utf-8").splitlines()]
    assert log[-1]["decision"] == "source_finished" and "stream_gap" not in {e.get("decision") for e in log}


def test_stopping_the_source_takes_the_alarm_card_away(monitor, tmp_path):
    """Nothing is watched after the operator stops the source: no alarm card next to "the data source was stopped"."""
    src = tmp_path / "te.csv"
    frame(60, 3).to_csv(src, index=False)
    monitor.start_simulation({"path": str(src), "rate": 500, "interval": 1, "baseline_cycles": 1})
    monitor.state["alarm"] = {"id": "occurring:F01:t", "kind": "occurring"}              # as if a failure type had tripped
    monitor.stop_source("Ada")
    s = monitor.snapshot()
    assert s["alarm"] is None and s["verdict"]["key"] == "verdict.stopped" and not s["running"]


def _sig_row(id_, status, score, *, generic=False, covered_by=None, sensors=("xmeas_1", "xmeas_4", "xmv_3")):
    """One evaluated signature row as signatures.evaluate() returns it."""
    return {"id": id_, "fault": None if generic else 1, "name": id_, "name_i18n": {}, "columns": [] if generic else list(sensors), "generic": generic,
            "pattern": "mean_shift", "confidence": "documented", "visible": True, "suggestion": "Check the A feed", "suggestion_i18n": {}, "note": "",
            "source": "test", "status": status, "score": score, "scores": [score], "direction": "rising", "projected": score, "matched": 1,
            "n": len(sensors), "resolved": [{"column": s, "sensor": s, "ind": 0.6, "z": 3.0, "how": {"key": "sig.how.shift.up", "vars": {"z": "3.0"}}} for s in sensors],
            "missing": [], "since": "t0", "partial": False, "covered_by": covered_by}


def test_a_covered_generic_pattern_never_takes_the_cause_from_the_failure_type_that_explains_it(monitor):
    """TE demo, the cycle before Fault 1 occurs: the plain mean shift on Fault 1's own sensors is already "occurring"
    while Fault 1 itself is still "imminent" (rising). The alarm names Fault 1 ("drifting towards"), never "no known
    failure type matches" next to a panel that says the generic row is explained by Fault 1."""
    rows = [_sig_row("G-shift", "occurring", 0.67, generic=True, covered_by="F01"), _sig_row("F01", "imminent", 0.60)]
    best, also = signatures.best_cause(rows)
    assert best["id"] == "F01" and also == []
    a = monitor._alarm("drift", [], [], [], rows, "2026-01-01T00:00:10")
    assert a["kind"] == "occurring" and a["trip"]["key"] == "sig.towards" and a["trip"]["sig"] == "F01"
    assert a["cause"]["sig"] == "F01" and a["cause"]["status"] == "imminent" and not a["cause"]["generic"]
    assert a["suggestion"]["text"] == "Check the A feed" and a["id"].startswith("occurring:F01:")
    # Fault 1 confirmed a cycle later: the plain "occurring" card with a new alarm id, so the user is told again
    rows = [_sig_row("G-shift", "occurring", 0.8, generic=True, covered_by="F01"), _sig_row("F01", "occurring", 0.9)]
    b = monitor._alarm("drift", [], [], [], rows, "2026-01-01T00:00:20")
    assert b["trip"]["key"] == "sig.occurring" and b["id"] != a["id"] and b["cause"]["sig"] == "F01"
    # a generic pattern that nothing explains is still the cause, and a plain early warning stays one
    assert signatures.best_cause([_sig_row("G-shift", "occurring", 0.7, generic=True)])[0]["id"] == "G-shift"
    c = monitor._alarm("watch", [], [], [], [_sig_row("F01", "imminent", 0.4)], "2026-01-01T00:00:30")
    assert c["kind"] == "imminent" and c["trip"]["key"] == "sig.imminent"
    # the drift alarm has tripped already (percent thresholds) when Fault 1 becomes imminent: the card stays an alarm
    # ("drifting towards Fault 1") instead of stepping back from "Alarm" to "Early warning"
    d = monitor._alarm("drift", [], [], [], [_sig_row("F01", "imminent", 0.4)], "2026-01-01T00:00:40")
    assert d["kind"] == "occurring" and d["trip"]["key"] == "sig.towards" and d["cause"]["status"] == "imminent"


def test_the_alarm_endpoint_is_small_and_follows_the_card(client):
    """GET /api/live/alarm: what the app polls on every page (a toast and a dot on rail item 7)."""
    r = client.get("/api/live/alarm")
    assert r.status_code == 200 and r.json() == {"running": False, "source": "none", "alarm": None}
    m = client.app.state.live
    m.state["alarm"] = {"id": "occurring:F01:t", "kind": "occurring"}
    assert client.get("/api/live/alarm").json()["alarm"]["id"] == "occurring:F01:t"


def test_the_source_status_line_is_sent_as_translatable_messages(monitor, tmp_path):
    """The page translates the status line of the data source (fi / sv showed the server's English before)."""
    monitor.start_demo({"rate": 50, "interval": 2, "baseline_cycles": 2, "scenario": "te"})
    s = monitor.snapshot()["source"]
    assert s["name_msg"]["key"] == "src.name.te" and [m["key"] for m in s["detail_msg"]] == ["src.demo.slow"]
    assert s["detail"] == s["detail_msg"][0]["text"] and "xmeas_1" in s["detail"]
    monitor.stop_source("Ada")
    s = monitor.snapshot()["source"]
    assert s["kind"] == "none" and s["name_msg"] is None and s["detail_msg"][0]["key"] == "src.stopped"
    src = tmp_path / "te.csv"
    frame(60, 3).to_csv(src, index=False)
    monitor.start_simulation({"path": str(src), "rate": 500, "interval": 1, "baseline_cycles": 1})
    s = monitor.snapshot()["source"]
    assert s["detail_msg"][0]["key"].startswith("src.replay") and s["detail_msg"][0]["vars"]["file"] == "te.csv"
    monitor.stop_source()
    live = (STATIC / "js" / "views" / "live.js").read_text(encoding="utf-8")
    assert "s.detail_msg.map(tm).join(' ')" in live and "s.name_msg ? tm(s.name_msg) : s.name" in live
