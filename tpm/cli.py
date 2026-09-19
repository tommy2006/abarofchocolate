"""Command line for the Trustworthy Process Monitor.

    python -m tpm run <path> [--profile no-egress|hybrid|eu-hosted] [--stages a,b] [--opt k=v] [--rules FILE] [--lang en]
    python -m tpm serve [--host 127.0.0.1] [--port 8000] [--open]
    python -m tpm replay <run_id> [--speed 10] [--max-batches N]
    python -m tpm report <run_id> [--format html|pdf|pptx|all] [--lang en|fi|sv|all] [--out FILE|DIR] [--no-llm]
    python -m tpm email <run_id> --to a@b.c [--lang en] [--pdf] [--pptx]
    python -m tpm export <run_id> [--out DIR]
    python -m tpm verify-log <run_id> [--json]          (hash chain + completeness audit of the decision log)
    python -m tpm bench-llm --run <run_id> [--profile hybrid] [--tasks a,b] [--n 3] [--routes local,external] [--dry-run]
    python -m tpm guard-demo --run <run_id> [--profile hybrid|eu-hosted] [--send] [--json]
    python -m tpm models [--run <run_id>] | bakeoff | demo | doctor | list
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
BANNER = "Trustworthy Process Monitor (TPM)"


# ----------------------------------------------------------------------------- console helpers
def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def _p(*a: Any, **k: Any) -> None:
    print(*a, **k)
    try:
        sys.stdout.flush()
    except Exception:
        pass


def _err(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)


def _settings(args: argparse.Namespace):
    if getattr(args, "workspace", None):
        os.environ["TPM_WORKSPACE"] = str(Path(args.workspace).resolve())
    from .config import load_settings

    return load_settings(getattr(args, "settings", None), profile=getattr(args, "profile", None))


def _open_ws(run_id: str, settings):
    from .workspace import Workspace

    root = settings.workspace_path
    if run_id in ("latest", "last"):
        runs = Workspace.list_runs(settings)
        if not runs:
            raise SystemExit(_fail(f"no runs found in {root}"))
        run_id = runs[0]["run_id"]
    if not (root / run_id / "status.json").exists():
        runs = [r["run_id"] for r in Workspace.list_runs(settings)][:10]
        raise SystemExit(_fail(f"run '{run_id}' not found in {root}. Available: {', '.join(runs) if runs else '(none)'}"))
    return Workspace.open(run_id, settings)


def _fail(msg: str) -> int:
    _err(msg)
    return 1


def _new_run_id() -> str:
    return datetime.now().strftime("run_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]


def _parse_opt(kv: str) -> tuple[str, Any]:
    if "=" not in kv:
        raise argparse.ArgumentTypeError(f"--opt expects key=value, got '{kv}'")
    k, v = kv.split("=", 1)
    k = k.strip()
    v = v.strip()
    low = v.lower()
    if low in ("true", "yes", "on"):
        return k, True
    if low in ("false", "no", "off"):
        return k, False
    if low in ("none", "null", ""):
        return k, None
    try:
        return k, int(v)
    except ValueError:
        pass
    try:
        return k, float(v)
    except ValueError:
        pass
    if v[:1] in "[{":
        try:
            return k, json.loads(v)
        except Exception:
            pass
    if "," in v and k.endswith("s"):
        return k, [x.strip() for x in v.split(",") if x.strip()]
    return k, v


def _read_rules_file(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        s = s.lstrip("-*0123456789. ").strip()
        if s:
            lines.append(s)
    return lines


class _ProgressDisplay:
    """Console progress: stage, %, message, elapsed vs budget. Uses \\r updates on a TTY, plain lines otherwise."""

    def __init__(self, budget_s: float, quiet: bool = False):
        self.t0 = time.time()
        self.budget = budget_s
        self.quiet = quiet
        self.tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._line_open = False

    def _elapsed(self) -> str:
        return f"{time.time() - self.t0:5.0f}s/{self.budget:.0f}s"

    def progress(self, stage: str, fraction: float, message: str) -> None:
        if self.quiet:
            return
        with self._lock:
            pct = int(max(0.0, min(1.0, fraction)) * 100)
            line = f"  [{stage:<8}] {pct:3d}%  {message[:70]:<70} {self._elapsed()}"
            if self.tty:
                print("\r" + line[:118], end="", flush=True)
                self._line_open = True
            else:
                last = self._last.get(stage, -1.0)
                if fraction - last >= 0.1 or fraction >= 1.0 or last < 0:
                    print(line)
                    self._last[stage] = fraction

    def stage_event(self, stage: str, state: str, message: str, seconds: Optional[float]) -> None:
        if self.quiet:
            return
        with self._lock:
            if self._line_open:
                print()
                self._line_open = False
            secs = f"{seconds:6.1f}s" if seconds is not None else "       "
            mark = {"done": "ok  ", "failed": "FAIL", "skipped": "skip", "running": "... "}.get(state, state[:4])
            print(f"  [{stage:<8}] {mark} {secs}  {message[:80]}  ({self._elapsed()})")
            self._line_open = False


def _monitor_status(ws_root: Path, run_id: str, display: _ProgressDisplay, stop: threading.Event) -> None:
    """Poll status.json and announce stage transitions (works even for stages that never call progress)."""
    seen: dict[str, str] = {}
    path = ws_root / run_id / "status.json"
    while not stop.is_set():
        try:
            if path.exists():
                d = json.loads(path.read_text(encoding="utf-8"))
                for s in d.get("stages", []):
                    st = s.get("state")
                    if st in ("running", "done", "failed", "skipped") and seen.get(s["stage"]) != st:
                        seen[s["stage"]] = st
                        secs = None
                        if s.get("started_at") and s.get("finished_at"):
                            try:
                                secs = (datetime.fromisoformat(s["finished_at"]) - datetime.fromisoformat(s["started_at"])).total_seconds()
                            except Exception:
                                secs = None
                        display.stage_event(s["stage"], st, s.get("message", ""), secs)
        except Exception:
            pass
        stop.wait(0.4)


def _print_run_summary(status, settings) -> Optional[Path]:
    from .report import report_path
    from .workspace import Workspace

    ws_dir = settings.workspace_path / status.run_id
    _p("")
    _p(f"Run {status.run_id}: {status.state.upper()}  (profile: {status.profile})")
    _p(f"  {'stage':<9} {'state':<8} message")
    for s in status.stages:
        _p(f"  {s.stage:<9} {s.state:<8} {(s.message or '')[:90]}")
    _p(f"Workspace: {ws_dir}")
    rep = None
    for p in sorted(ws_dir.glob("report_*.html")):
        rep = p
        _p(f"Report:    {p}")
    if rep is None:
        _p("Report:    (not generated; run `python -m tpm report <run_id>`)")
    return rep


# ----------------------------------------------------------------------------- commands
def cmd_run(args: argparse.Namespace) -> int:
    from .pipeline import STAGES, run_pipeline

    src = Path(args.path)
    if not src.exists():
        return _fail(f"input not found: {src}")
    settings = _settings(args)
    options: dict[str, Any] = {}
    for k, v in (args.opt or []):
        options[k] = v
    if args.lang:
        options["language"] = args.lang
    if args.no_llm:
        # every stage that can call a model reads one of these keys
        options["report_llm"] = False
        options["use_llm"] = False
        options["no_llm"] = True      # diagnose
        options["skip_llm"] = True    # profile
    stages = [s.strip() for s in args.stages.split(",") if s.strip()] if args.stages else None
    if stages:
        known = {s for s, _ in STAGES}
        bad = [s for s in stages if s not in known]
        if bad:
            return _fail(f"unknown stage(s): {', '.join(bad)}; known: {', '.join(known)}")
    run_id = args.run_id or _new_run_id()

    if args.rules:
        rp = Path(args.rules)
        if not rp.exists():
            return _fail(f"rules file not found: {rp}")
        texts = _read_rules_file(rp)
        options["rules"] = texts
        options["rules_file"] = str(rp.resolve())
        from .contracts import Rule
        from .workspace import Workspace

        ws0 = Workspace(run_id=run_id, settings=settings)
        if not ws0.exists("rules"):
            ws0.write_json("rules", [Rule(id=f"RULE-{i + 1:03d}", text=t, author="human", status="approved", compile_source="template").model_dump() for i, t in enumerate(texts)])
        ws0.close()
        _p(f"Loaded {len(texts)} rule(s) from {rp}")

    display = _ProgressDisplay(settings.time_budget_s, quiet=args.quiet)
    _p(f"{BANNER} - run {run_id}")
    _p(f"  input:   {src}  ({src.stat().st_size / 1e6:.1f} MB)")
    _p(f"  profile: {settings.profile}   stages: {', '.join(stages) if stages else 'all'}   budget: {settings.time_budget_s}s")
    stop = threading.Event()
    mon = threading.Thread(target=_monitor_status, args=(settings.workspace_path, run_id, display, stop), daemon=True)
    mon.start()
    try:
        status = run_pipeline(str(src), run_id=run_id, profile=args.profile, options=options, progress_cb=display.progress, stages=stages, settings=settings, continue_on_error=args.continue_on_error)
    except KeyboardInterrupt:
        stop.set()
        _p("\ninterrupted")
        return 130
    finally:
        stop.set()
        mon.join(timeout=1.0)
    time.sleep(0.05)
    _print_run_summary(status, settings)
    return 0 if status.state == "done" else 1


def cmd_serve(args: argparse.Namespace) -> int:
    settings = _settings(args)
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        return _fail("uvicorn is not installed. Run: pip install -r requirements.txt")
    if importlib.util.find_spec("tpm.api.server") is None:
        return _fail("the web UI (tpm/api/server.py) is not part of this build yet. You can still use `python -m tpm run <file>` and `python -m tpm report <run_id>`.")
    try:
        mod = importlib.import_module("tpm.api.server")
        if not hasattr(mod, "app"):
            return _fail("tpm.api.server has no `app` object")
    except Exception as e:
        return _fail(f"cannot import tpm.api.server: {e}")
    url = f"http://{args.host}:{args.port}"
    _p("=" * 64)
    _p(f" {BANNER}")
    _p(f" UI:        {url}")
    _p(f" Workspace: {settings.workspace_path}")
    _p(f" Profile:   {settings.profile}  (local model: {settings.local_llm.model})")
    _p(" Stop with Ctrl+C")
    _p("=" * 64)
    if args.open:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    import uvicorn

    # SSE progress streams stay open until the browser disconnects; without a graceful-shutdown timeout Ctrl+C hangs
    uvicorn.run("tpm.api.server:app", host=args.host, port=args.port, log_level="warning" if args.quiet else "info", reload=False, timeout_graceful_shutdown=3)
    return 0


def _call_flexible(fn: Callable[..., Any], candidates: dict[str, Any]) -> Any:
    """Call fn with only those keyword arguments its signature accepts."""
    sig = inspect.signature(fn)
    accepts_var = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
    kwargs = {k: v for k, v in candidates.items() if accepts_var or k in sig.parameters}
    positional = [p for p in sig.parameters.values() if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    missing = [p.name for p in positional if p.name not in kwargs]
    if missing:
        raise TypeError(f"{fn.__name__} requires {missing}")
    return fn(**kwargs)


def cmd_replay(args: argparse.Namespace) -> int:
    settings = _settings(args)
    ws = _open_ws(args.run_id, settings)
    try:
        stream = importlib.import_module("tpm.ingest.stream")
    except Exception as e:
        return _fail(f"replay is not available yet (tpm.ingest.stream missing: {e}). Use the UI's replay control when the ingest module is present.")
    fn = getattr(stream, "replay", None)
    if fn is None:
        return _fail("tpm.ingest.stream has no replay() function")
    from .pipeline import process_batch

    _p(f"Replaying run {ws.run_id} as a stream (speed: {'as fast as possible' if not args.speed else f'{args.speed} data-seconds per second'}, max batches: {args.max_batches or 'all'})")
    totals = {"batches": 0, "flags": 0, "untrusted": 0}

    def on_batch(df: Any, batch_id: str, meta: Any = None, **_: Any) -> Any:
        res = process_batch(ws, settings, df, batch_id)
        totals["batches"] += 1
        nflags = len(res.get("flags") or [])
        totals["flags"] += nflags
        trust = (res.get("trust") or {}) if isinstance(res.get("trust"), dict) else {}
        if trust and not trust.get("trusted", True):
            totals["untrusted"] += 1
        _p(f"  batch {batch_id}: rows={len(df)} checks={len(res.get('checks') or [])} trust={trust.get('trust_score', 'n/a')} flags={nflags} diagnoses={len(res.get('diagnoses') or [])}")
        return res

    try:
        _call_flexible(fn, {"ws": ws, "settings": settings, "callback": on_batch, "speed": args.speed, "max_batches": args.max_batches})
    except Exception as e:
        return _fail(f"replay failed: {e}")
    finally:
        ws.close()
    _p(f"Replayed {totals['batches']} batch(es): {totals['flags']} flag(s), {totals['untrusted']} untrusted batch(es)")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """HTML report, PDF document and / or PowerPoint deck. --out is a file when exactly one file is written (one
    language, one format), otherwise a directory that receives tpm_<run>_<lang>.<ext> files."""
    settings = _settings(args)
    ws = _open_ws(args.run_id, settings)
    from .report import download_name, export_context, generate_report

    langs = [args.lang] if args.lang and args.lang != "all" else settings.report.languages
    fmt = (getattr(args, "format", None) or "html").lower()
    formats = ["html", "pdf", "pptx", "summary"] if fmt == "all" else [fmt]
    single = len(langs) == 1 and len(formats) == 1
    out_dir = Path(args.out) if args.out and not single else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    def target(lang: str, ext: str) -> Optional[Path]:
        if args.out and single:
            return Path(args.out)
        return (out_dir / download_name(ws.run_id, lang, ext)) if out_dir is not None else None

    try:
        for lang in langs:
            if "html" in formats:
                out = generate_report(ws, settings, lang, use_llm=not args.no_llm, out_path=target(lang, "html"))
                _p(f"Report written: {out}")
            if "pdf" in formats or "pptx" in formats or "summary" in formats:
                t0 = time.time()
                ctx = export_context(ws, settings, lang)  # collected once per language, shared by every document
                if "summary" in formats:
                    from .report.summary_pdf import generate_summary

                    out = generate_summary(ws, settings, lang, out_path=target(lang, "summary"), context=ctx)
                    _p(f"One-page summary written: {out}")
                if "pdf" in formats:
                    from .report.pdf import browser_pdf, generate_pdf

                    out = None
                    if getattr(args, "pdf_engine", "native") == "browser":
                        out = browser_pdf(ws, settings, lang, out_path=target(lang, "pdf"))
                        if out is None:
                            _p("  no headless Edge / Chrome print available; using the built-in PDF writer")
                    out = out or generate_pdf(ws, settings, lang, out_path=target(lang, "pdf"), context=ctx)
                    _p(f"PDF written: {out}  ({out.stat().st_size / 1e6:.2f} MB)")
                if "pptx" in formats:
                    from .report.pptx_export import generate_pptx

                    out = generate_pptx(ws, settings, lang, out_path=target(lang, "pptx"), context=ctx)
                    _p(f"PowerPoint written: {out}  ({out.stat().st_size / 1e6:.2f} MB)")
                _p(f"  ({lang}: {time.time() - t0:.1f} s)")
        return 0
    except ImportError as e:
        return _fail(f"the {fmt} export needs a package that is not installed ({e}). Run: pip install -r requirements.txt")
    finally:
        ws.close()


def cmd_email(args: argparse.Namespace) -> int:
    settings = _settings(args)
    ws = _open_ws(args.run_id, settings)
    from .report import SmtpNotConfigured, email_report

    try:
        res = email_report(ws, settings, args.to, lang=args.lang or settings.report.default_language, subject=args.subject, regenerate=args.regenerate, attach_pdf=bool(getattr(args, "pdf", False)), attach_pptx=bool(getattr(args, "pptx", False)))
        _p(f"Sent {', '.join(res.get('attachments') or [Path(res['path']).name])} to {', '.join(res['to'])} via {res['host']}")
        return 0
    except SmtpNotConfigured as e:
        return _fail(str(e))
    except Exception as e:
        return _fail(f"e-mail failed: {e}")
    finally:
        ws.close()


def cmd_export(args: argparse.Namespace) -> int:
    settings = _settings(args)
    ws = _open_ws(args.run_id, settings)
    from .log.exports import export_run, list_export

    out_dir = Path(args.out) if args.out else (ROOT / "exports")
    try:
        zp = export_run(ws, out_dir, languages=[args.lang] if args.lang else None, use_llm=False)
    finally:
        ws.close()
    _p(f"Export written: {zp}")
    for name in list_export(zp):
        _p(f"  {name}")
    return 0


def cmd_verify_log(args: argparse.Namespace) -> int:
    """Chain check (exit code 0 / 1), then the completeness audit: per object type, how many objects the run's
    artifacts hold and how many have an entry of their own in the log, with the reason for every gap."""
    settings = _settings(args)
    ws = _open_ws(args.run_id, settings)
    from .log.completeness import audit, format_audit

    try:
        res = ws.log.verify_chain()
        n = ws.log.count()
        comp = None if getattr(args, "no_audit", False) else audit(ws)
    finally:
        ws.close()
    if getattr(args, "json", False):
        _p(json.dumps({"chain": {**res, "total": n}, "completeness": comp}, indent=1, default=str))
        return 0 if res.get("ok") else 1
    if res.get("ok"):
        _p(f"OK: hash chain intact, {res['checked']} entries verified ({n} total)")
    else:
        _p(f"FAIL: chain broken at seq {res.get('first_bad_seq')} after {res.get('checked')} good entries")
    for line in format_audit(comp) if comp else []:
        _p(line)
    return 0 if res.get("ok") else 1


def _ollama_tags(base_url: str, timeout: float = 2.5) -> Optional[list[dict[str, Any]]]:
    try:
        import httpx

        r = httpx.get(base_url.rstrip("/") + "/api/tags", timeout=timeout)
        if r.status_code == 200:
            return r.json().get("models", [])
    except Exception:
        return None
    return None


def _models_actions(args: argparse.Namespace, settings: Any) -> Optional[int]:
    """`models --pull NAME` downloads a model with progress; `models --use NAME|auto` chooses the chat model.
    With neither flag: prints which models the app uses and why, then returns None so the listing continues."""
    from .llm import models as mm

    if getattr(args, "pull", None):
        try:
            job = mm.PULLS.start(args.pull, settings)
        except ValueError as e:
            return _fail(str(e))
        last = ""
        while True:
            j = mm.PULLS.get(job["id"]) or {}
            line = f"  {j.get('status', '')} {j.get('percent', 0):5.1f}%" + (f"  ({j.get('completed_gb')} / {j.get('total_gb')} GB)" if j.get("total_gb") else "")
            if line != last:
                _p(line)
                last = line
            if j.get("state") not in ("queued", "running"):
                break
            time.sleep(1.0)
        if j.get("state") != "done":
            return _fail(j.get("error") or "the download did not finish")
        _p(f"{args.pull} is ready. Use it with: python -m tpm models --use {args.pull}")
        return 0
    if getattr(args, "use", None):
        kind = "embedding" if getattr(args, "embedding", False) else "chat"
        try:
            mm.select(kind, args.use, settings)
        except ValueError as e:
            return _fail(str(e))
        _p(f"Saved: {kind} model = {args.use}")
        return 0
    chosen = mm.choose(settings)
    _p(f"Chat model in use:      {chosen.get('chat') or 'none'}  ({chosen.get('chat_reason')})")
    _p(f"Search model in use:    {chosen.get('embedding') or 'none'}  ({chosen.get('embedding_reason')})")
    return None


def cmd_models(args: argparse.Namespace) -> int:
    settings = _settings(args)
    from .llm import available

    avail = available()
    _p(f"Profile: {settings.profile}  (external allowed: {settings.active_profile.allow_external})")
    _p(f"LLM layer says: {json.dumps(avail, default=str)}")
    rc = _models_actions(args, settings)
    if rc is not None:
        return rc
    tags = _ollama_tags(settings.local_llm.base_url)
    wanted = [settings.local_llm.model] + list(settings.local_llm.fallback_models) + [settings.local_llm.embedding_model]
    if tags is None:
        _p(f"Ollama: NOT reachable at {settings.local_llm.base_url}")
        _p("  Install from https://ollama.com/download, start it, then run:")
        for m in wanted[:2] + [settings.local_llm.embedding_model]:
            _p(f"    ollama pull {m}")
        _p("  Without Ollama the pipeline still runs end-to-end in template mode (no model-written text).")
    else:
        names = {m.get("name") for m in tags}
        _p(f"Ollama: reachable at {settings.local_llm.base_url}; {len(tags)} model(s) pulled")
        for m in tags:
            _p(f"  - {m.get('name')}  ({(m.get('size') or 0) / 1e9:.1f} GB)")
        _p("Configured local models:")
        for m in wanted:
            ok = m in names or any(n.startswith(m + ":") or n == m for n in names)
            _p(f"  {'[x]' if ok else '[ ]'} {m}" + ("" if ok else f"   ->  ollama pull {m}"))
    key = settings.external_llm.api_key
    _p(f"External model: {settings.external_llm.provider}/{settings.external_llm.model}  API key {'present' if key else 'NOT set'} ({settings.external_llm.api_key_env}); route allowed by profile: {settings.active_profile.allow_external}")
    try:
        line = _coverage_line(settings, getattr(args, "run", None))
    except Exception as e:  # the model listing must not fail because of one run
        line = f"Explanations: could not be read ({e})"
    if line:
        _p(line)
    return 0


def cmd_bench_llm(args: argparse.Namespace) -> int:
    """Time the model tasks of a finished run on the local and on the external route (tpm.llm.bench)."""
    settings = _settings(args)
    ws = _open_ws(args.run, settings)
    from .llm import bench

    tasks = [t.strip() for t in (args.tasks or "").split(",") if t.strip()] or None
    unknown = [t for t in (tasks or []) if t not in bench.BENCH_TASKS]
    if unknown:
        return _fail(f"unknown task(s): {', '.join(unknown)}. Valid: {', '.join(bench.BENCH_TASKS)}")
    routes = tuple(r.strip() for r in (args.routes or "").split(",") if r.strip()) or bench.ROUTES
    if any(r not in bench.ROUTES for r in routes):
        return _fail(f"--routes expects local, external or both, got '{args.routes}'")
    _p(f"Benchmark of run {ws.run_id}: profile {settings.profile}, {args.n} call(s) per task and route" + (" (dry run: stub providers, nothing is sent)" if args.dry_run else ""))
    if "external" in routes and not settings.active_profile.allow_external:
        _p(f"  profile '{settings.profile}' does not allow external models: only the local route is measured (use --profile hybrid)")
    try:
        result = bench.run_benchmark(ws, settings, tasks=tasks, n=args.n, routes=routes, chat=not args.no_chat, dry_run=args.dry_run, progress=_p)
    finally:
        ws.close()
    _p("")
    _p(bench.format_table(result))
    _p(f"Written: {ws.dir / (bench.DRY_RUN_FILE if args.dry_run else bench.BENCH_FILE)}")
    return 0


def cmd_guard_demo(args: argparse.Namespace) -> int:
    """Prove the egress guard on a real run: a real payload before / after the guard, an operator question naming
    original columns, and a deliberately unsafe payload that is blocked and never sent (tpm.llm.guard_demo)."""
    settings = _settings(argparse.Namespace(settings=getattr(args, "settings", None), workspace=getattr(args, "workspace", None), profile=None))
    ws = _open_ws(args.run, settings)
    from .llm import guard_demo

    try:
        result = guard_demo.run_demo(ws, settings, profile=args.profile, send=bool(args.send), language=args.lang or "en")
    except ValueError as e:
        ws.close()
        return _fail(str(e))
    except Exception as e:  # a broken artifact must not end in a traceback
        ws.close()
        return _fail(f"the guard demonstration failed: {e}")
    try:
        if args.json:
            _p(json.dumps(result, indent=1, ensure_ascii=False, default=str))
        else:
            for line in guard_demo.format_demo(result, ws.dir):
                _p(line)
    finally:
        ws.close()
    if (result.get("unsafe") or {}).get("verdict") != "blocked" or (result.get("headers_check") or {}).get("found"):
        _err("the guard let raw material through: see the lines above")
        return 2
    return 0


def _coverage_line(settings: Any, run_id: Optional[str]) -> Optional[str]:
    """`tpm models`: who wrote the explanations of a run (the latest one by default), in one plain sentence."""
    from .workspace import Workspace

    runs = Workspace.list_runs(settings)
    rid = run_id if run_id and run_id not in ("latest", "last") else (runs[0]["run_id"] if runs else None)
    if not rid or not (settings.workspace_path / rid / "status.json").exists():
        return None
    from .llm.ledger import narrative_coverage

    ws = Workspace.open(rid, settings)
    try:
        cov = narrative_coverage(ws, settings)
    finally:
        ws.close()
    if not (cov.get("diagnoses") or {}).get("total"):
        return None
    return f"Explanations in run {rid}: {cov['sentence']}" + "".join(f"\n  {d}" for d in cov.get("details") or [])


def cmd_bakeoff(args: argparse.Namespace) -> int:
    script = ROOT / "scripts" / "bakeoff.py"
    if not script.exists():
        return _fail("scripts/bakeoff.py is not present in this build (agent D). See docs/DATAFLOW.md for the local-model choice.")
    return subprocess.call([sys.executable, str(script)] + (args.extra or []))


def _ensure_samples(force: bool = False) -> dict[str, Path]:
    sys.path.insert(0, str(ROOT))
    from scripts.make_samples import make_all

    return make_all(ROOT / "samples", force=force)


def cmd_demo(args: argparse.Namespace) -> int:
    paths = _ensure_samples()
    sample = paths.get("process") or (ROOT / "samples" / "demo_process.csv")
    _p(f"Demo data: {sample}")
    ns = argparse.Namespace(path=str(sample), profile=args.profile, stages=args.stages, opt=[], rules=str(ROOT / "config" / "rules.example.md") if (ROOT / "config" / "rules.example.md").exists() and not args.no_rules else None, lang=args.lang, run_id=args.run_id, no_llm=args.no_llm, continue_on_error=True, quiet=False, settings=args.settings, workspace=args.workspace)
    rc = cmd_run(ns)
    _p("")
    _p("Next: start the UI and open the run:")
    _p("  python -m tpm serve --open        (or run.ps1 / run.sh)")
    _p("Or open the HTML report path printed above in a browser.")
    return rc


def _check_email(settings: Any, ok: Callable[[str], None], warn: Callable[..., None]) -> None:
    """Mail settings for the report's Send button / `tpm email`: present, a sender set, server reachable. Opens a TCP
    connection to the configured server and closes it; nothing is sent."""
    import socket

    s = settings.report.smtp
    if not os.environ.get(s.host_env, "").strip():
        warn(f"report e-mail not configured ({s.host_env} is empty); Send by email and `tpm email` will fail", "set the TPM_SMTP_* lines in .env (see .env.example; Resend works over SMTP)")
        return
    from .report.email import smtp_config

    cfg = smtp_config(settings)
    if not os.environ.get(s.from_env, "").strip():
        warn(f"{s.from_env} is empty, so the sender is '{cfg['from']}'; most providers reject that", f"set {s.from_env} (Resend: onboarding@resend.dev or an address on your verified domain)")
    try:
        socket.create_connection((cfg["host"], cfg["port"]), timeout=5).close()
        ok(f"report e-mail: {cfg['host']}:{cfg['port']} reachable, sender {cfg['from']}")
    except OSError as e:
        warn(f"report e-mail: {cfg['host']}:{cfg['port']} not reachable ({e})", "check the network; if it blocks this port use another one the provider offers (Resend: 465 or 2465 with SSL, 587 or 2587 with STARTTLS)")


def cmd_doctor(args: argparse.Namespace) -> int:
    problems = 0
    warnings = 0

    def ok(msg: str) -> None:
        _p(f"  [ok]   {msg}")

    def warn(msg: str, fix: str = "") -> None:
        nonlocal warnings
        warnings += 1
        _p(f"  [warn] {msg}" + (f"\n         fix: {fix}" if fix else ""))

    def bad(msg: str, fix: str = "") -> None:
        nonlocal problems
        problems += 1
        _p(f"  [FAIL] {msg}" + (f"\n         fix: {fix}" if fix else ""))

    _p(f"{BANNER} - doctor")
    v = sys.version_info
    if v >= (3, 10):
        ok(f"Python {v.major}.{v.minor}.{v.micro} at {sys.executable}")
    else:
        bad(f"Python {v.major}.{v.minor} is too old", "install Python 3.10+ and re-run run.ps1 / run.sh")

    mods = ["pandas", "numpy", "scipy", "sklearn", "lightgbm", "duckdb", "pyarrow", "fastapi", "uvicorn", "pydantic", "yaml", "jinja2", "httpx", "anthropic", "openpyxl", "psutil", "dotenv", "plotly", "ruptures", "multipart", "reportlab", "pptx"]
    if getattr(sys, "frozen", False):
        mods.remove("plotly")  # the installed Windows app ships plotly.min.js as a file instead of the Python package
    missing = []
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception:
            missing.append(m)
    if missing:
        bad(f"missing packages: {', '.join(missing)}", f"{sys.executable} -m pip install -r requirements.txt")
    else:
        ok(f"all {len(mods)} required packages import")

    try:
        settings = _settings(args)
        ok(f"settings loaded from {settings.settings_path} (profile {settings.profile})")
    except Exception as e:
        bad(f"settings failed to load: {e}", "check config/settings.yaml syntax")
        return 1

    try:
        from .memory import memory_snapshot

        snap = memory_snapshot()
        if snap["available_gb"] < 2.0:
            warn(f"only {snap['available_gb']} GB RAM free of {snap['total_gb']} GB", "close other applications; the pipeline works out-of-core but a local LLM needs ~6 GB")
        else:
            ok(f"RAM: {snap['available_gb']} GB free of {snap['total_gb']} GB")
    except Exception as e:
        warn(f"could not read memory: {e}")

    try:
        wsdir = settings.workspace_path
        wsdir.mkdir(parents=True, exist_ok=True)
        probe = wsdir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        du = shutil.disk_usage(wsdir)
        free_gb = du.free / 1e9
        if free_gb < 5:
            warn(f"workspace {wsdir} writable but only {free_gb:.1f} GB free", "a 6 GB input needs roughly 2-3x its size for parquet + scores; set TPM_WORKSPACE to another drive")
        else:
            ok(f"workspace {wsdir} writable ({free_gb:.0f} GB free)")
    except Exception as e:
        bad(f"workspace not writable: {e}", "set TPM_WORKSPACE=<writable dir> in .env")

    tags = _ollama_tags(settings.local_llm.base_url)
    if tags is None:
        warn(f"Ollama not reachable at {settings.local_llm.base_url} (pipeline runs in template mode without it)", f"install https://ollama.com/download then `ollama pull {settings.local_llm.model}`")
    else:
        try:
            from .llm import models as _models

            chosen = _models.choose(settings)
        except Exception:
            chosen = {"chat": settings.local_llm.model if settings.local_llm.model in {m.get("name") for m in tags} else None, "chat_reason": "", "embedding": None}
        if chosen.get("chat"):
            ok(f"Ollama reachable; chat model in use: {chosen['chat']} ({chosen.get('chat_reason', '')})")
        else:
            warn("Ollama reachable but no chat model is installed (explanations come from templates until one is)", f"`python -m tpm models --pull {settings.local_llm.model}`, or any model in the app: top bar > Local model")
        if not chosen.get("embedding"):
            warn("no embedding model installed (search falls back to word matching)", f"`python -m tpm models --pull {settings.local_llm.embedding_model}`")

    if (ROOT / ".env").exists():
        ok(".env present")
    else:
        warn(".env missing (defaults are used)", "copy .env.example to .env")
    if settings.active_profile.allow_external and not settings.external_llm.api_key:
        warn(f"profile {settings.profile} allows external calls but {settings.external_llm.api_key_env} is not set", f"add {settings.external_llm.api_key_env}=... to .env or use the no-egress profile")
    _check_email(settings, ok, warn)

    from .pipeline import STAGES, _resolve

    impl = [s for s, d in STAGES if _resolve(d) is not None]
    miss = [s for s, d in STAGES if _resolve(d) is None]
    if miss:
        warn(f"stages implemented: {', '.join(impl)}; not yet: {', '.join(miss)} (they will be skipped)")
    else:
        ok("all pipeline stages implemented: " + ", ".join(impl))
    if importlib.util.find_spec("tpm.api.server") is None:
        warn("web UI (tpm.api.server) not present; CLI + HTML report still work")
    else:
        ok("web UI module present (python -m tpm serve)")

    _p("")
    _p(f"{problems} problem(s), {warnings} warning(s)")
    return 1 if problems else 0


def cmd_showcase(args: argparse.Namespace) -> int:
    settings = _settings(args)
    from .showcase import run_showcase

    try:
        res = run_showcase(args.run_id, settings, rules_file=args.rules, chat_q=not args.no_chat, lang=args.lang)
    except Exception as e:
        return _fail(str(e))
    r = res["rules"]
    _p(f"1. Rules -> checks ({r['n_checks']} checks on every batch):")
    for x in r["rules"]:
        _p(f"   {x['id']} [{x['status']}] {x['text']}  ->  {x['results'] or 'not compiled'}")
    h = res["human_in_the_loop"]
    _p("2. Human in the loop:")
    for d in h.get("decisions", []):
        _p(f"   {d['action']:8s} {d['diagnosis']}: {d['note']}")
    if h.get("downstream"):
        ds = h["downstream"]
        _p(f"   downstream: the event of {ds['event_of']} was diagnosed again -> {ds['after']['id']}: '{ds['after']['fault_type']}' (was '{ds['before']['fault_type']}')")
    c = res["chat"]
    if c.get("question"):
        _p(f"3. Why-chat on {c['flag']} ({c.get('source')}, {c.get('seconds')} s):")
        _p(f"   Q: {c['question']}")
        _p("   A: " + " ".join(str(c.get("answer") or "")[:600].split()))
    _p(f"4. Report: {res.get('report') or res.get('report_error')}")
    _p("   Results: showcase.json in the run folder")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    settings = _settings(args)
    from .workspace import Workspace

    runs = Workspace.list_runs(settings)
    if not runs:
        _p(f"no runs in {settings.workspace_path}")
        return 0
    _p(f"{'run_id':<34} {'state':<8} {'profile':<10} source")
    for r in runs[: args.limit]:
        _p(f"{r.get('run_id', ''):<34} {r.get('state', ''):<8} {r.get('profile', ''):<10} {Path(str(r.get('source_path', ''))).name}")
    return 0


# ----------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tpm", description=f"{BANNER}: autonomous data-reliability pipeline for undocumented tabular data.")
    p.add_argument("--settings", help="path to settings.yaml (default config/settings.yaml)")
    p.add_argument("--workspace", help="workspace directory (default from settings / TPM_WORKSPACE)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the pipeline on a file")
    r.add_argument("path")
    r.add_argument("--profile", choices=["no-egress", "hybrid", "eu-hosted"])
    r.add_argument("--stages", help="comma-separated subset: ingest,profile,quality,detect,diagnose,assess,report")
    r.add_argument("--opt", action="append", type=_parse_opt, metavar="KEY=VALUE", help="dataset option (has_header=false, delimiter=;, group_columns=a,b, domain_hint=..., reference_period=...)")
    r.add_argument("--rules", help="plain-language rules file (one rule per line)")
    r.add_argument("--lang", choices=["en", "fi", "sv"], help="report language")
    r.add_argument("--run-id")
    r.add_argument("--no-llm", action="store_true", help="skip every language-model call (template mode)")
    r.add_argument("--continue-on-error", action="store_true")
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("serve", help="start the web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--open", action="store_true", help="open the browser")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--profile", choices=["no-egress", "hybrid", "eu-hosted"])
    s.set_defaults(fn=cmd_serve)

    rp = sub.add_parser("replay", help="replay a run's dataset as a stream of batches")
    rp.add_argument("run_id")
    rp.add_argument("--speed", type=float, default=0.0, help="data-seconds per wall-clock second (0 = as fast as possible, 1 = real time)")
    rp.add_argument("--max-batches", type=int)
    rp.add_argument("--profile", choices=["no-egress", "hybrid", "eu-hosted"])
    rp.set_defaults(fn=cmd_replay)

    re_ = sub.add_parser("report", help="(re)generate the report: HTML, PDF document, PowerPoint deck")
    re_.add_argument("run_id")
    re_.add_argument("--format", choices=["html", "pdf", "pptx", "summary", "all"], default="html", help="html (default) | pdf | pptx | summary (one page) | all")
    re_.add_argument("--lang", help="en | fi | sv | all")
    re_.add_argument("--out", help="output file (one language and one format), otherwise an output directory")
    re_.add_argument("--pdf-engine", choices=["native", "browser"], default="native", help="native = built-in typeset PDF (default); browser = headless Edge/Chrome print of the HTML report when installed")
    re_.add_argument("--no-llm", action="store_true")
    re_.set_defaults(fn=cmd_report)

    em = sub.add_parser("email", help="e-mail the report (SMTP from .env)")
    em.add_argument("run_id")
    em.add_argument("--to", required=True, help="recipient(s), comma-separated")
    em.add_argument("--lang", choices=["en", "fi", "sv"])
    em.add_argument("--subject")
    em.add_argument("--regenerate", action="store_true")
    em.add_argument("--pdf", action="store_true", help="also attach the PDF report")
    em.add_argument("--pptx", action="store_true", help="also attach the PowerPoint deck")
    em.set_defaults(fn=cmd_email)

    ex = sub.add_parser("export", help="export report(s) (HTML, PDF, PowerPoint), decision log, ledger and artifacts as a zip")
    ex.add_argument("run_id")
    ex.add_argument("--out", help="output directory (default exports/)")
    ex.add_argument("--lang", choices=["en", "fi", "sv"])
    ex.set_defaults(fn=cmd_export)

    vl = sub.add_parser("verify-log", help="verify the decision-log hash chain and audit its completeness")
    vl.add_argument("run_id")
    vl.add_argument("--json", action="store_true", help="print the chain check and the completeness audit as JSON")
    vl.add_argument("--no-audit", action="store_true", help="only the chain check")
    vl.set_defaults(fn=cmd_verify_log)

    mo = sub.add_parser("models", help="show local/external model availability")
    mo.add_argument("--profile", choices=["no-egress", "hybrid", "eu-hosted"])
    mo.add_argument("--pull", metavar="NAME", help="download a model through the local Ollama, with progress (e.g. qwen3:4b)")
    mo.add_argument("--use", metavar="NAME", help="use this installed model from now on ('auto' = best installed model for this machine)")
    mo.add_argument("--embedding", action="store_true", help="with --use: choose the search (embedding) model instead of the chat model")
    mo.add_argument("--run", metavar="RUN_ID", help="say who wrote the explanations of this run (default: the latest run)")
    mo.set_defaults(fn=cmd_models)

    gd = sub.add_parser("guard-demo", help="prove the egress guard on a real run: before / after, and an unsafe payload that is blocked")
    gd.add_argument("--run", required=True, metavar="RUN_ID", help="run id, or 'latest'")
    gd.add_argument("--profile", choices=["hybrid", "eu-hosted"], help="the external profile the guard speaks for (default: the active one if it allows external models, else hybrid)")
    gd.add_argument("--send", action="store_true", help="afterwards send the REAL payload once through the normal router (needs the key; off by default). The unsafe payload is never sent.")
    gd.add_argument("--lang", choices=["en", "fi", "sv"], help="language of the model reply when --send is used")
    gd.add_argument("--json", action="store_true", help="print the result as JSON (it is always written to guard_demo.json)")
    gd.set_defaults(fn=cmd_guard_demo)

    bl = sub.add_parser("bench-llm", help="time a finished run's model tasks on the local and the external route")
    bl.add_argument("--run", required=True, metavar="RUN_ID", help="run id, or 'latest'")
    bl.add_argument("--tasks", help="comma-separated subset: sensor_hypotheses,diagnosis_narrative,critique,report_narrative")
    bl.add_argument("--n", type=int, default=3, help="calls per task and route (default 3)")
    bl.add_argument("--routes", help="local,external (default both; external needs a profile that allows it)")
    bl.add_argument("--no-chat", action="store_true", help="skip the chat question")
    bl.add_argument("--dry-run", action="store_true", help="stub providers on a scratch copy of the run: nothing is sent, nothing is recorded in the run")
    bl.add_argument("--profile", choices=["no-egress", "hybrid", "eu-hosted"])
    bl.set_defaults(fn=cmd_bench_llm)

    bo = sub.add_parser("bakeoff", help="run the local-model bake-off script")
    bo.add_argument("extra", nargs="*")
    bo.set_defaults(fn=cmd_bakeoff)

    de = sub.add_parser("demo", help="generate synthetic sample data and run the pipeline on it")
    de.add_argument("--profile", choices=["no-egress", "hybrid", "eu-hosted"])
    de.add_argument("--stages")
    de.add_argument("--lang", choices=["en", "fi", "sv"])
    de.add_argument("--run-id")
    de.add_argument("--no-llm", action="store_true")
    de.add_argument("--no-rules", action="store_true")
    de.set_defaults(fn=cmd_demo)

    do = sub.add_parser("doctor", help="check the environment and print fixes")
    do.add_argument("--profile", choices=["no-egress", "hybrid", "eu-hosted"])
    do.set_defaults(fn=cmd_doctor)

    sc = sub.add_parser("showcase", help="on a finished run: compile + run rules, accept/question/override diagnoses (with the downstream effect), ask the why-chat, regenerate the report")
    sc.add_argument("--run", dest="run_id", required=True)
    sc.add_argument("--rules", help="plain-language rules file (default: 4 rules written from the run's own catalogue)")
    sc.add_argument("--no-chat", action="store_true", help="skip the why-chat question (the local model can take a minute)")
    sc.add_argument("--lang", choices=["en", "fi", "sv"], default="en")
    sc.set_defaults(fn=cmd_showcase)

    li = sub.add_parser("list", help="list runs in the workspace")
    li.add_argument("--limit", type=int, default=20)
    li.set_defaults(fn=cmd_list)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    _setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except SystemExit as e:
        return int(e.code or 0)
    except KeyboardInterrupt:
        _p("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
