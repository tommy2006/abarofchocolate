"""Shared plumbing of the PDF and PowerPoint exports (agent F).

    export_context(ws, settings, lang)            the report context (collect()) with the STORED model summary only
    ensure_export(ws, settings, lang, fmt)        API entry point: cached per language + format, same invalidation
                                                  stamp as ensure_report(); never calls the language model
    export_path(ws, lang, fmt) / download_name(run_id, lang, fmt) / MEDIA_TYPES
    headline_numbers(ctx) / interesting_signals(ctx) / top_diagnoses(ctx) / open_points(ctx) / next_steps(ctx)
                                                  the derived "what matters" lists both documents show
    fit_text(text, max_chars)                     shorten at a sentence boundary, then at a word, with an ellipsis

Both documents are built from the report CONTEXT (derived artifacts and aggregates): no raw rows, no long series.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from ..config import Settings, get_settings
from ..workspace import Workspace
from .i18n import normalize_lang
from .prose import whole_sentences

FORMATS = ("pdf", "pptx")
MEDIA_TYPES = {
    "html": "text/html",
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
ELLIPSIS = "…"
_ID_RE = re.compile(r"\b(?:EV|FLAG|DIAG|CHK|INF|RULE|PATTERN|EGR|REC)-[A-Z0-9]+\b")


# ----------------------------------------------------------------------------- paths
def export_path(ws: Workspace, lang: str, fmt: str) -> Path:
    return ws.dir / f"report_{normalize_lang(lang)}.{fmt}"


def download_name(run_id: str, lang: str, fmt: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(run_id)).strip("_") or "run"
    return f"tpm_{safe}_{normalize_lang(lang)}.{fmt}"


# ----------------------------------------------------------------------------- context
def export_context(ws: Workspace, settings: Optional[Settings] = None, lang: str = "en") -> dict[str, Any]:
    """collect() plus the model-written summary when ensure_report()/generate_report() already stored one for the
    current findings. The model is never called from here."""
    from . import report as _report

    settings = settings or ws.settings or get_settings()
    lang = normalize_lang(lang)
    narrative = None
    if _report._llm_enabled(True):
        try:
            narrative = _report._load_narrative(ws, lang)
        except Exception:
            narrative = None
    return _report.collect(ws, settings, lang, use_llm=False, narrative=narrative)


# ----------------------------------------------------------------------------- cache
_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}
_STAMP_FILE = "report_exports.json"


def _lock_for(ws: Workspace, lang: str, fmt: str) -> threading.Lock:
    key = f"{ws.dir}|{lang}|{fmt}"
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def _read_stamps(ws: Workspace) -> dict[str, Any]:
    try:
        with open(ws.dir / _STAMP_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_stamp(ws: Workspace, key: str, value: dict[str, Any]) -> None:
    from .report import _atomic_write

    with _locks_guard:  # one writer at a time in this process; the file is only a cache index
        d = _read_stamps(ws)
        d[key] = value
        try:
            _atomic_write(ws.dir / _STAMP_FILE, json.dumps(d, ensure_ascii=False, indent=1))
        except Exception:
            pass


def export_stamp(ws: Workspace, lang: str) -> str:
    """The stamp ensure_report() uses (artifacts, human decisions, report code / template / dictionaries) plus whether
    a model summary is stored for this language: the export is rebuilt when the summary arrives."""
    from . import report as _report

    have = False
    if _report._llm_enabled(True):
        try:
            n = _report._load_narrative(ws, lang)
            have = bool(n and n.get("status") == "ok")
        except Exception:
            have = False
    return f"{_report.report_fingerprint(ws)}|llm={'ready' if have else 'none'}"


def _generator(fmt: str):
    if fmt == "pdf":
        from .pdf import generate_pdf

        return generate_pdf
    if fmt == "pptx":
        from .pptx_export import generate_pptx

        return generate_pptx
    raise ValueError(f"unknown export format {fmt!r}; expected one of {', '.join(FORMATS)}")


def ensure_export(ws: Workspace, settings: Optional[Settings] = None, lang: str = "en", fmt: str = "pdf", force: bool = False) -> dict[str, Any]:
    """Generate report_<lang>.<fmt> on demand and cache it. Returns {"path", "filename", "media_type", "lang", "format",
    "regenerated", "seconds", "bytes"}. A cached file is reused while the artifacts, the human decisions, the stored
    model summary and the report code are unchanged. Never waits for (or starts) the language model."""
    fmt = str(fmt).lower().lstrip(".")
    gen = _generator(fmt)
    settings = settings or ws.settings or get_settings()
    lang = normalize_lang(lang)
    out = export_path(ws, lang, fmt)
    key = f"{fmt}_{lang}"
    t0 = time.time()
    regenerated = False
    with _lock_for(ws, lang, fmt):
        stamp = export_stamp(ws, lang)
        cached = _read_stamps(ws).get(key) or {}
        if force or not out.exists() or cached.get("stamp") != stamp or out.stat().st_size != cached.get("bytes"):
            gen(ws, settings, lang, out_path=out)
            regenerated = True
            _write_stamp(ws, key, {"stamp": stamp, "bytes": out.stat().st_size, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())})
    return {"path": str(out), "filename": download_name(ws.run_id, lang, fmt), "media_type": MEDIA_TYPES[fmt], "lang": lang, "format": fmt, "regenerated": regenerated, "seconds": round(time.time() - t0, 2), "bytes": out.stat().st_size}


def write_atomic_bytes(out: Path, data: bytes) -> None:
    """Unique temp name + replace with retries (Windows refuses while a reader has the old file open)."""
    import os

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    for i in range(60):
        try:
            tmp.replace(out)
            return
        except PermissionError:
            time.sleep(0.02 + 0.01 * i)
    try:
        with open(out, "wb") as f:
            f.write(data)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def log_export(ws: Workspace, out: Path, lang: str, fmt: str, extra: Optional[dict[str, Any]] = None) -> None:
    try:
        ws.log.record("system:report", "report_export", "report", out.name, {"lang": lang, "format": fmt, "bytes": out.stat().st_size, **(extra or {})})
    except Exception:
        pass


# ----------------------------------------------------------------------------- text
def fit_text(text: Any, max_chars: int) -> tuple[str, bool]:
    """(text that fits in max_chars, was it shortened). Cuts at a sentence boundary when one fits in the budget,
    otherwise at a word boundary; a shortened text always ends with an ellipsis."""
    s = re.sub(r"\s+", " ", "" if text is None else str(text)).strip()
    if max_chars <= 0:
        return "", bool(s)
    if len(s) <= max_chars:
        return s, False
    room = max(1, max_chars - 2)
    cut = whole_sentences(s, room)
    if cut and len(cut) >= min(40, room // 3):
        return cut + " " + ELLIPSIS, True
    head = s[:room]
    sp = head.rfind(" ")
    if sp >= room // 2:
        head = head[:sp]
    return head.rstrip(" ,;:-–—(") + ELLIPSIS, True


def ids_in(*texts: Any) -> list[str]:
    out: list[str] = []
    for x in texts:
        if isinstance(x, (list, tuple, set)):
            out.extend(ids_in(*x))
        elif x:
            out.extend(_ID_RE.findall(str(x)))
    return list(dict.fromkeys(out))


# ----------------------------------------------------------------------------- derived lists
def headline_numbers(ctx: dict[str, Any]) -> list[tuple[str, str]]:
    """(label, value) tiles for the title page / summary slide."""
    from .report import _thousands

    t, lang = ctx["t"], ctx["lang"]
    schema = ctx.get("schema") or {}
    q = ctx["quality"]
    out = [
        (t("rows"), _thousands(schema.get("n_rows"), lang) if schema.get("n_rows") is not None else t("na_short")),
        (t("signals"), str(ctx.get("n_signals_total") or 0)),
        (t("groups"), _thousands(schema.get("n_groups"), lang) if schema.get("n_groups") is not None else t("na_short")),
        (t("pdf_failed_checks"), f"{_thousands(q.get('n_fail', 0), lang)} / {_thousands(q.get('n_checks', 0), lang)}"),
        (t("pdf_untrusted_batches"), f"{_thousands(q.get('n_untrusted_total', 0), lang)} / {_thousands(q.get('n_batches', 0), lang)}"),
        (t("pdf_flagged_events"), _thousands(ctx["detect"].get("n_flags", 0), lang)),
        (t("pdf_diagnoses"), _thousands(ctx.get("n_diag_total", 0), lang)),
        (t("pdf_human_decisions"), str(len(ctx.get("human") or []))),
    ]
    if ctx.get("suspicious"):
        out.insert(6, (t("section_suspicious"), _thousands(ctx["suspicious"].get("n_rows") or ctx["suspicious"].get("n_point_flags") or 0, lang)))
    return out


def role_mix(ctx: dict[str, Any]) -> list[tuple[str, int]]:
    c = Counter(str(s.get("role_label") or s.get("role") or ctx["t"]("unknown")) for s in ctx.get("signals") or [])
    return c.most_common(8)


def interesting_signals(ctx: dict[str, Any], n: int = 4) -> list[dict[str, Any]]:
    """The signals a reader should look at first: those that lead flags and diagnoses, then human overrides, then the
    least certain roles. Each comes with its first evidence sentence (plain when available)."""
    sig = {s["id"]: s for s in ctx.get("signals") or []}
    score: Counter = Counter()
    led: Counter = Counter()
    for f in (ctx["detect"].get("flags") or [])[:100]:
        for k, s in enumerate((f.get("signals_ranked") or [])[:2]):
            score[str(s.get("signal"))] += (2 - k) * float(f.get("severity") or 0.5)
            if k == 0:
                led[str(s.get("signal"))] += 1
    for d in (ctx.get("diagnoses") or [])[:30]:
        for s in (d.get("ranked") or d.get("ranked_signals") or [])[:1]:
            score[str(s.get("signal"))] += 1.5
    for s in sig.values():
        if s.get("human_role_override"):
            score[s["id"]] += 1.0
    order = [k for k, _ in score.most_common() if k in sig]
    rest = sorted((s for s in sig.values() if s["id"] not in score and not s.get("excluded")), key=lambda s: float(s.get("structural_confidence") or 0))
    order += [s["id"] for s in rest]
    out = []
    for sid in order[:n]:
        s = sig[sid]
        ev = (s.get("evidence") or [{}])[0] if s.get("evidence") else {}
        out.append({"id": sid, "role_label": s.get("role_label"), "confidence": s.get("structural_confidence"), "instrument": s.get("instrument"), "instrument_confidence": s.get("instrument_confidence"), "sentence": ev.get("plain") or ev.get("technical") or "", "evidence_ids": (s.get("evidence_ids") or [])[:3], "why": "flags" if sid in score else "uncertain", "n_led": led.get(sid, 0)})
    return out


def top_diagnoses(ctx: dict[str, Any], n: int = 3) -> list[dict[str, Any]]:
    """The first n full diagnoses (collect() already orders large runs by flag severity), preferring distinct fault
    types so that three slides do not tell the same story."""
    full = [d for d in ctx.get("diagnoses") or [] if not d.get("compact")]
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in full:
        key = f"{d.get('fault_type')}|{d.get('cause_class')}"
        if key in seen:
            continue
        seen.add(key)
        picked.append(d)
        if len(picked) == n:
            return picked
    for d in full:
        if d not in picked:
            picked.append(d)
            if len(picked) == n:
                break
    return picked


def cause_split(ctx: dict[str, Any]) -> dict[str, int]:
    """Flag counts by cause class over ALL flags of the run: sensor+data / process / other."""
    t = ctx["t"]
    lab = {t.cause(k): k for k in ("process", "sensor", "data", "mixed", "unknown")}
    out = {"sensor": 0, "process": 0, "other": 0}
    for label, n in ctx["detect"].get("flags_by_cause") or []:
        k = lab.get(label, "unknown")
        out["process" if k == "process" else ("sensor" if k in ("sensor", "data") else "other")] += int(n)
    return out


def open_points(ctx: dict[str, Any], limit: int = 5) -> list[str]:
    t = ctx["t"]
    out: list[str] = []
    llm = ctx.get("llm")
    diags = ctx.get("diagnoses") or []
    verdicts = Counter((d.get("critique") or {}).get("verdict") for d in diags)
    if verdicts.get("weakened") or verdicts.get("rejected"):
        out.append(t("px_unc_critique", n_weak=verdicts.get("weakened", 0), n_rej=verdicts.get("rejected", 0), n=len(diags)))
    split = cause_split(ctx)
    if split["other"]:
        out.append(t("px_unc_unknown_cause", n=split["other"]))
    if ctx.get("suspicious"):
        out.append(ctx["suspicious"]["wording"])
    for a in (ctx["detect"].get("baseline_assumptions") or [])[:1]:
        out.append(str(a))
    if not ctx.get("evaluation"):
        out.append(t("px_unc_no_labels"))
    seen = {x.lower() for x in out}
    for d in diags[:3]:
        for u in (d.get("uncertainty") or [])[:1]:
            if u.lower() not in seen:
                seen.add(u.lower())
                out.append(u)
    if llm:
        for u in (llm.get("uncertainty") or [])[:1]:
            if u.lower() not in seen:
                out.append(u)
    return out[:limit] or [t("px_unc_none")]


def next_steps(ctx: dict[str, Any], limit: int = 5) -> list[str]:
    t = ctx["t"]
    out: list[str] = []
    if ctx.get("n_diag_total"):
        out.append(t("px_next_review", n=ctx["n_diag_total"]))
    if ctx["quality"].get("n_untrusted_total"):
        out.append(t("px_next_untrusted", n=ctx["quality"]["n_untrusted_total"]))
    if ctx.get("suspicious"):
        out.append(t("px_next_suspicious"))
    for r in ((ctx.get("assessor") or {}).get("recommendations") or [])[:1]:
        if r.get("action"):
            out.append(t("px_next_assessor", action=str(r["action"]).rstrip(".")))
    if ctx["detect"].get("baseline_items"):
        out.append(t("px_next_baseline"))
    n_low = sum(1 for s in ctx.get("signals") or [] if float(s.get("structural_confidence") or 0) < 0.6 and not s.get("excluded"))
    if n_low:
        out.append(t("px_next_roles", n=n_low))
    out.append(t("px_next_monitor"))
    return out[:limit]
