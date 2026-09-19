"""Agent F: PowerPoint export of the run report (tpm/report/pptx_export.py): slide count, native charts and tables,
speaker notes with evidence ids, and the rule that no text frame may hold more text than fits into it."""
from __future__ import annotations

import io
import re
from pathlib import Path

import pandas as pd
import pytest

pptx = pytest.importorskip("pptx")

from pptx import Presentation  # noqa: E402
from pptx.util import Emu  # noqa: E402

from tests.fixtures.fake_workspace_f import build_fake_workspace  # noqa: E402
from tpm.report import Translator  # noqa: E402
from tpm.report.export_common import ELLIPSIS, export_context, fit_text  # noqa: E402
from tpm.report.pptx_export import cell_capacity, fit_paragraphs, generate_pptx, render_pptx, text_budget  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ID_RE = re.compile(r"\b(?:EV|FLAG|DIAG|CHK|PATTERN|EGR)-[A-Z0-9]+\b")


@pytest.fixture(scope="module")
def fake_ws(tmp_path_factory):
    ws = build_fake_workspace(tmp_path_factory.mktemp("f_pptx") / "workspace")
    yield ws
    ws.close()


@pytest.fixture(scope="module")
def deck_en(fake_ws):
    out = generate_pptx(fake_ws, fake_ws.settings, "en")
    return out, Presentation(str(out))


def _frames(prs):
    for n, slide in enumerate(prs.slides, start=1):
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                yield n, shape


def _font_pt(shape) -> float:
    sizes = [r.font.size.pt for p in shape.text_frame.paragraphs for r in p.runs if r.font.size is not None]
    return min(sizes) if sizes else 12.0


