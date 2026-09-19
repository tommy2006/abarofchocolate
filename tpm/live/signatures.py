"""Known failure types ("signatures") for the live monitor: load them from YAML, match their sensors to the live
feed's columns, and score how far the current sensor behaviour has moved towards each of them.

The catalogue is data (``config/failure_signatures.yaml``, plus any ``*.yaml`` a judge drops into the monitor's
signature folder). Matching is generic: a signature names sensors and one behaviour pattern; the score is the mean
pattern indicator over its sensors, computed from ``tpm.live.engine.features`` (statistics of the rolling window
against the learned baseline). Fault numbers and this mapping stay in this process: nothing here is ever passed to
``tpm.llm`` or to any external API.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

PATTERNS = ("mean_shift", "variance_increase", "collapse", "slow_drift", "jumpy", "brief_bump", "fading_shift", "none")
# when two signatures match on the same sensors, the more specific behaviour wins the "cause" slot
SPECIFICITY = {"collapse": 3, "jumpy": 3, "variance_increase": 2, "brief_bump": 2, "fading_shift": 2, "slow_drift": 2, "mean_shift": 1, "none": 0}
CONFIDENCES = ("documented", "hypothesis")
OCCURRING = 0.6            # match score at which a failure type is "occurring" (with at least half of its sensors matching)
IMMINENT = 0.3             # ... "imminent" when the score is here and rising, or projected to cross OCCURRING within HORIZON cycles
HORIZON = 5
LEAVE_OCCURRING = 0.45     # hysteresis: an occurring signature stays until its score falls below this
LEAVE_IMMINENT = 0.2
COLUMN_MATCH = 0.5         # a sensor "matches" when its own indicator is at least this
COLUMN_MOVING = 0.2        # ... and is "moving towards" the pattern from this (imminent needs half of the sensors moving)
SCORE_KEEP = 8             # cycles of score history kept per signature
CONFIG_FILE = Path(__file__).resolve().parents[2] / "config" / "failure_signatures.yaml"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
STATUSES = ("quiet", "imminent", "occurring", "na", "invisible")


# ------------------------------------------------------------------ loading
def _normalize(raw: Any, source: str) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict) or not _ID_RE.match(str(raw.get("id") or "")):
        return None
    pattern = str(raw.get("pattern") or "none").strip().lower()
    if pattern not in PATTERNS:
        return None
    cols = raw.get("columns") or []
    if not isinstance(cols, (list, tuple)):
        cols = [cols]
    cols = [str(c).strip() for c in cols if str(c).strip()]
    conf = str(raw.get("confidence") or "hypothesis").strip().lower()
    generic = bool(raw.get("generic")) and not cols
    visible = raw.get("visible")
    visible = bool(visible) if visible is not None else (pattern != "none" and (bool(cols) or generic))
    if pattern == "none" or (not cols and not generic):
        visible = False
    out = {
        "id": str(raw["id"]), "fault": raw.get("fault"), "name": str(raw.get("name") or raw["id"]).strip(),
        "name_i18n": {k[5:]: str(v) for k, v in raw.items() if k.startswith("name_") and isinstance(v, str)},
        "columns": cols, "generic": generic, "pattern": pattern, "confidence": conf if conf in CONFIDENCES else "hypothesis",
        "visible": visible, "suggestion": str(raw.get("suggestion") or "").strip(),
        "suggestion_i18n": {k[11:]: str(v) for k, v in raw.items() if k.startswith("suggestion_") and isinstance(v, str)},
        "note": str(raw.get("note") or "").strip(), "source": source,
    }
    return out


def load(extra_dirs: Iterable[str | Path] = (), config_file: Optional[Path] = None) -> tuple[list[dict[str, Any]], list[str]]:
    """The catalogue: the built-in file first, then every *.yaml / *.yml in ``extra_dirs`` (a later entry with the same
    id replaces an earlier one, so judges can override as well as add). Returns (signatures, files read)."""
    files: list[Path] = []
    cf = Path(config_file) if config_file else CONFIG_FILE
    if cf.is_file():
        files.append(cf)
    for d in extra_dirs:
        d = Path(d)
        if d.is_dir():
            files.extend(sorted(p for p in d.iterdir() if p.suffix.lower() in (".yaml", ".yml") and p.is_file()))
    by_id: dict[str, dict[str, Any]] = {}
    sources: list[str] = []
    for p in files:
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        items = doc.get("signatures") if isinstance(doc, dict) else doc
        if not isinstance(items, list):
            continue
        n = 0
        for raw in items:
            s = _normalize(raw, p.name)
            if s is not None:
                by_id.pop(s["id"], None)
                by_id[s["id"]] = s
                n += 1
        if n:
            sources.append(str(p))
    return list(by_id.values()), sources


# ------------------------------------------------------------------ matching sensors
def _key(s: str) -> str:
    return re.sub(r"[\s_\-\.]+", "", str(s).strip().lower())


def match_columns(columns: list[str], sensors: list[str], names: Optional[dict[str, list[str]]] = None) -> tuple[dict[str, str], list[str]]:
    """Map a signature's column names onto the live feed's sensors: exact name, then case / separator-insensitive
    name, then any alternative name in ``names`` (positional alias S01.., operator display name). Returns
    ({column: sensor}, [columns not found])."""
    names = names or {}
    exact = {s: s for s in sensors}
    loose: dict[str, str] = {}
    for s in sensors:
        loose.setdefault(_key(s), s)
        for alt in names.get(s) or []:
            loose.setdefault(_key(alt), s)
    found: dict[str, str] = {}
    missing: list[str] = []
    for c in columns:
        s = exact.get(c) or loose.get(_key(c))
        if s and s not in found.values():
            found[c] = s
        else:
            missing.append(c)
    return found, missing


# ------------------------------------------------------------------ pattern indicators
def _clamp(x: float) -> float:
    return 0.0 if not math.isfinite(x) else max(0.0, min(1.0, x))


def indicator(pattern: str, f: dict[str, Any]) -> float:
    """How strongly one sensor shows one behaviour pattern, 0 (not at all) to 1 (clearly). Each pattern has its own
    threshold: mean_shift reaches 1 at 4 spreads from normal, variance_increase at 3x the normal jitter, ..."""
    z = float(f.get("z", 0.0))
    az = abs(z)
    hist = [float(x) for x in (f.get("z_hist") or [])]
    if pattern == "mean_shift":
        return _clamp((az - 1.0) / 3.0)
    if pattern == "variance_increase":
        # more spread than normal, unless the extra spread comes from a step pattern (jumps with normal moves between
        # them): a valve that sticks and jumps is "jumpy", not more jitter
        return _clamp((float(f.get("var_ratio", 1.0)) - 1.0) / 2.0) * (1.0 - indicator("jumpy", f))
    if pattern == "collapse":
        depth = _clamp((-z - 4.0) / 6.0)                                       # far below normal (1 at 10 spreads) ...
        fast = float(f.get("slope", 0.0)) <= -2.0 or any(x - z >= 3.0 for x in hist[-6:])   # ... and it fell fast, recently
        return depth if fast else depth * 0.15                                # settled at the low level = a mean shift now
    if pattern == "slow_drift":
        steady = float(f.get("mono", 0.0)) >= 0.75 and len(hist) >= 3
        return _clamp((az - 1.0) / 3.0) * (1.0 if steady else 0.3)
    if pattern == "jumpy":
        # share of big reading-to-reading jumps: both clearly more than normal AND enough of them (4 % of the readings),
        # while the typical move between the jumps stays normal (sticking). When every move is bigger (1.45x normal or
        # more) the jumps are just more jitter: that is a variance increase.
        ratio = float(f.get("jump_ratio", 1.0))
        excess = float(f.get("steps", 0.0)) - float(f.get("step_ref", 0.0))
        sticky = _clamp((1.45 - float(f.get("move_ratio", 1.0))) / 0.3)
        return _clamp((ratio - 1.0) / 3.0) * _clamp(excess / 0.04) * sticky
    if pattern == "brief_bump":
        peak = max([abs(x) for x in hist[-3:]] + [az])
        return _clamp((peak - 1.0) / 3.0) * (1.0 if az < peak - 0.5 else 0.8)   # a bump that is already compensating counts fully
    if pattern == "fading_shift":
        peak = max([abs(x) for x in hist[-4:]] + [az])
        return _clamp((peak - 1.0) / 3.0)
    return 0.0


def _how(pattern: str, f: dict[str, Any]) -> dict[str, Any]:
    """A short, translatable description of what the sensor does (key + vars), for the alarm card."""
    z = float(f.get("z", 0.0))
    if pattern == "variance_increase":
        return {"key": "sig.how.variance", "vars": {"r": f"{float(f.get('var_ratio', 1.0)):.1f}"}}
    if pattern == "jumpy":
        return {"key": "sig.how.jumpy", "vars": {"r": f"{float(f.get('jump_ratio', 1.0)):.1f}"}}
    if pattern == "collapse":
        return {"key": "sig.how.collapse", "vars": {"z": f"{abs(z):.1f}"}}
    if pattern == "slow_drift":
        return {"key": "sig.how.drift.up" if z > 0 else "sig.how.drift.down", "vars": {"z": f"{abs(z):.1f}"}}
    return {"key": "sig.how.shift.up" if z > 0 else "sig.how.shift.down", "vars": {"z": f"{abs(z):.1f}"}}


# ------------------------------------------------------------------ scoring + status
def _direction(scores: list[float]) -> tuple[str, float]:
    """Direction of travel over the last few cycles and the projected score HORIZON cycles ahead."""
    if len(scores) < 2:
        return "flat", scores[-1] if scores else 0.0
    diffs = [scores[i + 1] - scores[i] for i in range(len(scores) - 1)][-3:]
    slope = sum(diffs) / len(diffs)
    last = scores[-1] - scores[-2]
    direction = "rising" if last > 0.03 or (slope > 0.02 and last >= 0) else "falling" if last < -0.03 else "flat"
    return direction, _clamp(scores[-1] + HORIZON * slope)


def _status(prev: str, score: float, matched: int, moving: int, n: int, direction: str, projected: float, need: Optional[int] = None) -> str:
    """occurring: score >= 0.6 and at least half of the sensors match. imminent: 0.3 <= score < 0.6 on at least half of
    the sensors, and the score is rising or projected to cross 0.6 within HORIZON cycles. Hysteresis: occurring holds
    down to 0.45, imminent holds while the score stays >= 0.3 (or >= 0.2 and still rising). ``need`` replaces "half of
    the sensors" (a generic signature needs all of its sensors)."""
    half = need if need is not None else max(1, math.ceil(n / 2))
    if score >= OCCURRING and matched >= half:
        return "occurring"
    if prev == "occurring" and score >= LEAVE_OCCURRING and moving >= half:
        return "occurring"
    if IMMINENT <= score < OCCURRING and moving >= half and (direction == "rising" or projected >= OCCURRING):
        return "imminent"
    if prev in ("imminent", "occurring") and score >= IMMINENT and moving >= half:
        return "imminent"
    if prev == "imminent" and score >= LEAVE_IMMINENT and direction == "rising":
        return "imminent"
    return "quiet"


