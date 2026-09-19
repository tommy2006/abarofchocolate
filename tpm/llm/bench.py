"""`tpm bench-llm`: how long do the model tasks of a finished run take on the local model and on the external one?

    result = run_benchmark(ws, settings, tasks=None, n=3, routes=("local", "external"), chat=True, dry_run=False)
    print(format_table(result))          # also written to workspace/<run>/llm_benchmark.json

The payloads are rebuilt from the run's artifacts with the builders the stages themselves use (sensor_hypotheses,
diagnosis_narrative, critique, report_narrative), and one chat question goes through the tool agent. Every call takes
the normal router path (egress guard, run budget, ledger); the route is forced per call through a COPY of the settings
whose profile routing is overridden, the global settings are never touched. The external route is only measured when
the active profile allows it (run with --profile hybrid): a benchmark never opens a route the operator closed.

dry_run=True replaces both providers by stubs that answer at once and works on a scratch copy of the run, so that the
real run's ledger, chat history and decision log get no entries for calls that never happened. Its numbers say nothing
about a model; they go to llm_benchmark.dry_run.json, never to the file the Data-flow view shows.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import statistics
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from ..config import Profile, Settings
from ..contracts import now_iso

BENCH_FILE = "llm_benchmark.json"
DRY_RUN_FILE = "llm_benchmark.dry_run.json"
BENCH_TASKS = ["sensor_hypotheses", "diagnosis_narrative", "critique", "report_narrative"]
CHAT_TASK = "why_chat"
CHAT_QUESTION = "Which flags are the most severe, and which signals drive them?"
ROUTES = ("local", "external")
STUB_SLEEP_S = 0.01
STUB_TEXT = "Dry run: no model was asked. This stub sentence only exercises the benchmark plumbing."
SCRATCH_COPY_MAX_BYTES = 64_000_000  # larger files are hard-linked into the scratch copy, or left out

ProgressFn = Optional[Callable[[str], None]]


# ----------------------------------------------------------------------------------------------
# payloads: the same builders the stages use
# ----------------------------------------------------------------------------------------------


def task_jobs(ws: Any, settings: Settings, tasks: list[str], n: int, language: str = "en") -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """({task: [kwargs of complete(), one per repetition]}, notes). Narrative and critique use the n strongest event
    diagnoses of the run in turn; the other tasks repeat their one payload."""
    jobs: dict[str, list[dict[str, Any]]] = {}
    notes: list[str] = []
    diags = [d for d in ws.diagnoses() if d.fault_type != "isolated suspicious readings"][: max(1, n)] if any(t in tasks for t in ("diagnosis_narrative", "critique")) else []
    for task in tasks:
        if task == "sensor_hypotheses":
            from ..profile.roles import SENSOR_HYPOTHESES_SCHEMA, build_llm_payload

            schema = ws.schema()
            descriptors = ws.signals()
            if schema is None or not descriptors:
                notes.append("sensor_hypotheses skipped: the run has no signal catalog")
                continue
            payload = build_llm_payload(descriptors, ws.read_json("relations", {}) or {}, dict(schema.domain_likelihood or {}), None)
            jobs[task] = [dict(task=task, payload=payload, purpose="benchmark: propose instrument/unit-operation roles", schema=SENSOR_HYPOTHESES_SCHEMA, max_tokens=1500)] * n
        elif task in ("diagnosis_narrative", "critique"):
            from ..diagnose.critique import cited_evidence, code_checks, objections_payload
            from ..diagnose.diagnosis import narrative_payload

            if not diags:
                notes.append(f"{task} skipped: the run has no event diagnoses")
                continue
            jobs[task] = []
            for i in range(n):
                d = diags[i % len(diags)]
                if task == "diagnosis_narrative":
                    payload = narrative_payload(ws, d)
                else:
                    checks = list(d.critique.checks) if d.critique else code_checks(ws, d, {}, ws.read_json("baseline", {}) or {}, {}, int(settings.detect.window))
                    payload = objections_payload(d, checks, cited_evidence(ws, d))
                jobs[task].append(dict(task=task, payload=payload, purpose=f"benchmark: {'explain' if task == 'diagnosis_narrative' else 'critique'} {d.id}", language=language))
        elif task == "report_narrative":
            from ..report.report import LLM_MAX_TOKENS, collect

            payload = collect(ws, settings, language, use_llm=False).get("llm_payload") or {}
            jobs[task] = [dict(task=task, payload=payload, purpose=f"benchmark: report summary ({language})", language=language, max_tokens=LLM_MAX_TOKENS)] * n
        else:
            notes.append(f"{task} skipped: not a benchmark task ({', '.join(BENCH_TASKS)})")
    return jobs, notes


def forced_route(settings: Settings, route: str, tasks: list[str]) -> Settings:
    """A copy of the settings whose active profile sends `tasks` to `route`. allow_external is NOT changed: under a
    profile that forbids external models route_for() keeps answering 'local'."""
    s = settings.model_copy(deep=True)
    prof = s.profiles.get(s.profile) or Profile()
    prof.routing = {**(prof.routing or {}), **{t: route for t in tasks}}
    s.profiles[s.profile] = prof
    return s


# ----------------------------------------------------------------------------------------------
# dry run: stub providers, scratch copy of the run
# ----------------------------------------------------------------------------------------------


def _stub_value(key: str, schema: Any) -> Any:
    if not isinstance(schema, dict):
        return STUB_TEXT
    if schema.get("enum"):
        return next((v for v in schema["enum"] if v is not None), None)
    t = schema.get("type")
    t = next((x for x in t if x != "null"), "string") if isinstance(t, list) else t
    if t == "object":
        return {k: _stub_value(k, sub) for k, sub in (schema.get("properties") or {}).items() if k in (schema.get("required") or [])}
    if t == "array":
        return []
    if t in ("number", "integer"):
        return 1 if t == "integer" else 0.5
    if t == "boolean":
        return False
    return "final" if key == "action" else STUB_TEXT  # the tool agent's step schema: "final" ends the turn


def stub_reply(schema: Optional[dict[str, Any]]) -> tuple[str, Any]:
    """(text, parsed) a stub provider returns: the smallest object the task's schema accepts, or a sentence."""
    if not schema:
        return STUB_TEXT, None
    obj = _stub_value("", schema if schema.get("type") == "object" else {"type": "object"})
    if "answer" in (schema.get("properties") or {}):
        obj["answer"] = STUB_TEXT
    return json.dumps(obj), obj


