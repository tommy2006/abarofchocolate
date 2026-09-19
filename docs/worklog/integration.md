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

## 2026-09-19 03:30 — Runs 3-4 on the 6 GB file and the 2M-row slice; detection quality fixed
- Run 3 failed in attribution (onset placeholder shape when corr_break residuals were absent) -> fixed + regression test; corr_break kept in fold fits (ESSENTIAL_DETECTORS).
- Run 4 (21.3 min): false alarms gone but detection collapsed (even faults 1/4/7 at 0 %): spread-only sampling made ~80 % of the fit sample faulty; consensus-of-modes accepted settled fault states. Fix: plan_blocks samples a START block + a spread block per chosen group; new baseline candidate `early_segment` (temporal prior, cut at first change in any channel incl. levels; scored like the others; assumption stated).
- 2M slice (all 21 classes, 4000 groups): normal runs 0 % flagged, big faults 74-100 % post-onset, group false-alarm 0.2-0.6 %, AUROC 0.87, pattern-vs-fault AMI 0.74. Baseline winner robust_covariance (0.934) ~ consensus_of_modes (0.934) > early_segment (0.90).
- Slice exposed: (a) simulationRun leaked into signals (500 values -> not "low cardinality") -> schema rule: numeric columns constant within >=98 % of groups and varying across groups are per-group id/label, never signals; (b) 41 frozen runs = 5.7 % of a 200k batch made 25 signals batch-wide untrusted -> BATCH_MIN_EXPOSURE 0.25 (row-scoped below), verdict statement "usable overall" for local-only.
- Budgets: detect 450 s, assessor experiments 90 s (2M slice took 14 min end-to-end; full file projected ~20-21 min).
Pending: slice re-run (started 03:25) -> full run -> push to origin main -> tell the team to restart the launcher.

## 2026-09-19 05:00 — Team UI review: 8 items split with one UI subagent
Lead half (done, committed 9ce72b8): tpm/api/plain.py (+ GET /api/runs/{id}/plain?view=&lang=&enhance=) plain-language paragraphs per view (template first, local-model rewording/translation cached per artifact stamp); /understanding normalised (summary/assumptions/uncertain/hypotheses/signal_plain) so view 1 is no longer empty; understanding.js shows the plain box, a per-signal plain description and honours ?signal=; diagnosis summaries are prose (plain_summary) and the model's JSON narrative is parsed into summary/steps/uncertainty, never appended raw; critique objections parsed from {text, evidence_ids}.
Subagent half (in progress): clickable references + back navigation, evidence panels, prose rendering in diagnoses view, assessor initial content from the real assessor.json shape, data-flow/report overflow CSS, plain box hooks in quality/monitor/diagnoses/assessor, POST /api/demo -> real pipeline.
2M slice after schema/trust fixes: 52 signals (labels excluded), 0 untrusted batches (row-scoped ranges), diagnoses process 1137 / unknown 963 / sensor 128 / data 52, AUROC 0.89, group false-alarm 0.0, AMI 0.79, 14.2 min.
Final 6 GB run started as te_full_v2 (te_full's SQLite is held open by the team's running server).

## 2026-09-19 06:10 — Final 6 GB run (te_full_v2): 22.6 min under load, detection sound
ingest 266 s, profile 105, quality 171, detect 556 (robust_z+ewma; pca dropped for budget under CPU contention), diagnose 132, assess 110, report 14. Trust: 0 untrusted batches, ~320 row-scoped ranges per batch, 0.64 (duplicate rows 4 % batch-level). Flags 4014 in 3756 groups (11962 over threshold; rest in group_scores.json). Diagnoses 3758: process 1559, sensor 126, unknown 2063, data 10; narratives prose. Eval: AUROC 0.85, group detection 60 %, false alarm 0.0, delay 163 rows, AMI 0.60; normal runs 0 % flagged; big faults 95-99 % post-onset; subtle classes 5/10/16/19/20 undetected (known-hard); 8/12/13/14/17/18 at 46-91 %.
Machine was shared with the team's server + UI subagent tests; unloaded estimate ~20 min.

