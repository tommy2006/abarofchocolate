"""Generic synthetic multi-run process generator with hidden ground truth.

Used by every agent's tests and by the evaluation of the pipeline itself. It is NOT modelled on any
specific dataset: a few latent drivers, lagged measured signals, actuator-like steps, a sample-and-hold
analyzer channel, a constant, a derived/redundant signal, plus injected faults and data-quality issues.

    from tests.fixtures.synth import make_synthetic, write_variants
    df, truth = make_synthetic(seed=1)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

FAULT_TYPES = ["step", "ramp", "stuck_sensor", "corr_break", "oscillation", "noise_burst"]
DQ_TYPES = ["missing_block", "spike_out_of_range", "duplicate_rows", "unit_shift", "frozen_block", "timestamp_gap"]


def _ar1(n: int, phi: float, sigma: float, rng: np.random.Generator) -> np.ndarray:
    x = np.zeros(n)
    e = rng.normal(0, sigma, n)
    for t in range(1, n):
        x[t] = phi * x[t - 1] + e[t]
    return x


def make_synthetic(
    n_groups: int = 24,
    n_samples: int = 400,
    seed: int = 0,
    header: bool = True,
    fault_fraction: float = 0.6,
    dq_issues: bool = True,
    with_timestamp: bool = False,
    with_labels: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    rng = np.random.default_rng(seed)
    frames = []
    truth: dict[str, Any] = {"groups": {}, "dq": [], "signal_roles": {}, "relations": [], "n_groups": n_groups, "n_samples": n_samples}
    n_fault_groups = int(round(fault_fraction * n_groups))
    fault_groups = set(rng.choice(n_groups, size=n_fault_groups, replace=False).tolist())

    for g in range(n_groups):
        n = n_samples
        u1 = _ar1(n, 0.95, 0.30, rng)
        u2 = _ar1(n, 0.90, 0.40, rng)
        u3 = _ar1(n, 0.80, 0.50, rng)
        # actuator-like manipulated variables: piecewise constant steps in [0, 100]
        a1 = np.repeat(rng.uniform(30, 70, size=n // 25 + 1), 25)[:n]
        a2 = np.repeat(rng.uniform(20, 80, size=n // 40 + 1), 40)[:n]
        a1_eff = (a1 - 50) / 20.0
        a2_eff = (a2 - 50) / 30.0

        def lag(x: np.ndarray, k: int) -> np.ndarray:
            if k <= 0:
                return x
            return np.concatenate([np.full(k, x[0]), x[:-k]])

        s = {}
        s["flow_a"] = 100 + 8 * u1 + 3 * a1_eff + rng.normal(0, 0.8, n)
        s["press_r"] = 2700 + 25 * lag(u1, 2) + 10 * u2 + 12 * a2_eff + rng.normal(0, 2.5, n)
        s["temp_r"] = 120 + 1.5 * lag(u1, 4) - 0.8 * lag(u2, 1) + rng.normal(0, 0.15, n)
        s["level_s"] = 50 + 6 * u3 + 2 * lag(u2, 3) + rng.normal(0, 0.6, n)
        s["flow_b"] = 40 + 4 * lag(u2, 1) + 2 * a1_eff + rng.normal(0, 0.5, n)
        s["temp_s"] = 80 + 1.2 * lag(u1, 6) + 0.5 * u3 + rng.normal(0, 0.2, n)
        s["valve_1"] = a1
        s["valve_2"] = a2
        analyzer = 30 + 5 * lag(u1, 3) + rng.normal(0, 0.3, n)
        held = analyzer.copy()
        for t in range(n):
            held[t] = analyzer[(t // 6) * 6]
        s["comp_a"] = held
        s["const_c"] = np.full(n, 7.5)
        s["derived_sum"] = s["flow_a"] + s["flow_b"]
        s["power_c"] = 300 + 15 * lag(u2, 2) + 5 * a2_eff + rng.normal(0, 3.0, n)

        info: dict[str, Any] = {"fault": None, "onset": None}
        if g in fault_groups:
            ftype = FAULT_TYPES[g % len(FAULT_TYPES)]
            onset = int(rng.uniform(0.35, 0.6) * n)
            info = {"fault": ftype, "onset": onset}
            idx = np.arange(n)
            m = idx >= onset
            if ftype == "step":
                s["flow_a"][m] += 6.0
                s["press_r"][m] += 18.0 * 1.0
                s["temp_r"][m] += 1.2
            elif ftype == "ramp":
                r = (idx[m] - onset) / max(1, n - onset)
                s["press_r"][m] += 60 * r
                s["temp_r"][m] += 3.0 * r
                s["temp_s"][m] += 2.0 * r
            elif ftype == "stuck_sensor":
                s["level_s"][m] = s["level_s"][onset]
            elif ftype == "corr_break":
                s["flow_b"][m] = 40 + rng.normal(0, 0.5, m.sum())  # loses relation to u2
            elif ftype == "oscillation":
                s["press_r"][m] += 20 * np.sin(2 * np.pi * (idx[m] - onset) / 12.0)
                s["level_s"][m] += 4 * np.sin(2 * np.pi * (idx[m] - onset) / 12.0 + 0.6)
            elif ftype == "noise_burst":
                s["temp_r"][m] += rng.normal(0, 1.5, m.sum())
                s["flow_a"][m] += rng.normal(0, 4.0, m.sum())
        truth["groups"][str(g)] = info

        df = pd.DataFrame(s)
        df.insert(0, "sample", np.arange(1, n + 1))
        df.insert(0, "run", g + 1)
        if with_timestamp:
            t0 = pd.Timestamp("2026-01-01") + pd.Timedelta(hours=6 * g)
            df.insert(2, "timestamp", t0 + pd.to_timedelta(np.arange(n) * 180, unit="s"))
        if with_labels:
            df["fault_label"] = info["fault"] or "normal"
        frames.append(df)

    data = pd.concat(frames, ignore_index=True)
    signal_cols = [c for c in data.columns if c not in ("run", "sample", "timestamp", "fault_label")]

    if dq_issues:
        n_total = len(data)
        # missing block
        r0 = int(0.12 * n_total)
        data.loc[r0 : r0 + 60, "temp_s"] = np.nan
        truth["dq"].append({"type": "missing_block", "signal": "temp_s", "row_start": r0, "row_end": r0 + 60})
        # spike out of range
        r1 = int(0.33 * n_total)
        data.loc[r1, "press_r"] = 99999.0
        truth["dq"].append({"type": "spike_out_of_range", "signal": "press_r", "row_start": r1, "row_end": r1})
        # frozen block on a continuous signal (not a process fault, a dead sensor)
        r2 = int(0.55 * n_total)
        data.loc[r2 : r2 + 80, "flow_a"] = data.loc[r2, "flow_a"]
        truth["dq"].append({"type": "frozen_block", "signal": "flow_a", "row_start": r2, "row_end": r2 + 80})
        # unit shift (x1000) on a segment
        r3 = int(0.72 * n_total)
        data.loc[r3 : r3 + 50, "power_c"] = data.loc[r3 : r3 + 50, "power_c"] * 1000.0
        truth["dq"].append({"type": "unit_shift", "signal": "power_c", "row_start": r3, "row_end": r3 + 50})
        # duplicate rows
        r4 = int(0.85 * n_total)
        dup = data.iloc[r4 : r4 + 5].copy()
        data = pd.concat([data.iloc[: r4 + 5], dup, data.iloc[r4 + 5 :]], ignore_index=True)
        truth["dq"].append({"type": "duplicate_rows", "signal": None, "row_start": r4 + 5, "row_end": r4 + 9})
        if with_timestamp:
            r5 = int(0.45 * n_total)
            data.loc[r5:, "timestamp"] = data.loc[r5:, "timestamp"] + pd.Timedelta(minutes=45)
            truth["dq"].append({"type": "timestamp_gap", "signal": "timestamp", "row_start": r5, "row_end": r5})

    truth["signal_roles"] = {
        "flow_a": "continuous_measured", "press_r": "continuous_measured", "temp_r": "continuous_measured", "level_s": "continuous_measured",
        "flow_b": "continuous_measured", "temp_s": "continuous_measured", "valve_1": "actuator_like", "valve_2": "actuator_like",
        "comp_a": "held_sampled", "const_c": "constant", "derived_sum": "derived_redundant", "power_c": "continuous_measured",
        "run": "identifier", "sample": "counter", "timestamp": "timestamp", "fault_label": "label",
    }
    truth["relations"] = [
        {"a": "flow_a", "b": "press_r", "lag": 2}, {"a": "flow_a", "b": "temp_r", "lag": 4}, {"a": "flow_a", "b": "temp_s", "lag": 6},
        {"a": "flow_a", "b": "comp_a", "lag": 3}, {"a": "derived_sum", "b": "flow_a", "lag": 0}, {"a": "derived_sum", "b": "flow_b", "lag": 0},
    ]
    truth["signal_columns"] = signal_cols
    if not header:
        data.columns = [f"col_{i}" for i in range(data.shape[1])]
    return data, truth


def write_variants(df: pd.DataFrame, out_dir: str | Path, stem: str = "synth") -> dict[str, Path]:
    """Write the same data in several formats to test the readers."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    p = out / f"{stem}.csv"
    df.to_csv(p, index=False)
    paths["csv"] = p
    p = out / f"{stem}_noheader.dat"
    df.select_dtypes(include=[np.number]).to_csv(p, index=False, header=False, sep=" ")
    paths["dat_noheader"] = p
    p = out / f"{stem}_transposed.dat"
    num = df.select_dtypes(include=[np.number])
    np.savetxt(p, num.to_numpy().T, fmt="%.6g", delimiter="  ")
    paths["dat_transposed"] = p
    p = out / f"{stem}.parquet"
    df.to_parquet(p, index=False)
    paths["parquet"] = p
    p = out / f"{stem}.tsv"
    df.to_csv(p, index=False, sep="\t")
    paths["tsv"] = p
    p = out / f"{stem}.jsonl"
    df.to_json(p, orient="records", lines=True, date_format="iso")
    paths["jsonl"] = p
    try:
        p = out / f"{stem}.xlsx"
        df.head(5000).to_excel(p, index=False)
        paths["xlsx"] = p
    except Exception:
        pass
    return paths


if __name__ == "__main__":
    import sys

    df, truth = make_synthetic()
    print(df.shape)
    print(df.head())
    if len(sys.argv) > 1:
        print(write_variants(df, sys.argv[1]))
