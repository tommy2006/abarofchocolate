"""Agent F: PDF export of the run report (tpm/report/pdf.py). Pure-Python path only (ReportLab); text is read back
with pypdf. Uses tests/fixtures/fake_workspace_f.py; a smoke run on workspace/fix_check when that run exists."""
from __future__ import annotations

import io
import re
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("reportlab")
pypdf = pytest.importorskip("pypdf")

from tests.fixtures.fake_workspace_f import build_fake_workspace  # noqa: E402
from tpm.report import Translator  # noqa: E402
from tpm.report import report as report_mod  # noqa: E402
from tpm.report.pdf import _safe, generate_pdf  # noqa: E402
from tpm.report.prose import parse_narrative  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _text(path: Path) -> tuple[str, int]:
    reader = pypdf.PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages), len(reader.pages)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s)


@pytest.fixture(scope="module")
def fake_ws(tmp_path_factory):
    ws = build_fake_workspace(tmp_path_factory.mktemp("f_pdf") / "workspace")
    yield ws
    ws.close()


@pytest.fixture(scope="module")
def pdf_en(fake_ws, tmp_path_factory):
    out = generate_pdf(fake_ws, fake_ws.settings, "en")
    text, pages = _text(out)
    return out, _norm(text), pages


def test_pdf_is_produced_with_all_sections(fake_ws, pdf_en):
    out, text, pages = pdf_en
    assert out.exists() and out.name == "report_en.pdf" and out.parent == fake_ws.dir
    assert out.read_bytes()[:5] == b"%PDF-" and out.stat().st_size > 40_000
    assert pages >= 6
    t = Translator("en")
    for i in range(1, 9):
        assert text.count(t(f"section_{i}")) >= 2, f"section {i}: expected in the contents page and as a heading"
    for key in ("toc", "section_overview", "section_assessor", "section_eval", "section_appendix", "pdf_headline_numbers", "section_stages"):
        assert t(key) in text, key
    # title page facts, running header / footer with the run id and "page x of y"
    assert fake_ws.run_id in text and "fake_source.csv" in text and fake_ws.settings.local_llm.model in text
    assert text.count(f"{t('run_id')}: {fake_ws.run_id}") >= pages - 1
    assert re.search(rf"{t('pdf_page')} 2 {t('pdf_of')} ?{pages}\b", text), "page x of y (the total is a form defined at save time)"
    # content of every artifact family
    for needle in ("S01", "EV-000001", "INF-000001", "FLAG-000001", "DIAG-000001", "PATTERN-A", "RULE-001", "CHK-", "EGR-000002", "maija", "ville", "claude-sonnet-5", "auroc"):
        assert needle in text, needle
    assert t("chain_ok", n=fake_ws.log.count()) in text or t("chain_ok", n=fake_ws.log.count() - 1) in text
    assert t("llm_not_used") in text and t("section_llm") not in text  # no model in tests, and that is stated
    assert t("pdf_no_raw") in text


def test_pdf_contents_page_points_at_the_sections(pdf_en):
    out, _, pages = pdf_en
    reader = pypdf.PdfReader(str(out))
    toc = _norm(reader.pages[1].extract_text() or "")
    t = Translator("en")
    assert t("toc") in toc
    found = {}
    for i in range(1, 9):
        title = t(f"section_{i}")
        assert title in toc
        heading_pages = [k + 1 for k, p in enumerate(reader.pages) if k > 1 and title in _norm(p.extract_text() or "")]
        assert heading_pages, title
        found[i] = heading_pages[0]
        assert str(found[i]) in re.findall(r"\d+", toc), f"{title}: page {found[i]} is not listed on the contents page"
    assert [found[i] for i in range(1, 9)] == sorted(found[i] for i in range(1, 9)) and found[8] <= pages
    outline = [o.title for o in reader.outline if not isinstance(o, list)]
    assert t("section_1") in outline and t("section_8") in outline  # PDF bookmarks


@pytest.mark.parametrize("lang,letter", [("fi", "ä"), ("sv", "å")])
def test_pdf_finnish_and_swedish_letters(fake_ws, lang, letter):
    out = generate_pdf(fake_ws, fake_ws.settings, lang)
    text, pages = _text(out)
    text = _norm(text)
    t = Translator(lang)
    assert pages >= 6 and out.name == f"report_{lang}.pdf"
    assert letter in text and "ö" in text
    for i in range(1, 9):
        assert t(f"section_{i}") in text
    assert t("pdf_headline_numbers") in text and t("toc") in text
    # the glyphs come from a TrueType font embedded in the file, not from whatever the viewer's machine has
    assert b"FontFile2" in out.read_bytes()
    base_fonts = set()
    for page in pypdf.PdfReader(str(out)).pages[:4]:
        for ref in (page["/Resources"].get("/Font") or {}).values():
            base_fonts.add(str(ref.get_object().get("/BaseFont")))
    assert {f.split("+")[-1] for f in base_fonts} >= {"BitstreamVeraSans-Roman", "BitstreamVeraSans-Bold"}, base_fonts


def test_pdf_has_no_raw_rows(fake_ws, pdf_en):
    _, text, _ = pdf_en
    df = pd.read_parquet(fake_ws.path("dataset")).head(400)
    for v in df["press_r"].head(200):
        assert f"{float(v):.6f}" not in text and f"{float(v):.4f}" not in text
    for ts in df["timestamp"].astype(str).head(50):
        assert ts not in text


def test_pdf_font_fallback_never_drops_text():
    assert _safe("S05 → S03 (σ = 7.4, Δ ≥ 2) ✓") == "S05 -> S03 (sigma = 7.4, delta ≥ 2) ok"
    assert _safe("päätös – ”ok” … åäö") == "päätös – ”ok” … åäö"


