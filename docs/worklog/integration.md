# Worklog: integration (lead)

## 2026-09-18 21:10 — Foundation written and verified
Done:
- Merged the two pairs' answers into docs/DECISIONS.md (merge rules: non-default > default; more rigorous wins; on conflict, best unsupervised quality).
- docs/ARCHITECTURE.md: module ownership, stage/artifact contracts, trust conventions, routing table, detection design.
- Foundation code: tpm/contracts.py, tpm/config.py (+ config/settings.yaml with profiles no-egress / hybrid / eu-hosted), tpm/workspace.py (artifact I/O, evidence + inference registries, DuckDB handle), tpm/memory.py, tpm/log/decision_log.py (SQLite hash chain, verify, export), tpm/llm/__init__.py (single LLM entry point, template fallback), tpm/pipeline.py (stage orchestrator, process_batch, apply_decision), tests/fixtures/synth.py (generic synthetic generator with hidden truth), tests/conftest.py, requirements.txt, .gitignore, .env.example, config/rules.example.md.
- Smoke test passed: settings routing, pipeline skeleton (all stages "skipped" gracefully), evidence/inference registries, human decision -> log, hash chain verify, DuckDB.
- .venv created with all deps (Python 3.12).
- git init on main, remote origin = https://github.com/tommy2006/abarofchocolate.git (repo is empty upstream).
- Spawned 6 agents in parallel: A ingest+profile, B quality+assessor, C detect+diagnose, D llm, E api+ui, F platform/report/cli/docs. Each logs to docs/worklog/agent_*.md.

Pending (after agents finish):
- Run every agent's tests: .venv\Scripts\python.exe -m pytest -q
- End-to-end on samples/demo_process.csv via `python -m tpm run` and via the UI.
- Full-file run on te_process.csv as the product's own test (time budget 20 min; memory-bounded); fix what breaks.
- README/launcher check on a clean shell; push to origin main.

How to continue if the lead stops:
- Read every docs/worklog/agent_*.md "How to continue" section; run the pytest commands listed there.
- Integration order: ingest -> profile -> quality -> detect -> diagnose -> assess -> report; run `python -m tpm run samples/demo_process.csv` and fix the first failing stage (status.json in workspace/<run_id>/ has the traceback).
