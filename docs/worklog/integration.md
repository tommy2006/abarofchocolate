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

## 2026-09-18 23:55 — All six agents delivered; integration pass
Done:
- Full suite before fixes: 203 passed, 1 xfail, 1 failed (test_e stream replay: per-batch LLM diagnosis made replay exceed the test's 10 s wait), 1 error (tests/test_c_common.py::test_settings was a helper named like a test).
- Fixes: tpm/pipeline.process_batch now calls diagnose_flags(use_llm=False) (streaming path is template-only; LLM explanations on demand via chat); helper renamed make_settings; tpm/llm/agent.py rejects placeholder final answers ("answer the question") and nudges the model once; tpm/llm/ledger.data_flow_statement now names the local model actually used (fallback) not only the configured one; guard numeric budget raised 400 -> 4000 so a large signal catalog can go external in hybrid mode; autoencoder ConvergenceWarnings silenced (capped iterations are intentional).
- Verified through the API on the real demo run: flags/diagnoses/steps/assessor verdict/chat/assessor.ask/ledger/log chain/FI report all good. Report "encoding bug" was a console display artifact (file is clean UTF-8).
- Full 6 GB run started: `python -m tpm run te_process.csv --run-id te_full` (workspace/te_full/status.json shows progress).
Pending:
- Result of the 6 GB run (time budget 20 min) and any fixes it needs.
- Commit + push to origin main; final launch instructions for the team (ollama pull gemma4:e4b-it-qat + nomic-embed-text).
How to continue:
- `.venv\Scripts\python.exe -m pytest tests -q` must be green; then `python -m tpm serve --open`.
