"""Agent F, review round: model summary as prose, language switching (cache / no waiting on the model / concurrency),
large-run row caps, short detector labels, plain-language evidence, assessor wording. No live model is used."""
from __future__ import annotations

import json
import re
import threading
import time

import pytest

from tests.fixtures.fake_workspace_f import build_fake_workspace
from tpm.contracts import LLMResult
from tpm.report import Translator, ensure_report, generate_report, report_path, report_status
from tpm.report import report as report_mod
from tpm.report.prose import clean_text, detector_label, parse_narrative, strip_lead, whole_sentences

LONG_DETECTOR = "ensemble:autoencoder+pca+robust_z+iforest+ewma+corr_break+cusum+resid_spread (summary)"

NARRATIVE = {
    "title": "Data reliability report",
    "executive_summary": "Two groups need attention today. The strongest event is FLAG-000001, which the checks attribute to a sensor.",
    "sections": [
        {"heading": "What to look at first", "body": "Group 3 drifted from sample 80 onwards (FLAG-000001). The diagnosis DIAG-000001 points at one instrument rather than the process.", "evidence_ids": ["FLAG-000001", "DIAG-000001", "EV-999999"]},
        {"heading": "Data quality", "body": "Most batches passed the baseline checks.\n\nOne batch was marked untrusted.", "evidence_ids": ["EV-000001"]},
    ],
    "uncertainty": ["No normal data was provided, so the baseline is an estimate.", "Labels were not used."],
    "confidence": 0.8,
}


@pytest.fixture
def fake_ws(tmp_path, monkeypatch):
    monkeypatch.delenv("TPM_REPORT_LLM", raising=False)
    ws = build_fake_workspace(tmp_path / "workspace")
    yield ws
    ws.close()


def _fake_complete(result=None, delay=0.0, calls=None):
    def complete(task, payload, **kw):
        if calls is not None:
            calls.append({"task": task, "payload": payload, **kw})
        if delay:
            time.sleep(delay)
        return result if result is not None else LLMResult(text=json.dumps(NARRATIVE), data=dict(NARRATIVE), source="llm-local:test-model", model="test-model", route="local", ok=True)

    return complete


def _html(ws, lang="en"):
    return report_path(ws, lang).read_text(encoding="utf-8")


def _llm_block(html):
    m = re.search(r'<div class="llm">(.*?)</div>', html, re.S)
    return m.group(1) if m else None


# ----------------------------------------------------------------------------- bug 2: model summary as prose
def test_model_summary_is_rendered_as_prose(fake_ws, monkeypatch):
    calls = []
    monkeypatch.setattr("tpm.llm.complete", _fake_complete(calls=calls))
    generate_report(fake_ws, fake_ws.settings, "en", use_llm=True, llm_wait_s=20)
    html = _html(fake_ws)
    block = _llm_block(html)
    assert block, "model summary section missing"
    assert "executive_summary" not in html and "evidence_ids" not in block and "{" not in block and "}" not in block and "&#34;" not in block
    assert '<p class="lead">Two groups need attention today. The strongest event is FLAG-000001, which the checks attribute to a sensor.</p>' in block
    assert "<h4>What to look at first</h4>" in block and "<h4>Data quality</h4>" in block
    assert "<p>Most batches passed the baseline checks.</p>" in block and "<p>One batch was marked untrusted.</p>" in block  # paragraphs
    t = Translator("en")
    assert t("llm_uncertainty") in block and "No normal data was provided, so the baseline is an estimate." in block
    assert 'href="#FLAG-000001"' in block and 'id="FLAG-000001"' in html  # references link to the row in this report
    assert "EV-999999" not in block  # an id that does not exist in the run is not shown as a reference
    assert "llm-local:test-model" in block and t("llm_label", source="llm-local:test-model") in html  # labelled with its source
    assert t("llm_not_used") not in html
    # the request: enough tokens, language passed, whitelisted payload keys only, no raw rows
    assert calls and calls[0]["task"] == "report_narrative" and calls[0]["max_tokens"] >= 1200 and calls[0]["language"] == "en"
    assert set(calls[0]["payload"]) <= {"language", "instructions", "report_sections", "flags", "diagnoses"}
    # cached per language: a second generation does not call the model again
    generate_report(fake_ws, fake_ws.settings, "en", use_llm=True, llm_wait_s=20)
    assert len(calls) == 1 and _llm_block(_html(fake_ws))


