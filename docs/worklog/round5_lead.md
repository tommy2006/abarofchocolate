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
