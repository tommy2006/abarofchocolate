# Worklog: agent_f_platform

## 2026-09-18T22:40+03:00 — Kick-off, plan
Done:
- Read DECISIONS, ARCHITECTURE, contracts, config, workspace, pipeline, decision_log, llm/__init__, settings.yaml,
  synth fixture, requirements, .env.example, .gitignore, challenge PDF (8 expected outputs + bonus list).
- Environment: Python 3.12.10 in .venv, all deps import; git repo initialised on `main`, no commits yet;
  Ollama 0.34.1 reachable at localhost:11434 (has qwen3-coder, deepseek-r1:14b; NOT the configured gemma4:e4b-it-qat).
- Only foundation files exist; A/B/C/D/E packages are empty stubs at this moment.
Plan (files I own):
- `tests/fixtures/fake_workspace_f.py` — builds a complete fake workspace (every artifact of ARCHITECTURE §3) from synth data.
- `tpm/report/{__init__,report,i18n,charts,email}.py`, `tpm/report/templates/report.html.j2`, `tpm/report/i18n/{en,fi,sv}.json`.
- `tpm/log/exports.py` — export_run(ws, out_dir) -> zip.
- `tpm/cli.py`, `tpm/__main__.py` — run/serve/replay/report/email/export/verify-log/models/bakeoff/demo/doctor.
- `scripts/make_samples.py`, `samples/{demo_process.csv,demo_headerless.dat,demo_records.csv}`.
- `run.ps1`, `run.bat`, `run.sh`.
- `README.md`, `docs/{JUDGES_GUIDE,DATAFLOW,ADAPTABILITY,EVALUATION}.md`.
- `tests/test_f_cli.py`, `tests/test_f_report.py`.
Pending: everything above.
How to continue: `.venv\Scripts\python.exe -m pytest tests/test_f_* -q`.
Decisions / deviations:
- Report is template-first: every narrative sentence is composed from i18n dictionaries; the LLM "model-written summary"
  is optional and labelled. `generate_report(..., use_llm=False)` is used in tests so no model call can slow them down.
- Unknown-shape artifacts from other agents (relations.json, batches.json, baseline.json, detect_meta.json,
  evaluation.json, assessor.json, scores.parquet) are rendered defensively (key/value or count) and never break the report.

## 2026-09-18T23:05+03:00 — Report, export, CLI, samples, launchers working; 20 tests green
Done:
- `tests/fixtures/fake_workspace_f.py`: complete fake run (schema, signals, relations, checks, trust, rules, scores, flags,
  patterns, baseline, detect_meta, evaluation, diagnoses+critique, assessor, egress ledger, 5 human decisions via
  `pipeline.apply_decision`, hash-chained log).
