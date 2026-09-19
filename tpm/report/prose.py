"""Text helpers for the report (agent F): everything a reader sees must be prose, never a JSON / dict dump.

    clean_text(s)                      drop "[llm-local:...] {json}" fragments a model pasted into a statement, keep the sentence
    strip_lead(s)                      "Yes: 5 duplicate rows ..." -> "5 duplicate rows ..."
    whole_sentences(s, max_chars)      shorten only at a sentence boundary (never mid-sentence)
    parse_narrative(data, text, ...)   the report_narrative JSON shape -> paragraphs, sections, uncertainty, references
    detector_label(detector, t)        "ensemble:pca+iforest+... (summary)" -> "ensemble of 7 detectors (summary)" + the parts
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any, Callable, Iterable, Optional

_TAG_RE = re.compile(r"\[(?:llm-(?:local|external)[^\]]*|template|code|human)\]")
_TAG_FRAGMENT_RE = re.compile(r"\[(?:llm-[^\]]*|template)\]\s*(?=[{\[])")
_TEXT_KEYS = ("text", "summary", "objection", "message", "answer", "statement", "executive_summary", "body")
_ID_RE = re.compile(r"^(?:EV|FLAG|DIAG|CHK|INF|RULE|PATTERN|EGR)-[A-Z0-9]+$")
_SENTENCE_END_RE = re.compile(r"[.!?…](?:[\"'”’)\]]*)(?=\s|$)")
_LEAD_RE = re.compile(r"^\s*(?:yes|no|unclear|uncertain)\s*[:.,;!–—-]\s*", re.I)
_MID_LEAD_RE = re.compile(r"([:.;]\s+)(?:yes|no|unclear|uncertain)\s*:\s*(\S)", re.I)
# a model sometimes "cites" the payload keys it read from: "... 26 failed (overview, quality)." -> drop those
_PAYLOAD_CITE_RE = re.compile(r"\s*\((?:\s*(?:overview|quality|report_sections|flags|diagnoses|untrusted_batches|instructions)\s*,?)+\)", re.I)


# ----------------------------------------------------------------------------- cleaning
def _balanced_end(s: str, start: int) -> int:
    """Index just past the bracket that closes the one at `start`; -1 when unbalanced (e.g. truncated)."""
    depth = 0
    quote: Optional[str] = None
    i = start
    while i < len(s):
        ch = s[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            # an apostrophe inside a word (sensor's) is not a quote
            if not (ch == "'" and 0 < i < len(s) - 1 and s[i - 1].isalnum() and s[i + 1].isalnum()):
                quote = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _loads_any(fragment: str) -> Any:
    """JSON first, then a Python-literal repr ({'text': "..."}); None when neither parses."""
    try:
        return json.loads(fragment)
    except Exception:
        pass
    try:
        return ast.literal_eval(fragment)
    except Exception:
        return None


def _text_from_fragment(fragment: str) -> str:
    end = _balanced_end(fragment, 0)
    body = fragment[:end] if end > 0 else fragment
    obj = _loads_any(body)
    if isinstance(obj, dict):
        for k in _TEXT_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    for k in _TEXT_KEYS:  # truncated / malformed: take the first complete string value of a readable key
        m = re.search(r"""['"]%s['"]\s*:\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""" % re.escape(k), body)
        if m:
            raw = m.group(1) if m.group(1) is not None else m.group(2)
            return re.sub(r"\\(.)", lambda x: " " if x.group(1) == "n" else x.group(1), raw).strip()
    return ""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def clean_text(s: Any) -> str:
    """Readable sentences only: JSON / dict fragments pasted by a model are replaced by their text field (or dropped
    when they only repeat the sentence before them); "[llm-local:...]" source tags are removed."""
    if s is None:
        return ""
    out = str(s)
    for _ in range(8):
        m = _TAG_FRAGMENT_RE.search(out)
        if not m:
            break
        start = m.end()
        end = _balanced_end(out, start)
        fragment = out[start : end if end > 0 else len(out)]
        before = out[: m.start()].strip()
        after = out[end:] if end > 0 else ""
        inner = _text_from_fragment(fragment)
        dup = bool(inner and before and (_norm(inner)[:80] in _norm(before) or _norm(before)[:80] in _norm(inner)))
        out = before + ((("\n\n" if before else "") + inner) if inner and not dup else "") + after
    bare = out.strip()
    if bare[:1] in "{[":
        end = _balanced_end(bare, 0)
        out = _text_from_fragment(bare) + (bare[end:] if end > 0 else "")
    out = _TAG_RE.sub("", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"  +", " ", out)
    return out.strip()


