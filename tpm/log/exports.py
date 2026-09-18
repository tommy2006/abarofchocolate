"""Export a run as a shareable bundle: reports, decision log (JSONL), egress ledger, key JSON artifacts and a
chain-verification result, zipped. Never includes dataset.parquet / scores.parquet / the SQLite file itself.

    from tpm.log.exports import export_run
    zip_path = export_run(ws, out_dir)
"""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Any, Optional

from ..contracts import now_iso
from ..workspace import ARTIFACTS, Workspace, dumps

JSON_ARTIFACTS = ["meta", "status", "schema", "signals", "relations", "domain", "batches", "rules", "patterns", "baseline", "detect_meta", "evaluation", "assessor"]
JSONL_ARTIFACTS = ["evidence", "inferences", "checks", "trust", "flags", "diagnoses", "egress_ledger", "chat"]
EXCLUDED = {"dataset", "scores", "decision_log"}


def export_manifest(ws: Workspace) -> dict[str, Any]:
    present = {k: ws.exists(k) for k in ARTIFACTS if k not in EXCLUDED}
    return {"run_id": ws.run_id, "exported_at": now_iso(), "artifacts_present": present, "excluded": sorted(EXCLUDED), "note": "raw data (dataset.parquet, scores.parquet) is never exported; only derived artifacts"}


def export_run(ws: Workspace, out_dir: str | Path, languages: Optional[list[str]] = None, make_reports: bool = True, use_llm: bool = False) -> Path:
    """Write <out_dir>/<run_id>_export/ with all files and <out_dir>/<run_id>_export.zip; returns the zip path."""
    out_dir = Path(out_dir)
    folder = out_dir / f"{ws.run_id}_export"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True, exist_ok=True)

    # reports (generate the requested languages if missing)
    if make_reports:
        try:
            from ..report import generate_report, report_path

            langs = languages or [ws.settings.report.default_language]
            for lang in langs:
                p = report_path(ws, lang)
                if not p.exists():
                    generate_report(ws, ws.settings, lang, use_llm=use_llm)
        except Exception as e:  # reports are optional in the bundle
            (folder / "REPORT_ERROR.txt").write_text(str(e), encoding="utf-8")
    for p in sorted(ws.dir.glob("report_*.html")):
        shutil.copy2(p, folder / p.name)

    # decision log + verification
    ws.log.export_jsonl(folder / "decision_log.jsonl")
    verify = ws.log.verify_chain()
    (folder / "verify.json").write_text(dumps({"run_id": ws.run_id, "verified_at": now_iso(), **verify}, indent=1), encoding="utf-8")

    # derived artifacts
    for key in JSON_ARTIFACTS + JSONL_ARTIFACTS:
        p = ws.path(key)
        if p.exists():
            shutil.copy2(p, folder / p.name)
    if not (folder / "egress_ledger.jsonl").exists():
        (folder / "egress_ledger.jsonl").write_text("", encoding="utf-8")

    (folder / "manifest.json").write_text(dumps(export_manifest(ws), indent=1), encoding="utf-8")

    zip_path = out_dir / f"{ws.run_id}_export.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                z.write(p, arcname=str(p.relative_to(folder)))
    return zip_path


def list_export(zip_path: str | Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as z:
        return sorted(z.namelist())