def evaluate(sigs: list[dict[str, Any]], feats: dict[str, dict[str, Any]], sensors: list[str], names: Optional[dict[str, list[str]]],
             state: dict[str, dict[str, Any]], *, exclude: Iterable[str] = (), when: str = "") -> list[dict[str, Any]]:
    """Score every signature against this cycle's features and update its status with hysteresis. ``state`` (per
    signature id: score history, status, since) is owned by the caller and updated in place. Sensors in ``exclude``
    (dead / missing) never take part. Returns one row per signature, best match first."""
    excluded = set(exclude)
    usable = [s for s in sensors if s in feats and s not in excluded]
    rows: list[dict[str, Any]] = []
    for sig in sigs:
        st = state.setdefault(sig["id"], {"scores": [], "status": "quiet", "since": None})
        row = {k: sig[k] for k in ("id", "fault", "name", "name_i18n", "columns", "generic", "pattern", "confidence", "visible", "suggestion", "suggestion_i18n", "note", "source")}
        resolved: list[dict[str, Any]] = []
        missing: list[str] = []
        if not sig["visible"] or sig["pattern"] == "none":
            status, score, n = "invisible", 0.0, 0
        elif sig["generic"]:
            cands = sorted(((indicator(sig["pattern"], feats[s]), s) for s in usable), reverse=True)
            top = [(i, s) for i, s in cands[:2]]
            n = 2 if len(usable) >= 2 else len(usable)
            score = sum(i for i, _ in top) / n if n else 0.0
            resolved = [{"column": s, "sensor": s, "ind": round(i, 3), "z": round(feats[s]["z"], 2), "how": _how(sig["pattern"], feats[s])} for i, s in cands[:4] if i >= IMMINENT] or [{"column": s, "sensor": s, "ind": round(i, 3), "z": round(feats[s]["z"], 2), "how": _how(sig["pattern"], feats[s])} for i, s in top]
            status = None
        else:
            found, missing = match_columns(sig["columns"], usable, names)
            n_all = len(sig["columns"])
            # a signature can only be judged when at least half of its sensors (and at least two of them) are in the feed
            if len(found) < max(1, math.ceil(n_all / 2)) or (n_all >= 2 and len(found) < 2):
                _, missing = match_columns(sig["columns"], sensors, names)
                status, score, n = "na", 0.0, 0
                if not missing:                                             # present but dead / missing this cycle
                    missing = list(sig["columns"])
            else:
                n = len(found)
                inds = {c: indicator(sig["pattern"], feats[s]) for c, s in found.items()}
                score = sum(inds.values()) / n
                resolved = [{"column": c, "sensor": s, "ind": round(inds[c], 3), "z": round(feats[s]["z"], 2), "how": _how(sig["pattern"], feats[s])} for c, s in found.items()]
                status = None
        matched = sum(1 for r in resolved if r["ind"] >= COLUMN_MATCH)
        moving = sum(1 for r in resolved if r["ind"] >= COLUMN_MOVING)
        if status is None:
            st["scores"] = (st["scores"] + [round(score, 4)])[-SCORE_KEEP:]
            direction, projected = _direction(st["scores"])
            # a generic signature is "a pattern on some sensors": at least two of them must show it. One sensor that is
            # off on its own is the plain drift alarm's job (it may well be the sensor itself).
            need = max(2, n) if sig["generic"] else None
            status = _status(st["status"], score, matched, moving, n, direction, projected, need)
        else:
            st["scores"] = []
            direction, projected = "flat", 0.0
        if status != st["status"]:
            st["since"] = when if status in ("imminent", "occurring") else None
        st["status"] = status
        row.update(status=status, score=round(score, 3), scores=list(st["scores"]), direction=direction, projected=round(projected, 3),
                   matched=matched, n=n, resolved=resolved, missing=missing, since=st["since"], partial=bool(missing and resolved))
        rows.append(row)
    order = {"occurring": 0, "imminent": 1, "quiet": 2, "na": 3, "invisible": 4}
    rows.sort(key=lambda r: (order[r["status"]], r["generic"], -r["score"], -SPECIFICITY.get(r["pattern"], 0), str(r["fault"] or ""), r["id"]))
    _mark_covered(rows)
    return rows


