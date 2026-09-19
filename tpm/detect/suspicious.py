"""One headline list of suspicious rows.

Combines the two places where a single odd reading shows up: the data-quality checks (values far outside the
usual range, impossible values, local spikes against the rolling median) and the detectors' point anomalies
(isolated readings the ensemble scores far above threshold). A row is listed once, with every source that caught
it and the signals responsible. The wording is deliberate: a single reading like this is either a glitch or a
manipulation, and the data alone cannot tell which, so nothing here claims a process fault.

Only row numbers and deviations in units of normal spread are stored, never raw values."""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

from ..contracts import Flag

MAX_ROWS = 500
MAX_RUN_ROWS = 5  # longer out-of-range stretches are sustained events, not single suspicious readings
WORDING = "Each of these readings is either a glitch (sensor, transmission or entry error) or a deliberate manipulation; the data alone cannot tell which."
SOURCE_WORDS = {"detector": "the anomaly detectors", "range_check": "the range check", "local_spike": "the local spike check"}


def _group_lookup(ws):
    gs = ws.read_json("group_scores.json", None) or {}
    groups = [g for g in (gs.get("groups") or []) if g.get("row_start") is not None]
    if not groups:
        return None
    groups.sort(key=lambda g: g["row_start"])
    starts = np.array([g["row_start"] for g in groups], dtype=np.int64)
    ends = np.array([g["row_end"] for g in groups], dtype=np.int64)
    names = [str(g["group"]) for g in groups]

    def lookup(row: int) -> Optional[str]:
        i = int(np.searchsorted(starts, row, side="right")) - 1
        return names[i] if 0 <= i < len(names) and row <= ends[i] else None

    return lookup


