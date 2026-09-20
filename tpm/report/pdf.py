"""PDF export of the run report (agent F): a typeset A4 document built with ReportLab from the report CONTEXT.

    generate_pdf(ws, settings, lang="en", out_path=None) -> Path        pure Python, always available
    browser_pdf(ws, settings, lang="en", out_path=None) -> Path | None   optional: headless Edge / Chrome print of the
                                                                         HTML report when such a browser is installed

The document is not a screenshot of the HTML report: title page, table of contents (one pass: page numbers are PDF
form objects defined after the last page), running header / footer with "page x of y" and the run id, the eight
report sections plus suspicious rows, evaluation, assessor and the labelled model-written summary, tables with
repeating header rows and wrapped cells, and charts redrawn as vector graphics from the same aggregates the HTML
report uses (score timelines with threshold and flagged spans, pass / warn / fail bars, trust by batch, contribution
bars, learning curve, data-flow diagram). Only derived artifacts are read: no raw rows, no long series.

Fonts: Bitstream Vera (shipped inside the reportlab wheel) is embedded, so Finnish and Swedish letters render on any
machine; characters the font lacks (arrows, Greek) are replaced by readable ASCII before typesetting.
"""
from __future__ import annotations

import contextvars
import io
import os
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from xml.sax.saxutils import escape as _xml_escape

from ..config import Settings, get_settings
from ..workspace import Workspace
from .export_common import export_context, export_path, headline_numbers, log_export, write_atomic_bytes
from .export_common import fit_text as _fit_text
from .i18n import normalize_lang
from .report import _num, _pct, _short, _thousands

# row caps of the PDF (the HTML report shows more; the totals are always stated)
MAX_PDF_SIGNAL_CARDS = 24
MAX_PDF_SIGNAL_ROWS = 400
MAX_PDF_CHECKS = 80
MAX_PDF_UNTRUSTED = 60
MAX_PDF_TIMELINES = 12
MAX_PDF_FLAGS = 60
MAX_PDF_DIAG_CARDS = 8
MAX_PDF_DIAG_ROWS = 60
MAX_PDF_STEPS = 8
MAX_PDF_LEDGER = 60
MAX_PDF_LOG = 80
CELL_CHARS = 520  # a table row can never be taller than a page

FONT, FONT_B, FONT_I, FONT_BI = "TPMSans", "TPMSans-Bold", "TPMSans-Italic", "TPMSans-BoldItalic"
_FONT_FILES = {FONT: "Vera.ttf", FONT_B: "VeraBd.ttf", FONT_I: "VeraIt.ttf", FONT_BI: "VeraBI.ttf"}
_CMAP: set[int] = set()
_FALLBACK = {"→": "->", "←": "<-", "↔": "<->", "⇒": "=>", "⇐": "<=", "↑": "^", "↓": "v", "↗": "^", "↘": "v", "σ": "sigma", "Σ": "sum", "μ": "µ", "Δ": "delta", "δ": "delta", "α": "alpha", "β": "beta", "λ": "lambda", "ρ": "rho", "τ": "tau", "χ": "chi", "✓": "ok", "✔": "ok", "✗": "x", "✘": "x", "●": "•", "■": "•", "▪": "•", "◦": "•", "▲": "^", "▼": "v", " ": " ", " ": " ", " ": " ", "‑": "-", "‐": "-", "⁃": "-", "​": "", "﻿": "", "­": ""}


# every text limit of the document goes through fit_text(); the second attempt of render_pdf() shrinks them all
_FIT_SCALE: contextvars.ContextVar[float] = contextvars.ContextVar("tpm_pdf_fit_scale", default=1.0)


def fit_text(text: Any, max_chars: int) -> tuple[str, bool]:
    return _fit_text(text, max(40, int(max_chars * _FIT_SCALE.get())))


# ----------------------------------------------------------------------------- fonts / text
def _register_fonts() -> None:
    global _CMAP
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if _CMAP:
        return
    import reportlab

    font_dir = Path(reportlab.__file__).resolve().parent / "fonts"
    for name, fn in _FONT_FILES.items():
        pdfmetrics.registerFont(TTFont(name, str(font_dir / fn)))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_B, italic=FONT_I, boldItalic=FONT_BI)
    _CMAP = set(pdfmetrics.getFont(FONT).face.charToGlyph.keys())


def _safe(s: Any) -> str:
    """Text the embedded font can draw: unsupported characters become a readable fallback, never a missing glyph."""
    s = "" if s is None else str(s)
    if s.isascii():
        return s.replace("\r", "")
    out = []
    for ch in s:
        o = ord(ch)
        if o < 128 or o in _CMAP:
            out.append(ch)
        elif ch in _FALLBACK:
            out.append(_FALLBACK[ch])
        else:
            d = "".join(c for c in unicodedata.normalize("NFKD", ch) if ord(c) < 128 or ord(c) in _CMAP)
            out.append(d or "?")
    return "".join(out).replace("\r", "")


def esc(s: Any) -> str:
    """Escaped for Paragraph markup; line breaks kept."""
    return _xml_escape(_safe(s)).replace("\n", "<br/>")