def _sensors(r: dict[str, Any]) -> set[str]:
    """The sensors a row is matching on (a generic row: the ones that show its pattern at all)."""
    return {x["sensor"] for x in r["resolved"] if x["ind"] >= COLUMN_MOVING}


def _explains(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Active row ``a`` is a more specific explanation of what row ``b`` sees: same sensors (at least one shared) and
    a fixed sensor list where ``b`` is generic, or both of the same kind and ``a`` has the more specific pattern."""
    if not _sensors(a) & _sensors(b):
        return False
    if b["generic"] and not a["generic"]:
        return True
    return a["generic"] == b["generic"] and SPECIFICITY.get(a["pattern"], 0) > SPECIFICITY.get(b["pattern"], 0)


def _mark_covered(rows: list[dict[str, Any]]) -> None:
    """A generic signature that fires on the same sensors as a more specific one that is active too is only the vague
    version of that cause: ``covered_by`` names the specific one, and the monitor raises no alert for the generic one
    (one alarm, the most specific cause). Its status stays as measured, for the full list."""
    active = [r for r in rows if r["status"] in ("imminent", "occurring")]
    for r in rows:
        r["covered_by"] = None
        if r["generic"] and r["status"] in ("imminent", "occurring"):
            by = next((a for a in active if a is not r and _explains(a, r)), None)
            r["covered_by"] = by["id"] if by else None


def best_cause(rows: list[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    """The signature that explains the alarm best (occurring before imminent, documented sensors before generic,
    higher score, more specific pattern) and the others that match too. A generic row that another active signature
    covers (``covered_by``) is only the vague version of that cause and never takes its place: while Fault 1 builds up
    (imminent) the plain mean shift on its sensors may already be "occurring", and the cause is still Fault 1."""
    active = [r for r in rows if r["status"] in ("occurring", "imminent") and not r.get("covered_by")]
    if not active:
        return None, []
    active.sort(key=lambda r: (r["status"] != "occurring", r["generic"], -r["score"], -r["matched"], -SPECIFICITY.get(r["pattern"], 0)))
    best = active[0]
    # on the same sensors the more specific pattern explains more (a collapse is also a mean shift), even when the
    # plainer one scores a little higher
    better = [r for r in active[1:] if r["status"] == best["status"] and r["generic"] == best["generic"] and _explains(r, best)]
    if better:
        best = max(better, key=lambda r: (SPECIFICITY.get(r["pattern"], 0), r["score"]))
        active = [best] + [r for r in active if r is not best]
    # a generic signature always co-matches a documented one on the same behaviour: not worth listing next to it
    also = [r for r in active[1:] if not (r["generic"] and not best["generic"])][:3]
    return best, also