@contextmanager
def stub_providers() -> Iterator[None]:
    """Every provider answers after STUB_SLEEP_S with stub_reply(); no network client is built and Ollama is not
    contacted. Class attributes are swapped for the duration of the block and restored afterwards."""
    from .providers import AnthropicProvider, OllamaProvider, OpenAICompatProvider

    def ext_chat(self, messages, schema=None, max_tokens=None, model=None):
        time.sleep(STUB_SLEEP_S)
        self.last_usage = {"input_tokens": 0, "output_tokens": 0}
        text, parsed = stub_reply(schema)
        return text, parsed, int(STUB_SLEEP_S * 1000)

    def local_chat(self, messages, schema=None, max_tokens=None, model=None, temperature=None):
        time.sleep(STUB_SLEEP_S)
        text, parsed = stub_reply(schema)
        return text, parsed, int(STUB_SLEEP_S * 1000)

    def no_client(self):
        raise RuntimeError("dry run: the network client is never built")

    swaps = [
        (AnthropicProvider, "chat", ext_chat), (AnthropicProvider, "is_available", lambda self: True), (AnthropicProvider, "_client", no_client),
        (OpenAICompatProvider, "chat", ext_chat), (OpenAICompatProvider, "is_available", lambda self: True), (OpenAICompatProvider, "_client", no_client),
        (OllamaProvider, "chat", local_chat), (OllamaProvider, "is_available", lambda self: True), (OllamaProvider, "pick_model", lambda self: "stub-local"),
        (OllamaProvider, "list_models", lambda self: ["stub-local"]),
    ]
    saved = [(cls, name, cls.__dict__.get(name)) for cls, name, _ in swaps]
    try:
        for cls, name, fn in swaps:
            setattr(cls, name, fn)
        yield
    finally:
        for cls, name, old in saved:
            if old is None:
                delattr(cls, name)
            else:
                setattr(cls, name, old)


