"""Setup program of the Windows app (built into NorrinTPM-Setup.exe by build.ps1).

Per-user install, no administrator rights: files go to %LOCALAPPDATA%\\Programs\\NorrinTPM, shortcuts to the
Start menu and the desktop, and the app is registered under "Installed apps" (HKCU) with an uninstaller.
Analyses, settings and keys live in %LOCALAPPDATA%\\NorrinTPM and are never touched by install or upgrade.

    NorrinTPM-Setup.exe                     wizard
    NorrinTPM-Setup.exe /S [/D=C:\\path]    silent (also: --no-desktop --no-launch)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

APP_ID = "NorrinTPM"
APP_NAME = "Norrin Trustworthy Process Monitor"
PUBLISHER = "Team abarofchocolate"
EXE = "NorrinTPM.exe"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def resource(name: str) -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / name


def payload_info() -> dict:
    try:
        return json.loads(resource("payload.json").read_text(encoding="utf-8"))
    except Exception:
        return {"version": "0.0.0", "bytes": 0, "files": 0}


def default_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / APP_ID


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_ID


def _ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], capture_output=True, text=True, creationflags=NO_WINDOW)


def _q(s: object) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _folder(kind: str) -> Path:
    """Desktop / Programs through the shell, so OneDrive-redirected folders resolve correctly."""
    r = _ps(f"[Environment]::GetFolderPath({_q(kind)})")
    p = (r.stdout or "").strip()
    if p:
        return Path(p)
    home = Path.home()
    return home / "Desktop" if kind == "Desktop" else Path(os.environ.get("APPDATA", str(home))) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def make_shortcut(lnk: Path, target: Path, workdir: Path, icon: Path, description: str, arguments: str = "") -> bool:
    lnk.parent.mkdir(parents=True, exist_ok=True)
    script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({_q(lnk)});$s.TargetPath={_q(target)};"
              f"$s.Arguments={_q(arguments)};$s.WorkingDirectory={_q(workdir)};$s.IconLocation={_q(icon)};"
              f"$s.Description={_q(description)};$s.Save()")
    return _ps(script).returncode == 0 and lnk.exists()


def app_running() -> Optional[str]:
    try:
        url = json.loads((data_dir() / "instance.json").read_text(encoding="utf-8"))["url"]
        with urllib.request.urlopen(url + "/api/health", timeout=1.5):
            return str(url)
    except Exception:
        return None


UNINSTALL_PS1 = r"""# Uninstaller of __NAME__ (per-user install). Started from "Installed apps" or the Start menu.
param([switch]$Silent, [switch]$RemoveData, [string]$InstallDir = "")
$ErrorActionPreference = 'SilentlyContinue'
$appId = '__APPID__'; $name = '__NAME__'
$me = $MyInvocation.MyCommand.Path
if (-not $InstallDir) {
    # a script cannot delete the folder it runs from: continue from a copy in the temp folder
    $InstallDir = Split-Path -Parent $me
    $copy = Join-Path $env:TEMP ("uninstall_" + $appId + ".ps1")
    Copy-Item $me $copy -Force
    $a = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$copy`"", '-InstallDir', "`"$InstallDir`"")
    if ($Silent) { $a += '-Silent' }
    if ($RemoveData) { $a += '-RemoveData' }
    Start-Process powershell -ArgumentList $a -WindowStyle Hidden
    exit 0
}
Add-Type -AssemblyName System.Windows.Forms
if (-not $Silent) {
    $r = [System.Windows.Forms.MessageBox]::Show("Remove $name from this computer?", $name, 'YesNo', 'Question')
    if ($r -ne 'Yes') { exit 1 }
}
Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir, [System.StringComparison]::OrdinalIgnoreCase) } | Stop-Process -Force
Start-Sleep -Milliseconds 800
$programs = [Environment]::GetFolderPath('Programs'); $desktop = [Environment]::GetFolderPath('Desktop')
Remove-Item (Join-Path $programs 'Norrin TPM') -Recurse -Force
Remove-Item (Join-Path $desktop ($name + '.lnk')) -Force
for ($i = 0; $i -lt 5 -and (Test-Path $InstallDir); $i++) { Remove-Item $InstallDir -Recurse -Force; Start-Sleep -Milliseconds 500 }
Remove-Item ("HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\" + $appId) -Recurse -Force
$data = Join-Path $env:LOCALAPPDATA $appId
if (Test-Path $data) {
    $del = [bool]$RemoveData
    if (-not $Silent) {
        $r = [System.Windows.Forms.MessageBox]::Show("Also delete your analyses, settings and keys?`n$data`n`nChoose No to keep them for a later install.", $name, 'YesNo', 'Warning', 'Button2')
        $del = ($r -eq 'Yes')
    }
    if ($del) { Remove-Item $data -Recurse -Force }
}
if (-not $Silent) { [System.Windows.Forms.MessageBox]::Show("$name was removed.", $name, 'OK', 'Information') | Out-Null }
"""


def register(install_dir: Path, version: str, size_bytes: int) -> None:
    import winreg

    un = install_dir / "uninstall.ps1"
    cmd = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{un}"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}") as k:
        for name, val in (("DisplayName", APP_NAME), ("DisplayVersion", version), ("Publisher", PUBLISHER), ("InstallLocation", str(install_dir)),
                          ("DisplayIcon", str(install_dir / EXE)), ("UninstallString", cmd), ("QuietUninstallString", cmd + " -Silent"),
                          ("InstallDate", time.strftime("%Y%m%d")), ("URLInfoAbout", "https://github.com/tommy2006/abarofchocolate")):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
        for name, val in (("NoModify", 1), ("NoRepair", 1), ("EstimatedSize", max(1, size_bytes // 1024))):
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, int(val))


def install(install_dir: Path, desktop: bool = True, progress: Optional[Callable[[float, str], None]] = None) -> Path:
    """Extract the app, write the uninstaller, shortcuts and the "Installed apps" entry. Returns the app exe."""
    say = progress or (lambda f, m: None)
    info = payload_info()
    payload = resource("app.zip")
    if not payload.exists():
        raise RuntimeError("this setup file is incomplete (app.zip is missing)")
    if app_running():
        raise RuntimeError("The app is running. Choose 'Stop and exit' in its small control window, then run setup again.")
    install_dir = Path(install_dir)
    if install_dir.exists():
        say(0.01, "Removing the previous version ...")
        old = install_dir.with_name(install_dir.name + ".old")
        shutil.rmtree(old, ignore_errors=True)
        try:
            install_dir.rename(old)  # fails while any file is in use: nothing is half-deleted
        except OSError as e:
            raise RuntimeError(f"The previous version is still in use (close the app first): {e}")
        shutil.rmtree(old, ignore_errors=True)
    install_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(payload) as z:
        members = z.infolist()
        want = sum(m.file_size for m in members) or 1
        for i, m in enumerate(members):
            target = (install_dir / m.filename).resolve()
            if not str(target).startswith(str(install_dir.resolve())):
                raise RuntimeError(f"unsafe path in the package: {m.filename}")
            z.extract(m, install_dir)
            total += m.file_size
            if i % 40 == 0:
                say(0.02 + 0.9 * total / want, f"Copying files ... {total // 1_000_000} of {want // 1_000_000} MB")
    say(0.94, "Creating shortcuts ...")
    exe = install_dir / EXE
    (install_dir / "uninstall.ps1").write_text(UNINSTALL_PS1.replace("__NAME__", APP_NAME).replace("__APPID__", APP_ID), encoding="utf-8-sig")
    programs = _folder("Programs") / "Norrin TPM"
    make_shortcut(programs / f"{APP_NAME}.lnk", exe, install_dir, exe, "Trustworthy process monitor: runs fully on this computer")
    make_shortcut(programs / "Uninstall Norrin TPM.lnk", Path("powershell.exe"), install_dir, exe, "Remove the app", f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{install_dir / "uninstall.ps1"}"')
    if desktop:
        make_shortcut(_folder("Desktop") / f"{APP_NAME}.lnk", exe, install_dir, exe, "Trustworthy process monitor: runs fully on this computer")
    say(0.98, "Registering the app ...")
    register(install_dir, str(info.get("version", "0.0.0")), total)
    say(1.0, "Done.")
    return exe


def launch(exe: Path) -> None:
    subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True)


# ----------------------------------------------------------------------------------------------
# silent mode and wizard
# ----------------------------------------------------------------------------------------------


def run_silent(argv: list[str]) -> int:
    target = default_dir()
    for a in argv:
        if a.upper().startswith("/D="):
            target = Path(a[3:].strip('"'))
    if "--dir" in argv:
        target = Path(argv[argv.index("--dir") + 1])
    log = Path(os.environ.get("TEMP", ".")) / "NorrinTPM-Setup.log"
    try:
        exe = install(target, desktop="--no-desktop" not in argv)
        log.write_text(f"installed to {target}\n", encoding="utf-8")
        if "--no-launch" not in argv:
            launch(exe)
        return 0
    except Exception as e:
        log.write_text(f"FAILED: {e}\n", encoding="utf-8")
        return 2


def run_wizard() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    info = payload_info()
    root = tk.Tk()
    root.title(f"{APP_NAME} - Setup")
    root.resizable(False, False)
    try:
        root.iconbitmap(str(resource("norrin_tpm.ico")))
    except Exception:
        pass
    f = ttk.Frame(root, padding=22)
    f.grid()
    ttk.Label(f, text=f"Install {APP_NAME}", font=("Segoe UI", 14, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(f, text=(f"Version {info.get('version', '')}. Installs for your user only; no administrator rights needed.\n"
                       "The app runs fully on this computer. Your analyses and settings are stored separately\n"
                       f"in {data_dir()} and are kept when you upgrade or uninstall."), justify="left", font=("Segoe UI", 9)).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 14))
    ttk.Label(f, text="Install to:", font=("Segoe UI", 9)).grid(row=2, column=0, sticky="w")
    target = tk.StringVar(value=str(default_dir()))
    ttk.Entry(f, textvariable=target, width=58).grid(row=3, column=0, columnspan=2, sticky="we")

    def browse() -> None:
        p = filedialog.askdirectory(initialdir=str(Path(target.get()).parent))
        if p:
            target.set(str(Path(p) / APP_ID))

    b_browse = ttk.Button(f, text="Browse ...", command=browse)
    b_browse.grid(row=3, column=2, padx=(8, 0))
    v_desktop = tk.BooleanVar(value=True)
    v_launch = tk.BooleanVar(value=True)
    c1 = ttk.Checkbutton(f, text="Create a desktop shortcut", variable=v_desktop)
    c1.grid(row=4, column=0, columnspan=3, sticky="w", pady=(12, 0))
    c2 = ttk.Checkbutton(f, text="Start the app when setup finishes", variable=v_launch)
    c2.grid(row=5, column=0, columnspan=3, sticky="w")
    bar = ttk.Progressbar(f, length=470, maximum=1000)
    bar.grid(row=6, column=0, columnspan=3, sticky="we", pady=(16, 4))
    status = tk.StringVar(value=f"Needs about {max(1, int(info.get('bytes', 0)) // 1_000_000)} MB of disk space.")
    ttk.Label(f, textvariable=status, font=("Segoe UI", 9)).grid(row=7, column=0, columnspan=3, sticky="w")
    b_install = ttk.Button(f, text="Install")
    b_install.grid(row=8, column=2, sticky="e", pady=(16, 0))
    b_cancel = ttk.Button(f, text="Cancel", command=root.destroy)
    b_cancel.grid(row=8, column=1, sticky="e", pady=(16, 0))
    state: dict = {"exe": None, "error": None, "done": False}

    def progress(frac: float, msg: str) -> None:
        root.after(0, lambda: (bar.configure(value=int(frac * 1000)), status.set(msg)))

    def worker() -> None:
        try:
            state["exe"] = install(Path(target.get()), desktop=v_desktop.get(), progress=progress)
        except Exception as e:
            state["error"] = str(e)
        state["done"] = True

    def finish() -> None:
        if not state["done"]:
            root.after(200, finish)
            return
        if state["error"]:
            messagebox.showerror(APP_NAME, state["error"])
            for w in (b_install, b_cancel, b_browse, c1, c2):
                w.configure(state="normal")
            status.set("Setup did not finish. Nothing was half-installed.")
            return
        status.set("Installed. Find it in the Start menu as 'Norrin Trustworthy Process Monitor'.")
        b_cancel.configure(text="Close", state="normal")
        if v_launch.get() and state["exe"]:
            launch(state["exe"])
            root.after(1500, root.destroy)

    def start() -> None:
        for w in (b_install, b_cancel, b_browse, c1, c2):
            w.configure(state="disabled")
        threading.Thread(target=worker, daemon=True).start()
        root.after(200, finish)

    b_install.configure(command=start)
    root.mainloop()
    return 0 if state["exe"] else 1


def main() -> int:
    argv = sys.argv[1:]
    if any(a.upper() == "/S" or a == "--silent" for a in argv):
        return run_silent(argv)
    return run_wizard()


if __name__ == "__main__":
    raise SystemExit(main())
