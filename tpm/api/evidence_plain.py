"""Plain-language layer for evidence and for every other citable object of a run.

Two public entry points, both deterministic (template first, no model call):

* ``explain(ev, lang="en") -> str``: one to three sentences for a person with no data-science background,
  built from an evidence item's kind / values / signals / statement. It says what was observed and why it
  matters, in everyday words. Numbers and ids (S07, B00003, FLAG-000001) are kept exactly so the UI can
  still turn them into links. Unknown kinds fall back to the statement with the jargon glossary applied;
  the result is never empty and never contains raw JSON or dict reprs.
* ``resolve_refs(ws, ids, lang="en") -> (items, missing)``: resolves ANY object id that answers, diagnoses
  and log entries cite (EV-, DIAG-, FLAG-, CHK-, INF-, RULE-, PATTERN-, EGR-, batch ids) into an
  evidence-like item ``{id, kind, ref_type, statement, plain, signals, values, group_id, batch_id,
  evidence_ids, open}`` so the evidence popover can show it and drill down into its own evidence.
  Large artifacts (flags.jsonl, diagnoses.jsonl, checks.jsonl) are indexed once per run and reused until the
  file's mtime or size changes.

``lang`` is reserved: the templates are English; translation is a rewording pass layered on top elsewhere.
"""
from __future__ import annotations

import json
import math
import re
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

__all__ = ["explain", "explain_object", "with_plain", "resolve_refs", "resolve_ref", "dejargon", "KINDS", "REF_KINDS"]


# =====================================================================================================
# small formatting helpers
# =====================================================================================================
def _f(x: Any) -> Optional[float]:
    try:
        if x is None or isinstance(x, bool):
            return None
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _pct(x: Any, d: Optional[int] = None) -> str:
    """0.92 -> '92 %'. Small non-zero shares keep a decimal so they do not read as zero."""
    v = _f(x)
    if v is None:
        return "an unknown share"
    p = v * 100.0
    if d is None:
        a = abs(p)
        d = 0 if (a == 0 or abs(a - round(a)) < 0.05 or (10 <= a < 99)) else (1 if a >= 1 else 2)
    s = f"{p:.{d}f}"
    if d and float(s) == 0 and p != 0:
        s = f"{p:.3f}"
    return f"{s} %"


def _n(x: Any) -> str:
    """Counts: 8005 -> '8,005'."""
    v = _f(x)
    if v is None:
        return str(x)
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    return _num(v)


def _num(x: Any, sig: int = 3) -> str:
    """Measured values: compact, no exponent noise, no float tails."""
    v = _f(x)
    if v is None:
        return str(x)
    if v == 0:
        return "0"
    if abs(v - round(v)) < 1e-9 and abs(v) < 1e15:
        return str(int(round(v)))
    a = abs(v)
    if a >= 1e5:
        return f"{v:,.0f}"
    if a >= 100:
        return f"{v:.1f}".rstrip("0").rstrip(".")
    if a >= 1:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.{sig}g}"


def _x(v: Any) -> str:
    """Multiplier: 2.262 -> '2.3'."""
    f = _f(v)
    if f is None:
        return "?"
    return f"{f:.0f}" if f >= 20 else f"{f:.1f}"


def _dur(seconds: Any) -> str:
    s = _f(seconds)
    if s is None:
        return "an unknown time"
    if s < 1:
        return f"{_num(s)} seconds"
    if s < 120:
        return f"{_num(s)} seconds"
    def _r(x: float) -> str:
        return f"{x:.0f}" if x >= 10 or abs(x - round(x)) < 0.05 else f"{x:.1f}"

    if s < 7200:
        return f"{_r(s / 60)} minutes"
    if s < 172800:
        return f"{_r(s / 3600)} hours"
    return f"{_r(s / 86400)} days"


def _join(items: Iterable[Any], max_items: int = 5, word: str = "and") -> str:
    xs = [str(i) for i in items if i is not None and str(i) != ""]
    more = len(xs) - max_items
    if more > 0:
        xs = xs[:max_items]
    if not xs:
        return ""
    if more > 0:
        return ", ".join(xs) + f" {word} {more} more"
    if len(xs) == 1:
        return xs[0]
    return ", ".join(xs[:-1]) + f" {word} " + xs[-1]


def _paren(*parts: Any) -> str:
    xs = [str(x).strip() for x in parts if x is not None and str(x).strip()]
    return f" ({'; '.join(xs)})" if xs else ""


def _plural(n: Any, one: str, many: Optional[str] = None) -> str:
    v = _f(n)
    return one if v is not None and abs(v - 1) < 1e-9 else (many or one + "s")


def _conf_words(c: Any) -> str:
    v = _f(c)
    if v is None:
        return "confidence unknown"
    w = "very confident" if v >= 0.9 else "fairly confident" if v >= 0.7 else "moderately confident" if v >= 0.5 else "not very confident" if v >= 0.3 else "unsure"
    return f"{w}, {_pct(v, 0)}"


def _strength(r: Any) -> str:
    a = abs(_f(r) or 0.0)
    return "almost perfectly" if a >= 0.9 else "very closely" if a >= 0.75 else "clearly" if a >= 0.5 else "loosely" if a >= 0.3 else "hardly at all"


def _rating(x: Any) -> str:
    v = _f(x)
    if v is None:
        return "unrated"
    return "very good" if v >= 0.9 else "good" if v >= 0.75 else "fair" if v >= 0.55 else "weak" if v >= 0.35 else "poor"


DIRECTION = {"up": "went up", "down": "went down", "stuck": "stopped changing", "noisy": "became noisy", "shifted": "shifted to a new level", "flat": "stopped changing", "oscillating": "started to swing back and forth"}
CAUSE = {
    "data": "a problem with the data itself rather than with the process",
    "sensor": "a faulty instrument rather than a real change in the process",
    "process": "a real change in the process",
    "mixed": "a mix of a process change and data or sensor problems",
    "unknown": "unclear: the system cannot tell whether the process or the measurement is at fault",
}
NORMAL_LIMIT = "the largest deviation still seen in normal operation"
STRATEGY = {
    "robust_covariance": "keep the bulk of the data and set aside the rows that sit far away from everything else",
    "consensus_of_modes": "keep the rows where every signal sits at its most common level",
    "pre_changepoint": "keep the part of each run before anything changes",
    "densest_windows": "keep the stretches of time that look most alike",
    "early_segment": "keep the beginning of each run",
    "reference_period": "use the period an operator marked as normal",
}
CATEGORY = {
    "completeness": ("Completeness (is anything missing?)", "no missing values, empty rows or silent signals were found"),
    "validity": ("Validity (are the values believable?)", "no impossible, out-of-range or wrongly scaled values were found"),
    "consistency": ("Consistency (do the values agree with each other?)", "no frozen signals, duplicates or contradictions between related signals were found"),
    "timeliness": ("Timeliness (is the time line intact?)", "no holes, repeats or disorder in the time stamps were found"),
    "rule": ("Operating rules", "no operating rule was broken"),
}
DELIMITER = {",": "commas", ";": "semicolons", "\t": "tabs", "|": "vertical bars", "whitespace": "spaces", " ": "spaces"}
SIG_RE = re.compile(r"\bS\d{2,3}\b")


class _Ev:
    """Tolerant view of one evidence item (dict or pydantic object)."""

    def __init__(self, ev: Any):
        if hasattr(ev, "model_dump"):
            ev = ev.model_dump()
        d = ev if isinstance(ev, dict) else {}
        self.d = d
        self.kind = str(d.get("kind") or "")
        self.st = str(d.get("statement") or "").strip()
        v = d.get("values")
        self.v: dict[str, Any] = v if isinstance(v, dict) else {}
        sig = d.get("signals")
        self.sigs: list[str] = [str(s) for s in sig] if isinstance(sig, (list, tuple)) else []
        if not self.sigs:
            self.sigs = list(dict.fromkeys(SIG_RE.findall(self.st)))
        mb = re.search(r"\b(B\d{4,6})\b", self.st)
        self.batch = d.get("batch_id") or (mb.group(1) if mb else None)
        self.group = d.get("group_id")
        if self.group is None:
            m = re.search(r"\b[Gg]roup\s+(G?\d{1,6})\b", self.st)
            self.group = m.group(1) if m else None
        self.n = d.get("n_samples")

    @property
    def s0(self) -> str:
        return self.sigs[0] if self.sigs else "The signal"

    def in_batch(self) -> str:
        return f" in batch {self.batch}" if self.batch else ""

    def in_group(self) -> str:
        return f" (group {self.group})" if self.group not in (None, "") else ""

    def where(self) -> str:
        """'rows 960 to 1020' from values.events, or from the statement; '' when unknown."""
        evs = self.v.get("events") or self.v.get("episodes")
        if isinstance(evs, (list, tuple)) and evs and isinstance(evs[0], (list, tuple)) and len(evs[0]) >= 2:
            a, b = evs[0][0], evs[0][1]
            first = f"row {a}" if a == b else f"rows {a} to {b}"
            extra = len(evs) - 1
            return first + (f", and {extra} more {_plural(extra, 'stretch', 'stretches')}" if extra > 0 else "")
        m = re.search(r"rows?\s+(\d+)\s*[-–]\s*(\d+)", self.st)
        if m:
            return f"rows {m.group(1)} to {m.group(2)}"
        m = re.search(r"\brow\s+(\d+)", self.st)
        return f"row {m.group(1)}" if m else ""

    def rows(self, *more: str) -> str:
        """' (detail; rows 960 to 1020)': every non-empty detail inside ONE pair of parentheses."""
        return _paren(*more, self.where())

    def row_span(self) -> Optional[tuple[str, str]]:
        if self.v.get("row_start") is not None and self.v.get("row_end") is not None:
            return str(self.v["row_start"]), str(self.v["row_end"])
        m = re.search(r"[Rr]ows?\s+(\d+)\s*[-–]\s*(\d+)", self.st)
        return (m.group(1), m.group(2)) if m else None


# =====================================================================================================
# jargon glossary (fallback + final safety net)
# =====================================================================================================
def _r_words(m: "re.Match[str]") -> str:
    v = _f(m.group(1))
    if v is None:
        return "in step"
    return f"{_pct(abs(v), 0)} in step" + (" (in opposite directions)" if v < 0 else "")


_GLOSSARY: list[tuple["re.Pattern[str]", Any]] = [
    (re.compile(r"\(\s*Spearman[^)]*\)", re.I), ""),
    (re.compile(r"\bSpearman\s+[+-]?[\d.]+|\bSpearman\b", re.I), ""),
    (re.compile(r"average \|r\|\s*>=\s*([\d.]+)", re.I), lambda m: f"on average at least {_pct(m.group(1), 0)} in step"),
    (re.compile(r"\|r\|\s*>=\s*([\d.]+)", re.I), lambda m: f"at least {_pct(m.group(1), 0)} in step"),
    (re.compile(r"\br\s*=\s*([+-]?\d*\.?\d+)"), _r_words),
    (re.compile(r"\(r\s+([+-]?\d*\.?\d+)\s*->\s*([+-]?\d*\.?\d+)\)"), lambda m: f"(from {_pct(abs(float(m.group(1))), 0)} in step to {_pct(abs(float(m.group(2))), 0)})"),
    (re.compile(r"\bR\s?[2²]\s*=\s*([\d.]+)"), lambda m: f"{_pct(m.group(1), 0)} of its movement explained"),
    (re.compile(r"\bAUROC\b\s*(?:of\s*)?([\d.]+)?", re.I), lambda m: ("a separation quality of " + m.group(1) + " (1.0 = perfectly, 0.5 = no better than chance)") if m.group(1) else "separation quality (1.0 = perfectly)"),
    (re.compile(r"\bp\s*[=<]\s*(0?\.\d+)"), lambda m: f"a {_pct(m.group(1))} chance of being a coincidence"),
    (re.compile(r"\bp-values?\b", re.I), "chance of being a coincidence"),
    (re.compile(r"(\d[\d,.]*(?:e[+-]?\d+)?)\s*(?:robust\s+)?(?:sigma|σ)\b", re.I), r"\1 times its normal spread"),
    (re.compile(r"(\d[\d ,.]*)\s*robust standard deviations", re.I), r"\1 times its normal spread"),
    (re.compile(r"\brobust\s+(?:sigma|σ)\b", re.I), "times its normal spread"),
    (re.compile(r"\bsigma\b|σ", re.I), "times its normal spread"),
    (re.compile(r"\bstandard deviations?\b", re.I), "normal spread"),
    (re.compile(r"\blag-1 autocorrelation\b", re.I), "similarity between one reading and the next"),
    (re.compile(r"\bautocorrelation\b", re.I), "similarity between consecutive readings"),
    (re.compile(r"\bcross-correlation\b", re.I), "similarity"),
    (re.compile(r"\bcorrelat(?:e|es)\b", re.I), "move together"),
    (re.compile(r"\bcorrelated\b", re.I), "related"),
    (re.compile(r"\bcorrelation\b", re.I), "relation"),
    (re.compile(r"\bensemble score\b", re.I), "deviation score"),
    (re.compile(r"\bensemble threshold\b", re.I), "alarm level"),
    (re.compile(r"\bensemble\b", re.I), "deviation score"),
    (re.compile(r"(\d(?:[\d.]*\d)?)\s*x\s+threshold\b", re.I), r"\1 times the alarm level"),
    (re.compile(r"\bthresholds?\b", re.I), "alarm level"),
    (re.compile(r"\bout-of-fold\b", re.I), "held-out"),
    (re.compile(r"\bsilhouette\b", re.I), "separation"),
    (re.compile(r"\bquantization step\b", re.I), "smallest recorded step"),
    (re.compile(r"\bquantization\b", re.I), "rounding"),
    (re.compile(r"\bstd\b"), "spread"),
    (re.compile(r"\bn\s*=\s*(\d+)"), r"\1 readings"),
    (re.compile(r"\bz-scores?\b|\brobust z\b", re.I), "distance from normal"),
    (re.compile(r"\bbaseline\b", re.I), "picture of normal operation"),
    (re.compile(r"\bunimodal\b", re.I), "clustered around one level"),
    (re.compile(r"\bbimodal\b", re.I), "clustered around two levels"),
    (re.compile(r"\bmultimodal\b", re.I), "clustered around several levels"),
    (re.compile(r"\bregressors?\b", re.I), "source signals"),
    (re.compile(r"\bmonotone\b", re.I), "always moving forward"),
    (re.compile(r"\bcusum\b", re.I), "slow-drift detector"),
    (re.compile(r"\bewma\b", re.I), "smoothed-trend detector"),
    (re.compile(r"\bpca\b", re.I), "joint-pattern detector"),
    (re.compile(r"\biforest\b|\bisolation forest\b", re.I), "outlier detector"),
    (re.compile(r"\bautoencoder\b", re.I), "reconstruction detector"),
    (re.compile(r"\bcorr_break\b", re.I), "broken-relation detector"),
    (re.compile(r"\bresid_spread\b", re.I), "residual-spread detector"),
    (re.compile(r"\brobust_z\b", re.I), "distance-from-normal detector"),
    (re.compile(r"\bwithin \[\s*([^\],]+),\s*([^\],]+)\]"), r"between \1 and \2"),
    (re.compile(r"\b(outside|range) \[\s*([^\],]+),\s*([^\],]+)\]"), r"\1 the range \2 to \3"),
    (re.compile(r"\s*->\s*"), ", then "),
]
_TAG_RE = re.compile(r"\[(?:llm-(?:local|external)[^\]]*|template|code|human)\]")
_BRACE_RE = re.compile(r"\{[^{}]*\}")


