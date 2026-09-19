"""report stage (agent F): self-contained HTML report in EN/FI/SV, optional e-mail.

    run_report(ws, settings, ctx)                     pipeline stage -> report_<lang>.html
    generate_report(ws, settings, lang, use_llm=True) -> Path
    email_report(ws, settings, to, lang)              -> dict (raises SmtpNotConfigured with a clear message)
    ensure_report(ws, settings, lang)                 -> status dict; cached per language, never waits for the model (API)
    report_status(ws, lang)                           -> {"exists", "fresh", "llm": ready|pending|none, ...}
    report_path(ws, lang)                             -> Path
    collect(ws, settings, lang)                       -> template context (dicts only)
"""
from .email import SmtpNotConfigured, email_report, smtp_config  # noqa: F401
from .i18n import Translator, available_languages, normalize_lang  # noqa: F401
from .report import collect, ensure_report, generate_report, render_html, report_path, report_status, run_report  # noqa: F401

__all__ = ["run_report", "generate_report", "ensure_report", "report_status", "email_report", "report_path", "collect", "render_html", "available_languages", "normalize_lang", "Translator", "SmtpNotConfigured", "smtp_config"]
