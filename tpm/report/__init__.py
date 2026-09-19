"""report stage (agent F): self-contained HTML report in EN/FI/SV, PDF and PowerPoint exports, optional e-mail.

    run_report(ws, settings, ctx)                     pipeline stage -> report_<lang>.html
    generate_report(ws, settings, lang, use_llm=True) -> Path
    email_report(ws, settings, to, lang)              -> dict (raises SmtpNotConfigured with a clear message)
    ensure_report(ws, settings, lang)                 -> status dict; cached per language, never waits for the model (API)
    report_status(ws, lang)                           -> {"exists", "fresh", "llm": ready|pending|none, ...}
    report_path(ws, lang)                             -> Path
    collect(ws, settings, lang)                       -> template context (dicts only)
    generate_pdf(ws, settings, lang, out_path=None)   -> Path  (A4 document, ReportLab; tpm/report/pdf.py)
    generate_pptx(ws, settings, lang, out_path=None)  -> Path  (16:9 deck, python-pptx; tpm/report/pptx_export.py)
    ensure_export(ws, settings, lang, fmt)            -> {"path", "filename", "media_type", ...}; cached per language
                                                         and format with the stamp ensure_report() uses (API)
"""
from .email import SmtpNotConfigured, email_report, smtp_config  # noqa: F401
from .export_common import MEDIA_TYPES, download_name, ensure_export, export_context, export_path  # noqa: F401
from .i18n import Translator, available_languages, normalize_lang  # noqa: F401
from .report import collect, ensure_report, generate_report, render_html, report_path, report_status, run_report  # noqa: F401



def generate_pdf(*args, **kwargs):
    """See tpm.report.pdf.generate_pdf (imported on first use: ReportLab is only needed for the PDF)."""
    from .pdf import generate_pdf as _f

    return _f(*args, **kwargs)


def generate_pptx(*args, **kwargs):
    """See tpm.report.pptx_export.generate_pptx (imported on first use: python-pptx is only needed for the deck)."""
    from .pptx_export import generate_pptx as _f

    return _f(*args, **kwargs)


__all__ = ["generate_pdf", "generate_pptx", "ensure_export", "export_context", "export_path", "download_name", "MEDIA_TYPES", "run_report", "generate_report", "ensure_report", "report_status", "email_report", "report_path", "collect", "render_html", "available_languages", "normalize_lang", "Translator", "SmtpNotConfigured", "smtp_config"]
