# Round 4 (2026-09-19 afternoon) - lead log

User request: (1) hybrid "limited calls to the Anthropic API" (Sonnet 5 / Opus 5, never Fable; only aggregated,
coarsened or anonymised data may leave) and see whether it speeds things up; then (2) summary-first UI with
"Show technical analyses", (3) any local chat/embedding model + download Ollama and the two standard models from
the app, (4) a Windows installable app, installed on the user's laptop.

Who does what
- Hybrid feature: a workflow of agents (core -> chat / stages+benchmark / API+UI -> three reviewers -> fixer).
  Contract: `docs/HYBRID_SPEC.md`. Their log: `docs/worklog/hybrid.md`.
- Summary-first UI: one subagent. Its log: `docs/worklog/ui_round4.md`.
- Lead (this log): model management (3) and the Windows app (4).
- A parallel session did the e-mail work and committed it itself (f048599).

## Measured before the hybrid work (workspace/te_full_v2, 6 GB, this laptop)
1355 s = ingest 266 + profile 105 + quality 171 + detect 556 + diagnose 132 + assess 110 + report 14.
Local-model time inside: 149 s in 5 calls (25-37 s each). Chat: several sequential local calls per answer.
So an external model cannot touch ~89 % of the run time (CPU/disk work on raw data that must stay local).

## (3) Local models: no fixed model any more
- `tpm/llm/models.py` (new): Ollama state (installed / running / version), machine facts (RAM, GPU via
  nvidia-smi, disk), installed models with kind (chat / embedding; from /api/show capabilities, name hints as
  fallback), memory fit (fast / slow / too_big), ranking, `choose()` = user choice -> configured default ->
  fallback list -> best installed model (`local_llm.auto_select`), `select()` persists the choice with
  `model_selected_by: user` so it beats `TPM_LOCAL_MODEL` from .env after a restart, pull jobs with progress
  (`PULLS`, streaming /api/pull, cancel, plain error for unknown names), Ollama install on Windows (download the
  official installer, Authenticode signature must be valid AND signed by Ollama, then the installer is opened for
  the user; other systems get the download page), `start_ollama()`, `setup_prerequisites()`.
- `tpm/llm/providers.py`: `pick_model()` falls through to the automatic choice; new `embedding_model()`;
  `missing_models()` only reports what is really needed; `embed()` uses the chosen embedding model.
- `tpm/llm/embeddings.py`: the search index is rebuilt when the embedding model changed; queries are embedded with
  the model that built the index.
- `tpm/api/models_api.py` (new, registered from server.py): GET /api/models, POST /api/models/select,
  POST/GET/DELETE /api/models/pull[/{id}], POST /api/models/setup, POST/GET /api/ollama/install,
  POST /api/ollama/start. All plain `def` routes.
- UI: `tpm/api/static/js/models.js` (new): modal opened from the "Local model" lamp in the top bar. Plain status
  and next step first, choosers, "Get another model" (free text + suggestions with size and fit, progress bar,
  cancel), technical table under "Show technical analyses". en/fi/sv texts live in the module.
- CLI: `tpm models` prints the models in use and why; `--pull NAME`, `--use NAME|auto [--embedding]`.
  `doctor` accepts any installed chat model.
- config: `local_llm.model_selected_by`, `embedding_selected_by`, `auto_select`; `TPM_SETTINGS` env var points
  at a settings file outside the program folder.
- Tests: `tests/test_d_models.py` (13, fake Ollama, no network).
- Verified live on this laptop: overview, select gemma3:4b and back to auto (on a temp settings copy), pull of an
  installed model (manifest check), unknown model name -> plain error, panel rendering in the browser.
- Not verified live: the Ollama installer download (Ollama is installed here); covered by unit tests of the
  signature check and by a headers-only check that the URL resolves.

## (4) Windows app
- `tpm/desktop.py` (new): entry point of the installed app. Data folder `%LOCALAPPDATA%\NorrinTPM`
  (workspace, settings.yaml copy, .env, logs), environment prepared before tpm.config is imported, server thread
  on port 8000 or a free port, tkinter control window, UI in an Edge app window, single instance through
  `instance.json`, `--no-window` for headless use, output redirected to `logs/app.log` (a windowed exe has no
  stdout), `JOBLIB_MULTIPROCESSING=0` (a frozen app must never spawn copies of itself).
- `packaging/windows/`: `norrin_tpm.spec` (every tpm module as hidden import because stages are resolved by name;
  all non-Python package files; plotly excluded because plotly.min.js ships as a file), `installer.py` (wizard +
  `/S` silent mode, per-user, HKCU uninstall entry, shortcuts through WScript.Shell, PowerShell uninstaller that
  continues from a temp copy and keeps the analyses by default), `build.ps1`, `pack_payload.py`, `make_icon.py`.
- Trial build results: app folder 392 MB, setup 189 MB; frozen CLI ran the full pipeline on a bundled sample in
  28 s; frozen windowed app served UI, PDF and PPTX; silent install 12 s; registry entry, Start-menu and desktop
  shortcuts present; silent uninstall removed everything except the data folder.
- Tests: `tests/test_f_desktop.py` (9). Docs: `docs/WINDOWS_APP.md`.

## How to continue
- Rebuild after any code change: `powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1`.
- The setup file is too big for git (GitHub limit 100 MB): attach `dist\NorrinTPM-Setup.exe` to a release.
- `save_settings_overrides` rewrites settings.yaml without comments (old behaviour); the installed app works on
  its own copy, the repo file is only touched when someone changes settings from a source checkout.

## Renaming signals (user request during round 4: "rename S44 to 'possibly broken'")
A rename existed only inside the Understanding detail pane and did not reach the report or most pages.
- `tpm/naming.py` (new): `operator_names(ws)`, `expand_text`, `expand` (prose only: strings with a space; ids,
  anchors, keys stay), `clean_name` (one line, 80 chars). Used by `tpm/report/report.py::collect` so HTML, PDF and
  the deck say "possibly broken (S44)"; the payload for the model keeps the bare alias.
- `tpm/api/names_api.py` (new): GET /api/runs/{id}/signal-names -> {names, headers, units}. Saving is the existing
  logged decision `set_name` on the signal (POST /decisions), so it is in the decision log with actor and note.
- UI: `js/rename.js` (new): `renameBar` at the top of the Understanding page (above "Show technical analyses"),
  `renameSignalDialog`, `loadSignalNames` (called on run selection). `core.refLink` labels every signal link
  "name (S44)"; `linkifyRefs` keeps the bare id when the text already has a name in front of "(S44)".
- Tests: `tests/test_e_rename.py` (3). Checked live on a copy of demo_cli: rename through the bar, summary card,
  report prose.
