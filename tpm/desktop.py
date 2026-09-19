"""Desktop launcher: what the installed Windows app starts (Start menu / desktop shortcut).

It starts the local web server inside this process, shows a small control window (status, open, stop) and
opens the UI in an app-style window. No console, no commands. Also runs from source: `python -m tpm.desktop`.

User data never lives in the install folder: workspace, settings, keys and logs are under
%LOCALAPPDATA%\\NorrinTPM (or ~/.norrin_tpm on other systems), so an upgrade or uninstall keeps the analyses.
The environment is prepared BEFORE tpm.config is imported, because that module reads it at import time.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Optional

APP_NAME = "Norrin Trustworthy Process Monitor"
APP_ID = "NorrinTPM"
PREFERRED_PORT = 8000


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Folder that holds config/, samples/ and the tpm package: the PyInstaller bundle or the source checkout."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    override = os.environ.get("TPM_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / APP_ID if base else Path.home() / ".norrin_tpm"


def prepare_environment() -> Path:
    """Create the per-user data folder and point the app at it. Explicit environment variables always win."""
    d = data_dir()
    for sub in ("workspace", "logs"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TPM_WORKSPACE", str(d / "workspace"))
    # user settings: a private copy of the shipped defaults, so choices made in the UI survive an upgrade
    user_settings = d / "settings.yaml"
    shipped = bundle_root() / "config" / "settings.yaml"
    if not user_settings.exists() and shipped.exists():
        shutil.copyfile(shipped, user_settings)
    if user_settings.exists():
        os.environ.setdefault("TPM_SETTINGS", str(user_settings))
    env_file = d / ".env"
    if not env_file.exists():
        example = bundle_root() / ".env.example"
        try:
            if example.exists():
                shutil.copyfile(example, env_file)
            else:
                env_file.write_text("# Optional keys for this app. Example:\n# ANTHROPIC_API_KEY=\n", encoding="utf-8")
        except OSError:
            pass
    if os.environ.get("TPM_NO_DOTENV", "").strip().lower() not in ("1", "true", "yes"):  # same switch as tpm.config
        try:
            from dotenv import load_dotenv

            load_dotenv(env_file, override=False)
        except Exception:
            pass
    # a frozen app must never start worker *processes* (they would re-launch this window): threads only
    os.environ.setdefault("JOBLIB_MULTIPROCESSING", "0")
    return d


def _redirect_output(log_dir: Path) -> None:
    """A windowed app has no console: sys.stdout / sys.stderr are None and the first print() would raise."""
    if sys.stdout is not None and sys.stderr is not None and not is_frozen():
        return
    try:
        log = log_dir / "app.log"
        if log.exists() and log.stat().st_size > 5_000_000:
            log.replace(log_dir / "app.previous.log")
        f = open(log, "a", encoding="utf-8", buffering=1)
        sys.stdout = f
        sys.stderr = f
    except OSError:
        sys.stdout = open(os.devnull, "w")
        sys.stderr = sys.stdout


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port() -> int:
    if _port_free(PREFERRED_PORT):
        return PREFERRED_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get_json(url: str, timeout: float = 2.0) -> Optional[Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # loopback only
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def healthy(url: str) -> bool:
    return _get_json(url + "/api/health") is not None


def running_instance(d: Path) -> Optional[str]:
    """URL of an already running copy of this app (second double-click just opens the window again)."""
    try:
        info = json.loads((d / "instance.json").read_text(encoding="utf-8"))
        url = str(info["url"])
    except Exception:
        return None
    return url if healthy(url) else None


def _edge() -> Optional[str]:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if p.exists():
                return str(p)
    return None


def open_ui(url: str, app_window: bool = True) -> None:
    """App-style window (no address bar) through Edge, which every Windows 11 has; else the default browser."""
    edge = _edge() if app_window else None
    if edge:
        try:
            subprocess.Popen([edge, f"--app={url}", "--new-window"], close_fds=True)
            return
        except OSError:
            pass
    webbrowser.open(url)


class ServerThread(threading.Thread):
    def __init__(self, port: int):
        super().__init__(daemon=True, name="tpm-server")
        self.port = port
        self.server: Any = None
        self.error: Optional[str] = None

    def run(self) -> None:
        try:
            import uvicorn

            from tpm.api.server import app

            config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="info", reload=False, timeout_graceful_shutdown=3, use_colors=False)
            self.server = uvicorn.Server(config)
            self.server.run()
        except BaseException as e:  # shown in the control window instead of vanishing with the thread
            import traceback

            self.error = f"{type(e).__name__}: {e}"
            traceback.print_exc()

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True


def _analysis_running(url: str) -> bool:
    data = _get_json(url + "/api/runs", timeout=3.0)
    runs = data.get("runs") if isinstance(data, dict) else data
    return any(isinstance(r, dict) and r.get("state") in ("running", "pending") for r in (runs or []))


def _control_window(url: str, d: Path, srv: ServerThread) -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title(APP_NAME)
    root.resizable(False, False)
    try:
        ico = bundle_root() / "packaging" / "windows" / "norrin_tpm.ico"
        if ico.exists():
            root.iconbitmap(str(ico))
    except Exception:
        pass
    frame = ttk.Frame(root, padding=18)
    frame.grid()
    ttk.Label(frame, text=APP_NAME, font=("Segoe UI", 13, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
    status = tk.StringVar(value="Starting the local server ...")
    ttk.Label(frame, textvariable=status, font=("Segoe UI", 10)).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 2))
    ttk.Label(frame, text="Everything runs on this computer. Keep this window open while you work;\nclosing it stops the app.", foreground="#555", font=("Segoe UI", 9), justify="left").grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 12))
    b_open = ttk.Button(frame, text="Open the app", command=lambda: open_ui(url), state="disabled")
    b_open.grid(row=3, column=0, sticky="w")
    ttk.Button(frame, text="Open in my browser", command=lambda: open_ui(url, app_window=False)).grid(row=3, column=1, padx=8)
    ttk.Button(frame, text="Data folder", command=lambda: os.startfile(str(d)) if hasattr(os, "startfile") else None).grid(row=3, column=2)  # noqa: S606
    opened = {"done": False}

    def quit_app() -> None:
        if healthy(url) and _analysis_running(url):
            if not messagebox.askyesno(APP_NAME, "An analysis is still running. Stop the app anyway?\n\nFinished stages are kept; the run can be started again later."):
                return
        srv.stop()
        try:
            (d / "instance.json").unlink(missing_ok=True)
        except OSError:
            pass
        root.after(300, root.destroy)

    ttk.Button(frame, text="Stop and exit", command=quit_app).grid(row=4, column=0, sticky="w", pady=(12, 0))
    root.protocol("WM_DELETE_WINDOW", quit_app)

    def poll() -> None:
        if srv.error:
            status.set("The server could not start: " + srv.error[:160] + f"\nDetails: {d / 'logs' / 'app.log'}")
            return
        if healthy(url):
            status.set(f"Running at {url}")
            b_open.configure(state="normal")
            if not opened["done"]:
                opened["done"] = True
                open_ui(url)
            root.after(5000, poll)
        else:
            root.after(400, poll)

    root.after(200, poll)
    root.mainloop()


def main(argv: Optional[list[str]] = None) -> int:
    import multiprocessing

    multiprocessing.freeze_support()
    argv = list(sys.argv[1:] if argv is None else argv)
    d = prepare_environment()
    _redirect_output(d / "logs")
    existing = running_instance(d)
    if existing:
        open_ui(existing)
        return 0
    port = pick_port()
    url = f"http://127.0.0.1:{port}"
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] starting {APP_NAME} on {url}; data folder {d}", flush=True)
    srv = ServerThread(port)
    srv.start()
    try:
        (d / "instance.json").write_text(json.dumps({"url": url, "pid": os.getpid(), "started": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
    except OSError:
        pass
    if "--no-window" in argv:  # headless (tests, services): serve until the process is stopped
        try:
            while srv.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            srv.stop()
        return 0 if not srv.error else 1
    try:
        _control_window(url, d, srv)
    finally:
        srv.stop()
        srv.join(timeout=6)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
