"""Writes a complete, realistic fake run workspace (every artifact of ARCHITECTURE section 3) from the
shared synthetic generator, using the contracts. Used to develop and test the API + UI before the
stage packages exist, and as a demo run.

    from tests.fixtures.fake_workspace import build_fake_workspace
    ws = build_fake_workspace(settings, run_id="run_demo")          # writes workspace/<run_id>/...

CLI:  python -m tests.fixtures.fake_workspace [run_id] [--workspace DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tpm.config import Settings, load_settings  # noqa: E402
from tpm.contracts import (  # noqa: E402
    CheckResult,
    Critique,
    DatasetSchema,
    Diagnosis,
    EgressRecord,
    FaultPattern,
    Flag,
    GroupingCandidate,
    PropagationStep,
    Rule,
    RunStatus,
    SignalContribution,
    SignalDescriptor,
    StageStatus,
    TrustVerdict,
    now_iso,
)
from tpm.workspace import Workspace  # noqa: E402

from .synth import make_synthetic  # noqa: E402

ROLE_BY_NAME = {
    "flow_a": ("continuous_measured", "flow", "feed"),
    "press_r": ("continuous_measured", "pressure", "reactor"),
    "temp_r": ("continuous_measured", "temperature", "reactor"),
    "level_s": ("continuous_measured", "level", "separator"),
    "flow_b": ("continuous_measured", "flow", "separator"),
    "temp_s": ("continuous_measured", "temperature", "separator"),
    "valve_1": ("actuator_like", "valve", "feed"),
    "valve_2": ("actuator_like", "valve", "reactor"),
    "comp_a": ("held_sampled", "composition", "reactor"),
    "const_c": ("constant", None, None),
    "derived_sum": ("derived_redundant", "flow", None),
    "power_c": ("continuous_measured", "power", "compressor"),
}

FAULT_KIND = {"step": "anomaly", "ramp": "drift", "stuck_sensor": "dq", "corr_break": "anomaly", "oscillation": "anomaly", "noise_burst": "anomaly"}
FAULT_CAUSE = {"step": "process", "ramp": "process", "stuck_sensor": "sensor", "corr_break": "sensor", "oscillation": "process", "noise_burst": "mixed"}
FAULT_PATTERN = {"step": "PATTERN-A", "ramp": "PATTERN-B", "oscillation": "PATTERN-C", "noise_burst": "PATTERN-D", "corr_break": None, "stuck_sensor": None}
FAULT_SIGNALS = {
    "step": ["flow_a", "press_r", "temp_r"],
    "ramp": ["press_r", "temp_r", "temp_s"],
    "stuck_sensor": ["level_s"],
    "corr_break": ["flow_b"],
    "oscillation": ["press_r", "level_s"],
    "noise_burst": ["temp_r", "flow_a"],
}


def _r(x: Any, n: int = 3) -> float:
    try:
        return round(float(x), n)
    except Exception:
        return 0.0


def build_fake_workspace(settings: Optional[Settings] = None, run_id: str = "run_demo_synth", seed: int = 1, n_groups: int = 12, n_samples: int = 200, fresh: bool = True) -> Workspace:
    settings = settings or load_settings()
    root = settings.workspace_path
    if fresh and (root / run_id).exists():
        import shutil

        shutil.rmtree(root / run_id, ignore_errors=True)
    ws = Workspace(run_id=run_id, settings=settings)
    df, truth = make_synthetic(n_groups=n_groups, n_samples=n_samples, seed=seed, with_timestamp=True)
    src = ws.dir / "source_synth.csv"
    df.to_csv(src, index=False)

    # ---------- status / meta ----------
    stages = ["ingest", "profile", "quality", "detect", "diagnose", "assess", "report"]
    secs = [4.2, 11.8, 6.1, 48.5, 9.3, 21.0, 2.4]
    st = RunStatus(run_id=run_id, source_path=str(src), profile=settings.profile, state="done", options={"has_header": True, "delimiter": ",", "language": "en"})
    for s, sec in zip(stages, secs):
        st.stages.append(StageStatus(stage=s, state="done", progress=1.0, message=f"done in {sec}s", started_at=now_iso(), finished_at=now_iso()))
    ws.set_status(st)
    ws.write_json("meta", {"run_id": run_id, "source_path": str(src), "profile": settings.profile, "options": st.options, "created_at": now_iso(), "fake": True})
    ws.log.record("system:pipeline", "run_started", "run", run_id, {"source_path": str(src), "profile": settings.profile})

    # ---------- schema ----------
    signal_cols = truth["signal_columns"]
    alias = {c: f"S{i + 1:02d}" for i, c in enumerate(signal_cols)}
    n_rows = len(df)
    data = pd.DataFrame({alias[c]: df[c].astype("float32") for c in signal_cols})
    data.insert(0, "timestamp", df["timestamp"])
    data.insert(0, "__group__", df["run"].astype(str))
    data.insert(0, "__row__", np.arange(n_rows, dtype=np.int64))
    data["run"] = df["run"].astype(int)
    data["sample"] = df["sample"].astype(int)
    data.to_parquet(ws.path("dataset"), index=False)

    ev_schema = []
    ev_schema.append(ws.evidence.add("header", "First line is non-numeric in 15 of 15 columns; treated as a header", computed_by="ingest.readers.sniff", n_samples=n_rows).id)
    ev_schema.append(ws.evidence.add("period", "Timestamp column advances by 180 s in 99.6 % of consecutive rows", signals=[], values={"median_period_s": 180, "share": 0.996}, computed_by="ingest.schema.infer_period", n_samples=n_rows).id)
    ev_schema.append(ws.evidence.add("grouping", f"Column 'run' is block-constant with {n_groups} blocks; 'sample' resets to 1 at every block start", values={"n_blocks": n_groups}, computed_by="ingest.schema.grouping", n_samples=n_rows).id)
    inf_period = ws.inferences.add("dataset", "Sample period is 180 seconds", status="inferred", confidence=0.93, evidence_ids=[ev_schema[1]], reasoning="Consistent timestamp differences", stage="ingest")
    inf_group = ws.inferences.add("dataset", f"Rows form {n_groups} independent groups (batches/runs) keyed by column 'run'", status="inferred", confidence=0.9, evidence_ids=[ev_schema[2]], reasoning="Block-constant key column with a resetting counter", stage="ingest", alternatives=["change-point segmentation (score 0.41)", "no grouping (score 0.12)"])
    inf_sensor = ws.inferences.add("dataset", "The file is a sensor stream, not business records", status="inferred", confidence=0.86, evidence_ids=[ev_schema[1], ev_schema[2]], reasoning="Regular sampling, autocorrelated continuous columns, no free text", stage="profile")
    inf_units = ws.inferences.add("dataset", "Engineering units are unknown; values are treated in native scale", status="assumed", confidence=0.5, evidence_ids=[], reasoning="No unit information in the file", stage="ingest")
    schema = DatasetSchema(
        dataset_id=hashlib.sha1(run_id.encode()).hexdigest()[:12],
        source_path=str(src),
        format="csv",
        n_rows=n_rows,
        n_cols=df.shape[1],
        had_header=True,
        delimiter=",",
        columns=list(data.columns),
        time_column="timestamp",
        sample_period_seconds=180.0,
        order_column="sample",
        group_columns=["run"],
        grouping_method="key_columns",
        grouping_candidates=[
            GroupingCandidate(method="key_columns", columns=["run"], n_groups=n_groups, score=0.94, rationale="block-constant key with resetting counter"),
            GroupingCandidate(method="changepoint", columns=[], n_groups=15, score=0.41, rationale="segmentation on the first principal component"),
            GroupingCandidate(method="none", columns=[], n_groups=1, score=0.12, rationale="single sequence"),
        ],
        label_columns=[],
        meta_columns=["run", "sample", "timestamp"],
        signal_columns=signal_cols,
        signal_alias=alias,
        n_groups=n_groups,
        group_sizes={"min": n_samples, "median": n_samples, "max": n_samples + 5},
        domain_likelihood={"sensor_stream": 0.86, "business_records": 0.06, "lab_measurements": 0.08},
        assumptions=["Sample period 180 s applies to every group", "Units unknown; native scale", "No label columns present"],
        inference_ids=[inf_period.id, inf_group.id, inf_sensor.id, inf_units.id],
        evidence_ids=ev_schema,
        options_used={"has_header": True, "delimiter": ","},
    )
    ws.write_json("schema", schema)
    ws.log.record("system:ingest", "stage", "stage", "ingest", {"state": "done", "n_rows": n_rows})

    # ---------- signals / relations / domain / understanding ----------
    num = data[[alias[c] for c in signal_cols]]
    corr = num.corr().fillna(0.0)
    sig_objs: list[SignalDescriptor] = []
    clusters = {"C1": ["flow_a", "press_r", "temp_r", "temp_s", "comp_a", "derived_sum"], "C2": ["level_s", "flow_b", "power_c"], "C3": ["valve_1", "valve_2"]}
    cluster_of = {alias[s]: cid for cid, ss in clusters.items() for s in ss}
    lag_truth = {(alias[r["a"]], alias[r["b"]]): r["lag"] for r in truth["relations"]}
    all_related: list[dict[str, Any]] = []
    for i, c in enumerate(signal_cols):
        a = alias[c]
        col = data[a]
        role, instr, unit = ROLE_BY_NAME[c]
        x = col.to_numpy(dtype=float)
        diffs = np.diff(x[~np.isnan(x)])
        stuck_frac = float(np.mean(diffs == 0)) if len(diffs) else 0.0
        q = np.nanquantile(x, [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
        ac1 = float(pd.Series(x).autocorr(1)) if np.nanstd(x) > 0 else 1.0
        fp = {
            "count": int(np.sum(~np.isnan(x))),
            "missing_fraction": _r(np.mean(np.isnan(x)), 4),
            "mean": _r(np.nanmean(x)),
            "std": _r(np.nanstd(x)),
            "min": _r(np.nanmin(x)),
            "max": _r(np.nanmax(x)),
            "q01": _r(q[0]), "q05": _r(q[1]), "q25": _r(q[2]), "q50": _r(q[3]), "q75": _r(q[4]), "q95": _r(q[5]), "q99": _r(q[6]),
            "noise_level": _r(np.nanstd(diffs) / (np.nanstd(x) + 1e-9)),
            "autocorr_lag1": _r(ac1),
            "stuck_fraction": _r(stuck_frac),
            "quantization_step": _r(np.nanmin(np.abs(diffs[diffs != 0])) if np.any(diffs != 0) else 0.0, 4),
            "n_unique": int(pd.Series(x).nunique()),
            "dominant_period": None if role != "held_sampled" else 6,
        }
        evs = []
        evs.append(ws.evidence.add("distribution", f"{a}: mean {fp['mean']}, std {fp['std']}, range [{fp['min']}, {fp['max']}] over {fp['count']} samples", signals=[a], values={k: fp[k] for k in ("mean", "std", "min", "max", "q05", "q95")}, computed_by="profile.fingerprint", n_samples=fp["count"]).id)
        if role == "continuous_measured":
            evs.append(ws.evidence.add("noise", f"{a}: lag-1 autocorrelation {fp['autocorr_lag1']}, noise level {fp['noise_level']} -> continuously varying measurement", signals=[a], values={"autocorr_lag1": fp["autocorr_lag1"], "noise_level": fp["noise_level"]}, computed_by="profile.roles", n_samples=fp["count"]).id)
        elif role == "actuator_like":
            evs.append(ws.evidence.add("steps", f"{a}: piecewise constant, {fp['n_unique']} distinct plateaus, values within [0, 100]", signals=[a], values={"n_unique": fp["n_unique"], "stuck_fraction": fp["stuck_fraction"]}, computed_by="profile.roles", n_samples=fp["count"]).id)
        elif role == "held_sampled":
            evs.append(ws.evidence.add("hold", f"{a}: value updates every 6 samples (sample-and-hold), stuck fraction {fp['stuck_fraction']}", signals=[a], values={"period": 6, "stuck_fraction": fp["stuck_fraction"]}, computed_by="profile.roles", n_samples=fp["count"]).id)
        elif role == "constant":
            evs.append(ws.evidence.add("constant", f"{a}: single value {fp['mean']} in every row", signals=[a], values={"value": fp["mean"]}, computed_by="profile.roles", n_samples=fp["count"]).id)
        elif role == "derived_redundant":
            evs.append(ws.evidence.add("derived", f"{a} equals {alias['flow_a']} + {alias['flow_b']} within 1e-4 in 100 % of rows", signals=[a, alias["flow_a"], alias["flow_b"]], values={"residual_max": 1e-4}, computed_by="profile.relations.linear_dependence", n_samples=fp["count"]).id)
        # correlations
        related = []
        for j, c2 in enumerate(signal_cols):
            if c2 == c:
                continue
            r = float(corr.iloc[i, j])
            if abs(r) >= 0.35:
                lag = lag_truth.get((a, alias[c2]), lag_truth.get((alias[c2], a), 0))
                related.append({"signal": alias[c2], "r": _r(r), "lag": lag})
        related.sort(key=lambda d: -abs(d["r"]))
        related = related[:5]
        for rel in related[:2]:
            evid = ws.evidence.add("correlation", f"{a} and {rel['signal']} correlate r={rel['r']} at lag {rel['lag']}", signals=[a, rel["signal"]], values={"r": rel["r"], "lag": rel["lag"]}, computed_by="profile.relations.lagged_xcorr", n_samples=fp["count"]).id
            evs.append(evid)
            all_related.append({"a": a, "b": rel["signal"], "r": rel["r"], "lag": rel["lag"], "evidence_id": evid})
        inf_role = ws.inferences.add(a, f"{a} is a {role.replace('_', ' ')} signal", status="inferred" if role != "unknown" else "uncertain", confidence=0.92 if role in ("constant", "derived_redundant", "actuator_like") else 0.84, evidence_ids=evs[:2], reasoning="Structural fingerprint (noise, autocorrelation, plateaus, hold period)", stage="profile")
        infs = [inf_role.id]
        instr_conf = 0.0
        unit_conf = 0.0
        if instr:
            instr_conf = {"flow": 0.55, "pressure": 0.62, "temperature": 0.58, "level": 0.5, "valve": 0.7, "composition": 0.45, "power": 0.4}[instr]
            inf_i = ws.inferences.add(a, f"{a} may be a {instr} measurement", status="uncertain" if instr_conf < 0.5 else "assumed", confidence=instr_conf, evidence_ids=evs[:1], reasoning="Value range, noise level and coupling to other signals resemble a typical " + instr + " channel; the column name is weak supporting evidence", source="llm-local:gemma4", stage="profile", alternatives=["temperature", "flow", "pressure"] if instr not in ("valve",) else ["controller output"])
            infs.append(inf_i.id)
        if unit:
            unit_conf = 0.35
            inf_u = ws.inferences.add(a, f"{a} probably belongs to the {unit} unit", status="uncertain", confidence=unit_conf, evidence_ids=[], reasoning="Cluster membership only", source="llm-local:gemma4", stage="profile")
            infs.append(inf_u.id)
        sig_objs.append(SignalDescriptor(
            id=a, source_column=c, column_index=list(df.columns).index(c), dtype="float32", structural_role=role, structural_confidence=inf_role.confidence,
            instrument_hypothesis=instr, instrument_confidence=instr_conf, unit_operation_hypothesis=unit, unit_operation_confidence=unit_conf,
            cluster_id=cluster_of.get(a), related_signals=related, fingerprint=fp, units_hypothesis=None, confidence=_r((inf_role.confidence + instr_conf) / 2 if instr else inf_role.confidence),
            inference_ids=infs, evidence_ids=evs, excluded=role in ("constant", "derived_redundant"), excluded_reason={"constant": "constant column", "derived_redundant": "exact function of S01 + S05"}.get(role),
        ))
    ws.write_json("signals", sig_objs)
    ws.write_json("relations", {
        "signals": [alias[c] for c in signal_cols],
        "correlation": [[_r(v, 3) for v in row] for row in corr.to_numpy().tolist()],
        "lags": all_related,
        "clusters": [{"id": cid, "signals": [alias[s] for s in ss], "description": {"C1": "feed / reactor thermal group", "C2": "separator hydraulics", "C3": "manipulated variables"}[cid]} for cid, ss in clusters.items()],
        "method": "pearson + lagged cross-correlation (max lag 12)",
        "n_samples": n_rows,
    })
    ws.write_json("domain", {"likelihood": schema.domain_likelihood, "hint": None, "statement": "Regularly sampled, autocorrelated continuous channels with actuator-like plateaus: a process sensor stream. Instrument and unit hypotheses are guesses with low confidence.", "inference_ids": [inf_sensor.id], "evidence_ids": ev_schema[1:]})
    ws.write_json("understanding.json", {
        "summary": f"{n_rows} rows, {len(signal_cols)} signals in {n_groups} groups sampled every 180 s. Nine continuous measurements, two actuator-like manipulated variables, one sample-and-hold analyzer, one constant and one derived sum. Three correlation clusters.",
        "assumptions": schema.assumptions,
        "uncertain": ["Instrument types are hypotheses (confidence 0.40 to 0.70); confirm before relying on them", "Unit-operation membership is based on cluster structure only", "Whether groups are consecutive batches or parallel lines is unknown"],
        "hypotheses": [{"inference_id": i, "subject": ws.inferences.get(i).subject, "claim": ws.inferences.get(i).claim, "confidence": ws.inferences.get(i).confidence, "status": ws.inferences.get(i).status} for i in [inf_sensor.id, inf_group.id, inf_period.id, inf_units.id]],
        "inference_ids": schema.inference_ids,
        "clusters": clusters,
    })
    ws.log.record("system:profile", "stage", "stage", "profile", {"state": "done", "n_signals": len(sig_objs)})
    for inf in ws.inferences.all():
        ws.log.record("system:profile", "inference", "inference", inf.id, {"claim": inf.claim, "confidence": inf.confidence, "status": inf.status}, inf.evidence_ids)

    # ---------- batches / checks / trust ----------
    batches = []
    bid = 0
    group_rows = data.groupby("__group__", sort=False)["__row__"].agg(["min", "max"])
    for g, (r0, r1) in group_rows.iterrows():
        mid = int((r0 + r1) // 2)
        for a0, a1 in ((int(r0), mid), (mid + 1, int(r1))):
            bid += 1
            b = f"B{bid:04d}"
            batches.append({"batch_id": b, "group_id": str(g), "row_start": a0, "row_end": a1, "n_rows": a1 - a0 + 1, "time_start": str(data.loc[a0, "timestamp"]), "time_end": str(data.loc[a1, "timestamp"])})
    # batches.json is a plain list in tpm.ingest.stream.build_batches format (row_end EXCLUSIVE there);
    # the in-memory `batches` list keeps inclusive ends for the checks/flags built below.
    ws.write_json("batches", [{"batch_id": b["batch_id"], "row_start": b["row_start"], "row_end": b["row_end"] + 1, "n_rows": b["n_rows"], "group_ids": [b["group_id"]], "time_start": b["time_start"], "time_end": b["time_end"], "method": "rows"} for b in batches])

    def batch_for_row(r: int) -> dict[str, Any]:
        for b in batches:
            if b["row_start"] <= r <= b["row_end"]:
                return b
        return batches[-1]

    dq_by_batch: dict[str, list[dict[str, Any]]] = {}
    for dq in truth["dq"]:
        b = batch_for_row(dq["row_start"])
        dq_by_batch.setdefault(b["batch_id"], []).append(dq)

    # rules (one active, one draft, one rejected)
    rule_active = Rule(id="RULE-001", text=f"{alias['temp_r']} must stay between 100 and 125.", author="human:Maija(engineer)", status="active", compiled={"type": "range", "signal": alias["temp_r"], "min": 100, "max": 125, "severity": 0.6}, compile_source="template", compile_explanation=f"Range check on {alias['temp_r']}: fail when a sample is below 100 or above 125.", compile_confidence=0.95)
    rule_draft = Rule(id="RULE-002", text=f"{alias['press_r']} must not change by more than 40 per sample.", author="human:Maija(engineer)", status="draft", compiled={"type": "rate_of_change", "signal": alias["press_r"], "max_abs_delta": 40, "per_samples": 1, "severity": 0.5}, compile_source="llm-local:gemma4", compile_explanation=f"Rate-of-change check: the absolute difference between consecutive {alias['press_r']} samples must not exceed 40.", compile_confidence=0.82)
    rule_rejected = Rule(id="RULE-003", text="The reactor must never overheat.", author="human:Olli(operator)", status="rejected", compiled=None, compile_source="template", compile_explanation="No signal alias and no threshold could be identified. Name a signal (for example S03) and a limit.", compile_confidence=0.1)
    ws.write_json("rules", [rule_active, rule_draft, rule_rejected])
    ws.log.record("human:Maija(engineer)", "approve_rule", "rule", "RULE-001", {"note": "standard operating envelope"})
    ws.log.record("human:Maija(engineer)", "reject_rule", "rule", "RULE-003", {"note": "too vague"})

    checks: list[CheckResult] = []
    trusts: list[TrustVerdict] = []
    cid = 0
    for b in batches:
        bchecks: list[CheckResult] = []
        issues = dq_by_batch.get(b["batch_id"], [])
        issue_types = {d["type"] for d in issues}

        def add_check(ctype: str, cat: str, status: str, sev: float, stmt: str, sigs: list[str], values: dict[str, Any], rule_id: Optional[str] = None, rs: Optional[int] = None, re_: Optional[int] = None) -> None:
            nonlocal cid
            cid += 1
            ev_id = ws.evidence.add(ctype, stmt, signals=sigs, values=values, computed_by=f"quality.checks.{ctype}", n_samples=b["n_rows"], group_id=b["group_id"], batch_id=b["batch_id"]).id
            bchecks.append(CheckResult(check_id=f"CHK-{cid:06d}", check_type=ctype, category=cat, signals=sigs, batch_id=b["batch_id"], group_id=b["group_id"], status=status, severity=sev, statement=stmt, evidence_ids=[ev_id], rule_id=rule_id, values=values, row_start=rs if rs is not None else b["row_start"], row_end=re_ if re_ is not None else b["row_end"]))

        if "missing_block" in issue_types:
            d = next(x for x in issues if x["type"] == "missing_block")
            add_check("missing", "completeness", "fail", 0.7, f"{alias[d['signal']]} is missing in 61 consecutive rows ({_r(61 / b['n_rows'] * 100, 1)} % of the batch)", [alias[d["signal"]]], {"missing_run": 61, "missing_fraction": _r(61 / b["n_rows"], 3)}, rs=d["row_start"], re_=d["row_end"])
        else:
            add_check("missing", "completeness", "pass", 0.0, "No missing values in any signal", [], {"missing_fraction": 0.0})
        if "spike_out_of_range" in issue_types:
            d = next(x for x in issues if x["type"] == "spike_out_of_range")
            add_check("out_of_range", "validity", "fail", 0.9, f"{alias[d['signal']]} = 99999 at row {d['row_start']}: 38 000 robust standard deviations above the median", [alias[d["signal"]]], {"value": 99999.0, "robust_z": 38000}, rs=d["row_start"], re_=d["row_end"])
        else:
            add_check("out_of_range", "validity", "pass", 0.0, "All values within 6 robust standard deviations", [], {"max_robust_z": _r(np.random.default_rng(bid).uniform(2.5, 4.5), 2)})
        if "frozen_block" in issue_types:
            d = next(x for x in issues if x["type"] == "frozen_block")
            add_check("stuck", "validity", "fail", 0.8, f"{alias[d['signal']]} holds exactly the same value for 81 consecutive samples (a continuously varying signal): dead sensor or frozen transmitter", [alias[d["signal"]]], {"run_length": 81, "value": _r(data.loc[d["row_start"], alias[d["signal"]]])}, rs=d["row_start"], re_=d["row_end"])
        else:
            add_check("stuck", "validity", "pass", 0.0, "No continuous signal is stuck longer than 30 samples", [], {"max_run": 6})
        if "unit_shift" in issue_types:
            d = next(x for x in issues if x["type"] == "unit_shift")
            add_check("unit_shift", "consistency", "fail", 0.85, f"{alias[d['signal']]} jumps by a factor of about 1000 for 51 rows and returns: a units or scaling change, not a process event", [alias[d["signal"]]], {"factor": 1000, "n_rows": 51}, rs=d["row_start"], re_=d["row_end"])
        else:
            add_check("unit_shift", "consistency", "pass", 0.0, "No scale jumps detected", [], {})
        if "duplicate_rows" in issue_types:
            d = next(x for x in issues if x["type"] == "duplicate_rows")
            add_check("duplicate", "consistency", "warn", 0.4, "5 rows are exact duplicates of the preceding 5 rows", [], {"n_duplicates": 5}, rs=d["row_start"], re_=d["row_end"])
        else:
            add_check("duplicate", "consistency", "pass", 0.0, "No duplicate rows", [], {"n_duplicates": 0})
        if "timestamp_gap" in issue_types:
            d = next(x for x in issues if x["type"] == "timestamp_gap")
            add_check("gap", "timeliness", "warn", 0.5, "Timestamp jumps forward by 45 minutes (15 expected samples missing)", ["timestamp"], {"gap_s": 2700, "expected_period_s": 180}, rs=d["row_start"], re_=d["row_start"])
        else:
            add_check("gap", "timeliness", "pass", 0.0, "Timestamps advance by 180 s throughout", ["timestamp"], {"max_gap_s": 180})
        # rule check
        tr = data.loc[b["row_start"]: b["row_end"], alias["temp_r"]]
        viol = int(((tr < 100) | (tr > 125)).sum())
        add_check("rule:RULE-001", "rule", "fail" if viol > 0 else "pass", 0.6 if viol else 0.0, f"RULE-001: {alias['temp_r']} outside [100, 125] in {viol} rows" if viol else f"RULE-001: {alias['temp_r']} within [100, 125] in every row", [alias["temp_r"]], {"violations": viol}, rule_id="RULE-001")
        checks.extend(bchecks)
        fails = [c for c in bchecks if c.status == "fail"]
        warns = [c for c in bchecks if c.status == "warn"]
        score = max(0.0, 1.0 - sum(c.severity for c in fails) * 0.6 - sum(c.severity for c in warns) * 0.15)
        untrusted = sorted({s for c in fails for s in c.signals})
        trusted = score >= settings.quality.trust_fail_threshold
        reasons = [c.statement for c in fails + warns]
        stmt = "Data can be trusted for monitoring and detection" if trusted and not warns and not fails else (
            f"Data cannot be trusted for {', '.join(untrusted)} in this batch: " + "; ".join(c.check_type.replace('_', ' ') for c in fails) if not trusted else f"Data is usable with caution ({', '.join(c.check_type for c in fails + warns)})")
        trusts.append(TrustVerdict(batch_id=b["batch_id"], trusted=trusted, trust_score=_r(score, 2), untrusted_signals=untrusted, reasons=reasons, check_ids=[c.check_id for c in fails + warns], statement=stmt))
    ws.rewrite_jsonl("checks", checks)
    ws.rewrite_jsonl("trust", trusts)
    for c in checks:
        if c.status != "pass":
            ws.log.record("system:quality", "check", "check", c.check_id, {"status": c.status, "severity": c.severity, "batch_id": c.batch_id}, c.evidence_ids)
    for t in trusts:
        if not t.trusted:
            ws.log.record("system:quality", "trust", "trust", t.batch_id, {"trust_score": t.trust_score, "statement": t.statement}, [])
    ws.log.record("system:quality", "stage", "stage", "quality", {"state": "done", "n_checks": len(checks)})

    # ---------- baseline / detect_meta / scores / flags / patterns ----------
    rng = np.random.default_rng(seed + 7)
    aliases = [alias[c] for c in signal_cols]
    detect_sigs = [a for a, s in zip(aliases, sig_objs) if not s.excluded]
    score = np.abs(rng.normal(0, 0.18, n_rows)) + 0.25
    contrib = np.full((n_rows, len(detect_sigs)), 1.0 / len(detect_sigs), dtype=np.float32)
    contrib += rng.uniform(-0.02, 0.02, contrib.shape).astype(np.float32)
    threshold = 1.0
    flags: list[Flag] = []
    fid = 0
    trust_by_batch = {t.batch_id: t for t in trusts}

    def trust_ctx(b: dict[str, Any]) -> dict[str, Any]:
        t = trust_by_batch[b["batch_id"]]
        return {"batch_id": t.batch_id, "trusted": t.trusted, "trust_score": t.trust_score, "untrusted_signals": t.untrusted_signals}

    onset_pattern_events: dict[str, list[str]] = {}
    for g, info in truth["groups"].items():
        if not info["fault"]:
            continue
        gid = str(int(g) + 1)
        r0 = int(group_rows.loc[gid, "min"])
        r1 = int(group_rows.loc[gid, "max"])
        onset = r0 + int(info["onset"])
        ftype = info["fault"]
        sigs = [alias[s] for s in FAULT_SIGNALS[ftype]]
        m = np.arange(onset, r1 + 1)
        if ftype == "ramp":
            score[m] += np.linspace(0.2, 3.2, len(m))
        elif ftype == "oscillation":
            score[m] += 1.4 + 0.9 * np.abs(np.sin(2 * np.pi * (m - onset) / 12))
        elif ftype == "noise_burst":
            score[m] += np.abs(rng.normal(1.6, 0.8, len(m)))
        else:
            score[m] += 2.2 + rng.normal(0, 0.25, len(m))
        shares = np.array([0.5, 0.3, 0.2][: len(sigs)])
        shares = shares / shares.sum()
        contrib[m, :] *= 0.15
        for s_, sh in zip(sigs, shares):
            contrib[m, detect_sigs.index(s_)] += 0.85 * sh
        fid += 1
        b = batch_for_row(onset)
        ev_ids = []
        ev_ids.append(ws.evidence.add("changepoint", f"Group {gid}: ensemble score crosses the threshold {threshold} at row {onset} and stays above it in {_r((r1 - onset + 1) / (r1 - r0 + 1) * 100, 0)} % of the remaining rows", signals=sigs, values={"onset_row": onset, "peak": _r(float(score[m].max()))}, computed_by="detect.changepoints.onset", n_samples=r1 - r0 + 1, group_id=gid, batch_id=b["batch_id"]).id)
        for s_, sh in zip(sigs, shares):
            ev_ids.append(ws.evidence.add("contribution", f"{s_} accounts for {_r(sh * 85, 0)} % of the score after the onset (direction: {'up' if ftype in ('step', 'ramp') else 'noisy' if ftype == 'noise_burst' else 'shifted' if ftype != 'stuck_sensor' else 'stuck'})", signals=[s_], values={"share": _r(sh * 0.85)}, computed_by="detect.attribution", n_samples=len(m), group_id=gid, batch_id=b["batch_id"]).id)
        if ftype == "corr_break":
            ev_ids.append(ws.evidence.add("correlation", f"{alias['flow_b']} loses its correlation with {alias['level_s']} (r 0.71 -> 0.04) while every other pair keeps its structure", signals=[alias["flow_b"], alias["level_s"]], values={"r_before": 0.71, "r_after": 0.04}, computed_by="detect.corr_break", n_samples=len(m), group_id=gid).id)
        if ftype == "stuck_sensor":
            ev_ids.append(ws.evidence.add("stuck", f"{alias['level_s']} is constant from row {onset} to the end of the group while its correlated partners keep moving", signals=[alias["level_s"]], values={"run_length": int(r1 - onset + 1)}, computed_by="quality.checks.stuck", n_samples=len(m), group_id=gid).id)
        kind = FAULT_KIND[ftype]
        cause = FAULT_CAUSE[ftype]
        pat = FAULT_PATTERN[ftype]
        direction = {"step": "up", "ramp": "up", "stuck_sensor": "stuck", "corr_break": "shifted", "oscillation": "noisy", "noise_burst": "noisy"}[ftype]
        stmt = {
            "step": f"Sudden level shift in {', '.join(sigs)} starting at row {onset} (group {gid}); several correlated signals move together, which points to the process rather than a sensor.",
            "ramp": f"Gradual upward drift in {', '.join(sigs)} from row {onset} (group {gid}); slope increases toward the end of the group.",
            "stuck_sensor": f"{sigs[0]} stops changing at row {onset} (group {gid}) while related signals keep moving: most likely a frozen sensor, not a process change.",
            "corr_break": f"{sigs[0]} breaks its usual relation with the separator signals from row {onset} (group {gid}) while its own range looks normal: sensor or data issue is more likely than a process change.",
            "oscillation": f"Periodic oscillation (period about 12 samples) appears in {', '.join(sigs)} from row {onset} (group {gid}); likely a control loop cycling.",
            "noise_burst": f"Noise level in {', '.join(sigs)} rises several-fold from row {onset} (group {gid}); could be electrical noise on the sensors or an unstable process.",
        }[ftype]
        conf = {"step": 0.88, "ramp": 0.8, "stuck_sensor": 0.9, "corr_break": 0.66, "oscillation": 0.78, "noise_burst": 0.58}[ftype]
        sev = _r(min(1.0, 0.45 + 0.12 * float(score[m].max())), 2)
        flag = Flag(
            id=f"FLAG-{fid:06d}", kind=kind, batch_id=b["batch_id"], group_id=gid, row_start=onset, row_end=r1,
            time_start=str(data.loc[onset, "timestamp"]), time_end=str(data.loc[r1, "timestamp"]), severity=sev, score=_r(float(score[m].max())), threshold=threshold,
            detector="ensemble(pca,robust_z,ewma,corr_break,iforest)" if ftype != "corr_break" else "corr_break", statement=stmt,
            signals_ranked=[SignalContribution(signal=s_, contribution=_r(sh * 0.85), direction=direction, lag=lag_truth.get((sigs[0], s_), 0), explanation=f"{s_} contributes {_r(sh * 85, 0)} % of the anomaly score", evidence_ids=[ev_ids[1 + k]]) for k, (s_, sh) in enumerate(zip(sigs, shares))],
            evidence_ids=ev_ids, likely_cause_class=cause, confidence=conf, pattern_id=pat, trust_context=trust_ctx(b),
        )
        flags.append(flag)
        if pat:
            onset_pattern_events.setdefault(pat, []).append(gid)
        # cascade flag for step: propagation between clusters
        if ftype == "step":
            fid += 1
            ev_c = ws.evidence.add("lag", f"Onset order in group {gid}: {alias['flow_a']} (row {onset}) -> {alias['press_r']} (+2) -> {alias['temp_r']} (+4) -> {alias['temp_s']} (+6)", signals=[alias["flow_a"], alias["press_r"], alias["temp_r"], alias["temp_s"]], values={"lags": [0, 2, 4, 6]}, computed_by="detect.cascade", n_samples=len(m), group_id=gid).id
            flags.append(Flag(id=f"FLAG-{fid:06d}", kind="cascade", batch_id=b["batch_id"], group_id=gid, row_start=onset, row_end=onset + 8, time_start=str(data.loc[onset, "timestamp"]), time_end=str(data.loc[onset + 8, "timestamp"]), severity=_r(sev * 0.9, 2), score=_r(float(score[m].max())), threshold=threshold, detector="cascade", statement=f"The disturbance propagates from {alias['flow_a']} through {alias['press_r']} and {alias['temp_r']} to {alias['temp_s']} with lags of 2, 4 and 6 samples (group {gid}).", signals_ranked=[SignalContribution(signal=alias[s_], contribution=_r(0.4 - 0.1 * k), direction="up", lag=[0, 2, 4, 6][k], explanation=f"moves {[0, 2, 4, 6][k]} samples after {alias['flow_a']}", evidence_ids=[ev_c]) for k, s_ in enumerate(["flow_a", "press_r", "temp_r", "temp_s"])], evidence_ids=[ev_c], likely_cause_class="process", confidence=0.74, pattern_id=pat, trust_context=trust_ctx(b)))

    # DQ flags from checks
    for c in checks:
        if c.status == "fail" and c.category != "rule":
            fid += 1
            b = next(x for x in batches if x["batch_id"] == c.batch_id)
            flags.append(Flag(id=f"FLAG-{fid:06d}", kind="dq", batch_id=c.batch_id, group_id=c.group_id, row_start=c.row_start or b["row_start"], row_end=c.row_end or b["row_end"], time_start=str(data.loc[c.row_start or b["row_start"], "timestamp"]), time_end=str(data.loc[c.row_end or b["row_end"], "timestamp"]), severity=c.severity, score=c.severity, threshold=None, detector=f"quality.{c.check_type}", statement=c.statement, signals_ranked=[SignalContribution(signal=s_, contribution=1.0, direction="stuck" if c.check_type == "stuck" else "shifted", explanation=c.statement, evidence_ids=c.evidence_ids) for s_ in c.signals], evidence_ids=c.evidence_ids, likely_cause_class="data" if c.check_type in ("unit_shift", "duplicate", "gap", "missing") else "sensor", confidence=0.9, trust_context=trust_ctx(b)))
        elif c.status == "fail" and c.category == "rule":
            fid += 1
            b = next(x for x in batches if x["batch_id"] == c.batch_id)
            flags.append(Flag(id=f"FLAG-{fid:06d}", kind="rule", batch_id=c.batch_id, group_id=c.group_id, row_start=b["row_start"], row_end=b["row_end"], time_start=b["time_start"], time_end=b["time_end"], severity=c.severity, score=float(c.values.get("violations", 1)), threshold=0, detector="rule:RULE-001", statement=c.statement, signals_ranked=[SignalContribution(signal=s_, contribution=1.0, direction="up", explanation="outside the approved envelope", evidence_ids=c.evidence_ids) for s_ in c.signals], evidence_ids=c.evidence_ids, likely_cause_class="process", confidence=0.95, trust_context=trust_ctx(b)))
    flags.sort(key=lambda f: f.row_start)
    ws.rewrite_jsonl("flags", flags)
    for f in flags:
        ws.log.record("system:detect", "flag", "flag", f.id, {"kind": f.kind, "severity": f.severity, "group_id": f.group_id, "statement": f.statement[:200]}, f.evidence_ids)

    # scores.parquet
    scores = pd.DataFrame({"__row__": np.arange(n_rows, dtype=np.int64), "__group__": data["__group__"].to_numpy(), "score": score.astype(np.float32), "threshold": np.full(n_rows, threshold, dtype=np.float32)})
    scores["batch_id"] = [batch_for_row(r)["batch_id"] for r in range(n_rows)]
    contrib = contrib / contrib.sum(axis=1, keepdims=True)
    for k, s_ in enumerate(detect_sigs):
        scores[f"c_{s_}"] = contrib[:, k]
    for dname, fac in (("pca", 1.0), ("robust_z", 0.8), ("ewma", 0.9), ("cusum", 0.7), ("corr_break", 0.6), ("iforest", 0.85)):
        scores[f"d_{dname}"] = (score * fac + rng.normal(0, 0.1, n_rows)).astype(np.float32)
    scores.to_parquet(ws.path("scores"), index=False)

    patterns = [
        FaultPattern(id="PATTERN-A", name=None, signature={"signals": [alias["flow_a"], alias["press_r"], alias["temp_r"]], "directions": ["up", "up", "up"], "lag_order": [alias["flow_a"], alias["press_r"], alias["temp_r"]], "shape": "step"}, n_events=len(onset_pattern_events.get("PATTERN-A", [])), groups_affected=onset_pattern_events.get("PATTERN-A", []), description="Simultaneous upward step in the feed-flow / reactor-pressure / reactor-temperature trio; propagates with 2 to 6 sample lags.", confidence=0.86, evidence_ids=[f.evidence_ids[0] for f in flags if f.pattern_id == "PATTERN-A" and f.kind != "cascade"], classifier_reliability=0.91),
        FaultPattern(id="PATTERN-B", name=None, signature={"signals": [alias["press_r"], alias["temp_r"], alias["temp_s"]], "directions": ["up", "up", "up"], "shape": "ramp"}, n_events=len(onset_pattern_events.get("PATTERN-B", [])), groups_affected=onset_pattern_events.get("PATTERN-B", []), description="Slow, accelerating rise in pressure and both temperatures over the second half of a group.", confidence=0.79, evidence_ids=[f.evidence_ids[0] for f in flags if f.pattern_id == "PATTERN-B" and f.kind != "cascade"], classifier_reliability=0.84),
        FaultPattern(id="PATTERN-C", name=None, signature={"signals": [alias["press_r"], alias["level_s"]], "directions": ["noisy", "noisy"], "shape": "oscillation", "period": 12}, n_events=len(onset_pattern_events.get("PATTERN-C", [])), groups_affected=onset_pattern_events.get("PATTERN-C", []), description="Pressure and separator level oscillate together with a 12-sample period.", confidence=0.77, evidence_ids=[f.evidence_ids[0] for f in flags if f.pattern_id == "PATTERN-C" and f.kind != "cascade"], classifier_reliability=0.8),
        FaultPattern(id="PATTERN-D", name=None, signature={"signals": [alias["temp_r"], alias["flow_a"]], "directions": ["noisy", "noisy"], "shape": "noise_burst"}, n_events=len(onset_pattern_events.get("PATTERN-D", [])), groups_affected=onset_pattern_events.get("PATTERN-D", []), description="Noise variance rises several-fold in reactor temperature and feed flow without a mean shift.", confidence=0.6, evidence_ids=[f.evidence_ids[0] for f in flags if f.pattern_id == "PATTERN-D" and f.kind != "cascade"], classifier_reliability=0.66),
    ]
    ws.write_json("patterns", patterns)
    ev_base = ws.evidence.add("baseline", f"Rows {r0 if False else 0}-{n_samples // 3} of every group form the densest, most stable regime (per-signal modes agree in {len(detect_sigs)}/{len(detect_sigs)} signals)", values={"share_rows": 0.34}, computed_by="detect.baseline.consensus_of_modes", n_samples=n_rows).id
    inf_base = ws.inferences.add("dataset", "The first third of every group represents normal operation", status="assumed", confidence=0.72, evidence_ids=[ev_base], reasoning="Consensus of per-signal density modes; pre-change-point segments agree", stage="detect", alternatives=["robust covariance trimming (agreement 0.81)", "operator-provided reference period"])
    ws.write_json("baseline", {"method": "consensus_of_modes", "chosen": "dominant_stable_regime", "confidence": 0.72, "inference_id": inf_base.id, "evidence_ids": [ev_base], "candidates": [{"method": "consensus_of_modes", "score": 0.88, "selected": True}, {"method": "pre_changepoint_segments", "score": 0.84, "selected": False}, {"method": "densest_window_cluster", "score": 0.79, "selected": False}, {"method": "robust_covariance", "score": 0.81, "selected": False}], "assumptions": ["Normal operation is the most common state", "Every group starts in a normal state"], "reference_rows_per_group": [0, n_samples // 3], "operator_reference": None})
    ws.write_json("detect_meta", {"detectors": [
        {"name": "pca", "selected": True, "weight": 0.28, "stability": 0.93, "agreement": 0.88, "fit_seconds": 3.1},
        {"name": "robust_z", "selected": True, "weight": 0.2, "stability": 0.97, "agreement": 0.8, "fit_seconds": 0.4},
        {"name": "ewma", "selected": True, "weight": 0.16, "stability": 0.9, "agreement": 0.76, "fit_seconds": 0.5},
        {"name": "cusum", "selected": False, "weight": 0.0, "stability": 0.61, "agreement": 0.55, "fit_seconds": 0.6, "reason": "unstable across folds"},
        {"name": "corr_break", "selected": True, "weight": 0.18, "stability": 0.86, "agreement": 0.7, "fit_seconds": 2.2},
        {"name": "iforest", "selected": True, "weight": 0.18, "stability": 0.84, "agreement": 0.79, "fit_seconds": 9.8},
        {"name": "autoencoder", "selected": False, "weight": 0.0, "stability": 0.7, "agreement": 0.74, "fit_seconds": 31.0, "reason": "over time budget for this file"},
    ], "n_folds": 5, "fold_method": "GroupKFold over detected groups + leakage guard", "threshold": threshold, "threshold_method": "99th percentile of out-of-fold baseline scores", "fit_rows": n_rows, "elapsed_s": 48.5, "window": 20, "leakage_guard": {"enabled": True, "near_duplicate_groups_merged": 0}})
    ws.write_json("evaluation", {"available": False, "reason": "no label columns in this file; evaluation runs only when labels exist"})
    ws.log.record("system:detect", "stage", "stage", "detect", {"state": "done", "n_flags": len(flags)})

    # ---------- diagnoses ----------
    diags: list[Diagnosis] = []
    did = 0
    for f in flags:
        if f.kind in ("dq", "rule", "cascade"):
            continue
        did += 1
        sigs = [s.signal for s in f.signals_ranked]
        prop = []
        if f.pattern_id == "PATTERN-A":
            chain = [alias["flow_a"], alias["press_r"], alias["temp_r"], alias["temp_s"]]
            for k in range(len(chain) - 1):
                prop.append(PropagationStep(from_signal=chain[k], to_signal=chain[k + 1], lag=[2, 2, 2][k], strength=_r(0.8 - 0.15 * k), explanation=f"{chain[k + 1]} follows {chain[k]} about {[2, 2, 2][k]} samples later (6 min)", evidence_ids=[f.evidence_ids[0]]))
        elif f.pattern_id == "PATTERN-B":
            prop.append(PropagationStep(from_signal=alias["press_r"], to_signal=alias["temp_r"], lag=1, strength=0.6, explanation="temperature rises shortly after pressure", evidence_ids=[f.evidence_ids[0]]))
            prop.append(PropagationStep(from_signal=alias["temp_r"], to_signal=alias["temp_s"], lag=3, strength=0.5, explanation="separator temperature lags the reactor by 3 samples", evidence_ids=[f.evidence_ids[0]]))
        elif f.pattern_id == "PATTERN-C":
            prop.append(PropagationStep(from_signal=alias["press_r"], to_signal=alias["level_s"], lag=1, strength=0.55, explanation="level oscillation lags the pressure oscillation by 1 sample", evidence_ids=[f.evidence_ids[0]]))
        cause_reason = {
            "process": "Several correlated signals move together and keep their mutual relations, so the disturbance is in the process, not in a single instrument.",
            "sensor": "One signal breaks its usual relation to its partners while the partners stay consistent with each other: the instrument, not the process, is the likely cause.",
            "mixed": "The noise rise appears in two signals that are only weakly related; both a process instability and a shared electrical disturbance remain plausible.",
            "data": "The change is a pure data artefact (scale, duplicates or gaps) rather than a physical event.",
            "unknown": "The evidence does not favour a process or a sensor explanation.",
        }[f.likely_cause_class]
        steps = [
            f"At row {f.row_start} (group {f.group_id}) the anomaly score rose from about 0.4 to {f.score}, above the threshold {f.threshold}.",
            f"The score is driven by {', '.join(sigs)}; {sigs[0]} alone explains {_r(f.signals_ranked[0].contribution * 100, 0)} % of it.",
            cause_reason,
        ]
        if prop:
            steps.append("The disturbance moves in a fixed order: " + " then ".join(f"{p.from_signal} to {p.to_signal} (+{p.lag})" for p in prop) + ". This order matches the lags learned during normal operation.")
        steps.append({"process": "Check the process upstream of " + sigs[0] + " first (feed, setpoints, utilities).", "sensor": f"Inspect the instrument behind {sigs[0]} (wiring, transmitter, freeze) before acting on the process.", "mixed": "Compare with the operator log; if nothing changed in the process, check the sensor wiring.", "data": "Fix the data path (units, duplicates, gaps) and re-run; do not act on the process.", "unknown": "Gather more context before acting."}[f.likely_cause_class])
        objections = []
        checks_ = [{"name": "onset_inside_group", "passed": True, "detail": "onset is not at a group boundary"}, {"name": "not_explained_by_dq", "passed": f.trust_context.get("trusted", True) if f.trust_context else True, "detail": "batch trust score " + str(f.trust_context.get("trust_score") if f.trust_context else "n/a")}, {"name": "detector_agreement", "passed": f.confidence > 0.6, "detail": f"{4 if f.confidence > 0.75 else 3} of 5 detectors agree"}]
        if f.likely_cause_class == "mixed":
            objections.append("A noise burst can also be produced by a loose connection; the process explanation is not unique.")
        if f.trust_context and not f.trust_context.get("trusted", True):
            objections.append("The batch has data-quality failures; part of the score may be a data artefact.")
        if f.confidence < 0.7:
            objections.append("Only 3 of 5 detectors agree; the event may be a transient.")
        verdict = "supported" if not objections else ("weakened" if len(objections) == 1 else "rejected")
        adj = _r(f.confidence - 0.08 * len(objections), 2)
        d = Diagnosis(
            id=f"DIAG-{did:06d}", flag_ids=[f.id] + [c.id for c in flags if c.kind == "cascade" and c.group_id == f.group_id], group_id=f.group_id, pattern_id=f.pattern_id,
            fault_type=(f.pattern_id + " (unnamed)") if f.pattern_id else ("sensor fault" if f.likely_cause_class == "sensor" else "unclassified event"), cause_class=f.likely_cause_class,
            ranked_signals=f.signals_ranked, propagation=prop, steps=steps, summary=f.statement, confidence=adj, uncertainty=["Exact physical cause unknown without process context", "Instrument roles are hypotheses"] + (["Unit scale unknown"] if f.kind == "drift" else []), assumptions=["First third of every group is normal operation", "Lags learned from normal data still apply"],
            evidence_ids=f.evidence_ids, critique=Critique(verdict=verdict, objections=objections, checks=checks_, adjusted_confidence=adj, source="template"), narrative_source="template",
        )
        diags.append(d)
        ws.log.record("system:diagnose", "diagnosis", "diagnosis", d.id, {"fault_type": d.fault_type, "cause_class": d.cause_class, "confidence": d.confidence}, d.evidence_ids)
    ws.rewrite_jsonl("diagnoses", diags)
    ws.log.record("system:diagnose", "stage", "stage", "diagnose", {"state": "done", "n_diagnoses": len(diags)})

    # ---------- assessor ----------
    ev_lc = ws.evidence.add("learning_curve", "Out-of-fold detection stability rises from 0.61 at 10 % of the groups to 0.87 at 100 %, still climbing", values={"fractions": [0.1, 0.2, 0.4, 0.7, 1.0], "scores": [0.61, 0.7, 0.79, 0.85, 0.87]}, computed_by="assessor.learning_curve", n_samples=n_rows).id
    ev_cov = ws.evidence.add("coverage", "Only 2 of 12 groups cover the low-load regime (S07 below 40); the other regimes have 4 to 6 groups each", values={"low_load_groups": 2}, computed_by="assessor.coverage", n_samples=n_rows).id
    ws.write_json("assessor", {
        "score": 0.71,
        "components": {"ml_fitness": 0.74, "coverage": 0.58, "data_quality": 0.82, "label_availability": 0.0},
        "verdict": "The data is fit for unsupervised monitoring. Coverage of the low-load regime is thin; more data would help there and in separating pattern D from noise.",
        "learning_curve": [{"fraction": fr, "score": sc, "std": sd, "n_groups": int(round(fr * n_groups))} for fr, sc, sd in zip([0.1, 0.2, 0.4, 0.7, 1.0], [0.61, 0.7, 0.79, 0.85, 0.87], [0.09, 0.07, 0.05, 0.03, 0.02])],
        "coverage_by_regime": [{"regime": "low load", "n_groups": 2, "rows": 2 * n_samples, "coverage": 0.35}, {"regime": "nominal", "n_groups": 6, "rows": 6 * n_samples, "coverage": 0.92}, {"regime": "high load", "n_groups": 4, "rows": 4 * n_samples, "coverage": 0.78}],
        "dq_scores": {"completeness": 0.97, "validity": 0.9, "consistency": 0.88, "timeliness": 0.95},
        "would_more_data_help": {"answer": "yes, moderately", "expected_gain": 0.05, "confidence": 0.7, "evidence_ids": [ev_lc, ev_cov], "explanation": "The learning curve has not flattened (0.85 -> 0.87 in the last step). Two more low-load groups would raise coverage more than ten nominal ones."},
        "recommendations": [
            {"id": "REC-001", "action": "Collect 3 more groups from the low-load regime", "rationale": "coverage 0.35 in that regime; learning curve still rising", "expected_gain": 0.05, "confidence": 0.7, "evidence_ids": [ev_lc, ev_cov], "status": "proposed", "applicable": False},
            {"id": "REC-002", "action": "Exclude S10 (constant) and S11 (derived) from detection", "rationale": "no information; already excluded automatically, confirm to persist", "expected_gain": 0.0, "confidence": 0.95, "evidence_ids": [], "status": "proposed", "applicable": True},
            {"id": "REC-003", "action": "Merge PATTERN-D into 'noise events' with a lower alert severity", "rationale": "classifier reliability 0.66; events are short", "expected_gain": 0.02, "confidence": 0.55, "evidence_ids": [], "status": "proposed", "applicable": True},
        ],
        "experiments": [{"id": "EXP-001", "question": "Does dropping S12 change detection stability?", "result": "stability 0.87 -> 0.86", "seconds": 14.2}],
        "evidence_ids": [ev_lc, ev_cov],
        "held_back_pool": {"fraction": 0.2, "n_groups": 2},
    })
    ws.log.record("system:assessor", "stage", "stage", "assess", {"state": "done", "score": 0.71})

    # ---------- egress ledger / chat / report ----------
    ws.append_jsonl("egress_ledger", EgressRecord(id="EGR-000001", task="sensor_hypotheses", purpose="instrument hypotheses for 12 signals", route="local", provider="ollama", model="gemma4:e4b-it-qat", artifact_types=["signals", "relations"], payload_bytes=18422, payload_hash=hashlib.sha256(b"local").hexdigest(), guard_result="n/a", latency_ms=8400, ok=True))
    ws.append_jsonl("egress_ledger", EgressRecord(id="EGR-000002", task="diagnosis_narrative", purpose="narrative for DIAG-000001", route="external", provider="anthropic", model="claude-sonnet-5", artifact_types=["diagnosis", "evidence"], payload_bytes=6120, payload_hash=hashlib.sha256(b"ext").hexdigest(), payload_preview='{"diagnosis": {"id": "DIAG-000001", "fault_type": "PATTERN-A (unnamed)", "ranked_signals": [{"signal": "S01", "contribution": 0.43}, ...', guard_result="blocked", guard_reason="profile no-egress does not allow external calls; fell back to local", latency_ms=None, ok=False, error="blocked by guard"))
    ws.log.record("llm:local:gemma4", "egress", "egress", "EGR-000001", {"route": "local", "task": "sensor_hypotheses"})
    ws.log.record("system:guard", "egress", "egress", "EGR-000002", {"route": "external", "guard_result": "blocked"})
    ws.append_jsonl("chat", {"ts": now_iso(), "role": "user", "actor": "Olli(operator)", "message": "Which sensor is broken in group 3?", "context": {"flag_id": flags[0].id if flags else None}})
    ws.append_jsonl("chat", {"ts": now_iso(), "role": "assistant", "actor": "template", "message": "Nothing points at a broken sensor in group 3: the flagged signals move together and keep their relations, which is what a process change looks like.", "source": "template", "evidence_ids": flags[0].evidence_ids[:2] if flags else []})
    report = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>TPM report {run_id}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;color:#1c2530}}h1{{font-weight:600}}table{{border-collapse:collapse}}td,th{{border-bottom:1px solid #ccd;padding:.3rem .6rem;text-align:left}}</style></head>
<body><h1>Trustworthy Process Monitor report</h1><p>Run {run_id}: {n_rows} rows, {len(signal_cols)} signals, {n_groups} groups. {len(flags)} flags, {len(diags)} diagnoses. Assessor score 0.71.</p>
<h2>Flags</h2><table><tr><th>Flag</th><th>Kind</th><th>Group</th><th>Severity</th><th>Statement</th></tr>{''.join(f'<tr><td>{f.id}</td><td>{f.kind}</td><td>{f.group_id}</td><td>{f.severity}</td><td>{f.statement}</td></tr>' for f in flags)}</table>
<p>(Fixture report; the production report is produced by tpm.report.)</p></body></html>"""
    (ws.dir / "report_en.html").write_text(report, encoding="utf-8")
    ws.log.record("system:report", "stage", "stage", "report", {"state": "done", "languages": ["en"]})
    ws.log.record("system:pipeline", "run_finished", "run", run_id, {"state": "done", "seconds": sum(secs)})
    return ws


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Write a fake TPM run workspace")
    ap.add_argument("run_id", nargs="?", default="run_demo_synth")
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--groups", type=int, default=12)
    ap.add_argument("--samples", type=int, default=200)
    a = ap.parse_args(argv)
    s = load_settings()
    if a.workspace:
        s.workspace_dir = a.workspace
    ws = build_fake_workspace(s, run_id=a.run_id, n_groups=a.groups, n_samples=a.samples)
    print(ws.dir)


if __name__ == "__main__":
    main()
