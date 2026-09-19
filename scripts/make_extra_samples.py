"""Generate five extra randomized synthetic datasets in samples/ (different lengths, schemas, formats, fault mixes).

    python scripts/make_extra_samples.py [--force] [--out samples] [--seed N]

Unlike tests/fixtures/synth.py (one fixed plant), every dataset here draws its own random structure: latent
drivers, loadings, lags, scales, names, sampling period, group lengths, fault episodes and DQ issues. Nothing is
modelled on a specific dataset. The hidden ground truth of all five goes to samples/extra_truth.json.

Fault classes (so sensor-vs-process attribution can be scored):
  process   step, ramp, oscillation, noise_burst   injected on a latent driver -> every loading signal moves, with lags
  sensor    stuck_sensor, sensor_bias, sensor_drift, sensor_noise, corr_break   one measured signal only
  actuator  gain_change   the effect of one actuator on its coupled signals changes

Files:
  extra_small_batches.jsonl      ~1 k rows, 5 short batches, string batch ids, 0.5 s ISO-Z timestamps, JSON lines
  extra_uneven_headerless.dat    ~7 k rows, 14 runs of very uneven length, whitespace, no header, numeric only
  extra_wide_labeled.tsv         14.4 k rows, 30 runs x 480, 33 tag-style signals, numeric run-level fault label
  extra_modes_rowlabels.csv      ~30 k rows, 16 runs, operating modes (regime shifts that are NOT faults), row-level
                                 string labels, epoch-second timestamps with jitter, heavy DQ issues
  extra_long_continuous_eu.csv   40 k rows, one continuous run without group column, daily seasonality, transient
                                 fault episodes, ';' delimiter, decimal comma, day-first timestamps
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy.signal import lfilter

ROOT = Path(__file__).resolve().parents[1]

PROCESS_FAULTS = ["step", "ramp", "oscillation", "noise_burst"]
SENSOR_FAULTS = ["stuck_sensor", "sensor_bias", "sensor_drift", "sensor_noise", "corr_break"]
ACTUATOR_FAULTS = ["gain_change"]
ALL_FAULTS = PROCESS_FAULTS + SENSOR_FAULTS + ACTUATOR_FAULTS
STRUCTURAL_DQ = ("missing_rows", "duplicate_rows")
TEXT_TOKENS = ["ERR", "#N/A", "n/a", "BAD", "?"]


@dataclass
class Spec:
    name: str
    fmt: str  # csv | csv_eu | tsv | dat | jsonl
    n_groups: int
    len_range: tuple[int, int]
    n_measured: int
    n_actuators: int = 1
    n_held: int = 0
    n_const: int = 0
    n_derived: int = 0
    n_quantized: int = 0
    n_drivers: int = 3
    naming: str = "snake"  # snake | tag | plain | none
    group_col: Optional[str] = "run"
    group_ids: str = "int"  # int | str
    counter_col: Optional[str] = "sample"
    time_col: Optional[str] = None
    time_format: str = "iso"  # iso | isoz | eu | epoch
    period_s: float = 60.0
    jitter: float = 0.0
    seasonal: bool = False
    mode_col: Optional[str] = None
    labels: Optional[str] = None  # group_numeric | row_string
    label_col: str = "fault_label"
    fault_fraction: float = 0.6
    episodes_per_group: int = 1
    onset_range: tuple[float, float] = (0.3, 0.7)
    transient: float = 0.0  # probability that an episode ends before its group does
    magnitude: tuple[float, float] = (2.5, 6.0)  # in standard deviations of the affected driver / signal
    dq: tuple[str, ...] = ()
    description: str = ""


SPECS = [
    Spec(name="extra_small_batches.jsonl", fmt="jsonl", n_groups=5, len_range=(150, 320), n_measured=6, n_actuators=1, n_const=1, n_drivers=2,
         naming="snake", group_col="batch", group_ids="str", counter_col=None, time_col="ts", time_format="isoz", period_s=0.5,
         fault_fraction=0.6, magnitude=(3.0, 6.0), dq=("missing_block", "spike_out_of_range", "scattered_missing"),
         description="short batches, string batch ids, sub-second ISO-Z timestamps, JSON lines"),
    Spec(name="extra_uneven_headerless.dat", fmt="dat", n_groups=14, len_range=(120, 900), n_measured=9, n_actuators=2, n_held=1, n_quantized=1, n_drivers=3,
         naming="none", group_col="run", counter_col="sample", fault_fraction=0.5, transient=0.3,
         dq=("frozen_block", "unit_shift", "scattered_missing", "spike_out_of_range"),
         description="very uneven run lengths, whitespace-separated, no header, numeric only (NaN tokens)"),
    Spec(name="extra_wide_labeled.tsv", fmt="tsv", n_groups=30, len_range=(480, 480), n_measured=24, n_actuators=4, n_held=2, n_const=1, n_derived=2, n_drivers=6,
         naming="tag", group_col="RunNo", counter_col="Step", labels="group_numeric", label_col="fault_class", fault_fraction=0.7, onset_range=(0.1, 0.5),
         magnitude=(1.5, 5.0), dq=("duplicate_rows", "spike_out_of_range", "missing_block"),
         description="wide tag-style schema, equal runs, numeric run-level label (whole faulty run carries its class), some subtle faults"),
    Spec(name="extra_modes_rowlabels.csv", fmt="csv", n_groups=16, len_range=(1400, 2400), n_measured=10, n_actuators=2, n_held=1, n_derived=1, n_quantized=1, n_drivers=4,
         naming="plain", group_col="Campaign", counter_col=None, time_col="Time", time_format="epoch", period_s=20.0, jitter=0.15, mode_col="Operating mode",
         labels="row_string", label_col="State", fault_fraction=0.6, transient=0.5,
         dq=("missing_block", "scattered_missing", "spike_out_of_range", "frozen_block", "unit_shift", "sentinel_values", "duplicate_rows", "missing_rows", "timestamp_gap", "timestamp_disorder"),
         description="operating modes shift the process (not faults), row-level string labels, jittered epoch timestamps, heavy DQ issues"),
    Spec(name="extra_long_continuous_eu.csv", fmt="csv_eu", n_groups=1, len_range=(40000, 40000), n_measured=8, n_actuators=2, n_held=1, n_derived=1, n_drivers=3,
         naming="snake", group_col=None, counter_col=None, time_col="aikaleima", time_format="eu", period_s=10.0, seasonal=True,
         episodes_per_group=7, transient=1.0, magnitude=(2.5, 5.0),
         dq=("timestamp_gap", "missing_rows", "frozen_block", "unit_shift", "sentinel_values", "text_tokens"),
         description="one continuous run, no group column, daily seasonality, 7 transient fault episodes, ';' + decimal comma + day-first timestamps"),
]


# --------------------------------------------------------------------------------------------------
# structure (drawn once per dataset)
# --------------------------------------------------------------------------------------------------
def _names(spec: Spec, rng: np.random.Generator, roles: list[str]) -> list[str]:
    used: set[str] = set()
    locs = ["Inlet", "Outlet", "Reactor", "Tank", "Pump", "Line 2", "Cooler", "Header", "Mixer", "Feed"]
    qty = [("flow", "m3/h"), ("temperature", "°C"), ("pressure", "bar"), ("level", "%"), ("speed", "rpm"), ("current", "A"), ("density", "kg/m3")]
    snake = {"measured": ["flow", "temp", "press", "level", "speed", "current", "torque", "vib", "cond", "dens", "humid", "ph"], "actuator": ["valve", "setp"],
             "held": ["analyzer"], "const": ["cfg"], "derived": ["total"], "quantized": ["count"]}
    tag = {"measured": lambda: f"{rng.choice(list('FTPLSA'))}T", "actuator": lambda: f"{rng.choice(list('FTPL'))}V", "held": lambda: "AI", "const": lambda: "HS",
           "derived": lambda: "FY", "quantized": lambda: "ZT"}
    tag_suffix = {"measured": "PV", "actuator": "OP", "held": "PV", "const": "SP", "derived": "CV", "quantized": "PV"}
    out = []
    for i, role in enumerate(roles):
        while True:
            if spec.naming == "none":
                nm = f"c{i}"
            elif spec.naming == "snake":
                nm = f"{rng.choice(snake[role])}_{int(rng.integers(1, 100)):02d}"
            elif spec.naming == "tag":
                nm = f"{tag[role]()}-{int(rng.integers(100, 900))}.{tag_suffix[role]}"
            else:
                q, u = qty[int(rng.integers(len(qty)))]
                loc = str(rng.choice(locs))
                nm = {"measured": f"{loc} {q} [{u}]", "actuator": f"{loc} valve position [%]", "held": f"{loc} analyser [mg/l]", "const": "Recipe setpoint [-]",
                      "derived": f"Total {q} [{u}]", "quantized": f"{loc} counter [pcs]"}[role]
            if nm not in used:
                used.add(nm)
                out.append(nm)
                break
    return out


def _structure(spec: Spec, rng: np.random.Generator) -> dict[str, Any]:
    K, A = spec.n_drivers, spec.n_actuators
    roles = ["measured"] * spec.n_measured + ["actuator"] * A + ["held"] * spec.n_held + ["const"] * spec.n_const + ["derived"] * spec.n_derived + ["quantized"] * spec.n_quantized
    names = _names(spec, rng, roles)
    st: dict[str, Any] = {"drivers": [{"phi": float(rng.uniform(0.8, 0.98))} for _ in range(K)], "signals": [], "actuators": []}
    for nm, role in zip(names, roles):
        sig: dict[str, Any] = {"name": nm, "role": role}
        if role == "actuator":
            sig.update(index=len(st["actuators"]), hold=int(rng.integers(15, 80)))
            st["actuators"].append(sig)
        elif role == "const":
            sig["value"] = float(np.round(rng.uniform(1, 200), 1))
        elif role != "derived":  # measured-like: measured, held, quantized
            ks = rng.choice(K, size=int(rng.integers(1, min(2, K) + 1)), replace=False)
            scale = float(np.exp(rng.uniform(np.log(0.05), np.log(400))))
            sig.update(loads=[{"driver": int(k), "w": float(rng.uniform(0.5, 1.5) * rng.choice([-1, 1])), "lag": int(rng.integers(0, 9))} for k in ks],
                       noise=float(rng.uniform(0.05, 0.3)), scale=scale, offset=0.0 if rng.random() < 0.15 else scale * float(rng.uniform(4, 30)))
            if A and rng.random() < 0.5:
                sig["act"] = {"actuator": int(rng.integers(A)), "b": float(rng.uniform(0.3, 1.0) * rng.choice([-1, 1])), "lag": int(rng.integers(0, 4))}
            if role == "held":
                sig["hold"] = int(rng.integers(4, 13))
            if role == "quantized":
                sig["resolution"] = float(f"{scale * 0.5:.2g}")
        st["signals"].append(sig)
    measured = [s for s in st["signals"] if s["role"] == "measured"]
    for s in st["signals"]:
        if s["role"] == "derived":
            a, b = rng.choice(len(measured), size=2, replace=False)
            s.update(a=measured[a]["name"], b=measured[b]["name"], sign=int(rng.choice([-1, 1])))
    if spec.mode_col:
        st["modes"] = {"names": ["idle", "run", "boost"], "shift": rng.uniform(-2.5, 2.5, size=(3, K)).round(2).tolist()}
    return st


# --------------------------------------------------------------------------------------------------
# fault episodes
# --------------------------------------------------------------------------------------------------
def _plan_episodes(spec: Spec, rng: np.random.Generator, lengths: list[int]) -> dict[int, list[dict[str, Any]]]:
    types = list(rng.permutation(ALL_FAULTS))
    plan: dict[int, list[dict[str, Any]]] = {g: [] for g in range(len(lengths))}
    n_ep = 0

    def episode(onset: int, end: int) -> dict[str, Any]:
        nonlocal n_ep
        n_ep += 1
        return {"id": n_ep, "type": str(types[(n_ep - 1) % len(types)]), "onset": onset, "end": end, "magnitude": float(rng.uniform(*spec.magnitude)), "sign": int(rng.choice([-1, 1]))}

    if spec.episodes_per_group > 1:
        for g, n in enumerate(lengths):
            edges = np.linspace(0.15 * n, 0.97 * n, spec.episodes_per_group + 1).astype(int)
            for a, b in zip(edges[:-1], edges[1:]):
                dur = int(rng.uniform(0.25, 0.6) * (b - a))
                onset = int(rng.integers(a, b - dur))
                plan[g].append(episode(onset, onset + dur))
        return plan
    n_fault = int(round(spec.fault_fraction * len(lengths)))
    for g in sorted(rng.choice(len(lengths), size=n_fault, replace=False).tolist()):
        n = lengths[g]
        onset = int(rng.uniform(*spec.onset_range) * n)
        end = min(n, onset + max(20, int(rng.uniform(0.08, 0.25) * n))) if rng.random() < spec.transient else n
        plan[g].append(episode(onset, end))
    return plan


def _lag(x: np.ndarray, k: int) -> np.ndarray:
    return x if k <= 0 else np.concatenate([np.full(k, x[0]), x[:-k]])


def _ar1_unit(n: int, phi: float, rng: np.random.Generator) -> np.ndarray:
    """Stationary AR(1) with unit variance (200-sample burn-in)."""
    return lfilter([1.0], [1.0, -phi], rng.normal(0, 1, n + 200))[200:] * np.sqrt(1 - phi**2)


def _make_group(spec: Spec, st: dict[str, Any], rng: np.random.Generator, n: int, episodes: list[dict[str, Any]], t_offset: int) -> tuple[dict[str, np.ndarray], np.ndarray, Optional[np.ndarray]]:
    K = len(st["drivers"])
    idx = np.arange(n)
    U = np.stack([_ar1_unit(n, d["phi"], rng) for d in st["drivers"]])
    if spec.seasonal:
        U[0] += 1.5 * np.sin(2 * np.pi * (t_offset + idx) / (86400.0 / spec.period_s))
    mode = None
    if spec.mode_col:
        hold = int(rng.integers(200, 600))
        mode = np.repeat(rng.integers(0, 3, size=n // hold + 2), hold)[:n]
        shift = np.asarray(st["modes"]["shift"])[mode].T  # K x n
        U += lfilter([0.3], [1.0, -0.7], shift, axis=1)  # smooth regime transitions

    measured = [s for s in st["signals"] if s["role"] == "measured"]
    gains = np.ones((max(1, len(st["actuators"])), n))
    # process / actuator faults act before mixing; sensor faults after
    for ep in episodes:
        w = (idx >= ep["onset"]) & (idx < ep["end"])
        rel = (idx[w] - ep["onset"]).astype(float)
        mag = ep["magnitude"] * ep["sign"]
        if ep["type"] == "gain_change" and not any("act" in s for s in st["signals"]):
            ep["type"] = "step"  # nothing is coupled to an actuator in this structure
        if ep["type"] in PROCESS_FAULTS:
            k = int(rng.integers(K))
            ep.update({"class": "process", "driver": k, "signals": [s["name"] for s in st["signals"] if any(ld["driver"] == k for ld in s.get("loads", []))]})
            if ep["type"] == "step":
                U[k, w] += mag * (1 - np.exp(-(rel + 1) / rng.uniform(1, 6)))
            elif ep["type"] == "ramp":
                U[k, w] += mag * rel / max(1.0, ep["end"] - ep["onset"])
            elif ep["type"] == "oscillation":
                U[k, w] += 0.5 * mag * np.sin(2 * np.pi * rel / rng.uniform(8, 40))
            else:
                U[k, w] += rng.normal(0, 0.5 * abs(mag), int(w.sum()))
        elif ep["type"] == "gain_change":
            a = int(rng.choice([s["act"]["actuator"] for s in st["signals"] if "act" in s]))
            ep.update({"class": "actuator", "actuator": st["actuators"][a]["name"], "factor": float(rng.choice([0.2, 2.5])),
                       "signals": [s["name"] for s in st["signals"] if s.get("act", {}).get("actuator") == a]})
            gains[a, w] = ep["factor"]
        else:
            ep.update({"class": "sensor", "signals": [str(rng.choice([s["name"] for s in measured]))]})

    cols: dict[str, np.ndarray] = {}
    eff = []
    for a in st["actuators"]:
        cols[a["name"]] = np.repeat(rng.uniform(20, 80, size=n // a["hold"] + 2), a["hold"])[:n].round(1)
        eff.append((cols[a["name"]] - 50) / 20.0)
    for s in st["signals"]:
        if "loads" not in s:
            continue
        z = sum(ld["w"] * _lag(U[ld["driver"]], ld["lag"]) for ld in s["loads"]) + rng.normal(0, s["noise"], n)
        if "act" in s:
            z = z + s["act"]["b"] * _lag(eff[s["act"]["actuator"]] * gains[s["act"]["actuator"]], s["act"]["lag"])
        x = s["offset"] + s["scale"] * (z + rng.normal(0, 0.15))  # small run-to-run offset
        if s["role"] == "held":
            x = x[(idx // s["hold"]) * s["hold"]]
        elif s["role"] == "quantized":
            x = np.round(x / s["resolution"]) * s["resolution"]
        cols[s["name"]] = x
    for s in st["signals"]:
        if s["role"] == "const":
            cols[s["name"]] = np.full(n, s["value"])
        elif s["role"] == "derived":  # independent redundant measurement: a sensor fault on a or b breaks this relation
            noise = 0.02 * min(np.std(cols[s["a"]]), np.std(cols[s["b"]]))
            cols[s["name"]] = cols[s["a"]] + s["sign"] * cols[s["b"]] + rng.normal(0, noise, n)

    for ep in episodes:
        if ep.get("class") != "sensor":
            continue
        name = ep["signals"][0]
        x = cols[name] = cols[name].copy()
        w = (idx >= ep["onset"]) & (idx < ep["end"])
        rel = (idx[w] - ep["onset"]).astype(float)
        sd = float(np.std(x))
        mag = ep["magnitude"] * ep["sign"]
        if ep["type"] == "stuck_sensor":
            x[w] = x[ep["onset"]]
        elif ep["type"] == "sensor_bias":
            x[w] += mag * sd
        elif ep["type"] == "sensor_drift":
            x[w] += mag * sd * rel / max(1.0, ep["end"] - ep["onset"])
        elif ep["type"] == "sensor_noise":
            x[w] += rng.normal(0, 0.5 * abs(mag) * sd, int(w.sum()))
        else:  # corr_break: same level and spread, no relation to the process any more
            x[w] = float(np.mean(x)) + sd * _ar1_unit(int(w.sum()), 0.9, rng)

    ep_id = np.zeros(n, dtype=int)
    for ep in episodes:
        ep_id[ep["onset"] : ep["end"]] = ep["id"]
    return cols, ep_id, mode


# --------------------------------------------------------------------------------------------------
# data-quality issues
# --------------------------------------------------------------------------------------------------
def _apply_dq(df: pd.DataFrame, spec: Spec, st: dict[str, Any], rng: np.random.Generator) -> tuple[pd.DataFrame, list[dict[str, Any]], dict[str, dict[int, str]]]:
    issues = [d for d in spec.dq if spec.time_col or not d.startswith("timestamp")]
    n0 = len(df)
    edges = np.linspace(0.04 * n0, 0.96 * n0, len(issues) + 1).astype(int)
    order = rng.permutation(len(issues))
    zones = {issues[i]: (int(edges[z]), int(edges[z + 1])) for i, z in enumerate(order)}
    measured = [s["name"] for s in st["signals"] if s["role"] == "measured"]
    truth: list[dict[str, Any]] = []
    text_cells: dict[str, dict[int, str]] = {}

    def block(kind: str, lo: int, hi: int) -> tuple[int, int]:
        a, b = zones[kind]
        third = max(1, (b - a) // 3)
        return int(a + rng.integers(0, third)), int(min(max(1, rng.integers(lo, hi + 1)), third))

    # structural issues first, lowest rows first, so every recorded row index is final
    for kind in sorted([d for d in issues if d in STRUCTURAL_DQ], key=lambda d: zones[d][0]):
        r0, L = block(kind, 3, 10) if kind == "duplicate_rows" else block(kind, 5, 60)
        if kind == "duplicate_rows":
            df = pd.concat([df.iloc[: r0 + L], df.iloc[r0 : r0 + L], df.iloc[r0 + L :]], ignore_index=True)
            truth.append({"type": kind, "signal": None, "row_start": r0 + L, "row_end": r0 + 2 * L - 1})
        else:
            df = pd.concat([df.iloc[:r0], df.iloc[r0 + L :]], ignore_index=True)
            truth.append({"type": kind, "signal": None, "first_row_after_gap": r0, "n_rows_dropped": L})

    for kind in [d for d in issues if d not in STRUCTURAL_DQ]:
        sig = str(rng.choice(measured))
        a, b = zones[kind]
        if kind == "missing_block":
            r0, L = block(kind, 20, 200)
            df.loc[r0 : r0 + L - 1, sig] = np.nan
            truth.append({"type": kind, "signal": sig, "row_start": r0, "row_end": r0 + L - 1})
        elif kind == "scattered_missing":
            rate = float(rng.uniform(0.005, 0.03))
            rows = np.flatnonzero(rng.random(len(df)) < rate)
            df.loc[rows, sig] = np.nan
            truth.append({"type": kind, "signal": sig, "rate": round(rate, 4), "n_rows": int(len(rows))})
        elif kind == "spike_out_of_range":
            rows = sorted(rng.choice(np.arange(a, b), size=int(rng.integers(1, 5)), replace=False).tolist())
            df.loc[rows, sig] = [99999.0 if rng.random() < 0.5 else float(df[sig].abs().max() * rng.uniform(50, 1000)) for _ in rows]
            truth.append({"type": kind, "signal": sig, "rows": rows})
        elif kind == "frozen_block":
            r0, L = block(kind, 30, 150)
            df.loc[r0 : r0 + L - 1, sig] = df.loc[r0, sig]
            truth.append({"type": kind, "signal": sig, "row_start": r0, "row_end": r0 + L - 1})
        elif kind == "unit_shift":
            r0, L = block(kind, 40, 300)
            how = str(rng.choice(["x1000", "x0.001", "x1.8+32"]))
            v = df.loc[r0 : r0 + L - 1, sig]
            df.loc[r0 : r0 + L - 1, sig] = v * 1000 if how == "x1000" else v * 0.001 if how == "x0.001" else v * 1.8 + 32
            truth.append({"type": kind, "signal": sig, "transform": how, "row_start": r0, "row_end": r0 + L - 1})
        elif kind == "sentinel_values":
            rows = sorted(rng.choice(np.arange(a, b), size=int(rng.integers(10, 41)), replace=False).tolist())
            val = float(rng.choice([-999.0, 9999.0, -9999.0]))
            df.loc[rows, sig] = val
            truth.append({"type": kind, "signal": sig, "value": val, "rows": rows})
        elif kind == "text_tokens":
            rows = sorted(rng.choice(np.arange(a, b), size=int(rng.integers(5, 16)), replace=False).tolist())
            text_cells[sig] = {int(r): str(rng.choice(TEXT_TOKENS)) for r in rows}
            truth.append({"type": kind, "signal": sig, "rows": rows, "tokens": sorted(set(text_cells[sig].values()))})
        elif kind == "timestamp_gap":
            r0, _ = block(kind, 1, 1)
            gap = float(spec.period_s * rng.integers(20, 120))
            df.loc[r0:, spec.time_col] = df.loc[r0:, spec.time_col] + pd.Timedelta(seconds=gap)
            truth.append({"type": kind, "signal": spec.time_col, "row_start": r0, "row_end": r0, "gap_seconds": gap})
        elif kind == "timestamp_disorder":
            r0, L = block(kind, 10, 40)
            df.loc[r0 : r0 + L - 1, spec.time_col] = df.loc[r0 : r0 + L - 1, spec.time_col] - pd.Timedelta(seconds=float(spec.period_s * rng.integers(500, 3000)))
            truth.append({"type": kind, "signal": spec.time_col, "row_start": r0, "row_end": r0 + L - 1})
    return df, truth, text_cells


# --------------------------------------------------------------------------------------------------
# dataset = structure + groups + DQ + file
# --------------------------------------------------------------------------------------------------
def make_dataset(spec: Spec, seed: int) -> tuple[pd.DataFrame, dict[str, Any], dict[str, dict[int, str]]]:
    rng = np.random.default_rng(seed)
    st = _structure(spec, rng)
    lengths = [int(rng.integers(spec.len_range[0], spec.len_range[1] + 1)) for _ in range(spec.n_groups)]
    plan = _plan_episodes(spec, rng, lengths)
    if spec.group_ids == "str":
        gids: list[Any] = [f"B-{v:04d}" for v in sorted(rng.choice(np.arange(1, 9999), size=spec.n_groups, replace=False).tolist())]
    else:
        gids = list(range(1, spec.n_groups + 1))

    signal_order = [st["signals"][i]["name"] for i in rng.permutation(len(st["signals"]))]  # roles interleaved, not blocked
    t_cursor = pd.Timestamp("2026-04-01 06:00:00") + pd.Timedelta(days=int(rng.integers(0, 120)))
    frames, t_offset = [], 0
    for g, n in enumerate(lengths):
        cols, ep_id, mode = _make_group(spec, st, rng, n, plan[g], t_offset)
        df = pd.DataFrame({c: cols[c] for c in signal_order})
        lead: dict[str, Any] = {}
        if spec.group_col:
            lead[spec.group_col] = gids[g]
        if spec.counter_col:
            lead[spec.counter_col] = np.arange(1, n + 1)
        if spec.time_col:
            steps = spec.period_s * (1 + spec.jitter * np.clip(rng.normal(0, 1, n), -2.5, 2.5))
            ts = t_cursor + pd.to_timedelta(np.round(np.cumsum(steps), 3), unit="s")
            lead[spec.time_col] = ts
            t_cursor = ts[-1] + pd.Timedelta(minutes=int(rng.integers(30, 720)))
        if spec.mode_col:
            lead[spec.mode_col] = np.asarray(st["modes"]["names"])[mode]
        if spec.labels == "row_string":
            by_id = {ep["id"]: ep["type"] for ep in plan[g]}
            lead[spec.label_col] = [by_id.get(int(e), "normal") for e in ep_id]
        df = pd.concat([pd.DataFrame(lead), df], axis=1)
        if spec.labels == "group_numeric":  # whole run carries its class, 0 = normal run
            df[spec.label_col] = ALL_FAULTS.index(plan[g][0]["type"]) + 1 if plan[g] else 0
        df["__g"], df["__ep"] = g, ep_id
        frames.append(df)
        t_offset += n
    data = pd.concat(frames, ignore_index=True)
    data, dq_truth, text_cells = _apply_dq(data, spec, st, rng)

    episodes = []
    for g, eps in plan.items():
        for ep in eps:
            rows = np.flatnonzero(data["__ep"].to_numpy() == ep["id"])
            episodes.append({"group": gids[g], "type": ep["type"], "class": ep["class"], "signals": ep["signals"], "row_start": int(rows.min()), "row_end": int(rows.max()),
                             "onset_in_group": ep["onset"], "persists_to_group_end": bool(ep["end"] >= lengths[g]), "magnitude_sd": round(ep["magnitude"], 2),
                             **{k: ep[k] for k in ("driver", "actuator", "factor") if k in ep}})
    data = data.drop(columns=["__g", "__ep"])
    roles = {s["name"]: s["role"] for s in st["signals"]}
    roles.update({c: r for c, r in ((spec.group_col, "identifier"), (spec.counter_col, "counter"), (spec.time_col, "timestamp"), (spec.mode_col, "operating_mode"), (spec.label_col if spec.labels else None, "label")) if c})
    truth = {
        "file": spec.name, "description": spec.description, "seed": seed, "n_rows": int(len(data)), "n_groups": spec.n_groups, "group_lengths": lengths,
        "format": {"kind": spec.fmt, "header": spec.fmt != "dat", "time_format": spec.time_format if spec.time_col else None, "period_s": spec.period_s if spec.time_col else None},
        "columns": list(data.columns), "roles": roles, "label_kind": spec.labels, "label_codes": {f: i + 1 for i, f in enumerate(ALL_FAULTS)} if spec.labels == "group_numeric" else None,
        "relations": [{"signal": s["name"], "drivers": s["loads"], "actuator": ({**s["act"], "actuator": st["actuators"][s["act"]["actuator"]]["name"]} if "act" in s else None)} for s in st["signals"] if "loads" in s]
        + [{"signal": s["name"], "derived_from": [s["a"], s["b"]], "sign": s["sign"]} for s in st["signals"] if s["role"] == "derived"],
        "modes": st.get("modes"), "normal_groups": [gids[g] for g, eps in plan.items() if not eps], "episodes": episodes, "dq": dq_truth,
    }
    if spec.fmt == "dat":
        truth["note"] = "headerless file: 'columns' gives the hidden names in file column order"
    return data, truth, text_cells


def write_dataset(df: pd.DataFrame, spec: Spec, text_cells: dict[str, dict[int, str]], path: Path) -> None:
    df = df.copy()
    if spec.time_col:
        t = pd.to_datetime(df[spec.time_col])
        if spec.time_format == "epoch":
            df[spec.time_col] = ((t - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).astype("int64")
        elif spec.time_format == "isoz":
            df[spec.time_col] = t.dt.strftime("%Y-%m-%dT%H:%M:%S.%f").str[:-3] + "Z"
        else:
            df[spec.time_col] = t.dt.strftime("%d.%m.%Y %H:%M:%S" if spec.time_format == "eu" else "%Y-%m-%d %H:%M:%S")
    decimal = "," if spec.fmt == "csv_eu" else "."
    for col, cells in text_cells.items():  # a numeric column polluted with text tokens
        s = df[col].map(lambda v: "" if pd.isna(v) else f"{v:.6g}".replace(".", decimal)).astype(object)
        s.loc[list(cells)] = list(cells.values())
        df[col] = s
    if spec.fmt == "jsonl":
        df.to_json(path, orient="records", lines=True, double_precision=5, force_ascii=False)
    elif spec.fmt == "dat":
        df.to_csv(path, index=False, header=False, sep=" ", float_format="%.6g", na_rep="NaN")
    else:
        df.to_csv(path, index=False, sep={"csv": ",", "csv_eu": ";", "tsv": "\t"}[spec.fmt], decimal=decimal, float_format="%.6g", encoding="utf-8")


def _readme(truths: list[dict[str, Any]]) -> str:
    lines = ["# Extra training datasets (synthetic, randomized)\n", "Generated by `python scripts/make_extra_samples.py`. Nothing here is real data. Hidden ground truth (signal roles, relations, fault episodes with class process / sensor / actuator, DQ issues, all with final row indices) is in `extra_truth.json`.\n",
             "| file | rows | groups | what |", "|---|---|---|---|"]
    for t in truths:
        faults = ", ".join(sorted({e["type"] for e in t["episodes"]}))
        dq = ", ".join(d["type"] for d in t["dq"])
        lines.append(f"| {t['file']} | {t['n_rows']} | {t['n_groups']} | {t['description']}; faults: {faults}; DQ: {dq} |")
    return "\n".join(lines) + "\n"


def make_all(out_dir: str | Path, force: bool = False, seed: int = 20260919) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {s.name: out / s.name for s in SPECS}
    truth_path = out / "extra_truth.json"
    if not force and truth_path.exists() and all(p.exists() for p in paths.values()):
        return paths
    truths = []
    for i, spec in enumerate(SPECS):
        df, truth, text_cells = make_dataset(spec, seed + i)
        write_dataset(df, spec, text_cells, paths[spec.name])
        truths.append(truth)
    truth_path.write_text(json.dumps({"seed": seed, "datasets": truths}, indent=1, ensure_ascii=False, default=lambda o: o.item() if hasattr(o, "item") else str(o)), encoding="utf-8")
    (out / "README_extra.md").write_text(_readme(truths), encoding="utf-8")
    return paths


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "samples"))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--seed", type=int, default=20260919, help="base seed; another value gives five different random datasets")
    a = ap.parse_args()
    for name, p in make_all(a.out, force=a.force, seed=a.seed).items():
        print(f"{name:<32} {p}  ({p.stat().st_size / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
