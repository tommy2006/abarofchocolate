"""Live sensor monitor (tpm.live): analysis engine, data sources, AI-chosen sensitivity, API and page wiring.

    .venv\\Scripts\\python.exe -m pytest tests/test_g_live.py -q
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

from tpm.live import LiveMonitor, engine

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "tpm" / "api" / "static"


# ------------------------------------------------------------------------------------------ helpers
def frame(n: int, seed: int, *, drift: float = 0.0, freeze: bool = False, junk: bool = False) -> pd.DataFrame:
    """A cycle of readings: temp (smooth), flow (noisy), level, plus an id/time column that must be ignored."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    temp = 75 + np.cumsum(rng.normal(0, 0.02, n)) * 0 + rng.normal(0, 0.3, n) + drift * t / n
    flow = 32 + rng.normal(0, 0.5, n)
    level = 60 + rng.normal(0, 1.0, n)
    if freeze:
        level[:] = level[0]
    df = pd.DataFrame({"datetime": pd.date_range("2026-01-01", periods=n, freq="s").astype(str), "sample": t,
                       "temp": temp, "flow": flow, "level": level})
    if junk:
        df["label"] = "x"
    return df


def learned(cycles: int = 3, n: int = 200) -> tuple[dict, list[str]]:
    frames = [frame(n, s) for s in range(cycles)]
    sensors = engine.sensor_columns(frames[0])
    return engine.learn(frames, sensors), sensors


def judge_cycle(df: pd.DataFrame, base: dict, sensors: list[str], settings: dict | None = None, past: list | None = None):
    st = engine.cycle_stats(df, sensors, base)
    return {r["sensor"]: r for r in engine.judge(st, base, past or [], settings or dict(engine.DEFAULT_SETTINGS))}


# ------------------------------------------------------------------------------------------ engine
def test_sensor_columns_ignore_time_id_and_text():
    df = frame(50, 0, junk=True)
    assert engine.sensor_columns(df) == ["temp", "flow", "level"]


def test_normal_cycle_is_ok_and_types_are_described():
    base, sensors = learned()
    rows = judge_cycle(frame(200, 99), base, sensors, {**engine.DEFAULT_SETTINGS, "watch": 3.0, "alarm": 6.0})
    assert all(r["status"] == "ok" for r in rows.values()), {k: (r["status"], r["note"]) for k, r in rows.items()}
    assert base["temp"]["kind"]["key"] == "kind.continuous"
    assert any(p["key"] == "prof.unit" for p in base["temp"]["profile"])


def test_drift_is_flagged_by_level_and_trend_and_names_the_checks():
    base, sensors = learned()
    rows = judge_cycle(frame(200, 5, drift=6.0), base, sensors)          # temp drifts ~8 % over the cycle
    r = rows["temp"]
    assert r["status"] == "alarm"
    checks = {e["name"]: e["level"] for e in r["elements"]}
    assert checks["level"] == 2 and checks["trend"] >= 1
    assert rows["flow"]["status"] == "ok"
    assert all(set(e) >= {"name", "level", "key", "vars", "text", "short"} for e in r["elements"])
    lvl, v, broken, alarms = engine.verdict(list(rows.values()), len(rows))
    assert lvl == "drift" and v["key"] == "verdict.drift" and v["items"][0]["sensor"] == "temp"


def test_threshold_is_the_operators_choice():
    base, sensors = learned()
    df = frame(200, 5, drift=6.0)
    tight = judge_cycle(df, base, sensors, {**engine.DEFAULT_SETTINGS, "watch": 1.0, "alarm": 2.0})["temp"]["status"]
    loose = judge_cycle(df, base, sensors, {**engine.DEFAULT_SETTINGS, "watch": 40.0, "alarm": 80.0, "outside_pct": 100.0, "noise_ratio": 50.0})["temp"]
    assert tight == "alarm" and loose != "alarm"


