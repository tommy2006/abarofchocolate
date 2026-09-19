"""Names a person gave to signals ("S44" -> "possibly broken").

The pipeline works on aliases (S01..) so nothing depends on file headers; artifacts, evidence statements and
diagnoses therefore say "S44". When an operator renames a signal (decision `set_name` on the signal, logged like
every human decision, stored as `display_name` in signals.json), everything a person reads should say
"possibly broken (S44)": the alias stays visible so the text can still be matched to the evidence.

The web UI does this where it turns ids into links; this module does it for text produced on the server
(report in HTML / PDF / PowerPoint, e-mail). Only prose is rewritten, never ids, anchors or keys.
"""
from __future__ import annotations

import re
from typing import Any

# an alias that stands on its own: not part of a longer word or id, and not already inside "(S44)" / "[S44]"
ALIAS_RE = re.compile(r"(?<![\w(\[#/\"'=-])S\d{2,5}(?![\w)\]])")


def clean_name(name: Any) -> str:
    """What may be stored as a display name: one line, no control characters, at most 80 characters."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(name or ""))
    return re.sub(r"\s+", " ", s).strip()[:80]


def operator_names(ws: Any) -> dict[str, str]:
    """{alias: display name} for the signals somebody named. Empty when nothing was renamed."""
    try:
        raw = ws.read_json("signals") or []
    except Exception:
        return {}
    items = raw.get("signals", []) if isinstance(raw, dict) else raw
    out: dict[str, str] = {}
    for s in items or []:
        if isinstance(s, dict) and s.get("id") and s.get("display_name"):
            name = clean_name(s["display_name"])
            if name and name != s["id"]:
                out[str(s["id"])] = name
    return out


def label(signal_id: str, names: dict[str, str]) -> str:
    name = names.get(signal_id)
    return f"{name} ({signal_id})" if name else signal_id


def expand_text(text: str, names: dict[str, str]) -> str:
    if not names or not text or "S" not in text:
        return text
    return ALIAS_RE.sub(lambda m: label(m.group(0), names), text)


def expand(obj: Any, names: dict[str, str]) -> Any:
    """Copy of a JSON-like structure with aliases expanded in prose. A string counts as prose when it contains a
    space; bare ids ("S44"), anchors and dictionary keys are left alone so links and lookups keep working."""
    if not names:
        return obj
    if isinstance(obj, str):
        return expand_text(obj, names) if " " in obj else obj
    if isinstance(obj, list):
        return [expand(v, names) for v in obj]
    if isinstance(obj, tuple):
        return tuple(expand(v, names) for v in obj)
    if isinstance(obj, dict):
        return {k: expand(v, names) for k, v in obj.items()}
    return obj
