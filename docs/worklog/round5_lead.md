# Round 5 (2026-09-19 evening) - lead log

User requests: quality page "what was checked" + % meaning; chat controls; faulty data -> why -> fix path; suggestions
for every problem; sensor hypotheses + interactions; rail 0-7 + Settings; modes Basic / Operator / Engineer with colour
schemes; diagrams over lists; live alarm -> problem -> cause -> suggestion; text zoom; drift towards known failure
types (message.txt) in the live view; verify the Windows install; push the installer; README; logo from /graphics.

Split: subagent A = advice library, quality / diagnoses / monitor / understanding pages, diagrams (log: round5_a.md);
subagent B = chat controls, live failure signatures + alarm path (log: round5_b.md); lead = the rest (this log).
A duplicate copy of the lead session (after a restart) applied part of the lead slice between 18:51 and 18:53 and
then stood down; the lead continued from its edits.

## Done by the lead (with the duplicate's part)
- Rail: 0 Runs, 1 Understanding, 2 Data quality, 3 Monitor, 4 Diagnoses, 5 Assessor, 6 Report, 7 Live monitor,
  then a gear "Settings" (js/views/settings.js: Person & mode, Display [colours, text size, language], AI models,
  Data flow & privacy = former view 7, Decision log = former view 6, engineer only). Old links #/dataflow and #/log
  open the matching Settings tab (app.js route alias). Page numbers in report.js / live.js follow.
- Modes: core.js ROLE_LEVEL basic 0 / operator 1 / engineer 2 (reviewer = alias of engineer, stored users migrated);
  html[data-role] drives a colour scheme per mode in styles.css (basic blue, operator teal, engineer violet; light
  and dark); the rail shows the mode; brief.js hides the whole technical part in Basic mode (techDetails returns a
  hidden stub + "Show more (Operator mode)" note; techNested hidden too). server.py accepts role "basic".
- Text size: A- / A+ buttons in the top bar and in Settings > Display; settings.js applyZoom sets body zoom
  (0.85 .. 1.6), stored, Plotly charts get a resize event. Layout re-flows because zoom scales the CSS pixels.
- Logo: graphics/norrin-favicon-512.png -> tpm/api/static/logo.png (favicon + header), packaging/windows/make_icon.py
  builds the .ico from it (installer, exe, control window).
- README: new intro "What it does" + "Install on Windows"; the installer is tracked with git LFS
  (.gitattributes, .gitignore exception) so it can live in the team repo.
- i18n: role.basic, role.pick.*, nav.settings, settings.*, zoom.*, brief.basic* in en/fi/sv.
- Checked in the browser on port 8083 (basic / operator / engineer colours, Settings tabs with the embedded Data flow
  page, zoom 110 %, Basic mode without technical parts). Tests: test_e_ui_assets + test_e_brief 69 passed.

## Open at the time of writing
- Subagents A and B still running; then: full test suite, rebuild the installer with the new logo, install, verify,
  commit (LFS push of dist/NorrinTPM-Setup.exe), update the demo server on port 8000.

## 2026-09-19 — Finishing pass (after the credit outage)
Applied the confirmed findings of the review (critical, major, minor, QA open issues, i18n requests). Checked on my own
server (port 8096, TPM_NO_DOTENV=1, a scratch copy of demo_cli / swat_points / extra_modes_rowlabels / te_2m, local model
off unless stated) with the headless browser; port 8000 was never used (another session restarted it at 23:00). Scripts
and screenshots: session scratchpad `fp/`.

Fixed:
- 22:10 Live alarm said "No known failure type matches" while Fault 1 built up: `signatures.best_cause` skips a generic
  row that another active signature covers; `monitor._alarm` turns an imminent cause into an ALARM "drifting towards
  <failure type>" (trip key `sig.towards`, kind occurring) when its covered generic pattern is occurring OR the drift
  alarm has already tripped, so the card never steps back from "Alarm" to "Early warning". live.js card + toast, 2 keys.
  TE demo in the browser: drift -> drifting towards Fault 1 -> Fault 1 occurring. Regression test.
- 22:25 Text size broke Plotly hover / click (125 %: another sensor opened, 160 %: nothing) and shrank the charts: every
  chart is drawn outside the CSS zoom (`.tpm-plot { zoom: calc(1 / var(--zoom)) }`) and `charts.plot()` scales fonts,
  marker sizes, margins and height by the text size; charts are redrawn on `zoom.changed` (settings.applyZoom). Every
  100vh / 90vh / 70vh is divided by --zoom (frame, rail, chat drawer, narrow drawer, report frame, modal), `.modal-back`
  scrolls; the two styles-live.css media queries became container queries on the card / list. Browser, 1366x768, at
  100 / 125 / 160 %: 11/11 sensor bars hover right and the click opens the right sensor, trust-grid hover 9/9, the chat
  box is on screen and clickable, the Basic batch popup fits. Static guard test.
