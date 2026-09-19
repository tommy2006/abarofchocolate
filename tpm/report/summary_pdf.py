"""One-page shareable summary (review item 30): what was analysed, can the data be trusted, what was found, the three
findings to look at first (problem -> reason -> what to do), how well detection worked when labels exist, and what
left the machine. Built from the same report context as the HTML / PDF / deck; one A4 page by construction (every
text is capped), for handing to someone who will not open the full report."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..config import Settings, get_settings
from ..workspace import Workspace
from .export_common import export_context, log_export, write_atomic_bytes
from .i18n import normalize_lang
from .prose import whole_sentences

WORDS = {
    "en": {"title": "Process monitor - one-page summary", "run": "Run", "data": "Data", "trust": "Can the data be trusted?", "found": "What was found", "top": "Look at these first",
           "problem": "Problem", "reason": "Why", "todo": "What to do", "eval": "How well detection worked (labels used only for this check)", "flow": "What left this computer",
           "rows": "{n} rows, {s} sensors", "trust_line": "{u} of {b} batches cannot be trusted; {f} checks failed and {w} gave a warning (of {n} checks).", "more": "Full report: report_{lang}.html / .pdf / .pptx in the run folder; every statement cites its evidence."},
    "fi": {"title": "Prosessin valvonta - yhden sivun yhteenveto", "run": "Ajo", "data": "Data", "trust": "Voiko dataan luottaa?", "found": "Mitä löytyi", "top": "Katso nämä ensin",
           "problem": "Ongelma", "reason": "Miksi", "todo": "Mitä tehdä", "eval": "Kuinka hyvin havaitseminen toimi (leimoja käytettiin vain tähän tarkistukseen)", "flow": "Mitä tältä koneelta lähti",
           "rows": "{n} riviä, {s} anturia", "trust_line": "{u}/{b} erään ei voi luottaa; {f} tarkistusta epäonnistui ja {w} varoitti ({n} tarkistuksesta).", "more": "Koko raportti: report_{lang}.html / .pdf / .pptx ajon kansiossa; jokainen väite viittaa todisteisiinsa."},
    "sv": {"title": "Processövervakning - sammanfattning på en sida", "run": "Körning", "data": "Data", "trust": "Går det att lita på data?", "found": "Vad som hittades", "top": "Titta på dessa först",
           "problem": "Problem", "reason": "Varför", "todo": "Vad göra", "eval": "Hur väl detekteringen fungerade (etiketter användes bara för denna kontroll)", "flow": "Vad som lämnade datorn",
           "rows": "{n} rader, {s} givare", "trust_line": "{u} av {b} batcher går inte att lita på; {f} kontroller misslyckades och {w} gav en varning (av {n} kontroller).", "more": "Hela rapporten: report_{lang}.html / .pdf / .pptx i körningens mapp; varje påstående hänvisar till sina bevis."},
}


def _todo(steps: list[str]) -> str:
    for s in steps or []:
        low = str(s).lower()
        if low.startswith(("what to check", "mitä tarkistaa", "vad att kontrollera")):
            return str(s).split(":", 1)[-1].strip()
    return str((steps or [""])[-1])


def _why(steps: list[str]) -> str:
    for s in steps or []:
        if str(s).lower().startswith("why"):
            return str(s).split(":", 1)[-1].strip()
    return ""


def summary_path(ws: Workspace, lang: str) -> Path:
    return ws.dir / f"summary_{normalize_lang(lang)}.pdf"


def render_summary(ctx: dict[str, Any]) -> bytes:
    import io

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepInFrame, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from .pdf import FONT, FONT_B, _register_fonts, esc

    _register_fonts()
    lang = ctx.get("lang", "en")
    W = WORDS.get(lang, WORDS["en"])
    ink, accent, muted = colors.HexColor("#1f2937"), colors.HexColor("#1a4d8f"), colors.HexColor("#6b7280")
    st = {
        "title": ParagraphStyle("t", fontName=FONT_B, fontSize=17, leading=21, textColor=accent, spaceAfter=2),
        "sub": ParagraphStyle("s", fontName=FONT, fontSize=8.5, leading=11, textColor=muted, spaceAfter=8),
        "h": ParagraphStyle("h", fontName=FONT_B, fontSize=10.5, leading=13, textColor=accent, spaceBefore=6, spaceAfter=3),
        "b": ParagraphStyle("b", fontName=FONT, fontSize=8.6, leading=11.4, textColor=ink, spaceAfter=2),
        "cell": ParagraphStyle("c", fontName=FONT, fontSize=7.8, leading=10, textColor=ink),
        "cellh": ParagraphStyle("ch", fontName=FONT_B, fontSize=7.8, leading=10, textColor=ink),
    }
    P = lambda text, s="b": Paragraph(esc(str(text)), st[s])  # noqa: E731
    story: list[Any] = [P(W["title"], "title")]
    meta = ctx.get("meta") or {}
    schema = ctx.get("schema") or {}
    n_rows = f"{int(schema.get('n_rows') or 0):,}"
    rows_txt = W["rows"].format(n=n_rows, s=ctx.get("n_signals_total") or "-")
    story.append(P(f"{W['run']}: {ctx.get('run_id')} · {ctx.get('generated_at')} · {rows_txt} · {meta.get('profile', '')}", "sub"))
    story.append(P(W["found"], "h"))
    for line in (ctx.get("overview") or [])[:5]:
        story.append(P("• " + whole_sentences(str(line), 230)))
    q = ctx.get("quality") or {}
    if isinstance(q, dict) and q.get("n_checks"):
        story.append(P(W["trust"], "h"))
        story.append(P("• " + W["trust_line"].format(u=q.get("n_untrusted_total", 0), b=q.get("n_batches") or q.get("n_trust_total") or 0, f=q.get("n_fail", 0), w=q.get("n_warn", 0), n=q.get("n_checks", 0))))
    diags = [d for d in (ctx.get("diagnoses") or []) if not d.get("compact")][:3]
    if diags:
        story.append(P(W["top"], "h"))
        rows = [[P(W["problem"], "cellh"), P(W["reason"], "cellh"), P(W["todo"], "cellh")]]
        for d in diags:
            steps = d.get("steps") or []
            rows.append([P(f"{d.get('id')}: {d.get('fault_type')} ({d.get('cause_label')}, {round(100 * float(d.get('confidence') or 0))} %)", "cell"),
                         P(whole_sentences(_why(steps) or str(d.get("summary") or ""), 240), "cell"),
                         P(whole_sentences(_todo(steps), 240), "cell")])
        tbl = Table(rows, colWidths=[52 * mm, 64 * mm, 58 * mm])
        tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d9dde3")), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef4fb")), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(tbl)
    ev = (ctx.get("evaluation") or {}).get("headline") or []
    if ev:
        story.append(P(W["eval"], "h"))
        for h in ev[:2]:
            pct = lambda v: "-" if v is None else f"{100 * float(v):.0f} %"  # noqa: E731
            story.append(P(f"• {h['column']}: precision {pct(h['precision'])}, recall {pct(h['recall'])}, false alarms {pct(h['false_alarm'])} of {h.get('n_normal') or 0} normal groups, detected {pct(h['detection'])} of {h.get('n_abnormal') or 0}."))
    flow = (ctx.get("dataflow") or {}).get("statement") if isinstance(ctx.get("dataflow"), dict) else None
    if flow:
        story.append(P(W["flow"], "h"))
        story.append(P(whole_sentences(str(flow), 360)))
    story.append(Spacer(1, 4))
    story.append(P(W["more"].format(lang=lang), "sub"))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=12 * mm, title=W["title"])
    frame_h = A4[1] - 26 * mm
    doc.build([KeepInFrame(A4[0] - 32 * mm, frame_h, story, mode="shrink")])  # one page, whatever the content
    return buf.getvalue()


def generate_summary(ws: Workspace, settings: Optional[Settings] = None, lang: str = "en", out_path: Optional[str | Path] = None, context: Optional[dict[str, Any]] = None, log: bool = True) -> Path:
    settings = settings or ws.settings or get_settings()
    lang = normalize_lang(lang)
    ctx = context if context is not None and context.get("lang") == lang else export_context(ws, settings, lang)
    out = Path(out_path) if out_path else summary_path(ws, lang)
    write_atomic_bytes(out, render_summary(ctx))
    if log:
        log_export(ws, out, lang, "summary", {"pages": 1})
    return out
