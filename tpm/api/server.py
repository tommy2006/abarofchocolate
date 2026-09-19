"""FastAPI server for the Trustworthy Process Monitor.

    uvicorn tpm.api.server:app --port 8765

Every stage function that another agent owns is imported lazily; when it is missing the handler answers
HTTP 501 ``{"unavailable": "module.function", "available": false}`` so the UI can render a clear notice
instead of crashing. Artifact readers never raise on a missing file: they answer ``available: false``.
"""
from __future__ import annotations

import asyncio
import csv
import functools
import importlib
import io
import json
import re
import shutil
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import Settings, load_settings, save_settings_overrides
from ..contracts import HumanDecision, Rule, RunStatus, StageStatus, now_iso
from ..workspace import ARTIFACTS, Workspace, dumps
from . import fallback

STATIC = Path(__file__).resolve().parent / "static"
VENDOR = STATIC / "vendor"
STAGE_NAMES = ["ingest", "profile", "quality", "detect", "diagnose", "assess", "report"]
ARTIFACT_JSON = {"schema", "signals", "relations", "domain", "batches", "rules", "patterns", "baseline", "detect_meta", "evaluation", "assessor"}
ARTIFACT_JSONL = {"checks", "trust", "flags", "diagnoses", "egress_ledger", "chat"}


# --------------------------------------------------------------------------------------- helpers
def _lazy(dotted: str) -> Optional[Callable[..., Any]]:
    mod_name, _, fn_name = dotted.partition(":")
    try:
        mod = importlib.import_module(mod_name)
    except Exception:
        return None
    fn = getattr(mod, fn_name, None)
    return fn if callable(fn) else None


def _unavailable(dotted: str, message: str = "") -> JSONResponse:
    name = dotted.replace(":", ".")
    return JSONResponse({"unavailable": name, "available": False, "message": message or f"{name} is not implemented in this build yet."}, status_code=501)


def _jsonable(obj: Any) -> Any:
    return json.loads(dumps(obj))


def _ensure_plotly() -> None:
    """Serve plotly.min.js from the installed Python package (offline, no CDN)."""
    try:
        VENDOR.mkdir(parents=True, exist_ok=True)
        target = VENDOR / "plotly.min.js"
        if not target.exists() or target.stat().st_size < 1000:
            import plotly.offline as po

            target.write_text(po.get_plotlyjs(), encoding="utf-8")
    except Exception:
        pass


def _rewrite_profile_line(settings_path: Optional[Path], profile: str) -> bool:
    """Replace the top-level ``profile:`` line of settings.yaml in place (keeps comments). False if not found."""
    import re

    from ..config import _settings_path

    p = Path(settings_path) if settings_path else _settings_path()
    if not p.exists():
        return False
    text = p.read_text(encoding="utf-8")
    new, n = re.subn(r"(?m)^profile:\s*[^\s#]+", f"profile: {profile}", text, count=1)
    if n != 1:
        return False
    p.write_text(new, encoding="utf-8")
    return True


def _rewrite_external_model_line(settings_path: Optional[Path], model: str) -> bool:
    """Replace ``model:`` inside the top-level ``external_llm:`` block of settings.yaml in place (keeps comments).
    False if the block or the line is not found, or the id would need YAML quoting."""
    from ..config import _settings_path

    p = Path(settings_path) if settings_path else _settings_path()
    if not p.exists() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model):
        return False
    text = p.read_text(encoding="utf-8")
    block = re.search(r"(?m)^external_llm:[ \t]*(?:#.*)?\n((?:[ \t]+.*\n?|[ \t]*\n)*)", text)
    if not block:
        return False
    new, n = re.subn(r"(?m)^([ \t]+model:[ \t]*)[^\s#]+", lambda m: m.group(1) + model, block.group(1), count=1)
    if n != 1:
        return False
    p.write_text(text[: block.start(1)] + new + text[block.end(1):], encoding="utf-8")
    return True


