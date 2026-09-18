"""ingest stage: any supported file (or a DataFrame) -> ``dataset.parquet`` (+ ``__row__``, ``__group__``),
``schema.json`` (DatasetSchema), ``groups.json``, ``batches.json``, evidence, inferences and log entries.

    from tpm.ingest import run_ingest, ingest_dataframe, apply_override
    summary = run_ingest(ws, settings, {"source_path": "data.csv", "options": {...}, "progress": cb})

Options (``ctx["options"]``): has_header, delimiter, decimal, encoding, transposed, sheet, format,
group_columns, time_column, domain_hint. All optional; everything is inferred when absent.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from ..memory import memory_snapshot
from .readers import convert_to_parquet, dataframe_to_parquet, detect_format
from .schema import ACTOR, apply_override, infer_schema  # noqa: F401  (apply_override re-exported for pipeline.apply_decision)
from .stream import build_batches

__all__ = ["run_ingest", "ingest_dataframe", "apply_override", "detect_format"]


def _progress_fn(ctx: dict[str, Any]):
    fn = ctx.get("progress")

    def progress(fraction: float, message: str = "") -> None:
        if fn:
            try:
                fn(float(max(0.0, min(1.0, fraction))), message)
            except Exception:
                pass

    return progress


def _finish(ws, settings, ctx: dict[str, Any], conv: dict[str, Any], fmt: dict[str, Any], t0: float, progress) -> dict[str, Any]:
    ws.log.record(ACTOR, "converted", "dataset", ws.run_id, {"n_rows": conv["n_rows"], "n_cols": conv["n_cols"], "format": fmt.get("format"), "attempt": conv.get("attempt"), "seconds": conv.get("seconds"), "row_order_verified": conv.get("row_order_verified"), "notes": conv.get("notes", []), "memory": memory_snapshot()})
    progress(0.6, "inferring schema")
    schema = infer_schema(ws, settings, ctx, conv, fmt, progress=lambda f, m: progress(0.6 + 0.35 * f, m))
    ws.write_json("schema", schema)
    progress(0.96, "planning batches")
    try:
        batches = build_batches(ws, settings, schema=schema)
        n_batches = len(batches)
    except Exception as e:  # batches are a convenience here; stream.iter_batches rebuilds them on demand
        n_batches = 0
        ws.log.record(ACTOR, "warning", "dataset", ws.run_id, {"message": f"batch planning failed: {e}"})
    for inf_id in schema.inference_ids:
        inf = ws.inferences.get(inf_id)
        if inf:
            ws.log.record(ACTOR, "inference", "inference", inf_id, {"subject": inf.subject, "claim": inf.claim, "status": inf.status, "confidence": inf.confidence}, evidence_ids=inf.evidence_ids)
    summary = {
        "n_rows": schema.n_rows,
        "n_cols": schema.n_cols,
        "n_groups": schema.n_groups,
        "grouping_method": schema.grouping_method,
        "group_columns": schema.group_columns,
        "format": schema.format,
        "had_header": schema.had_header,
        "transposed": schema.transposed,
        "time_column": schema.time_column,
        "sample_period_seconds": schema.sample_period_seconds,
        "n_signals": len(schema.signal_columns),
        "label_columns": schema.label_columns,
        "meta_columns": schema.meta_columns,
        "domain_likelihood": schema.domain_likelihood,
        "n_batches": n_batches,
        "seconds": round(time.time() - t0, 2),
    }
    summary["message"] = f"{schema.n_rows} rows x {schema.n_cols} cols, {len(schema.signal_columns)} signals, {schema.n_groups} groups ({schema.grouping_method}), {schema.format}"
    ws.log.record(ACTOR, "schema", "schema", ws.run_id, {k: v for k, v in summary.items() if k != "message"}, evidence_ids=schema.evidence_ids[:50])
    progress(1.0, summary["message"])
    return summary


def run_ingest(ws, settings, ctx: dict[str, Any]) -> dict[str, Any]:
    """Pipeline stage entry point. ``ctx["source_path"]`` is the file; ``ctx["dataframe"]`` (optional)
    bypasses the readers for API uploads."""
    t0 = time.time()
    progress = _progress_fn(ctx)
    options = dict(ctx.get("options") or {})
    if ctx.get("dataframe") is not None:
        return ingest_dataframe(ws, settings, ctx["dataframe"], ctx=ctx)
    source = Path(str(ctx["source_path"]))
    if not source.exists():
        raise FileNotFoundError(f"source not found: {source}")
    progress(0.0, "detecting format")
    fmt = detect_format(source, options)
    for item in fmt.get("evidence", []):
        ws.evidence.add(item.get("kind", "format"), item["statement"], values=item.get("values", {}), computed_by="ingest.readers.detect_format", n_samples=fmt.get("n_lines_sampled"))
    ws.log.record(ACTOR, "format_detected", "dataset", ws.run_id, {k: v for k, v in fmt.items() if k not in ("evidence", "header_names")})
    if fmt.get("transposed"):
        ws.inferences.add("dataset", "the file stores signals in rows (transposed); it was transposed on ingest", status="inferred", confidence=0.8, evidence_ids=[e.id for e in ws.evidence.all() if e.kind == "orientation"][-1:], reasoning="lag-1 autocorrelation is much higher along rows than down columns", source="code", stage="ingest", alternatives=["columns are signals (operator can set options.transposed=false)"])
    progress(0.05, f"converting {fmt.get('format')} to parquet")
    conv = convert_to_parquet(source, fmt, ws.path("dataset"), settings, progress=lambda f, m: progress(0.05 + 0.55 * f, m), con=ws.duckdb())
    if fmt.get("transposed"):
        ws.evidence.add("orientation", f"transposed on ingest: {conv['n_cols']} signals (file rows) x {conv['n_rows']} samples (file columns)", values={"n_rows": conv["n_rows"], "n_cols": conv["n_cols"]}, computed_by="ingest.readers.convert_to_parquet", n_samples=conv["n_rows"])
    ws.duckdb()  # (re)create the dataset view
    return _finish(ws, settings, ctx, conv, fmt, t0, progress)


def ingest_dataframe(ws, settings, df, ctx: Optional[dict[str, Any]] = None, name: str = "dataframe") -> dict[str, Any]:
    """Small-data path (API uploads / assembled batches): DataFrame -> the same artifacts as run_ingest."""
    t0 = time.time()
    ctx = dict(ctx or {})
    ctx.setdefault("source_path", name)
    ctx.setdefault("options", {})
    progress = _progress_fn(ctx)
    progress(0.0, "writing dataframe")
    conv = dataframe_to_parquet(df, ws.path("dataset"), settings)
    fmt = {"format": "dataframe", "has_header": True, "transposed": False, "delimiter": None}
    ws.log.record(ACTOR, "format_detected", "dataset", ws.run_id, {"format": "dataframe", "n_rows": conv["n_rows"], "n_cols": conv["n_cols"]})
    ws.duckdb()
    return _finish(ws, settings, ctx, conv, fmt, t0, progress)