# ----------------------------------------------------------------------------- document
class _Doc:
    """Everything that needs the reportlab imports, built lazily so that importing tpm.report never requires them."""

    def __init__(self, ctx: dict[str, Any]):
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm

        _register_fonts()
        self.ctx = ctx
        self.t = ctx["t"]
        self.lang = ctx["lang"]
        self.mm = mm
        self.page_w, self.page_h = A4
        self.margin_x = 18 * mm
        self.margin_top = 22 * mm
        self.margin_bottom = 18 * mm
        self.W = self.page_w - 2 * self.margin_x
        c = colors.HexColor
        self.C = {"ink": c("#1f2937"), "muted": c("#6b7280"), "grid": c("#d9dde3"), "accent": c("#1a4d8f"), "soft": c("#eef4fb"), "box": c("#f5f6f8"), "zebra": c("#fafbfc"), "pass": c("#2e7d32"), "warn": c("#ed6c02"), "fail": c("#c62828"), "flag": c("#fde2e2"), "external": c("#7b1fa2"), "llm_bg": c("#fffbeb"), "warn_bg": c("#fff4e5"), "white": colors.white, "body": c("#374151")}
        C = self.C
        ps = ParagraphStyle
        base = dict(fontName=FONT, bulletFontName=FONT, textColor=C["ink"], splitLongWords=1)  # bullets too: no base-14 font
        self.S = {
            "title": ps("title", fontSize=25, leading=30, spaceAfter=6, **{**base, "fontName": FONT_B, "textColor": C["accent"]}),
            "subtitle": ps("subtitle", fontSize=12, leading=16, spaceAfter=18, **{**base, "textColor": C["muted"]}),
            "h1": ps("h1", fontSize=15, leading=19, spaceBefore=0, spaceAfter=7, keepWithNext=1, **{**base, "fontName": FONT_B, "textColor": C["accent"]}),
            "h2": ps("h2", fontSize=11.5, leading=15, spaceBefore=11, spaceAfter=4, **{**base, "fontName": FONT_B}),
            "h3": ps("h3", fontSize=9.5, leading=13, spaceBefore=7, spaceAfter=2, **{**base, "fontName": FONT_B}),
            "body": ps("body", fontSize=9, leading=13, spaceAfter=4, **base),
            "lead": ps("lead", fontSize=10, leading=14.5, spaceAfter=5, **base),
            "intro": ps("intro", fontSize=9, leading=13, spaceAfter=7, **{**base, "textColor": C["body"]}),
            "small": ps("small", fontSize=7.5, leading=10, spaceAfter=3, **{**base, "textColor": C["muted"]}),
            "na": ps("na", fontSize=8.5, leading=12, spaceAfter=6, leftIndent=6, borderPadding=(3, 4, 3, 6), **{**base, "fontName": FONT_I, "textColor": C["muted"]}),
            "bullet": ps("bullet", fontSize=9, leading=12.5, spaceAfter=2, leftIndent=11, bulletIndent=2, **base),
            "bullet_s": ps("bullet_s", fontSize=8, leading=10.5, spaceAfter=1.5, leftIndent=10, bulletIndent=2, **base),
            "step": ps("step", fontSize=8.5, leading=12, spaceAfter=2.5, leftIndent=15, bulletIndent=0, **base),
            "td": ps("td", fontSize=7.5, leading=9.6, **base),
            "th": ps("th", fontSize=7.5, leading=9.6, **{**base, "fontName": FONT_B, "splitLongWords": 0}),
            "kv_k": ps("kv_k", fontSize=8.5, leading=11.5, **{**base, "textColor": C["muted"]}),
            "kv_v": ps("kv_v", fontSize=8.5, leading=11.5, **base),
            "tile_n": ps("tile_n", fontSize=15, leading=18, alignment=1, **{**base, "fontName": FONT_B, "textColor": C["accent"]}),
            "tile_l": ps("tile_l", fontSize=7.5, leading=9.5, alignment=1, **{**base, "textColor": C["muted"]}),
            "wording": ps("wording", fontSize=11.5, leading=15.5, **{**base, "fontName": FONT_B}),
            "toc": ps("toc", fontSize=10, leading=14, **base),
        }
        self.sections: list[tuple[str, str, int]] = []  # (key, title, level) in document order
        self.section_pages: dict[str, int] = {}

    # ------------------------------------------------------------------ small flowable helpers
    def P(self, text: Any, style: str = "body"):
        from reportlab.platypus import Paragraph

        return Paragraph(esc(text), self.S[style])

    def PR(self, markup: str, style: str = "body"):
        """Paragraph from markup that is ALREADY escaped (built with esc())."""
        from reportlab.platypus import Paragraph

        return Paragraph(markup, self.S[style])

    def H(self, text: Any, level: int = 2, markup: bool = False, need_mm: Optional[float] = None) -> list[Any]:
        """Sub-heading that never sits alone at the foot of a page (a conditional break, not keepWithNext: that
        would drag a whole table to the next page)."""
        from reportlab.platypus import CondPageBreak, Paragraph

        need = need_mm if need_mm is not None else (34.0 if level == 2 else 22.0)
        return [CondPageBreak(need * self.mm), Paragraph(text if markup else esc(text), self.S["h2" if level == 2 else "h3"])]

    def sp(self, h_mm: float = 2.0):
        from reportlab.platypus import Spacer

        return Spacer(1, h_mm * self.mm)

    def muted(self, text: Any, size: float = 7.0) -> str:
        return f'<font size="{size}" color="#6b7280">{esc(text)}</font>'

    def badge(self, text: Any, kind: str = "muted") -> str:
        col = {"pass": "#2e7d32", "warn": "#ed6c02", "fail": "#c62828", "primary": "#1a4d8f", "ext": "#7b1fa2"}.get(kind, "#6b7280")
        return f'<font color="{col}"><b>{esc(text)}</b></font>'

    def bullets(self, items: Iterable[Any], style: str = "bullet", limit: int = 0, markup: bool = False) -> list[Any]:
        items = [x for x in items if x]
        out = []
        shown = items[:limit] if limit else items
        for x in shown:
            out.append(self.PR(f"<bullet>&bull;</bullet>{x if markup else esc(fit_text(x, 900)[0])}", style))
        if limit and len(items) > limit:
            out.append(self.P(self.t("more", n=len(items) - limit), "small"))
        return out

    def na(self, key: str = "not_available"):
        return self.P(self.t(key), "na")

    def heading(self, title: str, key: str, level: int = 0):
        """A section heading that is also a bookmark, an outline entry and a table-of-contents line."""
        from reportlab.platypus import Paragraph

        doc = self

        class _Heading(Paragraph):
            def draw(self_inner):  # noqa: N805
                canv = self_inner.canv
                canv.bookmarkPage(key)
                canv.addOutlineEntry(_safe(title), key, level=level, closed=level > 0)
                doc.section_pages.setdefault(key, canv.getPageNumber())
                Paragraph.draw(self_inner)

        self.sections.append((key, title, level))
        return _Heading(esc(title), self.S["h1" if level == 0 else "h2"])

    def evidence_list(self, items: Sequence[dict[str, Any]], limit: int = 3) -> list[Any]:
        """Plain sentence first, the technical statement and the id smaller underneath."""
        out = []
        for e in list(items)[:limit]:
            plain, tech, eid = e.get("plain") or "", e.get("technical") or "", e.get("id") or ""
            tail = f" [{eid}]" if eid else ""
            if plain:
                out.append(self.PR(f"<bullet>&bull;</bullet>{esc(fit_text(plain, 700)[0])}<br/>{self.muted(fit_text(tech, 420)[0] + tail)}", "bullet_s"))
            else:
                out.append(self.PR(f"<bullet>&bull;</bullet>{esc(fit_text(tech, 600)[0])}{self.muted(tail)}", "bullet_s"))
        return out

    def cell(self, v: Any, style: str = "td", max_chars: int = CELL_CHARS):
        """A table cell. Plain text is capped to what its column holds in about 40 lines, so that a row can never be
        taller than a page (markup cells are built by the caller from parts that are capped one by one)."""
        from reportlab.platypus import Flowable

        if isinstance(v, Flowable) or (isinstance(v, list) and v and isinstance(v[0], Flowable)):
            return v
        if isinstance(v, tuple) and len(v) == 2 and v[0] == "markup":
            return self.PR(v[1], style)
        if v in (None, ""):
            return self.P("", style)
        parts = [x for x in str(v).split("\n") if x.strip()]
        per_line = max(24, min(CELL_CHARS, max_chars) // max(1, len(parts)))
        return self.P("\n".join(fit_text(x, per_line)[0] for x in parts), style)

    def table(self, header: Sequence[Any], rows: Sequence[Sequence[Any]], widths: Sequence[float], zebra: bool = True, font_style: str = "td"):
        """Grid table: repeating header row, wrapped cells, column widths as fractions of the text width."""
        from reportlab.platypus import LongTable, Table, TableStyle

        from reportlab.pdfbase.pdfmetrics import stringWidth

        total = float(sum(widths))
        col_w = [self.W * w / total for w in widths]
        need = [max([stringWidth(_safe(wd), FONT_B, 7.5) for wd in str(h).split()] + [0.0]) + 7.5 if isinstance(h, str) else 0.0 for h in header]
        deficit = sum(max(0.0, n - w) for n, w in zip(need, col_w))
        slack = sum(max(0.0, w - n) for n, w in zip(need, col_w))
        if deficit > 0 and slack > deficit:
            col_w = [n if w < n else w - (w - n) * deficit / slack for n, w in zip(need, col_w)]
        budget = [max(40, int(w / (7.5 * 0.55)) * 40) for w in col_w]  # characters per line of the column x 40 lines
        data = [[self.cell(h, "th") for h in header]] + [[self.cell(c, font_style, budget[j]) for j, c in enumerate(r)] for r in rows]
        cls = LongTable if len(data) > 40 else Table
        tb = cls(data, colWidths=col_w, repeatRows=1, splitByRow=1)
        C = self.C
        style = [("GRID", (0, 0), (-1, -1), 0.4, C["grid"]), ("BACKGROUND", (0, 0), (-1, 0), C["box"]), ("LINEBELOW", (0, 0), (-1, 0), 0.8, C["accent"]), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 2.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6)]
        if zebra and len(data) > 3:
            style.append(("ROWBACKGROUNDS", (0, 1), (-1, -1), [C["white"], C["zebra"]]))
        tb.setStyle(TableStyle(style))
        tb.spaceAfter = 6
        tb.spaceBefore = 2
        return tb

    def kv(self, pairs: Sequence[tuple[Any, Any]], key_w: float = 0.32, width: Optional[float] = None):
        """Borderless key / value list."""
        from reportlab.platypus import Table, TableStyle

        pairs = [(k, v) for k, v in pairs if v not in (None, "")]
        if not pairs:
            return self.sp(0)
        W = width or self.W
        data = [[self.P(k, "kv_k"), v if not isinstance(v, (str, int, float, bool)) else self.P(fit_text(_num(v) if isinstance(v, float) else v, 700)[0], "kv_v")] for k, v in pairs]
        tb = Table(data, colWidths=[W * key_w, W * (1 - key_w)])
        tb.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5), ("LINEBELOW", (0, 0), (-1, -2), 0.25, self.C["grid"])]))
        tb.spaceAfter = 6
        return tb

    def box(self, flowables: Sequence[Any], bg: str = "soft", border: str = "accent", dashed: bool = False):
        """A tinted box with a coloured left rule. One table row per flowable, so a long box can break across pages."""
        from reportlab.platypus import Table, TableStyle

        rows = [[f] for f in flowables if f is not None]
        if not rows:
            return self.sp(0)
        tb = Table(rows, colWidths=[self.W])
        n = len(rows)
        style = [("BACKGROUND", (0, 0), (-1, -1), self.C[bg]), ("LINEBEFORE", (0, 0), (0, -1), 2.2, self.C[border]), ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5), ("TOPPADDING", (0, 0), (-1, 0), 6), ("BOTTOMPADDING", (0, n - 1), (-1, n - 1), 6)]
        if dashed:
            style.append(("BOX", (0, 0), (-1, -1), 0.5, self.C[border], None, (3, 2)))
        tb.setStyle(TableStyle(style))
        tb.spaceBefore = 4
        tb.spaceAfter = 8
        return tb

    def two_col(self, left: Sequence[Any], right: Sequence[Any], ratio: float = 0.5):
        from reportlab.platypus import Table, TableStyle

        gap = 5 * self.mm
        wl = (self.W - gap) * ratio
        tb = Table([[list(left) or [self.sp(0)], list(right) or [self.sp(0)]]], colWidths=[wl + gap / 2, self.W - wl - gap / 2])
        tb.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), gap / 2), ("LEFTPADDING", (1, 0), (1, 0), gap / 2), ("RIGHTPADDING", (1, 0), (1, 0), 0), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        return tb

    # ------------------------------------------------------------------ charts (vector, drawn from aggregates)
    def chart_timeline(self, values: Sequence[float], threshold: Optional[float], spans: Sequence[Sequence[int]], label: str, x_labels: Sequence[str], height_mm: float = 26.0):
        from reportlab.graphics.shapes import Drawing, Line, PolyLine, Rect, String

        C = self.C
        vals = [float(v) if isinstance(v, (int, float)) and v == v else 0.0 for v in values]
        W, H = self.W, height_mm * self.mm
        d = Drawing(W, H)
        if not vals:
            return d
        pad_l, pad_r, pad_t, pad_b = 30.0, 4.0, 11.0, 10.0
        w, h = W - pad_l - pad_r, H - pad_t - pad_b
        vmax = max(max(vals), threshold or 0.0, 1e-9)
        vmin = min(min(vals), 0.0)
        span = (vmax - vmin) or 1.0
        n = len(vals)
        X = lambda i: pad_l + w * i / max(1, n - 1)  # noqa: E731
        Y = lambda v: pad_b + (v - vmin) / span * h  # noqa: E731
        d.add(Rect(pad_l, pad_b, w, h, fillColor=C["white"], strokeColor=C["grid"], strokeWidth=0.5))
        merged: list[list[int]] = []
        for a, b in sorted((max(0, min(n - 1, int(a))), max(0, min(n - 1, int(b)))) for a, b in spans or []):
            if merged and a <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, max(a, b)])
        for a, b in merged:
            d.add(Rect(X(a), pad_b, max(1.2, X(b) - X(a)), h, fillColor=C["flag"], strokeColor=None))
        if threshold is not None:
            d.add(Line(pad_l, Y(threshold), pad_l + w, Y(threshold), strokeColor=C["fail"], strokeWidth=0.6, strokeDashArray=[3, 2]))
            d.add(String(pad_l - 3, Y(threshold) - 2, _safe(f"{threshold:.3g}"), fontName=FONT, fontSize=6, fillColor=C["fail"], textAnchor="end"))
        pts: list[float] = []
        for i, v in enumerate(vals):
            pts += [X(i), Y(v)]
        d.add(PolyLine(pts, strokeColor=C["accent"], strokeWidth=0.8, fillColor=None))
        ty = Y(threshold) if threshold is not None else -99.0
        if abs(ty - (pad_b + h)) > 8:
            d.add(String(pad_l - 3, pad_b + h - 5, _safe(f"{vmax:.3g}"), fontName=FONT, fontSize=6, fillColor=C["muted"], textAnchor="end"))
        if abs(ty - pad_b) > 8:
            d.add(String(pad_l - 3, pad_b, _safe(f"{vmin:.3g}"), fontName=FONT, fontSize=6, fillColor=C["muted"], textAnchor="end"))
        if x_labels:
            d.add(String(pad_l, 1.5, _safe(x_labels[0]), fontName=FONT, fontSize=6, fillColor=C["muted"]))
            d.add(String(pad_l + w, 1.5, _safe(x_labels[-1]), fontName=FONT, fontSize=6, fillColor=C["muted"], textAnchor="end"))
        if label:
            d.add(String(pad_l, pad_b + h + 3, _safe(label), fontName=FONT_B, fontSize=7, fillColor=C["ink"]))
        return d

    def chart_stacked(self, rows: Sequence[tuple[str, dict[str, int]]]):
        from reportlab.graphics.shapes import Drawing, Rect, String

        C = self.C
        row_h, gap, pad_l, pad_r = 11.0, 4.0, 78.0, 34.0
        H = len(rows) * (row_h + gap) + gap + 12
        d = Drawing(self.W, H)
        w = self.W - pad_l - pad_r
        for i, (name, counts) in enumerate(rows):
            total = max(1, sum(int(counts.get(k, 0)) for k in ("pass", "warn", "fail", "not_testable")))
            y0 = H - gap - (i + 1) * (row_h + gap) + gap
            d.add(String(pad_l - 5, y0 + 3, _safe(name), fontName=FONT, fontSize=7.5, fillColor=C["ink"], textAnchor="end"))
            x0 = pad_l
            for k in ("pass", "warn", "fail", "not_testable"):
                cnt = int(counts.get(k, 0))
                if cnt <= 0:
                    continue
                ww = w * cnt / total
                d.add(Rect(x0, y0, ww, row_h, fillColor=C.get(k, C["muted"]), strokeColor=C["white"], strokeWidth=0.4))
                if ww > 16:
                    d.add(String(x0 + ww / 2, y0 + 3, str(cnt), fontName=FONT, fontSize=6.5, fillColor=C["white"], textAnchor="middle"))
                x0 += ww
            d.add(String(pad_l + w + 4, y0 + 3, str(total), fontName=FONT, fontSize=7, fillColor=C["muted"]))
        x = pad_l
        for k in ("pass", "warn", "fail"):  # legend
            d.add(Rect(x, 1, 6, 6, fillColor=C[k], strokeColor=None))
            d.add(String(x + 9, 1.5, _safe(self.t(k)), fontName=FONT, fontSize=6.5, fillColor=C["muted"]))
            x += 58
        return d

    def chart_hbars(self, items: Sequence[tuple[str, float, str]], width: Optional[float] = None):
        from reportlab.graphics.shapes import Drawing, Rect, String

        C = self.C
        W = width or self.W
        row_h, gap, pad_l, pad_r = 8.5, 3.0, 30.0, 58.0
        H = len(items) * (row_h + gap) + gap
        d = Drawing(W, H)
        w = W - pad_l - pad_r
        vmax = max([float(v) for _, v, _ in items] + [1e-9])
        for i, (name, v, txt) in enumerate(items):
            y0 = H - (i + 1) * (row_h + gap)
            d.add(String(pad_l - 4, y0 + 2, _safe(name), fontName=FONT_B, fontSize=7, fillColor=C["ink"], textAnchor="end"))
            d.add(Rect(pad_l, y0, w, row_h, fillColor=C["box"], strokeColor=None))
            d.add(Rect(pad_l, y0, w * max(0.0, float(v)) / vmax, row_h, fillColor=C["accent"], strokeColor=None))
            d.add(String(pad_l + w + 4, y0 + 2, _safe(txt), fontName=FONT, fontSize=6.8, fillColor=C["muted"]))
        return d

    def chart_trust(self, series: Sequence[dict[str, Any]], threshold: Optional[float]):
        from reportlab.graphics.shapes import Drawing, Line, Rect, String

        C = self.C
        W, H = self.W, 30 * self.mm
        d = Drawing(W, H)
        if not series:
            return d
        pad_l, pad_r, pad_t, pad_b = 30.0, 4.0, 5.0, 10.0
        w, h = W - pad_l - pad_r, H - pad_t - pad_b
        n = len(series)
        bw = w / n
        d.add(Rect(pad_l, pad_b, w, h, fillColor=C["white"], strokeColor=C["grid"], strokeWidth=0.5))
        for i, p in enumerate(series):
            sc = max(0.0, min(1.0, float(p.get("score") or 0.0)))
            d.add(Rect(pad_l + i * bw + bw * 0.12, pad_b, bw * 0.76, max(0.6, h * sc), fillColor=C["accent"] if p.get("trusted", True) else C["fail"], strokeColor=None))
        if threshold is not None:
            yy = pad_b + h * max(0.0, min(1.0, float(threshold)))
            d.add(Line(pad_l, yy, pad_l + w, yy, strokeColor=C["fail"], strokeWidth=0.6, strokeDashArray=[3, 2]))
            d.add(String(pad_l - 3, yy - 2, _safe(_num(threshold)), fontName=FONT, fontSize=6, fillColor=C["fail"], textAnchor="end"))
        d.add(String(pad_l - 3, pad_b + h - 5, "1", fontName=FONT, fontSize=6, fillColor=C["muted"], textAnchor="end"))
        d.add(String(pad_l - 3, pad_b, "0", fontName=FONT, fontSize=6, fillColor=C["muted"], textAnchor="end"))
        d.add(String(pad_l, 1.5, _safe(series[0].get("label")), fontName=FONT, fontSize=6, fillColor=C["muted"]))
        d.add(String(pad_l + w, 1.5, _safe(series[-1].get("label")), fontName=FONT, fontSize=6, fillColor=C["muted"], textAnchor="end"))
        return d

    def chart_line(self, points: Sequence[tuple[float, float]], x_label: str = "", width: Optional[float] = None):
        from reportlab.graphics.shapes import Circle, Drawing, PolyLine, Rect, String

        C = self.C
        W, H = width or self.W * 0.5, 40 * self.mm
        d = Drawing(W, H)
        pts = [(float(a), float(b)) for a, b in points]
        if len(pts) < 2:
            return d
        pad_l, pad_r, pad_t, pad_b = 28.0, 6.0, 6.0, 16.0
        w, h = W - pad_l - pad_r, H - pad_t - pad_b
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        xmin, xmax = min(xs), max(xs)
        pad_y = (max(ys) - min(ys)) * 0.15 or abs(max(ys)) * 0.05 or 0.05
        ymin, ymax = min(ys) - pad_y, max(ys) + pad_y
        X = lambda a: pad_l + (a - xmin) / ((xmax - xmin) or 1.0) * w  # noqa: E731
        Y = lambda b: pad_b + (b - ymin) / ((ymax - ymin) or 1.0) * h  # noqa: E731
        d.add(Rect(pad_l, pad_b, w, h, fillColor=C["white"], strokeColor=C["grid"], strokeWidth=0.5))
        flat: list[float] = []
        for a, b in pts:
            flat += [X(a), Y(b)]
        d.add(PolyLine(flat, strokeColor=C["accent"], strokeWidth=1.2, fillColor=None))
        for a, b in pts:
            d.add(Circle(X(a), Y(b), 1.8, fillColor=C["accent"], strokeColor=None))
        d.add(String(pad_l - 3, pad_b + h - 5, _safe(f"{ymax:.3g}"), fontName=FONT, fontSize=6.5, fillColor=C["muted"], textAnchor="end"))
        d.add(String(pad_l - 3, pad_b, _safe(f"{ymin:.3g}"), fontName=FONT, fontSize=6.5, fillColor=C["muted"], textAnchor="end"))
        d.add(String(pad_l, 6, _safe(f"{xmin:.3g}"), fontName=FONT, fontSize=6.5, fillColor=C["muted"]))
        d.add(String(pad_l + w, 6, _safe(f"{xmax:.3g}"), fontName=FONT, fontSize=6.5, fillColor=C["muted"], textAnchor="end"))
        if x_label:
            d.add(String(pad_l + w / 2, 0.5, _safe(x_label), fontName=FONT, fontSize=6.5, fillColor=C["muted"], textAnchor="middle"))
        return d

    def chart_dataflow(self):
        """What stays inside and what may leave: the HTML report's diagram, redrawn (logical 760 x 300 units, scaled)."""
        from reportlab.graphics.shapes import Drawing, Group, Line, Polygon, Rect, String

        C, t, df = self.C, self.t, self.ctx["dataflow"]
        LW, LH = 760.0, 300.0
        k = self.W / LW
        d = Drawing(self.W, LH * k)
        g = Group()
        Yf = lambda y: LH - y  # noqa: E731  (the logical layout has its origin at the top)

        def rect(x, y, w, h, fill, stroke, sw=1.0, dash=None, r=6):
            g.add(Rect(x, Yf(y + h), w, h, rx=r, ry=r, fillColor=fill, strokeColor=stroke, strokeWidth=sw, strokeDashArray=dash))

        def text(x, y, s, size=11, color="ink", bold=False, anchor="start"):
            g.add(String(x, Yf(y), _safe(s), fontName=FONT_B if bold else FONT, fontSize=size, fillColor=C[color], textAnchor=anchor))

        def arrow(x1, y1, x2, y2, color="ink", dash=None):
            import math

            g.add(Line(x1, Yf(y1), x2, Yf(y2), strokeColor=C[color], strokeWidth=1.2, strokeDashArray=dash))
            a = math.atan2(Yf(y2) - Yf(y1), x2 - x1)
            L = 8.0
            g.add(Polygon([x2, Yf(y2), x2 - L * math.cos(a - 0.4), Yf(y2) - L * math.sin(a - 0.4), x2 - L * math.cos(a + 0.4), Yf(y2) - L * math.sin(a + 0.4)], fillColor=C[color], strokeColor=None))

        inside_w = 440
        rect(10, 20, inside_w, LH - 40, C["soft"], C["accent"], 1.5, r=10)
        text(24, 42, t("s8_stays"), 12.5, "accent", True)
        for txt, yy in (("dataset.parquet / raw rows", 70), ("evidence · signals · relations · checks", 100), ("scores · flags · diagnoses", 130), ("decision_log.sqlite (hash chain)", 160), ("egress_ledger.jsonl", 190)):
            rect(24, yy - 14, 232, 22, C["white"], C["grid"], 0.8, r=4)
            text(32, yy + 1, txt, 10.5)
        rect(280, 66, 158, 60, C["white"], C["accent"], 1.5, r=8)
        text(359, 86, f"{t('s8_local_model')} (Ollama)", 10.5, "accent", True, "middle")
        text(359, 104, str(df.get("local_model") or "")[:28], 10, "ink", False, "middle")
        text(359, 119, f"{df['summary']['n_local']} {t('calls_local').lower()}", 10, "muted", False, "middle")
        arrow(256, 100, 278, 96)
        rect(300, 170, 120, 54, colors_hex("#fff8e1"), C["warn"], 1.5, r=8)
        text(360, 191, t("guard_result"), 10.5, "warn", True, "middle")
        text(360, 209, f"{df['summary']['n_blocked']} {t('calls_blocked').lower()}", 9.5, "muted", False, "middle")
        arrow(256, 160, 298, 190)
        ext_x = inside_w + 60
        ew = LW - ext_x - 10
        allow = bool(df.get("allow_external"))
        rect(ext_x, 150, ew, 94, C["white"], C["external"], 1.5, dash=None if allow else [6, 4], r=8)
        cx = ext_x + ew / 2
        text(cx, 172, t("s8_external_model"), 10.5, "external", True, "middle")
        text(cx, 190, str(df.get("external_model") or "")[:30], 10, "ink", False, "middle")
        text(cx, 206, f"{df['summary']['n_external']} {t('calls_external').lower()}", 10, "muted", False, "middle")
        text(cx, 226, f"{t('s8_allow_external')}: {t('yes') if allow else t('no')}", 10, "muted", False, "middle")
        arrow(422, 197, ext_x - 3, 197, "external", None if allow else [6, 4])
        text(LW / 2, LH - 6, f"{t('s8_profile')}: {df.get('profile')}", 10, "muted", False, "middle")
        g.scale(k, k)
        d.add(g)
        return d

    # ------------------------------------------------------------------ page furniture
    def on_cover(self, canv, doc) -> None:
        C = self.C
        canv.saveState()
        canv.setFillColor(C["accent"])
        canv.rect(0, self.page_h - 14 * self.mm, self.page_w, 14 * self.mm, stroke=0, fill=1)
        canv.setFillColor(C["white"])
        canv.setFont(FONT_B, 9)
        canv.drawString(self.margin_x, self.page_h - 9 * self.mm, "TRUSTWORTHY PROCESS MONITOR")
        canv.setFillColor(C["muted"])
        canv.setFont(FONT, 7.5)
        canv.drawString(self.margin_x, 11 * self.mm, _safe(self.t("pdf_no_raw")))
        canv.restoreState()

    def on_page(self, canv, doc) -> None:
        C, t = self.C, self.t
        canv.saveState()
        canv.setFont(FONT, 7.5)
        canv.setFillColor(C["muted"])
        y = self.page_h - 13 * self.mm
        canv.drawString(self.margin_x, y, _safe(t("title")))
        canv.drawRightString(self.page_w - self.margin_x, y, _safe(f"{t('run_id')}: {self.ctx['run_id']}"))
        canv.setStrokeColor(C["accent"])
        canv.setLineWidth(0.8)
        canv.line(self.margin_x, y - 3, self.page_w - self.margin_x, y - 3)
        fy = 10 * self.mm
        canv.setStrokeColor(C["grid"])
        canv.setLineWidth(0.4)
        canv.line(self.margin_x, fy + 8, self.page_w - self.margin_x, fy + 8)
        canv.drawString(self.margin_x, fy, _safe(f"{self.ctx['source_name']} · {t('generated_at')} {self.ctx['generated_at']} UTC · {self.ctx['lang_name']}"))
        total_w = 16.0
        canv.drawRightString(self.page_w - self.margin_x - total_w, fy, _safe(f"{t('pdf_page')} {canv.getPageNumber()} {t('pdf_of')} "))
        canv.translate(self.page_w - self.margin_x - total_w, fy)
        canv.doForm("tpm-total-pages")  # defined when the document is saved: the page count is known then
        canv.restoreState()

    def toc_flowable(self):
        """One fixed-layout page: titles, dotted leaders and links are drawn now, the page numbers are form objects
        that are defined at save time (one pass, no second build)."""
        from reportlab.pdfbase.pdfmetrics import stringWidth
        from reportlab.platypus import Flowable

        doc = self
        entries = list(self.sections)  # filled before the story is built? no: read at draw time (see draw)

        class _Toc(Flowable):
            def wrap(self_inner, aw, ah):  # noqa: N805
                self_inner.width = aw
                self_inner.height = min(ah, max(60.0, 16.0 * (len(doc.sections) + 1)))
                return self_inner.width, self_inner.height

            def draw(self_inner):  # noqa: N805
                canv = self_inner.canv
                y = self_inner.height - 12
                for key, title, level in doc.sections:
                    size = 10 if level == 0 else 9
                    indent = 0 if level == 0 else 12
                    font = FONT_B if level == 0 else FONT
                    txt = _safe(title)
                    canv.setFont(font, size)
                    canv.setFillColor(doc.C["ink"] if level == 0 else doc.C["body"])
                    canv.drawString(indent, y, txt)
                    x0 = indent + stringWidth(txt, font, size) + 4
                    x1 = self_inner.width - 22
                    if x1 > x0:
                        canv.setStrokeColor(doc.C["grid"])
                        canv.setLineWidth(0.6)
                        canv.setDash(1, 2.5)
                        canv.line(x0, y + 1.5, x1, y + 1.5)
                        canv.setDash()
                    canv.saveState()
                    canv.translate(self_inner.width, y)
                    canv.doForm(f"tpm-toc-{key}")
                    canv.restoreState()
                    canv.linkRect("", key, (0, y - 3, self_inner.width, y + size), relative=1, thickness=0)
                    y -= 16.0 if level == 0 else 13.5

        del entries
        return _Toc()

    def canvas_class(self):
        from reportlab.pdfgen.canvas import Canvas

        doc = self

        class _Canvas(Canvas):
            def __init__(self_inner, *args, **kwargs):  # noqa: N805
                kwargs.setdefault("initialFontName", FONT)  # every page starts in the embedded font, not Helvetica
                Canvas.__init__(self_inner, *args, **kwargs)

            def save(self_inner):  # noqa: N805
                total = max(1, self_inner.getPageNumber() - 1)
                self_inner.beginForm("tpm-total-pages", lowerx=-2, lowery=-4, upperx=60, uppery=14)
                self_inner.setFont(FONT, 7.5)
                self_inner.setFillColor(doc.C["muted"])
                self_inner.drawString(0, 0, str(total))
                self_inner.endForm()
                for key, _title, level in doc.sections:
                    self_inner.beginForm(f"tpm-toc-{key}", lowerx=-60, lowery=-4, upperx=2, uppery=14)
                    self_inner.setFont(FONT_B if level == 0 else FONT, 10 if level == 0 else 9)
                    self_inner.setFillColor(doc.C["ink"])
                    self_inner.drawRightString(0, 0, str(doc.section_pages.get(key, "")))
                    self_inner.endForm()
                Canvas.save(self_inner)

        return _Canvas

    # ------------------------------------------------------------------ sections
    def cover(self) -> list[Any]:
        from reportlab.platypus import Table, TableStyle

        ctx, t = self.ctx, self.t
        df = ctx["dataflow"]
        out: list[Any] = [self.sp(34), self.P(t("title"), "title"), self.P(t("subtitle"), "subtitle")]
        out.append(self.kv([(t("run_id"), ctx["run_id"]), (t("source"), ctx["source_name"]), (t("generated_at"), f"{ctx['generated_at']} UTC"), (t("profile"), f"{df.get('profile')} — {df.get('description') or ''}".rstrip(" —")), (t("s8_local_model"), f"{df.get('local_provider')} / {df.get('local_model')}"), (t("run_state"), (ctx.get("status") or {}).get("state")), (t("language"), ctx["lang_name"])], key_w=0.26))
        out += [self.sp(8), self.P(t("pdf_headline_numbers"), "h2")]
        tiles = headline_numbers(ctx)
        per_row = 4
        rows = []
        for i in range(0, len(tiles), per_row):
            chunk = tiles[i : i + per_row]
            chunk += [("", "")] * (per_row - len(chunk))
            rows.append([[self.P(v, "tile_n"), self.P(k, "tile_l")] if (k or v) else self.P("", "tile_l") for k, v in chunk])
        tb = Table(rows, colWidths=[self.W / per_row] * per_row)
        style = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9), ("INNERGRID", (0, 0), (-1, -1), 3, self.C["white"]), ("BACKGROUND", (0, 0), (-1, -1), self.C["soft"])]
        tb.setStyle(TableStyle(style))
        out.append(tb)
        out.append(self.sp(8))
        for s in ctx["overview"][:2]:
            out.append(self.P(s, "body"))
        return out

    def overview(self) -> list[Any]:
        ctx, t = self.ctx, self.t
        out: list[Any] = [self.heading(t("section_overview"), "overview")]
        out += self.bullets(ctx["overview"])
        llm = ctx.get("llm")
        if llm:
            out.append(self.heading(t("section_llm"), "model-summary", 1))
            rows: list[Any] = [self.PR(f"<i>{esc(t('llm_label', source=llm.get('source') or 'llm'))}</i>", "small")]
            rows += [self.P(fit_text(p, 1600)[0], "lead") for p in llm.get("summary") or []]
            for s in llm.get("sections") or []:
                if s.get("heading"):
                    rows.append(self.P(s["heading"], "h3"))
                rows += [self.P(fit_text(p, 1600)[0], "body") for p in s.get("paragraphs") or []]
                refs = [r.get("id") for r in s.get("refs") or [] if r.get("id")]
                if refs:
                    rows.append(self.P(f"{t('llm_refs')}: {', '.join(refs)}", "small"))
            if llm.get("uncertainty"):
                rows.append(self.P(t("llm_uncertainty"), "h3"))
                rows += self.bullets(llm["uncertainty"], "bullet_s")
            tail = [t("llm_source_line", source=llm.get("source") or "llm")] + ([llm["generated_at"]] if llm.get("generated_at") else []) + ([f"{t('llm_confidence')}: {_pct(llm['confidence'])}"] if llm.get("confidence") is not None else []) + ([t("llm_truncated")] if llm.get("truncated") else [])
            rows.append(self.P(" · ".join(tail), "small"))
            out.append(self.box(rows, bg="llm_bg", border="warn", dashed=True))
        else:
            out.append(self.P(t("llm_not_used"), "small"))
        out += self.H(t("section_stages"), 2)
        if ctx.get("stages"):
            out.append(self.table([t("stage"), t("state"), t("duration"), t("message")], [[s.get("stage"), ("markup", self.badge(s.get("state"), "pass" if s.get("state") == "done" else ("fail" if s.get("state") == "failed" else "muted"))), f"{s['seconds']} {t('seconds')}" if s.get("seconds") else "", s.get("message")] for s in ctx["stages"]], [12, 10, 10, 68]))
        else:
            out.append(self.na())
        return out

    def suspicious(self) -> list[Any]:
        ctx, t, lang = self.ctx, self.t, self.lang
        su = ctx.get("suspicious")
        if not su:
            return []
        title = t("section_suspicious") + (f" ({_thousands(su['n_rows'], lang)})" if su.get("n_rows") else "")
        out: list[Any] = [self.heading(title, "suspicious")]
        if su.get("headline"):
            out.append(self.P(su["headline"], "lead"))
        out.append(self.box([self.P(fit_text(su["wording"], 700)[0], "wording")], bg="warn_bg", border="warn"))
        out.append(self.P(t("susp_intro") + (" " + su["regime_text"] if su.get("regime_text") else ""), "intro"))
        if su["rows"]:
            if (su.get("n_rows") or 0) > su["shown"]:
                out.append(self.P(t("susp_capped", shown=su["shown"], total=_thousands(su["n_rows"], lang)), "small"))
            rows = []
            for r in su["rows"]:
                ids = ", ".join(list(r.get("flag_ids") or []) + list(r.get("check_ids") or []) + list(r.get("evidence_ids") or []))
                msg = esc(fit_text(r.get("statement") or r.get("explanation"), 330)[0])
                if r.get("statement") and r.get("explanation") and r["explanation"] != r["statement"]:
                    msg += "<br/>" + self.muted(fit_text(r["explanation"], 220)[0])
                if ids:
                    msg += "<br/>" + self.muted(ids, 6.5)
                rows.append([r.get("row_text"), "\n".join(str(x) for x in (r.get("group_id"), r.get("batch_id")) if x), "\n".join(r.get("signals_text") or []), r.get("sources_text"), _num(r.get("strength")), ("markup", msg)])
            out.append(self.table([t("susp_row"), f"{t('group')} / {t('batch')}", t("susp_signals"), t("susp_found_by"), t("susp_strength"), t("message")], rows, [10, 10, 19, 13, 9, 39]))
        elif not su["point_flags"]:
            out.append(self.na("susp_none_listed"))
        if su["point_flags"]:
            out += self.H(t("susp_point_flags", n=_thousands(su["n_point_flags"], lang)), 2)
            if su["n_point_flags"] > len(su["point_flags"]):
                out.append(self.P(t("pdf_capped_rows", shown=len(su["point_flags"]), total=_thousands(su["n_point_flags"], lang)), "small"))
            rows = []
            for f in su["point_flags"]:
                msg = esc(fit_text(f.get("statement"), 320)[0])
                for e in f.get("ev") or []:
                    if e.get("plain"):
                        msg += "<br/>" + self.muted(fit_text(e["plain"], 260)[0])
                rows.append([f.get("id"), "\n".join(str(x) for x in (f.get("group_id"), f.get("batch_id"), f.get("row_text")) if x), f"{_num(f.get('score'))}" + (f" / {_num(f.get('threshold'))}" if f.get("threshold") is not None else ""), "\n".join(f.get("signals") or []), ("markup", msg)])
            out.append(self.table([t("id"), f"{t('group')} / {t('batch')} / {t('susp_row')}", f"{t('score')} / {t('threshold')}", t("responsible_signals"), t("message")], rows, [13, 15, 11, 18, 43]))
        return out

    def s1(self) -> list[Any]:
        ctx, t, lang = self.ctx, self.t, self.lang
        out: list[Any] = [self.heading(t("section_1"), "section-1"), self.P(t("s1_intro"), "intro")] + self.H(t("s1_dataset_assumptions"), 2)
        ds = ctx.get("dataset")
        if ds:
            fmt = str(ds.get("format") or "") + (f" ({ds.get('delimiter')!r})" if ds.get("delimiter") else "") + (", transposed" if ds.get("transposed") else "")
            grouping = f"{ds.get('grouping_method')} -> {ds.get('n_groups')} {t('groups')}" + (f" ({', '.join(ds.get('group_columns') or [])})" if ds.get("group_columns") else "")
            pairs = [(t("rows"), _thousands(ds.get("n_rows"), lang)), (t("columns"), ds.get("n_cols")), (t("format"), fmt), (t("s1_time_column"), ds.get("time_column") or t("none")), (t("s1_sample_period"), ds.get("sample_period_text")), (t("s1_grouping"), grouping), (t("s1_label_columns"), ", ".join(ds.get("label_columns") or []) or t("none")), (t("s1_meta_columns"), ", ".join(ds.get("meta_columns") or []) or t("none"))]
            pairs += [(f"{t('s1_domain')} · {k}", v) for k, v in ds.get("domain_items") or []]
            out.append(self.kv(pairs))
            if ds.get("assumptions"):
                out += self.H(t("assumptions"), 3)
                out += self.bullets(ds["assumptions"], "bullet_s", limit=8)
            if ds.get("candidates"):
                out += self.H(t("s1_grouping_candidates"), 3)
                out.append(self.table([t("s1_grouping"), t("columns"), t("groups"), t("score"), t("why")], [[c.get("method"), ", ".join(c.get("columns") or []), c.get("n_groups"), _num(c.get("score")), c.get("rationale")] for c in ds["candidates"][:8]], [16, 14, 9, 8, 53]))
            if ds.get("inferences"):
                out += self.H(t("hypotheses"), 3)
                out.append(self.table([t("id"), t("hypotheses"), t("status"), t("confidence"), t("evidence")], [[i.get("id"), ("markup", esc(fit_text(i.get("claim"), 300)[0]) + ("<br/>" + self.muted(fit_text(i.get("reasoning"), 260)[0]) if i.get("reasoning") else "")), i.get("status"), _pct(i.get("confidence")), ", ".join((i.get("evidence_ids") or [])[:6])] for i in ds["inferences"]], [13, 45, 11, 13, 18]))
        else:
            out.append(self.na())
        n_total = ctx.get("n_signals_total") or 0
        rel = ctx.get("relations_count")
        out += self.H(f"{t('s1_catalog')} ({n_total} {t('signals')}" + (f", {rel} {t('related_signals').lower()}" if rel else "") + ")", 2)
        signals = ctx.get("signals") or []
        if not signals:
            out.append(self.na("s1_no_signals"))
            return out
        for s in signals[:MAX_PDF_SIGNAL_CARDS]:
            head = f"<b>{esc(s['id'])}</b> — {esc(s.get('role_label'))} {self.muted('(' + t('confidence') + ' ' + _pct(s.get('structural_confidence')) + ')', 8)}"
            if s.get("excluded"):
                head += " " + self.badge(t("excluded") + (f": {s.get('excluded_reason')}" if s.get("excluded_reason") else ""))
            if s.get("human_role_override"):
                head += " " + self.badge(f"{t('human_override')}: {s['human_role_override']}", "primary")
            out += self.H(head, 3, markup=True, need_mm=34.0)
            facts = [f"{t('source_column')}: {s.get('source_column') or t('none')} ({s.get('dtype')})"]
            if s.get("instrument"):
                facts.append(f"{t('instrument_hypothesis')}: {s['instrument']} ({_pct(s.get('instrument_confidence'))})")
            if s.get("unit_op"):
                facts.append(f"{t('unit_operation')}: {s['unit_op']} ({_pct(s.get('unit_op_conf'))})")
            if s.get("cluster"):
                facts.append(f"{t('cluster')}: {s['cluster']}")
            if s.get("related"):
                facts.append(f"{t('related_signals')}: {'; '.join(s['related'][:4])}")
            out.append(self.P(" · ".join(facts), "small"))
            left: list[Any] = []
            if s.get("hypotheses"):
                left.append(self.PR(f"<b>{esc(t('hypotheses'))}</b>", "small"))
                left += self.bullets([f"{esc(fit_text(h.get('claim'), 200)[0])} {self.muted('[' + ', '.join(str(x) for x in (h.get('status'), _pct(h.get('confidence')), h.get('source')) if x) + ']')}" for h in s["hypotheses"][:4]], "bullet_s", markup=True)
            left.append(self.PR(f"<b>{esc(t('uncertain'))}</b>", "small"))
            left += self.bullets(s.get("uncertain") or [], "bullet_s", limit=3)
            right: list[Any] = [self.PR(f"<b>{esc(t('evidence_statements'))}</b>", "small")]
            right += self.evidence_list(s.get("evidence") or [], 3) or [self.na()]
            out += left + right
        rest = signals[MAX_PDF_SIGNAL_CARDS:MAX_PDF_SIGNAL_ROWS]
        if rest:
            out += self.H(t("pdf_signals_brief", n=len(rest)), 3)
            out.append(self.table([t("alias"), t("structural_role"), t("confidence"), t("instrument_hypothesis"), t("cluster"), t("uncertain")], [[s["id"], s.get("role_label"), _pct(s.get("structural_confidence")), (f"{s['instrument']} ({_pct(s.get('instrument_confidence'))})" if s.get("instrument") else ""), s.get("cluster") or "", (s.get("uncertain") or [""])[0]] for s in rest], [9, 22, 10, 20, 9, 30]))
        if n_total > len(signals):
            out.append(self.P(t("more", n=n_total - len(signals)), "small"))
        return out

    def s2(self) -> list[Any]:
        ctx, t, lang = self.ctx, self.t, self.lang
        q = ctx["quality"]
        out: list[Any] = [self.heading(t("section_2"), "section-2"), self.P(t("s2_intro"), "intro")]
        if q.get("verdict_text"):  # run-level verdict (round 6)
            out.append(self.P(" ".join(x for x in (q["verdict_text"], q.get("not_testable_text") or "") if x)))
        if q.get("n_checks"):
            nt = f" · {t('not_testable')} {_thousands(q['n_not_testable'], lang)}" if q.get("n_not_testable") else ""
            out += self.H(f"{t('s2_by_category')} — {_thousands(q['n_checks'], lang)} ({t('pass')} {_thousands(q['n_pass'], lang)} · {t('warn')} {_thousands(q['n_warn'], lang)} · {t('fail')} {_thousands(q['n_fail'], lang)}{nt})", 2)
            if q.get("by_category"):
                out.append(self.chart_stacked(q["by_category"]))
            out += self.H(t("s2_failed_checks"), 2)
            failed = q.get("failed") or []
            if failed:
                shown = failed[:MAX_PDF_CHECKS]
                if q.get("n_failed_total", 0) > len(shown):
                    out.append(self.P(t("pdf_capped_rows", shown=len(shown), total=_thousands(q["n_failed_total"], lang)), "small"))
                rows = []
                for c in shown:
                    trace = " ".join(x for x in [str(c.get("rule_id") or ""), ", ".join((c.get("evidence_ids") or [])[:3])] if x)
                    rows.append([c.get("check_id"), ("markup", self.badge(t.status(c.get("status")), c.get("status") if c.get("status") in ("pass", "warn", "fail") else "muted")), f"{t.cat(c.get('category'))}\n{c.get('check_type')}", ", ".join((c.get("signals") or [])[:6]), "\n".join(str(x) for x in (c.get("batch_id"), f"{c.get('row_start')}–{c.get('row_end')}" if c.get("row_start") is not None else "") if x), _num(c.get("severity")), ("markup", esc(fit_text(c.get("statement"), 360)[0]) + ("<br/>" + self.muted(trace, 6.5) if trace else ""))])
                out.append(self.table([t("id"), t("status"), f"{t('category')} / {t('check_type')}", t("signals"), f"{t('batch')} / {t('row_range')}", t("severity"), f"{t('message')} / {t('traceability')}"], rows, [13, 7, 15, 10, 13, 8, 34]))
            else:
                out.append(self.na("s2_all_pass"))
        else:
            out.append(self.na("s2_no_checks"))
        out += self.H(t("s2_rules"), 2)
        if q.get("rules"):
            out.append(self.table([t("id"), t("rule_text"), t("rule_status"), t("compiled_check"), t("compile_source"), t("confidence"), t("count")], [[r.get("id"), ("markup", esc(fit_text(r.get("text"), 300)[0]) + ("<br/>" + self.muted(fit_text(r.get("explanation_text"), 240)[0]) if r.get("explanation_text") else "")), r.get("status"), r.get("compiled") or t("na_short"), r.get("compile_source"), _pct(r.get("compile_confidence")), r.get("n_checks")] for r in q["rules"][:60]], [10, 32, 9, 25, 11, 8, 5]))
        else:
            out.append(self.na("s2_no_rules"))
        out += self.H(t("s2_trust"), 2)
        out.append(self.P(t("s2_trust_note", threshold=q.get("threshold")), "small"))
        series = q.get("trust_series") or []
        if series:
            n_total = q.get("n_trust_total") or len(series)
            out.append(self.chart_trust(series, q.get("threshold")))
            low = min(float(p.get("score") or 0) for p in series)
            out.append(self.P((t("px_trust_by_block") if n_total > len(series) else t("px_trust_by_batch")) + ". " + t("pdf_trust_all", trusted=_thousands(n_total - q.get("n_untrusted_total", 0), lang), total=_thousands(n_total, lang), low=_num(low)), "small"))
            unt = (q.get("untrusted") or [])[:MAX_PDF_UNTRUSTED]
            if unt:
                out += self.H(f"{t('s2_untrusted_batches')} ({_thousands(q.get('n_untrusted_total', len(unt)), lang)})", 3)
                if q.get("n_untrusted_total", 0) > len(unt):
                    out.append(self.P(t("capped_untrusted", shown=len(unt), total=_thousands(q["n_untrusted_total"], lang)), "small"))
                out.append(self.table([t("batch"), t("trust_score"), t("untrusted_signals"), t("reasons")], [[x.get("batch_id"), ("markup", self.badge(_num(x.get("trust_score")), "fail")), ", ".join((x.get("untrusted_signals") or [])[:10]), " ".join(["; ".join((x.get("reasons") or [])[:4]), str(x.get("statement") or "")]).strip()] for x in unt], [13, 11, 22, 54]))
            else:
                out.append(self.na("s2_no_untrusted"))
        else:
            out.append(self.na())
        return out

    def s3(self) -> list[Any]:
        ctx, t, lang = self.ctx, self.t, self.lang
        det = ctx["detect"]
        out: list[Any] = [self.heading(t("section_3"), "section-3"), self.P(t("s3_intro"), "intro")]
        out += self.H(t("s3_baseline"), 2)
        if det.get("baseline_items"):
            out.append(self.kv([(k, _num(v) if isinstance(v, float) else v) for k, v in det["baseline_items"] if k not in ("assumptions",)][:12]))
            if det.get("baseline_assumptions"):
                out += self.bullets(det["baseline_assumptions"], "bullet_s", limit=6)
        else:
            out.append(self.na())
        out += self.H(t("s3_detectors"), 2)
        if det.get("detect_items"):
            out.append(self.kv([(k, _num(v) if isinstance(v, float) else v) for k, v in det["detect_items"] if v not in ("", None)][:12]))
        else:
            out.append(self.na())
        tl = det.get("timelines") or {}
        th = tl.get("threshold")
        out += self.H(t("s3_timelines") + (f" — {t('threshold')} {_num(th)}" if th is not None else ""), 2)
        lines = [x for x in (tl.get("timelines") or []) if x.get("values")]
        if lines:
            gs = det.get("group_summary") or {}
            if gs.get("n_groups"):
                out.append(self.P(t("px_groups_over", n_over=_thousands(gs.get("n_over", 0), lang), n_groups=_thousands(gs["n_groups"], lang)), "body"))
            out.append(self.P(t("pdf_timeline_caption"), "small"))
            pick = sorted(lines, key=lambda x: (-int(x.get("n_flags") or 0), -float(x.get("max") or 0)))[:MAX_PDF_TIMELINES]
            keep = {id(x) for x in pick}
            for x in [x for x in lines if id(x) in keep]:
                out.append(self.chart_timeline(x["values"], th, x.get("spans") or [], f"{x.get('label') or x.get('group')}  ·  {t('s3_flags').lower()}: {x.get('n_flags')}  ·  max {_num(x.get('max'))}", x.get("x_labels") or ()))
                out.append(self.sp(1.2))
            if (tl.get("n_groups") or 0) > len(pick):
                out.append(self.P(t("pdf_timelines_shown", shown=len(pick), total=_thousands(tl["n_groups"], lang)), "small"))
        else:
            out.append(self.na("s3_no_scores"))
        out += self.H(f"{t('s3_flags')} ({_thousands(det.get('n_flags', 0), lang)})", 2)
        flags = det.get("flags") or []
        if flags:
            shown = flags[:MAX_PDF_FLAGS]
            if det.get("n_flags", 0) > len(shown):
                out.append(self.P(t("capped_flags", shown=len(shown), total=_thousands(det["n_flags"], lang)), "small"))
            if det.get("flags_by_kind"):
                line = f"{t('kind')}: " + " · ".join(f"{k} {_thousands(n, lang)}" for k, n in det["flags_by_kind"])
                if det.get("flags_by_cause"):
                    line += f" — {t('cause_class')}: " + " · ".join(f"{k} {_thousands(n, lang)}" for k, n in det["flags_by_cause"])
                out.append(self.P(line, "small"))
            rows = []
            for f in shown:
                msg = esc(fit_text(f.get("statement"), 340)[0])
                for e in (f.get("ev") or [])[: 1 if len(shown) <= 30 else 0]:
                    if e.get("plain"):
                        msg += "<br/>" + self.muted(fit_text(e["plain"], 300)[0], 6.8)
                if (f.get("det") or {}).get("short"):
                    msg += "<br/>" + self.muted(f"{t('detector')}: {f['det']['short']}", 6.5)
                if f.get("human_note"):
                    msg += "<br/>" + self.muted(f"{t('note')}: {fit_text(f['human_note'], 160)[0]}", 6.5)
                ident = esc(f.get("id")) + ("<br/>" + self.muted(f.get("pattern_id"), 6.5) if f.get("pattern_id") else "") + ("<br/>" + self.badge(f.get("human_status"), "primary") if f.get("human_status") else "")
                rows.append([("markup", ident), f.get("kind_label"), "\n".join(str(x) for x in (f.get("group_id"), f.get("batch_id"), f"{f.get('row_start')}–{f.get('row_end')}") if x not in (None, "")), f"{_num(f.get('score'))}" + (f" / {_num(f.get('threshold'))}" if f.get("threshold") is not None else "") + f"\n{t('severity').lower()} {_num(f.get('severity'))}", "\n".join((f.get("signals") or [])[:4]), f"{f.get('cause_label')}\n{_pct(f.get('confidence'))}", ("markup", msg)])
            out.append(self.table([t("id"), t("kind"), f"{t('group')} / {t('batch')} / {t('row_range')}", f"{t('score')} / {t('threshold')}", t("responsible_signals"), f"{t('cause_class')} / {t('confidence')}", t("message")], rows, [13, 8, 14, 10, 16, 11, 28]))
            if det.get("detector_legend"):
                out.append(self.P(f"{t('detector_legend')}: " + "; ".join(f"{d['short']} = {d['family']}" + (f": {', '.join(d['parts'])}" if d.get("parts") else "") for d in det["detector_legend"][:6]), "small"))
        else:
            out.append(self.na("s3_no_flags"))
        out += self.H(t("s3_patterns"), 2)
        if det.get("patterns"):
            out.append(self.table([t("pattern"), t("pattern_name"), t("n_events"), t("groups_affected"), t("confidence"), t("classifier_reliability"), t("message")], [[p.get("id"), p.get("name_label"), p.get("n_events"), _short(", ".join(str(g) for g in (p.get("groups_affected") or [])), 70), _pct(p.get("confidence")), _pct(p.get("classifier_reliability")) if p.get("classifier_reliability") is not None else t("na_short"), p.get("description")] for p in det["patterns"][:30]], [13, 13, 8, 15, 10, 10, 31]))
        else:
            out.append(self.na("s3_no_patterns"))
        return out

    def _diag_card(self, d: dict[str, Any]) -> list[Any]:
        t = self.t
        out: list[Any] = []
        crit = d.get("critique") or None
        head = f"<b>{esc(d.get('label') or d.get('id'))}</b> — {esc(t('fault_type'))}: <b>{esc(fit_text(d.get('fault_type'), 90)[0])}</b>"
        out += self.H(head, 2, markup=True, need_mm=62.0)
        meta = [f"{t('cause_class')}: {d.get('cause_label')}", f"{t('confidence')} {_pct(d.get('confidence'))}", f"{t('group')} {d.get('group_id') or t('na_short')}"]
        if d.get("flag_ids"):
            meta.append(f"{t('flags_ref')}: {', '.join(d['flag_ids'][:5])}")
        if d.get("pattern_id"):
            meta.append(f"{t('pattern')}: {d['pattern_id']}")
        line = esc(" · ".join(meta))
        if crit:
            line += " · " + self.badge(f"{t('critique')}: {d.get('verdict_label')}", "pass" if crit.get("verdict") == "supported" else ("fail" if crit.get("verdict") == "rejected" else "warn"))
        if d.get("human_status"):
            line += " · " + self.badge(f"{t('human_status')}: {d['human_status']}", "primary")
        out.append(self.PR(line, "small"))
        if d.get("summary"):
            out.append(self.P(fit_text(d["summary"], 1400)[0], "body"))
        ranked = d.get("ranked") or []
        if ranked:
            out += self.H(t("ranked_signals"), 3)
            out.append(self.chart_hbars([(str(s.get("signal")), float(s.get("contribution") or 0), f"{_pct(s.get('contribution'))}{' ' + str(s.get('direction')) if s.get('direction') else ''}") for s in ranked[:8]], width=self.W * 0.62))
            out.append(self.sp(1))
            out += self.bullets([f"<b>{esc(s.get('signal'))}</b> ({esc(', '.join(str(x) for x in (_pct(s.get('contribution')), s.get('direction'), (t('lag') + ' ' + str(s.get('lag'))) if s.get('lag') is not None else '') if x))}): {esc(fit_text(s.get('explanation') or t('why'), 300)[0])}" for s in ranked[:5]], "bullet_s", markup=True)
        if d.get("propagation"):
            out += self.H(t("propagation"), 3)
            out += self.bullets([f"<b>{esc(p.get('from_signal'))} -&gt; {esc(p.get('to_signal'))}</b>" + (f" ({esc(t('lag'))} {esc(p.get('lag'))})" if p.get("lag") is not None else "") + f" {self.muted(fit_text(p.get('explanation'), 240)[0], 7.2)}" for p in d["propagation"][:6]], "bullet_s", markup=True)
        out += self.H(t("steps"), 3)
        steps = d.get("steps") or []
        if steps:
            for i, s in enumerate(steps[:MAX_PDF_STEPS], start=1):
                out.append(self.PR(f"<bullet>{i}.</bullet>{esc(fit_text(s, 700)[0])}", "step"))
            if len(steps) > MAX_PDF_STEPS:
                out.append(self.P(t("more", n=len(steps) - MAX_PDF_STEPS), "small"))
        else:
            out.append(self.na())
        if d.get("uncertainty"):
            out += self.H(t("uncertainty"), 3)
            out += self.bullets(d["uncertainty"], "bullet_s", limit=5)
        if d.get("assumptions"):
            out += self.H(t("assumptions"), 3)
            out += self.bullets(d["assumptions"], "bullet_s", limit=4)
        if d.get("ev"):
            out += self.H(t("evidence"), 3)
            out += self.evidence_list(d["ev"], 4)
        if crit:
            rows: list[Any] = [self.PR(f"<b>{esc(t('critique'))}</b> — {esc(t('verdict'))}: {esc(d.get('verdict_label'))}" + (f" · {esc(t('adjusted_confidence'))}: {esc(_pct(crit.get('adjusted_confidence')))}" if crit.get("adjusted_confidence") is not None else "") + f" {self.muted('(' + str(crit.get('source') or '') + ')')}", "body")]
            rows += self.bullets(crit.get("objections") or [], "bullet_s", limit=5)
            if crit.get("checks"):
                rows.append(self.PR(f"{esc(t('code_checks'))}: " + "  ".join(self.badge(("+ " if c.get("passed") else "- ") + str(c.get("name")), "pass" if c.get("passed") else "fail") for c in crit["checks"][:8]), "small"))
            out.append(self.box(rows))
        out.append(self.P(f"{t('narrative_source')}: {d.get('narrative_source')}" + (f" · {t('note')}: {fit_text(d.get('human_note'), 200)[0]}" if d.get("human_note") else ""), "small"))
        return out

    def s4(self) -> list[Any]:
        ctx, t, lang = self.ctx, self.t, self.lang
        out: list[Any] = [self.heading(t("section_4"), "section-4"), self.P(t("s4_intro"), "intro")]
        diags = ctx.get("diagnoses") or []
        if not diags:
            out.append(self.na("s4_no_diag"))
            return out
        full = [d for d in diags if not d.get("compact")]
        cards, brief = full[:MAX_PDF_DIAG_CARDS], full[MAX_PDF_DIAG_CARDS:] + [d for d in diags if d.get("compact")]
        brief = brief[: max(0, MAX_PDF_DIAG_ROWS - len(cards))]
        if ctx.get("n_diag_total", 0) > len(cards) + len(brief):
            out.append(self.P(t("capped_diagnoses", shown=len(cards) + len(brief), total=_thousands(ctx["n_diag_total"], lang)), "small"))
        for d in cards:
            out += self._diag_card(d)
        if brief:
            out += self.H(t("s4_compact", n=len(brief)), 2)
            rows = []
            for d in brief:
                crit = d.get("critique") or {}
                verdict = ("markup", self.badge(d.get("verdict_label"), "pass" if crit.get("verdict") == "supported" else ("fail" if crit.get("verdict") == "rejected" else "warn"))) if crit else ""
                rows.append([("markup", esc(d.get("id")) + ("<br/>" + self.muted(d.get("pattern_id"), 6.5) if d.get("pattern_id") else "")), d.get("group_id") or "", d.get("fault_type"), f"{d.get('cause_label')}\n{_pct(d.get('confidence'))}", verdict, ("markup", esc(fit_text(d.get("summary"), 300)[0]) + "<br/>" + self.muted(f"{t('flags_ref')}: {', '.join((d.get('flag_ids') or [])[:4])}", 6.5))])
            out.append(self.table([t("id"), t("group"), t("fault_type"), f"{t('cause_class')} / {t('confidence')}", t("critique"), t("summary")], rows, [13, 9, 17, 12, 10, 39]))
        return out

    def s5(self) -> list[Any]:
        ctx, t = self.ctx, self.t
        out: list[Any] = [self.heading(t("section_5"), "section-5"), self.P(t("s5_intro"), "intro")]
        human = ctx.get("human") or []
        if human:
            shown = human[:150]
            if len(human) > len(shown):
                out.append(self.P(t("pdf_capped_rows", shown=len(shown), total=len(human)), "small"))
            out.append(self.table([t("seq"), t("when"), f"{t('actor')} / {t('role')}", t("action"), t("object"), t("before"), t("after"), t("note")], [[h.get("seq"), h.get("ts"), f"{h.get('actor')}\n{h.get('role')}", ("markup", self.badge(h.get("action_label"), "primary")), f"{h.get('object_type')} {h.get('object_id')}", h.get("before"), h.get("after"), h.get("note")] for h in shown], [5, 11, 10, 10, 13, 19, 16, 16]))
        else:
            out.append(self.na("s5_no_human"))
        return out

    def s6(self) -> list[Any]:
        ctx, t, lang = self.ctx, self.t, self.lang
        log = ctx["log"]
        ok = bool((log.get("chain") or {}).get("ok"))
        out: list[Any] = [self.heading(t("section_6"), "section-6"), self.P(t("s6_intro"), "intro")]
        out.append(self.box([self.PR(f"<b>{esc(t('s6_chain'))}:</b> {self.badge('OK' if ok else 'FAIL', 'pass' if ok else 'fail')} {esc(fit_text(log.get('chain_text'), 600)[0])}", "body")], bg="soft" if ok else "warn_bg", border="pass" if ok else "fail"))
        out.append(self.P(f"{t('s6_summary')}: {_thousands(log.get('total', 0), lang)}", "body"))
        half = (self.W - 5 * self.mm) / 2

        def mini(title: str, head: str, rows: list[Any]) -> list[Any]:
            from reportlab.platypus import Table, TableStyle

            data = [[self.P(head, "th"), self.P(t("count"), "th")]] + [[self.P(fit_text(a, 60)[0], "td"), self.P(_thousands(n, lang), "td")] for a, n in rows[:14]]
            tb = Table(data, colWidths=[half * 0.74, half * 0.26], repeatRows=1)
            tb.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, self.C["grid"]), ("BACKGROUND", (0, 0), (-1, 0), self.C["box"]), ("LINEBELOW", (0, 0), (-1, 0), 0.8, self.C["accent"]), ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.4)]))
            return [self.P(title, "h3"), tb]

        out.append(self.two_col(mini(t("s6_by_action"), t("action"), log.get("by_action") or []), mini(t("s6_by_actor"), t("actor"), log.get("by_actor") or [])))
        return out

    def s7(self) -> list[Any]:
        ctx, t = self.ctx, self.t
        out: list[Any] = [self.heading(t("section_7"), "section-7"), self.P(t("s7_intro"), "intro")]
        out += self.bullets([t("s7_generic"), t("s7_adapters"), t("s7_example")])
        ds = ctx.get("dataset") or {}
        dom = " · ".join(f"{k} {v}" for k, v in ds.get("domain_items") or []) or t("not_available")
        out.append(self.P(f"{t('s7_link')} {dom}", "body"))
        if ctx.get("domain_items"):
            out.append(self.kv([(k, v) for k, v in ctx["domain_items"]][:8]))
        return out

    def s8(self) -> list[Any]:
        ctx, t, lang = self.ctx, self.t, self.lang
        df = ctx["dataflow"]
        summ = df["summary"]
        out: list[Any] = [self.heading(t("section_8"), "section-8"), self.P(t("s8_intro"), "intro"), self.chart_dataflow(), self.sp(2)]
        out.append(self.box([self.PR(f"<b>{esc(t('s8_statement'))}</b>", "body")] + [self.P(fit_text(p, 1400)[0], "body") for p in str(df.get("statement") or "").split("\n") if p.strip()]))
        out.append(self.kv([(t("s8_profile"), f"{df.get('profile')} — {df.get('description') or ''}".rstrip(" —")), (t("s8_allow_external"), t("yes") if df.get("allow_external") else t("no")), (t("s8_guard_strict"), t("yes") if df.get("guard_strict") else t("no")), (t("s8_local_model"), f"{df.get('local_provider')} / {df.get('local_model')}"), (t("s8_external_model"), f"{df.get('external_provider')} / {df.get('external_model')}" + (f" @ {df.get('external_base_url')}" if df.get("external_base_url") else "")), (t("s8_stays"), t("s8_stays_items")), (t("s8_leaves"), t("s8_leaves_items")), (t("s8_guard_rules"), df.get("guard_items"))]))
        if df.get("routing"):
            out += self.H(t("s8_routing"), 3)
            out.append(self.P(" · ".join(f"{k}: {v}" for k, v in df["routing"]), "small"))
        cov = df.get("coverage")  # round 6: who wrote the explanations (model or evidence template, and why)
        if cov:
            out += self.H(cov.get("heading") or "", 3)
            out.append(self.P(fit_text(cov.get("sentence"), 900)[0], "body"))
            out += [self.P(fit_text(d, 400)[0], "small") for d in (cov.get("details") or [])[:6]]
        gd = df.get("guard_demo")  # round 6: the egress guard shown on this run's own data (python -m tpm guard-demo)
        if gd:
            out += self.H(gd.get("title") or "", 3)
            out.append(self.P(fit_text(gd.get("intro"), 600)[0], "small"))
            out += [self.P(fit_text(x, 400)[0], "small") for x in (gd.get("safe") or [])[:8]]
            q = gd.get("question")
            if q:
                out.append(self.kv([(q.get("before_label"), fit_text(q.get("before"), 400)[0]), (q.get("after_label"), fit_text(q.get("after"), 400)[0])]))
            badge = self.badge("demo_blocked" if gd.get("unsafe_ok") else "demo_allowed", "fail" if gd.get("unsafe_ok") else "warn")
            out += [self.PR((badge + " " if i == 0 else "") + esc(fit_text(x, 400)[0]), "small") for i, x in enumerate((gd.get("unsafe") or [])[:8])]
            if gd.get("layers"):
                out.append(self.P(f"{gd.get('layers_label')}: " + "; ".join(str(x) for x in gd["layers"][:10]), "small"))
            out.append(self.P(f"{gd.get('headers') or ''} {gd.get('ledger') or ''}".strip(), "small"))
        out += self.H(t("s8_ledger_summary"), 2)
        out.append(self.kv([(t("calls_local"), _thousands(summ.get("n_local", 0), lang)), (t("calls_external"), _thousands(summ.get("n_external", 0), lang)), (t("calls_blocked"), _thousands(summ.get("n_blocked", 0), lang)), (t("calls_fallback"), _thousands(summ.get("n_fallback", 0), lang)), (t("bytes_external"), _thousands(summ.get("bytes_external", 0), lang)), (t("model"), ", ".join(summ.get("models") or []))]))
        out += self.H(f"{t('s8_ledger')} ({_thousands(df.get('n_ledger', 0), lang)})", 2)
        ledger = (df.get("ledger") or [])[:MAX_PDF_LEDGER]
        if ledger:
            if df.get("n_ledger", 0) > len(ledger):
                out.append(self.P(t("pdf_capped_rows", shown=len(ledger), total=_thousands(df["n_ledger"], lang)), "small"))
            rows = []
            for r in ledger:
                gr = str(r.get("guard_result") or "")
                rows.append([r.get("id"), str(r.get("ts") or "").replace("T", " ")[:19], r.get("task"), ("markup", self.badge(r.get("route"), "ext" if r.get("route") == "external" else "primary")), f"{r.get('provider')}/{r.get('model')}", ("markup", self.badge(gr, "fail" if gr in ("blocked", "demo_blocked") else ("warn" if gr == "fallback" else ("muted" if gr == "demo_allowed" else "pass"))) + ("<br/>" + self.muted(fit_text(r.get("guard_reason"), 120)[0], 6.5) if r.get("guard_reason") else "")), _thousands(r.get("payload_bytes") or 0, lang), ("markup", esc(fit_text(r.get("purpose"), 140)[0]) + ("<br/>" + self.muted(fit_text(r.get("artifact_types"), 140)[0], 6.5) if r.get("artifact_types") else ""))])
            out.append(self.table([t("id"), t("when"), t("task"), t("route"), t("model"), t("guard_result"), t("bytes"), f"{t('purpose')} / {t('artifact_types')}"], rows, [11, 11, 13, 8, 15, 13, 7, 22]))
        else:
            out.append(self.na("s8_no_ledger"))
        out += self.H(t("s8_swap"), 3)
        out.append(self.P(t("s8_swap_text"), "small"))
        return out

    @staticmethod
    def _val(v: Any, limit: int = 150) -> str:
        if isinstance(v, bool) or v is None:
            return "" if v is None else str(v)
        if isinstance(v, (int, float)):
            try:
                if float(v).is_integer() and abs(v) >= 1000:  # a count: 3,500 rather than 3.5e+03
                    return f"{int(v):,}"
            except (OverflowError, ValueError):
                pass
            return _num(v, 3)
        if isinstance(v, dict):
            parts = [f"{k}: {_num(x, 3) if isinstance(x, (int, float)) and not isinstance(x, bool) else x}" for k, x in v.items() if isinstance(x, (str, int, float, bool))]
            return fit_text("; ".join(parts), limit)[0] if parts else f"({len(v)})"
        if isinstance(v, (list, tuple)):
            return fit_text(", ".join(str(x) for x in v[:8]), limit)[0]
        return fit_text(v, limit)[0]

    def evaluation(self) -> list[Any]:
        ctx, t = self.ctx, self.t
        ev = ctx.get("evaluation")
        if not ev:
            return []
        out: list[Any] = [self.heading(t("section_eval"), "evaluation"), self.P(t("eval_intro"), "intro")]
        out.append(self.kv([(k, self._val(v, 400)) for k, v in ev.get("scalars") or []][:16]))
        for tb in ev.get("tables") or []:
            all_cols = list(tb.get("cols") or [])
            rows_in = list(tb.get("rows") or [])[:40]
            if not all_cols:
                continue
            # a column of nested results (per-label metrics) reads badly inside a grid cell: it gets its own key / value
            # list per row underneath, and the grid keeps the scalar columns
            nested = [j for j, _ in enumerate(all_cols) if any(isinstance(vals[j], dict) for _, vals in rows_in if j < len(vals))]
            flat = [j for j in range(len(all_cols)) if j not in nested][:6]
            out += self.H(str(tb.get("name")), 3)
            if flat:
                rows = [[k] + [self._val(list(vals)[j]) if j < len(vals) else "" for j in flat] for k, vals in rows_in]
                out.append(self.table([t("key")] + [all_cols[j] for j in flat], rows, [16] + [84 / len(flat)] * len(flat)))
            for k, vals in rows_in[:6]:
                for j in nested[:3]:
                    v = vals[j] if j < len(vals) else None
                    pairs = [(str(kk).replace("_", " "), self._val(vv, 260)) for kk, vv in (v or {}).items() if isinstance(vv, (str, int, float, bool))][:10] if isinstance(v, dict) else []
                    if pairs:
                        out.append(self.PR(f"<b>{esc(k)}</b> · {esc(str(all_cols[j]).replace('_', ' '))}", "small"))
                        out.append(self.kv(pairs, key_w=0.36))
        return out

    def assessor(self) -> list[Any]:
        ctx, t = self.ctx, self.t
        out: list[Any] = [self.heading(t("section_assessor"), "assessor"), self.P(t("assessor_intro"), "intro")]
        a = ctx.get("assessor")
        if not a:
            out.append(self.na("assessor_no"))
            return out
        for v in a.get("verdicts") or []:
            kind = {"pass": "pass", "warn": "warn"}.get(v.get("state"), "muted")
            rows = [self.PR(f"<b>{esc(v.get('question'))}</b> {self.badge(v.get('answer'), kind)}" + (f" {self.muted('(' + t('expected_gain').lower() + ' ' + _num(v.get('gain'), 3) + ')', 8)}" if v.get("gain") is not None else ""), "body")]
            if v.get("why"):
                rows.append(self.P(fit_text(v["why"], 900)[0], "body"))
            out.append(self.box(rows))
        if a.get("summary"):
            out.append(self.P(a["summary"], "body"))
        left = [self.kv([(k, self._val(v, 200)) for k, v in a.get("scalars") or [] if k != "summary"][:10], key_w=0.5, width=(self.W - 5 * self.mm) / 2)]
        right: list[Any] = []
        if a.get("curve_points"):
            right = [self.P(t("learning_curve"), "h3"), self.chart_line(a["curve_points"], t("learning_curve"), width=(self.W - 5 * self.mm) / 2)]
        out.append(self.two_col(left, right))
        if a.get("recommendations"):
            out += self.H(t("recommendations"), 2)
            out.append(self.table([t("action"), t("expected_gain"), t("evidence")], [[("markup", (self.muted(r.get("id"), 6.5) + " " if r.get("id") else "") + f"<b>{esc(fit_text(r.get('action'), 140)[0])}</b>" + ("<br/>" + esc(fit_text(r.get("text"), 360)[0]) if r.get("text") else "")), _num(r.get("gain"), 3) if isinstance(r.get("gain"), (int, float)) else (r.get("gain") or ""), r.get("evidence")] for r in a["recommendations"]], [62, 12, 26]))
        return out

    def appendix(self) -> list[Any]:
        ctx, t = self.ctx, self.t
        log = ctx["log"]
        out: list[Any] = [self.heading(t("section_appendix"), "appendix")]
        rows = (log.get("appendix") or [])[:MAX_PDF_LOG]
        out.append(self.P(f"{t('s6_appendix_note', shown=len(rows), total=log.get('total', 0))} — {log.get('chain_text')}", "small"))
        if rows:
            out.append(self.table([t("seq"), t("when"), t("actor"), t("action"), t("object"), t("payload"), "hash"], [[e.get("seq"), e.get("ts"), e.get("actor"), e.get("action"), e.get("object"), fit_text(e.get("payload"), 150)[0], e.get("hash")] for e in rows], [6, 12, 17, 11, 17, 26, 11]))
        else:
            out.append(self.na())
        return out

    # ------------------------------------------------------------------ build
    def story(self) -> list[Any]:
        from reportlab.platypus import NextPageTemplate, PageBreak

        body: list[Any] = []
        for part in (self.overview, self.suspicious, self.s1, self.s2, self.s3, self.s4, self.s5, self.s6, self.s7, self.s8, self.evaluation, self.assessor, self.appendix):
            flow = part()
            if flow:
                if body:
                    body.append(PageBreak())
                body += flow
        # the sections are known now, so the contents page can be laid out (it sits before them in the document)
        front: list[Any] = [NextPageTemplate("body")] + self.cover() + [PageBreak(), self.P(self.t("toc"), "h1"), self.sp(2), self.toc_flowable(), PageBreak()]
        return front + body

    def build(self) -> bytes:
        from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate

        buf = io.BytesIO()
        ctx = self.ctx
        doc = BaseDocTemplate(buf, pagesize=(self.page_w, self.page_h), leftMargin=self.margin_x, rightMargin=self.margin_x, topMargin=self.margin_top, bottomMargin=self.margin_bottom, title=_safe(f"{self.t('title')} — {ctx['run_id']}"), author="Trustworthy Process Monitor", subject=_safe(self.t("subtitle")), creator="tpm.report.pdf", lang=self.lang)
        frame = lambda name: Frame(self.margin_x, self.margin_bottom, self.W, self.page_h - self.margin_top - self.margin_bottom, id=name, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)  # noqa: E731
        doc.addPageTemplates([PageTemplate(id="cover", frames=[frame("cover")], onPage=self.on_cover), PageTemplate(id="body", frames=[frame("body")], onPage=self.on_page)])
        doc.build(self.story(), canvasmaker=self.canvas_class())
        return buf.getvalue()


