"""Third domain: an event log gets derived signals (how often each kind of entry occurs, text length); sensor tables
are left alone."""
import json

import numpy as np
import pandas as pd

from tpm.config import load_settings
from tpm.pipeline import run_pipeline
from tpm.workspace import Workspace


def _log(n=3000, seed=3):
    rng = np.random.default_rng(seed)
    ts = pd.Timestamp("2026-03-01") + pd.to_timedelta(np.cumsum(rng.uniform(1, 5, n)), unit="s")
    level = rng.choice(["INFO", "WARN", "ERROR"], n, p=[0.9, 0.07, 0.03])
    level[1500:1800] = np.where(rng.random(300) < 0.4, "ERROR", level[1500:1800])
    svc = rng.choice(["api", "db", "cache"], n)
    return pd.DataFrame({"timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"), "service": svc, "level": level, "latency_ms": rng.lognormal(4, 0.3, n).round(1),
                         "message": [f"request {i} on {s} {'failed: timeout' if lv == 'ERROR' else 'ok'}" for i, (s, lv) in enumerate(zip(svc, level))]})


def test_event_log_gets_rates_and_text_length(tmp_path):
    s = load_settings()
    s.workspace_dir = str(tmp_path / "ws")
    p = tmp_path / "log.csv"
    _log().to_csv(p, index=False)
    st = run_pipeline(str(p), run_id="log", settings=s, stages=["ingest", "profile"], options={"no_llm": True, "skip_llm": True})
    assert st.state == "done", [(x.stage, x.error) for x in st.stages if x.state == "failed"]
    ws = Workspace(run_id="log", settings=s)
    derived = ws.read_json("derived_signals.json") or []
    names = {d["column"] for d in derived}
    assert "share_level_ERROR" in names and "length_message" in names, names
    sch = ws.read_json("schema")
    assert "share_level_ERROR" in sch["signal_columns"] and "level" not in sch["signal_columns"]
    sig = {x["source_column"]: x for x in ws.read_json("signals")}
    assert sig["share_level_ERROR"]["structural_role"] == "continuous_measured"
    assert sig["share_level_ERROR"]["display_name"].startswith("share of level = ERROR")
    v = ws.duckdb().execute('SELECT max("share_level_ERROR") FROM dataset').fetchone()[0]
    assert 0.2 < v <= 1.0  # the planted error burst is visible as a rate


def test_sensor_tables_are_left_alone(tmp_path):
    from tests.fixtures.synth import make_synthetic

    s = load_settings()
    s.workspace_dir = str(tmp_path / "ws")
    df, _ = make_synthetic(n_groups=4, n_samples=200, seed=5)
    p = tmp_path / "s.csv"
    df.to_csv(p, index=False)
    st = run_pipeline(str(p), run_id="s", settings=s, stages=["ingest"], options={"no_llm": True})
    assert st.state == "done"
    ws = Workspace(run_id="s", settings=s)
    assert not ws.read_json("derived_signals.json")
    assert not any(c.startswith(("share_", "length_")) for c in ws.read_json("schema")["signal_columns"])
