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

## 2026-09-19 — UI bug round (team test feedback), part 1: code landed
Done:
- Root cause of "evidence panels show Nothing here yet.": `tpm.workspace._Registry` loads `evidence.jsonl` once, when the `Workspace` is constructed. `POST /api/runs` constructs and caches the `Workspace` in `state.workspaces` *before* the pipeline (which uses its own `Workspace`) writes any evidence, so every later `GET /evidence?ids=` on that run answers `missing` until the server restarts. `demo_cli` only worked because it was opened after it had finished. Fixed inside `POST /api/demo` (the only server route I own): after `_start_job`, a release thread joins the job and drops the pre-job `Workspace` so the next request re-opens the finished run. **The lead must apply the same two lines in `_start_job` (or `create_run`) for `POST /api/runs`** — see the end of this entry.
- `POST /api/demo` now runs the REAL pipeline on `samples/demo_process.csv` (or `path` from the body) in a background thread, same as `POST /api/runs` with a path; returns 202 `{run_id, state: "pending", source_path, demo: true}` immediately. The fake fixture (`tests/fixtures/fake_workspace.py`) is used by tests only.
- `js/core.js`: `navigate()/hashFor()` moved here (app.js re-exports), in-app back stack (`recordNavigation`, `canGoBack`, `goBack`; a hash equal to the top of the stack is a step back, so browser Back and in-app Back agree), `viewHead()` shows "← Back" when there is somewhere to go; `linkifyRefs(text)` (FLAG/DIAG/EV/CHK/INF/EGR ids, RULE-003, PATTERN-A, B0003/B00003, S07, "group 15"/G00123) → `<a class="ref" data-ref-type data-ref-id>`; one delegated click handler (`installRefHandler`) → `openRef(type, id)`: batch→quality?batch, flag→monitor?flag, diagnosis→diagnoses?diag, pattern→diagnoses?pattern, signal→understanding?signal, group→monitor?group, rule→quality?rule, evidence/check/inference/egress → popover modal (`showRefModal`) with statement, values, signals, batch, nested links; `cleanText()` drops `[llm-...] {json}` / dict fragments (keeps the readable field, dedupes against the template text), strips source tags; `prose()`, `proseList()`, `stripNumbering()`; `evidencePanel(ids)` self-filling panel that names missing ids instead of "Nothing here yet."; `evidenceList` shows statement + values + signals (+ links) for every role; `confWords()`, `sevWords()`, `timesThreshold()`; `conf()/sev()` carry the words in their tooltip and accept `{words: true}`; `closeAllModals()`; `addPlainBox(view, name)` (imports `./plain.js`, silent when missing); `table().reveal(key)`.
- Views: diagnoses (prose paragraphs, numbered full sentences, critique/uncertainty/assumptions as prose lists, propagation chain + sentences, `?diag=` and `?pattern=` filter, flags behind it as links), monitor (`?flag=` selects group + highlights + detail + chat context, `?group=`, plain "x times the level considered normal", words for severity/confidence, evidence panel), quality (`?batch=` selects the batch and lists its checks, `?check=` highlights, `?rule=` scrolls, batch/signal/rule links in the checks table), assessor (rewritten for the real `assessor.json`: verdict cards for more/less data, combined score with fitness/coverage/data-quality + weights, learning curve from `fitness.curve`, coverage by regime with thin regimes named, DQ scores with per-category statements, recommendations with expected effect + evidence + apply-after-approval, question box last; still reads the old fixture shape), log (object ids + payload ids as links), dataflow (profile cards no longer spill), report (same-origin iframe gets a small style so the report's `nav.toc` wraps), chat (answers cleaned + linkified, `setChatContext`).
- CSS: `.cols > * {min-width:0}`, `minmax(0, …)`/`minmax(min(240px,100%), 1fr)` grids, `overflow-wrap:anywhere` on statements/paths/boxes, `.main {overflow-x: clip}` as the safety net, `a.ref` style, `.flash`, assessor cards.
- i18n: +112 keys in en/fi/sv (ref.*, plain.conf.*, plain.sev.*, plain.dir.*, plain.timesThreshold, evidence.missing, diag.why.*, ass.* …), parity kept.
- `tests/test_e_ui_assets.py`: node --check on every JS file, i18n parity + placeholders, plain-box hooks, POST /api/demo (202 + run id + job + evidence resolves after the job).
Pending: browser verification at 1280/1024/390 px; full `tests/test_e_*` run.
Lead must do:
- `tpm/api/server.py` `_start_job.worker`: after the pipeline finishes (or in `create_run` after `_start_job`), drop the cached pre-job workspace: `with state.lock: old = state.workspaces.pop(run_id, None)`; `old.close()`. Without it every run started from the Runs view shows "Evidence … is not in the registry of this run" until the server restarts. (Alternative: `_Registry.get()` re-reads the file on a miss.)
- `understanding.js`: read `params.signal` in `render(main, params)` and call `showSignal(byId[params.signal])` (+ scroll); signal links navigate to `#/understanding?signal=S07`.
- Report HTML (`tpm/report`): `nav.toc a { white-space: nowrap }` with no separators between the anchors makes one unbreakable line; `nav.toc { display:flex; flex-wrap:wrap }` fixes it at the source (the UI injects that style into the preview iframe meanwhile).

## 2026-09-19 — UI bug round, part 2: verified
Done:
- Headless sweep (patchright via the browser-automation skill, `scratchpad/qa.mjs`) on the real run `demo_cli` at 1280 / 1024 / 390 px: `documentElement.scrollWidth <= clientWidth` in every view (monitor, diagnoses, quality, assessor, dataflow, report, log, understanding, runs); profile cards end inside the grid (right edge 1252 / 996 / 374 px); the report preview's "Contents" nav wraps inside the iframe (`fixed: true`, navRight < clientWidth); zero console errors (one aborted `/api/settings` request from the test's own reload).
- Click-through verified: `#/monitor?flag=FLAG-000001` selects group 3, highlights the row, opens the detail (severity "moderate (54 %)", confidence "fairly confident (78 %)", "2.3× — 2.3 times the level considered normal") and fills the evidence panel (3 items with statements + values); batch link → `#/quality?batch=B00008` with the batch selected and its 4 checks listed; "← Back" → the flag detail again; evidence link → popover with the evidence statement/values; signal link → `#/understanding?signal=S06`; `#/diagnoses?pattern=PATTERN-A` filters 4 of 6.
- Diagnoses on the real run: summary as one clean paragraph (the `[llm-local:…] {json}` tail is gone), 8 numbered sentences, the two model objections show their text instead of a dict repr, 13 flag links, 4 evidence items.
- Assessor on load: "Would adding more data help? Yes, expected gain +5.1 pp", "Would removing bad data help? Yes, +0.7 pp", combined score 74 % with fitness 90 % / coverage 35 % / data quality 93 % and weights, learning curve + coverage + DQ charts (3 plots), "Thin regimes: R2 (1 group), R3 (1 group)", 2 recommendations with expected effect ("consistency 0.96 → 1.00 (+3.6 pp); 5 rows removed (0.06 %)"), evidence and the apply button; question box last.
- Topbar: lamps wrapped to a second row at 1280 px and overlapped scrolled content (fixed 54 px sticky bar). Now `min-height` + auto height, `.brand-name` hidden ≤1440 px, and `syncTopbarHeight()` in app.js keeps `--topbar-h` (rail/drawer sticky offsets) equal to the real bar height.
- Tests: `tests/test_e_api.py` 36 passed; `tests/test_e_ui_assets.py` 19 passed (node --check ×14, i18n parity 486 keys, hooks, POST /api/demo → real pipeline done in ~20 s on a 480-row synthetic CSV with the local model unreachable, evidence ids resolve afterwards). Full run of both: 6 min 43 s (the API suite runs the real pipeline and probes Ollama).
Deviations / notes:
- Windows race seen once while polling `/status` during a run: `PermissionError` in `Workspace.read_json` while the pipeline swapped `status.json`. The UI test now tolerates it; the foundation could retry the read once.
- The Claude browser pane is hidden in this session (layout widths read 0) and caches ES modules across hash navigations; layout claims come from the headless run.
How to continue: `.venv\Scripts\python.exe -m uvicorn tpm.api.server:app --port 8765`, open http://127.0.0.1:8765, "Create a demo run from synthetic data" now runs the real pipeline (progress in the Runs view); `node C:\Users\dangv\.claude\skills\browser-automation\browser.mjs http://127.0.0.1:8765/ --script scratchpad\qa.mjs` reproduces the sweep.
