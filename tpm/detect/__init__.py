"""detect stage: unsupervised drift/anomaly detection with per-signal attribution.

Public functions (see docs/ARCHITECTURE.md):
    run_detect(ws, settings, ctx)                          -> summary dict; writes scores.parquet, flags.jsonl,
                                                              patterns.json, baseline.json, detect_meta.json,
                                                              evaluation.json (only when label columns exist)
    score_batch(ws, settings, batch_df, batch_id, trust)   -> list[Flag] (streaming path; persisted models)
    fit_score_subset(ws, settings, train_groups, eval_groups, time_budget_s) -> metrics (assessor learning curves)
    apply_override(ws, settings, decision)                 -> flags: accept/question/override/dismiss; patterns: name

Labels never reach detection; evaluate.py reads them only to compute metrics.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import numpy as np

from ..contracts import Flag, HumanDecision, TrustVerdict
from ..memory import memory_snapshot
from ._common import Budget, DetectInputs, compute_relations, fetch_blocks, group_table, load_inputs, plan_blocks, progress_cb, segments, stage_budget
from .baseline import BaselineResult, Sample, estimate_baseline, ranges_to_mask
from .cascade import build_cascades, chain_for_flag
from .ensemble import FoldMap, FoldModel, ScoreStore, _fit_fold, calibrate_ensemble_threshold, fit_final_model, fit_fold_models, load_final_model, make_folds, pilot_and_select, save_models, score_all_rows
from .evaluate import run_evaluation
from .events import FlagIds, build_flags, find_segments, segment_score, severity_of, trust_context, sustained_mask
from .patterns import assign_to_pattern, build_patterns, name_pattern

DETECT_KINDS = {"anomaly", "drift", "changepoint", "cascade"}
FAST_DETECTORS = ["pca", "robust_z", "ewma", "cusum", "corr_break"]


# ------------------------------------------------------------------------------------------------
# sample
# ------------------------------------------------------------------------------------------------


def _build_sample(ws, inputs: DetectInputs, groups_meta: list[dict[str, Any]], settings, max_rows: Optional[int] = None) -> Sample:
    window = int(settings.detect.window)
    max_rows = int(max_rows or settings.detect.max_fit_rows)
    lead = window + 2
    block_len = max(200, 10 * window)
    blocks = plan_blocks(groups_meta, max_rows, block_len=block_len, lead=lead)
    rows, groups, X = fetch_blocks(ws, inputs.columns, inputs.group_col, [(s, e) for s, e, _ in blocks])
    total = sum(g["n"] for g in groups_meta)
    desc = {"n_sample_rows": int(len(rows)), "n_dataset_rows": int(total), "n_blocks": len(blocks), "block_len": block_len, "lead_in_rows": lead, "description": f"{len(blocks)} contiguous row blocks spread evenly over {len(groups_meta)} group(s) ({len(rows):,} of {total:,} rows); rolling features are exact inside a block"}
    return Sample(rows=rows, groups=groups, X=X, aliases=list(inputs.aliases), blocks=blocks, description=desc)


# ------------------------------------------------------------------------------------------------
# run_detect
# ------------------------------------------------------------------------------------------------


def run_detect(ws, settings, ctx: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    t_start = time.time()
    ctx = ctx or {}
    budget = stage_budget(settings, ctx)
    progress = progress_cb(ctx)
    timing: dict[str, float] = {}
    notes: list[str] = []
    seed = int((ctx.get("options") or {}).get("seed", 0))

    def lap(name: str, t0: float) -> None:
        timing[name] = round(time.time() - t0, 2)

    t0 = time.time()
    inputs = load_inputs(ws, settings)
    notes += inputs.notes
    groups_meta = group_table(ws, inputs.group_col)
    lap("load_inputs", t0)
    progress(0.02, f"{inputs.p} signals, {len(groups_meta)} groups, {inputs.n_rows:,} rows")

    t0 = time.time()
    sample = _build_sample(ws, inputs, groups_meta, settings)
    lap("sample", t0)
    if not inputs.relations or not inputs.relations.get("clusters"):
        t0 = time.time()
        inputs.relations = compute_relations(sample.X[: min(sample.n, 60_000)], inputs.aliases, inputs.roles)
        notes.append("relations.json missing or without clusters; correlation structure computed on the fit sample")
        lap("relations_fallback", t0)
    progress(0.08, "estimating the baseline regime")

    t0 = time.time()
    baseline = estimate_baseline(ws, settings, sample, inputs.roles, budget, ctx, seed=seed)
    lap("baseline", t0)
    ws.write_json("baseline", baseline.to_json() | {"sample": sample.description})
    progress(0.2, f"baseline: {baseline.strategy} ({baseline.mask.mean():.0%} of sample, confidence {baseline.confidence:.2f})")

    t0 = time.time()
    fold_map, fold_meta = make_folds(sample, groups_meta, settings, seed=seed)
    lap("folds", t0)
    detector_names = [d for d in settings.detect.detectors]
    t0 = time.time()
    models, sample_keys = fit_fold_models(sample, fold_map, baseline, inputs, settings, budget, detector_names, seed=seed, progress=progress, n_rows_total=int(inputs.n_rows))
    lap("fit_folds", t0)
    t0 = time.time()
    selection = pilot_and_select(models, sample, sample_keys, settings, budget, inputs.n_rows)
    lap("pilot_select", t0)
    progress(0.45, f"detectors selected: {', '.join(selection['selected'])}")
    t0 = time.time()
    final = fit_final_model(sample, baseline, inputs, settings, models, [n for n in detector_names if n in selection["selected"]] or detector_names, budget, seed=seed)
    final.selected = [n for n in selection["selected"] if n in final.detectors]
    final.ens_threshold = float(np.median([m.ens_threshold for m in models])) if models else 1.0
    final.reset_states()
    save_models(ws, models, final)
    lap("fit_final", t0)
    for m in models:
        ws.evidence.add("threshold", f"Fold {m.fold}: thresholds calibrated on {m.n_val_rows} validation baseline rows of {len(m.val_keys)} group(s); " + ", ".join(f"{k}={v:.3g}" for k, v in m.thresholds.items()) + f"; ensemble threshold {m.ens_threshold:.3g}.", values={"fold": m.fold, "thresholds": m.thresholds, "ens_threshold": m.ens_threshold, "n_fit_rows": m.n_fit_rows}, computed_by="detect.ensemble.fit_fold_models", n_samples=m.n_val_rows)
    ws.log.record("system:detect", "detectors_selected", "dataset", "detect", {"selected": selection["selected"], "dropped": selection["dropped"], "reliability": selection["reliability"]})

    progress(0.5, "scoring all rows out-of-fold")
    t0 = time.time()
    store, score_meta = score_all_rows(ws, inputs, models, fold_map, selection["selected"], settings, budget, progress=progress)
    lap("score_rows", t0)
    progress(0.8, "events and onsets")

    t0 = time.time()
    flags, onsets, ev_meta = build_flags(ws, inputs, settings, store, models, fold_map, budget, progress=progress)
    ids = FlagIds(ws)
    ids._n = max(ids._n, max((int(f.id.split("-")[-1]) for f in flags), default=0))
    cascade_flags, chains = build_cascades(ws, inputs, flags, ids, settings)
    flags.extend(cascade_flags)
    lap("events", t0)
    t0 = time.time()
    patterns, assign, pat_meta = build_patterns(ws, settings, flags, inputs.aliases, seed=seed)
    for f in flags:
        f.pattern_id = assign.get(f.id)
    lap("patterns", t0)
    # persist flags: keep flags from other stages (dq/rule), replace ours
    existing = [f for f in ws.read_jsonl("flags") if f.get("kind") not in DETECT_KINDS]
    ws.rewrite_jsonl("flags", existing + [f.model_dump() for f in flags])
    from ..log.stage_log import log_flags, log_stage_inferences  # decision log: EVERY flag gets its own entry (no cap), written in bulk

    flag_log = log_flags(ws, flags)
    ws.write_json("propagation", {fid: [s.model_dump() for s in steps] for fid, steps in chains.items()})
    try:
        from .suspicious import build_suspicious_rows

        susp = build_suspicious_rows(ws, settings, flags, (ev_meta or {}).get("points"))
        notes.append(f"suspicious rows: {susp['n_rows']} (point-dominated: {susp['regime']['point_dominated']})")
    except Exception as ex:  # the headline list must never break detection
        ws.log.record("system:detect", "warning", "dataset", "suspicious_rows", {"error": str(ex)[:300]})

    evaluation = None
    if inputs.label_columns:
        t0 = time.time()
        try:
            evaluation = run_evaluation(ws, inputs, store, flags, settings, rule=(ev_meta or {}).get("event_rule"))
        except Exception as e:
            notes.append(f"evaluation failed: {e}")
        lap("evaluation", t0)

    # rows inside the events this stage keeps - the same rule the evaluation counts with (find_segments, not the bare
    # mask), so "rows flagged" means one thing in the report, the pages and the evaluation
    _er = (ev_meta or {}).get("event_rule") or {}
    _min_len, _window = int(settings.detect.min_event_len), int(settings.detect.window)
    _persist = int(_er.get("persist_rows") or 0) or None
    _sthr = float(_er.get("sustained_threshold") or 1.0)
    n_flagged_rows = 0
    for _g, _idx in store.group_indices().items():
        _o = np.argsort(store.rows[_idx], kind="stable")
        _ens = store.ens[_idx][_o]
        n_flagged_rows += sum(int(_b - _a) for _a, _b in find_segments(_ens, _min_len, max(_min_len, _window), window=_window, persist=_persist, sustain_thr=_sthr))
    n_rows_above_row_threshold = int((store.ens >= 1.0).sum())
    n_rows_reported = sum(int(f.row_end) - int(f.row_start) + 1 for f in flags if f.kind in ("anomaly", "drift", "changepoint"))
    meta = {
        "detectors_configured": detector_names,
        "detectors_used": selection["selected"],
        "selection": selection,
        "folds": fold_meta | {"models": [m.describe() for m in models]},
        "final_model": final.describe(),
        "baseline": {"strategy": baseline.strategy, "confidence": baseline.confidence, "status": baseline.status, "inference_id": baseline.inference_id},
        "sample": sample.description,
        "scoring": score_meta,
        "events": ev_meta | {"n_flags": len(flags), "by_kind": {k: sum(1 for f in flags if f.kind == k) for k in DETECT_KINDS}, "n_onsets": len(onsets)},
        "patterns": pat_meta,
        "rows_flagged": n_flagged_rows,  # rows inside kept events; rows_above_row_threshold counts single readings

        "rows_flagged_fraction": round(n_flagged_rows / max(1, store.n), 4),
        "rows_above_row_threshold": n_rows_above_row_threshold,
        "rows_in_reported_events": n_rows_reported,
        "rows_in_reported_events_fraction": round(n_rows_reported / max(1, store.n), 4),
        "rows_counted": "rows_flagged = every stretch the event rule marks; rows_in_reported_events = the events this stage reported (the strongest per run); rows_above_row_threshold = single readings at or above the row threshold",
        "event_rule": _er,
        "timing_s": timing | {"total": round(time.time() - t_start, 2)},
        "time_budget_s": budget.seconds,
        "memory": memory_snapshot(),
        "notes": notes,
        "evaluation_written": evaluation is not None,
        "score_semantics": "scores.parquet: `ensemble` is normalized so that 1.0 is the calibrated row threshold (is_flagged = ensemble >= 1 for one reading); an EVENT needs the median score of event_rule.persist_rows consecutive rows to reach event_rule.sustained_threshold; per-detector columns are score/threshold; top-k shares are the ensemble attribution.",
    }
    ws.write_json("detect_meta", meta)
    n_inf_logged = log_stage_inferences(ws, "detect")  # claims of the detect modules that have no entry of their own yet
    ws.log.record("system:detect", "stage_summary", "dataset", "detect", {"n_flags": len(flags), "n_flags_logged": flag_log["n"], "log_seconds": flag_log["seconds"], "n_inferences_logged_at_end": n_inf_logged, "n_patterns": len(patterns), "detectors": selection["selected"], "seconds": round(time.time() - t_start, 1), "rows_flagged_fraction": meta["rows_flagged_fraction"]})
    progress(1.0, "done")
    msg = f"{len(flags)} flags ({ev_meta['n_groups_with_events']} of {ev_meta['n_groups']} groups), {len(patterns)} patterns, detectors {'+'.join(selection['selected'])}, baseline '{baseline.strategy}' (conf {baseline.confidence:.2f}), {time.time() - t_start:.0f}s"
    return {"message": msg, "n_flags": len(flags), "n_patterns": len(patterns), "detectors": selection["selected"], "baseline_strategy": baseline.strategy, "baseline_confidence": round(baseline.confidence, 3), "rows_flagged_fraction": meta["rows_flagged_fraction"], "seconds": round(time.time() - t_start, 1), "evaluation": None if evaluation is None else {c: r.get("auroc_ensemble") for c, r in evaluation.get("columns", {}).items()}}


# ------------------------------------------------------------------------------------------------
# streaming: score_batch
# ------------------------------------------------------------------------------------------------

_MODEL_CACHE: dict[str, tuple[float, FoldModel]] = {}


def _cached_final_model(ws) -> Optional[FoldModel]:
    p = ws.dir / "models" / "detect_final.joblib"
    if not p.exists():
        return None
    mtime = p.stat().st_mtime
    key = str(ws.dir)
    hit = _MODEL_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    m = load_final_model(ws)
    if m is not None:
        _MODEL_CACHE[key] = (mtime, m)
    return m


def _batch_arrays(batch_df, inputs: DetectInputs, batch_id: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    import pandas as pd

    n = len(batch_df)
    X = np.full((n, inputs.p), np.nan, dtype=np.float32)
    for j, (col, alias) in enumerate(zip(inputs.columns, inputs.aliases)):
        src = col if col in batch_df.columns else (alias if alias in batch_df.columns else (inputs.signal_names.get(alias) if inputs.signal_names.get(alias) in batch_df.columns else None))
        if src is None:
            continue
        X[:, j] = pd.to_numeric(batch_df[src], errors="coerce").to_numpy(dtype=np.float32, na_value=np.nan)
    if "__group__" in batch_df.columns:
        groups = batch_df["__group__"].astype(str).to_numpy(dtype=object)
    elif inputs.schema is not None and inputs.schema.group_columns and all(c in batch_df.columns for c in inputs.schema.group_columns):
        groups = batch_df[inputs.schema.group_columns].astype(str).agg("|".join, axis=1).to_numpy(dtype=object)
    else:
        groups = np.full(n, str(batch_id), dtype=object)
    if "__row__" in batch_df.columns:
        rows = batch_df["__row__"].to_numpy(dtype=np.int64)
    elif np.issubdtype(np.asarray(batch_df.index).dtype, np.integer):
        rows = np.asarray(batch_df.index, dtype=np.int64)
    else:
        rows = np.arange(n, dtype=np.int64)
    return rows, groups, X


def _quick_fit(ws, settings, inputs: DetectInputs, rows: np.ndarray, groups: np.ndarray, X: np.ndarray) -> FoldModel:
    """No persisted model: fit on the batch's own baseline (warned, low confidence) and persist it."""
    sample = Sample(rows=rows, groups=groups, X=X, aliases=list(inputs.aliases), description={"description": "the incoming batch itself"})
    budget = Budget(30)
    baseline = estimate_baseline(ws, settings, sample, inputs.roles, budget, None, quiet=True)
    keys = np.asarray(["all"] * len(rows), dtype=object)
    fm = _fit_fold(-1, ["all"], [], [], sample, keys, baseline, inputs, settings, FAST_DETECTORS, budget)
    fm.selected = list(fm.detectors)
    fm.ens_threshold = calibrate_ensemble_threshold(fm, fm.selected)
    fm.notes.append("quick fit on the batch's own baseline: no persisted model was available")
    fm.reset_states()
    inf = ws.inferences.add("dataset", "Detection model fitted on a single incoming batch (no persisted model)", status="assumed", confidence=0.3, reasoning="Run the full pipeline once to obtain out-of-fold models; batch-only thresholds are optimistic.", stage="detect")
    ws.log.record("system:detect", "warning", "inference", inf.id, {"note": fm.notes[-1]})
    save_models(ws, [], fm)
    _MODEL_CACHE.pop(str(ws.dir), None)
    return fm


