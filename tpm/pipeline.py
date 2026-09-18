"""Orchestrator. Runs stages in order, persisting status after each; supports batch processing and
human decisions. Stages are looked up by dotted path so agents can develop them independently; a
missing stage is recorded as 'skipped', a failing stage as 'failed' (later stages are skipped)."""
from __future__ import annotations

import importlib
import time
import traceback
from typing import Any, Callable, Optional

from .config import Settings, get_settings
from .contracts import HumanDecision, RunStatus, StageStatus, now_iso
from .workspace import Workspace

STAGES: list[tuple[str, str]] = [
    ("ingest", "tpm.ingest:run_ingest"),
    ("profile", "tpm.profile:run_profile"),
    ("quality", "tpm.quality:run_quality"),
    ("detect", "tpm.detect:run_detect"),
    ("diagnose", "tpm.diagnose:run_diagnose"),
    ("assess", "tpm.assessor:run_assess"),
    ("report", "tpm.report:run_report"),
]

ProgressCb = Callable[[str, float, str], None]


def _resolve(dotted: str) -> Optional[Callable[..., Any]]:
    mod_name, _, fn_name = dotted.partition(":")
    try:
        mod = importlib.import_module(mod_name)
    except Exception:
        return None
    return getattr(mod, fn_name, None)


def run_pipeline(
    source_path: str,
    run_id: Optional[str] = None,
    profile: Optional[str] = None,
    options: Optional[dict[str, Any]] = None,
    progress_cb: Optional[ProgressCb] = None,
    stages: Optional[list[str]] = None,
    settings: Optional[Settings] = None,
    continue_on_error: bool = False,
) -> RunStatus:
    settings = (settings or get_settings())
    if profile:
        settings = settings.with_profile(profile)
    ws = Workspace(run_id=run_id, settings=settings)
    options = options or {}
    wanted = [s for s, _ in STAGES if not stages or s in stages]

    status = RunStatus(run_id=ws.run_id, source_path=str(source_path), profile=settings.profile, state="running", options=options, stages=[StageStatus(stage=s, state="pending") for s, _ in STAGES])
    ws.set_status(status)
    ws.write_json("meta", {"run_id": ws.run_id, "source_path": str(source_path), "profile": settings.profile, "options": options, "created_at": now_iso(), "settings_snapshot": settings.model_dump(exclude={"profiles"})})
    ws.log.record("system:pipeline", "run_started", "run", ws.run_id, {"source_path": str(source_path), "profile": settings.profile, "options": options})

    t_start = time.time()
    failed = False
    for stage, dotted in STAGES:
        if stage not in wanted:
            ws.update_stage(stage, state="skipped", message="not requested")
            continue
        if failed and not continue_on_error:
            ws.update_stage(stage, state="skipped", message="previous stage failed")
            continue
        fn = _resolve(dotted)
        if fn is None:
            ws.update_stage(stage, state="skipped", message="not implemented yet")
            ws.log.record("system:pipeline", "stage", "stage", stage, {"state": "skipped", "reason": "not implemented"})
            continue

        def progress(fraction: float, message: str = "", _stage: str = stage) -> None:
            ws.update_stage(_stage, progress=fraction, message=message)
            if progress_cb:
                progress_cb(_stage, fraction, message)

        ctx = {"source_path": str(source_path), "options": options, "progress": progress, "run_id": ws.run_id, "t_start": t_start, "time_budget_s": settings.time_budget_s}
        ws.update_stage(stage, state="running", progress=0.0, message="starting")
        ws.log.record("system:pipeline", "stage", "stage", stage, {"state": "running"})
        t0 = time.time()
        try:
            result = fn(ws, settings, ctx) or {}
            ws.update_stage(stage, state="done", progress=1.0, message=str(result.get("message", "done"))[:500])
            ws.log.record("system:pipeline", "stage", "stage", stage, {"state": "done", "seconds": round(time.time() - t0, 1), "summary": {k: v for k, v in result.items() if k != "message"}})
        except Exception as e:
            tb = traceback.format_exc()
            ws.update_stage(stage, state="failed", message=str(e)[:500], error=tb[-4000:])
            ws.log.record("system:pipeline", "stage", "stage", stage, {"state": "failed", "error": str(e)[:1000]})
            failed = True

    st = ws.status()
    st.state = "failed" if failed else "done"
    ws.set_status(st)
    ws.log.record("system:pipeline", "run_finished", "run", ws.run_id, {"state": st.state, "seconds": round(time.time() - t_start, 1)})
    return st


def process_batch(ws: Workspace, settings: Settings, batch_df: Any, batch_id: str, ctx: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Stream path: DQ checks + trust -> scoring/flags -> diagnoses for one incoming batch."""
    ctx = ctx or {}
    out: dict[str, Any] = {"batch_id": batch_id}
    check_batch = _resolve("tpm.quality:check_batch")
    score_batch = _resolve("tpm.detect:score_batch")
    diagnose_flags = _resolve("tpm.diagnose:diagnose_flags")

    trust = None
    if check_batch:
        checks, trust = check_batch(ws, settings, batch_df, batch_id)
        out["checks"] = [c.check_id for c in checks]
        out["trust"] = trust.model_dump() if trust else None
    flags = []
    if score_batch:
        flags = score_batch(ws, settings, batch_df, batch_id, trust)
        out["flags"] = [f.id for f in flags]
    if diagnose_flags and flags:
        # Streaming path: template narratives only, so a batch never waits on a model call.
        # LLM-written explanations stay available on demand through the operator chat.
        try:
            diags = diagnose_flags(ws, settings, flags, use_llm=False)
        except TypeError:
            diags = diagnose_flags(ws, settings, flags)
        out["diagnoses"] = [d.id for d in diags]
    ws.log.record("system:pipeline", "batch_processed", "batch", batch_id, {k: v for k, v in out.items() if k != "trust"})
    return out


def apply_decision(ws: Workspace, settings: Settings, decision: HumanDecision) -> dict[str, Any]:
    """Record a human decision and let the owning module react (feed-back + log)."""
    actor = f"human:{decision.actor_name}({decision.role})"
    entry = ws.log.record(actor, decision.action, decision.object_type, decision.object_id, {"note": decision.note, "new_value": decision.new_value})
    owner = {
        "inference": "tpm.profile:apply_override",
        "signal": "tpm.profile:apply_override",
        "schema": "tpm.ingest:apply_override",
        "flag": "tpm.detect:apply_override",
        "pattern": "tpm.detect:apply_override",
        "diagnosis": "tpm.diagnose:apply_override",
        "rule": "tpm.quality:apply_override",
        "assessor": "tpm.assessor:apply_override",
    }.get(decision.object_type)
    result: dict[str, Any] = {"log_seq": entry.seq}
    if owner:
        fn = _resolve(owner)
        if fn:
            try:
                result["effect"] = fn(ws, settings, decision) or {}
            except Exception as e:
                result["effect_error"] = str(e)
    # generic status update on registries the foundation owns
    if decision.object_type == "inference":
        inf = ws.inferences.get(decision.object_id)
        if inf:
            inf.human_status = {"accept": "accepted", "question": "questioned", "override": "overridden"}.get(decision.action, inf.human_status)
            inf.human_note = decision.note
            ws.inferences.update(inf)
    return result
