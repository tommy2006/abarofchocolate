"""Local-model bake-off. For every candidate model that is pulled (configured + fallbacks, or --models),
runs six representative tasks on the demo workspace and measures JSON validity, schema compliance,
tool-call success and latency. Prints a table and writes docs/bakeoff_results.md.

    .venv\\Scripts\\python.exe scripts\\bakeoff.py [--models gemma3:4b,llama3:8b] [--repeat 1] [--out docs/bakeoff_results.md]

Everything runs locally; no external model is involved.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TASKS = ["column_roles", "rule_compile", "sensor_hypotheses", "diagnosis_narrative", "critique", "tool_agent"]


def _payloads(ws):
    from tpm.llm.sandbox import catalog_payload, diagnosis_payload

    cat = catalog_payload(ws, max_signals=8)
    roles = {"signals": [{"id": s["id"], "dtype": s["dtype"], "fingerprint": {k: s["fingerprint"].get(k) for k in ("stuck_fraction", "autocorr_lag1", "quantization_step", "n_unique", "noise_level", "std", "min", "max")}, "related_signals": s["related_signals"], "evidence_ids": s["evidence_ids"]} for s in cat["signals"]], "evidence": cat["evidence"][:20]}
    top = cat["signals"][0]["id"]
    rule = {"rule_text": f"{top} must not rise faster than 5 units per sample and must stay below 3000", "signals": [{"id": s["id"], "structural_role": s["structural_role"], "fingerprint": {k: s["fingerprint"].get(k) for k in ("mean", "std", "min", "max")}} for s in cat["signals"]]}
    diag = diagnosis_payload(ws)
    return {"column_roles": roles, "rule_compile": rule, "sensor_hypotheses": cat, "diagnosis_narrative": diag, "critique": diag}


def run_model(model: str, settings, ws, repeat: int) -> dict:
    from tpm.llm import agent as agent_mod
    from tpm.llm import router
    from tpm.llm.providers import validate_schema
    from tpm.llm.prompts import schema_for

    s = settings.model_copy(deep=True)
    s.local_llm.model = model
    s.local_llm.fallback_models = []
    payloads = _payloads(ws)
    rows = []
    for task in TASKS:
        for _ in range(repeat):
            t0 = time.time()
            if task == "tool_agent":
                out = agent_mod.chat(ws, s, "Which signal contributed most to FLAG-000001, what is its mean in group 1 versus the whole dataset, and which evidence supports the flag?", context={"flag_id": "FLAG-000001"}, actor="bakeoff", max_steps=4)
                ok_json = out["source"].startswith("llm-local")
                trace = out.get("tool_trace", [])
                n_tools = len([t for t in trace if t.get("tool")])
                tool_ok = len([t for t in trace if t.get("tool") and t.get("ok")])
                rows.append({"model": model, "task": task, "json_valid": ok_json, "schema_ok": ok_json and bool(out.get("citations")), "tool_calls": n_tools, "tool_ok": tool_ok, "latency_ms": int((time.time() - t0) * 1000), "note": (out["answer"][:120].replace("\n", " ") if ok_json else out.get("source"))})
            else:
                res = router.complete(task, payloads[task], purpose="bakeoff", ws=ws, settings=s, language="en")
                parsed = res.data
                errs = validate_schema(parsed, schema_for(task)) if parsed is not None else ["no json"]
                rows.append({"model": model, "task": task, "json_valid": parsed is not None, "schema_ok": parsed is not None and not errs, "tool_calls": 0, "tool_ok": 0, "latency_ms": res.latency_ms or int((time.time() - t0) * 1000), "note": (res.error or "")[:120] if (res.error or not res.ok) else ""})
    return {"model": model, "rows": rows}


def summarize(results: list[dict]) -> list[dict]:
    out = []
    for r in results:
        rows = r["rows"]
        n = len(rows)
        out.append({
            "model": r["model"],
            "tasks": n,
            "json_valid": sum(1 for x in rows if x["json_valid"]),
            "schema_ok": sum(1 for x in rows if x["schema_ok"]),
            "tool_ok": f"{sum(x['tool_ok'] for x in rows)}/{sum(x['tool_calls'] for x in rows)}",
            "median_latency_ms": sorted(x["latency_ms"] for x in rows)[n // 2] if n else 0,
            "total_s": round(sum(x["latency_ms"] for x in rows) / 1000, 1),
        })
    return out


def write_md(path: Path, results: list[dict], summary: list[dict], settings) -> None:
    lines = [f"# Local model bake-off ({datetime.now().strftime('%Y-%m-%d %H:%M')})", "", f"Configured default: `{settings.local_llm.model}`; fallbacks: {', '.join(settings.local_llm.fallback_models)}. num_ctx={settings.local_llm.num_ctx}, temperature={settings.local_llm.temperature}.", "", "| model | tasks | JSON valid | schema ok | tool calls ok | median latency (ms) | total (s) |", "|---|---|---|---|---|---|---|"]
    for s in summary:
        lines.append(f"| {s['model']} | {s['tasks']} | {s['json_valid']} | {s['schema_ok']} | {s['tool_ok']} | {s['median_latency_ms']} | {s['total_s']} |")
    lines += ["", "## Per task", "", "| model | task | JSON | schema | tools ok/called | latency (ms) | note |", "|---|---|---|---|---|---|---|"]
    for r in results:
        for x in r["rows"]:
            lines.append(f"| {x['model']} | {x['task']} | {'yes' if x['json_valid'] else 'no'} | {'yes' if x['schema_ok'] else 'no'} | {x['tool_ok']}/{x['tool_calls']} | {x['latency_ms']} | {x['note'].replace('|', '/')} |")
    lines += ["", "Tasks: column_roles / rule_compile / sensor_hypotheses / diagnosis_narrative / critique (JSON with schema) and a 3-step tool-agent question on the demo workspace.", "Selection rule: highest schema-ok count, then tool-call success, then latency. Switch with `TPM_LOCAL_MODEL=<model>` or `local_llm.model` in config/settings.yaml."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", default=None, help="comma-separated models to test (default: configured + fallbacks that are pulled)")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--out", default=str(ROOT / "docs" / "bakeoff_results.md"))
    args = ap.parse_args()

    from tpm.config import load_settings
    from tpm.llm.providers import OllamaProvider
    from tpm.llm.sandbox import make_demo_workspace

    settings = load_settings(profile="no-egress")
    prov = OllamaProvider(settings)
    if not prov.is_available():
        print(f"Ollama not reachable at {settings.local_llm.base_url}; nothing to bake off. Run scripts/check_models.py.")
        return 1
    pulled = prov.list_models()
    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        from tpm.llm.providers import _same_model

        models = [p for c in prov.candidate_models() for p in pulled if _same_model(p, c)]
    if not models:
        print("None of the candidate models is pulled. Missing: " + ", ".join(prov.pull_commands()))
        return 1
    print("Models under test:", ", ".join(models))
    tmp = tempfile.mkdtemp(prefix="tpm_bakeoff_")
    ws = make_demo_workspace(settings, root=tmp, run_id="bakeoff")
    results = []
    for m in models:
        print(f"\n== {m} ==")
        r = run_model(m, settings, ws, args.repeat)
        for x in r["rows"]:
            print(f"  {x['task']:<20} json={'Y' if x['json_valid'] else 'n'} schema={'Y' if x['schema_ok'] else 'n'} tools={x['tool_ok']}/{x['tool_calls']} {x['latency_ms']:>7} ms  {x['note'][:70]}")
        results.append(r)
    summary = summarize(results)
    print("\n" + json.dumps(summary, indent=1))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_md(out, results, summary, settings)
    print(f"\nwritten {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
