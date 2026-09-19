# Windows app and installer

The monitor can be installed like any Windows program: one setup file, a Start-menu entry, a desktop shortcut and an
entry under **Settings > Apps > Installed apps** with an uninstaller. No Python, no terminal, no administrator
rights. Everything still runs on the computer itself.

## For the person installing

1. Run `NorrinTPM-Setup.exe` (Windows may show "Windows protected your PC" because the file is not code-signed:
   choose *More info > Run anyway*).
2. Keep the defaults and press **Install**. The app starts when setup finishes; later start it from the Start menu:
   *Norrin Trustworthy Process Monitor*.
3. A small control window shows that the app is running and opens it in its own window. Closing that control
   window (or **Stop and exit**) stops the app.
4. Local AI: click **Local model** in the top bar of the app. The panel shows what is installed, lets you choose the
   model, download another one, and install Ollama if the computer has none. Without any model the analysis still
   works; explanations are then written from templates.

| What | Where |
| --- | --- |
| Program files | `%LOCALAPPDATA%\Programs\NorrinTPM` |
| Your analyses, settings, keys, logs | `%LOCALAPPDATA%\NorrinTPM` (`workspace\`, `settings.yaml`, `.env`, `logs\app.log`) |
| Shortcuts | Start menu folder *Norrin TPM*, desktop |
| Uninstall | Settings > Apps > Installed apps, or Start menu > *Uninstall Norrin TPM* |

Upgrading: run a newer setup file; analyses and settings are kept. Uninstalling asks whether to delete the
analyses too (default: keep). Optional keys (Anthropic for the hybrid profile, mail server) go into
`%LOCALAPPDATA%\NorrinTPM\.env`; the control window's **Data folder** button opens that folder.

Silent install (IT departments, tests): `NorrinTPM-Setup.exe /S [/D=C:\path] [--no-desktop] [--no-launch]`;
result in `%TEMP%\NorrinTPM-Setup.log`.

The same folder holds `NorrinTPM-cli.exe`, the command-line tool (`NorrinTPM-cli.exe run data.csv`,
`... doctor`, `... models --pull qwen3:4b`), using the same data folder.

## For the person building it

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```

needs the project environment (`.venv` with `requirements.txt`); the script installs PyInstaller when missing.
Steps: icon + plotly.min.js, freeze the app (`packaging/windows/norrin_tpm.spec`, one folder, two executables),
smoke test of the frozen app (doctor + a full analysis of a bundled sample), zip the payload, build the setup
program (`packaging/windows/installer.py`, a small wizard that carries the payload). Output:
`dist\NorrinTPM-Setup.exe` (about 150 MB; too large for a git repository: attach it to a GitHub release).

How it fits together:

- `tpm/desktop.py` is the app's entry point: prepares the per-user data folder and environment
  (`TPM_WORKSPACE`, `TPM_SETTINGS`, `.env`) before the rest of the package is imported, starts the web server in
  this process on port 8000 (or a free port), shows the control window and opens the UI in an app-style window
  (Edge `--app`, else the default browser). A second start just re-opens the window of the running instance.
- Pipeline stages are imported by name at run time, so the spec lists every `tpm.*` module as a hidden import and
  ships every non-Python file of the package (UI, prompt templates, report templates, translations), plus
  `config/settings.yaml` and `samples/`. The plotly Python package is not bundled; `plotly.min.js` ships as a file.
- The setup program writes only to the user's profile: files, shortcuts, and
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\NorrinTPM`. The uninstaller is a PowerShell script in
  the install folder that continues from a temp copy (a script cannot delete the folder it runs from).

Tests: `tests/test_f_desktop.py` (launcher, data folder, headless start, installer helpers).