def build_suspicious_rows(ws, settings, flags: list[Flag], points_meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    entries: dict[int, dict[str, Any]] = {}

    def entry(row: int, row_end: int) -> dict[str, Any]:
        for k in (row, row - 1, row + 1):  # a detector point and a check on adjacent rows are the same event
            if k in entries:
                e = entries[k]
                e["row_end"] = max(e["row_end"], row_end)
                return e
        entries[row] = {"row": int(row), "row_end": int(row_end), "group_id": None, "batch_id": None, "time": None, "signals": {}, "sources": [], "strength": 0.0, "flag_ids": [], "check_ids": [], "evidence_ids": []}
        return entries[row]

    def add_signal(e: dict[str, Any], signal: str, deviation: Optional[float], direction: Optional[str], explanation: str) -> None:
        cur = e["signals"].get(signal)
        if cur is None or (deviation or 0.0) > (cur.get("deviation") or 0.0):
            e["signals"][signal] = {"signal": signal, "deviation": None if deviation is None else round(float(deviation), 1), "direction": direction, "explanation": explanation}

    # ---- detector point anomalies
    for f in flags:
        if f.kind != "point" or f.human_status == "dismissed":
            continue
        e = entry(int(f.row_start), int(f.row_end))
        e["group_id"] = e["group_id"] or f.group_id
        e["batch_id"] = e["batch_id"] or f.batch_id
        if "detector" not in e["sources"]:
            e["sources"].append("detector")
        e["flag_ids"].append(f.id)
        e["evidence_ids"] += [x for x in f.evidence_ids if x not in e["evidence_ids"]]
        devs = []
        for sc in f.signals_ranked:
            m = None
            try:
                m = float(sc.explanation.split(" was ")[1].split(" times")[0]) if " times its normal spread" in sc.explanation else None
            except Exception:
                m = None
            devs.append(m or 0.0)
            add_signal(e, sc.signal, m, sc.direction, sc.explanation.rstrip("."))
        e["strength"] = max(e["strength"], max(devs) if devs and max(devs) > 0 else 3.0 * float(f.score))

    # ---- data-quality checks: out-of-range, impossible values, local spikes
    for c in ws.read_jsonl("checks"):
        ct = c.get("check_type")
        if ct not in ("out_of_range", "impossible_value", "local_spike") or c.get("status") == "pass":
            continue
        sig = (c.get("signals") or [None])[0]
        vals = c.get("values") or {}
        pts = vals.get("points") or []
        if not pts and ct == "impossible_value":
            pts = [[a, b, 99.0] for a, b in (vals.get("events") or [])]
        src = "local_spike" if ct == "local_spike" else "range_check"
        for p in pts:
            rs, re_ = int(p[0]), int(p[1])
            if re_ - rs + 1 > MAX_RUN_ROWS:
                continue
            z = float(p[2]) if len(p) > 2 and p[2] is not None else 0.0
            direction = p[3] if len(p) > 3 else None
            e = entry(rs, re_)
            e["batch_id"] = e["batch_id"] or c.get("batch_id")
            if src not in e["sources"]:
                e["sources"].append(src)
            if c.get("check_id") and c["check_id"] not in e["check_ids"]:
                e["check_ids"].append(c["check_id"])
            e["evidence_ids"] += [x for x in (c.get("evidence_ids") or []) if x not in e["evidence_ids"]]
            if sig:
                if ct == "local_spike":
                    expl = f"{sig} jumped {z:.0f} times the local noise {'upwards' if direction == 'up' else 'downwards' if direction == 'down' else 'away'} from its neighbours and came straight back"
                elif ct == "impossible_value":
                    expl = f"{sig} held an impossible value (not a finite, plausible number)"
                else:
                    expl = f"{sig} was {z:.0f} times its normal spread away from its usual level"
                add_signal(e, sig, z, direction, expl)
            e["strength"] = max(e["strength"], z)

    lookup = _group_lookup(ws)
    rows = []
    for e in entries.values():
        if e["group_id"] is None and lookup is not None:
            e["group_id"] = lookup(e["row"])
        sigs = sorted(e["signals"].values(), key=lambda s: -(s.get("deviation") or 0.0))
        where = f"Row {e['row']}" if e["row_end"] == e["row"] else f"Rows {e['row']}-{e['row_end']}"
        grp = f" (group {e['group_id']})" if e["group_id"] is not None else ""
        caught = " and ".join(SOURCE_WORDS[s] for s in e["sources"])
        e["signals"] = sigs[:4]
        e["strength"] = round(float(e["strength"]), 2)
        e["statement"] = f"{where}{grp}: " + "; ".join(s["explanation"] for s in sigs[:3]) + f". Caught by {caught}. Glitch or manipulation: the data alone can't tell."
        rows.append(e)
    rows.sort(key=lambda r: (-len(r["sources"]), -r["strength"]))
    n_rows = len(rows)
    pm = points_meta or {}
    regime = {"point_dominated": bool(pm.get("point_dominated", False)), "n_point_stretches": int(pm.get("n_point_stretches", 0)), "n_sustained_stretches": int(pm.get("n_sustained_stretches", 0)), "share_points": float(pm.get("share_points", 0.0))}
    if n_rows:
        n_sig = len({s["signal"] for r in rows for s in r["signals"]})
        headline = f"{n_rows} suspicious row{'s' if n_rows != 1 else ''}: single readings on {n_sig} signal{'s' if n_sig != 1 else ''} that do not fit their surroundings."
        if regime["point_dominated"]:
            headline += " Most deviations in this data are isolated readings like these, not sustained events."
    else:
        headline = "No isolated suspicious readings were found."
    out = {"headline": headline, "wording": WORDING, "regime": regime, "n_rows": n_rows, "n_listed": min(n_rows, MAX_ROWS), "cap": MAX_ROWS, "rows": rows[:MAX_ROWS]}
    ws.write_json("suspicious_rows.json", out)
    if n_rows:
        by_src: dict[str, int] = {}
        for r in rows:
            for s in r["sources"]:
                by_src[s] = by_src.get(s, 0) + 1
        ev = ws.evidence.add("suspicious_rows", f"{n_rows} rows hold an isolated odd reading ({', '.join(f'{v} caught by {SOURCE_WORDS[k]}' for k, v in by_src.items())}); {sum(1 for r in rows if len(r['sources']) > 1)} were caught by more than one check.", values={"n_rows": n_rows, "by_source": by_src, "regime": regime}, computed_by="detect.suspicious.build_suspicious_rows", n_samples=n_rows)
        ws.log.record("system:detect", "suspicious_rows", "dataset", "suspicious_rows", {"n_rows": n_rows, "by_source": by_src, "point_dominated": regime["point_dominated"]}, [ev.id])
    return out
