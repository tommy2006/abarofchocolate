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

## 2026-09-19 01:10 — First 6 GB run: 60 min, all batches "untrusted", 22/21000 groups flagged; fixed
Findings (workspace/te_full of the first run):
- detect 47 min: evaluation loop did a full 15M-row mask per group (21k groups) = 34 min; fit-sample range join over 21k blocks = 6 min; per-group rich event loop (DuckDB re-score + ruptures per event) could only cover 182 groups.
- quality: "S01 frozen at 0 for 367 samples" fired on 30/52 signals per batch (real zero-held actuator/flow runs in a few of the ~3000 groups per 1.5M-row batch) -> every batch untrusted -> every diagnosis "data".
Fixes: tpm/quality/trust.py (exposure-weighted severity; batch-wide only if >= 5 % of batch rows or effective severity >= 0.35; otherwise TrustVerdict.local_untrusted row ranges; n_rows stored), tpm/quality/checks.py stuck severity/status, tpm/detect/evaluate.py O(N) loop, tpm/detect/_common.fetch_blocks row-id semi-join, tpm/detect/events.py two-tier (scan all groups -> group_scores.json; rich for strongest <= 600 within 60 % of remaining budget; summary flags from scoring-pass attribution for the rest; cap 4000), trust_context honours local ranges, detect.time_budget_s 480.
Tests: A 26, B 74, C 20 (+1 xfail), D 28, E 36, F 20 all green after fixes.
Pending: second full run (started 01:05) -> verify < 20 min, trust sane, diagnoses not all "data"; then push.

## 2026-09-19 02:30 — Second 6 GB run (23.6 min, trust sane) exposed calibration + sampling bias; fixed
Findings: normal runs had 27-30 % of rows above threshold (0.8 % in the first 20 samples). Causes: (1) plan_blocks put every group's single sample block at the group's first rows -> fit sample = the seeded start-up transient repeated across classes; (2) consensus-of-modes baseline = low-variance core -> thresholds too tight for the normal regime's wandering; (3) fits of all 7 detectors on all 5 folds (262 s) before any were dropped for speed; (4) rich events got 5 s because the budget was gone; (5) evaluation's "most frequent label = normal" is wrong for balanced classes (AUROC inverted).
Fixes: _common.plan_blocks spreads blocks over a subset of groups at golden-ratio offsets; ensemble._fit_fold dilates the calibration mask +-window rows within blocks (group-wide extension was tried and rejected: it pulled a fault's own rows into calibration); fit_fold_models runs a fold-0 speed pilot before fitting the other folds; final model fitted with selected detectors only; events rich pass >= 45 s; evaluate.py picks "normal" as the label value with the lowest mean score and states it.
Tests: C 20 passed + 1 xfail; B 74; third full run started 02:25.