## 2026-09-19 06:40 — UI review round complete (subagent) + foundation fixes
Subagent delivered: linkifyRefs + delegated ref handler + in-app Back (core.js/app.js), evidence panels for all roles, prose diagnoses (cleanText), assessor initial content from the real assessor.json, overflow CSS (profiles, report contents, topbar at 1280 px), plain-box hooks in quality/monitor/diagnoses/assessor, POST /api/demo runs the real pipeline; +114 i18n keys; tests test_e_api 36 + test_e_ui_assets 19.
Root cause of "Nothing here yet.": tpm/workspace._Registry loaded evidence.jsonl once; the API's cached Workspace (opened before the run wrote evidence) never saw it. Fixed generally: registries refresh from the file (byte offset) on get-miss/all/next_id; read_json retries the Windows replace race; report template nav.toc wraps; serve has a 3 s graceful-shutdown timeout.

## 2026-09-19 — Team bug round 2 (10 items), lead + 2 subagents; suite 308 passed + 1 xfail
- View 0 "runs fail half the time": root cause = concurrent connections opening a FRESH decision_log.sqlite (API /status + /events vs the job thread) -> "database is locked" before the first stage; error was memory-only. Fix: DecisionLog busy_timeout + retried init + BEGIN IMMEDIATE around (last hash -> insert) so several writers keep one linear hash chain; workspace atomic replace retries PermissionError; RunStatus.error persisted and shown in the Runs view. Stress: 10/10 starts under concurrent polling; 12/12 chains verify with 4 writers.
- View 1: kind-of-data block read wrong keys (likelihood/statement vs domain_likelihood/explanation); header label clarified + "name this signal" decision (SignalDescriptor.display_name/display_unit, profile.apply_override set_name, logged with a human_decision evidence); two inference types had no evidence ids (detect group-count statement, operator overrides) -> both cite evidence now.
- Evidence (subagent): tpm/api/evidence_plain.py explain()/explain_object()/resolve_refs(); /evidence returns `plain` and resolves DIAG/FLAG/CHK/INF/RULE/PATTERN/EGR/batch ids; UI shows plain sentence first, technical detail below; GET /api/runs/{id}/object/{object_id}.
- Report + assessor (subagent): tpm/report/prose.py (model summary parsed to prose, never JSON), ensure_report() template-first + background model summary, per-language cache/lock, row caps, narrow flags table; assessor Yes/No wording.
- My mistake caught by a subagent: a runs.js edit put real newlines inside quoted strings -> whole UI blank; plain `node --check x.js` does not parse ES modules strictly. tests/test_e_ui_assets.py now checks a .mjs copy of every frontend file.
- Open: tpm/assessor/actions.py still emits "Yes:/No:" rationales (consumers strip them); assessor reasons stay English under localised headlines; print-to-PDF of the report not verified.

## 2026-09-19 — Round 3: point anomalies, suspicious-rows list, spike check, narrative gates (lead) + 2 subagents (UI; PDF/PPTX)
Lead slice (committed): tpm/quality/checks.py local_spike (rolling median w=7; leave-one-out rolling-mean local noise; must return to the neighbourhood, be isolated in time, not sit at a group boundary; spike_sigma 10; skipped in fast mode). Found + fixed a chunk-edge bug on the way ("nearest" padding made the local noise collapse to 0 near batch ends).
tpm/detect/events.py: point candidates = raw runs <= 2 rows (peak >= 1.5x) + short segments + segments that START at a quality single-reading row; echo test = repair those readings with the neighbour median, re-score with the fold model, drop the segment if no sustained segment remains (a level-deviation rule was tried first and rejected: naturally wandering signals exceed 4 spreads by chance). Flag kind "point"; regime (share of point stretches) in detect_meta.events.points + inference.
tpm/detect/suspicious.py -> suspicious_rows.json (contract given to both subagents) + GET /api/runs/{id}/suspicious. tpm/diagnose: build_point_diagnosis (one aggregate; leads only when point-dominated). cascade.chain_for_flag drops steps without a learned relation or with a contradicting lead/lag; patterns need lead_share >= 0.5 or top-3 overlap >= 0.5.
Validated end to end: points-only synthetic (14 injected readings) -> 9 point flags, 0 sustained events, 14/14 rows listed, 1 diagnosis; sustained-fault synthetic unchanged (4 of 7 groups, same as before). tests/test_c_points.py (5 tests).