def test_dead_sensor_is_a_sensor_problem_not_a_process_alarm():
    base, sensors = learned()
    rows = judge_cycle(frame(200, 7, freeze=True), base, sensors)
    assert rows["level"]["status"] == "dead"
    lvl, v, broken, _ = engine.verdict(list(rows.values()), len(rows))
    assert lvl == "quality" and [b["sensor"] for b in broken] == ["level"]


def test_settings_validation():
    ok = engine.check_settings({"mode": "percent", "watch": 1, "alarm": 2, "noise_ratio": 2, "outside_pct": 20})
    assert ok["alarm"] == 2.0
    for bad in ({"mode": "percent", "watch": 3, "alarm": 2, "noise_ratio": 2, "outside_pct": 20},
                {"mode": "nope", "watch": 1, "alarm": 2, "noise_ratio": 2, "outside_pct": 20},
                {"mode": "percent", "watch": 1, "alarm": 2, "noise_ratio": 0.5, "outside_pct": 20},
                {"mode": "percent", "watch": "x", "alarm": 2, "noise_ratio": 2, "outside_pct": 20}, {}):
        with pytest.raises(ValueError):
            engine.check_settings(bad)


def test_feed_reads_only_new_whole_lines_and_both_separators(tmp_path):
    p = tmp_path / "grow.csv"
    p.write_text("a;b\n1;2\n", encoding="utf-8")
    f = engine.Feed(p, from_start=False)
    assert f.read_new().empty                                            # starts at the end: nothing new yet
    with open(p, "a") as fh:
        fh.write("3;4\n5;6\n7;")                                        # last line is only half written
    assert f.read_new().to_dict("list") == {"a": [3, 5], "b": [4, 6]}
    with open(p, "a") as fh:
        fh.write("8\n")
    assert f.read_new().to_dict("list") == {"a": [7], "b": [8]}
    assert engine.Feed(p, from_start=True).read_new().shape[0] == 4


