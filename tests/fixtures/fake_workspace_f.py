"""Build a complete fake run workspace from synthetic data (every artifact of ARCHITECTURE.md section 3).

Used by the report / export / CLI tests of agent F so they never depend on the other agents' stages.
Shapes follow tpm/contracts.py; artifacts whose shape is owned by other agents (relations.json, batches.json,
baseline.json, detect_meta.json, evaluation.json, assessor.json, scores.parquet) are written in a plausible
form and the report must render them defensively.

    from tests.fixtures.fake_workspace_f import build_fake_workspace
    ws = build_fake_workspace(tmp_path / "workspace")
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from tpm.config import Settings, load_settings
from tpm.contracts import (
    CheckResult,
    Critique,
    DatasetSchema,
    Diagnosis,
    EgressRecord,
    FaultPattern,
    Flag,
    GroupingCandidate,
    HumanDecision,
    PropagationStep,
    Rule,
    RunStatus,
    SignalContribution,
    SignalDescriptor,
    StageStatus,
    TrustVerdict,
    now_iso,
)
from tpm.workspace import Workspace
from tests.fixtures.synth import make_synthetic


def _settings(root: Path) -> Settings:
    s = load_settings()
    s.workspace_dir = str(root)
    return s


def build_fake_workspace(
    root: str | Path,
    run_id: str = "fake_run_f",
    n_groups: int = 6,
    n_samples: int = 150,
    seed: int = 3,
    with_egress: bool = True,
    with_evaluation: bool = True,
    with_human: bool = True,
    settings: Optional[Settings] = None,
) -> Workspace:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    settings = settings or _settings(root)
    ws = Workspace(run_id=run_id, settings=settings, root=root)

    df, truth = make_synthetic(n_groups=n_groups, n_samples=n_samples, seed=seed, with_timestamp=True, with_labels=with_evaluation)
    df["__group__"] = df["run"].astype(str)
    n_rows = len(df)
    signal_cols = truth["signal_columns"]
    alias = {c: f"S{i + 1:02d}" for i, c in enumerate(signal_cols)}

    # ---------- meta / status ----------
    source_path = str(root / "fake_source.csv")
    ws.write_json("meta", {"run_id": run_id, "source_path": source_path, "profile": settings.profile, "options": {"language": "en"}, "created_at": now_iso(), "settings_snapshot": settings.model_dump(exclude={"profiles"})})
    ws.log.record("system:pipeline", "run_started", "run", run_id, {"source_path": source_path, "profile": settings.profile})

    # ---------- dataset ----------
    df.to_parquet(ws.path("dataset"), index=False)

    # ---------- schema ----------
    ev_sched = ws.evidence.add("period", "Median timestamp step is 180 s over 99.7 % of rows", signals=[], values={"median_step_s": 180, "share": 0.997}, computed_by="ingest.schema.sample_period", n_samples=n_rows)
    ev_group = ws.evidence.add("grouping", f"Column 0 is block-constant with {n_groups} blocks; column 1 resets to 1 at every block start", signals=[], values={"n_blocks": n_groups}, computed_by="ingest.schema.grouping", n_samples=n_rows)
    inf_group = ws.inferences.add("dataset", f"Rows form {n_groups} independent runs keyed by column 0", status="inferred", confidence=0.93, evidence_ids=[ev_sched.id, ev_group.id], reasoning="block-constant key column + counter reset", stage="ingest")
    schema = DatasetSchema(
        dataset_id="fake", source_path=source_path, format="csv", n_rows=n_rows, n_cols=df.shape[1] - 1, had_header=True, delimiter=",",
        columns=[c for c in df.columns if c != "__group__"], time_column="timestamp", sample_period_seconds=180.0, order_column="sample",
        group_columns=["run"], grouping_method="key_columns",
        grouping_candidates=[GroupingCandidate(method="key_columns", columns=["run"], n_groups=n_groups, score=0.93, rationale="block-constant"), GroupingCandidate(method="counter_reset", columns=["sample"], n_groups=n_groups, score=0.91, rationale="counter resets"), GroupingCandidate(method="none", n_groups=1, score=0.2, rationale="fallback")],
        label_columns=["fault_label"] if with_evaluation else [], meta_columns=["run", "sample"], signal_columns=signal_cols, signal_alias=alias,
        n_groups=n_groups, group_sizes={"min": n_samples, "median": n_samples, "max": n_samples + 5},
        domain_likelihood={"sensor_stream": 0.86, "business_records": 0.09, "log_records": 0.05},
        assumptions=["Sample period assumed constant at 180 s (inferred from timestamps)", "Groups assumed independent runs of the same process"],
        inference_ids=[inf_group.id], evidence_ids=[ev_sched.id, ev_group.id], options_used={"has_header": True},
    )
    ws.write_json("schema", schema)

    # ---------- signals ----------
    roles = truth["signal_roles"]
    inst = {"flow_a": ("flow", 0.62, "feed"), "press_r": ("pressure", 0.71, "reactor"), "temp_r": ("temperature", 0.66, "reactor"), "level_s": ("level", 0.58, "separator"), "flow_b": ("flow", 0.55, "feed"), "temp_s": ("temperature", 0.6, "separator"), "valve_1": ("valve", 0.8, None), "valve_2": ("valve", 0.8, None), "comp_a": ("composition", 0.64, "analyzer"), "const_c": (None, 0.0, None), "derived_sum": (None, 0.0, None), "power_c": ("power", 0.5, "compressor")}
    signals: list[SignalDescriptor] = []
    for i, c in enumerate(signal_cols):
        a = alias[c]
        col = pd.to_numeric(df[c], errors="coerce")
        fp = {"count": int(col.notna().sum()), "mean": float(col.mean()), "std": float(col.std()), "min": float(col.min()), "max": float(col.max()), "q01": float(col.quantile(0.01)), "q50": float(col.quantile(0.5)), "q99": float(col.quantile(0.99)), "noise_level": float(col.diff().std() / (col.std() + 1e-9)), "autocorr_1": float(col.autocorr(1)) if col.std() > 0 else 1.0, "stuck_fraction": float((col.diff() == 0).mean()), "quantization_step": 0.01, "missing_fraction": float(col.isna().mean())}
        ev1 = ws.evidence.add("distribution", f"{a}: mean {fp['mean']:.3g}, std {fp['std']:.3g}, autocorr(1) {fp['autocorr_1']:.2f}, stuck fraction {fp['stuck_fraction']:.2f}", signals=[a], values=fp, computed_by="profile.fingerprint", n_samples=fp["count"])
        role = roles.get(c, "unknown")
        if role == "label":
            continue
        conf = 0.9 if role in ("constant", "derived_redundant", "actuator_like") else 0.75
        inf1 = ws.inferences.add(a, f"{a} behaves like a {role.replace('_', ' ')} signal", status="inferred", confidence=conf, evidence_ids=[ev1.id], reasoning="fingerprint shape", stage="profile", alternatives=["held_sampled"] if role == "continuous_measured" else [])
        ih, ic, uo = inst.get(c, (None, 0.0, None))
        related = []
        if c == "press_r":
            related = [{"signal": alias["flow_a"], "r": 0.81, "lag": 2}]
        if c == "temp_r":
            related = [{"signal": alias["flow_a"], "r": 0.74, "lag": 4}]
        if c == "derived_sum":
            related = [{"signal": alias["flow_a"], "r": 0.99, "lag": 0}, {"signal": alias["flow_b"], "r": 0.98, "lag": 0}]
        sd = SignalDescriptor(id=a, source_column=c, column_index=i + 2, dtype="float32", structural_role=role, structural_confidence=conf, instrument_hypothesis=ih, instrument_confidence=ic, unit_operation_hypothesis=uo, unit_operation_confidence=0.4 if uo else 0.0, cluster_id="C1" if c in ("flow_a", "press_r", "temp_r", "comp_a", "temp_s") else "C2", related_signals=related, fingerprint=fp, units_hypothesis=None, confidence=conf, inference_ids=[inf1.id], evidence_ids=[ev1.id], excluded=role in ("constant", "derived_redundant"), excluded_reason="constant" if role == "constant" else ("derived" if role == "derived_redundant" else None))
        signals.append(sd)
    ws.write_json("signals", [s.model_dump() for s in signals])
    ws.write_json("relations", {"method": "lagged_xcorr", "max_lag": 10, "pairs": [{"a": alias["flow_a"], "b": alias["press_r"], "r": 0.81, "lag": 2}, {"a": alias["flow_a"], "b": alias["temp_r"], "r": 0.74, "lag": 4}, {"a": alias["flow_a"], "b": alias["temp_s"], "r": 0.6, "lag": 6}, {"a": alias["derived_sum"], "b": alias["flow_a"], "r": 0.99, "lag": 0}], "clusters": {"C1": [alias[c] for c in ("flow_a", "press_r", "temp_r", "comp_a", "temp_s")], "C2": [alias[c] for c in ("level_s", "flow_b", "power_c")]}})
    ws.write_json("domain", {"likelihood": schema.domain_likelihood, "hint": None, "statement": "Data looks like a sensor stream (autocorrelated continuous signals, actuator-like steps)."})

    # ---------- batches / checks / trust ----------
    batch_rows = max(50, n_rows // 10)
    batches = []
    b = 0
    for start in range(0, n_rows, batch_rows):
        end = min(n_rows, start + batch_rows)
        batches.append({"batch_id": f"B{b:04d}", "row_start": start, "row_end": end - 1, "n_rows": end - start, "method": "row_fraction"})
        b += 1
    ws.write_json("batches", {"method": "row_fraction", "batches": batches})

    rules = [
        Rule(id="RULE-001", text=f"{alias['temp_r']} must stay between 100 and 140.", author="human", status="active", compiled={"type": "range", "signal": alias["temp_r"], "min": 100, "max": 140}, compile_source="template", compile_explanation="range check on one signal", compile_confidence=0.95),
        Rule(id="RULE-002", text=f"{alias['press_r']} must not change by more than 50 per sample.", author="human", status="active", compiled={"type": "rate_of_change", "signal": alias["press_r"], "max_abs_delta": 50}, compile_source="llm-local:gemma4", compile_explanation="rate-of-change check", compile_confidence=0.8),
        Rule(id="RULE-003", text=f"{alias['level_s']} must not stay constant for more than 30 samples.", author="human", status="draft", compiled=None, compile_source="template", compile_explanation="", compile_confidence=0.0),
    ]
    ws.write_json("rules", [r.model_dump() for r in rules])

    checks: list[CheckResult] = []
    trust: list[TrustVerdict] = []
    k = 0
    for bi, bt in enumerate(batches):
        bid = bt["batch_id"]
        failing: list[str] = []
        for cat, ctype, sig in (("completeness", "missing", alias["temp_s"]), ("validity", "out_of_range", alias["press_r"]), ("consistency", "stuck", alias["flow_a"]), ("timeliness", "gap", "timestamp")):
            k += 1
            status = "pass"
            sev = 0.0
            if bi == 1 and ctype == "missing":
                status, sev = "fail", 0.7
            if bi == 3 and ctype == "out_of_range":
                status, sev = "fail", 0.9
            if bi == 5 and ctype == "stuck":
                status, sev = "warn", 0.4
            if bi == 4 and ctype == "gap":
                status, sev = "warn", 0.3
            ev = ws.evidence.add(ctype, f"{sig}: {ctype} check on {bid} -> {status}", signals=[sig] if sig != "timestamp" else [], values={"severity": sev}, computed_by=f"quality.checks.{ctype}", n_samples=bt["n_rows"], batch_id=bid)
            checks.append(CheckResult(check_id=f"CHK-{k:06d}", check_type=ctype, category=cat, signals=[sig] if sig != "timestamp" else [], batch_id=bid, group_id=None, status=status, severity=sev, statement=f"{sig}: {ctype} {status} in {bid}", evidence_ids=[ev.id], values={"severity": sev}, row_start=bt["row_start"], row_end=bt["row_end"]))
            if status == "fail":
                failing.append(sig)
        k += 1
        rule_status = "fail" if bi == 2 else "pass"
        checks.append(CheckResult(check_id=f"CHK-{k:06d}", check_type="rule:RULE-001", category="rule", signals=[alias["temp_r"]], batch_id=bid, status=rule_status, severity=0.6 if rule_status == "fail" else 0.0, statement=f"RULE-001 {rule_status} in {bid}", rule_id="RULE-001", row_start=bt["row_start"], row_end=bt["row_end"]))
        score = 1.0 - 0.35 * len(failing)
        trust.append(TrustVerdict(batch_id=bid, trusted=score >= 0.5, trust_score=round(score, 2), untrusted_signals=failing, reasons=[f"{s} failed a baseline check" for s in failing], check_ids=[c.check_id for c in checks if c.batch_id == bid and c.status != "pass"], statement="Batch can be trusted" if score >= 0.5 else "Batch cannot be trusted: too many failing signals"))
    ws.rewrite_jsonl("checks", [c.model_dump() for c in checks])
    ws.rewrite_jsonl("trust", [t.model_dump() for t in trust])
    for c in checks:
        if c.status != "pass":
            ws.log.record("system:quality", "check", "check", c.check_id, {"status": c.status, "batch_id": c.batch_id}, c.evidence_ids)

    # ---------- scores / flags / patterns / baseline / detect_meta ----------
    rng = np.random.default_rng(seed)
    score = np.abs(rng.normal(0.4, 0.15, n_rows))
    threshold = 1.0
    flags: list[Flag] = []
    groups = df["__group__"].to_numpy()
    fid = 0
    for g, info in truth["groups"].items():
        gid = str(int(g) + 1)
        idx = np.where(groups == gid)[0]
        if info["fault"] and len(idx):
            onset = int(info["onset"])
            sl = idx[onset:]
            score[sl] = np.clip(np.linspace(0.6, 2.4, len(sl)) + rng.normal(0, 0.1, len(sl)), 0, 3)
            fid += 1
            top = {"step": ("flow_a", "press_r", "temp_r"), "ramp": ("press_r", "temp_r", "temp_s"), "stuck_sensor": ("level_s", "flow_b", "power_c"), "corr_break": ("flow_b", "derived_sum", "flow_a"), "oscillation": ("press_r", "level_s", "temp_r"), "noise_burst": ("temp_r", "flow_a", "press_r")}[info["fault"]]
            ev = ws.evidence.add("contribution", f"{alias[top[0]]} contributes 48 % of the ensemble score after row {int(idx[onset])}", signals=[alias[t] for t in top], values={"shares": [0.48, 0.31, 0.21]}, computed_by="detect.attribution", n_samples=len(sl), group_id=gid)
            ev_cp = ws.evidence.add("changepoint", f"Change point at sample {onset} in group {gid} (abrupt)" if info["fault"] in ("step", "stuck_sensor", "corr_break") else f"Gradual change starting sample {onset} in group {gid}", signals=[alias[top[0]]], values={"onset": onset, "kind": "abrupt" if info["fault"] in ("step", "stuck_sensor", "corr_break") else "gradual"}, computed_by="detect.changepoints", n_samples=len(idx), group_id=gid)
            cause = "sensor" if info["fault"] in ("stuck_sensor",) else ("data" if info["fault"] == "corr_break" else "process")
            fl = Flag(id=f"FLAG-{fid:06d}", kind="drift" if info["fault"] == "ramp" else "anomaly", batch_id=f"B{int(idx[onset]) // batch_rows:04d}", group_id=gid, row_start=int(idx[onset]), row_end=int(idx[-1]), severity=0.8, score=float(score[sl].max()), threshold=threshold, detector="ensemble(pca,robust_z,corr_break)", statement=f"Group {gid}: score exceeds threshold from sample {onset}; {alias[top[0]]} leads", signals_ranked=[SignalContribution(signal=alias[top[j]], contribution=[0.48, 0.31, 0.21][j], direction=["up", "up", "stuck"][j] if info["fault"] == "stuck_sensor" else "up", lag=[0, 2, 4][j], explanation=f"{alias[top[j]]} moved outside its baseline band", evidence_ids=[ev.id]) for j in range(3)], evidence_ids=[ev.id, ev_cp.id], likely_cause_class=cause, confidence=0.72, pattern_id="PATTERN-A" if info["fault"] in ("step", "ramp") else "PATTERN-B", trust_context={"trusted": True, "trust_score": 0.95})
            flags.append(fl)
            ws.log.record("system:detect", "flag", "flag", fl.id, {"kind": fl.kind, "score": fl.score, "group_id": gid}, fl.evidence_ids)
    ws.rewrite_jsonl("flags", [f.model_dump() for f in flags])
    scores = pd.DataFrame({"row": np.arange(n_rows), "__group__": groups, "batch_id": [f"B{i // batch_rows:04d}" for i in range(n_rows)], "score": score.astype("float32"), "threshold": np.full(n_rows, threshold, dtype="float32"), "flagged": score > threshold})
    for c in ("flow_a", "press_r", "temp_r"):
        scores[f"contrib_{alias[c]}"] = rng.uniform(0, 1, n_rows).astype("float32")
    scores.to_parquet(ws.path("scores"), index=False)
    patterns = [
        FaultPattern(id="PATTERN-A", name=None, signature={"ranked": [alias["press_r"], alias["temp_r"], alias["flow_a"]], "directions": ["up", "up", "up"]}, n_events=sum(1 for f in flags if f.pattern_id == "PATTERN-A"), groups_affected=[f.group_id for f in flags if f.pattern_id == "PATTERN-A"], description="Pressure and temperature rise together after a feed change", confidence=0.7, classifier_reliability=0.83),
        FaultPattern(id="PATTERN-B", name="Level sensor freeze", signature={"ranked": [alias["level_s"]], "directions": ["stuck"]}, n_events=sum(1 for f in flags if f.pattern_id == "PATTERN-B"), groups_affected=[f.group_id for f in flags if f.pattern_id == "PATTERN-B"], description="A single signal stops moving while its partners continue", confidence=0.66, classifier_reliability=0.71),
    ]
    ws.write_json("patterns", [p.model_dump() for p in patterns])
    ws.write_json("baseline", {"method": "consensus_of_modes", "score": 0.81, "candidates": [{"method": "consensus_of_modes", "score": 0.81}, {"method": "pre_changepoint", "score": 0.77}, {"method": "densest_windows", "score": 0.7}], "reference_rows": int(0.35 * n_rows), "assumptions": ["The dominant regime is normal operation", "At least 30 % of every group is fault-free"], "inference_ids": []})
    ws.write_json("detect_meta", {"detectors": ["pca", "robust_z", "ewma", "cusum", "corr_break", "iforest"], "selected": ["pca", "robust_z", "corr_break"], "n_folds": 3, "threshold": threshold, "threshold_method": "oof_quantile_0.99", "fold_stability": {"pca": 0.91, "robust_z": 0.88, "corr_break": 0.8, "iforest": 0.62}, "seconds": 12.4, "rows_scored": n_rows, "out_of_fold": True})
    if with_evaluation:
        ws.write_json("evaluation", {"note": "evaluation only; labels never used for detection", "label_column": "fault_label", "auroc": 0.91, "detection_rate": 0.83, "false_alarm_rate": 0.04, "median_detection_delay_samples": 9, "pattern_label_purity": 0.78, "per_group": {g: {"detected": bool(info["fault"]), "delay": 9 if info["fault"] else None} for g, info in truth["groups"].items()}})

    # ---------- diagnoses ----------
    diags: list[Diagnosis] = []
    for i, fl in enumerate(flags, start=1):
        ranked = fl.signals_ranked
        prop = [PropagationStep(from_signal=ranked[0].signal, to_signal=ranked[1].signal, lag=2, strength=0.8, explanation=f"{ranked[0].signal} leads {ranked[1].signal} by 2 samples (r=0.81)"), PropagationStep(from_signal=ranked[1].signal, to_signal=ranked[2].signal, lag=2, strength=0.6, explanation=f"{ranked[1].signal} leads {ranked[2].signal} by 2 samples")]
        d = Diagnosis(id=f"DIAG-{i:06d}", flag_ids=[fl.id], group_id=fl.group_id, pattern_id=fl.pattern_id, fault_type=f"{fl.pattern_id} (unnamed)" if fl.pattern_id == "PATTERN-A" else "Level sensor freeze", cause_class=fl.likely_cause_class, ranked_signals=ranked, propagation=prop, steps=[f"The monitor compared group {fl.group_id} with the baseline regime.", f"From sample {fl.row_start} the combined score rose above {threshold}.", f"{ranked[0].signal} explains most of the deviation; {ranked[1].signal} follows two samples later.", "The pattern matches earlier events of the same shape."], summary=f"{ranked[0].signal} drifted first; {ranked[1].signal} and {ranked[2].signal} followed.", confidence=0.7, uncertainty=["Only 3 similar events seen so far", "The instrument type of the leading signal is a hypothesis"], assumptions=["Baseline regime is normal operation"], evidence_ids=fl.evidence_ids, critique=Critique(verdict="supported" if i % 2 else "weakened", objections=["A concurrent DQ warning on the same batch could inflate the score"] if i % 2 == 0 else [], checks=[{"name": "lead_lag_consistent", "passed": True, "detail": "lag order matches relations.json"}, {"name": "trust_ok", "passed": i % 2 == 1, "detail": "batch trust 0.95"}], adjusted_confidence=0.7 if i % 2 else 0.55, source="template"), narrative_source="template")
        diags.append(d)
        ws.log.record("system:diagnose", "diagnosis", "diagnosis", d.id, {"fault_type": d.fault_type, "confidence": d.confidence}, d.evidence_ids)
    ws.rewrite_jsonl("diagnoses", [d.model_dump() for d in diags])

    # ---------- assessor ----------
    ws.write_json("assessor", {"combined_score": 0.74, "ml_fitness": 0.78, "coverage": 0.7, "quality": 0.76, "summary": "The dataset is usable for unsupervised monitoring; more fault-free groups would tighten the baseline.", "learning_curve": [{"fraction": 0.1, "score": 0.51}, {"fraction": 0.2, "score": 0.6}, {"fraction": 0.4, "score": 0.68}, {"fraction": 0.7, "score": 0.73}, {"fraction": 1.0, "score": 0.74}], "recommendations": [{"action": "Collect 4 more fault-free runs", "expected_gain": 0.05, "evidence": "learning curve still rising"}, {"action": "Exclude S10 (constant)", "expected_gain": 0.0, "evidence": "zero variance"}]})

    # ---------- egress ledger ----------
    if with_egress:
        recs = [
            EgressRecord(id="EGR-000001", task="sensor_hypotheses", purpose="propose instrument roles for cluster C1", route="local", provider="ollama", model="gemma4:e4b-it-qat", artifact_types=["signal_catalog", "relations_summary"], payload_bytes=4200, payload_hash="ab12", guard_result="n/a", latency_ms=1800, ok=True),
            EgressRecord(id="EGR-000002", task="diagnosis_narrative", purpose="explain DIAG-000001", route="external", provider="anthropic", model="claude-sonnet-5", artifact_types=["diagnosis", "evidence_statements"], payload_bytes=3100, payload_hash="cd34", payload_preview='{"diagnosis": {"id": "DIAG-000001", "ranked_signals": ["S02", "S03"]}}', guard_result="allowed", latency_ms=2400, ok=True),
            EgressRecord(id="EGR-000003", task="critique", purpose="devil's advocate for DIAG-000002", route="external", provider="anthropic", model="claude-sonnet-5", artifact_types=["diagnosis"], payload_bytes=900, payload_hash="ef56", guard_result="blocked", guard_reason="payload contained a series longer than 20 points", ok=False, error="guard blocked; fell back to local"),
        ]
        for r in recs:
            ws.append_jsonl("egress_ledger", r.model_dump())
            ws.log.record(f"llm:{r.route}:{r.model}", "egress", "egress", r.id, {"task": r.task, "guard_result": r.guard_result})

    # ---------- human decisions ----------
    if with_human and diags and signals:
        from tpm.pipeline import apply_decision

        apply_decision(ws, settings, HumanDecision(actor_name="maija", role="engineer", action="accept", object_type="diagnosis", object_id=diags[0].id, note="Matches what we saw on the panel"))
        apply_decision(ws, settings, HumanDecision(actor_name="maija", role="engineer", action="override", object_type="signal", object_id=signals[3].id, note="This is a level transmitter, not a flow", new_value={"structural_role": "continuous_measured", "instrument_hypothesis": "level"}))
        apply_decision(ws, settings, HumanDecision(actor_name="ville", role="operator", action="question", object_type="flag", object_id=flags[0].id if flags else "FLAG-000001", note="Was the analyzer in calibration?"))
        apply_decision(ws, settings, HumanDecision(actor_name="ville", role="operator", action="name_pattern", object_type="pattern", object_id="PATTERN-A", new_value={"name": "Feed surge"}))
        inf_first = ws.inferences.all()[0]
        apply_decision(ws, settings, HumanDecision(actor_name="anna", role="reviewer", action="accept", object_type="inference", object_id=inf_first.id, note="grouping confirmed"))

    # ---------- status ----------
    st = RunStatus(run_id=run_id, source_path=source_path, profile=settings.profile, state="done", options={"language": "en"}, stages=[StageStatus(stage=s, state="done", progress=1.0, message="done", started_at=now_iso(), finished_at=now_iso()) for s in ("ingest", "profile", "quality", "detect", "diagnose", "assess")] + [StageStatus(stage="report", state="pending")])
    ws.set_status(st)
    ws.log.record("system:pipeline", "run_finished", "run", run_id, {"state": "done", "seconds": 42.0})
    return ws


if __name__ == "__main__":
    import sys

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("workspace_fake")
    w = build_fake_workspace(out)
    print(w.dir)