def colors_hex(h: str):
    from reportlab.lib import colors

    return colors.HexColor(h)


# ----------------------------------------------------------------------------- public API
def render_pdf(context: dict[str, Any]) -> bytes:
    """The PDF bytes for a report context (collect() / export_context()). Texts are capped so that no table row can
    outgrow a page; should a layout still fail, the document is built once more with much tighter caps."""
    try:
        return _Doc(context).build()
    except Exception as first:
        token = _FIT_SCALE.set(0.35)
        try:
            return _Doc(context).build()
        except Exception:
            raise first
        finally:
            _FIT_SCALE.reset(token)


def generate_pdf(ws: Workspace, settings: Optional[Settings] = None, lang: str = "en", out_path: Optional[str | Path] = None, context: Optional[dict[str, Any]] = None, log: bool = True) -> Path:
    """Write the PDF report (default <run>/report_<lang>.pdf) and return its path. Pure Python; the language model is
    never called: a model-written summary is included only when one is already stored for this run and language."""
    settings = settings or ws.settings or get_settings()
    lang = normalize_lang(lang)
    ctx = context if context is not None and context.get("lang") == lang else export_context(ws, settings, lang)
    out = Path(out_path) if out_path else export_path(ws, lang, "pdf")
    data = render_pdf(ctx)
    write_atomic_bytes(out, data)
    if log:
        log_export(ws, out, lang, "pdf", {"llm_summary": bool(ctx.get("llm")), "engine": "reportlab"})
    return out


