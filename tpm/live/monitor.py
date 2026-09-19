"""The live monitor: follows a CSV that keeps growing and re-analyses it every cycle (15 minutes by default).

One ``LiveMonitor`` lives in the web app. It owns the current data source (a file replayed at a chosen speed, a
built-in demo, a link, or a file another program keeps writing), the learned baseline, the operator's sensitivity
settings and the analysis loop. It is independent of the run pipeline: it needs no run and never touches one.

Data control: raw rows stay in this process and in the working file under ``workspace/_live``. The language
model (local only, through ``tpm.llm.router.local_chat`` so every call is recorded in the egress ledger) only ever
sees short summaries: statistics per sensor, never readings. The only network traffic this module can cause is
*downloading* from a link the operator pasted; nothing is uploaded.
"""
from __future__ import annotations

import copy
import itertools
import json
import math
import random
import re
import statistics
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import httpx

from . import engine, signatures
from .engine import Feed, check_settings, msg

MAX_ROWS_WITHOUT_RANGE = 100_000_000          # a link that cannot send "only the new part" may be at most this many bytes
KEEP = 96                                     # analysis cycles kept for the history graph (24 h at 15 min)
SPARK = 12                                    # cycle averages shown as a sparkline on the alarm card
INJECT_SPREADS = 5.0                          # an injected failure is this many normal spreads of its column (x strength)
INJECT_FLAT_SPREAD = 1e-3                     # ... a flat column's spread = 0.1 % of its level (as engine.learn takes it)
INJECT_STICK_ROWS = 8                         # an injected sticking valve holds for this many readings, then jumps

# The built-in demo plants. "plant": generic sensor names (the drift matches the generic mean-shift signature).
# "te": Tennessee-Eastman-style names, so the drift matches a documented failure type (Fault 1: mean shift on
# xmeas_1, xmeas_4, xmv_3). Values are made up; only the column names and the shape of the drift matter.
# "drift" is a fraction of the level reached at the freeze cycle, or with "drift_per_cycle" normal spreads added
# every cycle (up to "drift_cap"): slow enough that the failure type is "imminent" for a cycle or two before it is
# "occurring", as a real drift towards a failure would be. Every normal spread stays under 1 % of its level: with the
# default sensitivity (watch 1 %, alarm 2 % of the level) a wider normal wander tripped the trend check in normal
# running (a false "process drifting" alarm right after learning in most runs of the TE demo).
DEMO_PLANTS = {
    "plant": {"names": ["temperature", "pressure", "flow", "level", "vibration", "current"],
              "means": {"temperature": 75.0, "pressure": 2.4, "flow": 32.0, "level": 60.0, "vibration": 0.03, "current": 1.0},
              "sigmas": {"temperature": 0.15, "pressure": 0.01, "flow": 0.12, "level": 0.2, "vibration": 0.0002, "current": 0.005},
              "drift": {"temperature": 0.06, "flow": -0.08}, "freeze": "level", "freeze_cycle": 10, "cycles": 12, "signature": "G-shift"},
    "te": {"names": ["xmeas_1", "xmeas_4", "xmv_3", "xmeas_9", "xmeas_21", "xmv_10", "xmeas_10", "xmeas_30"],
           "means": {"xmeas_1": 0.25, "xmeas_4": 9.35, "xmv_3": 24.6, "xmeas_9": 120.4, "xmeas_21": 94.6, "xmv_10": 41.1, "xmeas_10": 0.337, "xmeas_30": 13.8},
           "sigmas": {"xmeas_1": 0.002, "xmeas_4": 0.03, "xmv_3": 0.2, "xmeas_9": 0.02, "xmeas_21": 0.05, "xmv_10": 0.2, "xmeas_10": 0.0016, "xmeas_30": 0.06},
           "drift": {"xmeas_1": 0.8, "xmeas_4": -0.8, "xmv_3": 0.8}, "drift_per_cycle": True, "drift_cap": 8.0,
           "freeze": "xmeas_30", "freeze_cycle": 12, "cycles": 15, "signature": "F01"},
}


def _iso(ts: Optional[float] = None) -> str:
    return (datetime.fromtimestamp(ts) if ts else datetime.now()).isoformat(timespec="seconds")


def demo_readings(plant: dict[str, Any], per_cycle: float, seed: int = 7) -> Iterator[list[float]]:
    """The readings of a built-in demo plant (DEMO_PLANTS), one list per row in the order of its names: a smooth
    wander around the normal level, the drift from cycle 7 on, one sensor frozen from its freeze cycle on."""
    names, means, sigmas, drift, freeze = plant["names"], plant["means"], plant["sigmas"], plant["drift"], plant["freeze"]
    drift_at, freeze_at = int(per_cycle * 6), int(per_cycle * (plant["freeze_cycle"] - 1))
    rng, x, frozen = random.Random(seed), {k: 0.0 for k in names}, None
    for i in range(int(per_cycle * plant["cycles"])):
        row = []
        for k in names:
            x[k] = 0.9 * x[k] + rng.gauss(0, sigmas[k] * 0.45)          # smooth wander around the normal level
            v = means[k] + x[k]
            if i >= drift_at and k in drift and plant.get("drift_per_cycle"):
                v += sigmas[k] * drift[k] * min((i - drift_at) / per_cycle, plant["drift_cap"] / abs(drift[k]))
            elif i >= drift_at and k in drift:
                v += means[k] * drift[k] * min(1.0, (i - drift_at) / max(1, freeze_at - drift_at))
            if k == freeze and i >= freeze_at:
                frozen = v if frozen is None else frozen
                v = frozen
            row.append(v)
        yield row


