"""E-mail a report over SMTP. Server settings come from the environment variables named in
settings.report.smtp (TPM_SMTP_HOST, TPM_SMTP_PORT, TPM_SMTP_USER, TPM_SMTP_PASSWORD, TPM_SMTP_FROM)."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Optional

from ..config import Settings, get_settings
from ..workspace import Workspace
from .i18n import Translator, normalize_lang
from .report import ensure_report, generate_report, report_path


class SmtpNotConfigured(RuntimeError):
    pass


def smtp_config(settings: Settings) -> dict[str, Any]:
    s = settings.report.smtp
    host = os.environ.get(s.host_env, "").strip()
    if not host:
        raise SmtpNotConfigured(f"SMTP is not configured: set {s.host_env} (and optionally {s.port_env}, {s.user_env}, {s.password_env}, {s.from_env}) in .env")
    port = int(os.environ.get(s.port_env, "587") or 587)
    user = os.environ.get(s.user_env, "").strip()
    password = os.environ.get(s.password_env, "")
    sender = os.environ.get(s.from_env, "").strip() or user or f"tpm@{host}"
    return {"host": host, "port": port, "user": user, "password": password, "from": sender}


_ATTACH_TYPES = {".pdf": ("application", "pdf"), ".pptx": ("application", "vnd.openxmlformats-officedocument.presentationml.presentation")}


def build_message(ws: Workspace, settings: Settings, to: list[str], lang: str, path: Path, sender: str, subject: Optional[str] = None, extra_attachments: Optional[list[Path]] = None) -> EmailMessage:
    t = Translator(lang)
    meta = ws.read_json("meta", {}) or {}
    msg = EmailMessage()
    msg["Subject"] = subject or t("email_subject", run_id=ws.run_id)
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.set_content(t("email_body", run_id=ws.run_id, lang=t("lang_name"), source=Path(str(meta.get("source_path", ""))).name, profile=meta.get("profile", settings.profile)))
    with open(path, "rb") as f:
        msg.add_attachment(f.read(), maintype="text", subtype="html", filename=path.name)
    for extra in extra_attachments or []:  # the PDF (and, when asked for, the deck) next to the HTML report
        maintype, subtype = _ATTACH_TYPES.get(Path(extra).suffix.lower(), ("application", "octet-stream"))
        with open(extra, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=f"tpm_{ws.run_id}_{lang}{Path(extra).suffix.lower()}")
    return msg


def email_report(ws: Workspace, settings: Any = None, to: Any = None, lang: str = "en", subject: Optional[str] = None, regenerate: bool = False, use_llm: bool = False, attach_pdf: bool = False, attach_pptx: bool = False) -> dict[str, Any]:
    """Send report_<lang>.html to one or more recipients; with attach_pdf / attach_pptx the PDF / the deck of the same
    language is attached too (generated on demand, cached). Raises SmtpNotConfigured with a clear message.

    Accepts both email_report(ws, settings, to, lang) and the API's shorter email_report(ws, to, lang)."""
    if isinstance(settings, (str, list)):  # called as (ws, to, lang)
        settings, to, lang = None, settings, (to if isinstance(to, str) and to else lang)
    settings = settings or ws.settings or get_settings()
    lang = normalize_lang(lang)
    recipients = [x.strip() for x in ([to] if isinstance(to, str) else to) for x in x.split(",") if x.strip()]
    if not recipients:
        raise ValueError("no recipient given")
    cfg = smtp_config(settings)
    path = report_path(ws, lang)
    if use_llm:  # explicit request: wait (bounded) for the model summary
        if regenerate or not path.exists():
            path = generate_report(ws, settings, lang, use_llm=True)
    else:  # send a report that matches the artifacts; a stored model summary is kept, the model is not called
        ensure_report(ws, settings, lang, use_llm=True, force=regenerate, ask_model=False)
    extras: list[Path] = []
    if attach_pdf or attach_pptx:
        from .export_common import ensure_export

        for fmt, wanted in (("pdf", attach_pdf), ("pptx", attach_pptx)):
            if wanted:
                extras.append(Path(ensure_export(ws, settings, lang, fmt, force=regenerate)["path"]))
    msg = build_message(ws, settings, recipients, lang, path, cfg["from"], subject, extra_attachments=extras)
    if cfg["port"] == 465:
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30)
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
    try:
        if cfg["port"] != 465:
            try:
                server.starttls()
            except smtplib.SMTPNotSupportedError:
                pass
        if cfg["user"]:
            server.login(cfg["user"], cfg["password"])
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass
    ws.log.record("system:report", "email", "report", path.name, {"to": recipients, "lang": lang, "host": cfg["host"], "attachments": [path.name] + [x.name for x in extras]})
    return {"sent": True, "to": recipients, "lang": lang, "path": str(path), "host": cfg["host"], "attachments": [path.name] + [x.name for x in extras]}
