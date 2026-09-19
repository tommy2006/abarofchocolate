"""Desktop launcher (the installed Windows app's entry point) and the setup program's pure helpers."""
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    monkeypatch.setenv("TPM_DATA_DIR", str(tmp_path / "data"))
    for var in ("TPM_WORKSPACE", "TPM_SETTINGS", "JOBLIB_MULTIPROCESSING"):
        monkeypatch.setenv(var, "x")  # registers the variable, so whatever the launcher sets is undone after the test
        monkeypatch.delenv(var)
    from tpm import desktop as mod

    return mod


def test_prepare_environment_keeps_user_data_outside_the_install_folder(desktop, tmp_path):
    d = desktop.prepare_environment()
    assert d == tmp_path / "data"
    assert (d / "workspace").is_dir() and (d / "logs").is_dir()
    assert os.environ["TPM_WORKSPACE"] == str(d / "workspace")
    # a private copy of the shipped settings, so UI choices survive an upgrade
    assert Path(os.environ["TPM_SETTINGS"]) == d / "settings.yaml"
    assert (d / "settings.yaml").read_text(encoding="utf-8") == (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    assert (d / ".env").exists()
    assert os.environ["JOBLIB_MULTIPROCESSING"] == "0"  # a frozen app must not spawn copies of itself


def test_prepare_environment_does_not_override_explicit_choices(desktop, tmp_path, monkeypatch):
    monkeypatch.setenv("TPM_WORKSPACE", str(tmp_path / "elsewhere"))
    desktop.prepare_environment()
    assert os.environ["TPM_WORKSPACE"] == str(tmp_path / "elsewhere")
    (tmp_path / "data" / "settings.yaml").write_text("profile: hybrid\n", encoding="utf-8")
    desktop.prepare_environment()  # second start: the user's file is left alone
    assert (tmp_path / "data" / "settings.yaml").read_text(encoding="utf-8") == "profile: hybrid\n"


def test_settings_file_from_the_environment_is_used(desktop, tmp_path, monkeypatch):
    custom = tmp_path / "my_settings.yaml"
    custom.write_text((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8").replace("time_budget_s: 1200", "time_budget_s: 777"), encoding="utf-8")
    monkeypatch.setenv("TPM_SETTINGS", str(custom))
    monkeypatch.delenv("TPM_TIME_BUDGET_S", raising=False)
    from tpm import config

    s = config.load_settings()
    assert s.time_budget_s == 777 and Path(s.settings_path) == custom


def test_pick_port_avoids_a_busy_port(desktop, monkeypatch):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        busy = s.getsockname()[1]
        monkeypatch.setattr(desktop, "PREFERRED_PORT", busy)
        port = desktop.pick_port()
        assert port != busy and 1024 < port < 65536


def test_running_instance_ignores_a_stale_file(desktop, tmp_path):
    d = desktop.prepare_environment()
    (d / "instance.json").write_text(json.dumps({"url": "http://127.0.0.1:9", "pid": 1}), encoding="utf-8")
    assert desktop.running_instance(d) is None


def test_headless_start_serves_the_ui_and_stops(desktop, tmp_path):
    d = desktop.prepare_environment()
    port = desktop.pick_port()
    srv = desktop.ServerThread(port)
    srv.start()
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 40
        while time.time() < deadline and not desktop.healthy(url) and not srv.error:
            time.sleep(0.2)
        assert srv.error is None, srv.error
        assert desktop.healthy(url)
        assert desktop._analysis_running(url) is False
    finally:
        srv.stop()
        srv.join(timeout=10)
    assert not srv.is_alive()


def _load_installer():
    spec = importlib.util.spec_from_file_location("tpm_installer", ROOT / "packaging" / "windows" / "installer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_installer_uninstall_script_is_complete():
    inst = _load_installer()
    ps = inst.UNINSTALL_PS1.replace("__NAME__", inst.APP_NAME).replace("__APPID__", inst.APP_ID)
    assert "__NAME__" not in ps and "__APPID__" not in ps
    for needle in ("CurrentVersion\\Uninstall\\", "GetFolderPath('Programs')", "GetFolderPath('Desktop')", "Stop-Process", "$env:LOCALAPPDATA"):
        assert needle in ps
    # analyses are kept unless the user says yes: the default button of that question is "No"
    assert "'Button2'" in ps


def test_installer_quotes_powershell_strings():
    inst = _load_installer()
    assert inst._q("it's") == "'it''s'"
    assert inst.default_dir().name == inst.APP_ID


def _spec_shipped_files() -> set[str]:
    """Paths (relative to the bundle root) of the data files the Windows build ships: the file lists of
    packaging/windows/norrin_tpm.spec, evaluated with the PyInstaller build steps left out."""
    spec = ROOT / "packaging" / "windows" / "norrin_tpm.spec"
    src = spec.read_text(encoding="utf-8").split("\na = Analysis(")[0]
    src = src.replace("from PyInstaller.utils.hooks import collect_data_files, collect_submodules", "")
    ns = {"SPECPATH": str(spec.parent), "collect_data_files": lambda *a, **k: [], "collect_submodules": lambda *a, **k: []}
    exec(compile(src, str(spec), "exec"), ns)
    return {(Path(dest) / Path(path).name).as_posix() for path, dest in ns["datas"]}


def test_the_windows_build_ships_every_config_file_the_app_reads():
    """The frozen app looks for config/*.yaml at the bundle root (tpm.config: settings.yaml, tpm.live.signatures: the
    known failure types). A file the spec leaves out silently switches a feature off in the installed app: without
    failure_signatures.yaml the live monitor only ever raises generic drift alarms."""
    from tpm.live import signatures

    shipped = _spec_shipped_files()
    need = {p.relative_to(ROOT).as_posix() for p in (ROOT / "config").glob("*.yaml")}
    need.add(signatures.CONFIG_FILE.relative_to(ROOT).as_posix())
    missing = sorted(need - shipped)
    assert not missing, f"packaging/windows/norrin_tpm.spec does not ship {missing}: add them to `datas` (e.g. every config/*.yaml)"
    for page in ("tpm/api/static/logo.png", "tpm/api/static/js/views/live.js", "tpm/api/static/js/views/settings.js", "tpm/api/static/styles-live.css"):
        assert page in shipped, page


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer")
def test_installer_refuses_a_package_without_payload(tmp_path):
    inst = _load_installer()
    with pytest.raises(RuntimeError):
        inst.install(tmp_path / "target", desktop=False)
    assert not (tmp_path / "target").exists()