def test_payload_states_totals_and_regenerate_asks_the_model_again(fake_ws, monkeypatch):
    calls = []
    monkeypatch.setattr("tpm.llm.complete", _fake_complete(calls=calls))
    generate_report(fake_ws, fake_ws.settings, "en", use_llm=True, llm_wait_s=20)
    payload = calls[0]["payload"]
    n_flags, n_diags = len(fake_ws.read_jsonl("flags")), len(fake_ws.read_jsonl("diagnoses"))
    # the lists are a sample (at most 8); the totals are stated so the model cannot mistake one for the other
    assert payload["report_sections"]["n_flags_total"] == n_flags and payload["report_sections"]["n_diagnoses_total"] == n_diags
    assert len(payload["flags"]) <= 8 and len(payload["diagnoses"]) <= 8 and "totals" in payload["instructions"]
    for f in payload["flags"]:
        assert f["severity"] is None or len(str(f["severity"]).split(".")[-1]) <= 3  # no 16-digit floats for the model to copy
    # cached: an ordinary view does not ask again; "Regenerate" (force) does
    assert not ensure_report(fake_ws, fake_ws.settings, "en")["regenerated"] and len(calls) == 1
    st = ensure_report(fake_ws, fake_ws.settings, "en", force=True)
    assert st["regenerated"] and st["llm"] == "pending"
    deadline = time.time() + 20
    while time.time() < deadline and ensure_report(fake_ws, fake_ws.settings, "en")["llm"] == "pending":
        time.sleep(0.1)
    assert len(calls) == 2 and ensure_report(fake_ws, fake_ws.settings, "en")["llm"] == "ready"
    # placeholder "citations" a model invents are removed, real ids stay
    n = parse_narrative({"executive_summary": "635 checks ran (CHK-...). See FLAG-000001.", "sections": []})
    assert n["summary"] == ["635 checks ran. See FLAG-000001."]


def test_model_summary_from_text_only_fenced_json(fake_ws, monkeypatch):
    res = LLMResult(text="```json\n" + json.dumps(NARRATIVE, indent=2) + "\n```", data=None, source="llm-local:test-model", ok=True)
    monkeypatch.setattr("tpm.llm.complete", _fake_complete(res))
    generate_report(fake_ws, fake_ws.settings, "en", use_llm=True, llm_wait_s=20)
    block = _llm_block(_html(fake_ws))
    assert block and "Two groups need attention today." in block and "{" not in block and "```" not in block


def test_truncated_reply_shows_whole_sentences_only(fake_ws, monkeypatch):
    cut = json.dumps(NARRATIVE)
    cut = cut[: cut.index("rather than the process") + 6]  # cut mid-sentence inside the first section body
    res = LLMResult(text=cut, data=None, source="llm-local:test-model", ok=False, error="model returned no parseable JSON")
    monkeypatch.setattr("tpm.llm.complete", _fake_complete(res))
    generate_report(fake_ws, fake_ws.settings, "en", use_llm=True, llm_wait_s=20)
    block = _llm_block(_html(fake_ws))
    assert block and "Two groups need attention today." in block
    assert "Group 3 drifted from sample 80 onwards (FLAG-000001)." in block  # the whole sentence before the cut
    assert "points at one instrument" not in block and "{" not in block and '"' not in re.sub(r"<[^>]+>", "", block)
    assert Translator("en")("llm_truncated") in block


@pytest.mark.parametrize("text", ['{"foo": [1, 2', '{"title": "x", "sections": []}', "", '[{"a": 1}]'])
def test_unreadable_reply_omits_the_model_section(fake_ws, monkeypatch, text):
    monkeypatch.setattr("tpm.llm.complete", _fake_complete(LLMResult(text=text, data=None, source="llm-local:test-model", ok=bool(text))))
    generate_report(fake_ws, fake_ws.settings, "en", use_llm=True, llm_wait_s=20)
    html = _html(fake_ws)
    assert _llm_block(html) is None and Translator("en")("llm_not_used") in html
    assert '"foo"' not in html and "&#34;foo&#34;" not in html and "&#34;title&#34;" not in html


