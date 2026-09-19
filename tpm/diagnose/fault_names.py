"""Naming recurring fault patterns (review item 9): match a pattern's signal-deviation signature against the known
failure types the plant has written down (config/failure_signatures.yaml - the same catalogue the live monitor
uses - plus any *.yaml dropped into workspace/_live/signatures/). A pattern is named only when enough of a failure
type's sensors lead it; otherwise it says "cannot name" and shows the closest candidate, instead of defaulting to a
generic "process" label. Matching is by column name as it appears in the data file (case-insensitive); a file
without the catalogue's sensors gets "cannot name" for every pattern. The catalogue never reaches a model.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..config import ROOT

MIN_MATCHED = 2       # sensors of a failure type that must lead the pattern
MIN_SHARE = 0.5       # ... and at least this share of the failure type's sensors
DIR_TO_PATTERN = {"stuck": ("collapse", "fading_shift"), "down": ("collapse", "mean_shift", "slow_drift", "fading_shift"), "up": ("mean_shift", "slow_drift", "brief_bump", "fading_shift"),
                  "noisy": ("variance_increase", "jumpy"), "shifted": ("mean_shift", "fading_shift", "slow_drift")}


def load_catalogue(ws=None) -> list[dict[str, Any]]:
    import yaml

    files = [ROOT / "config" / "failure_signatures.yaml"]
    if ws is not None:
        try:
            base = Path(ws.dir).parent / "_live" / "signatures"
            files += sorted(base.glob("*.yaml")) if base.exists() else []
        except Exception:
            pass
    by_id: dict[str, dict[str, Any]] = {}
    for f in files:
        try:
            data = yaml.safe_load(Path(f).read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        for s in (data.get("signatures") if isinstance(data, dict) else data) or []:
            if isinstance(s, dict) and s.get("id") and s.get("columns") and s.get("visible", True):
                by_id[str(s["id"])] = s
    return list(by_id.values())


def name_patterns(patterns: dict[str, dict[str, Any]], signals: list[dict[str, Any]], catalogue: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{pattern_id: {"named": bool, "id", "name", "matched", "of", "confidence", "text"}} for every pattern."""
    col_of = {str(s.get("id")): str(s.get("source_column") or "").strip().lower() for s in signals}
    have = {c for c in col_of.values() if c}
    out: dict[str, dict[str, Any]] = {}
    for pid, p in patterns.items():
        sig = p.get("signature") or {}
        lead = [str(x) for x in (sig.get("ranked_signals") or [])][:5]
        dirs = sig.get("directions") or {}
        lead_cols = {col_of.get(a, ""): a for a in lead}
        best: Optional[tuple[float, dict[str, Any], int, int]] = None
        for c in catalogue:
            cols = [str(x).strip().lower() for x in c.get("columns") or []]
            present = [x for x in cols if x in have]
            if not present:
                continue
            k = sum(1 for x in present if x in lead_cols)
            share = k / max(1, len(cols))
            agree = any(c.get("pattern") in DIR_TO_PATTERN.get(str(dirs.get(lead_cols[x], "")), ()) for x in present if x in lead_cols)
            score = share + (0.2 if agree else 0.0)
            if best is None or score > best[0]:
                best = (score, c, k, len(cols))
        if best is None:
            out[pid] = {"named": False, "text": "cannot name: the known failure types refer to sensors this file does not have"}
            continue
        score, c, k, n = best
        if k >= MIN_MATCHED and k / n >= MIN_SHARE:
            out[pid] = {"named": True, "id": c.get("id"), "name": c.get("name"), "matched": k, "of": n, "confidence": c.get("confidence", "hypothesis"),
                        "text": f"possibly {c.get('name')} ({k} of its {n} sensors lead this pattern; known failure type, {c.get('confidence', 'hypothesis')})"}
        else:
            out[pid] = {"named": False, "closest": c.get("name"), "matched": k, "of": n,
                        "text": f"cannot name: no known failure type fits (closest: {c.get('name')}, {k} of its {n} sensors)"}
    return out
