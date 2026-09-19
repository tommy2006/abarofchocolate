"""Round 5: the suggestion library (tpm/api/advice.py), GET /api/runs/{id}/advice, and why / fix on item briefs.

    python -m pytest tests/test_e_advice.py -q
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from tpm.api import advice as A
from tpm.api.brief import FORBIDDEN_RE

ROOT = Path(__file__).resolve().parents[1]
RUN = "run_advice"


def _entries(lang):
    return A.LIBRARY[lang]


def test_library_covers_every_problem_kind_in_three_languages():
    keys = {f"check.{c}" for c in A.CHECK_TYPES} | {f"diagnosis.{c}.{k}" for c in A.CAUSES for k in A.FLAG_KINDS} | {f"batch.{b}" for b in A.BATCH_KINDS}
    # every check type the quality stage can emit, and the words of plain.py, are covered
    src = (ROOT / "tpm" / "quality" / "checks.py").read_text(encoding="utf-8")
    for ct in set(re.findall(r'self\._make\("([a-z_]+)"', src)):
        assert "check." + A.check_key(ct) in keys and A.check_key(ct) != "other", ct
    for lang in A.LANGS:
        assert set(_entries(lang)) == keys, lang
        for k, e in _entries(lang).items():
            assert e["why"].strip() and e["why"].rstrip()[-1] in ".!?", (lang, k)
            assert 2 <= len(e["fix"]) <= A.MAX_FIX, (lang, k, len(e["fix"]))
            assert e["use"] in A.USE, (lang, k)
            for txt in [e["why"], *e["fix"]]:
                assert not FORBIDDEN_RE.search(txt), f"{lang}:{k}: {txt}"
                assert set(re.findall(r"\{(\w+)\}", txt)) <= set(A.DEFAULTS["en"]), f"{lang}:{k} unknown placeholder"
    assert len({_entries(lang)["check.stuck"]["why"] for lang in A.LANGS}) == 3


def test_advice_for_fills_facts_and_defaults():
    a = A.advice_for("check", "stuck", {"sensor": "Cooler current (S01)", "rows": "rows 120–180", "batch": "B00003"}, "en")
    assert "Cooler current (S01)" in a["why"] and "rows 120–180" in a["why"] and a["can_use_rows"] == "partly"
    assert any("Cooler current (S01)" in s for s in a["fix"])
    for lang in A.LANGS:
        for key in _entries(lang):
            kind, sub = key.split(".", 1)
            out = A.advice_for(kind, sub, {}, lang)
            assert out["key"] == key
            for txt in [out["why"], *out["fix"]]:
                assert "{" not in txt and "}" not in txt and txt[0] == txt[0].upper(), txt
    assert A.advice_for("check", "rule:RULE-001", {}, "en")["key"] == "check.rule"
    assert A.advice_for("check", "never_heard_of", {}, "en")["key"] == "check.other"
    assert A.advice_for("flag", ("sensor", "drift"), {}, "sv")["key"] == "diagnosis.sensor.drift"
    assert A.advice_for("diagnosis", "nonsense", {}, "xx")["key"] == "diagnosis.unknown.anomaly"
    p = A.advice_for("flag", ("process", "point"), {}, "en")
    assert "glitch or a manipulation" in p["why"]
    assert A.advice_for("batch", "untrusted", {"batch": "B00002", "n": 4}, "en")["can_use_rows"] == "no"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient

    from tests.fixtures.fake_workspace import build_fake_workspace
    from tpm.api.server import create_app
    from tpm.config import load_settings

    base = tmp_path_factory.mktemp("e_advice")
    settings_path = base / "settings.yaml"
    shutil.copy(ROOT / "config" / "settings.yaml", settings_path)
    s = load_settings(settings_path)
    s.workspace_dir = str(base / "workspace")
    build_fake_workspace(s, run_id=RUN, n_groups=8, n_samples=120).close()
    with TestClient(create_app(settings_path=settings_path, workspace_dir=base / "workspace")) as c:
        yield c


def _first(client, route, **params):
    return client.get(f"/api/runs/{RUN}/{route}", params=params).json()["items"][0]


@pytest.mark.parametrize("lang", A.LANGS)
def test_route_answers_for_check_flag_diagnosis_batch(client, lang):
    ids = {"check": _first(client, "checks", status="fail")["check_id"], "flag": _first(client, "flags")["id"], "diagnosis": _first(client, "diagnoses")["id"],
           "batch": client.get(f"/api/runs/{RUN}/trust").json()["items"][0]["batch_id"]}
    for kind, oid in ids.items():
        r = client.get(f"/api/runs/{RUN}/advice", params={"kind": kind, "id": oid, "lang": lang})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["id"] == oid and d["kind"] == kind and d["language"] == lang and d["can_use_rows"] in A.USE
        assert d["why"].strip() and 2 <= len(d["fix"]) <= 4 and "{" not in d["why"] + "".join(d["fix"])
        # the kind may be left out: the id says what it is
        assert client.get(f"/api/runs/{RUN}/advice", params={"id": oid, "lang": lang}).json()["why"] == d["why"]


def test_route_errors_and_item_briefs_carry_the_answer(client):
    assert client.get(f"/api/runs/{RUN}/advice", params={"kind": "nope", "id": "CHK-000001"}).status_code == 400
    assert client.get(f"/api/runs/{RUN}/advice", params={"kind": "check", "id": "CHK-999999"}).status_code == 404
    assert client.get(f"/api/runs/{RUN}/advice").status_code == 422
    assert client.get("/api/runs/no_such_run/advice", params={"id": "CHK-000001"}).status_code == 404
    chk = _first(client, "checks", status="fail")
    it = client.get(f"/api/runs/{RUN}/brief/item", params={"id": chk["check_id"]}).json()
    adv = client.get(f"/api/runs/{RUN}/advice", params={"id": chk["check_id"]}).json()
    assert it["why"] == adv["why"] and it["fix"] == adv["fix"] and it["can_use_rows"] == adv["can_use_rows"]
    for oid in (_first(client, "flags")["id"], _first(client, "diagnoses")["id"]):
        it = client.get(f"/api/runs/{RUN}/brief/item", params={"id": oid, "lang": "fi"}).json()
        assert it["why"] and len(it["fix"]) >= 2
    # names from the person's file, not bare short names
    assert not re.search(r"(?<!\()\bS\d{2}\b(?!\))", adv["why"]), adv["why"]
