# Worklog: agent_e_api_ui

## 2026-09-18T20:40Z — kickoff, plan
Done:
- Read DECISIONS (#24, #37, #43, #44, #46), ARCHITECTURE, frontend-design skill, contracts, config, workspace, pipeline, llm entry, synth fixture.
- All stage packages are still stubs, so every stage call in the API is lazy (`_lazy("tpm.quality:compile_rule")`) and returns HTTP 501 `{"unavailable": ...}` when missing.
Plan (in order):
1. `tests/fixtures/fake_workspace.py` — writes a complete fake run (every artifact in ARCHITECTURE §3) from `tests.fixtures.synth`, using the contracts.
2. `tpm/api/server.py` — FastAPI `app` + `create_app(settings_path=None, workspace_dir=None)`.
3. `tpm/api/static/{index.html,app.js,styles.css,i18n/*.json}` — vanilla ES2020, no build step, plotly served from the Python package.
4. `tests/test_e_api.py`.
Pending: everything above.
How to continue: see the sections below as they land.
Decisions / deviations:
- Column conventions assumed for row-shaped artifacts (server is defensive, but agents A/C please align):
  - `dataset.parquet`: signals stored under their alias (`S01..`) *or* original name (server resolves both through `schema.signal_alias`); optional `__row__` (0-based row index) and `__group__`.
  - `scores.parquet`: `__row__`, `__group__`, `batch_id`, `score` (ensemble), `threshold`, contributions as `c_<alias>`, per-detector scores as `d_<name>`. The server also accepts `contrib_<alias>` / `ensemble` / `ensemble_score`.

## 2026-09-18T20:20Z — API + UI + tests landed, integrated with the other agents' real modules
Done:
- `tests/fixtures/fake_workspace.py`: complete fake run (every artifact) from `tests.fixtures.synth`; CLI `python -m tests.fixtures.fake_workspace [run_id] [--workspace DIR]`; also used by `POST /api/demo`.
- `tpm/api/server.py`: `app` + `create_app(settings_path=None, workspace_dir=None)`; all routes of the brief (see the docstring / `/api/docs`). Lazy imports; 501 `{"unavailable": "module.function"}` when a stage function is missing; artifact GETs never 500 (`available: false`).
- `tpm/api/fallback.py`: template chat answers (flag / diagnosis / signal / run), template assessor answers, generic decision effects on artifacts when an owner module has no `apply_override`, replay + watch + align fallbacks, ledger summary + data-flow statement.
- `tpm/api/static/`: `index.html`, `styles.css`, `app.js` + `js/{core,charts,chat}.js` + `js/views/{runs,understanding,quality,monitor,diagnoses,assessor,log,dataflow,report}.js`, `i18n/{en,fi,sv}.json` (key parity checked), `vendor/plotly.min.js` written from the plotly package at startup.
- `tests/test_e_api.py`: 36 tests (all GETs + keys, decisions/log/verify/export, rules 501-or-draft, chat, assessor, stream push/replay, report, SSE, profile toggle, run create/delete, graceful empty run).
- Integration with modules that landed in parallel: `tpm.llm.agent.chat(ws, settings, message, context, history, actor, task=, language=)` (persists its own turns with `content`/`citations`; the API normalises), `tpm.llm.ledger.summary/data_flow_statement`, `tpm.llm.available()` (picked local model shown in the lamp), `tpm.quality.compile_rule(..., author=)` (persists itself), `run_active_rules` + `define_batches`, `add_rules_from_file`, `tpm.ingest.stream.replay/watch_folder/align_incoming` (callback `(df, batch_id, meta)`, `stop_event`), `tpm.report.run_report` / `email_report(ws, settings, to, lang)`, `tpm.log.exports.export_run` (`GET /api/runs/{id}/export`).
Pending:
- Screenshots of each view (browser pane screenshots time out; text/console verified). Mobile-width check.
- `tpm.assessor.ask` / `assess_new_file` do not exist yet: the API answers assessor questions through `tpm.llm.agent.chat(task="assessor_chat")`, then the template over `assessor.json`; upload returns 501.
How to continue:
- Run: `.venv\Scripts\python.exe -m uvicorn tpm.api.server:app --port 8765` (or `python -m tpm serve`), open http://localhost:8765, click "Create a demo run from synthetic data".
- Test: `.venv\Scripts\python.exe -m pytest tests/test_e_* -q` (about 3 min because the real pipeline runs for the create/delete test).
- JS syntax: `node --check tpm/api/static/app.js` (and each file under `static/js`).
Decisions / deviations:
- `batches.json` is a plain list in `tpm.ingest.stream.build_batches` format (row_end exclusive); the fixture writes that format. `GET /batches` wraps it as `{"batches": [...], "row_end_exclusive": true}`.
- `scores.parquet`: the API reads `ensemble` (threshold 1.0 by construction), `contrib_<alias>`, `score_<detector>`; it also accepts `score`/`threshold`/`c_`/`d_` columns (fixture).
- `PUT /api/settings {profile}` writes `settings.yaml` and, when `.env` pins `TPM_PROFILE`, overrides the env var for the running process so the UI toggle wins.
- When a stage owns no `apply_override`, the API applies a generic effect (human_status on flags/diagnoses, pattern name, signal role override, rule status, assessor recommendation status) so the UI reflects decisions; owners that exist (profile, ingest, quality, detect) take precedence.
- Chat turns via `tpm.llm.agent.chat` are not double-persisted; template-answered turns are written by the API in `{role, message, source, evidence_ids}` shape. `GET /chat` normalises both.

## 2026-09-18T21:05Z — verified in the browser, fixes, final state
Done:
- Browser verification (Claude browser pane + headless patchright): all 9 views render with zero console errors; flag click -> chat drawer with context -> answer from the local model (llama3:8b via `tpm.llm.agent.chat`, source labelled, evidence chips); profile toggle no-egress -> hybrid -> no-egress round-trips (lamps + statement follow); phone width 375/390 px has no horizontal overflow (rail becomes a horizontal strip, banner stacks).
- Fixed: `/scores` and `/series` now use a per-request DuckDB cursor (`ws.duckdb().cursor()`); the shared connection failed under concurrent requests, which emptied the monitor charts after a flag click. 16 concurrent requests verified.
- Fixed: diagnosis list cards no longer stretch to the detail height; table cells do not wrap (long text uses `.wrap`); severity bars nowrap; assessor DQ bar chart shows all category labels; assessor charts section renamed.
- Fixed: `PUT /api/settings {profile}` rewrites the `profile:` line of settings.yaml in place (keeps the file's comments); `save_settings_overrides` (foundation) re-serialises the YAML and drops comments, so it is only used for other keys. `config/settings.yaml` restored from git after the test toggle.
- Full suite: `tests/test_e_api.py` 36 passed (about 4 min: the real pipeline runs on the tiny CSVs of the create/delete test, and `tpm.llm.available()` probes Ollama).
Pending / known gaps:
- `tpm.assessor.ask` / `assess_new_file` still missing (agent B): questions go through `tpm.llm.agent.chat(task="assessor_chat")`, candidate-file upload answers 501.
- `report_fi.html` / `report_sv.html` are produced on demand by `tpm.report.run_report`; the fixture only writes `report_en.html`.
- Chat answers from the local model can take 10-60 s on this laptop (30B model pulled); the drawer shows "Working on it" meanwhile. No streaming of tokens.
- The demo run (`POST /api/demo`) writes synthetic artifacts; a real file goes through the full pipeline (verified on a 50-row CSV in the tests).
How to continue:
- Start: `.venv\Scripts\python.exe -m uvicorn tpm.api.server:app --port 8765` or `python -m tpm serve`; open http://localhost:8765; pick a name + role; "Create a demo run from synthetic data" or drop a CSV.
- Tests: `.venv\Scripts\python.exe -m pytest tests/test_e_* -q`; JS: `node --check tpm/api/static/app.js` (and `static/js/**/*.js`).
- Files: `tpm/api/server.py` (routes), `tpm/api/fallback.py` (templates, generic effects, stream fallbacks), `tpm/api/static/app.js` (boot/router/lamps/SSE), `static/js/core.js` (i18n, api, widgets, decisions, tables), `static/js/chat.js` (drawer), `static/js/views/*.js` (one file per view), `static/i18n/*.json`, `tests/fixtures/fake_workspace.py`, `tests/test_e_api.py`.