def test_every_message_key_exists_in_all_languages():
    """The engine returns keys; the page translates ``live.msg.<key>``. The three languages must carry all of them."""
    for lang in ("en", "fi", "sv"):
        d = json.loads((STATIC / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        missing = [k for k in engine.MSG if f"live.msg.{k}" not in d]
        assert not missing, f"{lang}: {missing}"
        for k, en in engine.MSG.items():
            ph = lambda s: set(re.findall(r"\{(\w+)\}", s))
            assert ph(d[f"live.msg.{k}"]) == ph(en), f"{lang}:{k}"


# ------------------------------------------------------------------------------------------ monitor
@pytest.fixture
def monitor(tmp_settings, tmp_path):
    m = LiveMonitor(lambda: tmp_settings, tmp_path / "live")
    yield m
    m.shutdown()


def write_rows(path: Path, frames: list[pd.DataFrame]) -> None:
    for df in frames:
        df.to_csv(path, mode="a", header=False, index=False)


def test_full_cycle_learn_then_detect_drift_and_dead_sensor(monitor, tmp_path):
    p = tmp_path / "plant.csv"
    frame(1, 0).iloc[:0].to_csv(p, index=False)                          # header only
    monitor.start_live({"target": str(p), "interval": 10, "rows_per_sec": 20, "baseline_cycles": 2})
    monitor._closing.set(); monitor._wake.set(); time.sleep(0.5)          # drive the cycles by hand, not by the clock
    assert monitor.snapshot()["running"] and monitor.snapshot()["source"]["kind"] == "live-file"

    def cycle(df):
        write_rows(p, [df])
        monitor.run_cycle(datetime.now())
        return monitor.snapshot()

    s = cycle(frame(200, 1))
    assert s["verdict"]["level"] == "learn" and s["verdict"]["vars"]["done"] == "1"
    s = cycle(frame(200, 2))
    assert s["baseline_ready"] and set(s["baseline"]) == {"temp", "flow", "level"}
    s = cycle(frame(200, 3))
    assert s["verdict"]["level"] in ("ok", "watch")                     # normal data stays quiet at a relaxed setting
    monitor.apply_settings({"mode": "percent", "watch": 3, "alarm": 6, "noise_ratio": 3, "outside_pct": 60})
    s = cycle(frame(200, 4, drift=8.0))
    assert s["verdict"]["level"] == "drift" and any(r["sensor"] == "temp" and r["status"] == "alarm" for r in s["table"])
    row = next(r for r in s["table"] if r["sensor"] == "temp")
    assert row["cycles"] >= 1 and row["since"] and row["notes"][0]["key"].startswith(("level", "trend", "range"))
    s = cycle(frame(200, 5, freeze=True))
    assert s["verdict"]["level"] == "quality" and next(r for r in s["table"] if r["sensor"] == "level")["status"] == "dead"
    s = cycle(frame(20, 6))                                             # far fewer rows than expected
    assert s["verdict"]["level"] == "untrusted" and s["verdict"]["key"] == "verdict.gap"
    log = [json.loads(x) for x in (tmp_path / "live" / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {"source_changed", "baseline_learned", "settings_changed", "stream_gap"} <= {e.get("decision") for e in log}


def test_changing_the_threshold_rejudges_the_latest_cycle_at_once(monitor, tmp_path):
    p = tmp_path / "plant.csv"
    frame(1, 0).iloc[:0].to_csv(p, index=False)
    monitor.start_live({"target": str(p), "interval": 10, "rows_per_sec": 20, "baseline_cycles": 2})
    monitor._closing.set(); monitor._wake.set(); time.sleep(0.5)
    for i, kw in enumerate([{}, {}, {"drift": 6.0}]):
        write_rows(p, [frame(200, i + 1, **kw)])
        monitor.run_cycle(datetime.now())
    assert monitor.snapshot()["verdict"]["level"] == "drift"
    monitor.apply_settings({"mode": "percent", "watch": 40, "alarm": 80, "noise_ratio": 50, "outside_pct": 100})
    assert monitor.snapshot()["verdict"]["level"] in ("ok", "watch")
    assert monitor.snapshot()["settings_by"]["by"] == "operator"


def test_settings_are_saved_and_come_back(tmp_settings, tmp_path):
    m = LiveMonitor(lambda: tmp_settings, tmp_path / "live")
    m.apply_settings({"mode": "percent", "watch": 4, "alarm": 9, "noise_ratio": 3, "outside_pct": 30}, by="operator", name="Ada")
    m.shutdown()
    again = LiveMonitor(lambda: tmp_settings, tmp_path / "live")
    assert (again.set["watch"], again.set["alarm"], again.set_by["by"], again.set_by["name"]) == (4.0, 9.0, "operator", "Ada")
    again.shutdown()


def test_ai_decide_needs_a_learned_baseline_keeps_guard_rails_and_logs_the_call(monitor, tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="not learned"):
        monitor.ai_decide()
    base, _ = learned()
    monitor.state["baseline"] = base
    seen = {}

    def fake(task, purpose, prompt, schema):
        seen.update(task=task, prompt=prompt)
        return SimpleNamespace(ok=True, model="fake-model", ledger_id="EGR-000001", error=None, data={
            "watch_pct": 0.01, "alarm_pct": 500, "noise_factor": 99, "out_of_range_pct": 1, "reason": "Because of the wander."})
    monkeypatch.setattr(monitor, "_ask_model", fake)
    out = monitor.ai_decide("en")
    s = out["settings"]
    assert 0.3 <= s["watch"] and s["alarm"] <= 50 and s["noise_ratio"] <= 6 and s["outside_pct"] >= 10          # clamped to safe limits
    assert monitor.set_by["by"] == "ai" and monitor.set_by["note"] == "Because of the wander."
    assert seen["task"] == "live_settings"
    assert "temp" in seen["prompt"] and "2026-01-01" not in seen["prompt"]          # sensor summaries only, no readings

    monkeypatch.setattr(monitor, "_ask_model", lambda *a, **k: SimpleNamespace(ok=False, error="ollama not reachable", data=None, model="", ledger_id=None))
    with pytest.raises(ValueError, match="Ollama"):
        monitor.ai_decide()


def test_simulation_replays_a_csv_from_a_start_row(monitor, tmp_path):
    src = tmp_path / "big.csv"
    pd.DataFrame({"t": range(50), "x": np.arange(50) * 2.0, "y": np.arange(50) * 3.0}).to_csv(src, index=False)
    monitor.start_simulation({"path": str(src), "rate": 200, "interval": 1, "baseline_cycles": 1, "start_row": 11, "max_rows": 10})
    deadline = time.time() + 5
    while time.time() < deadline and monitor.snapshot()["source"]["rows"] < 10:
        time.sleep(0.05)
    lines = monitor.work_file.read_text().splitlines()
    assert lines[0] == "t,x,y" and lines[1].startswith("10,") and len(lines) == 11
    with pytest.raises(ValueError, match="File not found"):
        monitor.start_simulation({"path": str(tmp_path / "nope.csv")})
    monitor.stop_source()
    assert monitor.snapshot()["source"]["kind"] == "none" and not monitor.snapshot()["running"]


def test_demo_source_produces_a_csv_with_sensors(monitor):
    monitor.start_demo({"rate": 50, "interval": 2, "baseline_cycles": 2})
    time.sleep(1.5)
    head = monitor.work_file.read_text().splitlines()
    assert head[0].startswith("datetime,temperature,pressure") and len(head) > 20
    monitor.stop_source()


def test_a_link_source_reports_bad_links_clearly(monitor):
    for target, word in (("http://127.0.0.1:9/none.csv", "reach"), ("", "Paste"), (str(Path("nope.csv")), "not found")):
        with pytest.raises(ValueError, match=f"(?i){word}"):
            monitor.start_live({"target": target})


# ------------------------------------------------------------------------------------------ API + page wiring
@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from tpm.api.server import create_app

    app = create_app(workspace_dir=tmp_path / "ws")
    with TestClient(app) as c:
        yield c
    app.state.live.shutdown()


def test_api_state_settings_upload_and_errors(client, tmp_path):
    s = client.get("/api/live/state").json()
    assert s["running"] is False and s["verdict"]["key"] == "verdict.nosource" and s["settings"]["mode"] == "percent"
    r = client.post("/api/live/settings", json={"mode": "percent", "watch": 2.5, "alarm": 5, "noise_ratio": 2, "outside_pct": 20, "actor": "Ada"})
    assert r.status_code == 200 and client.get("/api/live/state").json()["settings"]["watch"] == 2.5
    bad = client.post("/api/live/settings", json={"mode": "percent", "watch": 9, "alarm": 5, "noise_ratio": 2, "outside_pct": 20})
    assert bad.status_code == 400 and "Watch" in bad.json()["detail"]
    up = client.post("/api/live/upload?name=my file (1).csv", content=b"a,b\n1,2\n")
    assert up.status_code == 200 and up.json()["size"] == 8 and Path(up.json()["path"]).read_text() == "a,b\n1,2\n"
    assert Path(up.json()["path"]).name == "my_file__1_.csv"
    assert client.post("/api/live/source/simulate", json={"path": "C:/nope.csv"}).status_code == 400
    assert client.post("/api/live/ai-settings", json={}).status_code == 400          # nothing learned yet
    assert isinstance(client.get("/api/live/log").json()["entries"], list)


def test_page_is_registered_translated_and_never_locked():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "views/live.js" in app_js and "id: 'live'" in app_js
    core = (STATIC / "js" / "core.js").read_text(encoding="utf-8")
    assert not re.search(r"VIEWS_NEEDING_RUN\s*=\s*new Set\([^)]*live", core)          # it needs no run
    for lang in ("en", "fi", "sv"):
        d = json.loads((STATIC / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert d["nav.live"].strip()
    js = (STATIC / "js" / "views" / "live.js").read_text(encoding="utf-8")
    used = set(re.findall(r"\bt\('(live\.[\w.]*\w)'", js))          # a key ending in "." is a prefix built at run time
    en = json.loads((STATIC / "i18n" / "en.json").read_text(encoding="utf-8"))
    assert not [k for k in used if k not in en], sorted(k for k in used if k not in en)