def _strip_structures(s: str) -> str:
    """Remove JSON / dict fragments and source tags; keep the prose around them."""
    s = _TAG_RE.sub("", s)
    for _ in range(8):
        new = _BRACE_RE.sub("", s)
        if new == s:
            break
        s = new
    s = s.replace("{", "").replace("}", "")
    s = re.sub(r"\[\s*(?:['\"][^\]]*|\s*)\]", "", s)  # ['a', 'b'] list reprs and empty []
    return s


def _tidy(s: str) -> str:
    s = re.sub(r"[ \t\r\n]+", " ", s)
    s = re.sub(r"\(\s*\)", "", s)
    s = re.sub(r"\s+([,.;:)])", r"\1", s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"([,;:])\1+", r"\1", s)
    s = re.sub(r",\s*\.", ".", s)
    s = re.sub(r"\.{2,}", ".", s)
    s = re.sub(r":\s*\.", ".", s)
    return s.strip(" ;,:")


def dejargon(text: Any) -> str:
    """Apply the glossary to any technical sentence. Public so the report can reuse it."""
    s = _strip_structures(str(text or ""))
    for rx, rep in _GLOSSARY:
        s = rx.sub(rep, s)
    return _tidy(s)


def _sentence(s: str) -> str:
    s = _tidy(s)
    if not s:
        return s
    if s[0].islower():
        s = s[0].upper() + s[1:]
    if s[-1] not in ".!?":
        s += "."
    return s


def _generic(E: _Ev) -> str:
    base = dejargon(E.st)
    if not base:
        kind = E.kind.replace("_", " ").strip() or "observation"
        who = f" about {_join(E.sigs[:4])}" if E.sigs else ""
        return f"A {kind} measurement{who} was recorded, but it carries no description."
    return _sentence(base)


# =====================================================================================================
# kind-specific templates
# =====================================================================================================
def _k_format(E: _Ev) -> str:
    v, st = E.v, E.st
    if "delimiter" in v:
        name = DELIMITER.get(str(v.get("delimiter")), f"the character '{v.get('delimiter')}'")
        cons = _f(v.get("consistency"))
        tail = "so the layout of the file is consistent" if cons is None or cons >= 0.98 else "so a few lines of the file do not fit the layout and may be damaged"
        return f"The file was read as a table whose columns are separated by {name}: splitting it that way gives the same {_n(v.get('n_cols'))} columns on {_pct(cons)} of the sampled lines, {tail}."
    if "has_header" in v:
        nf0, nfd = v.get("numeric_fraction_row0"), v.get("numeric_fraction_data")
        if v.get("has_header"):
            return f"The first line of the file holds names rather than numbers ({_pct(nf0)} of it is numeric, against {_pct(nfd)} in the rest of the file), so it was read as the header with the column names."
        return f"The first line of the file looks like data, just like the other lines ({_pct(nf0)} of it is numeric), so the file was read as having no header line and the columns were given neutral names."
    if "n_comma" in v:
        return f"Numbers in this file use a comma as the decimal mark ({_n(v.get('n_comma'))} such values against {_n(v.get('n_dot'))} written with a point), so commas were read as decimal points."
    if "sheet" in v:
        return f"The Excel sheet '{v.get('sheet')}' was read as a table with {_n(v.get('n_cols'))} columns; its first row holds {'names' if (_f(v.get('numeric_fraction_row0')) or 0) < 0.5 else 'numbers'}, which decides whether it is a header."
    low = st.lower()
    if "orientation forced" in low:
        turned = "true" in low.split("transposed=")[-1][:6] if "transposed=" in low else False
        return "An operator stated how the table is laid out (" + ("each line is one signal" if turned else "each column is one signal") + "), so the system did not have to guess."
    if "parquet" in low:
        return "The file is in the Parquet table format, which describes its own columns, so nothing about the layout had to be guessed."
    if "json" in low:
        return "The file is in JSON format, where every value carries its own name, so nothing about the layout had to be guessed."
    return ""


def _k_header(E: _Ev) -> str:
    m = re.search(r"(\d+) of (\d+)", E.st)
    detail = f" (text in {m.group(1)} of {m.group(2)} columns)" if m else ""
    return f"The first line of the file holds names rather than numbers{detail}, so it was read as the header with the column names."


def _k_orientation(E: _Ev) -> str:
    v = E.v
    if "autocorr_down_columns" in v or "autocorr_along_rows" in v:
        down, along = _f(v.get("autocorr_down_columns")), _f(v.get("autocorr_along_rows"))
        turned = "transposed" in E.st and "columns are signals" not in E.st
        how = f"values change smoothly going {'along the lines' if turned else 'down the columns'} and jump around going the other way"
        smooth, rough = (along, down) if turned else (down, along)
        nums = f" (neighbouring values are {_pct(smooth, 0)} alike one way and {'not alike at all' if rough is not None and rough < 0.05 else _pct(rough, 0) + ' alike'} the other)" if smooth is not None and rough is not None else ""
        return f"The file has no header, so the system worked out which way the table runs: {how}{nums}. It therefore took each {'line' if turned else 'column'} to be one signal" + (" and turned the table around." if turned else ".")
    if "n_rows" in v and "n_cols" in v:
        return f"The table was turned around when it was read: in this file each line is one signal and each column one moment in time ({_n(v.get('n_cols'))} signals, {_n(v.get('n_rows'))} readings each)."
    return ""


def _k_sampling(E: _Ev) -> str:
    v = E.v
    if "stride" in v:
        which = f"batch {E.batch}" if E.batch else "the batch"
        return f"To stay within the time budget, {which} was checked on every {_n(v.get('stride'))}th row ({_n(v.get('rows_checked'))} rows looked at). Counts and run lengths are scaled up accordingly, so they are estimates rather than exact figures."
    if "n_rows" in v:
        low = E.st.lower()
        what = "the step that works out what each column contains" if "typing" in low else "the step that studies how each signal behaves" if "profile" in low else "this step"
        frac = _f(v.get("fraction"))
        blocks = f"{_n(v.get('n_chunks'))} {'whole ' + _plural(v.get('n_chunks'), 'group') if str(v.get('method')) == 'whole_groups' else _plural(v.get('n_chunks'), 'block') + ' spread over the file'}"
        total = f" of the {_n(v.get('total_rows'))} rows in the file" if v.get("total_rows") else ""
        tail = "That is all of the data, so nothing was left out." if frac is not None and frac >= 0.999 else "Figures based on this sample are close estimates, not exact counts."
        return f"To keep the analysis fast, {what} looked at {_n(v.get('n_rows'))} rows ({_pct(frac)}{total}), taken as {blocks}. {tail}"
    return ""