def strip_lead(s: Any) -> str:
    """The verdict word is shown once (headline / badge); the reason must read as its own sentence."""
    text = clean_text(s)
    prev = None
    while prev != text:
        prev = text
        text = _LEAD_RE.sub("", text, count=1)
    text = _MID_LEAD_RE.sub(lambda m: m.group(1) + (m.group(2).upper() if m.group(1).strip() == "." else m.group(2)), text)
    text = text.strip()
    return text[:1].upper() + text[1:] if text else text


def paragraphs(s: Any) -> list[str]:
    return [re.sub(r"\s*\n\s*", " ", p).strip() for p in re.split(r"\n\s*\n", clean_text(s)) if p.strip()]


def whole_sentences(s: Any, max_chars: int = 0, require_end: bool = False) -> str:
    """Shorten to at most max_chars at a sentence boundary. With require_end, a trailing fragment that does not end
    a sentence (a reply cut by the token limit) is dropped. Returns "" when no whole sentence fits."""
    text = re.sub(r"\s+", " ", "" if s is None else str(s)).strip()
    if not text:
        return ""
    too_long = bool(max_chars) and len(text) > max_chars
    ends_clean = bool(re.search(r"[.!?…][\"'”’)\]]*$", text))
    if not too_long and (ends_clean or not require_end):
        return text
    limit = max_chars if too_long else len(text)
    cut = 0
    for m in _SENTENCE_END_RE.finditer(text):
        if m.end() > limit:
            break
        # "e.g." / "approx. 3" / "1.5": a period followed by a lowercase letter or a digit does not end a sentence
        nxt = text[m.end() :].lstrip()[:1]
        if nxt and (nxt.islower() or nxt.isdigit()):
            continue
        cut = m.end()
    return text[:cut].strip()