def _slice_res(res: dict[str, Any], idx: np.ndarray) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in res.items():
        if isinstance(v, np.ndarray) and len(v) == len(res["rows"]):
            out[k] = v[idx]
        elif isinstance(v, dict) and k in ("norm", "contrib"):
            out[k] = {n: a[idx] for n, a in v.items()}
        else:
            out[k] = v
    return out


def score_batch(ws, settings, batch_df, batch_id: str, trust: Optional[TrustVerdict] = None) -> list[Flag]:
    _rule = ((ws.read_json("detect_meta") or {}).get("events") or {}).get("event_rule") or {}  # same event rule as the full run
    from .attribution import attribute_event
    from .changepoints import analyse_group_onset

    t_start = time.time()
    coltypes = None
    if not ws.exists("dataset"):
        coltypes = {c: ("DOUBLE" if np.issubdtype(batch_df[c].dtype, np.number) else "VARCHAR") for c in batch_df.columns}
    inputs = load_inputs(ws, settings, coltypes=coltypes)
    rows, groups, X = _batch_arrays(batch_df, inputs, batch_id)
    if len(rows) == 0:
        return []
    fm = _cached_final_model(ws)
    if fm is None or not fm.detectors:
        fm = _quick_fit(ws, settings, inputs, rows, groups, X)
    fm._inputs = inputs
    window = int(settings.detect.window)
    min_len = int(settings.detect.min_event_len)
    F = fm.spec.transform(X, groups)
    res = fm.score_features(F, groups, names=list(fm.detectors))
    res.update({"rows": rows, "X": X, "F": F, "z": F[:, fm.spec.block_slice("value")], "rstd_z": F[:, fm.spec.block_slice("rstd")], "raw_rstd": fm.spec.raw_rstd(X, groups)})
    ens = res["ensemble"]
    untrusted = set(trust.untrusted_signals or []) if trust is not None else set()
    tctx = None
    if trust is not None:
        tctx = {"batch_ids": [batch_id], "trusted": bool(trust.trusted), "untrusted_signals": sorted(untrusted), "trust_scores": {batch_id: round(float(trust.trust_score), 3)}}
    ids = FlagIds(ws)
    patterns = ws.patterns()
    flags: list[Flag] = []
    for s, e in segments(groups):
        g = str(groups[s])
        idx = np.arange(s, e)
        sub = _slice_res(res, idx)
        segs = find_segments(ens[s:e], min_len, max(min_len, window), window=window, persist=_rule.get("persist_rows"), sustain_thr=_rule.get("sustained_threshold"))
        onset = None
        if segs:
            try:
                onset = analyse_group_onset(ws, g, ens[s:e], rows[s:e], window, lambda rs, re_, _sub=sub: _slice_res(_sub, np.flatnonzero((_sub["rows"] >= rs) & (_sub["rows"] < re_))), inputs.aliases)
            except Exception:
                onset = None
        for a, b in segs[:8]:
            row_start, row_end = int(rows[s + a]), int(rows[s + b - 1]) + 1
            att = attribute_event(ws, inputs, fm, sub, row_start, row_end, g, untrusted)
            mean_norm = segment_score(ens[s + a : s + b])
            peak = float(ens[s + a : s + b].max())
            n_ev = b - a
            kind = "drift" if (onset is not None and onset.kind == "gradual" and n_ev >= 3 * window) else "anomaly"
            conf = float(np.clip(0.3 + 0.3 * att["agreement"] + 0.2 * min(1.0, (mean_norm - 1.0) / 2.0) + 0.2 * min(1.0, n_ev / (3.0 * min_len)), 0.05, 0.95))
            ranked = att["ranked"]
            lead = ", ".join(f"{r.signal} ({r.contribution:.0%}, {r.direction})" for r in ranked[:3])
            stmt = f"{'Drift' if kind == 'drift' else 'Anomaly'} in batch {batch_id}, group {g}, rows {row_start}-{row_end - 1} ({n_ev} rows): ensemble score {mean_norm:.1f}x threshold (peak {peak:.1f}x). Leading signals: {lead}. Likely cause: {att['cause']} ({att['cause_reason']})."
            ev_ids = list(att["evidence_ids"]) + ([onset.evidence_id] if onset is not None and onset.evidence_id else [])
            fl = Flag(id=ids.next(), kind=kind, batch_id=batch_id, group_id=g, row_start=row_start, row_end=row_end - 1, severity=severity_of(mean_norm, n_ev, window), score=round(mean_norm, 4), threshold=1.0, detector="ensemble:" + "+".join(fm.selected), statement=stmt, signals_ranked=ranked, evidence_ids=ev_ids, likely_cause_class=att["cause"], cause_detail=att.get("cause_detail"), confidence=round(conf, 3), trust_context=tctx)
            fl.pattern_id = assign_to_pattern(fl, patterns) if patterns else None
            flags.append(fl)
        if onset is not None:
            flags.append(Flag(id=ids.next(), kind="changepoint", batch_id=batch_id, group_id=g, row_start=onset.row, row_end=onset.row, severity=0.5, score=float(ens[s:e][onset.index : onset.index + 6 * window].max()) if onset.index < e - s else 0.0, threshold=1.0, detector="changepoints:cusum+ruptures", statement=onset.statement, signals_ranked=[], evidence_ids=[onset.evidence_id] if onset.evidence_id else [], likely_cause_class="unknown", confidence=round(onset.confidence, 3), trust_context=tctx))
    cascade_flags, chains = build_cascades(ws, inputs, flags, ids, settings)
    flags.extend(cascade_flags)
    for f in flags:
        ws.append_jsonl("flags", f.model_dump())
    from ..log.stage_log import log_flags  # one entry per flag, all of the batch in one transaction

    log_flags(ws, flags)
    if chains:
        prop = ws.read_json("propagation", {}) or {}
        prop.update({fid: [s.model_dump() for s in steps] for fid, steps in chains.items()})
        ws.write_json("propagation", prop)
    # per-batch scores for the UI
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        d = ws.dir / "batch_scores"
        d.mkdir(exist_ok=True)
        top = np.argsort(-res["shares"], axis=1)[:, :3]
        aliases = np.array(inputs.aliases, dtype=object)
        cols = {"__row__": rows, "__group__": pa.array(groups.astype(str)), "ensemble": ens, "is_flagged": ens >= 1.0}
        for n, arr in res["norm"].items():
            cols[f"score_{n}"] = arr
        for k in range(min(3, inputs.p)):
            cols[f"top{k + 1}_signal"] = pa.array(aliases[top[:, k]].astype(str))
            cols[f"top{k + 1}_share"] = np.take_along_axis(res["shares"], top[:, k : k + 1], axis=1)[:, 0]
        pq.write_table(pa.table(cols), str(d / f"{batch_id}.parquet"), compression="zstd")
    except Exception as e:
        ws.log.record("system:detect", "warning", "batch", batch_id, {"batch_scores_error": str(e)[:200]})
    ws.log.record("system:detect", "batch_scored", "batch", batch_id, {"n_rows": int(len(rows)), "n_flags": len(flags), "flagged_fraction": round(float((ens >= 1.0).mean()), 4), "seconds": round(time.time() - t_start, 2)})
    return flags