class LiveMonitor:
    def __init__(self, get_settings: Callable[[], Any], data_dir: str | Path):
        self._get_settings = get_settings
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.uploads = self.dir / "uploads"
        self.work_file = self.dir / "live_work.csv"
        self.log_file = self.dir / "log.jsonl"
        self.settings_file = self.dir / "settings.json"
        self.sig_dir = self.dir / "signatures"                # judges drop their own failure types here (*.yaml)
        self.names_file = self.dir / "names.json"             # optional {sensor: [other names]} for signature matching
        self.lock = threading.RLock()
        self.cfg = {"interval": 900.0, "rows_per_sec": 1.0, "baseline_cycles": 4}
        self.set, self.set_by = self._load_settings()
        self.state = self._fresh_state()
        self.src: dict[str, Any] = self._no_source()
        self.feed: Optional[Feed] = None
        self._worker: dict[str, Any] = {"thread": None, "stop": None}
        self._frames: list[Any] = []
        self._last: dict[str, Any] = {}
        self._prev: dict[str, Any] = {"status": {}, "sig": None}
        self._wake = threading.Event()
        self._closing = threading.Event()
        self._loop_thread: Optional[threading.Thread] = None
        self._advice_busy = False
        self.sigs: list[dict[str, Any]] = []
        self.sig_sources: list[str] = []
        self._sigstate: dict[str, dict[str, Any]] = {}
        self._sig_shown: dict[str, str] = {}                  # per signature: the status its last alert announced
        self._alarm_since: dict[str, Any] = {}
        self.reload_signatures()

    # ------------------------------------------------------------------ known failure types
    def reload_signatures(self) -> list[dict[str, Any]]:
        """Read the catalogue again (built-in file + the monitor's signature folder). Local data only."""
        sigs, sources = signatures.load([self.sig_dir, self.dir])
        with self.lock:
            self.sigs, self.sig_sources = sigs, sources
        return sigs

    def name_map(self, sensors: list[str]) -> dict[str, list[str]]:
        """Other names a sensor may go by: its positional alias (S01 = first sensor, as the run pipeline names them)
        and whatever names.json in the data folder lists ({sensor: "display name" | [names]})."""
        out = {s: [f"S{i + 1:02d}"] for i, s in enumerate(sensors)}
        if self.names_file.exists():
            try:
                extra = json.loads(self.names_file.read_text(encoding="utf-8"))
                for s, alts in (extra or {}).items():
                    if s in out:
                        out[s].extend([str(a) for a in (alts if isinstance(alts, list) else [alts]) if a])
            except Exception:
                pass
        return out

    # ------------------------------------------------------------------ small helpers
    @property
    def app_settings(self) -> Any:
        return self._get_settings()

    def expected_rows(self) -> float:
        return self.cfg["interval"] * self.cfg["rows_per_sec"]

    def _log(self, rec: dict[str, Any]) -> None:
        rec = {"ts": _iso(), **rec}
        with self.lock, self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def read_log(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.log_file.exists():
            return []
        out = []
        for line in self.log_file.read_text(encoding="utf-8").splitlines()[-max(1, limit):]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    @staticmethod
    def _no_source() -> dict[str, Any]:
        return {"kind": "none", "name": "", "path": "", "detail": "", "error": "", "rows": 0, "detail_msg": [], "name_msg": None}

    def _detail(self, *msgs: dict[str, Any]) -> None:
        """The status line of the source: the English text for the log, the message keys for the page (it translates them)."""
        self.src["detail"] = " ".join(m["text"] for m in msgs)
        self.src["detail_msg"] = list(msgs)

    @staticmethod
    def _fresh_state() -> dict[str, Any]:
        return {"baseline": None, "history": [], "events": [], "cycles": 0, "advice": "", "learning": 0, "sensors": [], "since": {},
                "updated": None, "next_at": None, "verdict": {"level": "wait", **msg("verdict.nosource")}, "table": [],
                "signatures": [], "alarm": None}

    def _load_settings(self) -> tuple[dict[str, Any], dict[str, Any]]:
        s, by = dict(engine.DEFAULT_SETTINGS), {"by": "default", "t": None, "note": "", "name": ""}
        if self.settings_file.exists():
            try:
                saved = json.loads(self.settings_file.read_text(encoding="utf-8"))
                by = saved.pop("_by", by)
                s = check_settings({**s, **saved})
            except Exception:
                pass
        return s, by

    def _reset_learning(self) -> None:
        self._frames.clear()
        self.state = self._fresh_state()
        self.state["verdict"] = {"level": "wait", **msg("verdict.wait")}
        self._prev = {"status": {}, "sig": None}
        self._last = {}
        self._sigstate = {}
        self._sig_shown = {}
        self._alarm_since = {}

    # ------------------------------------------------------------------ what the page shows
    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            st = self.state
            out = {
                "source": {k: self.src.get(k) for k in ("kind", "name", "path", "detail", "error", "rows", "scenario", "inject", "detail_msg", "name_msg")},
                "running": self.feed is not None,
                "cfg": dict(self.cfg), "expected_rows": int(self.expected_rows()),
                "settings": dict(self.set), "settings_by": dict(self.set_by),
                "baseline_ready": bool(st["baseline"]), "learning": st["learning"], "cycles": st["cycles"],
                "updated": st["updated"], "next_at": st["next_at"], "verdict": st["verdict"], "advice": st["advice"],
                "sensors": st["sensors"], "table": st["table"], "baseline": st["baseline"], "history": st["history"],
                "events": st["events"][:60], "now": _iso(),
                "signatures": st.get("signatures") or [], "alarm": st.get("alarm"),
                "signature_sources": list(self.sig_sources), "signature_dir": str(self.sig_dir), "n_signatures": len(self.sigs),
            }
            return json.loads(json.dumps(out, default=str))

    def alarm_brief(self) -> dict[str, Any]:
        """What the app-wide watcher needs on every page (a toast and a dot on rail item 7): is a source running, and
        the current alarm card. Small, so it can be polled from any page."""
        with self.lock:
            return json.loads(json.dumps({"running": self.feed is not None, "source": self.src.get("kind"), "alarm": self.state.get("alarm")}, default=str))

    # ------------------------------------------------------------------ operator settings
    def apply_settings(self, data: dict[str, Any], *, by: str = "operator", note: str = "", name: str = "") -> dict[str, Any]:
        new = check_settings(data)
        with self.lock:
            now = datetime.now()
            self.set, self.set_by = new, {"by": by, "t": now.isoformat(timespec="seconds"), "note": note, "name": name}
            self.settings_file.write_text(json.dumps({**self.set, "_by": self.set_by}), encoding="utf-8")
            if self.state.get("baseline") and self._last.get("st") is not None:
                self._analyse(real=False, when=now)
            self.state["events"].insert(0, {"t": self.set_by["t"], "kind": "settings", "by": by, "name": name, "settings": dict(new),
                                            "note": note, "text": f"{'The AI' if by == 'ai' else 'The operator'} set the sensitivity: "
                                            f"watch {new['watch']:g}, alarm {new['alarm']:g} ({new['mode']}), noise x{new['noise_ratio']:g}, "
                                            f"range {new['outside_pct']:g}%." + (f" {note}" if note else "")})
            self._log({"decision": "settings_changed", "by": by, "name": name, "settings": new, "note": note})
        return dict(new)

    def ai_decide(self, lang: str = "en") -> dict[str, Any]:
        """Let the local model choose the sensitivity from a summary of how the sensors behaved while learning."""
        with self.lock:
            base = self.state.get("baseline")
            if not base:
                raise ValueError("The monitor has not learned what normal looks like yet. Start a data source and wait "
                                 "for the learning cycles to finish.")
            minutes = self.cfg["baseline_cycles"] * self.cfg["interval"] / 60
            cycle_rows = int(self.expected_rows())
            table = [{"sensor": c, "type": b["kind"]["text"], "spread_pct": round(b["sigma"] / b["basis"] * 100, 2),
                      "range_pct": round((b["hi"] - b["lo"]) / b["basis"] * 100, 2),
                      "wander_in_cycle_pct": round(b["drift_ref"] / b["basis"] * 100, 2),
                      "jitter_pct": round(b["noise_ref"] / b["basis"] * 100, 2)} for c, b in list(base.items())[:60]]
        spread = sorted(t["spread_pct"] for t in table)
        wander = sorted(t["wander_in_cycle_pct"] for t in table)
        q = lambda xs, p: xs[min(len(xs) - 1, int(p * len(xs)))]
        from ..llm.prompts import language_name
        prompt = (
            "You set the alert thresholds of a plant sensor monitor. The monitor flags a sensor when its cycle average moves "
            "away from its learned normal level by more than WATCH percent (early warning) or ALARM percent (serious).\n"
            f"It learned 'normal' from {len(table)} sensors over only {minutes:.1f} minutes, and analyses {cycle_rows} rows per "
            "cycle. Per sensor (percent of its normal level): spread_pct = normal spread, range_pct = full min-max range, "
            "wander_in_cycle_pct = how much it normally drifts inside one cycle, jitter_pct = reading-to-reading noise.\n"
            f"Overall: median spread {q(spread, .5)}%, 90th percentile spread {q(spread, .9)}%, median wander {q(wander, .5)}%, "
            f"90th percentile wander {q(wander, .9)}%.\n{json.dumps(table)}\n"
            "Choose thresholds so that normal operation is NOT flagged all the time, yet a real shift is still caught. WATCH must "
            "sit above the normal wander; ALARM is usually 2 to 3 times WATCH. If the learning period is short, be more generous. "
            "noise_factor (usually 2-3) = flag when reading-to-reading jitter is this many times normal. out_of_range_pct "
            "(usually 20-50) = flag when this share of readings falls outside the min-max seen while learning.\n"
            'Reply as JSON: {"watch_pct": number, "alarm_pct": number, "noise_factor": number, "out_of_range_pct": number, '
            f'"reason": "2 plain sentences an operator understands, naming the numbers you used, written in {language_name(lang)}"}}')
        res = self._ask_model("live_settings", "AI chooses the sensitivity thresholds (summary statistics only, no raw rows)",
                              prompt, {"type": "object", "required": ["watch_pct", "alarm_pct", "noise_factor", "out_of_range_pct"]})
        if not res.ok or not res.data:
            raise ValueError(f"The local model could not answer ({res.error or 'no answer'}). Is Ollama running?")
        out = res.data
        try:                                                # guard rails: keep the model's choice inside sensible limits
            w = min(max(float(out["watch_pct"]), 0.3), 20.0)
            a = min(max(float(out["alarm_pct"]), w * 1.5), 50.0)
            new = {"mode": "percent", "watch": round(w, 2), "alarm": round(a, 2),
                   "noise_ratio": round(min(max(float(out["noise_factor"]), 1.5), 6.0), 2),
                   "outside_pct": round(min(max(float(out["out_of_range_pct"]), 10.0), 70.0), 1)}
        except (KeyError, TypeError, ValueError):
            raise ValueError("The model's answer was not usable. Try again, or set the values yourself.")
        reason = str(out.get("reason", "")).strip()[:600]
        self.apply_settings(new, by="ai", note=reason, name=res.model)
        return {"settings": new, "reason": reason, "model": res.model, "ledger_id": res.ledger_id}

    def _ask_model(self, task: str, purpose: str, prompt: str, schema: dict[str, Any]) -> Any:
        """One call to the local model through the app's LLM layer (recorded in the egress ledger)."""
        from ..contracts import LLMResult
        try:
            from ..llm.router import local_chat
            return local_chat([{"role": "system", "content": "You reply with a single JSON object and nothing else."},
                               {"role": "user", "content": prompt}], task=task, purpose=purpose, ws=None,
                              settings=self.app_settings, schema=schema, max_tokens=700, artifact_types=["live_summary"], temperature=0)
        except Exception as e:
            return LLMResult(text="", data=None, source="template", route="none", ok=False, error=str(e))

    def _advice(self, facts: dict[str, Any], lang: str) -> None:
        """Short 'what to check first' note. Runs in its own thread so a slow model never delays an analysis cycle."""
        try:
            from ..llm.prompts import language_name
            prompt = (f"You advise a plant operator (not a data scientist). Facts, already computed: {json.dumps(facts)}\n"
                      f'Reply as JSON: {{"next_steps": "2 short plain sentences on what to check first, written in {language_name(lang)}"}}. '
                      "Do not repeat the facts and do not invent numbers.")
            res = self._ask_model("live_advice", "plain-language advice for a changed alarm set (sensor names only)", prompt,
                                  {"type": "object", "required": ["next_steps"]})
            text = str((res.data or {}).get("next_steps", "")).strip() if res.ok else ""
            if re.findall(r"\d+", re.sub(r"\w*[A-Za-z_]\w*\d\w*", "", text)):     # never show numbers the model made up
                text = ""
            with self.lock:
                self.state["advice"] = text
                self._log({"decision": "advice", "ok": bool(text), "model": res.model, "ledger_id": res.ledger_id})
        finally:
            self._advice_busy = False

    # ------------------------------------------------------------------ one analysis cycle
    def _analyse(self, *, real: bool, when: datetime) -> tuple[list[dict], str, dict, list[dict], list[dict]]:
        """Judge the latest cycle. real=False re-judges the same cycle after a setting changed."""
        st, base = self._last["st"], self.state["baseline"]
        past = self.state["history"] if real else self.state["history"][:-1]
        rows = engine.judge(st, base, past, self.set)
        if real:
            self.state["history"].append({"t": when.isoformat(timespec="seconds"), "shift": {r["sensor"]: r["shift"] for r in rows},
                                          "mean": {r["sensor"]: r["mean"] for r in rows}})
            self.state["history"] = self.state["history"][-KEEP:]
        since = self.state.setdefault("since", {})           # how long has each sensor been off normal, counted in cycles
        for r in rows:
            if r["status"] == "ok":
                since.pop(r["sensor"], None)
            elif r["sensor"] not in since:
                since[r["sensor"]] = {"t": (when - timedelta(seconds=self.cfg["interval"])).isoformat(timespec="seconds"), "cycles": 1}
            elif real:
                since[r["sensor"]]["cycles"] += 1
            e = since.get(r["sensor"])
            r["since"], r["cycles"] = (e["t"], e["cycles"]) if e else (None, 0)
            r["off_for_s"] = r["cycles"] * self.cfg["interval"]
        self.state["table"] = rows
        level, m, broken, alarms = engine.verdict(rows, len(rows))
        self.state["verdict"] = {"level": level, **m}
        t_iso = when.isoformat(timespec="seconds")
        if real:                                             # event log = status changes only
            for r in rows:
                old = self._prev["status"].get(r["sensor"], "ok")
                if old != r["status"]:
                    self.state["events"].insert(0, {"t": t_iso, "kind": "status", "sensor": r["sensor"],
                                                    "old": old, "new": r["status"], "notes": r["notes"],
                                                    "text": f"{r['sensor']}: {old} -> {r['status']}. {r['note']}"})
            # known failure types: how far the sensors have moved towards each one (statistics only, this process only)
            feats = engine.features(st, base, past)
            sensors = [r["sensor"] for r in rows]
            sig_rows = signatures.evaluate(self.sigs, feats, sensors, self.name_map(sensors), self._sigstate,
                                           exclude=[r["sensor"] for r in broken], when=t_iso)
            self.state["signatures"] = sig_rows
            self._signature_alerts(sig_rows, t_iso)
        self._prev["status"] = {r["sensor"]: r["status"] for r in rows}
        self.state["alarm"] = self._alarm(level, rows, broken, alarms, self.state.get("signatures") or [], t_iso)
        return rows, level, m, broken, alarms

    def _signature_alerts(self, sig_rows: list[dict], t_iso: str) -> None:
        """Events + log lines for known failure types that start, escalate or clear. One alarm per cause: a generic
        signature covered by a more specific one (same sensors) raises nothing, and a generic alert from this or the
        previous cycle is replaced by the specific one (the specific alert names what it replaces)."""
        active = ("imminent", "occurring")
        prev_t = self.state["history"][-2]["t"] if len(self.state["history"]) >= 2 else t_iso
        replaced: dict[str, list[str]] = {}
        for r in sig_rows:                                   # generic alerts that a specific cause now explains
            if r.get("covered_by") and self._sig_shown.get(r["id"], "quiet") in active:
                replaced.setdefault(r["covered_by"], []).append(r["id"])
                self.state["events"] = [e for e in self.state["events"] if not (e.get("kind") == "signature" and e.get("sig") == r["id"] and str(e.get("t")) >= prev_t)]
                self._sig_shown[r["id"]] = "quiet"
        for r in sig_rows:
            shown = "quiet" if r.get("covered_by") else r["status"]
            old = self._sig_shown.get(r["id"], "quiet")
            self._sig_shown[r["id"]] = shown
            if old == shown or not (shown in active or old in active):
                continue
            cols = ", ".join(x["sensor"] for x in r["resolved"])
            key = {"imminent": "sig.imminent", "occurring": "sig.occurring"}.get(shown, "sig.cleared")
            ev = {"t": t_iso, "kind": "signature", "sig": r["id"], "name": r["name"], "name_i18n": r["name_i18n"], "old": old, "new": shown,
                  "score": r["score"], "sensors": cols, "confidence": r["confidence"], "generic": r["generic"], "replaces": replaced.get(r["id"], []),
                  **msg(key, name=r["name"], score=f"{r['score'] * 100:.0f}", sensors=cols or "-")}
            self.state["events"].insert(0, ev)
            self._log({"decision": "signature_alert", "sig": r["id"], "status": shown, "was": old, "score": r["score"],
                       "sensors": [x["sensor"] for x in r["resolved"]], "confidence": r["confidence"], "generic": r["generic"],
                       "replaces": replaced.get(r["id"], [])})
        self.state["events"] = self.state["events"][:60]

    def _alarm(self, level: str, rows: list[dict], broken: list[dict], alarms: list[dict], sig_rows: list[dict], t_iso: str) -> Optional[dict[str, Any]]:
        """The alarm card: ALARM (what tripped) -> PROBLEM (which sensors behave how) -> CAUSE (the known failure type
        that matches, or none) -> SUGGESTION (its advice, or generic advice). None when nothing is wrong."""
        cause, also = signatures.best_cause(sig_rows)
        # the process IS drifting (the drift alarm has tripped, or the plain pattern on the cause's own sensors is already
        # there) while the failure type itself is still building up: an alarm "drifting towards" it, never a step back
        # from "Alarm" to "Early warning"
        towards = cause is not None and cause["status"] == "imminent" and (level == "drift" or any(
            r.get("covered_by") == cause["id"] and r["status"] == "occurring" for r in sig_rows))
        if cause is not None:
            kind = "occurring" if towards else cause["status"]                  # occurring | imminent
        elif level == "drift":
            kind = "drift"
        elif level in ("quality", "untrusted"):
            kind = "quality"
        else:
            self._alarm_since = {}
            return None
        key = ("towards" if towards else kind, cause["id"] if cause else None)
        if self._alarm_since.get("key") != key:
            self._alarm_since = {"key": key, "t": t_iso}
        since = self._alarm_since["t"]
        by_sensor = {r["sensor"]: r for r in rows}
        hist = self.state["history"][-SPARK:]

        def problem(sensor: str, how: Optional[dict[str, Any]] = None) -> dict[str, Any]:
            r = by_sensor.get(sensor) or {}
            b = (self.state["baseline"] or {}).get(sensor) or {}
            return {"sensor": sensor, "status": r.get("status", "ok"), "notes": r.get("notes") or [], "how": how,
                    "dev": r.get("dev"), "dev_pct": r.get("dev_pct"), "mu": b.get("mu"), "sigma": b.get("sigma"),
                    "spark": [h["mean"].get(sensor) for h in hist] + [r.get("mean")]}
        if cause is not None:
            sensors = [problem(x["sensor"], x["how"]) for x in cause["resolved"][:4]]
            trip = {"key": "sig.towards" if towards else "sig." + kind, "sig": cause["id"], "name": cause["name"], "name_i18n": cause["name_i18n"], "score": cause["score"], "n": len(sensors)}
            cause_out = {"sig": cause["id"], "fault": cause["fault"], "name": cause["name"], "name_i18n": cause["name_i18n"], "confidence": cause["confidence"],
                         "score": cause["score"], "status": cause["status"], "generic": cause["generic"], "pattern": cause["pattern"], "direction": cause["direction"],
                         "matched": cause["matched"], "n": cause["n"], "partial": cause["partial"],
                         "also": [{"sig": a["id"], "name": a["name"], "name_i18n": a["name_i18n"], "status": a["status"], "score": a["score"], "generic": a["generic"]} for a in also]}
            sug = {"text": cause["suggestion"], "text_i18n": cause["suggestion_i18n"], "generic": cause["generic"], "key": None}
        elif kind == "drift":
            sensors = [problem(r["sensor"]) for r in alarms[:4]]
            trip = {"key": "drift", "sig": None, "n": len(alarms)}
            cause_out = {"sig": None, "generic": True, "also": [{"sig": a["id"], "name": a["name"], "name_i18n": a["name_i18n"], "status": a["status"], "score": a["score"], "generic": a["generic"]} for a in sig_rows if a["status"] == "imminent"][:3]}
            sug = {"text": "", "text_i18n": {}, "generic": True, "key": "generic.drift"}
        else:
            sensors = [problem(r["sensor"]) for r in broken[:4]]
            trip = {"key": "quality", "sig": None, "n": len(broken)}
            cause_out = {"sig": None, "generic": True, "quality": True, "also": []}
            sug = {"text": "", "text_i18n": {}, "generic": True, "key": "generic.quality"}
        return {"id": f"{kind}:{key[1] or ''}:{since}", "kind": kind, "t": t_iso, "since": since, "level": level,
                "trip": trip, "problem": sensors, "cause": cause_out, "suggestion": sug}

    def run_cycle(self, when: Optional[datetime] = None, lang: Optional[str] = None) -> None:
        """Read what arrived since the last cycle and analyse it (called by the loop; tests call it directly)."""
        now = when or datetime.now()
        lang = lang or getattr(getattr(self.app_settings, "report", None), "default_language", "en") or "en"
        with self.lock:
            feed = self.feed
            if feed is None:
                return
            df = feed.read_new()
            if len(df) < 0.5 * self.expected_rows() and self._source_done():
                # a demo or a replay that has played all its rows: the last analysis (verdict, alarm card, failure
                # types) stays on screen and the cycles stop, instead of a "stream gap" every cycle from then on
                self.feed = None
                self.state["next_at"] = None
                self._log({"cycle": self.state["cycles"], "rows": len(df), "decision": "source_finished", "kind": self.src["kind"]})
                return
            if self.src["kind"] == "live-file":
                self.src["rows"] += len(df)
            sensors = engine.sensor_columns(df) if len(df) else self.state["sensors"]
            st = self.state
            st["sensors"], st["cycles"], st["updated"] = sensors, st["cycles"] + 1, now.isoformat(timespec="seconds")
            log = {"cycle": st["cycles"], "rows": len(df)}

            if len(df) < 0.5 * self.expected_rows():         # timeliness / completeness of the stream itself
                m = msg("verdict.gap", rows=len(df), expected=f"{self.expected_rows():g}")
                st["verdict"] = {"level": "untrusted", **m}
                st["events"].insert(0, {"t": st["updated"], "kind": "gap", **m})
                self._log({**log, "decision": "stream_gap"})
                return

            if st["baseline"] is None:                       # ---- learning phase
                self._frames.append(df)
                st["learning"] = len(self._frames)
                st["verdict"] = {"level": "learn", **msg("verdict.learn", done=len(self._frames), total=self.cfg["baseline_cycles"])}
                if len(self._frames) >= self.cfg["baseline_cycles"]:
                    st["baseline"] = engine.learn(self._frames, sensors)
                    self._frames.clear()                     # raw rows are dropped, only statistics stay
                    log["decision"] = "baseline_learned"
                self._log(log)
                return

            self._last["st"] = engine.cycle_stats(df, [c for c in sensors if c in st["baseline"]], st["baseline"])
            rows, level, m, broken, alarms = self._analyse(real=True, when=now)
            sig = (tuple(r["sensor"] for r in broken), tuple(r["sensor"] for r in alarms))
            if sig != self._prev["sig"] and (broken or alarms):   # ask the model only when something changed
                if not self._advice_busy:
                    self._advice_busy = True
                    # sensor names and the verdict only: the known-failure-type catalogue (fault numbers, names,
                    # column mapping) is local data and is never part of a model prompt
                    facts = {"broken_sensors": [r["sensor"] for r in broken],
                             "alarm_sensors_most_affected_first": [r["sensor"] for r in alarms[:5]], "verdict": m["text"]}
                    threading.Thread(target=self._advice, args=(facts, lang), daemon=True).start()
            elif not (broken or alarms):
                st["advice"] = ""
            self._prev["sig"] = sig
            active = [(r["id"], r["status"], r["score"]) for r in st.get("signatures") or [] if r["status"] in ("imminent", "occurring")]
            self._log({**log, "decision": level, "verdict": m["text"], "settings": dict(self.set),
                       "dead": [r["sensor"] for r in rows if r["status"] == "dead"],
                       "missing": [r["sensor"] for r in rows if r["status"] == "missing"],
                       "alarms": [(r["sensor"], [e["name"] for e in r["elements"] if e["level"] == 2]) for r in alarms],
                       "signatures": active})

    def _loop(self) -> None:
        nxt = time.time() + self.cfg["interval"]
        while not self._closing.is_set():
            if self.feed is None:                            # idle: wait for a source to be chosen
                self._wake.wait(1.0)
                self._wake.clear()
                nxt = time.time() + self.cfg["interval"]
                continue
            with self.lock:
                self.state["next_at"] = _iso(nxt)
            while time.time() < nxt and not self._wake.is_set() and not self._closing.is_set():
                time.sleep(0.25)
            if self._closing.is_set():
                return
            if self._wake.is_set():                          # source or timing changed: start a fresh countdown
                self._wake.clear()
                nxt = time.time() + self.cfg["interval"]
                continue
            nxt += self.cfg["interval"]
            try:
                self.run_cycle()
            except Exception as e:                           # never let one bad cycle stop the monitor
                self._log({"decision": "cycle_error", "error": f"{type(e).__name__}: {e}"})

    def _ensure_loop(self) -> None:
        if self._loop_thread is None or not self._loop_thread.is_alive():
            self._closing.clear()
            self._loop_thread = threading.Thread(target=self._loop, name="live-monitor", daemon=True)
            self._loop_thread.start()

    def shutdown(self) -> None:
        self._closing.set()
        self._wake.set()
        self._stop_worker()

    # ------------------------------------------------------------------ data sources
    def _source_done(self) -> bool:
        """A demo or a replay (not repeating) whose player has written all its rows and ended by itself."""
        th, stop = self._worker["thread"], self._worker["stop"]
        return self.src["kind"] in ("demo", "simulate") and th is not None and not th.is_alive() and not (stop is not None and stop.is_set())

    def _stop_worker(self) -> None:
        if self._worker["stop"]:
            self._worker["stop"].set()
        if self._worker["thread"]:
            self._worker["thread"].join(timeout=10)
        self._worker.update(thread=None, stop=None)

    def _set_cfg(self, interval: Any, rows_per_sec: Any, baseline_cycles: Any) -> None:
        try:
            interval, rows_per_sec, baseline_cycles = float(interval), float(rows_per_sec), int(float(baseline_cycles))
        except (TypeError, ValueError):
            raise ValueError("Interval, rows per second and learning cycles must be numbers.")
        if not 1 <= interval <= 86400:
            raise ValueError("Analysis interval must be between 1 second and 24 hours.")
        if not 0.01 <= rows_per_sec <= 100000:
            raise ValueError("Rows per second must be between 0.01 and 100000.")
        if not 1 <= baseline_cycles <= 100:
            raise ValueError("Learning cycles must be between 1 and 100.")
        self.cfg.update(interval=interval, rows_per_sec=rows_per_sec, baseline_cycles=baseline_cycles)

    def _begin(self, kind: str, name: str, path: Path, from_start: bool, prepare: Optional[Callable] = None,
               reset: bool = True, detail: Optional[dict[str, Any]] = None, actor: str = "") -> None:
        """Switch to a new data source. ``prepare(stop_event)`` may start a worker thread and may raise ValueError."""
        with self.lock:
            self._stop_worker()
            self.src = self._no_source()
            self.feed = None
            if reset:
                self._reset_learning()
            try:
                if prepare:
                    stop = threading.Event()
                    self._worker.update(thread=prepare(stop), stop=stop)
            except ValueError as e:
                self.src["error"] = str(e)
                self.state["verdict"] = {"level": "wait", **msg("verdict.nosource")}
                raise
            if detail:
                self._detail(detail)
            self.src.update(kind=kind, name=name, path=str(path))
            self.feed = Feed(path, from_start)
            self._wake.set()
            self._ensure_loop()
            self._log({"decision": "source_changed", "kind": kind, "name": name, "actor": actor, "config": dict(self.cfg)})

    def stop_source(self, actor: str = "") -> None:
        with self.lock:
            self._stop_worker()
            self.src.update(kind="none", name="", name_msg=None, error="")
            self._detail(msg("src.stopped"))
            self.feed = None
            self.state["verdict"] = {"level": "wait", **msg("verdict.stopped")}
            self.state["alarm"] = None                       # nothing is watched any more: no alarm card next to "stopped"
            self.state["next_at"] = None
            self._wake.set()
            self._log({"decision": "source_stopped", "actor": actor})

    # ------------------------------------------------------------------ injecting a known failure type into a replay
    def find_signature(self, sig_id: str) -> Optional[dict[str, Any]]:
        sid = str(sig_id or "").strip().lower()
        return next((s for s in self.sigs if s["id"].lower() == sid), None)

    def _injection_plan(self, header: bytes, sig_id: str, sep: str) -> dict[str, Any]:
        """Which columns of the replayed file get which behaviour. A signature with sensors needs them in the header
        (matched like the monitor matches them); a generic signature or "auto" takes the first two sensor-like columns."""
        cols = [c.strip().strip('"') for c in header.decode("utf-8-sig", errors="replace").strip().split(sep)]
        sensor_like = [c for c in cols if c.lower() not in engine.NOT_SENSORS and not re.search(r"time|date", c, re.I)]
        sig = self.find_signature(sig_id) if sig_id.lower() != "auto" else None
        if sig_id.lower() != "auto" and sig is None:
            raise ValueError(f"Unknown failure type '{sig_id}'.")
        if sig is not None and (sig["pattern"] == "none" or not sig["visible"]):     # with or without a column list
            raise ValueError(f"{sig['name']}: the source says this failure is not reliably visible, so there is no pattern to inject.")
        if sig is not None and sig["columns"]:
            found, missing = signatures.match_columns(sig["columns"], cols, {c: [f"S{i + 1:02d}"] for i, c in enumerate(sensor_like)})
            if not found:
                raise ValueError(f"{sig['name']} needs the columns {', '.join(sig['columns'])}, but the file has none of them.")
            targets, pattern, label = [cols.index(s) for s in found.values()], sig["pattern"], sig["name"]
        else:
            if len(sensor_like) < 1:
                raise ValueError("The file has no sensor-like columns to inject into.")
            targets = [cols.index(c) for c in sensor_like[:2]]
            pattern = sig["pattern"] if sig is not None else "mean_shift"
            label = sig["name"] if sig is not None else "generic mean shift"
        return {"targets": targets, "pattern": pattern, "label": label, "columns": [cols[i] for i in targets], "sig": sig["id"] if sig else "auto"}

    @staticmethod
    def _inject_amplitude(spread: Optional[float], level: float, strength: float) -> float:
        """Size of an injected failure in the column's own units: INJECT_SPREADS normal spreads, what the matcher
        needs to call it clearly and what stands out on the cycle-average charts (they scale to the data). A flat
        column has no spread: it takes INJECT_FLAT_SPREAD of its level, the spread the monitor itself gives such a
        column, so the injection is just as clear to the matcher; 1.0 for a flat column at zero."""
        s = float(spread or 0.0)
        if not s > 1e-9 * max(abs(level), 1.0):
            s = INJECT_FLAT_SPREAD * abs(level) or 1.0
        return INJECT_SPREADS * s * strength

    @staticmethod
    def _rows_in_file(f: Any, data_start: int) -> Optional[int]:
        """Number of data rows of an open CSV: counted when the file is small, else estimated from the file size and
        the average length of its first lines. None when there is nothing to go by."""
        pos = f.tell()
        try:
            size = f.seek(0, 2) - data_start
            f.seek(data_start)
            sample = f.read(65536)
        finally:
            f.seek(pos)
        if size <= 0:
            return None
        if len(sample) >= size:
            return sum(1 for line in sample.splitlines() if line.strip())
        lines = sample.count(b"\n")
        return int(size * lines / (sample.rfind(b"\n") + 1)) if lines else None

    @staticmethod
    def _inject_value(pattern: str, v: float, k: int, ramp: int, amp: float, rng: random.Random) -> float:
        """One reading, ``k`` rows after the injection started. ``amp`` is the size of the effect (a few normal
        spreads, see _inject_amplitude), ``ramp`` the number of rows a shift takes to build up (about two analysis
        cycles, fewer when the file is short)."""
        frac = min(1.0, k / max(1, ramp))
        if pattern == "mean_shift":
            return v + amp * frac
        if pattern == "collapse":                                     # most of its level, and at least 2 x amp
            return v - max(0.8 * abs(v), 2 * amp) * min(1.0, k / max(1, ramp // 4))
        if pattern == "variance_increase":
            return v + rng.gauss(0.0, amp) * frac
        if pattern == "slow_drift":
            return v + amp * min(1.0, k / max(1, 3 * ramp))
        if pattern == "jumpy":                                # sticks, then jumps by amp around the same level (stick-slip)
            return v + (amp / 2 if (k // INJECT_STICK_ROWS) % 2 == 0 else -amp / 2) * frac
        if pattern == "brief_bump":
            return v + amp if k < ramp else v
        if pattern == "fading_shift":
            return v + amp * max(0.3, 1.0 - k / max(1, 3 * ramp))
        return v

    def start_simulation(self, d: dict[str, Any], actor: str = "") -> None:
        """Replay a (possibly huge) CSV into the working file at a chosen speed, like a plant writing live data.
        ``inject`` (a signature id from the catalogue, or "auto") adds that failure type's behaviour to its columns
        after ``inject_after`` rows (default: two cycles after learning), so the drift alarm can be demonstrated on
        any file."""
        src = Path(str(d.get("path") or "").strip().strip('"').strip("'"))
        try:
            rate, start_row, max_rows = float(d.get("rate", 10)), int(d.get("start_row", 1) or 1), int(d.get("max_rows", 0) or 0)
            inject_after = int(d.get("inject_after") or 0)
            strength = float(d.get("inject_strength") or 1.0)
        except (TypeError, ValueError):
            raise ValueError("Rate, start row, rows to play and the injection settings must be numbers.")
        loop = bool(d.get("loop"))
        inject = str(d.get("inject") or "").strip()
        if not src.is_file():
            raise ValueError(f"File not found: {src}. Drop a file, or type its full path.")
        if rate <= 0 or start_row < 1 or max_rows < 0 or inject_after < 0 or not 0.1 <= strength <= 20:
            raise ValueError("Rate must be above 0, start row at least 1, rows to play 0 or more, injection strength 0.1 to 20.")
        self._set_cfg(d.get("interval", 20), rate, d.get("baseline_cycles", 2))
        per_cycle = max(1, int(rate * self.cfg["interval"]))
        if inject and not inject_after:
            inject_after = per_cycle * (self.cfg["baseline_cycles"] + 2)

        def prepare(stop: threading.Event) -> threading.Thread:
            f = open(src, "rb")
            header = f.readline()
            if not header.strip() or header.lstrip()[:1] in (b"{", b"[", b"<"):
                f.close()
                raise ValueError("This does not look like a CSV file with a header line.")
            header = header if header.endswith(b"\n") else header + b"\n"
            sep = ";" if header.count(b";") > header.count(b",") else ","
            plan, ramp = None, per_cycle * 2
            if inject:
                try:
                    plan = self._injection_plan(header, inject, sep)
                    rows = self._rows_in_file(f, len(header))
                    if rows is not None:
                        rows = max(0, rows - (start_row - 1))
                        rows = min(rows, max_rows) if max_rows else rows
                        learn_rows = per_cycle * self.cfg["baseline_cycles"]
                        if not loop and rows < inject_after + 3:
                            raise ValueError(
                                f"The file has only about {rows:,} rows to play, so a failure injected after row {inject_after:,} would "
                                "never show. " + (f"Set 'Inject after' between {learn_rows:,} and {rows - 3:,} (the monitor learns "
                                f"from the first {learn_rows:,} rows)" if learn_rows < rows - 3 else f"At these settings the monitor "
                                f"needs {learn_rows:,} rows just to learn what normal looks like: lower the rate or the interval")
                                + ", switch on 'Repeat', or use a longer file.")
                        if not loop:                             # a short file: the shift is complete within its first third
                            ramp = max(1, min(ramp, (rows - inject_after) // 3))
                except ValueError:
                    f.close()
                    raise
                plan.update(ramp_rows=ramp, spreads=INJECT_SPREADS * strength, amplitude={})
            self.work_file.write_bytes(header)
            key = "src.replay" + (".rows" if max_rows else "") + (".loop" if loop else "")
            parts = [msg(key, file=src.name, rate=f"{rate:g}", start=f"{start_row:,}", **({"rows": f"{max_rows:,}"} if max_rows else {}))]
            if plan:
                parts.append(msg("src.inject", after=f"{inject_after:,}", label=plan["label"], columns=", ".join(plan["columns"])))
            self._detail(*parts)
            self.src["inject"] = plan

            def play() -> None:
                carry, total, rng, amps = 0.0, 0, random.Random(11), {}
                before: dict[int, list[float]] = {i: [] for i in (plan["targets"] if plan else [])}   # readings before the injection

                def remember(line: bytes) -> None:
                    parts = line.decode("utf-8", errors="replace").rstrip("\r\n").split(sep)
                    for idx, seen in before.items():
                        try:
                            seen.append(float(parts[idx]))
                        except (IndexError, ValueError):
                            continue
                        if len(seen) > 5000:
                            del seen[:1000]

                def amplitude(idx: int, v: float) -> float:
                    # the column's normal spread: what the monitor learned, else the readings played before the injection
                    col = plan["columns"][plan["targets"].index(idx)]
                    b = (self.state.get("baseline") or {}).get(col)
                    seen = [x for x in before.get(idx, []) if math.isfinite(x)]
                    if b:
                        spread, level = b["sigma"], b["mu"]
                    elif len(seen) >= 3:
                        spread, level = statistics.pstdev(seen), statistics.fmean(seen)
                    else:
                        spread, level = None, v
                    a = self._inject_amplitude(spread, level, strength)
                    plan["amplitude"] = {**plan["amplitude"], col: float(f"{a:.6g}")}
                    return a

                def transform(line: bytes, k: int) -> bytes:
                    parts = line.decode("utf-8", errors="replace").rstrip("\r\n").split(sep)
                    for idx in plan["targets"]:
                        if idx >= len(parts):
                            continue
                        try:
                            v = float(parts[idx])
                        except ValueError:
                            continue
                        if idx not in amps:
                            amps[idx] = amplitude(idx, v)
                        parts[idx] = f"{self._inject_value(plan['pattern'], v, k, ramp, amps[idx], rng):.6g}"
                    return (sep.join(parts) + "\n").encode("utf-8")
                try:
                    with open(self.work_file, "ab") as out:
                        while not stop.is_set():
                            f.seek(len(header))
                            for _ in range(start_row - 1):           # skip to the chosen start row
                                if stop.is_set() or not f.readline():
                                    break
                            played = 0
                            while not stop.is_set() and (not max_rows or played < max_rows):
                                carry += rate
                                n = int(carry)
                                carry -= n
                                chunk = []
                                for _ in range(min(n, max_rows - played) if max_rows else n):
                                    line = f.readline()
                                    if not line:
                                        break
                                    line = line if line.endswith(b"\n") else line + b"\n"
                                    if plan and total >= inject_after:
                                        line = transform(line, total - inject_after)
                                    elif plan:
                                        remember(line)
                                    chunk.append(line)
                                    total += 1
                                if n and not chunk:
                                    break                               # reached the end of the file
                                if chunk:
                                    out.write(b"".join(chunk))
                                    out.flush()
                                    played += len(chunk)
                                    self.src["rows"] += len(chunk)
                                stop.wait(1.0)
                            if not loop:
                                break
                    if not stop.is_set():
                        self._detail(msg("src.finished", rows=f"{self.src['rows']:,}"))
                finally:
                    f.close()
            th = threading.Thread(target=play, name="live-replay", daemon=True)
            th.start()
            return th
        self._begin("simulate", src.name, self.work_file, True, prepare, actor=actor)

    def start_demo(self, d: dict[str, Any], actor: str = "") -> None:
        """A built-in synthetic plant: normal running, then a drift on a few sensors, then one sensor freezes.
        ``scenario`` "plant" (generic names; the drift matches the generic mean-shift signature) or "te"
        (Tennessee-Eastman-style names; the drift matches the documented Fault 1 signature)."""
        rate = float(d.get("rate", 20) or 20)
        scenario = str(d.get("scenario") or "plant").strip().lower()
        if scenario not in DEMO_PLANTS:
            raise ValueError(f"Unknown demo scenario '{scenario}'. Choose one of: {', '.join(DEMO_PLANTS)}.")
        plant = DEMO_PLANTS[scenario]
        self._set_cfg(d.get("interval", 10), rate, d.get("baseline_cycles", 3))
        per_cycle = rate * self.cfg["interval"]
        freeze_cycle, total = plant["freeze_cycle"], int(per_cycle * plant["cycles"])

        def prepare(stop: threading.Event) -> threading.Thread:
            names, drift, freeze = plant["names"], plant["drift"], plant["freeze"]
            self.work_file.write_text("datetime," + ",".join(names) + "\n", encoding="utf-8")
            common = {"rate": f"{rate:g}", "drift": ", ".join(drift), "freeze": freeze, "cycle": freeze_cycle}
            self._detail(msg("src.demo.slow", step=f"{max(abs(a) for a in drift.values()):g}", **common) if plant.get("drift_per_cycle")
                         else msg("src.demo.fast", end=freeze_cycle - 1, **common))
            self.src["scenario"] = scenario
            self.src["name_msg"] = msg("src.name." + scenario)

            def play() -> None:
                readings, i, carry, t0 = demo_readings(plant, per_cycle), 0, 0.0, datetime.now()
                with open(self.work_file, "a", encoding="utf-8") as out:
                    while not stop.is_set() and i < total:
                        carry += rate
                        lines = []
                        for row in itertools.islice(readings, int(carry)):
                            lines.append(f"{(t0 + timedelta(seconds=i)).isoformat(timespec='seconds')},{','.join(f'{v:.5g}' for v in row)}\n")
                            i += 1
                        carry -= int(carry)
                        if lines:
                            out.write("".join(lines))
                            out.flush()
                            self.src["rows"] += len(lines)
                        stop.wait(1.0)
                if not stop.is_set():
                    self._detail(msg("src.demo.finished", rows=f"{self.src['rows']:,}"))
            th = threading.Thread(target=play, name="live-demo", daemon=True)
            th.start()
            return th
        self._begin("demo", "demo plant" if scenario == "plant" else "demo plant (TE names)", self.work_file, True, prepare, actor=actor)

    def start_live(self, d: dict[str, Any], actor: str = "") -> None:
        """Follow a real, growing dataset: a link (http/https) or the path of a file another program keeps writing."""
        target = str(d.get("target") or "").strip().strip('"').strip("'")
        self._set_cfg(d.get("interval", 900), d.get("rows_per_sec", 1), d.get("baseline_cycles", 4))
        if not target:
            raise ValueError("Paste a link or a file path first.")
        if not re.match(r"https?://", target, re.I):
            p = Path(target)
            if not p.is_file():
                raise ValueError(f"File not found: {p}")
            return self._begin("live-file", p.name, p, False, detail=msg("src.watching", path=str(p)), actor=actor)
        url = target

        def prepare(stop: threading.Event) -> threading.Thread:
            client = httpx.Client(timeout=20, follow_redirects=True)
            try:                                              # peek at the start: header line and where the data ends now
                r = client.get(url, headers={"Range": "bytes=0-65535"})
            except httpx.HTTPError as e:
                client.close()
                raise ValueError(f"Could not reach the link: {e}")
            if r.status_code not in (200, 206):
                client.close()
                raise ValueError(f"The link answered with HTTP {r.status_code}.")
            ranged, body = r.status_code == 206, r.content
            if ranged:
                m = re.search(r"/(\d+)\s*$", r.headers.get("content-range", ""))
                if not m:
                    client.close()
                    raise ValueError("The server did not say how large the file is.")
                size = int(m.group(1))
            else:
                size = len(body)
                if size > MAX_ROWS_WITHOUT_RANGE:
                    client.close()
                    raise ValueError("The server cannot send only the new part and the file is large (over 100 MB).")
            nl = body.find(b"\n")
            if nl < 0 or body.lstrip()[:1] in (b"{", b"[", b"<"):
                client.close()
                raise ValueError("The link does not return CSV text with a header line (JSON or web pages are not supported).")
            header = body[: nl + 1]
            self.work_file.write_bytes(header)
            self._detail(msg("src.link.waiting", url=url))

            def poll() -> None:
                nonlocal ranged
                pos = size                                    # only rows added from now on are analysed
                try:
                    with open(self.work_file, "ab") as out:
                        while not stop.wait(1.0):
                            try:
                                if ranged:
                                    r = client.get(url, headers={"Range": f"bytes={pos}-"})
                                    if r.status_code == 416:
                                        data = b""                            # nothing new yet
                                    elif r.status_code == 206:
                                        data = r.content
                                    elif r.status_code == 200:                # the server ignores ranges after all
                                        ranged, data = False, r.content[pos:]
                                    else:
                                        raise RuntimeError(f"HTTP {r.status_code}")
                                else:
                                    r = client.get(url)
                                    if r.status_code != 200:
                                        raise RuntimeError(f"HTTP {r.status_code}")
                                    if len(r.content) < pos:                  # the file was replaced by a shorter one
                                        pos = len(header)
                                    data = r.content[pos:]
                                cut = data.rfind(b"\n")
                                if cut >= 0:
                                    out.write(data[: cut + 1])
                                    out.flush()
                                    pos += cut + 1
                                    self.src["rows"] += data[: cut + 1].count(b"\n")
                                self.src["error"] = ""
                                self._detail(msg("src.link.checked", url=url, time=f"{datetime.now():%H:%M:%S}"))
                            except Exception as e:
                                self.src["error"] = f"Could not fetch new data: {e}"
                finally:
                    client.close()
            th = threading.Thread(target=poll, name="live-link", daemon=True)
            th.start()
            return th
        self._begin("live-url", url, self.work_file, True, prepare, actor=actor)
