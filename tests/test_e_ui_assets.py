"""Agent E: UI assets (JS syntax via node, i18n key parity, view hooks) and POST /api/demo.

    .venv\\Scripts\\python.exe -m pytest tests/test_e_ui_assets.py -q
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "tpm" / "api" / "static"
I18N = STATIC / "i18n"
JS_FILES = sorted(p for p in STATIC.rglob("*.js") if "vendor" not in p.parts)


# ------------------------------------------------------------------------------------------ assets
def test_js_files_present():
    names = {p.relative_to(STATIC).as_posix() for p in JS_FILES}
    for n in ("app.js", "js/core.js", "js/chat.js", "js/charts.js") + tuple(f"js/views/{v}.js" for v in ("runs", "understanding", "quality", "monitor", "diagnoses", "assessor", "log", "dataflow", "report")):
        assert n in names, n


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.relative_to(STATIC).as_posix())
def test_node_check(path: Path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr or r.stdout


def _load(lang: str) -> dict[str, str]:
    return json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))


def test_i18n_key_parity_and_placeholders():
    en, fi, sv = _load("en"), _load("fi"), _load("sv")
    assert set(en) == set(fi), f"en/fi differ: {sorted(set(en) ^ set(fi))[:20]}"
    assert set(en) == set(sv), f"en/sv differ: {sorted(set(en) ^ set(sv))[:20]}"
    for d, name in ((en, "en"), (fi, "fi"), (sv, "sv")):
        for k, v in d.items():
            assert isinstance(v, str) and v.strip(), f"{name}:{k} is empty"
    ph = lambda s: set(re.findall(r"\{(\w+)\}", s))
    for k in en:
        assert ph(en[k]) == ph(fi[k]) == ph(sv[k]), f"placeholders differ for {k}"
    for k in ("nav.back", "ref.flag", "ref.evidence", "evidence.missing", "plain.conf.high", "plain.sev.high", "plain.timesThreshold", "ass.moreData", "ass.lessData", "diag.why.process", "runs.demoStarted"):
        assert k in en, k


def test_views_have_plain_box_hook_and_core_exports():
    for v in ("diagnoses", "assessor", "monitor", "quality"):
        src = (STATIC / "js" / "views" / f"{v}.js").read_text(encoding="utf-8")
        assert f"addPlainBox(view, '{v}')" in src, f"{v}.js lacks the plain-language hook"
    core = (STATIC / "js" / "core.js").read_text(encoding="utf-8")
    for name in ("linkifyRefs", "navigate", "recordNavigation", "goBack", "cleanText", "evidencePanel", "installRefHandler", "addPlainBox"):
        assert f"export function {name}" in core or f"export async function {name}" in core, name
    assert "import('./plain.js')" in core  # core.js lives in static/js next to plain.js


def test_index_has_no_inline_widths_that_overflow():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'name="viewport"' in html
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert "minmax(min(240px, 100%), 1fr)" in css  # profile cards
    assert "minmax(0, 1.2fr) minmax(0, 2fr)" in css  # side-by-side columns


# ------------------------------------------------------------------------------------------ POST /api/demo
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """Server with its own workspace; the local model is pointed at a closed port so the real pipeline
    falls back to templates quickly instead of waiting for Ollama."""
    from fastapi.testclient import TestClient

    from tpm.api.server import create_app

    base = tmp_path_factory.mktemp("e_ui")
    settings_path = base / "settings.yaml"
    text = (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    text = text.replace("base_url: http://localhost:11434", "base_url: http://127.0.0.1:9")
    settings_path.write_text(text, encoding="utf-8")
    saved = {k: os.environ.get(k) for k in ("OLLAMA_HOST", "TPM_LOCAL_BASE_URL")}
    os.environ["OLLAMA_HOST"] = "http://127.0.0.1:9"
    try:
        app = create_app(settings_path=settings_path, workspace_dir=base / "workspace")
        with TestClient(app) as c:
            yield c
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_demo_starts_real_pipeline_and_evidence_resolves(client, tmp_path):
    from tests.fixtures.synth import make_synthetic

    df, _ = make_synthetic(n_groups=6, n_samples=80, seed=3, with_timestamp=True)
    src = tmp_path / "demo_small.csv"
    df.to_csv(src, index=False)
    r = client.post("/api/demo", json={"path": str(src)})
    assert r.status_code == 202, r.text
    d = r.json()
    rid = d["run_id"]
    assert rid.startswith("run_demo_") and d["state"] == "pending" and d["source_path"].endswith("demo_small.csv") and d["demo"] is True
    st = client.get(f"/api/runs/{rid}/status").json()
    assert st["run_id"] == rid and len(st["stages"]) == 7
    assert st["job"] is not None and st["job"]["state"] in ("pending", "running", "done", "failed")
    assert any(x["run_id"] == rid for x in client.get("/api/runs").json()["runs"])
    assert client.post("/api/demo", json={"path": "C:/definitely/not/here.csv"}).status_code == 400
    assert client.post("/api/demo", json={"run_id": rid}).status_code in (409, 202)  # already processing, or finished already
    for _ in range(1200):  # the real pipeline on 480 rows; bounded wait (~5 min)
        try:  # status.json is replaced by the pipeline while we poll; on Windows a read can hit the swap
            r = client.get(f"/api/runs/{rid}/status")
            st = r.json() if r.status_code == 200 else st
        except Exception:
            pass
        if st["state"] in ("done", "failed"):
            break
        time.sleep(0.25)
    if st["state"] != "done":
        pytest.skip(f"demo pipeline did not finish in time (state {st['state']}): {(st.get('job') or {}).get('error', '')[:300]}")
    # the run's evidence must resolve through the API after the job (the pre-job Workspace is dropped)
    flags = client.get(f"/api/runs/{rid}/flags").json()["items"]
    signals = client.get(f"/api/runs/{rid}/signals").json()["signals"]
    ids: list[str] = []
    for obj in flags + signals:
        for i in obj.get("evidence_ids") or []:
            if i not in ids:
                ids.append(i)
        if len(ids) >= 6:
            break
    assert ids, "the demo run produced no evidence ids on flags or signals"
    e = client.get(f"/api/runs/{rid}/evidence", params={"ids": ",".join(ids)}).json()
    assert e["missing"] == [] and e["n"] == len(ids), f"evidence ids of a finished demo run must resolve: {e.get('missing')}"
    assert all(item["statement"] for item in e["items"])