def test_pdf_graceful_when_artifacts_missing(tmp_path):
    from tpm.config import load_settings
    from tpm.workspace import Workspace

    s = load_settings()
    s.workspace_dir = str(tmp_path / "ws")
    ws = Workspace(run_id="bare", settings=s)
    ws.write_json("meta", {"run_id": "bare", "source_path": "x.csv", "profile": s.profile})
    out = generate_pdf(ws, s, "en")
    text, pages = _text(out)
    text = _norm(text)
    t = Translator("en")
    assert pages >= 4
    for key in ("s1_no_signals", "s2_no_checks", "s3_no_flags", "s4_no_diag", "s5_no_human", "s8_no_ledger", "assessor_no"):
        assert t(key) in text, key
    assert t("section_eval") not in text  # the evaluation block appears only when labels existed
    ws.close()


def test_pdf_large_tables_repeat_their_header_and_state_totals(tmp_path):
    ws = build_fake_workspace(tmp_path / "workspace", run_id="many_flags")
    flags = [f.model_dump() for f in ws.flags()]
    many = []
    for i in range(140):
        f = dict(flags[i % len(flags)])
        f["id"] = f"FLAG-{i + 1:06d}"
        f["severity"] = 0.2 + 0.005 * i
        many.append(f)
    ws.rewrite_jsonl("flags", many)
    out = generate_pdf(ws, ws.settings, "en")
    reader = pypdf.PdfReader(str(out))
    t = Translator("en")
    header_pages = [k for k, p in enumerate(reader.pages) if t("responsible_signals") in _norm(p.extract_text() or "")]
    assert len(header_pages) >= 3, "the flags table spans several pages and repeats its header row on each"
    text = _norm("\n".join(p.extract_text() or "" for p in reader.pages))
    assert t("capped_flags", shown=60, total="140") in text  # capped like the HTML report, with the total
    assert "FLAG-000140" in text and "FLAG-000081" in text and "FLAG-000080" not in text  # the 60 most severe are kept
    ws.close()


def test_pdf_survives_very_long_texts(fake_ws):
    """No statement, step or evidence text can make a table row or a box taller than a page."""
    from tpm.report.export_common import export_context
    from tpm.report.pdf import render_pdf

    ctx = export_context(fake_ws, fake_ws.settings, "en")
    wall = "Averyveryverylongtokenwithoutanyspaces" * 120
    prose = "The pressure signal rose well above its usual band and stayed there for the rest of the group. " * 150
    for f in ctx["detect"]["flags"]:
        f["statement"], f["human_note"], f["signals"] = prose, wall, [wall] * 5
        f["ev"] = [{"id": "EV-000001", "plain": prose, "technical": wall}]
    for d in ctx["diagnoses"]:
        d["summary"], d["steps"], d["uncertainty"], d["fault_type"] = prose, [prose] * 12, [prose] * 6, wall
        if d.get("critique"):
            d["critique"]["objections"] = [prose] * 6
    for c in ctx["quality"]["failed"]:
        c["statement"] = prose
    for s in ctx["signals"]:
        s["evidence"] = [{"id": "EV-000002", "plain": prose, "technical": wall}] * 3
    ctx["overview"] = [prose] * 5
    ctx["dataflow"]["statement"] = prose
    data = render_pdf(ctx)
    reader = pypdf.PdfReader(io.BytesIO(data))
    assert data[:5] == b"%PDF-" and 8 <= len(reader.pages) <= 80


def test_pdf_model_summary_only_when_already_stored(tmp_path, monkeypatch):
    monkeypatch.delenv("TPM_REPORT_LLM", raising=False)
    ws = build_fake_workspace(tmp_path / "workspace", run_id="with_summary")

    def boom(*a, **k):
        raise AssertionError("the export must never call the language model")

    monkeypatch.setattr("tpm.llm.complete", boom)
    t = Translator("en")
    text, _ = _text(generate_pdf(ws, ws.settings, "en"))
    assert t("section_llm") not in _norm(text)
    narrative = parse_narrative({"executive_summary": "Two groups need attention today. The strongest event is FLAG-000001.", "sections": [{"heading": "What to look at first", "body": "Group 3 drifted from sample 80 onwards.", "evidence_ids": ["FLAG-000001"]}], "uncertainty": ["No normal data was provided."]})
    report_mod._store_narrative(ws, "en", report_mod.narrative_fingerprint(ws), {"status": "ok", "narrative": narrative, "source": "llm-local:test-model", "model": "test-model", "route": "local"})
    text = _norm(_text(generate_pdf(ws, ws.settings, "en"))[0])
    assert t("section_llm") in text and "Two groups need attention today." in text and "What to look at first" in text
    assert "llm-local:test-model" in text and "reading aid" in text  # clearly labelled as model-written
    ws.close()


@pytest.mark.skipif(not (ROOT / "workspace" / "fix_check" / "status.json").exists(), reason="workspace/fix_check is not present")
def test_pdf_smoke_on_a_real_run(tmp_path):
    from tpm.config import load_settings
    from tpm.workspace import Workspace

    s = load_settings()
    s.workspace_dir = str(ROOT / "workspace")
    ws = Workspace.open("fix_check", s)
    try:
        out = generate_pdf(ws, s, "en", out_path=tmp_path / "fix_check.pdf", log=False)  # nothing is written into the run
    finally:
        ws.close()
    text, pages = _text(out)
    assert pages >= 10 and out.stat().st_size > 60_000
    t = Translator("en")
    for i in range(1, 9):
        assert t(f"section_{i}") in _norm(text)