# ----------------------------------------------------------------------------- optional: browser print of the HTML report
def find_browser() -> Optional[str]:
    """Edge or Chrome, when installed (used only on request; nothing is downloaded)."""
    for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    cands = []
    for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA")):
        if base:
            cands += [Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe", Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"]
    cands += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"), Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    return next((str(c) for c in cands if c.exists()), None)


def browser_pdf(ws: Workspace, settings: Optional[Settings] = None, lang: str = "en", out_path: Optional[str | Path] = None, timeout_s: float = 90.0) -> Optional[Path]:
    """Print report_<lang>.html with a headless Edge / Chrome (the HTML report's own print layout). Returns None when no
    such browser is installed or the print fails; callers then use generate_pdf(). Offline: a local file is printed."""
    from .report import ensure_report, report_path

    exe = find_browser()
    if not exe:
        return None
    settings = settings or ws.settings or get_settings()
    lang = normalize_lang(lang)
    try:
        ensure_report(ws, settings, lang, ask_model=False)
        src = report_path(ws, lang)
        out = Path(out_path) if out_path else ws.dir / f"report_{lang}_print.pdf"
        out.parent.mkdir(parents=True, exist_ok=True)
        profile = ws.dir / ".browser_print_profile"
        cmd = [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--disable-extensions", f"--user-data-dir={profile}", "--no-pdf-header-footer", f"--print-to-pdf={out}", src.resolve().as_uri()]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout_s, check=False)
        shutil.rmtree(profile, ignore_errors=True)
        if out.exists() and out.stat().st_size > 2000:
            log_export(ws, out, lang, "pdf", {"engine": "browser"})
            return out
    except Exception:
        return None
    return None