def test_parse_narrative_variants():
    n = parse_narrative(NARRATIVE, "", known_ids={"FLAG-000001"})
    assert n["summary"] and [s["heading"] for s in n["sections"]] == ["What to look at first", "Data quality"]
    assert n["sections"][0]["refs"] == ["FLAG-000001"] and n["sections"][1]["paragraphs"] == ["Most batches passed the baseline checks.", "One batch was marked untrusted."]
    assert n["confidence"] == 0.8 and not n["truncated"]
    assert parse_narrative(None, "The run looks healthy. Nothing needs attention.")["summary"] == ["The run looks healthy. Nothing needs attention."]
    assert parse_narrative({"result": NARRATIVE}, "")["summary"] == n["summary"]  # router wrapper
    assert parse_narrative(None, "") is None and parse_narrative({}, '{"x": 1}') is None
    # payload keys "cited" by a model are dropped, sentences stay
    assert parse_narrative({"executive_summary": "26 checks failed (overview, quality). See FLAG-000001.", "sections": []})["summary"] == ["26 checks failed. See FLAG-000001."]


def test_text_helpers():
    assert whole_sentences("First sentence. Second one is longer than the limit allows.", 30) == "First sentence."
    assert whole_sentences("Score 7.2x threshold (peak 8.4x). Leading signals: S44 (12%, stu", require_end=True) == "Score 7.2x threshold (peak 8.4x)."
    assert whole_sentences("no sentence end here", require_end=True) == ""
    raw = "Sensor fault on S44. Confidence 88%.\n\n[llm-local:gemma] {\"summary\": \"A sensor fault was detected on S44.\", \"steps\": [\"1. x\"]}"
    assert clean_text(raw) == "Sensor fault on S44. Confidence 88%.\n\nA sensor fault was detected on S44."
    assert clean_text("[llm-local:m] {'text': \"The claim is weak because 'S44' is not alone.\", 'severity': 0.7}") == "The claim is weak because 'S44' is not alone."
    assert strip_lead("Yes: 5 exact duplicate rows were found.") == "5 exact duplicate rows were found."
    assert strip_lead("No data-quality issue was found.") == "No data-quality issue was found."


# ----------------------------------------------------------------------------- bug 4: languages, cache, no waiting
@pytest.mark.parametrize("lang", ["fi", "sv"])
def test_fi_sv_generation_with_model_summary(fake_ws, monkeypatch, lang):
    calls = []
    monkeypatch.setattr("tpm.llm.complete", _fake_complete(calls=calls))
    out = generate_report(fake_ws, fake_ws.settings, lang, use_llm=True, llm_wait_s=20)
    html = out.read_text(encoding="utf-8")
    t = Translator(lang)
    assert f'<html lang="{lang}">' in html and t("section_llm") in html and t("llm_uncertainty") in html and _llm_block(html)
    for i in range(1, 9):
        assert f'id="section-{i}"' in html and t(f"section_{i}") in html
    assert calls[0]["language"] == lang and lang in calls[0]["payload"]["language"]
    assert ("Finnish" if lang == "fi" else "Swedish") in calls[0]["payload"]["instructions"]
    st = report_status(fake_ws, lang)
    assert st["exists"] and st["fresh"] and st["llm"] == "ready"


def test_ensure_report_is_cached_per_language_and_invalidated_by_artifacts(fake_ws):
    a = ensure_report(fake_ws, fake_ws.settings, "fi", use_llm=False)
    assert a["regenerated"] and a["exists"] and a["fresh"] and a["llm"] == "none"
    b = ensure_report(fake_ws, fake_ws.settings, "fi", use_llm=False)
    assert not b["regenerated"] and b["fresh"]
    c = ensure_report(fake_ws, fake_ws.settings, "sv", use_llm=False)  # another language has its own cache entry
    assert c["regenerated"] and report_path(fake_ws, "sv").exists()
    flags = fake_ws.read_jsonl("flags")
    flags[0]["statement"] = "CHANGED-STATEMENT for the cache test."
    time.sleep(0.02)
    fake_ws.rewrite_jsonl("flags", flags)
    assert not report_status(fake_ws, "fi")["fresh"]
    d = ensure_report(fake_ws, fake_ws.settings, "fi", use_llm=False)
    assert d["regenerated"] and "CHANGED-STATEMENT" in _html(fake_ws, "fi")
    assert ensure_report(fake_ws, fake_ws.settings, "fi", use_llm=False, force=True)["regenerated"]
    # a report written by older code (no cache stamp in its head) is regenerated as well
    p = report_path(fake_ws, "sv")
    p.write_text(re.sub(r'<meta name="tpm-report"[^>]*>\n?', "", p.read_text(encoding="utf-8")), encoding="utf-8")
    assert ensure_report(fake_ws, fake_ws.settings, "sv", use_llm=False)["regenerated"]