# ------------------------------------------------------------------------------------------------
# bounded experiments for the assessor
# ------------------------------------------------------------------------------------------------


def fit_score_subset(ws, settings, train_groups: list[str], eval_groups: list[str], time_budget_s: float = 60.0) -> dict[str, Any]:
    """Fit on the baseline rows of `train_groups`, calibrate with internal group folds, score the sampled rows
    of `eval_groups`. Returns stability/agreement/flagged-fraction metrics (+ AUROC when labels exist).
    Writes nothing to the run artifacts except a log entry."""
    from scipy.stats import spearmanr

    t_start = time.time()
    budget = Budget(time_budget_s)
    inputs = load_inputs(ws, settings)
    train_groups = [str(g) for g in train_groups]
    eval_groups = [str(g) for g in eval_groups]
    all_meta = group_table(ws, inputs.group_col)
    wanted = set(train_groups) | set(eval_groups)
    gm = [g for g in all_meta if g["group"] in wanted]
    if not gm:
        return {"error": "no matching groups", "seconds": round(time.time() - t_start, 2)}
    max_rows = int(min(settings.detect.max_fit_rows, max(5000, 2000 * time_budget_s)))
    sample = _build_sample(ws, inputs, gm, settings, max_rows=max_rows)
    if not inputs.relations or not inputs.relations.get("clusters"):
        inputs.relations = compute_relations(sample.X[: min(sample.n, 30_000)], inputs.aliases, inputs.roles)
    bl_json = ws.read_json("baseline", None)
    if bl_json and bl_json.get("ranges"):
        mask = ranges_to_mask(bl_json["ranges"], sample.rows, sample.groups)
        if mask.sum() < 50:
            mask = np.ones(sample.n, dtype=bool)
        baseline = BaselineResult(strategy=bl_json.get("strategy", "persisted"), mask=mask, ranges=bl_json["ranges"], confidence=float(bl_json.get("confidence", 0.5)), status=bl_json.get("status", "assumed"), candidates=[], assumptions=[], evidence_ids=[])
    else:
        baseline = estimate_baseline(ws, settings, sample, inputs.roles, budget, None, quiet=True)
    names = FAST_DETECTORS if time_budget_s < 90 else [d for d in settings.detect.detectors if d != "autoencoder"]
    train_keys = [g for g in train_groups if g in set(sample.groups.tolist())]
    n_int = int(max(2, min(3, len(train_keys))))
    sample_keys = sample.groups.astype(str)
    fold_models: list[FoldModel] = []
    if len(train_keys) >= 2:
        rng = np.random.default_rng(0)
        perm = list(rng.permutation(train_keys))
        for f in range(n_int):
            held = perm[f::n_int]
            rest = [k for k in perm if k not in set(held)]
            if not rest:
                continue
            fm = _fit_fold(f, rest, held, [], sample, sample_keys, baseline, inputs, settings, names, budget)
            fold_models.append(fm)
            if budget.fraction_used() > 0.6:
                break
    final = _fit_fold(-1, train_keys or list(dict.fromkeys(sample_keys.tolist())), [], [], sample, sample_keys, baseline, inputs, settings, names, budget)
    for n in final.detectors:
        vals = [m.thresholds[n] for m in fold_models if n in m.thresholds]
        if vals:
            final.thresholds[n] = float(np.median(vals))
    final.selected = list(final.detectors)
    final.ens_threshold = float(np.median([calibrate_ensemble_threshold(m, final.selected) for m in fold_models])) if fold_models else calibrate_ensemble_threshold(final, final.selected)
    final.reset_states()
    stability = {}
    for n in final.detectors:
        vals = [m.thresholds[n] for m in fold_models if n in m.thresholds]
        stability[n] = round(float(np.std(vals) / max(np.mean(vals), 1e-9)), 4) if len(vals) >= 2 else None
    idx = np.flatnonzero(np.isin(sample_keys, eval_groups))
    out: dict[str, Any] = {"n_train_groups": len(train_keys), "n_eval_groups": len(set(sample_keys[idx].tolist())), "n_fit_rows": final.n_fit_rows, "n_eval_rows": int(len(idx)), "detectors": list(final.detectors), "threshold_cv": stability, "threshold_cv_mean": round(float(np.mean([v for v in stability.values() if v is not None])), 4) if any(v is not None for v in stability.values()) else None, "baseline_strategy": baseline.strategy, "internal_folds": len(fold_models)}
    if len(idx) > 0:
        F = final.spec.transform(sample.X[idx], sample.groups[idx])
        res = final.score_features(F, sample.groups[idx])
        ens = res["ensemble"]
        out["flagged_fraction"] = round(float((ens >= 1.0).mean()), 4)
        out["mean_ensemble"] = round(float(ens.mean()), 4)
        names_ok = list(res["norm"])
        cors = []
        for i, a in enumerate(names_ok):
            for b in names_ok[i + 1 :]:
                try:
                    r = spearmanr(res["norm"][a], res["norm"][b]).correlation
                    if r is not None and not np.isnan(r):
                        cors.append(float(r))
                except Exception:
                    pass
        out["detector_agreement"] = round(float(np.mean(cors)), 4) if cors else None
        per_group = {}
        for s, e in segments(sample.groups[idx]):
            g = str(sample.groups[idx][s])
            per_group[g] = round(float((ens[s:e] >= 1.0).mean()), 4)
        out["flagged_fraction_by_group"] = per_group
        if inputs.label_columns:
            try:
                from .evaluate import _auroc, _lit
                from ._common import _arrow

                col = inputs.label_columns[0]
                con = ws.duckdb()
                normal = con.execute(f"SELECT CAST(\"{col}\" AS VARCHAR) v, COUNT(*) n FROM dataset GROUP BY v ORDER BY n DESC LIMIT 1").fetchone()[0]
                import pyarrow as pa

                con.register("__eval_rows", pa.table({"r": pa.array(sample.rows[idx].astype(np.int64))}))
                tbl = _arrow(con.execute(f"SELECT d.__row__, CASE WHEN CAST(d.\"{col}\" AS VARCHAR) IS DISTINCT FROM {_lit(normal)} THEN 1 ELSE 0 END AS abn FROM dataset d JOIN __eval_rows e ON d.__row__ = e.r ORDER BY d.__row__"))
                con.unregister("__eval_rows")
                lr = tbl.column("__row__").to_numpy()
                ab = tbl.column("abn").to_numpy()
                pos = np.searchsorted(lr, sample.rows[idx])
                pos = np.clip(pos, 0, len(lr) - 1)
                ok = lr[pos] == sample.rows[idx]
                auc = _auroc(ens[ok], ab[pos][ok]) if ok.any() else None
                out["auroc"] = None if auc is None else round(auc, 4)
                out["auroc_note"] = "evaluation only; labels never used for fitting"
            except Exception as e:
                out["auroc_error"] = str(e)[:200]
    out["seconds"] = round(time.time() - t_start, 2)
    ws.log.record("system:detect", "experiment", "dataset", "fit_score_subset", {k: v for k, v in out.items() if k != "flagged_fraction_by_group"})
    return out


