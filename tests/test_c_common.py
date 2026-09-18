"""Shared helpers for agent C tests: build a workspace from the synthetic fixture without the other stages.

Writes dataset.parquet (__row__, __group__ + original columns), schema.json, signals.json, relations.json,
batches.json and trust.jsonl the way agents A/B are expected to (minimal, computed simply here).
"""
from __future__ import annotations

import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fixtures.synth import make_synthetic  # noqa: E402
from tpm.config import load_settings  # noqa: E402
from tpm.contracts import DatasetSchema, SignalDescriptor, TrustVerdict  # noqa: E402
from tpm.workspace import Workspace  # noqa: E402


def make_settings(workspace_dir: Path):
    s = load_settings()
    s.workspace_dir = str(workspace_dir)
    s.detect.max_fit_rows = 20000
    s.detect.time_budget_s = 120
    s.detect.n_folds = 3
    s.detect.pattern_min_events = 2  # 24 groups over 6 fault types: ~2 events per type
    s.time_budget_s = 600
    return s


def build_workspace(root: Path, n_groups: int = 24, n_samples: int = 400, seed: int = 0, with_labels: bool = False, write_relations: bool = True, write_trust: bool = True, run_id: str = "synth") -> tuple[Workspace, Any, pd.DataFrame]:
    df, truth = make_synthetic(n_groups=n_groups, n_samples=n_samples, seed=seed, with_labels=with_labels)
    settings = make_settings(root / "workspace")
    ws = Workspace(run_id=run_id, settings=settings)
    signal_cols = list(truth["signal_columns"])
    out = df.copy()
    out.insert(0, "__group__", df["run"].astype(str))
    out.insert(0, "__row__", np.arange(len(df), dtype=np.int64))
    for c in signal_cols:
        out[c] = out[c].astype(np.float32)
    out.to_parquet(ws.path("dataset"), index=False)
    alias = {c: f"S{i + 1:02d}" for i, c in enumerate(signal_cols)}
    schema = DatasetSchema(dataset_id="synth", source_path="synthetic", format="parquet", n_rows=len(out), n_cols=df.shape[1], had_header=True, columns=list(out.columns), group_columns=["run"], group_column="__group__", grouping_method="key_columns", label_columns=["fault_label"] if with_labels else [], meta_columns=["run", "sample"], signal_columns=signal_cols, signal_alias=alias, n_groups=n_groups, order_column="sample")
    ws.write_json("schema", schema.model_dump())
    # relations (simple, via the detect fallback) and signals
    from tpm.detect._common import compute_relations

    X = out[signal_cols].to_numpy(dtype=np.float32)
    aliases = [alias[c] for c in signal_cols]
    rel = compute_relations(X[:20000], aliases, None)
    cluster_of = {m: cid for cid, ms in rel["clusters"].items() for m in ms}
    sigs = []
    for i, c in enumerate(signal_cols):
        col = out[c].to_numpy(dtype=np.float64)
        finite = col[np.isfinite(col)]
        role = "constant" if finite.size and np.nanstd(finite) < 1e-9 else "unknown"
        fp = {"mean": float(np.nanmean(finite)) if finite.size else None, "std": float(np.nanstd(finite)) if finite.size else None, "quantiles": [float(q) for q in np.nanquantile(finite, [0.01, 0.25, 0.5, 0.75, 0.99])] if finite.size else [], "stuck_fraction": 0.0, "hold_period": 1}
        sigs.append(SignalDescriptor(id=alias[c], source_column=c, column_index=list(out.columns).index(c), dtype="float32", structural_role=role, structural_confidence=0.9 if role == "constant" else 0.3, cluster_id=cluster_of.get(alias[c]), fingerprint=fp, excluded=role == "constant", excluded_reason="constant" if role == "constant" else None))
    ws.write_json("signals", [s.model_dump() for s in sigs])
    if write_relations:
        ws.write_json("relations", rel)
    # batches: 10 % of rows each; trust: untrusted power_c where the unit shift lives
    n = len(out)
    size = max(100, n // 10)
    batches = []
    b = 0
    for s in range(0, n, size):
        b += 1
        batches.append({"batch_id": f"B{b:04d}", "row_start": int(s), "row_end": int(min(n, s + size)), "n_rows": int(min(n, s + size) - s)})
    ws.write_json("batches", batches)
    if write_trust:
        dq = {d["type"]: d for d in truth["dq"]}
        us = dq.get("unit_shift")
        for bt in batches:
            untrusted = []
            if us and not (bt["row_end"] <= us["row_start"] or bt["row_start"] > us["row_end"]):
                untrusted.append(alias[us["signal"]])
            v = TrustVerdict(batch_id=bt["batch_id"], trusted=not untrusted, trust_score=0.4 if untrusted else 0.95, untrusted_signals=untrusted, reasons=["unit shift detected"] if untrusted else [], statement="ok" if not untrusted else "power signal x1000 unit shift")
            ws.append_jsonl("trust", v.model_dump())
    truth["alias"] = alias
    truth["group_of_row"] = out["__group__"].to_numpy()
    truth["row_of_group_start"] = {str(g): int(out.index[out["__group__"] == str(g)][0]) for g in out["__group__"].unique()}
    return ws, truth, out


@lru_cache(maxsize=None)
def detect_run(with_labels: bool = False) -> tuple[Any, Any, Any, dict[str, Any]]:
    """Run detect once per session and cache (ws, truth, df, summary)."""
    root = Path(tempfile.mkdtemp(prefix="tpm_c_"))
    ws, truth, df = build_workspace(root, n_groups=24, n_samples=400, seed=0, with_labels=with_labels)
    from tpm.detect import run_detect

    ctx = {"source_path": "synthetic", "options": {}, "run_id": ws.run_id, "t_start": None, "time_budget_s": None}
    summary = run_detect(ws, ws.settings, ctx)
    return ws, truth, df, summary


def truth_onset_row(truth: dict[str, Any], group: str) -> int | None:
    """Dataset row of the true onset for a group (before the duplicate-row insertion, row ids are exact:
    the duplicate block is appended after 85 % of rows, so groups before it are unaffected)."""
    info = truth["groups"].get(str(int(group) - 1))
    if not info or info["onset"] is None:
        return None
    return truth["row_of_group_start"][group] + int(info["onset"])
