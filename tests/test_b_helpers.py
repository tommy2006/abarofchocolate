"""Helpers for agent B tests: build a workspace (dataset.parquet, schema.json, signals.json, relations.json)
from the shared synthetic generator without depending on agent A's ingest/profile code.

Not a test module (no test_ functions); the name keeps it inside agent B's file ownership.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from tpm.config import Settings, load_settings
from tpm.contracts import DatasetSchema, SignalDescriptor
from tpm.workspace import Workspace

META = ("run", "sample", "timestamp", "fault_label")


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    s = load_settings()
    s.workspace_dir = str(tmp_path / "workspace")
    s.batch.min_rows = 50
    s.assessor.experiment_time_budget_s = 20
    s.assessor.learning_curve_fractions = [0.25, 0.5, 1.0]
    for k, v in overrides.items():
        parts = k.split("__")
        obj = s
        for p in parts[:-1]:
            obj = getattr(obj, p)
        setattr(obj, parts[-1], v)
    return s


def alias_map(df: pd.DataFrame, signal_columns: list[str]) -> dict[str, str]:
    return {c: f"S{i + 1:02d}" for i, c in enumerate(signal_columns)}


def fingerprint(x: pd.Series) -> dict[str, Any]:
    v = pd.to_numeric(x, errors="coerce").to_numpy(dtype="float64")
    v = v[np.isfinite(v)]
    if v.size == 0:
        return {"count": 0}
    med = float(np.median(v))
    starts = np.flatnonzero(np.concatenate([[True], v[1:] != v[:-1]]))
    lengths = np.diff(np.append(starts, v.size))
    return {
        "count": int(v.size), "mean": float(v.mean()), "std": float(v.std()), "median": med, "mad": float(np.median(np.abs(v - med))),
        "min": float(v.min()), "max": float(v.max()), "q01": float(np.percentile(v, 1)), "q05": float(np.percentile(v, 5)),
        "q25": float(np.percentile(v, 25)), "q75": float(np.percentile(v, 75)), "q95": float(np.percentile(v, 95)), "q99": float(np.percentile(v, 99)),
        "stuck_fraction": float(lengths[lengths >= 30].sum() / v.size), "hold_period": float(np.median(lengths)) if np.median(lengths) > 1.5 else 1.0,
    }


def build_workspace(tmp_path: Path, settings: Settings, df: pd.DataFrame, truth: dict[str, Any], run_id: str = "run_test", roles: Optional[dict[str, str]] = None, with_signals: bool = True, with_relations: bool = True) -> Workspace:
    ws = Workspace(run_id=run_id, settings=settings)
    signal_columns = [c for c in df.columns if c not in META]
    aliases = alias_map(df, signal_columns)
    data = df.copy()
    data.insert(0, "__row__", np.arange(len(data), dtype="int64"))
    grp = data["run"].astype(str) if "run" in data.columns else pd.Series(["0"] * len(data))
    data.insert(1, "__group__", grp.to_numpy())
    data.to_parquet(ws.path("dataset"), index=False)
    roles = roles or truth.get("signal_roles", {})
    schema = DatasetSchema(
        dataset_id=run_id, source_path="synthetic", format="parquet", n_rows=len(data), n_cols=len(df.columns), had_header=True,
        columns=list(data.columns), time_column="timestamp" if "timestamp" in df.columns else None,
        sample_period_seconds=180.0 if "timestamp" in df.columns else None, order_column="sample" if "sample" in df.columns else None,
        group_columns=["run"] if "run" in df.columns else [], grouping_method="key_columns", label_columns=["fault_label"] if "fault_label" in df.columns else [],
        meta_columns=[c for c in ("run", "sample") if c in df.columns], signal_columns=signal_columns, signal_alias=aliases,
        n_groups=int(grp.nunique()),
    )
    ws.write_json("schema", schema)
    if with_signals:
        sigs = []
        for i, c in enumerate(signal_columns):
            role = roles.get(c, "unknown")
            fp = fingerprint(df[c])
            if role == "held_sampled":
                fp["hold_period"] = 6
            sigs.append(SignalDescriptor(id=aliases[c], source_column=c, column_index=list(df.columns).index(c), dtype=str(df[c].dtype), structural_role=role, structural_confidence=0.9, fingerprint=fp, excluded=(role == "constant"), excluded_reason="constant" if role == "constant" else None))
        ws.write_json("signals", sigs)
    if with_relations and "derived_sum" in aliases and "flow_a" in aliases and "flow_b" in aliases:
        rel = {
            "pairs": [{"a": aliases["derived_sum"], "b": aliases["flow_a"], "r": float(df["derived_sum"].corr(df["flow_a"])), "lag": 0, "kind": "redundant"}],
            "derived": [{"signal": aliases["derived_sum"], "of": [aliases["flow_a"], aliases["flow_b"]], "coef": [1.0, 1.0], "intercept": 0.0}],
        }
        ws.write_json("relations", rel)
    return ws


def batch_of_row(batches: list[dict[str, Any]], row: int) -> Optional[dict[str, Any]]:
    """Batches are half-open: row_start <= row < row_end."""
    for b in batches:
        if b["row_start"] <= row < b["row_end"]:
            return b
    return None