# ------------------------------------------------------------------------------------------------
# human feedback
# ------------------------------------------------------------------------------------------------


def apply_override(ws, settings, decision: HumanDecision) -> dict[str, Any]:
    if decision.object_type == "pattern":
        name = (decision.new_value or {}).get("name") if decision.new_value else None
        if decision.action in ("name_pattern", "override", "accept") and name:
            res = name_pattern(ws, decision.object_id, str(name))
            ws.log.record("system:detect", "pattern_named", "pattern", decision.object_id, res)
            return res
        return {"pattern_id": decision.object_id, "note": "no name supplied"}
    if decision.object_type == "flag":
        status = {"accept": "accepted", "question": "questioned", "override": "overridden", "dismiss": "dismissed"}.get(decision.action)
        flags = ws.read_jsonl("flags")
        found = None
        for f in flags:
            if f.get("id") == decision.object_id:
                found = f
                if status:
                    f["human_status"] = status
                if decision.note is not None:
                    f["human_note"] = decision.note
                if decision.action == "override" and decision.new_value:
                    for k in ("likely_cause_class", "kind", "severity", "pattern_id", "confidence"):
                        if k in decision.new_value:
                            f[k] = decision.new_value[k]
        if found is None:
            return {"flag_id": decision.object_id, "found": False}
        ws.rewrite_jsonl("flags", flags)
        return {"flag_id": decision.object_id, "found": True, "human_status": found.get("human_status")}
    return {}