def test_ensure_report_never_waits_for_the_model(fake_ws, monkeypatch):
    calls = []
    monkeypatch.setattr("tpm.llm.complete", _fake_complete(delay=1.5, calls=calls))
    t0 = time.time()
    st = ensure_report(fake_ws, fake_ws.settings, "sv")
    assert time.time() - t0 < 1.4, "the template report must be returned before the model answers"
    assert st["exists"] and st["llm"] == "pending"
    html = _html(fake_ws, "sv")
    assert _llm_block(html) is None and Translator("sv")("llm_pending") in html and 'id="section-8"' in html
    assert ensure_report(fake_ws, fake_ws.settings, "sv")["llm"] == "pending" and len(calls) == 1  # polling starts no second call
    deadline = time.time() + 20
    while time.time() < deadline and ensure_report(fake_ws, fake_ws.settings, "sv")["llm"] == "pending":
        time.sleep(0.1)
    st = ensure_report(fake_ws, fake_ws.settings, "sv")
    assert st["llm"] == "ready" and st["fresh"] and len(calls) == 1
    assert "Two groups need attention today." in _llm_block(_html(fake_ws, "sv"))


def test_failed_model_call_is_remembered_and_report_is_complete(fake_ws, monkeypatch):
    calls = []
    monkeypatch.setattr("tpm.llm.complete", _fake_complete(LLMResult(text="", ok=False, error="ollama not reachable"), calls=calls))
    generate_report(fake_ws, fake_ws.settings, "en", use_llm=True, llm_wait_s=20)
    html = _html(fake_ws)
    assert Translator("en")("llm_not_used") in html and 'id="section-8"' in html
    st = ensure_report(fake_ws, fake_ws.settings, "en")
    assert st["llm"] == "none" and len(calls) == 1  # no retry storm while the model is down


def test_blocking_generation_is_time_boxed(fake_ws, monkeypatch):
    monkeypatch.setattr("tpm.llm.complete", _fake_complete(delay=5.0))
    t0 = time.time()
    out = generate_report(fake_ws, fake_ws.settings, "en", use_llm=True, llm_wait_s=0.5)
    assert time.time() - t0 < 4.0 and out.exists()
    html = out.read_text(encoding="utf-8")
    assert _llm_block(html) is None and Translator("en")("llm_not_used") in html


