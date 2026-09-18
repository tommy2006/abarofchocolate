"""Builds a small but complete demo workspace (dataset + schema + signal catalog + relations + evidence +
checks + trust + flags + diagnosis + rules) from the shared synthetic generator, without depending on the
other agents' stages. Used by tests/test_d_*, scripts/bakeoff.py and the UI demo when no run exists yet."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..config import ROOT, Settings, get_settings
from ..contracts import CheckResult, DatasetSchema, Diagnosis, FaultPattern, Flag, PropagationStep, Rule, RunStatus, SignalContribution, SignalDescriptor, StageStatus, TrustVerdict
from ..workspace import Workspace


def _synthetic(n_groups: int, n_samples: int, seed: int) -> tuple[pd.DataFrame, dict[str, Any]]:
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from tests.fixtures.synth import make_synthetic  # shared generator

        return make_synthetic(n_groups=n_groups, n_samples=n_samples, seed=seed)
    except Exception:
        rng = np.random.default_rng(seed)
        frames = []
        for g in range(n_groups):
            u = np.cumsum(rng.normal(0, 0.3, n_samples))
            df = pd.DataFrame({"run": g + 1, "sample": np.arange(1, n_samples + 1), "flow_a": 100 + 8 * u + rng.normal(0, 0.8, n_samples), "press_r": 2700 + 25 * np.roll(u, 2) + rng.normal(0, 2.5, n_samples), "temp_r": 120 + 1.5 * np.roll(u, 4) + rng.normal(0, 0.15, n_samples), "valve_1": np.repeat(rng.uniform(30, 70, n_samples // 25 + 1), 25)[:n_samples], "const_c": 7.5})
            frames.append(df)
        return pd.concat(frames, ignore_index=True), {"signal_roles": {}}


def _fingerprint(x: pd.Series) -> dict[str, Any]:
    v = pd.to_numeric(x, errors="coerce")
    n = int(v.notna().sum())
    if n == 0:
        return {"count": 0}
    d = v.dropna()
    diffs = d.diff().dropna()
    stuck = float((diffs == 0).mean()) if len(diffs) else 0.0
    ac1 = float(d.autocorr(lag=1)) if len(d) > 3 and d.std() > 0 else 0.0
    q = d.quantile([0.01, 0.05, 0.5, 0.95, 0.99]).tolist()
    step = float(np.min(np.abs(diffs[diffs != 0]))) if (diffs != 0).any() else 0.0
    return {
        "count": n, "missing_fraction": float(1 - n / max(1, len(v))), "mean": float(d.mean()), "std": float(d.std()), "min": float(d.min()), "max": float(d.max()),
        "q01": q[0], "q05": q[1], "median": q[2], "q95": q[3], "q99": q[4], "stuck_fraction": stuck, "autocorr_lag1": ac1,
        "quantization_step": step, "n_unique": int(d.nunique()), "noise_level": float(diffs.std()) if len(diffs) else 0.0,
    }


def make_demo_workspace(settings: Optional[Settings] = None, root: Optional[str | Path] = None, run_id: str = "demo_llm", n_groups: int = 6, n_samples: int = 200, seed: int = 1) -> Workspace:
    settings = settings or get_settings()
    ws = Workspace(run_id=run_id, settings=settings, root=root)
    df, truth = _synthetic(n_groups, n_samples, seed)
    df = df.copy()
    df["__group__"] = df["run"].astype(str) if "run" in df.columns else "0"
    ws.path("dataset").parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(ws.path("dataset"), index=False)

    meta_cols = [c for c in ("run", "sample", "timestamp") if c in df.columns]
    label_cols = [c for c in ("fault_label",) if c in df.columns]
    signal_cols = [c for c in df.columns if c not in meta_cols + label_cols + ["__group__"]]
    alias = {c: f"S{i + 1:02d}" for i, c in enumerate(signal_cols)}
    schema = DatasetSchema(
        dataset_id=run_id, source_path="synthetic://demo", format="synthetic", n_rows=int(len(df)), n_cols=int(df.shape[1]), had_header=True,
        columns=list(df.columns), time_column=None, sample_period_seconds=None, order_column="sample" if "sample" in df.columns else None,
        group_columns=["run"] if "run" in df.columns else [], group_column="__group__", grouping_method="key_columns",
        label_columns=label_cols, meta_columns=meta_cols, signal_columns=signal_cols, signal_alias=alias, n_groups=int(df["__group__"].nunique()),
        group_sizes={"min": int(df.groupby("__group__").size().min()), "median": int(df.groupby("__group__").size().median()), "max": int(df.groupby("__group__").size().max())},
        domain_likelihood={"sensor_stream": 0.85, "business_records": 0.05}, assumptions=["sample period unknown: working in sample units"],
    )
    ws.write_json("schema", schema)

    roles = truth.get("signal_roles", {})
    num = df[signal_cols].apply(pd.to_numeric, errors="coerce")
    corr = num.corr().fillna(0.0)
    evidence_by_signal: dict[str, list[str]] = {c: [] for c in signal_cols}
    signals: list[SignalDescriptor] = []
    relations: list[dict[str, Any]] = []
    n_rows = int(len(df))
    for c in signal_cols:
        fp = _fingerprint(df[c])
        ev = ws.evidence.add("distribution", f"{alias[c]} has mean {fp.get('mean', 0):.3g}, std {fp.get('std', 0):.3g}, range [{fp.get('min', 0):.3g}, {fp.get('max', 0):.3g}] over {fp.get('count', 0)} samples", signals=[alias[c]], values={k: fp[k] for k in ("mean", "std", "min", "max") if k in fp}, computed_by="sandbox.fingerprint", n_samples=fp.get("count", 0))
        evidence_by_signal[c].append(ev.id)
        if fp.get("stuck_fraction", 0) > 0.5:
            ev2 = ws.evidence.add("stuck", f"{alias[c]} repeats its previous value in {fp['stuck_fraction']:.0%} of samples (sample-and-hold or constant)", signals=[alias[c]], values={"stuck_fraction": fp["stuck_fraction"]}, computed_by="sandbox.fingerprint", n_samples=n_rows)
            evidence_by_signal[c].append(ev2.id)
    for i, a in enumerate(signal_cols):
        for b in signal_cols[i + 1:]:
            r = float(corr.loc[a, b])
            if abs(r) >= 0.5:
                ev = ws.evidence.add("correlation", f"{alias[a]} and {alias[b]} correlate r={r:.2f} at lag 0", signals=[alias[a], alias[b]], values={"r": r, "lag": 0}, computed_by="sandbox.corr", n_samples=n_rows)
                evidence_by_signal[a].append(ev.id)
                evidence_by_signal[b].append(ev.id)
                relations.append({"a": alias[a], "b": alias[b], "r": round(r, 3), "lag": 0, "evidence_id": ev.id})
    for i, c in enumerate(signal_cols):
        fp = _fingerprint(df[c])
        role = roles.get(c, "unknown")
        related = sorted([{"signal": rel["b"] if rel["a"] == alias[c] else rel["a"], "r": rel["r"], "lag": rel["lag"]} for rel in relations if alias[c] in (rel["a"], rel["b"])], key=lambda d: -abs(d["r"]))[:3]
        inf = ws.inferences.add(alias[c], f"{alias[c]} is {role}", status="inferred" if role != "unknown" else "uncertain", confidence=0.8 if role != "unknown" else 0.3, evidence_ids=evidence_by_signal[c], reasoning="structural fingerprint", source="code", stage="profile")
        signals.append(SignalDescriptor(id=alias[c], source_column=c, column_index=int(list(df.columns).index(c)), dtype=str(df[c].dtype), structural_role=role, structural_confidence=0.8 if role != "unknown" else 0.3, cluster_id=f"C{(i % 3) + 1}", related_signals=related, fingerprint=fp, confidence=0.7, inference_ids=[inf.id], evidence_ids=evidence_by_signal[c], excluded=role in ("constant", "counter", "identifier", "timestamp"), excluded_reason=("constant or index" if role in ("constant", "counter", "identifier", "timestamp") else None)))
    ws.write_json("signals", signals)
    ws.write_json("relations", {"pairs": relations, "clusters": {"C1": [s.id for s in signals if s.cluster_id == "C1"], "C2": [s.id for s in signals if s.cluster_id == "C2"], "C3": [s.id for s in signals if s.cluster_id == "C3"]}, "n_samples": n_rows})

    # one batch, a stuck check, trust verdict
    batch_id = "B0001"
    stuck_sig = next((s for s in signals if s.structural_role == "held_sampled"), signals[0])
    ev_stuck = ws.evidence.add("stuck", f"{stuck_sig.id} held its value for runs of 6 samples in batch {batch_id}", signals=[stuck_sig.id], values={"max_run": 6, "stuck_fraction": 0.83}, computed_by="sandbox.check", n_samples=n_rows, batch_id=batch_id)
    chk = CheckResult(check_id="CHK-000001", check_type="stuck", category="validity", signals=[stuck_sig.id], batch_id=batch_id, status="warn", severity=0.4, statement=f"{stuck_sig.id} looks sample-and-hold (updates every 6 samples); treated as analyzer, not a dead sensor", evidence_ids=[ev_stuck.id], values={"max_run": 6}, row_start=0, row_end=n_rows - 1)
    ws.append_jsonl("checks", chk)
    trust = TrustVerdict(batch_id=batch_id, trusted=True, trust_score=0.92, untrusted_signals=[], reasons=["no critical data-quality failures"], check_ids=[chk.check_id], statement=f"Batch {batch_id} can be trusted (score 0.92)")
    ws.append_jsonl("trust", trust)
    ws.write_json("batches", [{"batch_id": batch_id, "row_start": 0, "row_end": n_rows - 1}])

    # a flag on the first faulty group (or group 1), with contributions + evidence
    fault_group = next((g for g, info in (truth.get("groups") or {}).items() if info.get("fault")), None)
    onset = int((truth.get("groups") or {}).get(fault_group, {}).get("onset") or n_samples // 2) if fault_group is not None else n_samples // 2
    g_index = int(fault_group) if fault_group is not None else 0
    row_start = g_index * n_samples + onset
    row_end = min(n_rows - 1, (g_index + 1) * n_samples - 1)
    top = [s for s in signals if s.source_column in ("press_r", "flow_a", "temp_r")] or signals[:3]
    ev_flag = ws.evidence.add("contribution", f"Rows {row_start}-{row_end} (group {g_index + 1}): PCA SPE score 4.1 above threshold 2.0; {top[0].id} contributes 52% of the residual", signals=[t.id for t in top], values={"score": 4.1, "threshold": 2.0, "contribution_top": 0.52}, computed_by="sandbox.detect", n_samples=row_end - row_start + 1, group_id=str(g_index + 1), batch_id=batch_id)
    ev_lag = ws.evidence.add("lag", f"{top[0].id} leads {top[1].id} by 2 samples (cross-correlation 0.88) in group {g_index + 1}" if len(top) > 1 else f"{top[0].id} changed first", signals=[t.id for t in top[:2]], values={"lag": 2, "r": 0.88}, computed_by="sandbox.xcorr", n_samples=n_samples, group_id=str(g_index + 1))
    ev_cp = ws.evidence.add("changepoint", f"Change point in {top[0].id} at row {row_start} (abrupt mean shift, +18 units, CUSUM)", signals=[top[0].id], values={"row": row_start, "shift": 18.0, "kind": "abrupt"}, computed_by="sandbox.cusum", n_samples=n_samples, group_id=str(g_index + 1))
    contribs = [SignalContribution(signal=t.id, contribution=c, direction="up", lag=l, explanation=f"{t.id} shifted", evidence_ids=[ev_flag.id]) for t, c, l in zip(top, (0.52, 0.31, 0.17), (0, 2, 4))]
    flag = Flag(id="FLAG-000001", kind="anomaly", batch_id=batch_id, group_id=str(g_index + 1), row_start=row_start, row_end=row_end, severity=0.8, score=4.1, threshold=2.0, detector="pca_spe", statement=f"Anomaly in group {g_index + 1} from row {row_start}: {', '.join(t.id for t in top)} shifted together", signals_ranked=contribs, evidence_ids=[ev_flag.id, ev_lag.id, ev_cp.id], likely_cause_class="process", confidence=0.7, pattern_id="PATTERN-A", trust_context={"trusted": True, "trust_score": 0.92})
    ws.append_jsonl("flags", flag)
    ws.write_json("patterns", [FaultPattern(id="PATTERN-A", signature={"ranked": [t.id for t in top], "directions": ["up"] * len(top)}, n_events=3, groups_affected=[str(g_index + 1)], description=f"{top[0].id} shifts first, {', '.join(t.id for t in top[1:])} follow", confidence=0.6, evidence_ids=[ev_flag.id])])
    prop = [PropagationStep(from_signal=top[0].id, to_signal=top[1].id, lag=2, strength=0.88, explanation="lagged correlation", evidence_ids=[ev_lag.id])] if len(top) > 1 else []
    diag = Diagnosis(id="DIAG-000001", flag_ids=[flag.id], group_id=str(g_index + 1), pattern_id="PATTERN-A", fault_type="PATTERN-A (unnamed)", cause_class="process", ranked_signals=contribs, propagation=prop,
                     steps=[f"1. {top[0].id} shifted abruptly at row {row_start} [{ev_cp.id}]", f"2. {top[1].id if len(top) > 1 else top[0].id} followed 2 samples later [{ev_lag.id}]", f"3. Several correlated signals moved together, so a process change is more likely than a sensor fault [{ev_flag.id}]"],
                     summary=f"A process-side step change starting at {top[0].id} propagated to related signals in group {g_index + 1}.", confidence=0.7, uncertainty=["sample period unknown", "single event in this group"], assumptions=["baseline = dominant regime of the group"], evidence_ids=[ev_flag.id, ev_lag.id, ev_cp.id])
    ws.append_jsonl("diagnoses", diag)
    ws.write_json("rules", [Rule(id="RULE-001", text=f"{top[0].id} must stay below {float(_fingerprint(df[top[0].source_column]).get('q99', 0)):.0f}", status="active", compiled={"rule_type": "threshold", "signals": [top[0].id], "params": {"operator": "<", "threshold": float(_fingerprint(df[top[0].source_column]).get("q99", 0))}}, compile_source="template", compile_explanation="threshold on the 99th percentile", compile_confidence=0.5)])
    ws.set_status(RunStatus(run_id=run_id, source_path="synthetic://demo", profile=settings.profile, state="done", stages=[StageStatus(stage=s, state="done", progress=1.0) for s in ("ingest", "profile", "quality", "detect", "diagnose")]))
    ws.write_json("meta", {"run_id": run_id, "source_path": "synthetic://demo", "profile": settings.profile, "sandbox": True})
    return ws


def catalog_payload(ws: Workspace, max_signals: int = 12) -> dict[str, Any]:
    """The standard external-safe payload for sensor_hypotheses: signal catalog + relations + evidence statements."""
    signals = [s.model_dump() for s in ws.signals() if not s.excluded][:max_signals]
    ids = {s["id"] for s in signals}
    rel = ws.read_json("relations", {}) or {}
    pairs = [p for p in rel.get("pairs", []) if p.get("a") in ids and p.get("b") in ids][:40]
    ev_ids = {e for s in signals for e in s.get("evidence_ids", [])}
    evidence = [{"id": e.id, "kind": e.kind, "signals": e.signals, "statement": e.statement, "n_samples": e.n_samples} for e in ws.evidence.all() if e.id in ev_ids][:60]
    sch = ws.schema()
    schema_summary = {"n_rows": sch.n_rows, "n_groups": sch.n_groups, "sample_period_seconds": sch.sample_period_seconds, "domain_likelihood": sch.domain_likelihood, "assumptions": sch.assumptions, "signal_alias": sch.signal_alias} if sch else {}
    return {"signals": signals, "relations": {"pairs": pairs, "clusters": rel.get("clusters", {})}, "evidence": evidence, "schema_summary": schema_summary}


def diagnosis_payload(ws: Workspace, diag_id: Optional[str] = None) -> dict[str, Any]:
    diags = ws.diagnoses()
    if not diags:
        return {}
    d = next((x for x in diags if x.id == diag_id), diags[0])
    flags = [f.model_dump() for f in ws.flags() if f.id in d.flag_ids]
    ev_ids = set(d.evidence_ids) | {e for f in flags for e in f.get("evidence_ids", [])}
    evidence = [{"id": e.id, "kind": e.kind, "signals": e.signals, "statement": e.statement, "n_samples": e.n_samples} for e in ws.evidence.all() if e.id in ev_ids]
    trust = [t.model_dump() for t in ws.trust() if t.batch_id in {f.get("batch_id") for f in flags}]
    return {"diagnosis": d.model_dump(exclude={"critique"}), "flags": flags, "evidence": evidence, "trust": trust}