def _sanitizer_totals(ledger: list[dict[str, Any]]) -> dict[str, Any]:
    """What the egress guard changed in the payloads it cleared for the external model (sent, or failed on the wire),
    summed over a run's ledger: three plain totals for the Data-flow view plus the guard's own counters (`detail`).
    Blocked and budget-refused payloads never left and are not counted."""
    detail: dict[str, int] = {}
    sent = [r for r in ledger if r.get("route") == "external" and r.get("guard_result") == "allowed"]
    for r in sent:
        for k, v in (r.get("sanitizer") or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and k != "vocabulary_size":
                detail[k] = detail.get(k, 0) + int(v)
    total = lambda *keys: int(sum(detail.get(k, 0) for k in keys))  # noqa: E731
    return {
        "payloads": len(sent),
        "numbers_rounded": total("floats_rounded", "numbers_in_strings_rounded"),
        "names_aliased": total("names_aliased"),
        "values_withheld": total("values_redacted", "times_redacted", "files_redacted", "keys_dropped", "fields_dropped", "items_dropped", "human_notes_dropped"),
        "detail": detail,
    }


def _external_model_state(s: Settings) -> dict[str, Any]:
    """Which external models the UI may offer, and why the external route cannot be used right now (None = usable).
    Same answers as tpm.llm.available(), without probing the local model server."""
    try:
        from ..llm.router import EXTERNAL_MODEL_CHOICES as choices
    except Exception:
        choices = ["claude-sonnet-5", "claude-opus-5"]
    cfg = s.external_llm
    configured = [cfg.model] + [m for m in (cfg.model_by_task or {}).values() if m]
    blocked = next((why for ok, why in (cfg.model_allowed(m) for m in configured) if not ok), None)
    unavailable = s.external_block_reason()
    if unavailable is None and not cfg.api_key:
        unavailable = f"no API key in env {cfg.api_key_env}"
    return {
        "external_models_allowed": [m for m in choices if cfg.model_allowed(m)[0]],
        "external_model_blocked_reason": blocked,
        "external_unavailable_reason": unavailable,
    }


def _external_caps(s: Settings) -> dict[str, Any]:
    """The limits on external-model use from the settings (per run, per chat turn, concurrency)."""
    return {k: getattr(s.external_llm, k) for k in ("max_calls_per_run", "max_calls_per_chat_turn", "max_output_tokens_per_run", "max_parallel", "max_narratives_per_run", "timeout_s")}


def _benchmark_rows(data: Any) -> list[dict[str, Any]]:
    """Rows of the latency table from llm_benchmark.json (`tpm bench-llm`): one per task with the average seconds
    per call on the local and on the external route. Tolerant about the file's exact shape; [] when it has none."""

    def seconds(side: Any) -> Optional[float]:
        if isinstance(side, (int, float)) and not isinstance(side, bool):
            return round(float(side) / 1000.0, 2)  # a bare number is milliseconds, like the ledger's latency_ms
        if not isinstance(side, dict):
            return None
        for key, div in (("avg_latency_ms", 1000.0), ("mean_latency_ms", 1000.0), ("avg_ms", 1000.0), ("mean_ms", 1000.0), ("latency_ms", 1000.0), ("avg_s", 1.0), ("mean_s", 1.0), ("seconds", 1.0)):
            v = side.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return round(float(v) / div, 2)
        return None

    tasks = data.get("tasks") if isinstance(data, dict) else None
    if isinstance(tasks, dict):
        tasks = [dict(v, task=k) for k, v in tasks.items() if isinstance(v, dict)]
    if isinstance(tasks, list) and any(isinstance(t, dict) and t.get("route") in ("local", "external") for t in tasks):
        # tpm.llm.bench writes one row per task AND route ({task, route, n, ok, mean_s, ...}): fold them per task
        folded: dict[str, dict[str, Any]] = {}
        for t in tasks:
            if isinstance(t, dict) and t.get("route") in ("local", "external"):
                item = folded.setdefault(str(t.get("task") or "?"), {"task": t.get("task"), "n": t.get("n")})
                item[t["route"]] = t
                item["n"] = max(int(item.get("n") or 0), int(t.get("n") or 0))
        tasks = list(folded.values())
    rows: list[dict[str, Any]] = []
    for item in tasks if isinstance(tasks, list) else []:
        if not isinstance(item, dict):
            continue
        local_s, external_s = seconds(item.get("local")), seconds(item.get("external"))
        n_of = lambda side: (item.get(side) or {}).get("ok", (item.get(side) or {}).get("n")) if isinstance(item.get(side), dict) else None  # noqa: E731
        rows.append({
            "task": str(item.get("task") or "?"),
            "n": item.get("n"),
            "local_s": local_s,
            "external_s": external_s,
            "local_ok": n_of("local"),
            "external_ok": n_of("external"),
            "speedup": round(local_s / external_s, 1) if local_s and external_s else None,
        })
    return rows


def _offload(handler):
    """Run a blocking async handler OFF the server's event loop.

    These routes are `async def` only because they read the request body; after that they call blocking code
    (local-model calls, DuckDB, file writes, SMTP). Executed on the event loop, one such request froze the whole
    server for every client until it returned (a chat question waiting on the model made the UI look dead).
    The body is read and cached on the server loop, then the handler coroutine runs in a worker thread with its
    own short-lived loop; `await request.json()` / `request.form()` there only touch the cached body."""

    @functools.wraps(handler)
    async def wrapper(*args, **kwargs):
        req = kwargs.get("request")
        if req is None:
            req = next((a for a in args if isinstance(a, Request)), None)
        if req is not None:
            await req.body()
        return await asyncio.to_thread(lambda: asyncio.run(handler(*args, **kwargs)))

    return wrapper


_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")


def _check_run_id(run_id: Optional[str]) -> Optional[str]:
    """A client-chosen run id becomes a folder name under the workspace: letters, digits, '_', '-', '.' only,
    no separators, no '..'."""
    if run_id is None:
        return None
    run_id = str(run_id)
    if not _RUN_ID_RE.fullmatch(run_id) or ".." in run_id:
        raise HTTPException(400, "run_id may contain only letters, digits, '_', '-' and '.', must start with a letter or digit, and be at most 80 characters")
    return run_id


def _heal_orphan(ws: Workspace, jobs: dict[str, Any]) -> bool:
    """A run left 'running' by a process that no longer exists (server stopped, machine restarted) is marked
    failed/interrupted so the UI does not show it as running forever. Runs owned by a live process (this
    server's job threads, or a CLI run in another process) are left alone."""
    st = ws.status()
    if st.state not in ("running", "pending"):
        return False
    job = jobs.get(ws.run_id)
    th = job.get("thread") if job else None
    if th is not None and th.is_alive():
        return False
    meta = ws.read_json("meta") or {}
    alive = False
    try:
        import psutil
        from datetime import datetime

        pid = meta.get("pid")
        if pid and psutil.pid_exists(int(pid)):
            created = datetime.fromisoformat(str(meta.get("created_at"))).timestamp()
            alive = psutil.Process(int(pid)).create_time() <= created + 5 and int(pid) != __import__("os").getpid()
        elif not pid:
            upd = datetime.fromisoformat(str(st.updated_at)).timestamp()
            alive = (time.time() - upd) < 900  # older runs carry no pid: give a live CLI run 15 minutes between updates
    except Exception:
        alive = False
    if alive:
        return False
    for sg in st.stages:
        if sg.state == "running":
            sg.state = "failed"
            sg.message = "interrupted"
            sg.error = "The process running this analysis stopped before the stage finished."
    st.state = "failed"
    st.error = "Interrupted: the process that was running this analysis is no longer alive (server stopped or machine restarted). Stages that finished are still available; start the run again to complete it."
    ws.set_status(st)
    try:
        ws.log.record("system:api", "run_interrupted", "run", ws.run_id, {"reason": "owning process not alive"})
    except Exception:
        pass
    return True


class _State:
    """Process-wide registry: settings, open workspaces, background jobs, event ring buffers."""

    def __init__(self, settings: Settings, settings_path: Optional[Path]):
        self.settings = settings
        self.settings_path = settings_path
        self.lock = threading.RLock()
        self.workspaces: dict[str, Workspace] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: dict[str, deque] = {}
        self.stream: dict[str, dict[str, Any]] = {}
        self.event_seq = 0

    def ws(self, run_id: str) -> Workspace:
        if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
            raise HTTPException(404, f"unknown run {run_id!r}")
        with self.lock:
            w = self.workspaces.get(run_id)
            if w is None:
                d = self.settings.workspace_path / run_id
                if not d.is_dir():
                    raise HTTPException(404, f"unknown run {run_id!r}")
                w = Workspace(run_id=run_id, settings=self.settings)
                self.workspaces[run_id] = w
            return w

    def forget(self, run_id: str) -> None:
        with self.lock:
            w = self.workspaces.pop(run_id, None)
            self.events.pop(run_id, None)
            self.stream.pop(run_id, None)
        if w is not None:
            try:
                w.close()
            except Exception:
                pass

    def emit(self, run_id: str, event: str, data: dict[str, Any]) -> None:
        with self.lock:
            self.event_seq += 1
            q = self.events.setdefault(run_id, deque(maxlen=500))
            q.append({"seq": self.event_seq, "event": event, "data": data, "ts": now_iso()})

    def events_since(self, run_id: str, seq: int) -> list[dict[str, Any]]:
        with self.lock:
            q = self.events.get(run_id)
            if not q:
                return []
            return [e for e in q if e["seq"] > seq]

    def reload_settings(self) -> Settings:
        s = load_settings(self.settings_path)
        s.workspace_dir = self.settings.workspace_dir
        self.settings = s
        with self.lock:
            for w in self.workspaces.values():
                w.settings = s
        return s


# --------------------------------------------------------------------------------------- app factory
def create_app(settings_path: Optional[str | Path] = None, workspace_dir: Optional[str | Path] = None) -> FastAPI:
    settings = load_settings(settings_path)
    if workspace_dir:
        settings.workspace_dir = str(workspace_dir)
    settings.workspace_path.mkdir(parents=True, exist_ok=True)
    state = _State(settings, Path(settings_path) if settings_path else None)
    _ensure_plotly()

    app = FastAPI(title="Trustworthy Process Monitor", version="0.1.0", docs_url="/api/docs", redoc_url=None)
    app.state.tpm = state
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    # ---------- live sensor monitor (own page, independent of runs) ----------
    from ..live import LiveMonitor, create_router as _live_router

    live_monitor = LiveMonitor(lambda: state.settings, settings.workspace_path / "_live")
    app.state.live = live_monitor
    app.include_router(_live_router(lambda: live_monitor))

    # ---------- basics ----------
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        try:
            from ..memory import memory_snapshot

            mem = memory_snapshot()
        except Exception:
            mem = {}
        return {"ok": True, "version": "0.1.0", "time": now_iso(), "memory": mem, "runs": len(Workspace.list_runs(state.settings)), "jobs": {k: v.get("state") for k, v in state.jobs.items()}}

    @app.get("/api/settings")
    def get_settings_() -> dict[str, Any]:
        s = state.settings
        try:
            from ..llm import available

            models = available()
        except Exception as e:
            models = {"local": False, "external": False, "error": str(e)}
        ext_calls = 0
        blocked = 0
        try:
            for d in s.workspace_path.iterdir():
                p = d / ARTIFACTS["egress_ledger"]
                if p.exists():
                    for line in p.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        if rec.get("route") == "external":
                            if rec.get("guard_result") == "allowed" and rec.get("ok", True):
                                ext_calls += 1
                            else:
                                blocked += 1
        except Exception:
            pass
        prof = s.active_profile
        return {
            "profile": s.profile,
            "profiles": {k: {"description": v.description, "allow_external": v.allow_external, "guard_strict": v.guard_strict, "routing": v.routing} for k, v in s.profiles.items()},
            "allow_external": prof.allow_external,
            "guard_strict": prof.guard_strict,
            "routing": prof.routing,
            "models": models,
            "local_model": s.local_llm.model,
            "local_base_url": s.local_llm.base_url,
            "external_model": s.external_llm.model,
            "external_provider": s.external_llm.provider,
            "external_base_url": s.external_llm.base_url,
            "external_key_configured": bool(s.external_llm.api_key),
            "external_route_exists": bool(prof.allow_external and s.external_llm.api_key),
            **_external_model_state(s),
            "external_caps": _external_caps(s),
            "external_sig_digits": s.guard.external_sig_digits,
            "external_calls": ext_calls,
            "external_blocked": blocked,
            "languages": s.report.languages,
            "default_language": s.report.default_language,
            "workspace_dir": str(s.workspace_path),
            "time_budget_s": s.time_budget_s,
            "blind_mode": s.ingest.blind_mode,
        }

    @app.put("/api/settings")
    @_offload
    async def put_settings(request: Request) -> dict[str, Any]:
        body = await request.json()
        allowed = {}
        if "profile" in body:
            if body["profile"] not in state.settings.profiles:
                raise HTTPException(400, f"unknown profile {body['profile']!r}; choose one of {sorted(state.settings.profiles)}")
            allowed["profile"] = body["profile"]
        if "local_model" in body and body["local_model"]:
            allowed["local_llm"] = {"model": str(body["local_model"])}
        if "external_llm" in body or "external_model" in body:
            # only the model id may be changed here, and only to one the settings allow: never a Fable / Mythos model
            ext = body["external_llm"] if "external_llm" in body else {"model": body.get("external_model")}
            if not isinstance(ext, dict) or set(ext) != {"model"}:
                raise HTTPException(400, "only external_llm.model can be changed here")
            ext_model = str(ext["model"] or "").strip()
            ok, why = state.settings.base_external_llm.model_allowed(ext_model)  # the top-level model is what gets written
            if not ok:
                raise HTTPException(400, why)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,99}", ext_model):
                raise HTTPException(400, f"'{ext_model[:60]}' is not a model id")
            allowed["external_llm"] = {"model": ext_model}
        if not allowed:
            raise HTTPException(400, "nothing to change (profile, local_model, external_llm.model)")
        old = state.settings.profile
        old_ext = state.settings.external_llm.model
        try:
            pending = dict(allowed)  # in-place edits keep the comments of settings.yaml; the rest is merged and rewritten
            if "profile" in pending and _rewrite_profile_line(state.settings_path, pending["profile"]):
                pending.pop("profile")
            if "external_llm" in pending and _rewrite_external_model_line(state.settings_path, pending["external_llm"]["model"]):
                pending.pop("external_llm")
            if pending:
                save_settings_overrides(pending, state.settings_path)
        except Exception as e:
            raise HTTPException(500, f"could not save settings: {e}")
        import os

        if "profile" in allowed and os.environ.get("TPM_PROFILE"):  # .env may pin the profile; the UI choice wins for this process
            os.environ["TPM_PROFILE"] = allowed["profile"]
        if "local_llm" in allowed and os.environ.get("TPM_LOCAL_MODEL"):
            os.environ["TPM_LOCAL_MODEL"] = allowed["local_llm"]["model"]
        if "external_llm" in allowed and os.environ.get("TPM_EXTERNAL_MODEL"):
            os.environ["TPM_EXTERNAL_MODEL"] = allowed["external_llm"]["model"]
        s = state.reload_settings()
        for rid, w in list(state.workspaces.items()):
            try:
                w.log.record("human:ui(reviewer)", "settings", "settings", "profile", {"from": old, "to": s.profile})
                if "external_llm" in allowed:
                    w.log.record("human:ui(reviewer)", "settings", "settings", "external_model", {"from": old_ext, "to": s.external_llm.model})
            except Exception:
                pass
        return {"ok": True, "profile": s.profile, "allow_external": s.active_profile.allow_external, "external_model": s.external_llm.model, "changed": allowed}

    # ---------- runs ----------
    def _start_job(run_id: str, source_path: str, options: dict[str, Any], profile: Optional[str]) -> None:
        def worker() -> None:
            job = state.jobs[run_id]
            job["state"] = "running"
            job["started_at"] = now_iso()

            def progress(stage: str, fraction: float, message: str) -> None:
                state.emit(run_id, "progress", {"stage": stage, "progress": fraction, "message": message})

            try:
                from ..pipeline import run_pipeline

                st = run_pipeline(source_path, run_id=run_id, profile=profile, options=options, progress_cb=progress, settings=state.settings)
                job["state"] = st.state
            except Exception as e:
                job["state"] = "failed"
                job["error"] = f"{e}\n{traceback.format_exc()[-2000:]}"
                try:
                    w = state.ws(run_id)
                    st = w.status()
                    st.state = "failed"
                    st.error = str(job["error"])[:4000]
                    w.set_status(st)
                except Exception:
                    pass
            job["finished_at"] = now_iso()
            import gc

            gc.collect()  # run_pipeline's own Workspace holds a sqlite handle; release it so the run can be deleted on Windows
            state.emit(run_id, "status", {"state": job["state"]})

        state.jobs[run_id] = {"state": "pending", "run_id": run_id, "source_path": source_path, "created_at": now_iso()}
        t = threading.Thread(target=worker, name=f"tpm-run-{run_id}", daemon=True)
        state.jobs[run_id]["thread"] = t
        t.start()

    @app.post("/api/runs", status_code=202)
    async def create_run(request: Request) -> dict[str, Any]:
        ctype = request.headers.get("content-type", "")
        options: dict[str, Any] = {}
        source: Optional[str] = None
        received: Any = None  # tpm.api.upload.Received: the data file, already on disk (staged next to the runs)
        if ctype.startswith("multipart/form-data"):
            # The data file can be many GB: it is streamed to the workspace volume in 8 MB chunks and never held
            # in memory (see tpm/api/upload.py). Nothing is rejected by size; a full disk answers 507 with the numbers.
            from .upload import UploadFailed, receive_multipart

            try:
                received = await receive_multipart(request, state.settings.workspace_path)
            except UploadFailed as e:
                raise HTTPException(e.status_code, e.detail)
            for k, v in received.fields:
                options[k] = v
            if "rules_file" in received.small_files:
                options["rules_text"] = received.small_files["rules_file"].decode("utf-8", errors="replace")
        else:
            try:
                options = await request.json()
            except Exception:
                options = {}
            options = dict(options or {})
        source = options.pop("path", None) or options.pop("source_path", None)
        profile = options.pop("profile", None) or None
        try:
            run_id = _check_run_id(options.pop("run_id", None) or None)
        except HTTPException:
            if received is not None:
                received.discard()
            raise
        # normalise option types
        for k in ("has_header", "transposed"):
            if k in options and isinstance(options[k], str):
                options[k] = options[k].lower() in ("1", "true", "yes", "on")
        if isinstance(options.get("group_columns"), str):
            options["group_columns"] = [c.strip() for c in options["group_columns"].split(",") if c.strip()]
        options = {k: v for k, v in options.items() if v not in ("", None)}
        if profile and profile not in state.settings.profiles:
            if received is not None:
                received.discard()
            raise HTTPException(400, f"unknown profile {profile!r}")
        ws = Workspace(run_id=run_id, settings=state.settings)
        run_id = ws.run_id
        if received is not None and received.path is not None:
            # staged on the same volume: a rename into workspace/<run_id>/uploads/<name>, nothing is copied
            source = str(received.move_into(ws.dir / "uploads"))
        if not source:
            raise HTTPException(400, "provide a file (multipart field 'file') or a local path ('path')")
        if not Path(source).exists():
            raise HTTPException(400, f"path not found: {source}")
        st = RunStatus(run_id=run_id, source_path=str(source), profile=profile or state.settings.profile, state="pending", options=options, stages=[StageStatus(stage=s, state="pending") for s in STAGE_NAMES])
        ws.set_status(st)
        with state.lock:
            state.workspaces[run_id] = ws
        _start_job(run_id, str(source), options, profile)
        return {"run_id": run_id, "state": "pending", "source_path": str(source), "options": options, "profile": st.profile}

    @app.get("/api/runs")
    def list_runs() -> dict[str, Any]:
        runs = Workspace.list_runs(state.settings)
        healed = False
        for r in runs:
            if r.get("state") in ("running", "pending") and r.get("run_id"):
                try:
                    healed = _heal_orphan(state.ws(r["run_id"]), state.jobs) or healed
                except Exception:
                    pass
        if healed:
            runs = Workspace.list_runs(state.settings)
        for r in runs:
            job = state.jobs.get(r.get("run_id", ""))
            r["job"] = {k: v for k, v in job.items() if k != "thread"} if job else None
            try:
                d = state.settings.workspace_path / r["run_id"]
                r["artifacts"] = sorted(p.name for p in d.iterdir() if p.is_file() and not p.name.endswith(".tmp"))
                r["n_flags"] = sum(1 for _ in open(d / ARTIFACTS["flags"], "r", encoding="utf-8")) if (d / ARTIFACTS["flags"]).exists() else 0
                if (d / ARTIFACTS["meta"]).exists():
                    m = json.loads((d / ARTIFACTS["meta"]).read_text(encoding="utf-8"))
                    r["meta"] = {"fake": m.get("fake", False), "created_at": m.get("created_at")}
            except Exception:
                r["artifacts"] = []
        return {"runs": runs, "workspace_dir": str(state.settings.workspace_path)}

    @app.get("/api/runs/{run_id}/status")
    def run_status(run_id: str) -> dict[str, Any]:
        ws = state.ws(run_id)
        try:
            _heal_orphan(ws, state.jobs)
        except Exception:
            pass
        st = _jsonable(ws.status())
        job = state.jobs.get(run_id)
        st["job"] = {k: v for k, v in job.items() if k != "thread"} if job else None
        st["time_budget_s"] = state.settings.time_budget_s
        st["artifacts"] = {k: ws.exists(k) for k in ARTIFACTS if k not in ("meta", "status")}
        st["artifacts"]["understanding"] = (ws.dir / "understanding.json").exists()
        st["artifacts"]["report_en"] = (ws.dir / "report_en.html").exists()
        meta = ws.read_json("meta") or {}
        st["meta"] = {k: meta.get(k) for k in ("created_at", "fake", "options")}
        st["stream"] = state.stream.get(run_id)
        return st

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: str) -> dict[str, Any]:
        ws = state.ws(run_id)
        job = state.jobs.get(run_id)
        if job and job.get("state") in ("running", "pending"):
            raise HTTPException(409, "run is still processing; wait for it to finish")
        d = ws.dir
        state.forget(run_id)
        state.jobs.pop(run_id, None)
        import gc

        gc.collect()
        last: Optional[Exception] = None
        for _ in range(5):
            try:
                shutil.rmtree(d)
                last = None
                break
            except Exception as e:  # Windows keeps sqlite/parquet handles open for a moment
                last = e
                gc.collect()
                time.sleep(0.3)
        if last is not None:
            raise HTTPException(409, f"could not delete yet, files are still in use: {last}")
        return {"ok": True, "deleted": run_id}

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request, max_events: int = Query(0, ge=0), since: int = Query(0, ge=0)) -> StreamingResponse:
        ws = state.ws(run_id)

        async def gen():
            sent = 0
            last_seq = since
            status_path = ws.path("status")
            flags_path = ws.path("flags")
            chat_path = ws.path("chat")
            last_status = -1.0
            last_flags = flags_path.stat().st_size if flags_path.exists() else 0
            last_chat = chat_path.stat().st_size if chat_path.exists() else 0
            last_keepalive = time.time()

            def sse(event: str, data: Any) -> str:
                return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

            yield sse("status", _jsonable(ws.status()))
            sent += 1
            while True:
                if max_events and sent >= max_events:
                    break
                if await request.is_disconnected():
                    break
                try:
                    m = status_path.stat().st_mtime if status_path.exists() else 0
                    if m != last_status and last_status >= 0:
                        yield sse("status", _jsonable(ws.status()))
                        sent += 1
                    last_status = m
                    fs = flags_path.stat().st_size if flags_path.exists() else 0
                    if fs > last_flags:
                        new = ws.read_jsonl("flags")[-5:]
                        yield sse("flags", {"n": len(ws.read_jsonl("flags")), "latest": new})
                        sent += 1
                    last_flags = fs
                    cs = chat_path.stat().st_size if chat_path.exists() else 0
                    if cs > last_chat:
                        yield sse("chat", {"latest": ws.read_jsonl("chat")[-2:]})
                        sent += 1
                    last_chat = cs
                    for e in state.events_since(run_id, last_seq):
                        last_seq = e["seq"]
                        yield sse(e["event"], e["data"])
                        sent += 1
                    if time.time() - last_keepalive > 15:
                        yield ": keepalive\n\n"
                        last_keepalive = time.time()
                except Exception as e:  # never kill the stream
                    yield sse("error", {"error": str(e)})
                    sent += 1
                await asyncio.sleep(0.05 if max_events else 1.0)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---------- artifacts ----------
    def _read_artifact(ws: Workspace, name: str) -> tuple[Any, bool]:
        if name in ARTIFACT_JSONL:
            if not ws.exists(name):
                return [], False
            return ws.read_jsonl(name), True
        p = ws.dir / f"{name}.json" if name not in ARTIFACTS else ws.path(name)
        if not p.exists():
            return None, False
        try:
            return json.loads(p.read_text(encoding="utf-8")), True
        except Exception:
            return None, False

    def _wrap(items: Any, available: bool, key: str = "items", **extra: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"available": available}
        if isinstance(items, list):
            out[key] = items
            out["n"] = len(items)
        elif isinstance(items, dict):
            out.update(items)
        elif items is not None:
            out[key] = items
        out.update(extra)
        return out

    @app.get("/api/runs/{run_id}/schema")
    def get_schema(run_id: str) -> dict[str, Any]:
        d, ok = _read_artifact(state.ws(run_id), "schema")
        return _wrap(d or {}, ok)

    @app.get("/api/runs/{run_id}/signals")
    def get_signals(run_id: str) -> dict[str, Any]:
        d, ok = _read_artifact(state.ws(run_id), "signals")
        return _wrap(d or [], ok, "signals")

    @app.get("/api/runs/{run_id}/relations")
    def get_relations(run_id: str) -> dict[str, Any]:
        d, ok = _read_artifact(state.ws(run_id), "relations")
        return _wrap(d or {}, ok)

    @app.get("/api/runs/{run_id}/domain")
    def get_domain(run_id: str) -> dict[str, Any]:
        d, ok = _read_artifact(state.ws(run_id), "domain")
        return _wrap(d or {}, ok)

    @app.get("/api/runs/{run_id}/suspicious")
    def get_suspicious(run_id: str, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)) -> dict[str, Any]:
        """ONE headline list of suspicious rows (detector point anomalies + range/spike checks)."""
        ws = state.ws(run_id)
        d = ws.read_json("suspicious_rows.json", None)
        if not isinstance(d, dict):
            return {"available": False}
        rows = d.get("rows") or []
        return {"available": True, **{k: v for k, v in d.items() if k != "rows"}, "rows": rows[offset: offset + limit], "offset": offset, "limit": limit}

    @app.get("/api/runs/{run_id}/plain")
    def get_plain(run_id: str, view: str = Query("understanding"), lang: str = Query("en"), enhance: int = Query(0)) -> dict[str, Any]:
        """Plain-language explanation of a view for non-specialists (template; local-model rewording on request)."""
        from .plain import VIEWS, plain_for

        ws = state.ws(run_id)
        if view not in VIEWS:
            return {"available": False, "error": f"unknown view {view}", "views": list(VIEWS)}
        try:
            d = plain_for(ws, state.settings, view, lang=lang, enhance=bool(enhance))
        except Exception as e:
            return {"available": False, "error": str(e)[:300]}
        return {"available": bool(d.get("paragraphs")), **d}

    @app.get("/api/runs/{run_id}/brief")
    def get_brief(run_id: str, view: str = Query("overview"), lang: str = Query("en")) -> dict[str, Any]:
        """Short jargon-free summary of a view (or of the whole run: view=overview): verdict, one headline, up to
        three points and clickable next steps. Deterministic templates in en / fi / sv; never calls a model."""
        from .brief import VIEWS as BRIEF_VIEWS, brief_for

        ws = state.ws(run_id)
        if view not in BRIEF_VIEWS:
            raise HTTPException(400, f"unknown view {view!r}; choose one of {list(BRIEF_VIEWS)}")
        return brief_for(ws, state.settings, view, lang=lang)

    @app.get("/api/runs/{run_id}/brief/item")
    def get_brief_item(run_id: str, id: str = Query(..., min_length=1, max_length=64), lang: str = Query("en")) -> dict[str, Any]:
        """The same kind of summary for one object: DIAG- / FLAG- / CHK- / EV- / INF- / RULE- / PATTERN- ids, batch ids."""
        from .brief import brief_item

        item = brief_item(state.ws(run_id), state.settings, id, lang=lang)
        if item is None:
            raise HTTPException(status_code=404, detail=f"{id} was not found in run {run_id}")
        return item

    @app.get("/api/runs/{run_id}/understanding")
    def get_understanding(run_id: str) -> dict[str, Any]:
        ws = state.ws(run_id)
        d, ok = _read_artifact(ws, "understanding")
        if ok and d:
            # normalise the profile stage's artifact into what the UI/report expect (summary, assumptions,
            # uncertain, hypotheses, per-signal plain text) -- the raw keys are kept alongside
            try:
                from .plain import understanding_normalized

                d = understanding_normalized(ws)
            except Exception as e:
                d = dict(d); d["normalize_error"] = str(e)[:200]
        if not ok:
            # derive a minimal understanding from schema + inferences so the view is never empty
            sch, ok2 = _read_artifact(ws, "schema")
            if ok2 and sch:
                infs = [i for i in ws.inferences.all() if i.subject == "dataset"]
                d = {"summary": f"{sch.get('n_rows')} rows, {len(sch.get('signal_columns', []))} signals, {sch.get('n_groups', 1)} groups.", "assumptions": sch.get("assumptions", []), "uncertain": [i.claim for i in infs if i.status == "uncertain"], "hypotheses": [{"inference_id": i.id, "subject": i.subject, "claim": i.claim, "confidence": i.confidence, "status": i.status} for i in infs], "inference_ids": [i.id for i in infs], "derived": True}
                ok = True
        return _wrap(d or {}, ok)

    @app.get("/api/runs/{run_id}/evidence")
    def get_evidence(run_id: str, ids: str = Query(""), signal: str = Query(""), kind: str = Query(""), limit: int = Query(200, ge=1, le=5000), offset: int = Query(0, ge=0), lang: str = Query("en")) -> dict[str, Any]:
        """Evidence with a plain-language sentence (`plain`) on every item. `ids` may name ANY citable object
        (EV-, DIAG-, FLAG-, CHK-, INF-, RULE-, PATTERN-, EGR-, batch ids): non-evidence objects come back as
        evidence-like items (kind = diagnosis | flag | check | ..., with their own evidence_ids to drill into)."""
        from .evidence_plain import resolve_refs, with_plain

        ws = state.ws(run_id)
        if ids:
            wanted = [i.strip() for i in ids.split(",") if i.strip()]
            items, missing = resolve_refs(ws, wanted, lang=lang)
            return {"available": ws.exists("evidence"), "items": _jsonable(items), "n": len(items), "missing": missing}
        items = ws.evidence.all()
        if signal:
            items = [e for e in items if signal in e.signals]
        if kind:
            items = [e for e in items if e.kind == kind]
        total = len(items)
        return {"available": ws.exists("evidence"), "items": [with_plain(_jsonable(e), lang) for e in items[offset: offset + limit]], "n": total, "offset": offset, "limit": limit}

    @app.get("/api/runs/{run_id}/object/{object_id}")
    def get_object(run_id: str, object_id: str, lang: str = Query("en")) -> dict[str, Any]:
        """One citable object of the run (any id prefix) as an evidence-like item with `plain`; 404 when unknown."""
        from .evidence_plain import resolve_ref

        item = resolve_ref(state.ws(run_id), object_id, lang=lang)
        if item is None:
            raise HTTPException(status_code=404, detail=f"{object_id} was not found in run {run_id}")
        return _jsonable(item)

    @app.get("/api/runs/{run_id}/inferences")
    def get_inferences(run_id: str, subject: str = Query(""), status: str = Query(""), stage: str = Query(""), ids: str = Query("")) -> dict[str, Any]:
        ws = state.ws(run_id)
        items = ws.inferences.all()
        if ids:
            wanted = {i.strip() for i in ids.split(",") if i.strip()}
            items = [i for i in items if i.id in wanted]
        if subject:
            items = [i for i in items if i.subject == subject]
        if status:
            items = [i for i in items if i.status == status]
        if stage:
            items = [i for i in items if i.stage == stage]
        return {"available": ws.exists("inferences"), "items": [_jsonable(i) for i in items], "n": len(items)}

    @app.get("/api/runs/{run_id}/batches")
    def get_batches(run_id: str) -> dict[str, Any]:
        d, ok = _read_artifact(state.ws(run_id), "batches")
        if isinstance(d, list):  # tpm.ingest.stream.build_batches writes a plain list (row_end exclusive)
            d = {"batches": d, "row_end_exclusive": True}
        return _wrap(d or {"batches": []}, ok)

    @app.get("/api/runs/{run_id}/checks")
    def get_checks(run_id: str, batch: str = Query(""), signal: str = Query(""), status: str = Query(""), category: str = Query(""), limit: int = Query(500, ge=1, le=20000), offset: int = Query(0, ge=0)) -> dict[str, Any]:
        items, ok = _read_artifact(state.ws(run_id), "checks")
        if batch:
            items = [c for c in items if c.get("batch_id") == batch]
        if signal:
            items = [c for c in items if signal in (c.get("signals") or [])]
        if status:
            items = [c for c in items if c.get("status") == status]
        if category:
            items = [c for c in items if c.get("category") == category]
        summary: dict[str, dict[str, int]] = {}
        for c in items:
            s = summary.setdefault(c.get("category", "other"), {"pass": 0, "warn": 0, "fail": 0})
            s[c.get("status", "pass")] = s.get(c.get("status", "pass"), 0) + 1
        total = len(items)
        return {"available": ok, "items": items[offset: offset + limit], "n": total, "offset": offset, "limit": limit, "summary": summary}

    @app.get("/api/runs/{run_id}/trust")
    def get_trust(run_id: str) -> dict[str, Any]:
        items, ok = _read_artifact(state.ws(run_id), "trust")
        untrusted = [t for t in items if not t.get("trusted", True)]
        worst = min(items, key=lambda t: t.get("trust_score", 1.0)) if items else None
        # run-level verdict in plain words (round 6, quality_summary.json): the UI must not call the data fine while checks fail
        summary = state.ws(run_id).read_json("quality_summary.json", None)
        return {"available": ok, "items": items, "n": len(items), "n_untrusted": len(untrusted), "untrusted": untrusted, "worst": worst, "overall": (sum(t.get("trust_score", 0) for t in items) / len(items)) if items else None, "summary": summary}

    @app.get("/api/runs/{run_id}/rules")
    def get_rules(run_id: str) -> dict[str, Any]:
        d, ok = _read_artifact(state.ws(run_id), "rules")
        return _wrap(d or [], ok, "rules")

    @app.get("/api/runs/{run_id}/flags")
    def get_flags(run_id: str, kind: str = Query(""), group: str = Query(""), batch: str = Query(""), min_severity: float = Query(0.0, ge=0, le=1), status: str = Query(""), limit: int = Query(1000, ge=1, le=100000), offset: int = Query(0, ge=0)) -> dict[str, Any]:
        items, ok = _read_artifact(state.ws(run_id), "flags")
        if kind:
            kinds = set(kind.split(","))
            items = [f for f in items if f.get("kind") in kinds]
        if group:
            items = [f for f in items if str(f.get("group_id")) == group]
        if batch:
            items = [f for f in items if f.get("batch_id") == batch]
        if min_severity:
            items = [f for f in items if float(f.get("severity", 0)) >= min_severity]
        if status:
            items = [f for f in items if (f.get("human_status") or "open") == status]
        kinds_count: dict[str, int] = {}
        for f in items:
            kinds_count[f.get("kind", "?")] = kinds_count.get(f.get("kind", "?"), 0) + 1
        return {"available": ok, "items": items[offset: offset + limit], "n": len(items), "offset": offset, "limit": limit, "kinds": kinds_count, "groups": sorted({str(f.get("group_id")) for f in items if f.get("group_id") is not None}, key=lambda x: (len(x), x))}

    @app.get("/api/runs/{run_id}/patterns")
    def get_patterns(run_id: str) -> dict[str, Any]:
        d, ok = _read_artifact(state.ws(run_id), "patterns")
        return _wrap(d or [], ok, "patterns")

    @app.get("/api/runs/{run_id}/baseline")
    def get_baseline(run_id: str) -> dict[str, Any]:
        d, ok = _read_artifact(state.ws(run_id), "baseline")
        return _wrap(d or {}, ok)

    @app.get("/api/runs/{run_id}/detect_meta")
    def get_detect_meta(run_id: str) -> dict[str, Any]:
        d, ok = _read_artifact(state.ws(run_id), "detect_meta")
        return _wrap(d or {}, ok)

    @app.get("/api/runs/{run_id}/evaluation")
    def get_evaluation(run_id: str) -> dict[str, Any]:
        d, ok = _read_artifact(state.ws(run_id), "evaluation")
        return _wrap(d or {}, ok)

    @app.get("/api/runs/{run_id}/diagnoses")
    def get_diagnoses(run_id: str, group: str = Query(""), cause: str = Query(""), pattern: str = Query("")) -> dict[str, Any]:
        items, ok = _read_artifact(state.ws(run_id), "diagnoses")
        if group:
            items = [d for d in items if str(d.get("group_id")) == group]
        if cause:
            items = [d for d in items if d.get("cause_class") == cause]
        if pattern:
            items = [d for d in items if d.get("pattern_id") == pattern]
        return {"available": ok, "items": items, "n": len(items)}

    @app.get("/api/runs/{run_id}/assessor")
    def get_assessor(run_id: str) -> dict[str, Any]:
        d, ok = _read_artifact(state.ws(run_id), "assessor")
        return _wrap(d or {}, ok)

    # ---------- series / scores (DuckDB, downsampled; never leaves the machine) ----------
    def _columns(con: Any, view: str) -> list[str]:
        return [r[0] for r in con.execute(f"DESCRIBE {view}").fetchall()]

    def _row_expr(cols: list[str]) -> str:
        for c in ("__row__", "row", "row_id", "row_index"):
            if c in cols:
                return f'"{c}"'
        return "(row_number() OVER () - 1)"

    @app.get("/api/runs/{run_id}/scores")
    def get_scores(run_id: str, signal: str = Query(""), group: str = Query(""), row_start: int = Query(0, ge=0), row_end: int = Query(-1), max_points: int = Query(600, ge=10, le=20000), detectors: bool = Query(False)) -> dict[str, Any]:
        ws = state.ws(run_id)
        if not ws.exists("scores"):
            return {"available": False, "rows": [], "score": [], "threshold": [], "contrib": {}, "signals": []}
        try:
            con = ws.duckdb().cursor()  # per-request cursor: the shared connection is not safe for concurrent queries
            cols = _columns(con, "scores")
            rexpr = _row_expr(cols)
            score_col = next((c for c in ("ensemble", "score", "ensemble_score", "anomaly_score") if c in cols), None)
            if not score_col:
                return {"available": False, "reason": "no score column in scores.parquet", "columns": cols}
            thr_col = next((c for c in ("threshold", "thr", "ens_threshold") if c in cols), None)
            contrib_cols = [c for c in cols if c.startswith("c_") or c.startswith("contrib_")]
            det_cols = [c for c in cols if c.startswith("d_") or c.startswith("det_") or c.startswith("score_")] if detectors else []
            if signal:
                wanted = {s.strip() for s in signal.split(",") if s.strip()}
                contrib_cols = [c for c in contrib_cols if c.split("_", 1)[1] in wanted]
            group_col = next((c for c in ("__group__", "group_id", "group") if c in cols), None)
            batch_col = next((c for c in ("batch_id", "batch") if c in cols), None)
            base = f'SELECT {rexpr} AS r, "{score_col}" AS score' + (f', "{thr_col}" AS thr' if thr_col else ", NULL AS thr") + (f', CAST("{group_col}" AS VARCHAR) AS grp' if group_col else ", NULL AS grp") + (f', CAST("{batch_col}" AS VARCHAR) AS bt' if batch_col else ", NULL AS bt")
            for c in contrib_cols + det_cols:
                base += f', "{c}"'
            base += " FROM scores"
            where = []
            params: list[Any] = []
            if group and group_col:
                where.append("grp = ?")
                params.append(group)
            sub = f"({base}) s" + ((" WHERE " + " AND ".join(where)) if where else "")
            n_rows, rmin, rmax = con.execute(f"SELECT COUNT(*), MIN(r), MAX(r) FROM {sub}", params).fetchone()
            if not n_rows:
                return {"available": True, "rows": [], "score": [], "threshold": [], "contrib": {}, "signals": [], "n_rows": 0}
            lo = max(row_start, int(rmin))
            hi = int(rmax) if row_end < 0 else min(row_end, int(rmax))
            span = max(1, hi - lo + 1)
            bucket = max(1, -(-span // max_points))
            aggs = ", ".join([f'avg("{c}") AS "{c}"' for c in contrib_cols] + [f'max("{c}") AS "{c}"' for c in det_cols])
            q = f"SELECT floor((r - {lo}) / {bucket}) AS b, min(r) AS r0, max(r) AS r1, max(score) AS score, avg(thr) AS thr, any_value(grp) AS grp, any_value(bt) AS bt" + (", " + aggs if aggs else "") + f" FROM {sub} {'AND' if where else 'WHERE'} r BETWEEN {lo} AND {hi} GROUP BY b ORDER BY b"
            rows = con.execute(q, params).fetchall()
            out: dict[str, Any] = {"available": True, "row_start": lo, "row_end": hi, "n_rows": int(n_rows), "bucket": bucket, "rows": [], "row_end_bucket": [], "score": [], "threshold": [], "group": [], "batch": [], "contrib": {}, "detectors": {}, "signals": [c.split("_", 1)[1] for c in contrib_cols]}
            for c in contrib_cols:
                out["contrib"][c.split("_", 1)[1]] = []
            for c in det_cols:
                out["detectors"][c.split("_", 1)[1]] = []
            for rr in rows:
                out["rows"].append(int(rr[1]))
                out["row_end_bucket"].append(int(rr[2]))
                out["score"].append(None if rr[3] is None else round(float(rr[3]), 4))
                out["threshold"].append(None if rr[4] is None else round(float(rr[4]), 4))
                out["group"].append(rr[5])
                out["batch"].append(rr[6])
                k = 7
                for c in contrib_cols:
                    out["contrib"][c.split("_", 1)[1]].append(None if rr[k] is None else round(float(rr[k]), 4))
                    k += 1
                for c in det_cols:
                    out["detectors"][c.split("_", 1)[1]].append(None if rr[k] is None else round(float(rr[k]), 4))
                    k += 1
            out["threshold_value"] = next((t for t in out["threshold"] if t is not None), None)
            if out["threshold_value"] is None and score_col == "ensemble":
                out["threshold_value"] = 1.0  # detect.ensemble normalises so that 1.0 is the calibrated threshold
                out["threshold"] = [1.0] * len(out["rows"])
            out["score_column"] = score_col
            con.close()
            return out
        except Exception as e:
            return {"available": False, "error": str(e)}

    @app.get("/api/runs/{run_id}/series")
    def get_series(run_id: str, signals: str = Query(""), group: str = Query(""), row_start: int = Query(0, ge=0), row_end: int = Query(-1), max_points: int = Query(600, ge=10, le=20000)) -> dict[str, Any]:
        ws = state.ws(run_id)
        if not ws.exists("dataset"):
            return {"available": False, "rows": [], "series": {}}
        try:
            con = ws.duckdb().cursor()  # per-request cursor: the shared connection is not safe for concurrent queries
            cols = _columns(con, "dataset")
            sch = ws.read_json("schema") or {}
            alias = sch.get("signal_alias", {}) or {}
            inv = {v: k for k, v in alias.items()}
            wanted = [s.strip() for s in signals.split(",") if s.strip()] or list(alias.values())[:6]
            resolved: list[tuple[str, str]] = []
            for s in wanted:
                col = s if s in cols else inv.get(s) if inv.get(s) in cols else (alias.get(s) if alias.get(s) in cols else None)
                if col:
                    resolved.append((s, col))
            if not resolved:
                return {"available": True, "rows": [], "series": {}, "missing": wanted}
            rexpr = _row_expr(cols)
            group_col = next((c for c in ("__group__", sch.get("group_column") or "", "group_id") if c and c in cols), None)
            time_col = sch.get("time_column") if sch.get("time_column") in cols else None
            base = f"SELECT {rexpr} AS r" + (f', CAST("{group_col}" AS VARCHAR) AS grp' if group_col else ", NULL AS grp") + (f', CAST("{time_col}" AS VARCHAR) AS t' if time_col else ", NULL AS t")
            for _, col in resolved:
                base += f', TRY_CAST("{col}" AS DOUBLE) AS "{col}"'
            base += " FROM dataset"
            params: list[Any] = []
            where = ""
            if group and group_col:
                where = " WHERE grp = ?"
                params.append(group)
            sub = f"({base}) s{where}"
            n_rows, rmin, rmax = con.execute(f"SELECT COUNT(*), MIN(r), MAX(r) FROM {sub}", params).fetchone()
            if not n_rows:
                return {"available": True, "rows": [], "series": {}, "n_rows": 0}
            lo = max(row_start, int(rmin))
            hi = int(rmax) if row_end < 0 else min(row_end, int(rmax))
            span = max(1, hi - lo + 1)
            bucket = max(1, -(-span // max_points))
            aggs = ", ".join(f'avg("{col}") AS "m_{i}", min("{col}") AS "lo_{i}", max("{col}") AS "hi_{i}"' for i, (_, col) in enumerate(resolved))
            q = f"SELECT floor((r - {lo}) / {bucket}) AS b, min(r) AS r0, any_value(grp) AS grp, min(t) AS t, {aggs} FROM {sub} {'AND' if where else 'WHERE'} r BETWEEN {lo} AND {hi} GROUP BY b ORDER BY b"
            rows = con.execute(q, params).fetchall()
            out: dict[str, Any] = {"available": True, "row_start": lo, "row_end": hi, "n_rows": int(n_rows), "bucket": bucket, "rows": [], "group": [], "time": [], "series": {s: {"mean": [], "min": [], "max": [], "column": col} for s, col in resolved}}
            for rr in rows:
                out["rows"].append(int(rr[1]))
                out["group"].append(rr[2])
                out["time"].append(rr[3])
                for i, (s, _) in enumerate(resolved):
                    m, lo_, hi_ = rr[4 + 3 * i], rr[5 + 3 * i], rr[6 + 3 * i]
                    out["series"][s]["mean"].append(None if m is None else round(float(m), 5))
                    out["series"][s]["min"].append(None if lo_ is None else round(float(lo_), 5))
                    out["series"][s]["max"].append(None if hi_ is None else round(float(hi_), 5))
            con.close()
            return out
        except Exception as e:
            return {"available": False, "error": str(e)}

    # ---------- decisions / log ----------
    def _apply(ws: Workspace, decision: HumanDecision) -> dict[str, Any]:
        from ..pipeline import apply_decision

        result = apply_decision(ws, state.settings, decision)
        if "effect" not in result:
            result["generic_effect"] = fallback.apply_generic_effect(ws, state.settings, decision)
        state.emit(ws.run_id, "decision", {"action": decision.action, "object_type": decision.object_type, "object_id": decision.object_id, "actor": decision.actor_name, "role": decision.role})
        return result

    @app.post("/api/runs/{run_id}/decisions")
    @_offload
    async def post_decision(run_id: str, request: Request) -> dict[str, Any]:
        ws = state.ws(run_id)
        body = await request.json()
        try:
            decision = HumanDecision(**body)
        except Exception as e:
            raise HTTPException(422, f"invalid decision: {e}")
        if decision.role not in ("basic", "operator", "engineer", "reviewer"):
            raise HTTPException(422, "role must be operator | engineer | reviewer")
        try:
            return {"ok": True, **_apply(ws, decision), "decision": _jsonable(decision)}
        except Exception as e:
            raise HTTPException(500, f"decision failed: {e}")

    @app.get("/api/runs/{run_id}/log")
    def get_log(run_id: str, object_type: str = Query(""), object_id: str = Query(""), action: str = Query(""), actor: str = Query(""), since: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=100000), offset: int = Query(0, ge=0)) -> dict[str, Any]:
        ws = state.ws(run_id)
        entries = ws.log.entries(object_type=object_type or None, object_id=object_id or None, action=action or None, actor_prefix=actor or None, since_seq=since, limit=1_000_000)
        total = len(entries)
        page = entries[offset: offset + limit]
        return {"available": True, "items": [_jsonable(e) for e in page], "n": total, "offset": offset, "limit": limit, "count_total": ws.log.count(), "actions": sorted({e.action for e in entries}), "object_types": sorted({e.object_type for e in entries}), "actors": sorted({e.actor for e in entries})}

    @app.get("/api/runs/{run_id}/log/verify")
    def verify_log(run_id: str) -> dict[str, Any]:
        ws = state.ws(run_id)
        res = ws.log.verify_chain()
        res["verified_at"] = now_iso()
        res["count"] = ws.log.count()
        return res

    @app.get("/api/runs/{run_id}/log/completeness")
    def log_completeness(run_id: str) -> dict[str, Any]:
        """Is every decision logged? Objects in the run's artifacts vs objects with an entry of their own, and the reason
        for every gap (review item 21). Read-only, well under a second on the 15 M-row practice run."""
        ws = state.ws(run_id)
        fn = _lazy("tpm.log.completeness:audit")
        if fn is None:
            return _unavailable("tpm.log.completeness:audit", "The completeness audit is not available in this build.")
        res = fn(ws)
        st_fn = _lazy("tpm.log.completeness:completeness_statement")
        res["statement"] = st_fn(res) if st_fn else ""
        return _jsonable(res)

    @app.get("/api/runs/{run_id}/log/export")
    def export_log(run_id: str) -> FileResponse:
        ws = state.ws(run_id)
        out = ws.dir / "decision_log_export.jsonl"
        ws.log.export_jsonl(out)
        return FileResponse(str(out), media_type="application/x-ndjson", filename=f"{run_id}_decision_log.jsonl")

    # ---------- rules ----------
    def _save_rule(ws: Workspace, rule: Any) -> dict[str, Any]:
        rules = ws.read_json("rules", []) or []
        rd = _jsonable(rule)
        if not rd.get("id"):
            rd["id"] = f"RULE-{len(rules) + 1:03d}"
        for i, r in enumerate(rules):
            if r.get("id") == rd["id"]:
                rules[i] = rd
                break
        else:
            rules.append(rd)
        ws.write_json("rules", rules)
        return rd

    def _next_rule_id(ws: Workspace) -> str:
        rules = ws.read_json("rules", []) or []
        n = 0
        for r in rules:
            try:
                n = max(n, int(str(r.get("id", "RULE-0")).split("-")[-1]))
            except Exception:
                pass
        return f"RULE-{n + 1:03d}"

    @app.post("/api/runs/{run_id}/rules")
    @_offload
    async def compile_rule(run_id: str, request: Request):
        ws = state.ws(run_id)
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text is required")
        author = body.get("actor") or "human"
        fn = _lazy("tpm.quality:compile_rule")
        if fn is None:
            return _unavailable("tpm.quality:compile_rule", "The rule compiler is not available in this build; the rule was not saved.")
        try:
            try:
                rule = fn(ws, state.settings, text, author=author)
            except TypeError:
                rule = fn(ws, state.settings, text)
        except Exception as e:
            raise HTTPException(500, f"rule compilation failed: {e}")
        if isinstance(rule, dict):
            rule.setdefault("id", _next_rule_id(ws))
            rule.setdefault("text", text)
            rule = Rule(**rule)
        if not getattr(rule, "id", None):
            rule.id = _next_rule_id(ws)
        existing = {r.get("id") for r in (ws.read_json("rules", []) or [])}
        rd = _jsonable(rule) if rule.id in existing else _save_rule(ws, rule)  # the compiler may persist itself
        if rule.id not in existing:
            ws.log.record(f"human:{author}", "rule", "rule", rd["id"], {"text": text, "status": rd.get("status"), "compile_source": rd.get("compile_source"), "confidence": rd.get("compile_confidence")})
        return {"ok": True, "rule": rd}

    @app.post("/api/runs/{run_id}/rules/{rule_id}/{verb}")
    @_offload
    async def rule_decision(run_id: str, rule_id: str, verb: str, request: Request) -> dict[str, Any]:
        if verb not in ("approve", "reject"):
            raise HTTPException(404, "use approve or reject")
        ws = state.ws(run_id)
        try:
            body = await request.json()
        except Exception:
            body = {}
        decision = HumanDecision(actor_name=body.get("actor_name") or "ui", role=body.get("role") or "engineer", action=f"{verb}_rule", object_type="rule", object_id=rule_id, note=body.get("note"))
        res = _apply(ws, decision)
        rule = next((r for r in (ws.read_json("rules", []) or []) if r.get("id") == rule_id), None)
        return {"ok": True, "rule": rule, **res}

    @app.post("/api/runs/{run_id}/rules/run")
    @_offload
    async def run_rules(run_id: str):
        ws = state.ws(run_id)
        run_active = _lazy("tpm.quality:run_active_rules")
        define_batches = _lazy("tpm.quality:define_batches")
        fn = _lazy("tpm.quality:run_rules") or _lazy("tpm.quality:run_quality")
        if run_active is None and fn is None:
            return _unavailable("tpm.quality:run_active_rules")
        try:
            if run_active is not None:
                b = ws.read_json("batches") or {}
                batches = define_batches(ws, state.settings) if define_batches else (b.get("batches") if isinstance(b, dict) else b) or []
                checks = run_active(ws, state.settings, batches)
                res = {"n_checks": len(checks or []), "n_fail": sum(1 for c in checks or [] if getattr(c, "status", "") == "fail"), "batches": len(batches)}
            else:
                ctx = {"source_path": ws.status().source_path, "options": {"rules_only": True}, "progress": lambda *a, **k: None, "run_id": run_id}
                res = fn(ws, state.settings, ctx) if fn.__name__ == "run_quality" else fn(ws, state.settings)
        except Exception as e:
            raise HTTPException(500, f"rules run failed: {e}")
        state.emit(run_id, "rules", {"ran": True})
        return {"ok": True, "result": _jsonable(res) if res is not None else {}}

    @app.post("/api/runs/{run_id}/rules/upload")
    @_offload
    async def upload_rules(run_id: str, request: Request) -> dict[str, Any]:
        ws = state.ws(run_id)
        form = await request.form()
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise HTTPException(400, "multipart field 'file' required")
        text = (await f.read()).decode("utf-8", errors="replace")
        author = form.get("actor") or "human"
        lines = [ln.strip() for ln in text.splitlines()]
        lines = [ln for ln in lines if ln and not ln.startswith("#")]
        fn = _lazy("tpm.quality:compile_rule")
        saved = []
        from_file = _lazy("tpm.quality:add_rules_from_file")
        if from_file is not None:
            up = ws.dir / "uploads"
            up.mkdir(parents=True, exist_ok=True)
            p = up / Path(getattr(f, "filename", "rules.md") or "rules.md").name
            p.write_text(text, encoding="utf-8")
            try:
                rules = from_file(ws, state.settings, p, author=str(author))
                ws.log.record(f"human:{author}", "rules_uploaded", "rule", "file", {"n": len(rules), "filename": p.name})
                return {"ok": True, "rules": [_jsonable(r) for r in rules], "n": len(rules), "compiler_available": True}
            except Exception as e:
                ws.log.record(f"human:{author}", "rules_upload_failed", "rule", "file", {"error": str(e)[:300]})
        for ln in lines:
            rule = None
            if fn is not None:
                try:
                    rule = fn(ws, state.settings, ln)
                    if isinstance(rule, dict):
                        rule.setdefault("id", _next_rule_id(ws))
                        rule.setdefault("text", ln)
                        rule = Rule(**rule)
                    if not getattr(rule, "id", None):
                        rule.id = _next_rule_id(ws)
                except Exception as e:
                    rule = Rule(id=_next_rule_id(ws), text=ln, status="draft", compile_source="template", compile_explanation=f"compile failed: {e}", compile_confidence=0.0)
            if rule is None:
                rule = Rule(id=_next_rule_id(ws), text=ln, status="draft", compile_source="human", compile_explanation="Stored as draft text; the rule compiler is not available in this build.", compile_confidence=0.0)
            rule.author = str(author)
            saved.append(_save_rule(ws, rule))
        ws.log.record(f"human:{author}", "rules_uploaded", "rule", "file", {"n": len(saved), "filename": getattr(f, "filename", "")})
        return {"ok": True, "rules": saved, "n": len(saved), "compiler_available": fn is not None}

    # ---------- chat ----------
    @app.post("/api/runs/{run_id}/chat")
    @_offload
    async def post_chat(run_id: str, request: Request) -> dict[str, Any]:
        ws = state.ws(run_id)
        body = await request.json()
        message = (body.get("message") or "").strip()
        if not message:
            raise HTTPException(400, "message is required")
        context = body.get("context") or {}
        history = body.get("history") or []
        actor = body.get("actor") or "operator"
        role = body.get("role") or "operator"
        language = body.get("language") or "en"
        # one drawer conversation = one chat_id; the client names the turn in advance so POST /chat/stop can flag it
        chat_id = fallback.clean_chat_id(body.get("chat_id"))
        client_turn_id = fallback.clean_turn_id(body.get("client_turn_id"))
        answer: dict[str, Any]
        fn = _lazy("tpm.llm.agent:chat")
        persisted_by_agent = False
        if fn is not None:
            try:
                task = "assessor_chat" if context.get("object_type") == "assessor" else "why_chat"
                res = fn(ws, state.settings, message, context, history, f"human:{actor}({role})", task=task, language=language, chat_id=chat_id, client_turn_id=client_turn_id)
                persisted_by_agent = True  # tpm.llm.agent.chat writes both turns to chat.jsonl and the decision log
                answer = fallback.normalize_chat_result(res)
                if answer.get("stopped"):
                    entry = {"ts": now_iso(), "turn_id": answer.get("turn_id") or client_turn_id, "chat_id": chat_id, "role": "assistant", "actor": "stopped", "message": "", "source": "stopped", "route": answer.get("route", "none"), "model": "", "evidence_ids": [], "context": context, "status": "stopped", "stopped": True, "followups": [], "tool_trace": answer.get("tool_trace") or []}
                    state.emit(run_id, "chat", {"role": "assistant", "source": "stopped", "chat_id": chat_id})
                    return {"ok": True, "answer": entry, "stopped": True}
                if not answer.get("text"):
                    answer = fallback.template_chat_answer(ws, state.settings, message, context, language)
                    answer["note"] = "model returned no text; template answer shown"
            except Exception as e:
                answer = fallback.template_chat_answer(ws, state.settings, message, context, language)
                answer["note"] = f"model call failed ({e}); template answer shown"
        else:
            answer = fallback.template_chat_answer(ws, state.settings, message, context, language)
        turn_id = answer.get("turn_id") or client_turn_id or ("CHAT-" + now_iso().replace(":", "").replace("-", "")[:15])
        entry = {"ts": now_iso(), "turn_id": turn_id, "chat_id": chat_id, "role": "assistant", "actor": answer.get("source", "template"), "message": answer.get("text", ""), "source": answer.get("source", "template"), "route": answer.get("route", "none"), "model": answer.get("model", ""), "evidence_ids": answer.get("evidence_ids", []), "context": context, "note": answer.get("note"), "followups": answer.get("followups") or [], "confidence": answer.get("confidence"), "tool_trace": answer.get("tool_trace") or [], "series": answer.get("series")}
        if not persisted_by_agent:
            ws.append_jsonl("chat", {"ts": now_iso(), "turn_id": turn_id, "chat_id": chat_id, "role": "user", "actor": f"{actor}({role})", "message": message, "context": context})
            ws.append_jsonl("chat", {k: v for k, v in entry.items() if k != "series"})
            try:
                ws.log.record(f"human:{actor}({role})", "chat", context.get("object_type") or "chat", context.get("object_id") or context.get("flag_id") or context.get("diagnosis_id") or "run", {"message": message[:500], "answer_source": entry["source"], "chat_id": chat_id}, entry["evidence_ids"])
            except Exception:
                pass
        if fallback.is_stopped(turn_id):  # the person pressed Stop while the template answer was being composed
            entry.update(message="", source="stopped", actor="stopped", status="stopped", stopped=True, followups=[])
            return {"ok": True, "answer": entry, "stopped": True}
        state.emit(run_id, "chat", {"role": "assistant", "source": entry["source"], "chat_id": chat_id})
        return {"ok": True, "answer": entry}

    @app.get("/api/runs/{run_id}/chat")
    def get_chat(run_id: str, limit: int = Query(200, ge=1, le=5000), chat_id: Optional[str] = Query(None)) -> dict[str, Any]:
        """Persisted turns, of one chat when ``chat_id`` is given (turns written before chats existed are "default").
        ``chats`` lists every chat id in the file with its turn count, so the drawer can rebuild its list."""
        ws = state.ws(run_id)
        items = ws.read_jsonl("chat") if ws.exists("chat") else []
        chats: dict[str, dict[str, Any]] = {}
        for m in items:
            cid = fallback.turn_chat_id(m)
            c = chats.setdefault(cid, {"chat_id": cid, "n": 0, "first_question": "", "context": None, "last_ts": None})
            c["n"] += 1
            c["last_ts"] = m.get("ts") or c["last_ts"]
            if m.get("role") == "user" and not c["first_question"]:
                c["first_question"] = str(m.get("message") or m.get("content") or "")[:120]
                c["context"] = m.get("context")
        if chat_id:
            items = [m for m in items if fallback.turn_chat_id(m) == chat_id]
        out = []
        for m in items[-limit:]:  # accept both the API's shape (message/evidence_ids) and tpm.llm.agent's (content/citations)
            m = dict(m)
            if "message" not in m:
                m["message"] = m.get("content", "")
            if "evidence_ids" not in m:
                m["evidence_ids"] = m.get("citations", [])
            m["chat_id"] = fallback.turn_chat_id(m)
            out.append(m)
        return {"available": True, "items": out, "n": len(items), "chats": list(chats.values())}

    @app.post("/api/runs/{run_id}/chat/stop")
    async def stop_chat(run_id: str, request: Request) -> dict[str, Any]:
        """The drawer's Stop button: flag the running turn (by the client's turn id) or every running turn of a chat.
        tpm.llm.agent.run_agent checks the flag before each model call and after each tool call."""
        ws = state.ws(run_id)
        body = await request.json()
        turn_id = fallback.clean_turn_id(body.get("turn_id") or body.get("client_turn_id"))
        chat_id = fallback.clean_chat_id(body.get("chat_id")) if body.get("chat_id") else None
        if not turn_id and not chat_id:
            raise HTTPException(400, "turn_id or chat_id is required")
        # a named turn stops only that turn: flagging the whole chat too could stop the next question when the person
        # asks it right after Stop (both requests race); every running turn of a chat stops only when no turn is named
        flagged = fallback.request_stop(turn_id, None if turn_id else chat_id)
        actor = body.get("actor") or "operator"
        role = body.get("role") or "operator"
        try:
            ws.log.record(f"human:{actor}({role})", "chat_stop", "chat", turn_id or chat_id or "chat", {"chat_id": chat_id, "turn_ids": flagged})
        except Exception:
            pass
        return {"ok": True, "stopped": flagged}

    def _clear_chat(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Remove the persisted turns of one chat ("Clear history" keeps the chat, "Delete chat" removes it in the UI;
        the server side is the same). Logged as a human decision."""
        ws = state.ws(run_id)
        chat_id = fallback.clean_chat_id(body.get("chat_id"))
        actor = body.get("actor") or "operator"
        role = body.get("role") or "operator"
        fallback.request_stop(None, chat_id)  # a turn still running for this chat stops too
        removed = fallback.clear_chat(ws, chat_id)
        try:
            ws.log.record(f"human:{actor}({role})", "chat_deleted" if body.get("delete") else "chat_cleared", "chat", chat_id, {"chat_id": chat_id, "turns_removed": removed})
        except Exception:
            pass
        state.emit(run_id, "chat", {"role": "system", "source": "cleared", "chat_id": chat_id})
        return {"ok": True, "removed": removed, "chat_id": chat_id}

    @app.post("/api/runs/{run_id}/chat/clear")
    async def clear_chat(run_id: str, request: Request) -> dict[str, Any]:
        return _clear_chat(run_id, await request.json())

    @app.delete("/api/runs/{run_id}/chat")
    def delete_chat(run_id: str, chat_id: str = Query(...), actor: str = Query("operator"), role: str = Query("operator"), delete: bool = Query(False)) -> dict[str, Any]:
        return _clear_chat(run_id, {"chat_id": chat_id, "actor": actor, "role": role, "delete": delete})

    # ---------- assessor ----------
    @app.post("/api/runs/{run_id}/assessor/ask")
    @_offload
    async def assessor_ask(run_id: str, request: Request):
        ws = state.ws(run_id)
        body = await request.json()
        question = (body.get("question") or "").strip()
        if not question:
            raise HTTPException(400, "question is required")
        actor = body.get("actor") or "operator"
        fn = _lazy("tpm.assessor:ask")
        if fn is not None:
            try:
                res = fn(ws, state.settings, question, actor)
                ans = fallback.normalize_chat_result(res)
                if ans.get("text"):
                    ws.log.record(f"human:{actor}", "assessor_question", "assessor", "chat", {"question": question[:500], "source": ans.get("source")})
                    return {"ok": True, "answer": ans}
            except Exception as e:
                ans = fallback.template_assessor_answer(ws, question)
                ans["note"] = f"assessor call failed ({e}); template answer shown"
                return {"ok": True, "answer": ans}
        agent_chat = _lazy("tpm.llm.agent:chat")
        if agent_chat is not None:  # the local tool agent answers assessor questions over the artifacts + raw data
            try:
                res = agent_chat(ws, state.settings, question, {"object_type": "assessor", "object_id": "assessor"}, [], f"human:{actor}", task="assessor_chat", language=body.get("language") or "en")
                ans = fallback.normalize_chat_result(res)
                if ans.get("text"):
                    return {"ok": True, "answer": ans}
            except Exception:
                pass
        if not ws.exists("assessor"):
            return _unavailable("tpm.assessor:ask", "The assessor has not run for this file yet.")
        ans = fallback.template_assessor_answer(ws, question)
        ws.log.record(f"human:{actor}", "assessor_question", "assessor", "chat", {"question": question[:500], "source": "template"})
        return {"ok": True, "answer": ans}

    @app.post("/api/runs/{run_id}/assessor/upload")
    @_offload
    async def assessor_upload(run_id: str, request: Request):
        ws = state.ws(run_id)
        form = await request.form()
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise HTTPException(400, "multipart field 'file' required")
        up = ws.dir / "uploads"
        up.mkdir(parents=True, exist_ok=True)
        p = up / Path(getattr(f, "filename", "candidate.csv") or "candidate.csv").name
        p.write_bytes(await f.read())
        fn = _lazy("tpm.assessor:assess_new_file")
        if fn is None:
            return _unavailable("tpm.assessor:assess_new_file", f"File stored at {p.name}; the assessor cannot evaluate it in this build.")
        try:
            res = fn(ws, state.settings, str(p))
        except Exception as e:
            raise HTTPException(500, f"assessment failed: {e}")
        return {"ok": True, "path": str(p), "result": _jsonable(res) if res is not None else {}}

    @app.post("/api/runs/{run_id}/assessor/apply")
    @_offload
    async def assessor_apply(run_id: str, request: Request) -> dict[str, Any]:
        ws = state.ws(run_id)
        body = await request.json()
        action = body.get("action")
        if not action:
            raise HTTPException(400, "action (recommendation id or text) is required")
        decision = HumanDecision(actor_name=body.get("actor_name") or "ui", role=body.get("role") or "engineer", action="apply_assessor_action", object_type="assessor", object_id=str(action if isinstance(action, str) else action.get("id", "action")), note=body.get("note"), new_value=action if isinstance(action, dict) else {"action": action})
        res = _apply(ws, decision)
        return {"ok": True, **res}

    # ---------- streaming ----------
    def _df_from_rows(rows: list[dict[str, Any]]) -> Any:
        import pandas as pd

        return pd.DataFrame(rows)

    def _df_from_csv_bytes(b: bytes) -> Any:
        import pandas as pd

        return pd.read_csv(io.BytesIO(b))

    def _process(ws: Workspace, df: Any, batch_id: str, origin: str, aligned: bool = False) -> dict[str, Any]:
        from ..pipeline import process_batch

        align = _lazy("tpm.ingest.stream:align_incoming")
        if aligned:
            pass
        elif align is not None:
            try:
                df = align(ws, state.settings, df, batch_id)
                batch_id = str(getattr(df, "attrs", {}).get("batch_id") or batch_id)
            except Exception as e:
                df = fallback.align_incoming(ws, df)
                origin += f" (align fallback: {e})"
        else:
            df = fallback.align_incoming(ws, df)
        res = process_batch(ws, state.settings, df, batch_id)
        res["n_rows"] = int(len(df))
        res["origin"] = origin
        st = state.stream.setdefault(ws.run_id, {"pushed": 0, "replay": None, "watch": None, "last": None})
        st["pushed"] = st.get("pushed", 0) + 1
        st["last"] = {"batch_id": batch_id, "n_rows": int(len(df)), "ts": now_iso(), "flags": res.get("flags", []), "origin": origin}
        state.emit(ws.run_id, "batch", {k: v for k, v in res.items() if k != "trust"} | {"trust": res.get("trust")})
        return res

    @app.post("/api/runs/{run_id}/stream/push")
    @_offload
    async def stream_push(run_id: str, request: Request) -> dict[str, Any]:
        ws = state.ws(run_id)
        ctype = request.headers.get("content-type", "")
        batch_id = None
        if ctype.startswith("multipart/form-data"):
            form = await request.form()
            f = form.get("file")
            if f is None or not hasattr(f, "read"):
                raise HTTPException(400, "multipart field 'file' required")
            df = _df_from_csv_bytes(await f.read())
            batch_id = form.get("batch_id")
        else:
            body = await request.json()
            rows = body.get("rows")
            if not isinstance(rows, list) or not rows:
                raise HTTPException(400, "rows (list of objects) required")
            df = _df_from_rows(rows)
            batch_id = body.get("batch_id")
        batch_id = str(batch_id or f"PUSH-{int(time.time())}")
        try:
            res = _process(ws, df, batch_id, "push")
        except Exception as e:
            raise HTTPException(500, f"batch processing failed: {e}")
        return {"ok": True, **_jsonable(res)}

    @app.post("/api/runs/{run_id}/stream/replay")
    @_offload
    async def stream_replay(run_id: str, request: Request) -> dict[str, Any]:
        ws = state.ws(run_id)
        try:
            body = await request.json()
        except Exception:
            body = {}
        speed = float(body.get("speed") or 1.0)
        max_batches = int(body.get("max_batches") or 10)
        if not ws.exists("dataset"):
            raise HTTPException(409, "dataset.parquet not present; run the pipeline first")
        st = state.stream.setdefault(run_id, {"pushed": 0, "replay": None, "watch": None, "last": None})
        if st.get("replay") and st["replay"].get("state") == "running":
            raise HTTPException(409, "replay already running")
        st["replay"] = {"state": "running", "done": 0, "max_batches": max_batches, "speed": speed, "started_at": now_iso(), "stop": False}

        stop = threading.Event()
        st["replay"]["stop_event"] = stop
        their_replay = _lazy("tpm.ingest.stream:replay")

        def worker() -> None:
            ctl = st["replay"]
            try:
                if their_replay is not None:
                    delay = 1.0 / max(0.01, speed)

                    def cb(df: Any, bid: str, meta: dict[str, Any]) -> Any:
                        r = _process(ws, df, f"REPLAY-{bid}", "replay", aligned=True)
                        ctl["done"] = ctl.get("done", 0) + 1
                        ctl["last_batch"] = bid
                        if not stop.is_set():
                            stop.wait(delay)
                        return r

                    their_replay(ws, state.settings, callback=cb, speed=0.0, max_batches=max_batches, stop_event=stop)
                else:
                    fallback.replay(ws, state.settings, ctl, lambda df, bid: _process(ws, df, bid, "replay"))
                ctl["state"] = "stopped" if stop.is_set() else "done"
            except Exception as e:
                ctl["state"] = "failed"
                ctl["error"] = str(e)
            ctl["finished_at"] = now_iso()
            state.emit(run_id, "replay", {"state": ctl["state"], "done": ctl.get("done", 0)})

        threading.Thread(target=worker, name=f"tpm-replay-{run_id}", daemon=True).start()
        return {"ok": True, "replay": {k: v for k, v in st["replay"].items() if k not in ("stop", "stop_event")}}

    @app.post("/api/runs/{run_id}/stream/stop")
    @_offload
    async def stream_stop(run_id: str) -> dict[str, Any]:
        st = state.stream.get(run_id) or {}
        for k in ("replay", "watch"):
            if st.get(k):
                st[k]["stop"] = True
                ev = st[k].get("stop_event")
                if ev is not None:
                    ev.set()
        return {"ok": True}

    @app.post("/api/runs/{run_id}/stream/watch")
    @_offload
    async def stream_watch(run_id: str, request: Request) -> dict[str, Any]:
        ws = state.ws(run_id)
        body = await request.json()
        folder = body.get("folder")
        if not folder or not Path(folder).is_dir():
            raise HTTPException(400, "folder must be an existing directory")
        st = state.stream.setdefault(run_id, {"pushed": 0, "replay": None, "watch": None, "last": None})
        if st.get("watch") and st["watch"].get("state") == "running":
            st["watch"]["stop"] = True
            time.sleep(0.1)
        st["watch"] = {"state": "running", "folder": str(folder), "seen": [], "started_at": now_iso(), "stop": False, "poll_s": float(body.get("poll_s") or 2.0)}
        fn = _lazy("tpm.ingest.stream:watch_folder")
        stop = threading.Event()
        st["watch"]["stop_event"] = stop

        def worker() -> None:
            try:
                if fn is not None:
                    fn(ws, state.settings, str(folder), callback=lambda df, bid, meta: _process(ws, df, f"WATCH-{bid}", "watch", aligned=True), poll_s=float(st["watch"]["poll_s"]), stop_event=stop)
                else:
                    fallback.watch_folder(ws, st["watch"], lambda df, bid: _process(ws, df, bid, "watch"))
                st["watch"]["state"] = "stopped"
            except Exception as e:
                st["watch"]["state"] = "failed"
                st["watch"]["error"] = str(e)

        threading.Thread(target=worker, name=f"tpm-watch-{run_id}", daemon=True).start()
        return {"ok": True, "watch": {k: v for k, v in st["watch"].items() if k not in ("stop", "stop_event")}}

    @app.get("/api/runs/{run_id}/stream/status")
    def stream_status(run_id: str) -> dict[str, Any]:
        state.ws(run_id)
        st = state.stream.get(run_id) or {"pushed": 0, "replay": None, "watch": None, "last": None}
        out = json.loads(json.dumps(st, default=lambda o: None if isinstance(o, threading.Event) else str(o)))
        for k in ("replay", "watch"):
            if out.get(k):
                out[k].pop("stop", None)
                out[k].pop("stop_event", None)
        return {"available": True, **out}

    # ---------- report ----------
    @app.get("/api/runs/{run_id}/report")
    def get_report(run_id: str, lang: str = Query("en"), download: bool = Query(False), status: bool = Query(False), embed: bool = Query(False), refresh: bool = Query(False)):
        """The HTML report in one language. It is cached per language and regenerated when artifacts change; the
        template report is written first (seconds even for large runs) and never waits for the language model: the
        model-written summary is added by a worker when it arrives. `status=1` answers JSON ({"llm": "ready" |
        "pending" | "none", ...}) so the view can show progress; `embed=1` is the in-app preview (no auto-reload)."""
        ws = state.ws(run_id)
        lang = lang if lang in state.settings.report.languages else state.settings.report.default_language
        p = ws.dir / f"report_{lang}.html"
        ensure = _lazy("tpm.report:ensure_report")
        if ensure is None and not p.exists():
            return _unavailable("tpm.report:ensure_report", f"No report_{lang}.html in this run and the report module is not available.")
        st: dict[str, Any] = {"lang": lang, "exists": p.exists(), "llm": "none"}
        if ensure is not None:
            try:
                st = ensure(ws, state.settings, lang, force=refresh)
            except Exception as e:
                if status:
                    return JSONResponse({"available": True, "ok": False, "lang": lang, "exists": p.exists(), "llm": "none", "error": f"report generation failed: {e}"}, status_code=200 if p.exists() else 500)
                if not p.exists():
                    raise HTTPException(500, f"report generation failed: {e}")
        if not p.exists():
            return _unavailable("tpm.report:ensure_report", f"report_{lang}.html was not produced.")
        if status:
            return JSONResponse({"available": True, "ok": True, **{k: v for k, v in st.items() if k != "path"}}, headers={"Cache-Control": "no-store"})
        headers = {"Cache-Control": "no-store", "X-TPM-Report-LLM": str(st.get("llm", "none"))}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{run_id}_report_{lang}.html"'
        if st.get("llm") == "pending" and not download and not embed:
            # opened in its own tab while the model summary is still being written: reload until it is there
            html = p.read_text(encoding="utf-8").replace('<meta charset="utf-8">', '<meta charset="utf-8">\n<meta http-equiv="refresh" content="15">', 1)
            return HTMLResponse(html, headers=headers)
        return FileResponse(str(p), media_type="text/html", headers=headers)

    def _report_export(run_id: str, fmt: str, lang: str, refresh: bool):
        """PDF / PowerPoint of the report: generated on demand from the report context, cached per language with
        the stamp ensure_report() uses, never waits for the language model (a stored model summary is included)."""
        ws = state.ws(run_id)
        lang = lang if lang in state.settings.report.languages else state.settings.report.default_language
        ensure = _lazy("tpm.report:ensure_export")
        if ensure is None:
            return _unavailable("tpm.report:ensure_export", f"The {fmt} export is not available in this build (pip install -r requirements.txt).")
        try:
            res = ensure(ws, state.settings, lang, fmt, force=refresh)
        except ImportError as e:
            return _unavailable("tpm.report:ensure_export", f"The {fmt} export needs a package that is not installed ({e}); run pip install -r requirements.txt.")
        except Exception as e:
            raise HTTPException(500, f"{fmt} export failed: {e}")
        headers = {"Cache-Control": "no-store", "X-TPM-Export-Regenerated": "1" if res.get("regenerated") else "0", "X-TPM-Export-Seconds": str(res.get("seconds", ""))}
        return FileResponse(res["path"], media_type=res["media_type"], filename=res["filename"], headers=headers)

    @app.get("/api/runs/{run_id}/report.pdf")
    def get_report_pdf(run_id: str, lang: str = Query("en"), refresh: bool = Query(False)):
        return _report_export(run_id, "pdf", lang, refresh)

    @app.get("/api/runs/{run_id}/report.pptx")
    def get_report_pptx(run_id: str, lang: str = Query("en"), refresh: bool = Query(False)):
        return _report_export(run_id, "pptx", lang, refresh)

    @app.get("/api/runs/{run_id}/summary.pdf")
    def get_summary_pdf(run_id: str, lang: str = Query("en"), refresh: bool = Query(False)):
        """One A4 page to share (review item 30): what was found, how sure, what to do next."""
        return _report_export(run_id, "summary", lang, refresh)

    @app.post("/api/runs/{run_id}/report/email")
    @_offload
    async def email_report(run_id: str, request: Request):
        ws = state.ws(run_id)
        body = await request.json()
        to = (body.get("to") or "").strip()
        lang = body.get("lang") or "en"
        if not to or "@" not in to:
            raise HTTPException(400, "a valid recipient address is required")
        fn = _lazy("tpm.report:email_report")
        if fn is None:
            return _unavailable("tpm.report:email_report", "Email sending is not available in this build.")
        try:
            res = fn(ws, state.settings, to, lang, attach_pdf=bool(body.get("attach_pdf")), attach_pptx=bool(body.get("attach_pptx")))
        except Exception as e:
            raise HTTPException(500, f"email failed: {e}")
        ws.log.record("human:ui", "report_emailed", "report", lang, {"to": to, "attachments": (res or {}).get("attachments") if isinstance(res, dict) else None})
        return {"ok": True, "result": _jsonable(res) if res is not None else {}}

    # ---------- data flow ----------
    def _coverage(ws: Any, lang: str) -> Optional[dict[str, Any]]:
        """Who wrote the explanations of a run (model or evidence template, and why), in one sentence + details."""
        try:
            from ..llm.ledger import coverage_details, coverage_sentence, narrative_coverage

            cov = narrative_coverage(ws, state.settings)
            if not (cov.get("diagnoses") or {}).get("total"):
                return None
            return {"sentence": coverage_sentence(cov, lang), "details": coverage_details(cov, lang)}
        except Exception:
            return None

    def _guard_demo_ctx(ws: Any, lang: str) -> Optional[dict[str, Any]]:
        try:
            from ..llm.guard_demo import report_context

            return report_context(ws, lang)
        except Exception:
            return None

    @app.get("/api/runs/{run_id}/explanations")
    def get_explanations(run_id: str, lang: str = Query("en")) -> dict[str, Any]:
        """Diagnoses page: how many explanations a model wrote and why the rest use the evidence template."""
        ws = state.ws(run_id)
        lang = lang if lang in state.settings.report.languages else state.settings.report.default_language
        return {"coverage": _coverage(ws, lang)}

    @app.post("/api/runs/{run_id}/guard-demo")
    def post_guard_demo(run_id: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Run the egress-guard demonstration on this run's own data (review item 22): a real payload goes through the
        guard, an operator question naming original headers is cleaned, and a deliberately unsafe payload made of the
        run's raw rows is blocked. Nothing is sent from here (the CLI has --send); the ledger marks the records demo_*."""
        ws = state.ws(run_id)
        body = body or {}
        lang = str(body.get("lang") or "en")
        lang = lang if lang in state.settings.report.languages else state.settings.report.default_language
        fn = _lazy("tpm.llm.guard_demo:run_demo")
        if fn is None:
            return _unavailable("tpm.llm.guard_demo:run_demo", "The guard demonstration is not available in this build.")
        try:
            res = fn(ws, state.settings, profile=body.get("profile") or None, send=False, language=lang)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, f"guard demonstration failed: {e}")
        actor, role = str(body.get("actor") or "ui")[:60], str(body.get("role") or "operator")[:20]
        ws.log.record(f"human:{actor}({role})", "guard_demo_requested", "run", run_id, {"profile": res.get("profile"), "unsafe_verdict": (res.get("unsafe") or {}).get("verdict"), "sent": False})
        return {"ok": True, "guard_demo": _guard_demo_ctx(ws, lang), "unsafe_blocked": (res.get("unsafe") or {}).get("verdict") == "blocked", "headers_leaked": bool((res.get("headers_check") or {}).get("found"))}

    @app.get("/api/runs/{run_id}/egress")
    def get_egress(run_id: str, lang: str = Query("en")) -> dict[str, Any]:
        ws = state.ws(run_id)
        lang = lang if lang in state.settings.report.languages else state.settings.report.default_language
        ledger = ws.read_jsonl("egress_ledger") if ws.exists("egress_ledger") else []
        summary = fallback.ledger_summary(ledger)
        fn = _lazy("tpm.llm.ledger:summary")
        if fn is not None:
            try:
                summary["detail"] = _jsonable(fn(ws, last_n=0))
            except Exception:
                pass
        statement = None
        fn2 = _lazy("tpm.llm.ledger:data_flow_statement")
        if fn2 is not None:
            try:
                statement = fn2(ws, state.settings)
            except Exception:
                statement = None
        if not statement:
            statement = fallback.data_flow_statement(state.settings, summary)
        return {"available": ws.exists("egress_ledger"), "ledger": ledger, "n": len(ledger), "summary": summary, "statement": statement, "coverage": _coverage(ws, lang), "guard_demo": _guard_demo_ctx(ws, lang), "profile": state.settings.profile, "allow_external": state.settings.active_profile.allow_external, "guard_strict": state.settings.active_profile.guard_strict, "external_key_configured": bool(state.settings.external_llm.api_key), "local_model": state.settings.local_llm.model, "external_model": state.settings.external_llm.model, "external_base_url": state.settings.external_llm.base_url}

    @app.get("/api/runs/{run_id}/llm/usage")
    def get_llm_usage(run_id: str) -> dict[str, Any]:
        """External-model use of one run for the Data-flow view: calls and tokens against the run's budget, average
        latency per route (tpm.llm.ledger.usage), the caps from the settings, what the egress guard changed in the
        payloads that were sent, and the latency table of `tpm bench-llm` when llm_benchmark.json exists."""
        ws = state.ws(run_id)
        s = state.settings
        ledger = ws.read_jsonl("egress_ledger") if ws.exists("egress_ledger") else []
        usage: dict[str, Any] = {}
        fn = _lazy("tpm.llm.ledger:usage")
        if fn is not None:
            try:
                usage = _jsonable(fn(ws, s))
            except Exception:
                usage = {}
        benchmark = None
        p = ws.dir / "llm_benchmark.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                benchmark = {k: data.get(k) for k in ("created_at", "n", "local_model", "external_model") if isinstance(data, dict)}
                benchmark["rows"] = _benchmark_rows(data)
            except Exception:
                benchmark = None
        return {
            "available": ws.exists("egress_ledger"),
            "profile": s.profile,
            "allow_external": s.active_profile.allow_external,
            "external_model": s.external_llm.model,
            "external_model_by_task": dict(s.external_llm.model_by_task or {}),
            **_external_model_state(s),
            "usage": usage,
            "caps": _external_caps(s),
            "sanitizer": _sanitizer_totals(ledger),
            "guard": {"external_sig_digits": s.guard.external_sig_digits, "alias_names_external": s.guard.alias_names_external, "min_aggregate_n": s.guard.min_aggregate_n, "max_series_points": s.guard.max_series_points},
            "benchmark": benchmark,
        }

    @app.get("/api/llm/status")
    def llm_status() -> dict[str, Any]:
        """Which model routes are reachable right now (tpm.llm.available), always with the external models the
        settings allow and the reason a configured one is refused."""
        info: dict[str, Any] = {}
        fn = _lazy("tpm.llm:available")
        if fn is not None:
            try:
                info = _jsonable(fn(state.settings))
            except Exception as e:
                info = {"local": False, "external": False, "error": str(e)}
        return {**_external_model_state(state.settings), **info, "profile": state.settings.profile, "allow_external": state.settings.active_profile.allow_external}

    # ---------- misc ----------
    @app.post("/api/demo", status_code=202)
    @_offload
    async def create_demo(request: Request) -> dict[str, Any]:
        """Start a demo run: the REAL pipeline on ``samples/demo_process.csv`` (or ``path`` from the body)
        in a background thread, exactly like ``POST /api/runs`` with a path. Answers at once with the run
        id; progress arrives over ``/events`` like any other run. The synthetic fixture workspace
        (tests/fixtures/fake_workspace.py) is for tests only and is no longer used here."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = dict(body or {})
        run_id = _check_run_id(str(body.pop("run_id", None) or time.strftime("run_demo_%Y%m%d_%H%M%S")))
        if "/" in run_id or "\\" in run_id or run_id.startswith("."):
            raise HTTPException(400, "invalid run_id")
        src_raw = body.pop("path", None) or body.pop("source_path", None)
        if src_raw:
            src = Path(str(src_raw))
        else:
            src = Path(__file__).resolve().parents[2] / "samples" / "demo_process.csv"
            if not src.exists():  # no sample shipped: write one from the shared synthetic generator
                try:
                    from tests.fixtures.synth import make_synthetic

                    df, _truth = make_synthetic(n_groups=12, n_samples=200, seed=1, with_timestamp=True)
                    src = state.settings.workspace_path / "_demo_source" / "demo_process.csv"
                    src.parent.mkdir(parents=True, exist_ok=True)
                    df.to_csv(src, index=False)
                except Exception as e:
                    raise HTTPException(501, f"no demo data available (samples/demo_process.csv missing and the synthetic generator failed: {e})")
        if not src.exists():
            raise HTTPException(400, f"path not found: {src}")
        profile = body.pop("profile", None) or None
        if profile and profile not in state.settings.profiles:
            raise HTTPException(400, f"unknown profile {profile!r}")
        options: dict[str, Any] = {k: v for k, v in body.items() if v not in ("", None)}
        for k in ("has_header", "transposed"):
            if k in options and isinstance(options[k], str):
                options[k] = options[k].lower() in ("1", "true", "yes", "on")
        options.setdefault("has_header", True)
        existing = state.jobs.get(run_id)
        if existing and existing.get("state") in ("pending", "running"):
            raise HTTPException(409, f"run {run_id} is already processing")
        ws = Workspace(run_id=run_id, settings=state.settings)
        st = RunStatus(run_id=run_id, source_path=str(src), profile=profile or state.settings.profile, state="pending", options=options, stages=[StageStatus(stage=s, state="pending") for s in STAGE_NAMES])
        ws.set_status(st)
        with state.lock:
            state.workspaces[run_id] = ws
        _start_job(run_id, str(src), options, profile)
        # The Workspace registered above was opened before the pipeline wrote evidence.jsonl / inferences.jsonl
        # and a registry is loaded once at construction, so it would answer "missing" for every evidence id
        # for the rest of the process. Drop it when the job ends; the next request re-opens the finished run.
        th = state.jobs[run_id].get("thread")

        def release() -> None:
            if th is not None:
                th.join()
            with state.lock:
                old = state.workspaces.pop(run_id, None)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass

        threading.Thread(target=release, name=f"tpm-demo-release-{run_id}", daemon=True).start()
        return {"run_id": run_id, "state": "pending", "source_path": str(src), "profile": st.profile, "options": options, "demo": True}

    @app.get("/api/runs/{run_id}/export")
    def export_run(run_id: str):
        """Zip of every artifact + reports (tpm.log.exports.export_run) for reviewers."""
        ws = state.ws(run_id)
        fn = _lazy("tpm.log.exports:export_run")
        if fn is None:
            return _unavailable("tpm.log.exports:export_run")
        try:
            out = fn(ws, ws.dir / "exports", make_reports=False)
        except Exception as e:
            raise HTTPException(500, f"export failed: {e}")
        return FileResponse(str(out), media_type="application/zip", filename=Path(out).name)

    @app.get("/api/runs/{run_id}/artifact/{name}")
    def raw_artifact(run_id: str, name: str):
        """Raw download of a small artifact (json/jsonl/html) for engineers."""
        ws = state.ws(run_id)
        safe = Path(name).name
        p = ws.dir / safe
        if not p.exists() or p.suffix not in (".json", ".jsonl", ".html", ".md", ".txt"):
            raise HTTPException(404, "artifact not found")
        return FileResponse(str(p), filename=safe)

    # ---------- AI models on this computer (choose, download, install Ollama) ----------
    from .models_api import register as _register_models

    _register_models(app, state)
    from .names_api import register as _register_names

    _register_names(app, state)
    from .advice_api import register as _register_advice; _register_advice(app, state)  # why / what-to-do library (round 5)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": str(exc), "type": type(exc).__name__}, status_code=500)

    return app


app = create_app()
