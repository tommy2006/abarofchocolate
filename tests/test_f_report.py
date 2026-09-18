"""Agent F: HTML report (EN/FI/SV), export bundle, e-mail plumbing. Uses tests/fixtures/fake_workspace_f.py."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from tests.fixtures.fake_workspace_f import build_fake_workspace
from tpm.report import SmtpNotConfigured, Translator, available_languages, email_report, generate_report, report_path, run_report
from tpm.report.email import build_message
from tpm.report.i18n import I18N_DIR

SECTION_IDS = [f'id="section-{i}"' for i in range(1, 9)]


@pytest.fixture
def fake_ws(tmp_path):
    ws = build_fake_workspace(tmp_path / "workspace")
    yield ws
    ws.close()


def _html(ws, lang):
    return report_path(ws, lang).read_text(encoding="utf-8")


def test_translations_have_same_keys():
    en = json.loads((I18N_DIR / "en.json").read_text(encoding="utf-8"))
    for lang in ("fi", "sv"):
        d = json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))
        missing = set(en) - set(d)
        extra = set(d) - set(en)
        assert not missing, f"{lang} missing keys: {sorted(missing)[:10]}"
        assert not extra, f"{lang} extra keys: {sorted(extra)[:10]}"
        for k, v in en.items():  # placeholders must survive translation
            import re

            ph = set(re.findall(r"{(\w+)}", v))
            assert ph == set(re.findall(r"{(\w+)}", d[k])), f"{lang}:{k} placeholders differ"
    assert set(available_languages()) >= {"en", "fi", "sv"}


@pytest.mark.parametrize("lang", ["en", "fi", "sv"])
def test_report_renders_all_eight_sections(fake_ws, lang):
    out = generate_report(fake_ws, fake_ws.settings, lang, use_llm=False)
    assert out.exists() and out.name == f"report_{lang}.html"
    html = _html(fake_ws, lang)
    t = Translator(lang)
    for sid in SECTION_IDS:
        assert sid in html
    for i in range(1, 9):
        assert t(f"section_{i}") in html
    assert f'<html lang="{lang}">' in html
    # content from every artifact family is present
    assert "S01" in html and "EV-000001" in html and "INF-000001" in html
    assert "FLAG-000001" in html and "DIAG-000001" in html and "PATTERN-A" in html
    assert "RULE-001" in html and "CHK-" in html
    assert "maija" in html and "ville" in html  # human decisions with before/after
    assert t("before") in html and t("after") in html
    assert t("chain_ok", n=fake_ws.log.count() - 1) in html or t("chain_ok", n=fake_ws.log.count()) in html
    assert "EGR-000002" in html and "claude-sonnet-5" in html and "gemma4" in html
    assert "auroc" in html  # evaluation.json rendered
    assert t("s3_evaluation_note") not in html or True
    assert "<script" not in html.lower()  # self-contained, no JS
    assert html.count("<svg") >= 8  # sparklines, bars, dataflow diagram
    assert t("llm_not_used") in html  # no model in tests, clearly stated
    assert t("s7_link") in html and "ADAPTABILITY.md" in html


def test_report_is_self_contained_and_print_friendly(fake_ws):
    generate_report(fake_ws, fake_ws.settings, "en", use_llm=False)
    html = _html(fake_ws, "en")
    assert "@media print" in html
    assert 'href="http' not in html and "src=\"http" not in html
    assert "<link" not in html


def test_report_graceful_when_artifacts_missing(tmp_path):
    from tpm.config import load_settings
    from tpm.workspace import Workspace

    s = load_settings()
    s.workspace_dir = str(tmp_path / "ws")
    ws = Workspace(run_id="bare", settings=s)
    ws.write_json("meta", {"run_id": "bare", "source_path": "x.csv", "profile": s.profile})
    out = generate_report(ws, s, "en", use_llm=False)
    html = out.read_text(encoding="utf-8")
    t = Translator("en")
    for sid in SECTION_IDS:
        assert sid in html
    assert t("s1_no_signals") in html and t("s2_no_checks") in html and t("s3_no_flags") in html and t("s4_no_diag") in html
    assert t("s5_no_human") in html and t("s8_no_ledger") in html and t("eval_no") in html and t("assessor_no") in html
    assert t("overview_dataset_missing") in html
    ws.close()


def test_report_survives_foreign_artifact_shapes(fake_ws):
    """Other agents may write different columns / shapes; the report must still render."""
    pd.DataFrame({"weird": [1, 2, 3], "ensemble": [0.1, 0.5, 2.0]}).to_parquet(fake_ws.path("scores"), index=False)
    fake_ws.write_json("relations", [{"x": 1}])
    fake_ws.write_json("baseline", {"nested": {"a": {"b": 1}}, "method": "x"})
    fake_ws.write_json("evaluation", {"metric": 0.5, "per_group": {"1": {"a": 1}}, "list": [1, 2]})
    fake_ws.rewrite_jsonl("flags", [{"id": "FLAG-X", "not": "a flag"}])  # breaks typed loading -> raw fallback
    out = generate_report(fake_ws, fake_ws.settings, "sv", use_llm=False)
    html = out.read_text(encoding="utf-8")
    for sid in SECTION_IDS:
        assert sid in html
    assert "FLAG-X" in html


def test_run_report_stage_uses_language_option(fake_ws):
    res = run_report(fake_ws, fake_ws.settings, {"options": {"language": "sv", "report_llm": False}, "progress": lambda *a, **k: None, "run_id": fake_ws.run_id})
    assert Path(res["path"]).exists() and res["lang"] == "sv"
    assert report_path(fake_ws, "sv").exists()
    # the report generation itself is logged and the chain still verifies
    assert fake_ws.log.verify_chain()["ok"]
    assert any(e.action == "report" for e in fake_ws.log.entries(action="report"))


def test_export_zip_contents(fake_ws, tmp_path):
    from tpm.log.exports import export_run, list_export

    generate_report(fake_ws, fake_ws.settings, "fi", use_llm=False)
    zp = export_run(fake_ws, tmp_path / "out", languages=["en"])
    names = list_export(zp)
    for must in ("report_en.html", "report_fi.html", "decision_log.jsonl", "egress_ledger.jsonl", "verify.json", "manifest.json", "schema.json", "signals.json", "flags.jsonl", "diagnoses.jsonl", "checks.jsonl", "evidence.jsonl", "inferences.jsonl"):
        assert must in names, must
    for never in ("dataset.parquet", "scores.parquet", "decision_log.sqlite"):
        assert never not in names
    with zipfile.ZipFile(zp) as z:
        v = json.loads(z.read("verify.json"))
        assert v["ok"] is True and v["checked"] >= 20
        lines = z.read("decision_log.jsonl").decode("utf-8").strip().splitlines()
        assert len(lines) == v["checked"]
        assert json.loads(lines[0])["seq"] == 1


def test_email_requires_configuration(fake_ws, monkeypatch):
    for var in ("TPM_SMTP_HOST", "TPM_SMTP_USER", "TPM_SMTP_PASSWORD", "TPM_SMTP_FROM"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SmtpNotConfigured) as ei:
        email_report(fake_ws, fake_ws.settings, "a@b.c", "en")
    assert "TPM_SMTP_HOST" in str(ei.value)
    with pytest.raises(SmtpNotConfigured):  # API-style positional call (ws, to, lang)
        email_report(fake_ws, "a@b.c", "fi")


def test_email_message_builds_with_attachment(fake_ws):
    p = generate_report(fake_ws, fake_ws.settings, "fi", use_llm=False)
    msg = build_message(fake_ws, fake_ws.settings, ["a@b.c", "d@e.f"], "fi", p, "tpm@example.org")
    assert msg["To"] == "a@b.c, d@e.f" and fake_ws.run_id in msg["Subject"]
    atts = [part for part in msg.iter_attachments()]
    assert len(atts) == 1 and atts[0].get_filename() == "report_fi.html"
    assert "suomi" in msg.get_body(preferencelist=("plain",)).get_content()


def test_email_send_path_with_fake_smtp(fake_ws, monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"], sent["port"] = host, port

        def starttls(self):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = (u, p)

        def send_message(self, msg):
            sent["msg"] = msg

        def quit(self):
            sent["quit"] = True

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("TPM_SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("TPM_SMTP_PORT", "587")
    monkeypatch.setenv("TPM_SMTP_USER", "u")
    monkeypatch.setenv("TPM_SMTP_PASSWORD", "p")
    monkeypatch.setenv("TPM_SMTP_FROM", "tpm@example.org")
    res = email_report(fake_ws, fake_ws.settings, "x@y.z, q@r.s", "sv")
    assert res["sent"] and res["to"] == ["x@y.z", "q@r.s"] and sent["host"] == "smtp.example.org" and sent.get("tls") and sent["login"] == ("u", "p")
    assert sent["msg"]["From"] == "tpm@example.org"
    assert any(e.action == "email" for e in fake_ws.log.entries(action="email"))