- 22:35 Data quality: the % is explained as a trust grade, not "information lost" (dq.score.means / full / how,
  dq.trustHelp with the threshold, en/fi/sv), and each batch says what lowered it ("1 of 12 sensors unusable (frozen);
  rows out of order in the whole batch"). "The faulty data, worst first" sorts by the batch trust score (B00037 first:
  the batch the summary names and the lowest bar), pieces of a batch together, batch info once per batch. Every piece
  shows the four questions it was asked (✓, or ✕ / ! with the kinds found; hover names every check behind the question);
  "What was checked" lists every check of every question (✓ = found nothing). A test keeps the UI list equal to
  quality/checks.py CATEGORY_OF.
- 22:45 Basic mode by far the least: `praStrip({ compact })` = the problem, ONE sentence of reason (the cause sentence
  `because` for findings, new in advice / item briefs), two fix steps, no id / meta rows, no chat buttons naming ids;
  ONE strip on Monitor, Diagnoses, Data quality, Assessor and Understanding + "N more are listed in Operator mode"; no
  top-bar lamps. Visible words Basic / Operator (demo_cli, te_2m): Understanding 31 / 40 %, Data quality 20 / 21 %,
  Monitor 36 / 34 %, Diagnoses 31 / 29 %, Assessor about 50 %; Report stays about 100 words in both modes.
- 22:50 Chat: stop flags keyed by (run, chat) (Clear in run B no longer stops run A's "default" chat); Clear / Delete /
  Stop refuse an invalid chat id (400) instead of clearing "default"; `Workspace.filter_jsonl` holds the lock over read,
  filter and rewrite (both clear_chat); a turn running while its chat is cleared is not saved back
  (`agent.discard_running`); an answer's follow-ups are saved with it; "+ New chat" starts about the item selected on the
  page. Stop now ends the local model call itself: inside a chat turn Ollama is asked in streaming mode and the
  connection is closed when Stop is pressed (a watcher covers the prompt phase; one retry like the non-streaming call).
  Real gemma4: stopped 1.0-1.5 s after Stop, the next question answered 4.2 s later. Tests for all of it.
- 22:55 Assessor: a suggestion is never applied twice (route answers already_applied and logs nothing, apply_override
  checks too; every Apply button of the suggestion is switched off and the list line repaints); problem and reason are
  built from the suggestion's numbers in en/fi/sv (duplicates, more data / thin modes, drop sensor / group / rows), the
  assessor's English sentence stays in the list below; Basic one strip. Test.
- 23:00 Confidence words use the server's scale (0.85 / 0.65 / 0.45). A drifting sensor fault starts its answer with the
  instrument step ("A slow drift of one sensor usually means fouling, calibration drift or a failing transmitter"), not
  "look for a leak"; a data problem gets no event step (test). Understanding: the summary says the system guessed what
  each sensor measures from its values; the network help explains the lag numbers; sensor cards show guesses, reasons and
  unit operations in plain words (every reason of profile/roles.py has one, test).
- 23:05 Live alarms reach every page: GET /api/live/alarm (small), app.js polls it every 10 s, toasts once per alarm id
  (live.noteAlarm, shared with page 7) and puts an amber / red dot on rail item 7 (browser: both toasts on page 4, dot
  red). The live source name / status line are message keys (engine MSG src.*), translated in fi / sv.
- 23:08 `doctor` reports the live monitor's known failure types ([FAIL] when none are found). Settings changes are logged
  as human:ui(engineer); live.basic.hint / live.empty.role name the modes.
- i18n: +50 keys and 8 reworded values in en / fi / sv; the scratchpad checker: 1,118 keys in use, 0 missing, 0
  unresolved prefixes, 0 placeholder mismatches. The 9 requested live.* keys were already present.

Checked, nothing to change: no sideways scroll at 360 px (top bar inside the window, Basic and Operator); the "PROBLEM"
label does not break with the chat drawer open at 1440 px or at 160 % (A4's container query). Final sweep: 3 modes x
2 runs x 9 pages, no console errors, no raw keys, no "null", no sideways scroll.

Deferred to the lead (not my files):
- packaging/windows/norrin_tpm.spec ships only config/settings.yaml: add
  `datas += [(str(p), "config") for p in (ROOT / "config").glob("*.yaml")]` in place of line 34.
  tests/test_f_desktop.py::test_the_windows_build_ships_every_config_file_the_app_reads fails until then, on purpose;
  build.ps1 should stop when `NorrinTPM-cli.exe doctor` fails (`if ($LASTEXITCODE -ne 0) { throw ... }` after line 33).
- samples/demo_process.csv lost its header in the working tree (an Excel re-save, not mine): restore the HEAD version,
  then rebuild dist/NorrinTPM-Setup.exe (it predates the Live monitor and round 5), reinstall from a normal shell, check
  that page 7 lists >= 20 known failure types and that doctor prints them, commit through LFS.
- Restart the port-8000 demo server (brief.py, advice.py, agent.py, providers.py, monitor.py, server.py changed).
- README / JUDGES_GUIDE fixes: listed in the finishing-pass report.

Full suite (`pytest tests -p no:cacheprovider`, final code): 1 failed, 589 passed, 1 xfailed in 8 min 39 s. The failure
is the new packaging guard (test_f_desktop::test_the_windows_build_ships_every_config_file_the_app_reads), red until the
spec line is added; the xfail is test_c_detect::test_onset_localisation as before. One earlier run stopped at 41 % from
outside (exit 127, no failing test); the run above is the re-run.