def _k_typing(E: _Ev) -> str:
    kinds = E.v.get("kinds")
    words = {"numeric": "decimal numbers", "integer": "whole numbers", "datetime": "dates or times", "constant": "one single value", "text": "free text", "categorical": "a few fixed codes", "boolean": "yes/no values", "empty": "nothing at all"}
    counts: dict[str, int] = {}
    if isinstance(kinds, dict):
        for k in kinds.values():
            counts[str(k)] = counts.get(str(k), 0) + 1
    else:
        for k, n in re.findall(r"(\w+)=(\d+)", E.st):
            counts[k] = int(n)
    if not counts:
        return ""
    parts = [f"{n} {_plural(n, 'holds', 'hold')} {words.get(k, k.replace('_', ' '))}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    return f"Every column was classified by looking at its values: {_join(parts, 6)}. This decides which columns can be treated as sensor signals and which are bookkeeping."


def _k_name_hint(E: _Ev) -> str:
    v = E.v
    if "hints" in v:
        n = len(v["hints"]) if isinstance(v.get("hints"), dict) else None
        return f"The column names hint at what {_n(n) if n is not None else 'some'} {_plural(n, 'column is', 'columns are')} (for example a run number or a time stamp). Names only count as a weak hint and never decide anything on their own, because names can be wrong, missing or in another language."
    return f"Signals are referred to by neutral codes instead of their original names{' (' + _n(v.get('n_signals')) + ' signals)' if v.get('n_signals') else ''}, so that conclusions rest on how the data behaves and not on what a column happens to be called."


def _k_time(E: _Ev) -> str:
    v = E.v
    if v.get("column") or v.get("period") is not None:
        irr = _f(v.get("irregular"))
        tail = "" if not irr or irr < 0.0005 else f"; {_pct(irr)} of the steps are clearly longer or shorter than that"
        return f"The column '{v.get('column')}' works as the clock of the data: time moves forward in {_pct(v.get('monotone'), 1)} of the steps and readings are normally {_dur(v.get('period'))} apart{tail}. This lets the system order events in time and notice holes in the recording."
    return "No usable time stamp column was found, so the rows are taken to be in the order in which they were recorded, and time is counted in samples instead of seconds."


def _k_period(E: _Ev) -> str:
    v = E.v
    if v.get("median_period_s") is None:
        return ""
    return f"Readings arrive every {_dur(v.get('median_period_s'))} (in {_pct(v.get('share'), 1)} of consecutive rows), so the data is sampled at a steady pace."


def _k_counter(E: _Ev) -> str:
    cs = E.v.get("counters")
    if isinstance(cs, dict) and cs:
        name, c = next(iter(cs.items()))
        c = c if isinstance(c, dict) else {}
        resets = _f(c.get("resets")) or 0
        tail = f" and starts again {_n(resets)} {_plural(resets, 'time')}; each restart marks the beginning of a new run" if resets else ""
        return f"The column '{name}' counts upwards by {_num(c.get('step'))} from one row to the next in {_pct(c.get('step_fraction'), 1)} of the rows{tail}. Such a counter shows the order of the readings."
    return ""


def _k_grouping(E: _Ev) -> str:
    v = E.v
    if "method" in v:
        cols = v.get("columns") or []
        col_txt = _join([f"'{c}'" for c in cols], 3)
        how = {
            "key_columns": f"by the value of the column {col_txt}" if cols else "by a key column",
            "counter_reset": f"at every point where the counter {col_txt} starts again" if cols else "at every point where a counter starts again",
            "time_gaps": "wherever there is a long pause in the time stamps",
            "changepoint": "wherever the behaviour of the data changes abruptly",
            "none": "not at all, treating the whole file as one run",
        }.get(str(v.get("method")), f"using the method '{str(v.get('method')).replace('_', ' ')}'")
        if str(v.get("method")) == "none":
            return f"One option is not to split the data at all and to treat the whole file as one run of {_n(round(_f(v.get('median_len')) or 0)) if _f(v.get('median_len')) else 'all'} rows. The system rates this option as {_rating(v.get('score'))} ({_pct(v.get('score'), 0)}); the best-rated option is the one that is used."
        size = ""
        if _f(v.get("median_len")) is not None:
            size = f" of about {_n(round(_f(v.get('median_len'))))} rows each" + (f" (between {_n(v.get('min_len'))} and {_n(v.get('max_len'))})" if v.get("min_len") is not None and v.get("min_len") != v.get("max_len") else "")
        return f"One possible way to split the data into separate runs is {how}: that gives {_n(v.get('n_groups'))} {_plural(v.get('n_groups'), 'run')}{size}. The system rates this split as {_rating(v.get('score'))} ({_pct(v.get('score'), 0)}); the best-rated split is the one that is used."
    if "n_blocks" in v:
        return f"The data falls into {_n(v.get('n_blocks'))} separate runs: a run number stays the same within each block of rows and a counter starts again at each block. Each run is analysed as its own unit."
    return ""


def _k_label_detection(E: _Ev) -> str:
    v = E.v
    n_lab = len(v.get("label_columns") or [])
    n_meta = len(v.get("meta_columns") or [])
    m = re.search(r"(\d+)\s+signal columns", E.st)
    sig_txt = f" and the remaining {m.group(1)} are treated as sensor signals" if m else ""
    lab = f"{n_lab} {_plural(n_lab, 'column looks', 'columns look')} like labels (answers someone added afterwards, such as a fault number)" if n_lab else "No column looks like a label (an answer someone added afterwards, such as a fault number)"
    return f"{lab}, {n_meta} {_plural(n_meta, 'is a bookkeeping column', 'are bookkeeping columns')} such as a run number, counter or time stamp{sig_txt}. Labels are kept away from fault detection so the system cannot peek at the answer; they are only used afterwards to check how well it did."


def _k_domain(E: _Ev) -> str:
    v = E.v
    raw = v.get("raw") if isinstance(v.get("raw"), dict) else {}
    names = {"sensor_stream": "a stream of sensor readings", "business_records": "business records", "event_log": "an event log", "unknown": "none of the kinds of data the system knows"}
    verdict = ""
    if raw:
        top = max(raw.items(), key=lambda kv: _f(kv[1]) or 0)
        verdict = f" Taken together this looks most like {names.get(top[0], str(top[0]).replace('_', ' '))} (rated {_pct(top[1], 0)} likely)."
    if v.get("share_continuous") is None:
        return ""
    return f"{_pct(v.get('share_continuous'), 0)} of the columns are continuously varying numbers, each reading closely resembles the one before it ({_pct(v.get('median_autocorr'), 0)} alike) and {_pct(v.get('share_text'), 0)} of the columns contain text.{verdict}"


def _k_distribution(E: _Ev) -> str:
    v, a = E.v, E.s0
    mean, std = _f(v.get("mean")), _f(v.get("std"))
    if mean is None:
        return ""
    count = v.get("count") or E.n
    over = f" over {_n(count)} readings" if count else ""
    if std == 0 or v.get("n_unique") == 1:
        return f"{a} has the same value ({_num(mean)}) in every one of its readings{over and ' (' + _n(count) + ')'}, so it carries no information for fault detection."
    rng = f" and has been seen between {_num(v.get('min'))} and {_num(v.get('max'))}" if v.get("min") is not None and v.get("max") is not None else ""
    s = f"{a} usually sits around {_num(mean)}, normally varies by about {_num(std)} up or down{rng}{over}."
    mr = _f(v.get("missing_rate"))
    if mr is not None:
        s += f" {_pct(mr)} of its readings are missing." if mr > 0 else " It has no missing readings."
    shape = str(v.get("distribution_shape") or "")
    if shape in ("bimodal", "multimodal"):
        s += f" Its values gather around {'two' if shape == 'bimodal' else 'several'} different levels, which suggests the process runs in more than one operating mode."
    elif shape == "unimodal":
        s += " Its values gather around one typical level, which is what a steady process looks like."
    return s


def _smooth_words(ac: Optional[float]) -> str:
    if ac is None:
        return "changes"
    return "changes smoothly" if ac >= 0.9 else "changes fairly smoothly" if ac >= 0.6 else "changes unevenly" if ac >= 0.3 else "jumps around"


def _k_dynamics(E: _Ev) -> str:
    v, a = E.v, E.s0
    ac, nl = _f(v.get("autocorr_lag1")), _f(v.get("noise_level"))
    stuck, hold = _f(v.get("stuck_fraction")) or 0.0, _f(v.get("hold_period"))
    if ac is None and nl is None and not stuck:
        return f"{a} does not change enough for its rhythm to be measured." if E.sigs else ""
    s = f"{a} {_smooth_words(ac)} from one reading to the next" + (f" (consecutive readings are {_pct(ac, 0)} alike)" if ac is not None else "")
    if nl is not None:
        s += " and carries " + ("a lot of random noise" if nl > 0.7 else "very little random noise" if nl < 0.15 else "an ordinary amount of random noise")
    s += "."
    if stuck >= 0.3:
        s += f" It repeats its previous value in {_pct(stuck, 0)} of its readings" + (f", getting a new value about every {_num(hold)} readings" if hold and hold > 1 else "") + ", which is typical for a setting or a slowly updating instrument rather than a live measurement."
    if _f(v.get("dominant_period")):
        s += f" It also shows a regular rhythm that repeats about every {_num(v.get('dominant_period'))} samples."
    return s


def _k_noise(E: _Ev) -> str:
    ac, nl = _f(E.v.get("autocorr_lag1")), _f(E.v.get("noise_level"))
    noise = "" if nl is None else (" with a lot of random noise" if nl > 0.7 else " with very little random noise" if nl < 0.15 else " with an ordinary amount of random noise")
    return f"{E.s0} {_smooth_words(ac)} from one reading to the next" + (f" (consecutive readings are {_pct(ac, 0)} alike)" if ac is not None else "") + f"{noise}. That is how a live measurement such as a flow, pressure or temperature behaves."


def _k_steps(E: _Ev) -> str:
    v = E.v
    return f"{E.s0} stays flat and then jumps to a new level ({_n(v.get('n_unique'))} different levels; it is flat {_pct(v.get('stuck_fraction'), 0)} of the time). That is how a setting such as a valve position behaves, not a free-running measurement."


def _k_hold(E: _Ev) -> str:
    p = E.v.get("period") or E.v.get("hold_period")
    return f"{E.s0} only gets a new value every {_num(p)} readings and keeps it in between. That is typical for a laboratory analyser or a slow instrument, so these repeats are normal and not a fault."


def _k_constant(E: _Ev) -> str:
    val = E.v.get("value")
    return f"{E.s0} has the same value{' (' + _num(val) + ')' if val is not None else ''} in every row, so it carries no information and is left out of fault detection."


ROLE_ONE = {
    "continuous_measured": "a continuously varying measurement, such as a flow, pressure, temperature or level",
    "actuator_like": "a setting that moves in steps inside a fixed range, such as a valve position or a setpoint",
    "held_sampled": "a value that is refreshed only every few samples, such as a laboratory analyser",
    "constant": "a value that never changes in this data",
    "derived_redundant": "a copy or combination of other signals rather than an independent measurement",
    "counter": "a counter, not a measurement",
    "timestamp": "a time stamp",
    "categorical": "a category code",
    "text": "free text",
    "identifier": "an identifier",
    "unknown": "a signal whose role could not be determined",
}


def _k_role(E: _Ev) -> str:
    role = str(E.v.get("role") or E.v.get("structural_role") or "")
    if role not in ROLE_ONE:
        return ""
    c = E.v.get("confidence") if E.v.get("confidence") is not None else E.v.get("structural_confidence")
    conf = f" ({_conf_words(c)})" if _f(c) is not None else ""
    return f"{E.s0} behaves like {ROLE_ONE[role]}{conf}. This was worked out from how its values change, not from its name."


def _k_derived(E: _Ev) -> str:
    a, rest = E.s0, E.sigs[1:]
    src = _join(rest, 4) or "other signals"
    return f"{a} can be calculated exactly from {src}, so it is a computed value rather than an independent measurement. It adds no new information, but if it ever stops matching them, one of the instruments is wrong."


def _k_redundancy(E: _Ev) -> str:
    v = E.v
    a = v.get("signal") or E.s0
    partners = v.get("partners") or E.sigs[1:]
    src = _join(partners, 4) or "other signals"
    r2 = _f(v.get("r2"))
    expl = f" ({_pct(r2, 0 if r2 is None or r2 >= 0.995 or r2 < 0.9 else 1)} of its movement is explained by them)" if r2 is not None else ""
    if v.get("derived", True):
        return f"{a} can be reproduced almost exactly from {src}{expl}, so it is most likely calculated from them rather than measured on its own. It adds no new information, but if it ever stops matching them, one of the instruments is wrong."
    return f"{a} and {src} carry practically the same information{expl}; {a} is kept as the reference of this set. If they ever disagree, one of the instruments is probably wrong."


def _k_cluster(E: _Ev) -> str:
    members = E.v.get("members") or E.sigs
    m = re.search(r"\bcluster\s+(C\d+)", E.st, re.I)
    cid = f" (family {m.group(1)})" if m else ""
    if not members:
        return ""
    return f"{_join(members, 8)} tend to rise and fall together, so the system treats them as one family of related signals{cid}, probably belonging to the same part of the process. When several of them are disturbed at once the process is the likely cause; when one goes its own way, that sensor is the suspect."


def _k_correlation(E: _Ev) -> str:
    v = E.v
    a = E.sigs[0] if E.sigs else "One signal"
    b = E.sigs[1] if len(E.sigs) > 1 else "another signal"
    if v.get("r_before") is not None and v.get("r_after") is not None:
        return f"{a} normally moves together with {b} ({_pct(abs(_f(v.get('r_before')) or 0), 0)} in step), but here that link has all but disappeared ({_pct(abs(_f(v.get('r_after')) or 0), 0)}). Every other pair of signals kept behaving as usual, which points at {a} itself, for example a faulty sensor, rather than at the process."
    r = _f(v.get("r"))
    if r is None:
        return ""
    p = _pct(abs(r), 0)
    lag = _f(v.get("lag"))
    lag_txt = f" The match is best when one of them is shifted by {_n(abs(lag))} {_plural(abs(lag), 'sample')}." if lag else ""
    if abs(r) < 0.3:
        return f"{a} and {b} hardly follow each other at all ({p} in step), so one says very little about the other.{lag_txt}"
    if abs(r) < 0.5:
        return f"{a} and {b} only loosely follow each other ({p} in step), so they are at most weakly connected.{lag_txt}"
    if r < 0:
        return f"{a} and {b} move in opposite directions {_strength(r)}: when one rises the other falls ({p} in step). They probably belong to the same part of the process.{lag_txt}"
    return f"{a} and {b} rise and fall together {_strength(r)} ({p} in step), so they probably belong to the same part of the process.{lag_txt}"


def _k_lag(E: _Ev) -> str:
    v = E.v
    if isinstance(v.get("lags"), (list, tuple)) and E.sigs:
        lags = list(v["lags"])
        steps = [E.sigs[0]]
        for s, lg in list(zip(E.sigs, lags))[1:6]:
            f = _f(lg)
            steps.append(f"{s} ({_n(f)} {_plural(f, 'sample')} later)" if f else f"{s} (at the same time)")
        grp = f" in group {E.group}" if E.group not in (None, "") else ""
        return f"The disturbance{grp} did not hit all signals at once: it showed up first in {steps[0]}" + (", then in " + _join(steps[1:], 6, "and then") if len(steps) > 1 else "") + ". The order in which signals react points to where the problem started."
    lag = _f(v.get("lag"))
    if lag is None or len(E.sigs) < 2:
        return ""
    lead, follow = (E.sigs[0], E.sigs[1]) if lag >= 0 else (E.sigs[1], E.sigs[0])
    r = _f(v.get("r_at_lag"))
    match = f" (the two then match {_strength(r)}, {_pct(abs(r), 0)} in step)" if r is not None else ""
    if lag == 0:
        return f"{lead} and {follow} react at the same moment{match}, so neither clearly comes before the other."
    return f"{follow} follows {lead} about {_n(abs(lag))} {_plural(abs(lag), 'sample')} later{match}, which suggests {lead} influences {follow} rather than the other way round."


def _k_stuck(E: _Ev) -> str:
    v, a = E.v, E.s0
    if not E.d.get("signals") and E.st.lower().startswith("no "):
        lim = re.search(r"longer than (\d+)", E.st)
        longest = (f": the longest run of identical readings was {_n(v.get('max_run'))}" + (f", below the limit of {lim.group(1)}" if lim else "")) if v.get("max_run") is not None else ""
        return f"No signal stayed frozen for a suspiciously long time{E.in_batch()}{longest}."
    run = v.get("longest_run") or v.get("run_length") or v.get("max_run")
    if run:
        val = f" ({_num(v.get('value'))})" if v.get("value") is not None else ""
        frac = _f(v.get("fraction") if v.get("fraction") is not None else v.get("stuck_fraction"))
        share = f" The system does not rely on {a} in those rows ({_pct(frac)} of the batch)." if frac is not None and E.batch else ""
        if v.get("at_zero"):
            why = f"Staying at exactly zero can be genuine, for example a closed valve or no flow, but it can also be a dead sensor, so it is worth checking."
        else:
            why = "A healthy instrument on a running process almost never does that, so the sensor or its connection is probably at fault, not the process."
        more = _f(v.get("n_runs"))
        again = f" This happened {_n(more)} times." if more and more > 1 else ""
        return f"{a} showed exactly the same value{val} for {_n(run)} readings in a row{E.in_batch()}{E.rows()}. {why}{again}{share}"
    sf = _f(v.get("stuck_fraction"))
    if sf is not None:
        hold = _f(v.get("hold_period"))
        per = f", typically for about {_num(hold)} readings in a row" if hold and hold > 1 else ""
        return f"{a} repeats its previous value in {_pct(sf, 0)} of its readings{per}. That is normal for a setting or for an instrument that updates slowly, but it would be suspicious for a live measurement."
    if "constant" in E.st.lower() or "frozen" in E.st.lower():
        return f"{a} stopped changing{E.rows()} while the signals that normally move with it kept moving. A healthy instrument on a running process almost never does that, so the sensor or its connection is probably at fault."
    return ""


def _k_stale(E: _Ev) -> str:
    v, a = E.v, E.s0
    return f"{a} normally gets a new value every {_num(v.get('hold_period'))} readings, but it did not update for {_n(v.get('longest_run'))} readings{E.in_batch()}{E.rows()}. The instrument probably stopped delivering fresh values, so its reading is out of date there."


def _k_saturation(E: _Ev) -> str:
    v, a = E.v, E.s0
    at = str(v.get("at") or "limit")
    side = "higher" if at.startswith("max") else "lower" if at.startswith("min") else "different"
    return f"{a} sat at its {at}{' (' + _num(v.get('value')) + ')' if v.get('value') is not None else ''} for {_n(v.get('longest_run'))} readings in a row{E.in_batch()}{E.rows()}. The instrument may have reached the end of its measuring range, so the true value could be {side} than what was recorded."


def _k_missing(E: _Ev) -> str:
    v, a = E.v, E.s0
    rate = _f(v.get("missing_rate") if v.get("missing_rate") is not None else v.get("missing_fraction"))
    if (rate is None or rate == 0) and not v.get("n_missing") and not v.get("missing_run"):
        return f"No readings are missing in any signal{E.in_batch()}." if not E.d.get("signals") else f"No readings of {a} are missing{E.in_batch()}."
    longest = v.get("longest_missing") or v.get("missing_run")
    cnt = f"{_n(v.get('n_missing'))} readings" if v.get("n_missing") else ""
    hole = f"the longest hole is {_n(longest)} readings in a row" if longest else ""
    return f"{a} has no value in {_pct(rate)} of {'batch ' + E.batch if E.batch else 'the batch'}{E.rows(cnt, hole)}. While a signal is missing the system cannot see what that part of the process is doing, so alarms involving {a} in those rows are treated with caution."


def _k_dropout(E: _Ev) -> str:
    a = E.s0
    return f"{a} delivered no values at all{E.in_batch()}. The instrument or its data link was probably down, so nothing can be said about {a} for this batch."


def _k_impossible(E: _Ev) -> str:
    return f"{E.s0} contains {_n(E.v.get('n'))} {_plural(E.v.get('n'), 'value')} that cannot be real measurements (infinite or absurdly large numbers){E.in_batch()}{E.rows()}. These are recording errors, not process behaviour, and they are kept out of the fault reasoning."


def _k_out_of_range(E: _Ev) -> str:
    v, a = E.v, E.s0
    z = _f(v.get("max_robust_z") if v.get("max_robust_z") is not None else v.get("robust_z"))
    n = _f(v.get("n"))
    lim = re.search(r"(?:beyond|within)\s+(\d+(?:\.\d+)?)\s+robust", E.st)
    if n is None and v.get("value") is None:
        limit = f", against a limit of {lim.group(1)}" if lim else ""
        return f"All readings stayed within their usual range{E.in_batch()}: the most unusual one was {_x(z)} times the normal spread away from its typical value{limit}." if z is not None else f"All readings stayed within their usual range{E.in_batch()}."
    verdict = "A jump that large is almost certainly a recording or sensor error rather than a real process value." if z is not None and z >= 50 else "Values this unusual deserve a look: either the process went somewhere it normally never goes, or the sensor misread."
    far = f": the most extreme one lies about {_n(round(z)) if z and z >= 20 else _x(z)} times its normal spread away from its typical value" if z is not None else ""
    if v.get("value") is not None and n is None:
        return f"{a} shows the value {_num(v.get('value'))}{E.rows()}, which lies about {_n(round(z)) if z and z >= 20 else _x(z)} times its normal spread away from its typical value. {verdict}"
    return f"{a} has {_n(n)} {_plural(n, 'reading')} far outside its usual range{E.in_batch()}{E.rows()}{far}. {verdict}"


def _k_unit_shift(E: _Ev) -> str:
    v, a = E.v, E.s0
    factor = _f(v.get("factor"))
    ks = v.get("k")
    if factor is None and isinstance(ks, (list, tuple)) and ks and _f(ks[0]) is not None:
        factor = 10.0 ** float(ks[0])
    if factor is None:
        if not E.d.get("signals") and "no " in E.st.lower():
            return f"No sudden changes of scale, such as a mix-up of units or a misplaced decimal point, were found{E.in_batch()}."
        return ""
    size = f"{_n(round(factor))} times larger" if factor >= 1 else f"{_n(round(1 / factor))} times smaller"
    back = " and then returned to normal" if "return" in E.st.lower() else ""
    return f"{a} suddenly became about {size}{E.in_batch()}{E.rows()}{back}. A real process does not jump by a round factor like that: it is almost certainly a change of units or a misplaced decimal point in the data, not an event in the process."


def _k_quantization(E: _Ev) -> str:
    v, a = E.v, E.s0
    return f"{a} was recorded much more coarsely than usual for {_n(v.get('n_samples'))} readings{E.in_batch()}{E.rows()}: it shows only {_pct(v.get('uniqueness_ratio'), 0)} of its usual variety of values. That points to a change in how the data was logged or rounded, not to a change in the process."


def _k_sign(E: _Ev) -> str:
    v, a = E.v, E.s0
    return f"{a} is never below zero anywhere else in the data, but it has {_n(v.get('n'))} negative {_plural(v.get('n'), 'reading')}{' (lowest ' + _num(v.get('min')) + ')' if v.get('min') is not None else ''}{E.in_batch()}{E.rows()}. For a quantity that cannot be negative this indicates a sensor or recording error."


def _k_empty_rows(E: _Ev) -> str:
    return f"{_n(E.v.get('n'))} {_plural(E.v.get('n'), 'row')}{E.in_batch()} contain no sensor values at all{E.rows(_pct(E.v.get('fraction')) if _f(E.v.get('fraction')) is not None else '')}. Those moments are blind spots: nothing can be said about the process there."


def _k_duplicate_rows(E: _Ev) -> str:
    v = E.v
    n = _f(v.get("n") if v.get("n") is not None else v.get("n_duplicates"))
    if not n:
        return f"No row appears twice{E.in_batch()}."
    return f"{_n(n)} {_plural(n, 'row')}{E.in_batch()} {_plural(n, 'is an exact copy', 'are exact copies')} of another row{E.rows(_pct(v.get('fraction')) if _f(v.get('fraction')) is not None else '')}. Exact copies are almost always a logging or export glitch; they add no information and can make a situation look more common than it was."


def _k_duplicate_key(E: _Ev) -> str:
    n = E.v.get("n")
    return f"{_n(n)} {_plural(n, 'row')}{E.in_batch()} {_plural(n, 'has', 'have')} the same run and position as an earlier row, as if the same moment had been recorded twice{E.rows()}. One of each pair is probably a repeat or a wrongly numbered row."


def _k_duplicate_ts(E: _Ev) -> str:
    n = E.v.get("n")
    return f"{_n(n)} time {_plural(n, 'stamp')}{E.in_batch()} {_plural(n, 'appears', 'appear')} more than once{_paren(_pct(E.v.get('fraction')) if _f(E.v.get('fraction')) is not None else '')}. Two readings cannot belong to the same instant, so the clock or the logger hiccupped there."


def _k_out_of_order(E: _Ev) -> str:
    n = E.v.get("n")
    return f"{_n(n)} time {_plural(n, 'stamp goes', 'stamps go')} backwards{E.in_batch()}{E.rows()}. Time cannot run backwards, so rows were shuffled or the clock was reset; the order of events around those rows is uncertain."


def _k_gap(E: _Ev) -> str:
    v = E.v
    period = _f(v.get("period_s") if v.get("period_s") is not None else v.get("expected_period_s"))
    gap = _f(v.get("gap_s") if v.get("gap_s") is not None else v.get("max_gap_s"))
    if gap is None:
        return ""
    if period is None and v.get("n") is None:
        return f"The time stamps advance steadily ({_dur(gap)} apart) with no holes{E.in_batch()}."
    lost = f"about {_n(round(gap / period) - 1)} readings are missing" if period and gap / period >= 2 else ""
    n = _f(v.get("n"))
    lead = f"There {_plural(n, 'is', 'are')} {_n(n)} {_plural(n, 'hole')} in the time line{E.in_batch()}: the longest lasts {_dur(gap)}" if n else f"The time stamps{E.in_batch()} jump forward by {_dur(gap)}"
    usual = f", where readings normally arrive every {_dur(period)}" if period else ""
    return f"{lead}{usual}{E.rows(lost)}. Whatever happened in the process during that time was not recorded."


def _k_irregular(E: _Ev) -> str:
    return f"Readings{E.in_batch()} do not arrive at a steady pace: {_pct(E.v.get('fraction'))} of the intervals differ by more than a fifth from the usual {_dur(E.v.get('period_s'))}. Methods that assume evenly spaced readings are less exact here."


def _k_relation_break(E: _Ev) -> str:
    v, a = E.v, E.s0
    if v.get("r_batch") is not None:
        b = E.sigs[1] if len(E.sigs) > 1 else "its twin"
        return f"{a} and {b} normally tell the same story ({_pct(abs(_f(v.get('r_expected')) or 0), 0)} in step), but{E.in_batch() or ' here'} they agree only {_pct(abs(_f(v.get('r_batch')) or 0), 0)}. When two copies of the same information disagree, one of the instruments is probably faulty."
    if v.get("rms_residual") is not None:
        return f"{a} is normally calculated from other signals, but{E.in_batch() or ' here'} it no longer matches that calculation: it is off by {_num(v.get('rms_residual'))}, against a usual spread of {_num(v.get('scale'))}. Either {a} or one of its source signals is wrong."
    return ""


def _k_check_pass(E: _Ev) -> str:
    m = re.search(r"no (\w+) problems", E.st)
    cat = m.group(1) if m else ""
    what = CATEGORY.get(cat, ("", "no problems were found"))[1]
    rows = f" in its {_n(E.v.get('n_rows'))} rows" if E.v.get("n_rows") else ""
    return f"{'Batch ' + E.batch if E.batch else 'The batch'} passed the {cat + ' ' if cat else ''}checks: {what}{rows}."


def _rule_text(E: _Ev) -> str:
    m = re.search(r"Rule:\s*(.+)$", E.st, re.S)
    return _tidy(_strip_structures(m.group(1))) if m else ""


def _k_rule_violation(E: _Ev) -> str:
    v = E.v
    rid = v.get("rule_id") or (re.search(r"RULE-\d+", E.st).group(0) if re.search(r"RULE-\d+", E.st) else "An operating rule")
    text = _rule_text(E)
    says = f' The rule says: "{text.rstrip(".")}".' if text else ""
    n = v.get("n_violating")
    return f"Operating rule {rid} was broken{E.in_batch()}: {_n(n)} {_plural(n, 'reading')} in {_n(v.get('n_episodes'))} {_plural(v.get('n_episodes'), 'episode')}{E.rows()}.{says} Rules come from the people who run the plant, so a broken rule is a finding in its own right."


def _k_rule_pass(E: _Ev) -> str:
    v = E.v
    rid = v.get("rule_id") or (re.search(r"RULE-\d+", E.st).group(0) if re.search(r"RULE-\d+", E.st) else "The operating rule")
    text = _rule_text(E)
    says = f' The rule says: "{text.rstrip(".")}".' if text else ""
    return f"Operating rule {rid} was kept throughout {'batch ' + E.batch if E.batch else 'the batch'} ({_n(v.get('n_rows') or E.n)} readings checked).{says}"


def _k_rule_generic(E: _Ev) -> str:
    rid_m = re.search(r"RULE-\d+", E.kind + " " + E.st)
    rid = rid_m.group(0) if rid_m else "The operating rule"
    detail = dejargon(re.sub(r"^\s*RULE-\d+\s*:\s*", "", E.st))
    viol = _f(E.v.get("violations") if E.v.get("violations") is not None else E.v.get("n_violating"))
    if viol:
        return f"Operating rule {rid} was broken in {_n(viol)} {_plural(viol, 'row')}{E.in_batch()}: {detail.rstrip('.')}. Rules come from the people who run the plant, so a broken rule is a finding in its own right."
    return f"Operating rule {rid} was kept{E.in_batch()}: {detail.rstrip('.')}."


def _k_rule_compile(E: _Ev) -> str:
    v = E.v
    spec = v.get("spec") if isinstance(v.get("spec"), dict) else {}
    rid_m = re.search(r"RULE-\d+", E.st)
    quoted = re.search(r"compiled '(.+?)' into", E.st, re.S)
    by = "a language model" if "llm" in E.st.lower() else "the built-in rule grammar"
    typ = str(spec.get("type") or "").replace("_", " ")
    on = _join(E.sigs, 4)
    return f"The rule{' ' + rid_m.group(0) if rid_m else ''} written in plain language" + (f' ("{quoted.group(1).strip().rstrip(".")}")' if quoted else "") + f" was turned into an automatic {typ + ' ' if typ else ''}check" + (f" on {on}" if on else "") + f" by {by} ({_conf_words(v.get('confidence'))}). A person has to approve this translation before the rule is used."


def _k_trust(E: _Ev) -> str:
    v = E.v
    score = _f(v.get("trust_score"))
    if score is None:
        return ""
    bs = _f(v.get("batch_severity")) or 0.0
    whole = "no problems affecting the whole batch" if bs < 0.05 else ("minor problems affecting the whole batch, such as time gaps or duplicates" if bs < 0.4 else "serious problems affecting the whole batch, such as time gaps or duplicates")
    verdict = "Alarms from this batch can be taken at face value." if score >= 0.8 else "Alarms that involve the affected signals should be read with care." if score >= 0.5 else "The batch is unreliable: an alarm raised on it may be caused by bad data rather than by the process."
    bad = f" ({_join(E.d.get('signals') or [], 6)})" if E.d.get("signals") else ""
    return f"The data of {'batch ' + E.batch if E.batch else 'this batch'} is rated {_pct(score, 0)} trustworthy: {_n(v.get('n_untrusted'))} of {_n(v.get('n_signals'))} signals had reliability problems{bad}, and there were {whole}. {verdict}"


def _k_batching(E: _Ev) -> str:
    v = E.v
    how = f" of {_dur(v.get('window_seconds'))} each" if v.get("method") == "time_window" and v.get("window_seconds") else " of similar size"
    return f"For the quality checks the {_n(v.get('n_rows'))} rows were cut into {_n(v.get('n_batches'))} consecutive portions (batches){how}. Each batch gets its own checks and its own trust rating, so a problem can be pinned to a time and place."


def _k_alignment(E: _Ev) -> str:
    v = E.v
    nm, nx = len(v.get("missing") or []), len(v.get("extra") or [])
    return f"The incoming {'batch ' + E.batch if E.batch else 'batch'} did not have exactly the expected columns: {nm} expected {_plural(nm, 'column was', 'columns were')} absent (their values are treated as missing) and {nx} unknown {_plural(nx, 'column was', 'columns were')} ignored."


def _k_threshold(E: _Ev) -> str:
    m = re.search(r"calibrated on (\d+)", E.st)
    n = m.group(1) if m else E.n
    fold = _f(E.v.get("fold"))
    rnd = f" (check round {int(fold) + 1})" if fold is not None else ""
    return f"The alarm level was set from normal operation only{rnd}: on {_n(n)} rows of normal data that the detectors had not been trained on, the system measured how large the deviation score gets when nothing is wrong and put the alarm line just above that. On the charts this line is shown as 1.0, which means {NORMAL_LIMIT}."


def _k_baseline(E: _Ev) -> str:
    v = E.v
    closing = "Every alarm is measured against this picture of normal, so if it is wrong the alarms shift with it; an operator can replace it with a known good period."
    if v.get("strategy"):
        k = len(v["candidate_scores"]) if isinstance(v.get("candidate_scores"), dict) else None
        tried = f"tried {k} different ways of picking the normal part of the data and chose" if k and k > 1 else "chose"
        name = str(v.get("strategy"))
        return f"Nobody told the system what normal looks like, so it {tried} to {STRATEGY.get(name, 'use the strategy ' + name.replace('_', ' '))}. This treats {_pct(v.get('fraction'), 0)} of the sampled rows, spread over {_n(v.get('n_groups'))} {_plural(v.get('n_groups'), 'group')}, as normal operation. {closing}"
    if "reference" in v:
        return f"An operator told the system which period counts as normal operation; it covers {_pct(v.get('fraction'), 0)} of the sampled rows. Every alarm is measured against that period."
    if v.get("share_rows") is not None:
        return f"The calmest, most repeatable stretch of every group (about {_pct(v.get('share_rows'), 0)} of its rows) was taken as the picture of normal operation, because all signals sit at their usual levels there. {closing}"
    return ""


def _k_baseline_candidate(E: _Ev) -> str:
    v = E.v
    name = str(v.get("name") or (re.search(r"candidate '([^']+)'", E.st).group(1) if re.search(r"candidate '([^']+)'", E.st) else "candidate"))
    au = _f(v.get("separation_auroc"))
    sep = f" It separates normal from unusual rows in groups it has not seen with a quality of {au:.2f} (1.0 = perfectly, 0.5 = no better than chance)." if au is not None else ""
    cov = f" It covers {_pct(v.get('coverage'), 0)} of the groups and agrees {_pct(v.get('consensus'), 0)} with the other candidates;" if v.get("coverage") is not None else ""
    return f"One way to pick out normal operation is to {STRATEGY.get(name, 'use the strategy ' + name.replace('_', ' '))} ('{name}'); it would keep {_pct(v.get('fraction'), 0)} of the sampled rows as normal.{sep}{cov} its overall rating is {_rating(v.get('score'))} ({_pct(v.get('score'), 0)}). The best-rated candidate becomes the picture of normal operation."


def _first_signals(first: Any, limit: int = 4) -> str:
    out = []
    for i, f in enumerate(first or []):
        if not isinstance(f, dict) or not f.get("signal"):
            continue
        d = DIRECTION.get(str(f.get("direction")), "")
        lag = _f(f.get("lag"))
        when = "" if i == 0 or not lag else f" {_n(abs(lag))} {_plural(abs(lag), 'sample')} later"
        out.append(f"{f['signal']}{when}" + (f" ({d})" if d else ""))
        if len(out) >= limit:
            break
    return _join(out, limit, "then")


def _k_changepoint(E: _Ev) -> str:
    v = E.v
    grp = f"In group {E.group} the" if E.group not in (None, "") else "The"
    if v.get("row") is not None:
        kind = str(v.get("kind") or "")
        r80 = _f(v.get("rows_to_80pct"))
        how = "sudden, from one reading to the next" if kind == "abrupt" else (f"gradual, building up over about {_n(r80)} rows" if kind == "gradual" and r80 else "gradual" if kind == "gradual" else kind)
        order = str(v.get("order") or "")
        what = ", and it is mainly the way the signals move (their speed or unrest) that changed rather than their level" if order == "second" else (", with signals shifting to a new level" if order == "first" else "")
        firsts = _first_signals(v.get("first_signals"))
        tail = f" The first to react: {firsts}. What moves first is usually closest to the cause." if firsts else ""
        return f"{grp} behaviour changed at row {v.get('row')}. The change was {how}{what}.{tail}"
    if v.get("onset_row") is not None:
        m = re.search(r"in ([\d.]+)\s*% of the remaining", E.st)
        stay = f" and stayed above it in {m.group(1)} % of the rows that followed" if m else ""
        peak = f", peaking at {_x(v.get('peak'))} times that level" if _f(v.get("peak")) else ""
        return f"{grp} deviation score first crossed the alarm level ({NORMAL_LIMIT}) at row {v.get('onset_row')}{stay}{peak}. That marks the moment the problem started."
    return ""


def _share_list(shares: dict[str, Any], directions: dict[str, Any], skip: int = 0, limit: int = 3) -> list[str]:
    out = []
    for s, sh in list(shares.items())[skip: skip + limit]:
        d = DIRECTION.get(str((directions or {}).get(s)), "")
        out.append(f"{s} ({_pct(sh, 0)}" + (f", {d})" if d else ")"))
    return out


def _k_contribution(E: _Ev) -> str:
    v = E.v
    shares = v.get("shares") if isinstance(v.get("shares"), dict) else None
    if shares:
        directions = v.get("directions") if isinstance(v.get("directions"), dict) else {}
        span = E.row_span()
        where = f"Between rows {span[0]} and {span[1]}{E.in_group()}" if span else f"During this event{E.in_group()}"
        mean, peak = _f(v.get("mean_ensemble")), _f(v.get("peak_ensemble"))
        if mean is not None and mean >= 1:
            level = f"the process looked {_x(mean)} times more unusual than anything seen in normal operation" + (f" ({_x(peak)} times at its peak)" if peak and peak > mean * 1.05 else "")
        elif mean is not None:
            level = "the process was on average still within what is seen in normal operation" + (f", but reached {_x(peak)} times that limit at its peak" if peak and peak >= 1 else "")
        else:
            level = "the process looked unusual"
        top, share = next(iter(shares.items()))
        d = DIRECTION.get(str(directions.get(top)), "")
        sh = _f(share) or 0.0
        lead = f"{top} alone explains {_pct(sh, 0)} of that" if sh >= 0.5 else f"{top} explains the largest part of that ({_pct(sh, 0)})"
        lead += f" (it {d})" if d else ""
        rest = _share_list(shares, directions, skip=1, limit=3)
        nxt = f"; next come {_join(rest, 3)}" if rest else ""
        m = re.search(r"Detector agreement (\d+)\s*%", E.st)
        agree = f" {m.group(1)} % of the detection methods agreed on this event" + ("." if int(m.group(1)) >= 60 else ", so it is worth a second look.") if m else ""
        return f"{where} {level}. {lead}{nxt}.{agree}"
    if v.get("share") is not None:
        m = re.search(r"direction:\s*(\w+)", E.st)
        d = DIRECTION.get(m.group(1), "") if m else ""
        sh = _f(v.get("share")) or 0.0
        rank = "the first signal to look at" if sh >= 0.3 else "one of the signals to look at"
        return f"{E.s0} accounts for {_pct(sh, 0)} of the deviation after the problem started" + (f" (it {d})" if d else "") + f", which makes it {rank}."
    return ""


def _k_segment(E: _Ev) -> str:
    v = E.v
    if v.get("row_start") is None:
        return ""
    lead = ""
    ls = v.get("leading_signal_shares")
    if isinstance(ls, dict) and ls:
        items = list(ls.items())
        lead = f" {items[0][0]} was the most unusual signal in {_pct(items[0][1], 0)} of those rows" + (f", followed by {_join([f'{s} ({_pct(x, 0)})' for s, x in items[1:3]], 2)}" if len(items) > 1 else "") + "."
    grp = f"In group {E.group} the" if E.group not in (None, "") else "The"
    return f"{grp} deviation score stayed at or above the alarm level from row {v.get('row_start')} to {v.get('row_end')} ({_n(v.get('n_rows'))} rows): on average {_x(v.get('mean_norm'))} times {NORMAL_LIMIT}, and {_x(v.get('peak'))} times at its peak.{lead}"


def _k_cause(E: _Ev) -> str:
    v = E.v
    cause = str(v.get("cause") or "unknown")
    span = E.row_span()
    where = f"For rows {span[0]} to {span[1]}{E.in_group()}" if span else f"For this event{E.in_group()}"
    why = E.st.split("):", 1)[1] if "):" in E.st else ""
    why = dejargon(why)
    reason = f" Reason: {why.rstrip('.')}." if why else ""
    lead = "the cause is " + CAUSE["unknown"] if cause == "unknown" else "the most likely explanation is " + CAUSE.get(cause, cause)
    return f"{where} {lead} ({_conf_words(v.get('confidence'))}).{reason}"


def _k_cascade(E: _Ev) -> str:
    v = E.v
    chain = [c for c in (v.get("chain") or []) if isinstance(c, dict)]
    if not chain:
        return ""
    waves: list[tuple[list[str], float]] = [([str(chain[0].get("from_signal"))], 0.0)]
    for c in chain[:8]:
        lag = _f(c.get("lag")) or 0.0
        to = str(c.get("to_signal"))
        if not lag:
            if to not in waves[-1][0]:
                waves[-1][0].append(to)
        else:
            waves.append(([to], abs(lag)))
    parts = [_join(waves[0][0], 4) + (" at the same time" if len(waves[0][0]) > 1 else "")]
    parts += [f"{_join(w, 4)} {_n(lg)} {_plural(lg, 'sample')} later" for w, lg in waves[1:]]
    span = E.row_span()
    where = (f"In group {E.group}" if E.group not in (None, "") else "Here") + (f" (rows {span[0]} to {span[1]})" if span else "")
    flag = f" It belongs to alarm {v.get('parent_flag')}." if v.get("parent_flag") else ""
    return f"{where} the disturbance spread step by step instead of hitting everything at once: it started in {parts[0]}" + (f", then reached {_join(parts[1:], 6, 'and then')}" if len(parts) > 1 else "") + f". The order shows where the problem began and how it travelled through the process, so the earliest signals are the place to look first.{flag}"


def _k_pattern(E: _Ev) -> str:
    v = E.v
    m = re.match(r"\s*(PATTERN-[A-Z]+)", E.st)
    pid = m.group(1) if m else "This pattern"
    shares = v.get("mean_shares") if isinstance(v.get("mean_shares"), dict) else {}
    directions = v.get("directions") if isinstance(v.get("directions"), dict) else {}
    n_ev = v.get("n_events")
    groups = v.get("groups") or []
    s = f"{pid} is a kind of event that keeps coming back: it was seen {_n(n_ev)} {_plural(n_ev, 'time')}" + (f" in {len(groups)} {_plural(len(groups), 'group')}" if groups else "") + "."
    if shares:
        s += f" It mainly involves {_join(_share_list(shares, directions, 0, 3), 3)}."
    lo = [x for x in (v.get("lag_order") or []) if isinstance(x, (list, tuple)) and len(x) >= 2]
    if len(lo) > 1:
        seq = [str(lo[0][0])] + [f"{a} (about {_num(lg)} samples later)" if _f(lg) else f"{a} (at the same time)" for a, lg in lo[1:4]]
        s += f" It typically unfolds in the order {_join(seq, 4, 'then')}."
    causes = v.get("cause_classes") if isinstance(v.get("cause_classes"), dict) else {}
    if causes:
        top, cnt = max(causes.items(), key=lambda kv: _f(kv[1]) or 0)
        words = {"data": "data problems", "sensor": "instrument faults", "process": "real process changes", "mixed": "a mix of causes", "unknown": "events of unclear cause"}.get(str(top), str(top))
        s += f" Most of these events ({_n(cnt)} of {_n(sum(_f(x) or 0 for x in causes.values()))}) look like {words}."
    return s


def _k_learning_curve(E: _Ev) -> str:
    v = E.v
    xs = v.get("fractions") or []
    ys = v.get("primary") or v.get("scores") or []
    if not xs or not ys or len(xs) != len(ys):
        return "" if E.st else "The learning curve could not be computed."
    s = f"To see whether more data would help, the detectors were trained on growing portions of the data: with {_pct(xs[0], 0)} of it they reached a quality of {_f(ys[0]):.2f}, with {_pct(xs[-1], 0)} of it {_f(ys[-1]):.2f} (1.0 is best)."
    dim, slope, unc = _f(v.get("diminishing_returns_fraction")), _f(v.get("slope")), _f(v.get("slope_uncertainty"))
    gain = _f(v.get("estimated_gain_more_data"))
    if dim is not None:
        s += f" The improvement levels off from about {_pct(dim, 0)} of the data, so more data of the same kind is unlikely to help much."
    elif slope is not None and slope > max(unc or 0.0, 0.01):
        s += " The curve is still rising at the end, so more data of the same kind would probably improve detection" + (f" (estimated gain about {_pct(gain, 0)})." if gain and gain > 0.005 else ".")
    elif slope is not None:
        s += " At the end the curve is nearly flat, or its trend is smaller than its own uncertainty, so it is unclear whether more data of the same kind would help."
    elif _f(ys[-1]) is not None and _f(ys[0]) is not None and _f(ys[-1]) > _f(ys[-2] if len(ys) > 1 else ys[0]) + 0.01:
        s += " The curve is still rising at the end, so more data of the same kind would probably improve detection."
    return s


def _k_regime_coverage(E: _Ev) -> str:
    v = E.v
    k, n = _f(v.get("k")), v.get("n_units")
    unit = str(v.get("unit_kind") or "group")
    if k is None:
        return ""
    if k <= 1:
        return f"All {_n(n)} {unit}s look like one and the same way of operating, so there are no rare operating conditions that the system would see too little of."
    shares = [_pct(x, 0) for x in (v.get("shares") or [])]
    thin = [str(x) for x in (v.get("thin") or [])]
    bal = _f(v.get("balance"))
    even = "very unevenly" if bal is not None and bal < 0.5 else "fairly evenly" if bal is not None and bal >= 0.8 else "somewhat unevenly"
    s = f"The {_n(n)} {unit}s fall into {_n(k)} distinct ways of operating, and they are represented {even}" + (f": {_join(shares, 6)} of the {unit}s" if shares else "") + "."
    if thin:
        s += f" {_join(thin, 4)} {_plural(len(thin), 'is', 'are')} rare, so the system sees few examples of {_plural(len(thin), 'it', 'them')} and will recognise faults there less reliably; more data from those conditions would help."
    return s


def _k_signal_information(E: _Ev) -> str:
    v = E.v
    nc = [str(x) for x in (v.get("near_constant") or [])]
    pairs = [p for p in (v.get("redundant_pairs") or []) if isinstance(p, dict)]
    a = f"{len(nc)} {_plural(len(nc), 'signal hardly ever changes', 'signals hardly ever change')} and so {_plural(len(nc), 'carries', 'carry')} almost no information ({_join(nc, 6)})." if nc else "No signal is so flat that it carries no information."
    ptxt = _join([str(p.get("a")) + " and " + str(p.get("b")) for p in pairs], 4, "and")
    b = f" {len(pairs)} {_plural(len(pairs), 'pair of signals is a near-copy', 'pairs of signals are near-copies')} of each other ({ptxt}); keeping both adds little, but a disagreement between them is a useful warning of a faulty sensor." if pairs else " No two signals are near-copies of each other."
    return a + b


def _k_coverage(E: _Ev) -> str:
    base = dejargon(E.st)
    if not base:
        return ""
    return _sentence(base) + " Operating conditions that are rarely seen are learned poorly, so faults that happen there are harder to recognise."


def _k_dq_score(E: _Ev) -> str:
    v = E.v
    if v.get("overall") is not None:
        return f"Overall the data quality is rated {_pct(v.get('overall'))}. On average a batch is {_pct(v.get('mean_trust'))} trustworthy, and {_n(v.get('n_untrusted_batches'))} of {_n(v.get('n_batches'))} batches were judged too unreliable to raise alarms on."
    cat = str(v.get("category") or "")
    if not cat:
        return ""
    title = CATEGORY.get(cat, (cat.capitalize(), ""))[0]
    nf, nw = v.get("n_fail") or 0, v.get("n_warn") or 0
    sig = f", involving {_n(v.get('n_signals'))} {_plural(v.get('n_signals'), 'signal')}" if v.get("n_signals") else ""
    worst = E.st.split("worst:", 1)[1] if "worst:" in E.st else ""
    worst = dejargon(worst)
    if len(worst) > 220:
        worst = worst[:217].rsplit(" ", 1)[0] + "…"
    tail = f" The most serious finding: {worst.rstrip('.')}." if worst else ""
    return f"{title} scores {_pct(v.get('score'))}: {_n(nf)} failed and {_n(nw)} borderline checks{sig}.{tail}"


ACTION_WORDS = {"drop_duplicates": "remove exact duplicate rows", "downsample": "keep only a part of the rows", "drop_regime": "leave out one operating condition", "drop_group": "leave out some groups", "drop_range": "leave out a range of rows", "drop_signal": "leave out one or more signals", "add_more_like": "add more data of the same kind", "add_file": "add another file"}


def _k_assessor_evaluation(E: _Ev) -> str:
    v = E.v
    act = v.get("action") if isinstance(v.get("action"), dict) else {}
    typ = str(act.get("type") or "")
    rec = str(v.get("recommendation") or "")
    advice = {"recommend": "it is worth doing", "advise_against": "better not", "neutral": "it makes little difference either way"}.get(rec, rec.replace("_", " ") or "no clear advice")
    rationale = E.st.split(" - ", 1)[1] if " - " in E.st else ""
    rationale = dejargon(rationale)
    tail = f" {_sentence(rationale)}" if rationale else ""
    return f"The system tried out the idea to {ACTION_WORDS.get(typ, typ.replace('_', ' ') or 'change the data')} on a copy of the data to see what it would change. Its advice: {advice}.{tail} Nothing is changed until a person approves it."


def _k_new_file(E: _Ev) -> str:
    v = E.v
    return f"A new file was compared with this run: it has {_n(v.get('n_rows'))} rows and {_n(v.get('n_cols'))} columns, and {_n(v.get('mapped'))} of its columns could be matched to signals the system already knows. This shows whether adding it would bring new operating conditions or just more of the same."


def _k_human_decision(E: _Ev) -> str:
    v = E.v
    who = str(v.get("actor") or "A person") + (f" ({v.get('role')})" if v.get("role") else "")
    body = E.st.split(":", 1)[1] if ":" in E.st else E.st
    body = dejargon(body).rstrip(".")
    target = E.sigs[0] if E.d.get("signals") else "this item"
    return f"{who} made a decision about {target}: {body}. A decision made by a person is recorded in the log and takes priority over the system's own guess."


def _k_curation(E: _Ev) -> str:
    v = E.v
    dropped = v.get("columns_dropped") or []
    cols = f", and {len(dropped)} {_plural(len(dropped), 'column was', 'columns were')} left out" if dropped else ""
    return f"An approved clean-up step was applied to a copy of the data: the number of rows went from {_n(v.get('n_rows_before'))} to {_n(v.get('n_rows_after'))}{cols}. The original file is untouched."


_KIND: dict[str, Callable[[_Ev], str]] = {
    "format": _k_format, "header": _k_header, "orientation": _k_orientation, "sampling": _k_sampling, "typing": _k_typing,
    "name_hint": _k_name_hint, "time": _k_time, "period": _k_period, "counter": _k_counter, "grouping": _k_grouping,
    "label_detection": _k_label_detection, "domain": _k_domain,
    "distribution": _k_distribution, "fingerprint": _k_distribution, "dynamics": _k_dynamics, "noise": _k_noise, "steps": _k_steps,
    "hold": _k_hold, "constant": _k_constant, "derived": _k_derived, "redundancy": _k_redundancy, "cluster": _k_cluster,
    "correlation": _k_correlation, "lag": _k_lag, "role": _k_role,
    "stuck": _k_stuck, "stale": _k_stale, "saturation": _k_saturation, "missing": _k_missing, "dropout": _k_dropout,
    "impossible_value": _k_impossible, "out_of_range": _k_out_of_range, "range": _k_out_of_range, "unit_shift": _k_unit_shift,
    "quantization_change": _k_quantization, "sign_violation": _k_sign, "empty_rows": _k_empty_rows,
    "duplicate_rows": _k_duplicate_rows, "duplicate": _k_duplicate_rows, "duplicates": _k_duplicate_rows, "duplicate_key": _k_duplicate_key,
    "duplicate_timestamp": _k_duplicate_ts, "out_of_order": _k_out_of_order, "gap": _k_gap, "timeliness": _k_gap,
    "irregular_sampling": _k_irregular, "relation_break": _k_relation_break, "check_pass": _k_check_pass,
    "rule_violation": _k_rule_violation, "rule_pass": _k_rule_pass, "rule_compile": _k_rule_compile, "rule": _k_rule_generic,
    "trust": _k_trust, "batching": _k_batching, "alignment": _k_alignment,
    "threshold": _k_threshold, "baseline": _k_baseline, "baseline_candidate": _k_baseline_candidate,
    "changepoint": _k_changepoint, "onset": _k_changepoint, "contribution": _k_contribution, "segment": _k_segment,
    "cause": _k_cause, "cascade": _k_cascade, "propagation": _k_cascade, "pattern": _k_pattern,
    "learning_curve": _k_learning_curve, "regime_coverage": _k_regime_coverage, "coverage": _k_coverage,
    "signal_information": _k_signal_information, "dq_score": _k_dq_score, "assessor_evaluation": _k_assessor_evaluation,
    "new_file_profile": _k_new_file, "curation": _k_curation, "human_decision": _k_human_decision,
}
KINDS = tuple(sorted(_KIND))

_BANNED = (("AUROC", "separation quality"), ("auroc", "separation quality"), ("sigma", "times its normal spread"), ("σ", "times its normal spread"))


def _final(text: str) -> str:
    """Safety net applied to every result: no structures, no leaked jargon, tidy sentence."""
    s = _strip_structures(text)
    s = re.sub(r"\br\s*=\s*([+-]?\d*\.?\d+)", _r_words, s)
    for bad, good in _BANNED:
        if bad in s:
            s = s.replace(bad, good)
    return _sentence(s)


def explain(ev: Any, lang: str = "en") -> str:
    """One to three everyday sentences for an evidence item. Deterministic; never empty; never raw JSON."""
    E = _Ev(ev)
    kind = E.kind
    fn = _KIND.get(kind)
    if fn is None and kind.startswith("rule"):
        fn = _k_rule_generic
    if fn is None and kind.endswith("_ok"):
        fn = _k_check_pass
    text = ""
    if fn is not None:
        try:
            text = fn(E) or ""
        except Exception:
            text = ""
    if not text.strip():
        try:
            text = _generic(E)
        except Exception:
            text = ""
    out = _final(text)
    return out or "An observation was recorded, but it carries no description."


def with_plain(item: dict[str, Any], lang: str = "en") -> dict[str, Any]:
    """Copy of an evidence dict with `plain` and `ref_type` filled in."""
    out = dict(item)
    out.setdefault("ref_type", "evidence")
    if not out.get("plain"):
        out["plain"] = explain(out, lang)
    return out


# =====================================================================================================
# other citable objects -> evidence-like items
# =====================================================================================================
REF_KINDS = {"EV": "evidence", "DIAG": "diagnosis", "FLAG": "flag", "CHK": "check", "INF": "inference", "RULE": "rule", "PATTERN": "pattern", "EGR": "egress"}
_ID_RE = re.compile(r"^(EV|DIAG|FLAG|CHK|INF|EGR|RULE|PATTERN)-([A-Za-z0-9]+)$")
_BATCH_RE = re.compile(r"^B\d{4,6}$")


def _clean_prose(s: Any) -> str:
    """Server-side twin of the UI's cleanText: drop '[llm-…] {json}' tails and source tags, keep the prose."""
    out = str(s or "")
    m = re.search(r"\[(?:llm-[^\]]*|template)\]\s*(?=[{\[])", out)
    if m:
        before = out[: m.start()].strip()
        if before:
            out = before
        else:
            frag = out[m.end():]
            inner = ""
            for key in ("text", "summary", "objection", "message", "answer", "statement"):
                mm = re.search(r"['\"]" + key + r"['\"]\s*:\s*(?:\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)')", frag)
                if mm:
                    inner = (mm.group(1) if mm.group(1) is not None else mm.group(2)).replace("\\n", " ").replace('\\"', '"').replace("\\'", "'")
                    break
            out = inner
    out = _TAG_RE.sub("", out)
    return re.sub(r"[ \t]+", " ", out).strip()


def _top_signals(ranked: Any, limit: int = 4) -> tuple[list[str], dict[str, float], dict[str, str]]:
    sigs: list[str] = []
    shares: dict[str, float] = {}
    dirs: dict[str, str] = {}
    for r in ranked or []:
        if not isinstance(r, dict) or not r.get("signal"):
            continue
        s = str(r["signal"])
        sigs.append(s)
        c = _f(r.get("contribution"))
        if c is not None:
            shares[s] = round(c, 4)
        if r.get("direction"):
            dirs[s] = str(r["direction"])
        if len(sigs) >= limit:
            break
    return sigs, shares, dirs


def _plain_flag(f: dict[str, Any]) -> str:
    kind = str(f.get("kind") or "anomaly")
    grp = f" (group {f.get('group_id')})" if f.get("group_id") not in (None, "") else ""
    a, b = f.get("row_start"), f.get("row_end")
    where = f"between rows {a} and {b}{grp}" if a is not None and b is not None and a != b else f"at row {a}{grp}" if a is not None else f"here{grp}"
    score, thr = _f(f.get("score")), _f(f.get("threshold"))
    ratio = score / thr if score is not None and thr else None
    sigs, shares, dirs = _top_signals(f.get("signals_ranked"))
    lead = ""
    if sigs:
        s0 = sigs[0]
        d = DIRECTION.get(dirs.get(s0, ""), "")
        sh = shares.get(s0)
        if sh is not None and sh > 0:
            lead = (f" {s0} alone explains {_pct(sh, 0)} of that" if sh >= 0.5 else f" {s0} explains the largest part of that ({_pct(sh, 0)})") + (f" (it {d})." if d else ".")
        else:
            lead = f" The signal to look at first is {s0}" + (f" (it {d})." if d else ".")
    cause = str(f.get("likely_cause_class") or "unknown")
    cause_txt = f" The cause is {CAUSE['unknown']}" if cause == "unknown" else f" The most likely explanation is {CAUSE.get(cause, cause)}"
    cause_txt += f" ({_conf_words(f.get('confidence'))})."
    fid = f.get("id")
    if kind == "dq":
        return f"Alarm {fid} is about the data, not the process: {dejargon(_clean_prose(f.get('statement'))).rstrip('.')}." + cause_txt
    if kind == "rule":
        return f"Alarm {fid}: an operating rule was broken {where}. {_sentence(dejargon(_clean_prose(f.get('statement'))))}"
    if kind == "cascade":
        return f"Alarm {fid}: {where} a disturbance spread from signal to signal step by step instead of hitting everything at once." + (f" It involves {_join(sigs, 4)}." if sigs else "") + " The order in which the signals reacted shows where the problem began." + cause_txt
    if kind == "changepoint":
        movers = [f"{s_} ({DIRECTION[dirs[s_]]})" if dirs.get(s_) in DIRECTION else s_ for s_ in sigs[:4]]
        first = f" The first signals to react were {_join(movers, 4)}; what moves first is usually closest to the cause." if movers else ""
        return f"Alarm {fid}: the behaviour of the process changed {where}.{first}{cause_txt}"
    level = f"the process looked {_x(ratio)} times more unusual than anything seen in normal operation" if ratio is not None and ratio >= 1 else "the process looked unusual compared with normal operation"
    if kind == "drift":
        level = "the process slowly moved away from its normal behaviour" + (f", reaching {_x(ratio)} times {NORMAL_LIMIT}" if ratio is not None and ratio >= 1 else "")
    return f"Alarm {fid}: {where} {level}.{lead}{cause_txt}"


def _plain_diag(d: dict[str, Any]) -> str:
    cause = str(d.get("cause_class") or "unknown")
    grp = f" in group {d.get('group_id')}" if d.get("group_id") not in (None, "") else ""
    sigs, shares, dirs = _top_signals(d.get("ranked_signals"), 3)
    involved = ""
    if sigs:
        bits = []
        for s in sigs:
            extra = [x for x in ((_pct(shares[s], 0) if s in shares and shares[s] > 0 else ""), DIRECTION.get(dirs.get(s, ""), "")) if x]
            bits.append(f"{s} ({', '.join(extra)})" if extra else s)
        involved = f" The signals most involved are {_join(bits, 3)}."
    verdict = str((d.get("critique") or {}).get("verdict") or "") if isinstance(d.get("critique"), dict) else ""
    crit = {"supported": " The diagnosis held up when the system challenged it.", "weakened": " When the system challenged this diagnosis it found weak points, so treat it with some caution.", "rejected": " When the system challenged this diagnosis it did not hold up, so it should not be relied on."}.get(verdict, "")
    lead = f"the system cannot tell whether the process or the measurement is at fault" if cause == "unknown" else f"the system's conclusion is {CAUSE.get(cause, cause)}"
    ft = dejargon(str(d.get("fault_type") or "")).strip()
    return f"Diagnosis {d.get('id')}{grp}: {lead}" + (f', described as "{ft}"' if ft else "") + f" ({_conf_words(d.get('confidence'))}).{involved}{crit}"


_STATUS_WORDS = {"pass": "This check passed.", "warn": "This check raised a warning.", "fail": "This check failed."}


def _plain_check(c: dict[str, Any]) -> str:
    ctype = str(c.get("check_type") or "")
    kind = "check_pass" if ctype.endswith("_ok") else ("rule_violation" if ctype.startswith("rule") and c.get("status") == "fail" and (c.get("values") or {}).get("n_violating") else ("rule_pass" if ctype.startswith("rule") and "holds in batch" in str(c.get("statement")) else ctype))
    body = explain({"kind": kind, "statement": c.get("statement"), "values": c.get("values") or {}, "signals": c.get("signals") or [], "batch_id": c.get("batch_id"), "group_id": c.get("group_id")})
    status = _STATUS_WORDS.get(str(c.get("status")), "")
    return f"{status} {body}".strip()


_GROUP_HOW = {"key_columns": "from the value of the column", "counter_reset": "from the points where the counter starts again:", "time_gaps": "from long pauses in the time stamps", "changepoint": "from abrupt changes in the data", "none": "as a single run"}


def _plain_claim(subject: str, claim: str) -> str:
    """Everyday wording for the claim shapes the pipeline writes; anything else goes through the glossary."""
    c = claim.strip()
    m = re.match(r"structural role:\s*(\w+)", c)
    if m and m.group(1) in ROLE_ONE:
        return f"{subject} behaves like {ROLE_ONE[m.group(1)]}"
    m = re.match(r"instrument hypothesis:\s*(.+)", c)
    if m:
        what = m.group(1).strip().rstrip(".")
        return f"it cannot guess what kind of instrument {subject} is" if what.lower() == "unknown" else f"{subject} may be this kind of instrument: {what} (a guess from how the signal behaves, to be confirmed by a person)"
    m = re.match(r"unit operation hypothesis:\s*(.+)", c)
    if m:
        what = m.group(1).strip().rstrip(".")
        return f"it cannot tell which part of the plant {subject} belongs to" if what.lower() == "unknown" else f"{subject} probably belongs to this part of the plant: {what} (a guess, to be confirmed by a person)"
    m = re.match(r"groups:\s*(\d+)\s+via\s+(\w+)(?:\s+on\s+(.+))?", c)
    if m:
        how = _GROUP_HOW.get(m.group(2), "using " + m.group(2).replace("_", " "))
        col = f" '{m.group(3).strip()}'" if m.group(3) and m.group(2) in ("key_columns", "counter_reset") else ""
        return f"the data consists of {_n(m.group(1))} separate runs, recognised {how}{col}"
    m = re.match(r"data looks like (.+?) \(likelihood ([\d.]+)\)", c)
    if m:
        return f"the data looks like a {m.group(1).replace('_', ' ')} ({_pct(m.group(2), 0)} likely)"
    m = re.match(r"timestamp column:\s*(\S+);\s*sample period ~\s*([\d.]+)\s*s", c)
    if m:
        return f"the column '{m.group(1)}' is the clock of the data and readings are about {_dur(m.group(2))} apart"
    m = re.match(r"Baseline \(normal\) regime = rows selected by '(\w+)' \((\d+)% of the sample\)", c)
    if m:
        return f"normal operation is taken to be the {m.group(2)} % of the sampled rows picked by the strategy to {STRATEGY.get(m.group(1), m.group(1).replace('_', ' '))}"
    out = dejargon(c).rstrip(".")
    return out[0].lower() + out[1:] if out[:1].isupper() and not re.match(r"[A-Z]{2}|S\d", out) else out


def _is_parameter_dump(s: str) -> bool:
    return s.count("=") >= 2 or s.count(";") >= 3 or bool(re.search(r"\b\w+_\w+\b", s))


def _plain_inference(i: dict[str, Any], basis: str = "") -> str:
    status = {"inferred": "This was worked out from measurements", "assumed": "This is an assumption the system had to make", "uncertain": "The system is unsure about this"}.get(str(i.get("status")), "This is a conclusion of the system")
    claim = _plain_claim(str(i.get("subject") or "the data"), _clean_prose(i.get("claim")))
    raw_reason = _clean_prose(i.get("reasoning"))
    reasoning = "" if _is_parameter_dump(raw_reason) else dejargon(raw_reason)
    why = f" Reasoning: {reasoning.rstrip('.')}." if reasoning else (f" It rests on this observation: {basis}" if basis else "")
    human = {"accepted": " A person has accepted it.", "questioned": " A person has questioned it.", "overridden": " A person has overridden it."}.get(str(i.get("human_status")), "")
    return f"The system concluded: {claim}. {status} ({_conf_words(i.get('confidence'))}).{why}{human}"


def _plain_rule(r: dict[str, Any]) -> str:
    status = {"draft": "It is a draft that still waits for approval, so it is not checked yet.", "approved": "It has been approved.", "active": "It is active and checked on every batch.", "rejected": "It was rejected and is not used.", "retired": "It has been retired and is no longer checked."}.get(str(r.get("status")), "")
    expl = dejargon(_clean_prose(r.get("compile_explanation")))
    author = str(r.get("author") or "").replace("human:", "")
    return f'Operating rule {r.get("id")}' + (f" written by {author}" if author and author != "human" else "") + f': "{str(r.get("text") or "").strip().rstrip(".")}". {status}' + (f" How the system understood it: {expl.rstrip('.')}." if expl else "")


def _plain_pattern(p: dict[str, Any]) -> str:
    sig = p.get("signature") if isinstance(p.get("signature"), dict) else {}
    shares = sig.get("mean_shares") if isinstance(sig.get("mean_shares"), dict) else {}
    directions = sig.get("directions") if isinstance(sig.get("directions"), dict) else {}
    ranked = [str(s) for s in (sig.get("ranked_signals") or sig.get("signals") or []) if isinstance(s, str)]
    body = explain({"kind": "pattern", "statement": f"{p.get('id')}: {p.get('description') or ''}", "values": {"n_events": p.get("n_events"), "groups": p.get("groups_affected") or [], "mean_shares": shares, "directions": directions, "lag_order": sig.get("lag_order") if isinstance(sig.get("lag_order"), list) and sig.get("lag_order") and isinstance(sig["lag_order"][0], (list, tuple)) else []}, "signals": ranked})
    if not shares and ranked:
        body += f" It mainly involves {_join(ranked, 4)}."
    name = f' A person named it "{p.get("name")}".' if p.get("name") else " Nobody has given it a name yet."
    rel = _f(p.get("classifier_reliability"))
    rec = f" New events of this kind are recognised with a reliability of {_pct(rel, 0)}." if rel is not None else ""
    return body + name + rec


def _plain_egress(r: dict[str, Any]) -> str:
    task = str(r.get("task") or "a task").replace("_", " ")
    purpose = f" ({dejargon(r.get('purpose')).rstrip('.')})" if r.get("purpose") else ""
    model = str(r.get("model") or r.get("provider") or "a model")
    guard = str(r.get("guard_result") or "")
    size = _f(r.get("payload_bytes"))
    kb = f", {_num(size / 1024)} kB" if size else ""
    arts = _join([str(a).replace("_", " ") for a in (r.get("artifact_types") or [])], 5)
    if guard == "blocked":
        return f"Record of a model call for the task '{task}'{purpose}: the egress guard blocked it" + (f" ({dejargon(r.get('guard_reason')).rstrip('.')})" if r.get("guard_reason") else "") + ". Nothing was sent outside this machine."
    if str(r.get("route")) == "external":
        return f"Record of a model call for the task '{task}'{purpose}: it was sent to the external model {model} after the egress guard checked it. Only derived summaries were sent" + (f" ({arts}{kb})" if arts else "") + ", never raw rows."
    ok = "" if r.get("ok", True) else " The call failed, so the built-in template text was used instead."
    return f"Record of a model call for the task '{task}'{purpose}: it was handled by the local model {model} on this machine, so no data left the machine.{ok}"


def _plain_batch(tv: dict[str, Any]) -> str:
    score = _f(tv.get("trust_score"))
    bad = tv.get("untrusted_signals") or []
    verdict = "can be trusted" if tv.get("trusted") else "cannot be trusted"
    sig = f" Signals with reliability problems: {_join(bad, 6)}." if bad else ""
    local = len(tv.get("local_untrusted") or [])
    loc = f" In addition, {local} short {_plural(local, 'stretch was', 'stretches were')} marked unreliable for a single signal." if local else ""
    return f"The data of batch {tv.get('batch_id')} {verdict} (rated {_pct(score, 0)} trustworthy).{sig}{loc}"


def explain_object(ref_type: str, obj: Any, lang: str = "en") -> str:
    """Plain sentence for a non-evidence object (dict or pydantic): diagnosis | flag | check | inference |
    rule | pattern | egress | batch. `evidence` falls through to explain()."""
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump()
    obj = obj if isinstance(obj, dict) else {}
    fn = {"flag": _plain_flag, "diagnosis": _plain_diag, "check": _plain_check, "inference": _plain_inference, "rule": _plain_rule, "pattern": _plain_pattern, "egress": _plain_egress, "batch": _plain_batch}.get(ref_type)
    if fn is None:
        return explain(obj, lang)
    try:
        text = fn(obj)
    except Exception:
        text = ""
    if not text.strip():
        text = dejargon(_clean_prose(obj.get("statement") or obj.get("summary") or obj.get("claim") or obj.get("text") or obj.get("description") or "")) or f"{ref_type.capitalize()} {obj.get('id', '')} has no description."
    return _final(text)


# ---------- item builders (compact: only what the popover needs) ----------
def _round(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, dict):
        return {k: _round(x) for k, x in v.items()}
    return v


def _small(d: dict[str, Any]) -> dict[str, Any]:
    return {k: _round(v) for k, v in d.items() if v is not None and v != "" and v != [] and v != {}}


def _item_flag(f: dict[str, Any]) -> dict[str, Any]:
    sigs, shares, dirs = _top_signals(f.get("signals_ranked"), 6)
    return {"id": f.get("id"), "kind": "flag", "ref_type": "flag", "statement": _clean_prose(f.get("statement")), "plain": explain_object("flag", f), "signals": sigs,
            "values": _small({"flag_kind": f.get("kind"), "severity": f.get("severity"), "score": f.get("score"), "threshold": f.get("threshold"), "rows": f"{f.get('row_start')}-{f.get('row_end')}", "likely_cause": f.get("likely_cause_class"), "confidence": f.get("confidence"), "pattern_id": f.get("pattern_id"), "top_signals": shares, "human_status": f.get("human_status")}),
            "group_id": f.get("group_id"), "batch_id": f.get("batch_id"), "evidence_ids": list(f.get("evidence_ids") or []), "computed_by": f.get("detector"), "created_at": f.get("created_at"),
            "open": {"view": "monitor", "params": {"flag": f.get("id")}}}


def _item_diag(d: dict[str, Any]) -> dict[str, Any]:
    sigs, shares, _ = _top_signals(d.get("ranked_signals"), 6)
    crit = d.get("critique") if isinstance(d.get("critique"), dict) else {}
    return {"id": d.get("id"), "kind": "diagnosis", "ref_type": "diagnosis", "statement": _clean_prose(d.get("summary")) or str(d.get("fault_type") or ""), "plain": explain_object("diagnosis", d), "signals": sigs,
            "values": _small({"fault_type": d.get("fault_type"), "cause": d.get("cause_class"), "confidence": d.get("confidence"), "top_signals": shares, "flags": list(d.get("flag_ids") or [])[:8], "pattern_id": d.get("pattern_id"), "critique": crit.get("verdict"), "human_status": d.get("human_status")}),
            "group_id": d.get("group_id"), "batch_id": None, "evidence_ids": list(d.get("evidence_ids") or []), "computed_by": f"diagnose ({d.get('narrative_source') or 'template'})", "created_at": d.get("created_at"),
            "open": {"view": "diagnoses", "params": {"diag": d.get("id")}}}


def _item_check(c: dict[str, Any]) -> dict[str, Any]:
    vals = {k: v for k, v in (c.get("values") or {}).items() if k not in ("events", "episodes", "examples", "gaps") and not isinstance(v, (dict, list))}
    rows = f"{c.get('row_start')}-{c.get('row_end')}" if c.get("row_start") is not None else None
    return {"id": c.get("check_id"), "kind": "check", "ref_type": "check", "statement": _clean_prose(c.get("statement")), "plain": explain_object("check", c), "signals": list(c.get("signals") or []),
            "values": _small({"status": c.get("status"), "check_type": c.get("check_type"), "category": c.get("category"), "severity": c.get("severity"), "rows": rows, "rule_id": c.get("rule_id"), **vals}),
            "group_id": c.get("group_id"), "batch_id": c.get("batch_id"), "evidence_ids": list(c.get("evidence_ids") or []), "computed_by": f"quality.checks.{c.get('check_type')}", "created_at": c.get("created_at"),
            "open": {"view": "quality", "params": _small({"batch": c.get("batch_id"), "check": c.get("check_id")})}}


def _item_inference(i: dict[str, Any], basis: str = "") -> dict[str, Any]:
    subj = str(i.get("subject") or "")
    is_sig = bool(re.fullmatch(r"S\d{2,3}", subj))
    claim, reasoning = _clean_prose(i.get("claim")), _clean_prose(i.get("reasoning"))
    return {"id": i.get("id"), "kind": "inference", "ref_type": "inference", "statement": claim + (f" — {reasoning}" if reasoning else ""), "plain": _final(_plain_inference(i, basis)), "signals": [subj] if is_sig else [],
            "values": _small({"status": i.get("status"), "confidence": i.get("confidence"), "subject": subj, "stage": i.get("stage"), "source": i.get("source"), "alternatives": "; ".join(str(a) for a in (i.get("alternatives") or [])[:4]), "human_status": i.get("human_status")}),
            "group_id": None, "batch_id": None, "evidence_ids": list(i.get("evidence_ids") or []), "computed_by": i.get("source"), "created_at": i.get("created_at"),
            "open": {"view": "understanding", "params": {"signal": subj} if is_sig else {}}}


def _item_rule(r: dict[str, Any]) -> dict[str, Any]:
    comp = r.get("compiled") if isinstance(r.get("compiled"), dict) else {}
    sigs = [str(x) for x in ([comp.get("signal")] + list(comp.get("signals") or [])) if isinstance(x, str)] or list(dict.fromkeys(SIG_RE.findall(str(r.get("text") or ""))))
    expl = _clean_prose(r.get("compile_explanation"))
    return {"id": r.get("id"), "kind": "rule", "ref_type": "rule", "statement": str(r.get("text") or "") + (f" — {expl}" if expl else ""), "plain": explain_object("rule", r), "signals": sigs,
            "values": _small({"status": r.get("status"), "author": r.get("author"), "check_type": comp.get("type"), "compile_source": r.get("compile_source"), "compile_confidence": r.get("compile_confidence")}),
            "group_id": None, "batch_id": None, "evidence_ids": [], "inference_ids": list(r.get("inference_ids") or []), "computed_by": r.get("compile_source"), "created_at": r.get("created_at"),
            "open": {"view": "quality", "params": {"rule": r.get("id")}}}


def _item_pattern(p: dict[str, Any]) -> dict[str, Any]:
    sig = p.get("signature") if isinstance(p.get("signature"), dict) else {}
    ranked = [str(s) for s in (sig.get("ranked_signals") or sig.get("signals") or []) if isinstance(s, str)][:6]
    return {"id": p.get("id"), "kind": "pattern", "ref_type": "pattern", "statement": _clean_prose(p.get("description")) or str(p.get("id")), "plain": explain_object("pattern", p), "signals": ranked,
            "values": _small({"name": p.get("name"), "n_events": p.get("n_events"), "n_groups": len(p.get("groups_affected") or []), "confidence": p.get("confidence"), "classifier_reliability": p.get("classifier_reliability"), "top_signals": sig.get("mean_shares") if isinstance(sig.get("mean_shares"), dict) else None}),
            "group_id": None, "batch_id": None, "evidence_ids": list(p.get("evidence_ids") or []), "computed_by": "detect.patterns", "open": {"view": "diagnoses", "params": {"pattern": p.get("id")}}}


def _item_egress(r: dict[str, Any]) -> dict[str, Any]:
    st = f"{r.get('task')}: {r.get('purpose') or ''} — {r.get('route')} {r.get('provider') or ''} {r.get('model') or ''}; guard {r.get('guard_result')}" + (f" ({r.get('guard_reason')})" if r.get("guard_reason") else "")
    return {"id": r.get("id"), "kind": "egress", "ref_type": "egress", "statement": re.sub(r"\s+", " ", st).strip(), "plain": explain_object("egress", r), "signals": [],
            "values": _small({"task": r.get("task"), "route": r.get("route"), "provider": r.get("provider"), "model": r.get("model"), "guard_result": r.get("guard_result"), "artifacts": ", ".join(str(a) for a in (r.get("artifact_types") or [])), "payload_bytes": r.get("payload_bytes"), "ok": r.get("ok"), "latency_ms": r.get("latency_ms")}),
            "group_id": None, "batch_id": None, "evidence_ids": [], "computed_by": "llm.ledger", "created_at": r.get("ts"), "open": {"view": "dataflow", "params": {"egress": r.get("id")}}}


def _item_batch(tv: dict[str, Any]) -> dict[str, Any]:
    return {"id": tv.get("batch_id"), "kind": "batch", "ref_type": "batch", "statement": _clean_prose(tv.get("statement")) or f"Batch {tv.get('batch_id')}", "plain": explain_object("batch", tv), "signals": list(tv.get("untrusted_signals") or [])[:12],
            "values": _small({"trusted": tv.get("trusted"), "trust_score": tv.get("trust_score"), "n_rows": tv.get("n_rows"), "n_checks": len(tv.get("check_ids") or [])}),
            "group_id": None, "batch_id": tv.get("batch_id"), "evidence_ids": [], "check_ids": list(tv.get("check_ids") or [])[:40], "computed_by": "quality.trust", "created_at": tv.get("created_at"),
            "open": {"view": "quality", "params": {"batch": tv.get("batch_id")}}}


# ---------- per-run index of the file-backed objects, cached on (mtime, size) ----------
_SOURCES: dict[str, tuple[str, str, Callable[[dict[str, Any]], dict[str, Any]]]] = {
    # ref_type: (file name, id key, item builder)
    "flag": ("flags.jsonl", "id", _item_flag),
    "diagnosis": ("diagnoses.jsonl", "id", _item_diag),
    "check": ("checks.jsonl", "check_id", _item_check),
    "rule": ("rules.json", "id", _item_rule),
    "pattern": ("patterns.json", "id", _item_pattern),
    "egress": ("egress_ledger.jsonl", "id", _item_egress),
    "batch": ("trust.jsonl", "batch_id", _item_batch),
}
_INDEX: dict[tuple[str, str], tuple[tuple[int, int], dict[str, dict[str, Any]]]] = {}
_INDEX_LOCK = threading.RLock()
_INDEX_MAX = 48  # (run, artifact) pairs kept in memory


def _load_records(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if path.suffix == ".jsonl":
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if isinstance(o, dict):
                    out.append(o)
        return out
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("rules") or data.get("patterns") or data.get("items") or []
    return [o for o in data if isinstance(o, dict)] if isinstance(data, list) else []


def _index_for(ws: Any, ref_type: str) -> dict[str, dict[str, Any]]:
    """{id: compact item} for one artifact of one run; rebuilt only when the file changed."""
    fname, id_key, build = _SOURCES[ref_type]
    path = Path(ws.dir) / fname
    try:
        st = path.stat()
    except OSError:
        return {}
    stamp = (st.st_mtime_ns, st.st_size)
    key = (str(path.parent), ref_type)
    with _INDEX_LOCK:
        hit = _INDEX.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    try:
        records = _load_records(path)
    except (OSError, ValueError):
        return hit[1] if hit is not None else {}
    idx: dict[str, dict[str, Any]] = {}
    for rec in records:
        rid = rec.get(id_key)
        if not rid:
            continue
        try:
            idx[str(rid)] = build(rec)
        except Exception:
            continue
    with _INDEX_LOCK:
        if len(_INDEX) >= _INDEX_MAX and key not in _INDEX:
            _INDEX.pop(next(iter(_INDEX)))
        _INDEX[key] = (stamp, idx)
    return idx


def _canonical(raw: str) -> str:
    """'[diag-5]' -> 'DIAG-000005'; unknown shapes are returned trimmed."""
    s = str(raw or "").strip().strip("[](){}<>.,;:'\"").strip()
    m = _ID_RE.match(s.upper())
    if not m:
        return s.upper() if _BATCH_RE.match(s.upper()) else s
    prefix, tail = m.group(1), m.group(2)
    if prefix in ("EV", "DIAG", "FLAG", "CHK", "INF", "EGR") and tail.isdigit():
        tail = tail.zfill(6)
    elif prefix == "RULE" and tail.isdigit():
        tail = tail.zfill(3)
    return f"{prefix}-{tail}"


def resolve_ref(ws: Any, raw_id: str, lang: str = "en") -> Optional[dict[str, Any]]:
    """Evidence-like item for any known object id of the run, or None."""
    cid = _canonical(raw_id)
    if not cid:
        return None
    item: Optional[dict[str, Any]] = None
    prefix = cid.split("-", 1)[0]
    ref_type = REF_KINDS.get(prefix) or ("batch" if _BATCH_RE.match(cid) else None)
    if ref_type == "evidence":
        e = ws.evidence.get(cid)
        if e is not None:
            item = with_plain(e.model_dump(), lang)
    elif ref_type == "inference":
        i = ws.inferences.get(cid)
        if i is not None:
            basis = ""
            cited = [e for e in (ws.evidence.get(eid) for eid in (i.evidence_ids or [])[:12]) if e is not None]
            main = [e for e in cited if not e.kind.endswith("_candidate") and e.kind != "sampling"] or cited
            if main:
                basis = explain(main[0], lang)
            item = _item_inference(i.model_dump(), basis)
    elif ref_type in _SOURCES:
        idx = _index_for(ws, ref_type)
        found = idx.get(cid)
        if found is None and ref_type == "rule":  # RULE-1 vs RULE-001 vs RULE-0001
            num = cid.split("-", 1)[1].lstrip("0")
            found = next((v for k, v in idx.items() if k.split("-", 1)[-1].lstrip("0") == num), None)
        item = dict(found) if found is not None else None
    if item is not None and str(raw_id).strip() != item.get("id"):
        item["requested_id"] = str(raw_id).strip()
    return item


def resolve_refs(ws: Any, ids: Iterable[str], lang: str = "en") -> tuple[list[dict[str, Any]], list[str]]:
    """(items, missing) in the order asked; duplicates are returned once."""
    items: list[dict[str, Any]] = []
    missing: list[str] = []
    seen: set[str] = set()
    for raw in ids:
        raw = str(raw or "").strip()
        if not raw:
            continue
        it = resolve_ref(ws, raw, lang)
        if it is None:
            if raw not in missing:
                missing.append(raw)
            continue
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        items.append(it)
    return items, missing