# ----------------------------------------------------------------------------- model narrative
def _strip_fences(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```\s*$", t, re.S)
    return m.group(1) if m else t


def _looks_like_json(text: str) -> bool:
    t = _strip_fences(text)
    return t[:1] in "{[" or bool(re.search(r"""["'](?:executive_summary|sections|heading)["']\s*:""", t))


def _json_string(raw: str) -> str:
    try:
        return json.loads('"' + raw + '"')
    except Exception:
        return re.sub(r"\\(.)", lambda x: " " if x.group(1) == "n" else x.group(1), raw)


def _salvage(text: str) -> Optional[dict[str, Any]]:
    """A reply cut by the token limit is not valid JSON. Keep every complete string value and the whole sentences
    of the value that was cut; nothing is shown mid-sentence."""
    t = _strip_fences(text)
    out: dict[str, Any] = {"sections": [], "uncertainty": [], "truncated": True}
    m = re.search(r'"executive_summary"\s*:\s*"((?:[^"\\]|\\.)*)("?)', t)
    if m:
        val = _json_string(m.group(1))
        out["executive_summary"] = val if m.group(2) else whole_sentences(val, require_end=True)
    for sm in re.finditer(r'"heading"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"body"\s*:\s*"((?:[^"\\]|\\.)*)("?)', t):
        body = _json_string(sm.group(2))
        if not sm.group(3):
            body = whole_sentences(body, require_end=True)
        ids: list[str] = []
        tail = t[sm.end() : sm.end() + 600]
        em = re.match(r'\s*,\s*"evidence_ids"\s*:\s*\[([^\]]*)\]', tail)
        if em:
            ids = re.findall(r'"([^"]+)"', em.group(1))
        if body:
            out["sections"].append({"heading": _json_string(sm.group(1)), "body": body, "evidence_ids": ids})
    um = re.search(r'"uncertainty"\s*:\s*\[(.*?)\]', t, re.S)
    if um:
        out["uncertainty"] = [_json_string(x) for x in re.findall(r'"((?:[^"\\]|\\.)*)"', um.group(1))]
    return out if out.get("executive_summary") or out["sections"] else None


def _as_text(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return "\n\n".join(_as_text(x) for x in v if x)
    if isinstance(v, dict):
        for k in _TEXT_KEYS:
            if isinstance(v.get(k), str) and v[k].strip():
                return v[k]
    return ""


def _prose_paragraphs(v: Any, require_end: bool) -> list[str]:
    out = []
    for p in paragraphs(_as_text(v)):
        if _looks_like_json(p):
            continue
        p = whole_sentences(_PAYLOAD_CITE_RE.sub("", p), require_end=require_end)
        if p:
            out.append(p)
    return out


def parse_narrative(data: Any, text: Any = "", known_ids: Optional[Iterable[str]] = None) -> Optional[dict[str, Any]]:
    """LLMResult.data / .text of the report_narrative task -> {"summary": [paragraph], "sections": [{"heading",
    "paragraphs", "refs"}], "uncertainty": [sentence], "refs": [id], "confidence", "truncated"}.
    None when nothing readable can be extracted (the caller then omits the model section; JSON is never printed)."""
    text = "" if text is None else str(text)
    obj: Any = data if isinstance(data, dict) and data else None
    if isinstance(obj, dict) and set(obj.keys()) == {"result"}:  # router wraps non-dict JSON as {"result": ...}
        obj = obj["result"] if isinstance(obj["result"], dict) else None
    if obj is None and text.strip():
        t = _strip_fences(text)
        start = t.find("{")
        if start >= 0:
            end = _balanced_end(t, start)
            if end > 0:
                cand = _loads_any(t[start:end])
                obj = cand if isinstance(cand, dict) else None
        if obj is None:
            if _looks_like_json(t):
                obj = _salvage(t)
            else:  # the model answered in plain prose
                obj = {"executive_summary": t}
    if not isinstance(obj, dict):
        return None
    truncated = bool(obj.get("truncated"))
    known = set(known_ids) if known_ids is not None else None

    def refs_of(v: Any) -> list[str]:
        ids = [str(x).strip() for x in (v if isinstance(v, (list, tuple)) else [])]
        return [i for i in dict.fromkeys(ids) if _ID_RE.match(i) and (known is None or i in known)]

    summary = _prose_paragraphs(obj.get("executive_summary") or obj.get("summary") or obj.get("text") or "", truncated)
    sections = []
    for s in obj.get("sections") or []:
        if isinstance(s, str):
            s = {"heading": "", "body": s}
        if not isinstance(s, dict):
            continue
        paras = _prose_paragraphs(s.get("body") or s.get("text") or s.get("content") or "", truncated)
        if not paras:
            continue
        heading = re.sub(r"\s+", " ", clean_text(s.get("heading") or s.get("title") or "")).strip().rstrip(":")
        sections.append({"heading": heading[:120], "paragraphs": paras, "refs": refs_of(s.get("evidence_ids"))})
    unc_raw = obj.get("uncertainty") or []
    if isinstance(unc_raw, str):
        unc_raw = [unc_raw]
    uncertainty = [p for u in unc_raw if isinstance(u, (str, dict)) for p in _prose_paragraphs(u, truncated)]
    if not summary and not sections:
        return None
    if not summary:  # lead with the first section rather than with nothing
        summary = sections[0]["paragraphs"][:1]
        sections[0]["paragraphs"] = sections[0]["paragraphs"][1:]
        sections = [s for s in sections if s["paragraphs"]]
    conf = obj.get("confidence")
    try:
        conf = float(conf) if conf is not None and not isinstance(conf, bool) else None
        if conf is not None and not (0.0 <= conf <= 1.0):
            conf = None
    except Exception:
        conf = None
    all_refs = list(dict.fromkeys(r for s in sections for r in s["refs"]))
    return {"summary": summary, "sections": sections, "uncertainty": uncertainty, "refs": all_refs, "confidence": conf, "truncated": truncated}


# ----------------------------------------------------------------------------- detector label
def detector_label(detector: Any, t: Optional[Callable[..., str]] = None) -> dict[str, Any]:
    """'ensemble:autoencoder+pca+robust_z+iforest (summary)' -> short 'ensemble of 4 detectors (summary)', parts
    ['autoencoder', 'pca', ...]; the full string stays available for a title attribute."""
    full = "" if detector is None else str(detector).strip()
    if not full:
        return {"short": "", "full": "", "parts": [], "family": "", "note": ""}
    tr = t or (lambda key, **kw: {"detector_ensemble": "ensemble of {n} detectors", "detector_changepoints": "change-point detection ({n} methods)", "detector_cascade": "cascade (lag order)", "detector_summary": "summary"}.get(key, key).format(**kw))
    note = ""
    core = full
    m = re.search(r"\s+\(([^()]*)\)\s*$", full)  # a trailing remark such as " (summary)", not "ensemble(a,b)"
    if m and not re.search(r"[+,]", m.group(1)):
        note, core = m.group(1).strip(), full[: m.start()].strip()
    fm = re.match(r"^([^:(]+)[:(](.*?)\)?$", core)  # "ensemble:a+b" and "ensemble(a,b)"
    family, rest = (fm.group(1), fm.group(2)) if fm else (core, "")
    parts = [p.strip() for p in re.split(r"[+,]", rest) if p.strip()] if rest else []
    fam = family.strip().lower()
    if fam == "ensemble" and parts:
        short = tr("detector_ensemble", n=len(parts)) if len(parts) > 1 else parts[0]
    elif fam in ("changepoints", "changepoint") and parts:
        short = tr("detector_changepoints", n=len(parts))
    elif fam == "cascade":
        short = tr("detector_cascade")
    elif len(parts) > 2:
        short = f"{family.strip()} ({len(parts)})"
    else:
        short = core if len(core) <= 28 else core[:27] + "…"
    if note:
        short += f" ({tr('detector_summary') if note.lower() == 'summary' else note})"
    return {"short": short, "full": full, "parts": parts, "family": family.strip(), "note": note}