- `tpm/report/`: `report.py` (collect + generate_report + run_report), `i18n.py` + `i18n/{en,fi,sv}.json` (same key set,
  placeholder-checked by test), `charts.py` (inline SVG sparkline/stacked bars/hbars/line/dataflow diagram),
  `templates/report.html.j2` (8 sections with `id="section-N"`, evaluation, assessor, appendix, print CSS, no JS),
  `email.py` (SMTP via env names from settings.report.smtp; accepts both `(ws, settings, to, lang)` and the API's `(ws, to, lang)`).
- `tpm/log/exports.py`: export_run -> folder + zip (reports, decision_log.jsonl, egress_ledger.jsonl, verify.json,
  manifest.json, derived JSON/JSONL artifacts; never dataset/scores/sqlite).
- `tpm/cli.py` + `tpm/__main__.py`: run (live progress from progress_cb + status.json monitor thread, summary, exit code),
  serve, replay (uses tpm.ingest.stream.replay with process_batch callback), report, email, export, verify-log, models,
  bakeoff, demo, doctor, list. Verified against agent A's real ingest and agent E's real API server.
- `scripts/make_samples.py` + `samples/` (demo_process.csv 1.0 MB, demo_process_labeled.csv, demo_headerless.dat, demo_records.csv, README.md).
- `run.ps1`, `run.bat`, `run.sh`.
- Tests: `tests/test_f_report.py`, `tests/test_f_cli.py` -> 20 passed (26 s).
Pending:
- README.md, docs/JUDGES_GUIDE.md, docs/DATAFLOW.md, docs/ADAPTABILITY.md, docs/EVALUATION.md.
- Local git commit.
How to continue:
- `.venv\Scripts\python.exe -m pytest tests/test_f_* -q`; `python -m tpm demo`; `python -m tpm report <run_id> --lang all`.
Decisions / deviations:
- `email_report` signature made tolerant because tpm/api/server.py calls it as `fn(ws, to, lang)`.
- CLI `--rules FILE` writes rules.json (status approved) into the run dir before the pipeline and sets
  options.rules / options.rules_file (agent B reads `options.rules_file`).
- `replay --speed` follows agent A's semantics (data-seconds per wall-clock second, 0 = as fast as possible).

## 2026-09-18T23:35+03:00 — Docs written, end-to-end verified with real stages, local commit
Done:
- README.md (60-second start, views, CLI reference, profiles, local model setup, report/export/email, troubleshooting,
  structure), docs/JUDGES_GUIDE.md (5-minute walkthrough mapped to the 8 outputs + bonus table), docs/DATAFLOW.md
  (ASCII diagram, profile routing table, guard rules, ledger fields, swap table, evidence), docs/ADAPTABILITY.md
  (generic vs adapter table, drifting sensor vs corrupted record batch through the same pipeline, log/free-text
  walkthrough), docs/EVALUATION.md (what is measured, synthetic truth, labels evaluation-only, GroupKFold OOF scheme).
- End-to-end: `python -m tpm demo --no-llm` with the real ingest/profile/quality/detect stages (A/B/C) -> 57 s on
  samples/demo_process.csv; diagnose/assess still "not implemented yet" (skipped) at this time; report renders 12 signals,
  8 flags, 20 group timelines, PATTERN-A, ledger, chain OK, in EN/FI/SV with zero "not available" fallbacks.
- Report fixes after seeing agent C's real scores.parquet: score column preference (`ensemble` before `score_*`),
  threshold fallback (detect_meta.scoring/final_model/events -> flags' threshold -> threshold column).
- `run.ps1 -Doctor` exercised (installs deps, creates .env, checks Ollama, runs doctor); `bash -n run.sh` clean.
- Tests: `.venv\Scripts\python.exe -m pytest tests/test_f_* -q` -> 20 passed.
Pending / known gaps:
- Visual check of the HTML report in a real browser was only partial (preview pane unusable here); structure and SVG
  counts are test-verified. Print-to-PDF layout not measured.
- `replay` and `serve` are wired to agents A/E code paths but `replay` was only exercised until agent A's signature
  appeared (callback now matches `callback(df, batch_id, meta)`); re-run `python -m tpm replay <run_id> --max-batches 3`
  after a full run to confirm.
- The report's model-written summary needs agent D's router + a pulled model; not exercised with a live model here
  (`use_llm=False` in tests, `--no-llm` in the demo run).
- E-mail send path tested only with a fake SMTP object.
How to continue:
- After agents C (diagnose) and B (assess) land: `python -m tpm demo` then open report sections 4 and "Data assessor";
  adjust `_scalars`/tables in tpm/report/report.py if their JSON shapes need nicer rendering.
- Lead: `git push origin main` (agent F committed only its own files).

## 2026-09-19T10:45+03:00 — Team bug round: assessor "Yes"es, model summary as prose, flags table width, language switch
Done:
- Bug 1 (assessor): the verdict word was printed twice ("Yes" headline + "Removing bad data helps: Yes: 5 rows ...";
  chat "Yes. Yes: ..."). Source fixed in `tpm/assessor/__init__.py` (`_strip_lead`, used for recommendation text, the
  less-data reason and the chat lead; the "Uncertain:" reason reworded). `actions.py` still writes "Yes:/No:" rationales
  (not mine) so `assessor.js` strips defensively too (`reason()`, `singleLead()`; old assessor.json files read correctly)
  and drops "More data: yes. Less data: yes." from the summary line under the cards. `test_b_assessor` still green
  (chat answers still start with one "Yes."/"No.").
- Bug 2 (report model summary): root cause `max_tokens=700` cut the JSON mid-sentence -> unparseable -> a repair round
  (2x latency) -> the raw truncated JSON text was printed in a `white-space: pre-wrap` box. New `tpm/report/prose.py`:
  `parse_narrative` (data dict, or json.loads of text, fenced JSON, router `{"result":..}` wrapper, plain prose; a cut
  reply is salvaged to whole sentences only; unreadable -> section omitted, JSON is never printed), `clean_text`
  (port of the UI's cleanText: "[llm-...] {json}" fragments in diagnosis summaries / critique objections),
  `whole_sentences`, `strip_lead`, `detector_label`. Template renders lead paragraph, headings + paragraphs,
  "What remains uncertain", references (links to the row/card when rendered; ids unknown to the run are dropped),
  source line. `max_tokens` 1600 + brevity/language instructions; payload under guard-whitelisted keys
  (`language`, `instructions`, `report_sections`, `flags`, `diagnoses`), totals stated, numbers rounded.
  Verified live (gemma4:e4b-it-qat) in EN/FI/SV on demo_cli and te_2m: prose, headings in the language, no braces.
- Bug 3 (flags table width): 13 columns -> 7 in a `table-layout: fixed` table with a colgroup; detector shown as
  "ensemble of 8 detectors (summary)" with the full string in `title` + a wrapped legend under the table. All tables:
  `td, th { overflow-wrap: anywhere }` (cells can shrink below their longest token), ids/badges/numbers nowrap,
  `.grid2`/`.kv` use `minmax(0, ..)`. Print block kept (+ `thead` repeats, `tr` avoids breaks). Measured in the browser
  at 740 / 1009 px on demo_cli and te_2m: 0 overflowing tables, no horizontal scroll.
- Bug 4 (language switch): root causes (a) the GET blocked on the model (49.6 s measured on the SMALL run, no feedback,
  blank preview), (b) the view fires several GETs for one language (status fetch + iframe + Open/Download) and
  concurrent generations shared one `.tmp` file -> FileNotFoundError/PermissionError -> HTTP 500 (reproduced with 4
  threads), (c) a report, once written, was served forever. Now: `ensure_report()` (API) writes the template report
  first (0.3 s small, ~3 s te_2m) and never waits for the model; the summary comes from a worker thread (one per run +
  language), is stored in `report_llm_<lang>.json` and the report is rendered again when it arrives; failures are
  remembered for 10 min; per-run lock + unique temp names + replace retries; cache stamp in
  `<meta name="tpm-report">` (artifact sizes/mtimes + human-decision count + code/template stamp) -> regenerated when
  artifacts, decisions or the report code change; blocking callers (pipeline stage, CLI) wait at most 75 s.
  Large runs: top 300 flags / 100 diagnoses by severity (30 full cards + 70 compact rows), 100 untrusted batches, 300
  appendix rows, with notes giving the totals: te_2m 2.4 MB -> 1.4 MB. Handler: `?status=1` (JSON for progress),
  `?embed=1` (preview), `?refresh=1` (Regenerate: also asks the model again), `Cache-Control: no-store`; a pending
  report opened in its own tab gets a meta refresh (never the file on disk, the download or the preview).
  `report.js`: "Generating..." -> ready / summary pending -> polls -> reloads the preview; stale answers of an older
  selection are ignored; Regenerate button; retry on failure. `TPM_REPORT_LLM=0` disables the model summary.
- 5: `tpm.api.evidence_plain.explain` is used when importable (any exception -> technical statement only): plain
  sentence first, technical statement smaller underneath (signals, flags, diagnoses).
- Also: assessor section of the report printed the action dict (`{'type': 'drop_duplicates', ...}`) -> action label +
  reason; verdicts as one statement each; thousands separators follow the language.
- Tests: `tests/test_f_report_fixes.py` (25). `pytest tests/test_f_* tests/test_e_ui_assets.py
  tests/test_e_api.py::test_report_and_email` -> 65 passed; `test_b_assessor` + `test_e_api` -> 57 passed.
Pending / known gaps:
- Print: the `@media print` rules were inspected as parsed by the browser; no real print-to-PDF was produced.
- The model's content can still be loose (seen once: "Eight diagnoses were produced" = the 8 it was shown, of 2,280;
  the payload now states the totals and says the lists are a sample). The box is labelled "reading aid, not evidence".
- Assessor reasons stay English under a localized headline ("Ja" + English sentence): `tpm/assessor` writes English only.
- `tpm/assessor/actions.py` still starts rationales with "Yes:/No:/Unclear:" (agent B); harmless now, but the clean fix
  is there.
How to continue:
- `.venv\Scripts\python.exe -m pytest tests/test_f_* -q`. JS: `node --check` does NOT parse these files as modules;
  use `node --input-type=module --check - < file.js`.
- A dev server without `--reload` keeps old Python but serves the new template: restart it after editing tpm/report.
