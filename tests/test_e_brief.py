"""Agent E, UI round 4: brief, jargon-free, actionable summaries (tpm/api/brief.py) and their two routes.

    .venv\\Scripts\\python.exe -m pytest tests/test_e_brief.py -q
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from tpm.api import brief as B

ROOT = Path(__file__).resolve().parents[1]
RUN = "run_brief"
LANGS = ("en", "fi", "sv")
UI_VIEWS = {"runs", "understanding", "quality", "monitor", "diagnoses", "assessor", "log", "dataflow", "report"}
VERDICTS = {"ok", "attention", "problem", "pending"}
FORBIDDEN = re.compile(
    r"(?<![\w-])(detectors?|baselines?|pca|z-?scores?|sigma|folds?|out-of-fold|thresholds?|auroc|changepoints?|exposure|severity score|ensemble|quantiles?|robust|"
    r"artifacts?|egress|ledger|inferences?|hypothes[ie]s|clusters?|autoencoder|isolation forest|cusum|ewma|residuals?|covariance|contamination|p-values?|json|schema|alias(?:es)?)(?![\w-])",
    re.I,
)
REF = re.compile(r"^(?:(?:DIAG|FLAG|CHK|EV|INF|RULE|PATTERN|EGR)-[0-9A-Z]+|B\d{4,6}|S\d{2,3}|rows:\d+-\d+(?::[\w,]+)?|section:[a-z]+)$")


def _texts(b: dict) -> list[str]:
    return [b["headline"], *b["points"], *[a["text"] for a in b["actions"]], *[a["ask"] for a in b["actions"] if a.get("ask")]]


def _check_shape(b: dict, *, item: bool = False) -> None:
    assert b["verdict"] in VERDICTS and b["source"] == "template" and b["language"] in LANGS
    assert isinstance(b["headline"], str) and b["headline"].strip()
    assert len(b["headline"].split()) <= B.MAX_HEADLINE_WORDS, b["headline"]
    assert b["headline"].rstrip()[-1] in ".!?…", b["headline"]
    assert isinstance(b["points"], list) and len(b["points"]) <= B.MAX_POINTS
    for p in b["points"]:
        assert isinstance(p, str) and p.strip() and len(p.split()) <= B.MAX_POINT_WORDS, p
    assert 1 <= len(b["actions"]) <= B.MAX_ACTIONS
    for a in b["actions"]:
        assert set(a) == {"text", "view", "ref", "ask"}, a
        assert a["text"].strip() and (a["view"] or a["ask"]), a
        assert a["view"] is None or a["view"] in UI_VIEWS, a
        assert a["ref"] is None or (a["view"] and REF.match(a["ref"])), a
    for t in _texts(b):
        m = FORBIDDEN.search(t)
        assert not m, f"forbidden word {m.group(0)!r} in: {t}"
        assert "{" not in t and "}" not in t, f"unfilled template: {t}"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient

    from tests.fixtures.fake_workspace import build_fake_workspace
    from tpm.api.server import create_app
    from tpm.config import load_settings
    from tpm.contracts import RunStatus, StageStatus
    from tpm.workspace import Workspace

    base = tmp_path_factory.mktemp("e_brief")
    settings_path = base / "settings.yaml"
    shutil.copy(ROOT / "config" / "settings.yaml", settings_path)
    s = load_settings(settings_path)
    s.workspace_dir = str(base / "workspace")
    ws = build_fake_workspace(s, run_id=RUN, n_groups=8, n_samples=120)
    ws.close()
    # a run whose stages have not run yet, and one that died in the first stage
    for rid, state, first in (("run_fresh", "running", "running"), ("run_dead", "failed", "failed")):
        w = Workspace(run_id=rid, settings=s)
        st = RunStatus(run_id=rid, source_path="x.csv", profile="no-egress", state=state)
        for i, name in enumerate(("ingest", "profile", "quality", "detect", "diagnose", "assess", "report")):
            st.stages.append(StageStatus(stage=name, state=first if i == 0 else "pending"))
        w.set_status(st)
        w.close()
    app = create_app(settings_path=settings_path, workspace_dir=base / "workspace")
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------------------------------------ views
@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("view", B.VIEWS)
def test_every_view_and_language_has_a_brief(client, view, lang):
    r = client.get(f"/api/runs/{RUN}/brief", params={"view": view, "lang": lang})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["view"] == view and b["language"] == lang
    _check_shape(b)
    assert b["verdict"] != "pending", f"{view}: the fake run is finished"


def test_views_cover_the_pages_and_the_whole_run():
    assert set(B.VIEWS) == {"overview", "understanding", "quality", "monitor", "diagnoses", "assessor", "dataflow", "log", "report"}
    for lang in LANGS:
        missing = {k for k in B.T["en"] if not k.endswith(".1")} - set(B.T[lang])
        assert not missing, f"{lang} lacks {sorted(missing)[:10]}"
        for k, v in B.T[lang].items():
            assert not FORBIDDEN.search(v), f"{lang}:{k}"
            assert set(re.findall(r"\{(\w+)\}", v)) <= set(re.findall(r"\{(\w+)\}", B.T["en"].get(k, v))) | {"n"}, f"{lang}:{k} uses a placeholder the English text lacks"


def test_languages_differ_and_numbers_survive(client):
    got = {lang: client.get(f"/api/runs/{RUN}/brief", params={"view": "quality", "lang": lang}).json() for lang in LANGS}
    assert len({got[lang]["headline"] for lang in LANGS}) == 3, "the three languages must not share the English sentence"
    nums = {lang: sorted(re.findall(r"\d+", got[lang]["headline"])) for lang in LANGS}
    assert nums["en"] == nums["fi"] == nums["sv"] and nums["en"], nums
    assert client.get(f"/api/runs/{RUN}/brief", params={"view": "quality", "lang": "xx"}).json()["language"] == "en"


def test_content_is_concrete_and_names_sensors_by_the_file_header(client):
    q = client.get(f"/api/runs/{RUN}/brief", params={"view": "quality"}).json()
    assert re.search(r"\d+ of \d+ batches|all \d+ batches|One of \d+ batches", q["headline"]), q["headline"]
    d = client.get(f"/api/runs/{RUN}/brief", params={"view": "diagnoses"}).json()
    assert d["verdict"] in ("attention", "problem") and d["headline"].startswith("Most important finding")
    assert any(a["view"] == "diagnoses" and (a["ref"] or "").startswith("DIAG-") for a in d["actions"]), d["actions"]
    assert any(a["ask"] for a in d["actions"])
    # a sensor is never called by its internal short name alone when the file had a header
    for view in B.VIEWS:
        b = client.get(f"/api/runs/{RUN}/brief", params={"view": view}).json()
        for t in [b["headline"], *b["points"], *[a["text"] for a in b["actions"]]]:
            for m in re.finditer(r"\bS\d{2,3}\b", t):
                assert t[max(0, m.start() - 1)] == "(" and t[m.end():m.end() + 1] == ")", f"{view}: bare short name in: {t}"
    o = client.get(f"/api/runs/{RUN}/brief", params={"view": "overview"}).json()
    assert len(o["points"]) >= 2 and o["actions"][0]["view"] in ("diagnoses", "quality")
    f = client.get(f"/api/runs/{RUN}/brief", params={"view": "dataflow"}).json()
    assert "computer" in f["headline"]


def test_unknown_view_and_run(client):
    assert client.get(f"/api/runs/{RUN}/brief", params={"view": "nope"}).status_code == 400
    assert client.get("/api/runs/no_such_run/brief", params={"view": "overview"}).status_code == 404
    assert client.get("/api/runs/..%2Fx/brief").status_code in (400, 404)


@pytest.mark.parametrize("lang", LANGS)
def test_pending_before_the_stages_ran(client, lang):
    for view in ("understanding", "quality", "monitor", "diagnoses", "assessor", "report", "overview"):
        b = client.get("/api/runs/run_fresh/brief", params={"view": view, "lang": lang}).json()
        _check_shape(b)
        assert b["verdict"] == "pending", (view, b)
        assert b["actions"][0]["view"] == "runs"
    en = client.get("/api/runs/run_fresh/brief", params={"view": "quality", "lang": "en"}).json()
    assert en["headline"] == "This step has not finished yet."
    # pages that need no stage still say something true
    for view in ("dataflow", "log"):
        b = client.get("/api/runs/run_fresh/brief", params={"view": view, "lang": lang}).json()
        _check_shape(b)
        assert b["verdict"] != "pending"
    dead = client.get("/api/runs/run_dead/brief", params={"view": "overview", "lang": lang}).json()
    _check_shape(dead)
    assert dead["verdict"] == "problem"
    assert client.get("/api/runs/run_dead/brief", params={"view": "monitor", "lang": lang}).json()["verdict"] == "pending"


# ------------------------------------------------------------------------------------------ items
def _first(client, route: str, **params) -> dict:
    return client.get(f"/api/runs/{RUN}/{route}", params=params).json()["items"][0]


@pytest.mark.parametrize("lang", LANGS)
def test_item_briefs_for_diagnosis_flag_check_batch_evidence(client, lang):
    diag = _first(client, "diagnoses")
    flag = _first(client, "flags")
    check = _first(client, "checks", status="fail")
    batch = client.get(f"/api/runs/{RUN}/trust").json()["items"][0]["batch_id"]
    ev = flag["evidence_ids"][0]
    for oid, kind in ((diag["id"], "diagnosis"), (flag["id"], "flag"), (check["check_id"], "check"), (batch, "batch"), (ev, "evidence"), ("PATTERN-A", "pattern"), ("RULE-001", "rule")):
        r = client.get(f"/api/runs/{RUN}/brief/item", params={"id": oid, "lang": lang})
        assert r.status_code == 200, (oid, r.text)
        it = r.json()
        assert it["id"] == oid and it["kind"] == kind, it
        _check_shape(it, item=True)
    assert client.get(f"/api/runs/{RUN}/brief/item", params={"id": "DIAG-999999", "lang": lang}).status_code == 404
    assert client.get(f"/api/runs/{RUN}/brief/item", params={"lang": lang}).status_code == 422


def test_item_content(client):
    diag = _first(client, "diagnoses")
    it = client.get(f"/api/runs/{RUN}/brief/item", params={"id": diag["id"]}).json()
    assert any(p.startswith("How sure:") and re.search(r"(very sure|fairly sure|not very sure|unsure)", p) for p in it["points"]), it["points"]
    assert any(p.startswith("Where:") and "row" in p for p in it["points"]), it["points"]
    assert any(a["view"] == "diagnoses" and a["ref"] == diag["id"] for a in it["actions"]), "accept / question / correct stays one click away"
    assert any(a["ask"] and diag["id"] in a["ask"] for a in it["actions"])
    # a data-quality check says what is wrong and whether the rows can still be used
    check = _first(client, "checks", status="fail")
    ck = client.get(f"/api/runs/{RUN}/brief/item", params={"id": check["check_id"]}).json()
    assert ck["headline"].startswith("Data problem:") and any("rows" in p.lower() and ("used" in p or "set aside" in p or "trusted" in p) for p in ck["points"]), ck
    ok = _first(client, "checks", status="pass")
    assert client.get(f"/api/runs/{RUN}/brief/item", params={"id": ok["check_id"]}).json()["verdict"] == "ok"
    # sloppy ids resolve like everywhere else
    assert client.get(f"/api/runs/{RUN}/brief/item", params={"id": "diag-1"}).json()["id"] == "DIAG-000001"


def test_point_findings_keep_the_teams_wording(tmp_path):
    """A single odd reading is 'a glitch or a manipulation; the data alone can't tell' - in every language."""
    from tpm.config import load_settings
    from tpm.workspace import Workspace

    s = load_settings()
    s.workspace_dir = str(tmp_path / "ws")
    ws = Workspace(run_id="pts", settings=s)
    flag = {"id": "FLAG-000001", "kind": "point", "group_id": "0", "batch_id": "B00001", "row_start": 812, "row_end": 812, "severity": 0.5, "score": 3.1, "threshold": 1.0, "detector": "ensemble:robust_z+iforest",
            "statement": "Isolated suspicious reading", "signals_ranked": [{"signal": "S16", "contribution": 0.4, "direction": "down"}], "evidence_ids": [], "likely_cause_class": "unknown", "confidence": 0.62}
    ws.append_jsonl("flags.jsonl", flag)
    ws.append_jsonl("diagnoses.jsonl", {"id": "DIAG-000001", "flag_ids": ["FLAG-000001"], "group_id": "0", "fault_type": "isolated suspicious readings", "cause_class": "unknown", "confidence": 0.54,
                                         "ranked_signals": [{"signal": "S16", "contribution": 0.4}], "critique": {"verdict": "supported"}, "evidence_ids": []})
    ws.write_json("signals.json", [{"id": "S16", "source_column": "P402"}])
    ws.write_json("detect_meta.json", {"detectors_used": ["robust_z"]})
    wording = {"en": ("a glitch or a manipulation", "the data alone can't tell"), "fi": ("häiriö tai manipulointi", "pelkästä datasta sitä ei voi päätellä"), "sv": ("en störning eller en manipulation", "enbart data kan inte avgöra det")}
    for lang, (a, b) in wording.items():
        for oid in ("FLAG-000001", "DIAG-000001"):
            it = B.brief_item(ws, s, oid, lang)
            _check_shape(it, item=True)
            assert a in it["headline"] and b in it["headline"], (lang, oid, it["headline"])
        m = B.brief_for(ws, s, "monitor", lang)
        _check_shape(m)
        assert m["verdict"] == "attention" and any(b in p for p in m["points"]), m
    en = B.brief_item(ws, s, "DIAG-000001", "en")
    assert any("P402 (S16)" in t for t in en["points"]), "the sensor is named by the header of the person's file, short name in brackets"
    assert en["actions"][0]["ref"] == "section:suspicious" and en["actions"][0]["view"] == "monitor"
    ws.close()