def test_concurrent_generation_of_one_language(fake_ws):
    errors = []

    def work():
        try:
            generate_report(fake_ws, fake_ws.settings, "fi", use_llm=False)
        except Exception as e:  # pragma: no cover
            errors.append(repr(e))

    threads = [threading.Thread(target=work) for _ in range(4)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert not errors and "</html>" in _html(fake_ws, "fi")
    assert not list(fake_ws.dir.glob("report_fi.html*.tmp"))


def test_api_report_language_switch_and_status(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tpm.api.server import create_app

    monkeypatch.setenv("TPM_REPORT_LLM", "0")  # no model in this test
    ws = build_fake_workspace(tmp_path / "workspace", run_id="api_run")
    ws.close()
    with TestClient(create_app(workspace_dir=tmp_path / "workspace")) as c:
        for lang in ("en", "fi", "sv"):
            r = c.get("/api/runs/api_run/report", params={"lang": lang, "status": 1})
            assert r.status_code == 200, r.text
            d = r.json()
            assert d["ok"] and d["lang"] == lang and d["exists"] and d["llm"] in ("none", "ready") and d["regenerated"]
            r = c.get("/api/runs/api_run/report", params={"lang": lang, "embed": 1})
            assert r.status_code == 200 and f'<html lang="{lang}">' in r.text and r.headers["cache-control"] == "no-store"
            assert not c.get("/api/runs/api_run/report", params={"lang": lang, "status": 1}).json()["regenerated"]
        r = c.get("/api/runs/api_run/report", params={"lang": "fi", "download": 1})
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and "api_run_report_fi.html" in r.headers["content-disposition"]
        assert 'http-equiv="refresh"' not in r.text
        assert c.get("/api/runs/api_run/report", params={"lang": "xx"}).status_code == 200  # unknown language -> default


def test_api_pending_report_reloads_itself_only_in_its_own_tab(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tpm.api.server import create_app

    monkeypatch.delenv("TPM_REPORT_LLM", raising=False)
    monkeypatch.setattr("tpm.llm.complete", _fake_complete(delay=2.0))
    ws = build_fake_workspace(tmp_path / "workspace", run_id="api_run2")
    ws.close()
    with TestClient(create_app(workspace_dir=tmp_path / "workspace")) as c:
        t0 = time.time()
        d = c.get("/api/runs/api_run2/report", params={"lang": "sv", "status": 1}).json()
        assert time.time() - t0 < 1.9 and d["llm"] == "pending"
        own_tab = c.get("/api/runs/api_run2/report", params={"lang": "sv"})
        assert own_tab.headers["x-tpm-report-llm"] == "pending" and 'http-equiv="refresh"' in own_tab.text
        assert 'http-equiv="refresh"' not in c.get("/api/runs/api_run2/report", params={"lang": "sv", "embed": 1}).text
        assert 'http-equiv="refresh"' not in c.get("/api/runs/api_run2/report", params={"lang": "sv", "download": 1}).text
        deadline = time.time() + 20
        while time.time() < deadline and d["llm"] == "pending":
            time.sleep(0.2)
            d = c.get("/api/runs/api_run2/report", params={"lang": "sv", "status": 1}).json()
        assert d["llm"] == "ready"
        r = c.get("/api/runs/api_run2/report", params={"lang": "sv"})
        assert "Two groups need attention today." in r.text and 'http-equiv="refresh"' not in r.text


# ----------------------------------------------------------------------------- bug 4 (large runs) and bug 3 (detector)
def test_large_run_rows_are_capped_by_severity_with_totals(fake_ws):
    base_f = fake_ws.read_jsonl("flags")[0]
    base_d = fake_ws.read_jsonl("diagnoses")[0]
    n_flags, n_diags = 1000, 250
    flags, diags = [], []
    for i in range(n_flags):
        flags.append({**base_f, "id": f"FLAG-{i + 1:06d}", "severity": round(i / n_flags, 4), "detector": LONG_DETECTOR, "group_id": f"G{i % 40:04d}"})
    for i in range(n_diags):
        diags.append({**base_d, "id": f"DIAG-{i + 1:06d}", "flag_ids": [f"FLAG-{(i + 1) * 4:06d}"], "group_id": f"G{i % 40:04d}"})
    fake_ws.rewrite_jsonl("flags", flags)
    fake_ws.rewrite_jsonl("diagnoses", diags)
    out = generate_report(fake_ws, fake_ws.settings, "en", use_llm=False)
    html = out.read_text(encoding="utf-8")
    t = Translator("en")
    shown_flags = re.findall(r'<tr id="(FLAG-\d+)"', html)
    assert len(shown_flags) == report_mod.MAX_FLAGS == 300
    assert shown_flags[0] == "FLAG-001000" and "FLAG-000700" not in shown_flags and set(shown_flags) == {f"FLAG-{i:06d}" for i in range(701, 1001)}
    assert t("capped_flags", shown=300, total="1,000") in html
    shown_diags = re.findall(r'id="(DIAG-\d+)"', html)
    assert len(shown_diags) == report_mod.MAX_DIAGNOSES == 100 and shown_diags[0] == "DIAG-000250" and "DIAG-000150" not in shown_diags
    assert html.count('<div class="card" id="DIAG-') == report_mod.MAX_DIAG_CARDS  # full cards for the worst, compact rows for the rest
    assert t("capped_diagnoses", shown=100, total="250") in html
    assert len(html.encode("utf-8")) < 1_500_000
    st = report_status(fake_ws, "en")
    assert st["flags"] == "300/1000" and st["diagnoses"] == "100/250"


def test_detector_label_is_short_and_full_list_stays_available(fake_ws):
    flags = fake_ws.read_jsonl("flags")
    for f in flags:
        f["detector"] = LONG_DETECTOR
    fake_ws.rewrite_jsonl("flags", flags)
    for lang in ("en", "fi"):
        html = generate_report(fake_ws, fake_ws.settings, lang, use_llm=False).read_text(encoding="utf-8")
        t = Translator(lang)
        short = f"{t('detector_ensemble', n=8)} ({t('detector_summary')})"
        assert f'<span class="det" title="{LONG_DETECTOR}">{short}</span>' in html
        visible = re.sub(r'title="[^"]*"', "", html)
        assert "autoencoder+pca+robust_z" not in visible  # the unbreakable string is no longer table text
        assert "autoencoder, pca, robust_z, iforest, ewma, corr_break, cusum, resid_spread" in html  # legend, wraps
    d = detector_label(LONG_DETECTOR)
    assert d["short"] == "ensemble of 8 detectors (summary)" and len(d["parts"]) == 8 and d["full"] == LONG_DETECTOR
    assert detector_label("ensemble(pca,robust_z,corr_break)")["short"] == "ensemble of 3 detectors"
    assert detector_label("changepoints:cusum+ruptures+knee")["short"] == "change-point detection (3 methods)"
    assert detector_label("pca")["short"] == "pca" and detector_label(None)["short"] == ""


def test_tables_can_shrink_to_the_page(fake_ws):
    html = generate_report(fake_ws, fake_ws.settings, "en", use_llm=False).read_text(encoding="utf-8")
    css = html[html.index("<style>") : html.index("</style>")]
    assert "overflow-wrap: anywhere" in css and "table.fixed { table-layout: fixed; }" in css and "@media print" in css
    assert '<table class="fixed flags">' in html and "<colgroup>" in html and "<thead>" in html
    assert "white-space: pre-wrap" not in css  # the old model box printed raw text


# ----------------------------------------------------------------------------- 5: plain-language evidence; 1: assessor wording
def test_plain_evidence_sentence_first_technical_underneath(fake_ws, monkeypatch):
    monkeypatch.setattr(report_mod, "_plain_explainer", lambda: (lambda ev: f"In plain words about {ev['id']}."))
    html = generate_report(fake_ws, fake_ws.settings, "en", use_llm=False).read_text(encoding="utf-8")
    ev_id = fake_ws.read_jsonl("flags")[0]["evidence_ids"][0]
    ev = fake_ws.evidence.get(ev_id)
    plain = f"In plain words about {ev_id}."
    assert plain in html
    i = html.index(plain)
    j = html.index('<span class="tech">', i)  # the technical statement follows, smaller, right underneath
    assert j - i < len(plain) + 40 and html[j:].startswith('<span class="tech">' + ev.statement[:12])
    # a broken explainer never breaks the report
    monkeypatch.setattr(report_mod, "_plain_explainer", lambda: (lambda ev: 1 / 0))
    assert "</html>" in generate_report(fake_ws, fake_ws.settings, "en", use_llm=False).read_text(encoding="utf-8")


def test_assessor_section_has_no_redundant_yes_and_no_dict_dump(fake_ws):
    a = fake_ws.read_json("assessor")
    a.update({
        "more_data_verdict": {"would_help": True, "why": "The learning curve is still rising: stability would gain about +0.051.", "estimated_gain": 0.051},
        "less_data_verdict": {"would_help": True, "why": "Removing bad data helps: Yes: 5 exact duplicate rows (0.06%) were found in 1 batch(es).", "estimated_gain": 0.0066},
        "summary": "Combined score 0.74 (fitness 0.9, coverage 0.35, data quality 0.93). More data: yes. Less data: yes. 1 recommendation(s).",
        "recommendations": [{"id": "REC-001", "text": "Yes: 5 exact duplicate rows (0.06%) were found in 1 batch(es).", "action": {"type": "drop_duplicates", "params": {}, "source": "auto"}, "expected_effect": {"dq_scores": {"overall": {"delta": 0.0066}}}, "evidence_ids": ["EV-000001"]}],
    })
    fake_ws.write_json("assessor", a)
    html = generate_report(fake_ws, fake_ws.settings, "en", use_llm=False).read_text(encoding="utf-8")
    sec = html[html.index('<section id="assessor">') : html.index('<section id="appendix">')]
    assert "Yes:" not in sec and "More data: yes" not in sec and "&#39;type&#39;" not in sec and "{" not in sec
    assert "5 exact duplicate rows (0.06%) were found in 1 batch(es)." in sec and "drop duplicates" in sec
    assert sec.count(">yes<") == 2  # one verdict word per question


def test_assessor_source_text_strips_the_lead():
    from tpm.assessor import _strip_lead, _template_answer

    assert _strip_lead("Yes: the learning curve is still rising.") == "The learning curve is still rising."
    assert _strip_lead("No: keeping 50% of the data would lower stability.") == "Keeping 50% of the data would lower stability."
    assert _strip_lead("No data-quality issue was found on S03.") == "No data-quality issue was found on S03."
    ans = _template_answer("drop duplicates?", {"type": "drop_duplicates", "params": {}}, {"recommendation": "recommend", "rationale": "Yes: 5 exact duplicate rows were found.", "expected_effect": {}}, {})
    assert ans.startswith("Yes. 5 exact duplicate rows were found.") and "Yes: " not in ans
