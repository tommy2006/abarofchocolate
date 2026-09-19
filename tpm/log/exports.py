"""Export a run as a shareable bundle: reports (HTML, PDF, PowerPoint), decision log (JSONL), egress ledger, key JSON
artifacts and a chain-verification result, zipped. Never includes dataset.parquet / scores.parquet / the SQLite file.

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


EXPORT_FORMATS = ("pdf", "pptx")


def export_run(ws: Workspace, out_dir: str | Path, languages: Optional[list[str]] = None, make_reports: bool = True, use_llm: bool = False, make_exports: bool = True) -> Path:
    """Write <out_dir>/<run_id>_export/ with all files and <out_dir>/<run_id>_export.zip; returns the zip path.
    make_exports: also put the PDF and the PowerPoint deck of each requested language into the bundle (generated on
    demand and cached in the run; they never call the language model, so this also holds for make_reports=False)."""
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

    # PDF + PowerPoint of the requested languages (cached per language; regenerated when the artifacts changed)
    if make_exports:
        errors = []
        try:
            from ..report import ensure_export
        except Exception as e:  # reportlab / python-pptx missing: the bundle is still complete without them
            ensure_export = None
            errors.append(f"exports unavailable: {e}")
        for lang in (languages or [ws.settings.report.default_language]) if ensure_export else []:
            for fmt in EXPORT_FORMATS:
                try:
                    res = ensure_export(ws, ws.settings, lang, fmt)
                    shutil.copy2(res["path"], folder / Path(res["path"]).name)
                except Exception as e:
                    errors.append(f"{fmt} ({lang}): {e}")
        if errors:
            (folder / "EXPORT_ERROR.txt").write_text("\n".join(errors), encoding="utf-8")

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