@contextmanager
def scratch_copy(ws: Any, settings: Settings) -> Iterator[Any]:
    """A throw-away copy of the run directory (big files hard-linked, the decision log left out) opened as a Workspace."""
    from ..workspace import Workspace

    tmp = Path(tempfile.mkdtemp(prefix="tpm_bench_"))
    dst = tmp / ws.run_id
    dst.mkdir(parents=True)
    for p in ws.dir.iterdir():
        if not p.is_file() or p.name.startswith("decision_log.sqlite"):
            continue
        if p.stat().st_size <= SCRATCH_COPY_MAX_BYTES:
            shutil.copy2(p, dst / p.name)
            continue
        try:
            os.link(p, dst / p.name)
        except OSError:
            pass  # another volume: the dry run works without the big file (tools that need it answer with an error)
    copy = Workspace(ws.run_id, settings=settings, root=tmp)
    try:
        yield copy
    finally:
        copy.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------------------------------
# measurement
# ----------------------------------------------------------------------------------------------


def _machine(settings: Settings) -> dict[str, Any]:
    out: dict[str, Any] = {"platform": platform.platform(), "processor": platform.processor() or platform.machine(), "cpu_count": os.cpu_count(), "python": platform.python_version()}
    try:
        from ..memory import memory_snapshot

        out["memory"] = memory_snapshot()
    except Exception:
        pass
    out["local_model_configured"] = settings.local_llm.model
    out["external_model"] = settings.external_model_for(None)
    return out


def _row(task: str, route: str, seconds: list[float], ok_seconds: list[float], model: str, answered: dict[str, int], errors: list[str], skipped: Optional[str] = None) -> dict[str, Any]:
    row: dict[str, Any] = {"task": task, "route": route, "model": model, "n": len(seconds), "ok": len(ok_seconds), "median_s": round(statistics.median(ok_seconds), 3) if ok_seconds else None, "mean_s": round(statistics.fmean(ok_seconds), 3) if ok_seconds else None}
    if ok_seconds:
        row["min_s"], row["max_s"] = round(min(ok_seconds), 3), round(max(ok_seconds), 3)
    if answered:
        row["answered_by"] = answered  # a call the requested route could not answer falls back (external -> local -> template)
    if errors:
        row["errors"] = errors[:3]
    if skipped:
        row["skipped"] = skipped
    return row


def _measure(ws: Any, settings: Settings, task: str, route: str, jobs: list[dict[str, Any]], say: Callable[[str], None]) -> dict[str, Any]:
    from . import complete

    seconds: list[float] = []
    ok_seconds: list[float] = []
    answered: dict[str, int] = {}
    errors: list[str] = []
    model = ""
    for i, job in enumerate(jobs):
        kwargs = {k: v for k, v in job.items() if k not in ("task", "payload")}
        t0 = time.time()
        res = complete(task, job["payload"], ws=ws, settings=settings, **kwargs)
        dt = time.time() - t0
        seconds.append(dt)
        answered[res.route] = answered.get(res.route, 0) + 1
        if res.ok and res.route == route:  # only calls the requested route answered itself count
            ok_seconds.append(dt)
            model = res.model or model
        elif res.error:
            errors.append(str(res.error)[:200])
        say(f"  {task:<20} {route:<9} call {i + 1}/{len(jobs)}: {dt:6.2f} s  ({res.source}{'' if res.ok else ', failed'})")
        if res.route != route:
            break  # the route did not answer (the call fell back): repeating it would only keep the fallback model busy
    return _row(task, route, seconds, ok_seconds, model or (settings.external_model_for(task) if route == "external" else settings.local_llm.model), answered, errors)


