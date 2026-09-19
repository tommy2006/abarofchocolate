"""Command line for the Trustworthy Process Monitor.

    python -m tpm run <path> [--profile no-egress|hybrid|eu-hosted] [--stages a,b] [--opt k=v] [--rules FILE] [--lang en]
    python -m tpm serve [--host 127.0.0.1] [--port 8000] [--open]
    python -m tpm replay <run_id> [--speed 10] [--max-batches N]
    python -m tpm report <run_id> [--lang en|fi|sv] [--out FILE] [--no-llm]
    python -m tpm email <run_id> --to a@b.c [--lang en]
    python -m tpm export <run_id> [--out DIR]
    python -m tpm verify-log <run_id>
    python -m tpm models | bakeoff | demo | doctor | list
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
        options["report_llm"] = False
        options["use_llm"] = False
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
    settings = _settings(args)
    ws = _open_ws(args.run_id, settings)
    from .report import generate_report

    langs = [args.lang] if args.lang and args.lang != "all" else settings.report.languages
    try:
        for lang in langs:
            out = generate_report(ws, settings, lang, use_llm=not args.no_llm, out_path=args.out if len(langs) == 1 else None)
            _p(f"Report written: {out}")
        return 0
    finally:
        ws.close()


def cmd_email(args: argparse.Namespace) -> int:
    settings = _settings(args)
    ws = _open_ws(args.run_id, settings)
    from .report import SmtpNotConfigured, email_report

    try:
        res = email_report(ws, settings, args.to, lang=args.lang or settings.report.default_language, subject=args.subject, regenerate=args.regenerate)
        _p(f"Sent {Path(res['path']).name} to {', '.join(res['to'])} via {res['host']}")
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
    settings = _settings(args)
    ws = _open_ws(args.run_id, settings)
    try:
        res = ws.log.verify_chain()
        n = ws.log.count()
    finally:
        ws.close()
    if res.get("ok"):
        _p(f"OK: hash chain intact, {res['checked']} entries verified ({n} total)")
        return 0
    _p(f"FAIL: chain broken at seq {res.get('first_bad_seq')} after {res.get('checked')} good entries")
    return 1


def _ollama_tags(base_url: str, timeout: float = 2.5) -> Optional[list[dict[str, Any]]]:
    try:
        import httpx

        r = httpx.get(base_url.rstrip("/") + "/api/tags", timeout=timeout)
        if r.status_code == 200:
            return r.json().get("models", [])
    except Exception:
        return None
    return None


def cmd_models(args: argparse.Namespace) -> int:
    settings = _settings(args)
    from .llm import available

    avail = available()
    _p(f"Profile: {settings.profile}  (external allowed: {settings.active_profile.allow_external})")
    _p(f"LLM layer says: {json.dumps(avail, default=str)}")
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
    return 0


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

    mods = ["pandas", "numpy", "scipy", "sklearn", "lightgbm", "duckdb", "pyarrow", "fastapi", "uvicorn", "pydantic", "yaml", "jinja2", "httpx", "anthropic", "openpyxl", "psutil", "dotenv", "plotly", "ruptures"]
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
        names = {m.get("name") for m in tags}
        if settings.local_llm.model in names:
            ok(f"Ollama reachable; configured model {settings.local_llm.model} is pulled")
        else:
            fb = [m for m in settings.local_llm.fallback_models if m in names]
            warn(f"Ollama reachable but {settings.local_llm.model} is not pulled" + (f" (fallbacks available: {', '.join(fb)})" if fb else ""), f"ollama pull {settings.local_llm.model}")

    if (ROOT / ".env").exists():
        ok(".env present")
    else:
        warn(".env missing (defaults are used)", "copy .env.example to .env")
    if settings.active_profile.allow_external and not settings.external_llm.api_key:
        warn(f"profile {settings.profile} allows external calls but {settings.external_llm.api_key_env} is not set", f"add {settings.external_llm.api_key_env}=... to .env or use the no-egress profile")

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

    re_ = sub.add_parser("report", help="(re)generate the HTML report")
    re_.add_argument("run_id")
    re_.add_argument("--lang", help="en | fi | sv | all")
    re_.add_argument("--out", help="output file (single language only)")
    re_.add_argument("--no-llm", action="store_true")
    re_.set_defaults(fn=cmd_report)

    em = sub.add_parser("email", help="e-mail the report (SMTP from .env)")
    em.add_argument("run_id")
    em.add_argument("--to", required=True, help="recipient(s), comma-separated")
    em.add_argument("--lang", choices=["en", "fi", "sv"])
    em.add_argument("--subject")
    em.add_argument("--regenerate", action="store_true")
    em.set_defaults(fn=cmd_email)

    ex = sub.add_parser("export", help="export report(s), decision log, ledger and artifacts as a zip")
    ex.add_argument("run_id")
    ex.add_argument("--out", help="output directory (default exports/)")
    ex.add_argument("--lang", choices=["en", "fi", "sv"])
    ex.set_defaults(fn=cmd_export)

    vl = sub.add_parser("verify-log", help="verify the decision-log hash chain")
    vl.add_argument("run_id")
    vl.set_defaults(fn=cmd_verify_log)

    mo = sub.add_parser("models", help="show local/external model availability")
    mo.add_argument("--profile", choices=["no-egress", "hybrid", "eu-hosted"])
    mo.set_defaults(fn=cmd_models)

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