def assert_every_text_fits(prs) -> int:
    """Two checks per text frame: the budget the generator wrote into the shape name, and an independent estimate
    from the frame's geometry and font size (0.42 em per character, 1.15 line height: more generous than the
    generator's own 0.45 / 1.2, so a pass here means the generator kept its margin)."""
    checked = 0
    for n, shape in _frames(prs):
        text = shape.text_frame.text
        m = re.search(r"budget=(\d+)", shape.name)
        assert m, f"slide {n}: text frame {shape.name!r} was not created through the budgeted text helper"
        assert len(text) <= int(m.group(1)), f"slide {n} {shape.name}: {len(text)} characters in a budget of {m.group(1)}"
        size = _font_pt(shape)
        w_pt, h_pt = Emu(shape.width).pt - 14.4, Emu(shape.height).pt - 5.0
        lines = max(1, int(h_pt / (size * 1.15)))
        cpl = max(1, int(w_pt / (size * 0.42)))
        need = sum(max(1, -(-len(p.text) // cpl)) for p in shape.text_frame.paragraphs if p.text)
        assert need <= lines, f"slide {n} {shape.name}: needs {need} lines, the frame holds {lines} ({text[:60]!r})"
        checked += 1
    for n, slide in enumerate(prs.slides, start=1):
        for shape in slide.shapes:
            if not getattr(shape, "has_table", False) or not shape.has_table:
                continue
            tbl = shape.table
            for i, row in enumerate(tbl.rows):
                for j, cell in enumerate(row.cells):
                    size = next((r.font.size.pt for p in cell.text_frame.paragraphs for r in p.runs if r.font.size), 11.0)
                    cap = cell_capacity(Emu(tbl.columns[j].width).inches, Emu(row.height).inches, size)
                    assert len(cell.text_frame.text) <= cap, f"slide {n} table cell ({i},{j}): {len(cell.text_frame.text)} > {cap}"
                    checked += 1
            assert Emu(shape.top + shape.height).inches <= 7.0, f"slide {n}: a table runs into the footer"
    return checked


def test_deck_structure(fake_ws, deck_en):
    out, prs = deck_en
    assert out.name == "report_en.pptx" and out.parent == fake_ws.dir and out.stat().st_size > 60_000
    assert 10 <= len(prs.slides) <= 16
    assert abs(Emu(prs.slide_width).inches / Emu(prs.slide_height).inches - 16 / 9) < 0.01
    charts = [s for sl in prs.slides for s in sl.shapes if getattr(s, "has_chart", False) and s.has_chart]
    tables = [s for sl in prs.slides for s in sl.shapes if getattr(s, "has_table", False) and s.has_table]
    pictures = [s for sl in prs.slides for s in sl.shapes if s.shape_type == 13]
    assert len(charts) >= 5 and len(tables) >= 3 and not pictures  # native and editable, no screenshots
    kinds = {c.chart.chart_type for c in charts}
    assert len(kinds) >= 3  # bars, stacked bars, line (score timeline with threshold), xy (learning curve)
    line = next(c.chart for c in charts if "LINE" in str(c.chart.chart_type) and "XY" not in str(c.chart.chart_type))
    t = Translator("en")
    assert t("threshold") in [s.name for s in line.plots[0].series]
    assert all(c.chart.part.chart_workbook.xlsx_part is not None for c in charts)  # the data travels with the chart
    titles = [next((s.text_frame.text for s in sl.shapes if s.name.startswith("tpm-title")), "") for sl in prs.slides]
    for key in ("px_summary", "px_analysed", "px_sensors", "px_trust", "px_monitoring", "px_patterns", "px_human", "px_dataflow", "px_assessor", "px_evaluation", "px_next"):
        assert t(key) in titles, key
    assert sum(1 for sl in prs.slides if any(s.name.startswith("tpm-kicker") and "DIAG-" in s.text_frame.text for s in sl.shapes)) == 3


def test_every_text_frame_fits_its_budget(deck_en):
    _, prs = deck_en
    assert assert_every_text_fits(prs) > 80


def test_notes_carry_evidence_ids_and_footer_has_run_id(fake_ws, deck_en):
    _, prs = deck_en
    notes = [sl.notes_slide.notes_text_frame.text for sl in prs.slides]
    assert all(n.strip() for n in notes)
    with_ids = [n for n in notes if ID_RE.search(n)]
    assert len(with_ids) >= 7
    assert any("DIAG-000001" in n and "FLAG-000001" in n and "EV-" in n for n in notes)
    assert any("EGR-000002" in n for n in notes) and any("PATTERN-A" in n for n in notes) and any("CHK-" in n for n in notes)
    known = {e.id for e in fake_ws.evidence.all()} | {f.id for f in fake_ws.flags()} | {d.id for d in fake_ws.diagnoses()}
    cited = {i for n in notes for i in ID_RE.findall(n) if i.startswith(("EV-", "FLAG-", "DIAG-"))}
    assert cited and cited <= known  # only ids that exist in the run
    for k, sl in enumerate(prs.slides, start=1):
        if k == 1:
            continue
        footer = [s for s in sl.shapes if s.name.startswith("tpm-footer")]
        assert footer and fake_ws.run_id in footer[0].text_frame.text
        num = [s for s in sl.shapes if s.name.startswith("tpm-slide-number")]
        assert num and 'type="slidenum"' in num[0]._element.xml  # a real slide-number field


def test_long_texts_are_cut_at_sentence_boundaries(fake_ws):
    ctx = export_context(fake_ws, fake_ws.settings, "en")
    long_sentence = "The pressure signal rose well above its usual band and stayed there for the rest of the group. "
    for d in ctx["diagnoses"]:
        d["summary"] = long_sentence * 40
        d["steps"] = [long_sentence * 6 for _ in range(9)]
        d["uncertainty"] = [long_sentence * 8 for _ in range(4)]
        d["fault_type"] = "an unusually long fault type name that would never fit into a slide title " * 3
    ctx["overview"] = [long_sentence * 10 for _ in range(7)]
    for c in ctx["quality"]["failed"]:
        c["statement"] = long_sentence * 12
    for p in ctx["detect"]["patterns"]:
        p["description"] = long_sentence * 12
    prs = Presentation(io.BytesIO(render_pptx(ctx)))
    assert 10 <= len(prs.slides) <= 16
    assert_every_text_fits(prs)
    t = Translator("en")
    diag = next(sl for sl in prs.slides if any(s.name.startswith("tpm-kicker") and "DIAG-" in s.text_frame.text for s in sl.shapes))
    body = [p.text for s in diag.shapes if s.name.startswith("tpm-body") for p in s.text_frame.paragraphs if p.text]
    cut = [x for x in body if x.endswith(ELLIPSIS)]
    assert cut and all(x.endswith(f". {ELLIPSIS}") for x in cut)  # whole sentences, then the ellipsis
    assert any(s.name.startswith("tpm-note") and s.text_frame.text == t("px_shortened") for s in diag.shapes)
    assert t("px_shortened") in diag.notes_slide.notes_text_frame.text


def test_fit_helpers():
    text, cut = fit_text("First sentence is here. Second sentence is a good deal longer than the first one. Third.", 60)
    assert cut and text == f"First sentence is here. {ELLIPSIS}"
    text, cut = fit_text("one single very long clause without any full stop that simply keeps going on and on", 40)
    assert cut and text.endswith(ELLIPSIS) and len(text) <= 40 and " " in text
    assert fit_text("short", 40) == ("short", False)
    paras, cut = fit_paragraphs([{"text": "a" * 50, "size": 12}, {"text": "word " * 200, "size": 12}, {"text": "never shown", "size": 12}], 4.0, 1.0)
    assert cut and len(paras) == 2 and paras[1]["text"].endswith(ELLIPSIS)
    assert sum(len(p["text"]) for p in paras) <= text_budget(4.0, 1.0, 12)
    assert fit_paragraphs([{"text": "fits", "size": 12}], 4.0, 1.0) == ([{"text": "fits", "size": 12}], False)


@pytest.mark.parametrize("lang,letter", [("fi", "ä"), ("sv", "å")])
def test_deck_is_localised(fake_ws, lang, letter):
    out = generate_pptx(fake_ws, fake_ws.settings, lang)
    prs = Presentation(str(out))
    t = Translator(lang)
    titles = [next((s.text_frame.text for s in sl.shapes if s.name.startswith("tpm-title")), "") for sl in prs.slides]
    for key in ("px_summary", "px_trust", "px_monitoring", "px_dataflow", "px_next"):
        assert t(key) in titles, key
    everything = " ".join(s.text_frame.text for _, s in _frames(prs))
    assert letter in everything
    assert_every_text_fits(prs)


def test_deck_has_no_raw_rows(fake_ws, deck_en):
    _, prs = deck_en
    everything = " ".join(s.text_frame.text for _, s in _frames(prs))
    df = pd.read_parquet(fake_ws.path("dataset")).head(300)
    for v in df["press_r"].head(150):
        assert f"{float(v):.4f}" not in everything
    for c in (s.chart for sl in prs.slides for s in sl.shapes if getattr(s, "has_chart", False) and s.has_chart):
        for plot in c.plots:
            for ser in plot.series:
                assert len(list(ser.values)) <= 200  # aggregates only: a bucketed timeline, never a raw series


def test_deck_graceful_when_artifacts_missing(tmp_path):
    from tpm.config import load_settings
    from tpm.workspace import Workspace

    s = load_settings()
    s.workspace_dir = str(tmp_path / "ws")
    ws = Workspace(run_id="bare", settings=s)
    ws.write_json("meta", {"run_id": "bare", "source_path": "x.csv", "profile": s.profile})
    prs = Presentation(str(generate_pptx(ws, s, "en")))
    assert 8 <= len(prs.slides) <= 12
    assert_every_text_fits(prs)
    ws.close()


@pytest.mark.skipif(not (ROOT / "workspace" / "fix_check" / "status.json").exists(), reason="workspace/fix_check is not present")
def test_deck_smoke_on_a_real_run(tmp_path):
    from tpm.config import load_settings
    from tpm.workspace import Workspace

    s = load_settings()
    s.workspace_dir = str(ROOT / "workspace")
    ws = Workspace.open("fix_check", s)
    try:
        out = generate_pptx(ws, s, "fi", out_path=tmp_path / "fix_check.pptx", log=False)  # nothing is written into the run
    finally:
        ws.close()
    prs = Presentation(str(out))
    assert 10 <= len(prs.slides) <= 16
    assert_every_text_fits(prs)
    assert sum(1 for sl in prs.slides if ID_RE.search(sl.notes_slide.notes_text_frame.text)) >= 6