def _measure_chat(ws: Any, settings: Settings, route: str, question: str, say: Callable[[str], None]) -> dict[str, Any]:
    from . import chat

    t0 = time.time()
    out = chat(ws, settings, question, context=None, history=[], actor="system:bench-llm", task=CHAT_TASK, language="en")
    dt = time.time() - t0
    say(f"  {'chat (' + CHAT_TASK + ')':<20} {route:<9} 1 turn:    {dt:6.2f} s  ({out.get('source')})")
    return {"route": route, "answered_by": out.get("route") or "none", "source": out.get("source"), "ok": out.get("route") == route, "seconds": round(dt, 3), "tool_calls": len([t for t in out.get("tool_trace") or [] if t.get("tool")]), "external_calls": int(out.get("external_calls") or 0), "error": out.get("error")}


def _concurrent(ws: Any, settings: Settings, jobs: dict[str, list[dict[str, Any]]], say: Callable[[str], None]) -> Optional[dict[str, Any]]:
    """One call per task at the same time, the way the diagnose stage sends its narratives: wall clock against the sum
    of the single latencies."""
    from . import complete_many

    batch = [dict(js[0]) for js in jobs.values() if js]
    if len(batch) < 2:
        return None
    t0 = time.time()
    results = complete_many(batch, ws=ws, settings=settings)
    wall = time.time() - t0
    ok = [r for r in results if r.ok and r.route == "external"]
    total = sum((r.latency_ms or 0) for r in ok) / 1000.0
    say(f"  {len(batch)} external calls at once: {wall:6.2f} s wall clock, {total:.2f} s of model time")
    return {"jobs": len(batch), "ok": len(ok), "max_parallel": int(settings.external_llm.max_parallel), "wall_s": round(wall, 3), "sum_latency_s": round(total, 3)}


def _speedup(rows: list[dict[str, Any]], chat_rows: list[dict[str, Any]], concurrent: Optional[dict[str, Any]]) -> dict[str, Any]:
    med = {(r["task"], r["route"]): r["median_s"] for r in rows if r.get("median_s")}
    per_task = {t: round(med[(t, "local")] / med[(t, "external")], 2) for t in sorted({t for t, _ in med}) if (t, "local") in med and (t, "external") in med}
    both = [t for t in per_task]
    out: dict[str, Any] = {"definition": "median seconds on the local route divided by median seconds on the external route; above 1 means the external model is faster", "per_task": per_task}
    if both:
        out["all_tasks"] = round(sum(med[(t, "local")] for t in both) / sum(med[(t, "external")] for t in both), 2)
    chat_s = {c["route"]: c["seconds"] for c in chat_rows if c.get("ok") and c.get("seconds")}
    if "local" in chat_s and "external" in chat_s:
        out["chat"] = round(chat_s["local"] / chat_s["external"], 2)
    if concurrent and concurrent.get("wall_s") and concurrent.get("ok"):
        out["concurrency"] = round(concurrent["sum_latency_s"] / concurrent["wall_s"], 2)
    return out