def test_soften_removes_method_words():
    text = "The ensemble score crossed the threshold; PCA and the isolation forest detector agree (out-of-fold, 6 sigma, baseline regime, cluster C01)."
    out = B.soften(text)
    assert not FORBIDDEN.search(out), out
    assert B._clip("one two three four five six", 4) == "one two three …"
    assert len(B._clip("word " * 40, B.MAX_POINT_WORDS).split()) <= B.MAX_POINT_WORDS


# ------------------------------------------------------------------------------------------ UI wiring (no browser)
STATIC = ROOT / "tpm" / "api" / "static"
PAGE_VIEWS = ("understanding", "quality", "monitor", "diagnoses", "assessor", "log", "dataflow", "report")


def _js(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_every_page_shows_the_summary_first_and_one_expander():
    for v in PAGE_VIEWS:
        src = _js(f"js/views/{v}.js")
        assert "from '../brief.js'" in src, v
        assert f"summaryCard('{v}')" in src and f"techDetails('{v}')" in src, f"{v}: summary card + ONE expander"
        assert src.count("techDetails(") == 1, f"{v}: exactly one page-level expander"
        assert "const view = tech.body;" in src, f"{v}: everything the view rendered before lives inside the expander"
        assert src.index("summaryCard(") < src.index("const view = tech.body;"), f"{v}: the summary comes first"
    runs = _js("js/views/runs.js")
    assert "summaryCard('overview')" in runs and "techDetails(" not in runs, "the start page keeps its workflow and gains the overview card"


def test_item_level_explanations_are_plain_first():
    diag, mon, qual = _js("js/views/diagnoses.js"), _js("js/views/monitor.js"), _js("js/views/quality.js")
    core, chat, ass = _js("js/core.js"), _js("js/chat.js"), _js("js/views/assessor.js")
    assert "itemBrief(d.id" in diag and "techNested()" in diag and "brief-decide" in diag, "diagnosis: summary + decision first, the rest nested"
    assert "brief.topFindings" in diag and ".slice(0, 3)" in diag, "accept / question / override of the top findings sit above the expander"
    assert "itemBrief(f.id" in mon and "itemBriefLocal(" in mon and "brief.sus.headline" in mon
    assert "itemBrief(x.batch_id" in qual and "itemBrief(c.check_id" in qual and "techNested(checkCard(c))" in qual
    assert "import('./brief.js')" in core and "revealAncestors(node)" in core, "popovers show the summary first; flash() unfolds the technical part"
    assert "answerBlock(" in chat and "toolTrace(" in chat and "answerBlock(" in ass
    # lists never fire one request per row: item briefs are asked for when a card / row is opened or for the few rows on screen
    assert "rows.map((c) => itemBrief" not in qual and "diags.map((d) => itemBrief" not in diag


def test_brief_js_helpers_under_node(tmp_path):
    import json
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    for name in ("core.js", "brief.js"):
        (tmp_path / name).write_text(_js(f"js/{name}"), encoding="utf-8")
    (tmp_path / "h.js").write_text(
        "import { actionTarget, firstSentences, hashHasTarget, bt } from './brief.js';\n"
        "const out = { diag: actionTarget({ view: 'diagnoses', ref: 'DIAG-000001' }), rows: actionTarget({ view: 'monitor', ref: 'rows:120-180:S01,S02' }), sec: actionTarget({ view: 'monitor', ref: 'section:suspicious' }),\n"
        "  batch: actionTarget({ view: 'quality', ref: 'B00003' }), sig: actionTarget({ view: 'understanding', ref: 'S07' }), none: actionTarget({ view: 'report', ref: null }),\n"
        "  two: firstSentences('One. Two! Three? Four.'), deep: [hashHasTarget('#/monitor?flag=FLAG-000001'), hashHasTarget('#/monitor'), hashHasTarget('#/monitor?x=')], label: [bt('brief.showTech'), bt('brief.hideTech')] };\n"
        "console.log(JSON.stringify(out));\n", encoding="utf-8")
    r = subprocess.run([node, str(tmp_path / "h.js")], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["diag"] == {"view": "diagnoses", "params": {"diag": "DIAG-000001"}}
    assert out["rows"] == {"view": "monitor", "params": {"rows": "120-180", "signals": "S01,S02"}}
    assert out["sec"] == {"view": "monitor", "params": {"section": "suspicious"}}
    assert out["batch"]["params"] == {"batch": "B00003"} and out["sig"]["params"] == {"signal": "S07"} and out["none"]["params"] == {}
    assert out["two"] == "One. Two!" and out["deep"] == [True, False, False]
    assert out["label"] == ["Show technical analyses", "Hide technical analyses"], "the exact labels, also when the dictionaries are not loaded"


def test_sections_named_by_the_server_exist_in_the_views():
    """Every `section:<name>` an action can carry is tagged in the view it points to."""
    import json as _json

    wanted = set(re.findall(r'"([a-z]+)", "section:([a-z]+)"', (ROOT / "tpm" / "api" / "brief.py").read_text(encoding="utf-8")))
    assert wanted, "brief.py names page sections in its actions"
    for view, name in wanted:
        src = _js(f"js/views/{view}.js")
        assert f"'{name}'" in src and "briefSection" in src, f"{view}: no block tagged {name}"
    assert "focusSection(main, params.section)" in _js("app.js")
    for lang in LANGS:
        d = _json.loads(_js(f"i18n/{lang}.json"))
        for k in ("brief.showTech", "brief.hideTech", "brief.todo", "brief.kicker", "brief.verdict.ok", "brief.verdict.attention", "brief.verdict.problem", "brief.verdict.pending", "brief.topFindings", "brief.sus.headline", "brief.overviewTitle", "brief.toolTrace"):
            assert d.get(k, "").strip(), f"{lang}:{k}"
    en = _json.loads(_js("i18n/en.json"))
    assert en["brief.showTech"] == "Show technical analyses" and en["brief.hideTech"] == "Hide technical analyses"
    css = _js("styles.css")
    for cls in (".brief-card", ".brief-item", "details.tech-details", ".tech-caret", '[data-verdict="problem"]', ".brief-top-item"):
        assert cls in css, cls