def run_benchmark(ws: Any, settings: Settings, tasks: Optional[list[str]] = None, n: int = 3, routes: tuple[str, ...] = ROUTES, chat: bool = True, dry_run: bool = False, question: str = CHAT_QUESTION, progress: ProgressFn = None) -> dict[str, Any]:
    """Time the run's model tasks per route and write the result next to the run. Never raises for a model problem: a
    route that cannot be used is reported as skipped with the reason."""
    say = progress or (lambda msg: None)
    tasks = [t for t in (tasks or BENCH_TASKS)]
    n = max(1, int(n))
    result: dict[str, Any] = {"generated_at": now_iso(), "run_id": ws.run_id, "profile": settings.profile, "dry_run": bool(dry_run), "n_requested": n, "machine": _machine(settings), "tasks": [], "chat": {}, "speedup": {}, "notes": []}

    def body(run_ws: Any) -> None:
        from . import external_ready, usage

        jobs, notes = task_jobs(run_ws, settings, tasks, n)
        result["notes"] += notes
        chat_rows: list[dict[str, Any]] = []
        concurrent = None
        for route in [r for r in ROUTES if r in routes]:
            s_route = forced_route(settings, route, list(jobs) + [CHAT_TASK])
            skipped = None
            if route == "external":
                ready, why = external_ready(next(iter(jobs), CHAT_TASK), run_ws, s_route)
                skipped = None if ready else why
            if skipped:
                say(f"  external route not measured: {skipped}")
                result["notes"].append(f"external route not measured: {skipped}")
                result["tasks"] += [_row(t, route, [], [], s_route.external_model_for(t), {}, [], skipped=skipped) for t in jobs]
                continue
            for task, task_jobs_ in jobs.items():
                result["tasks"].append(_measure(run_ws, s_route, task, route, task_jobs_, say))
            if route == "external":
                concurrent = _concurrent(run_ws, s_route, jobs, say)
            if chat:
                chat_rows.append(_measure_chat(run_ws, s_route, route, question, say))
        result["chat"] = {"task": CHAT_TASK, "question": question, "turns": chat_rows}
        if concurrent:
            result["concurrent"] = concurrent
        result["speedup"] = _speedup(result["tasks"], chat_rows, concurrent)
        # what the Data-flow view reads next to the rows (GET /api/runs/{id}/llm/usage)
        result.update(created_at=result["generated_at"], n=n, local_model=next((r["model"] for r in result["tasks"] if r["route"] == "local" and r["ok"]), settings.local_llm.model), external_model=settings.external_model_for(None))
        if not dry_run:
            result["usage"] = usage(run_ws, settings)

    if dry_run:
        with stub_providers(), scratch_copy(ws, settings) as copy:
            body(copy)
        result["notes"].append("dry run: both providers were stubs; the timings measure the plumbing, not a model")
    else:
        body(ws)
    ws.write_json(DRY_RUN_FILE if dry_run else BENCH_FILE, result)
    return result


def format_table(result: dict[str, Any]) -> str:
    fmt = lambda v: f"{v:9.2f}" if isinstance(v, (int, float)) else f"{'-':>9}"  # noqa: E731
    lines = [f"{'task':<22}{'route':<10}{'model':<28}{'n':>3}{'ok':>4}{'median s':>10}{'mean s':>10}"]
    order = {t: i for i, t in enumerate(BENCH_TASKS)}
    for r in sorted(result.get("tasks", []), key=lambda r: (order.get(r["task"], 99), ROUTES.index(r["route"]) if r["route"] in ROUTES else 9)):
        lines.append(f"{r['task']:<22}{r['route']:<10}{str(r.get('model') or '')[:27]:<28}{r['n']:>3}{r['ok']:>4} {fmt(r.get('median_s'))} {fmt(r.get('mean_s'))}" + (f"   skipped: {r['skipped']}" if r.get("skipped") else ""))
    for c in (result.get("chat") or {}).get("turns", []):
        lines.append(f"{'chat (' + CHAT_TASK + ')':<22}{c['route']:<10}{str(c.get('source') or '')[:27]:<28}{1:>3}{1 if c.get('ok') else 0:>4} {fmt(c.get('seconds'))} {fmt(c.get('seconds'))}" + ("" if c.get("ok") else f"   answered by: {c.get('answered_by')}"))
    sp = result.get("speedup") or {}
    if sp.get("per_task"):
        lines.append("")
        lines.append("Speed-up (local median / external median): " + ", ".join(f"{t} {x}x" for t, x in sp["per_task"].items()) + (f"; all tasks {sp['all_tasks']}x" if sp.get("all_tasks") else "") + (f"; chat {sp['chat']}x" if sp.get("chat") else ""))
    c = result.get("concurrent")
    if c:
        lines.append(f"Concurrency: {c['jobs']} external calls at once took {c['wall_s']} s wall clock for {c['sum_latency_s']} s of model time.")
    for note in result.get("notes", []):
        lines.append(f"Note: {note}")
    return "\n".join(lines)
