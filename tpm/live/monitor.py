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
import json
import random
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from . import engine
from .engine import Feed, check_settings, msg

MAX_ROWS_WITHOUT_RANGE = 100_000_000          # a link that cannot send "only the new part" may be at most this many bytes
KEEP = 96                                     # analysis cycles kept for the history graph (24 h at 15 min)


def _iso(ts: Optional[float] = None) -> str:
    return (datetime.fromtimestamp(ts) if ts else datetime.now()).isoformat(timespec="seconds")


class LiveMonitor:
    def __init__(self, get_settings: Callable[[], Any], data_dir: str | Path):
        self._get_settings = get_settings
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.uploads = self.dir / "uploads"
        self.work_file = self.dir / "live_work.csv"
        self.log_file = self.dir / "log.jsonl"
        self.settings_file = self.dir / "settings.json"
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
        return {"kind": "none", "name": "", "path": "", "detail": "", "error": "", "rows": 0}

    @staticmethod
    def _fresh_state() -> dict[str, Any]:
        return {"baseline": None, "history": [], "events": [], "cycles": 0, "advice": "", "learning": 0, "sensors": [], "since": {},
                "updated": None, "next_at": None, "verdict": {"level": "wait", **msg("verdict.nosource")}, "table": []}

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

    # ------------------------------------------------------------------ what the page shows
    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            st = self.state
            out = {
                "source": {k: self.src.get(k) for k in ("kind", "name", "path", "detail", "error", "rows")},
                "running": self.feed is not None,
                "cfg": dict(self.cfg), "expected_rows": int(self.expected_rows()),
                "settings": dict(self.set), "settings_by": dict(self.set_by),
                "baseline_ready": bool(st["baseline"]), "learning": st["learning"], "cycles": st["cycles"],
                "updated": st["updated"], "next_at": st["next_at"], "verdict": st["verdict"], "advice": st["advice"],
                "sensors": st["sensors"], "table": st["table"], "baseline": st["baseline"], "history": st["history"],
                "events": st["events"][:60], "now": _iso(),
            }
            return json.loads(json.dumps(out, default=str))

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
        if real:                                             # event log = status changes only
            for r in rows:
                old = self._prev["status"].get(r["sensor"], "ok")
                if old != r["status"]:
                    self.state["events"].insert(0, {"t": when.isoformat(timespec="seconds"), "kind": "status", "sensor": r["sensor"],
                                                    "old": old, "new": r["status"], "notes": r["notes"],
                                                    "text": f"{r['sensor']}: {old} -> {r['status']}. {r['note']}"})
            self.state["events"] = self.state["events"][:60]
        self._prev["status"] = {r["sensor"]: r["status"] for r in rows}
        return rows, level, m, broken, alarms

    def run_cycle(self, when: Optional[datetime] = None, lang: Optional[str] = None) -> None:
        """Read what arrived since the last cycle and analyse it (called by the loop; tests call it directly)."""
        now = when or datetime.now()
        lang = lang or getattr(getattr(self.app_settings, "report", None), "default_language", "en") or "en"
        with self.lock:
            feed = self.feed
            if feed is None:
                return
            df = feed.read_new()
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
                    facts = {"broken_sensors": [r["sensor"] for r in broken],
                             "alarm_sensors_most_affected_first": [r["sensor"] for r in alarms[:5]], "verdict": m["text"]}
                    threading.Thread(target=self._advice, args=(facts, lang), daemon=True).start()
            elif not (broken or alarms):
                st["advice"] = ""
            self._prev["sig"] = sig
            self._log({**log, "decision": level, "verdict": m["text"], "settings": dict(self.set),
                       "dead": [r["sensor"] for r in rows if r["status"] == "dead"],
                       "missing": [r["sensor"] for r in rows if r["status"] == "missing"],
                       "alarms": [(r["sensor"], [e["name"] for e in r["elements"] if e["level"] == 2]) for r in alarms]})

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
               reset: bool = True, detail: str = "", actor: str = "") -> None:
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
            self.src.update(kind=kind, name=name, path=str(path), detail=detail or self.src["detail"])
            self.feed = Feed(path, from_start)
            self._wake.set()
            self._ensure_loop()
            self._log({"decision": "source_changed", "kind": kind, "name": name, "actor": actor, "config": dict(self.cfg)})

    def stop_source(self, actor: str = "") -> None:
        with self.lock:
            self._stop_worker()
            self.src.update(kind="none", name="", detail="Stopped by the operator.", error="")
            self.feed = None
            self.state["verdict"] = {"level": "wait", **msg("verdict.stopped")}
            self._wake.set()
            self._log({"decision": "source_stopped", "actor": actor})

    def start_simulation(self, d: dict[str, Any], actor: str = "") -> None:
        """Replay a (possibly huge) CSV into the working file at a chosen speed, like a plant writing live data."""
        src = Path(str(d.get("path") or "").strip().strip('"').strip("'"))
        try:
            rate, start_row, max_rows = float(d.get("rate", 10)), int(d.get("start_row", 1) or 1), int(d.get("max_rows", 0) or 0)
        except (TypeError, ValueError):
            raise ValueError("Rate, start row and rows to play must be numbers.")
        loop = bool(d.get("loop"))
        if not src.is_file():
            raise ValueError(f"File not found: {src}. Drop a file, or type its full path.")
        if rate <= 0 or start_row < 1 or max_rows < 0:
            raise ValueError("Rate must be above 0, start row at least 1, rows to play 0 or more.")
        self._set_cfg(d.get("interval", 20), rate, d.get("baseline_cycles", 2))

        def prepare(stop: threading.Event) -> threading.Thread:
            f = open(src, "rb")
            header = f.readline()
            if not header.strip() or header.lstrip()[:1] in (b"{", b"[", b"<"):
                f.close()
                raise ValueError("This does not look like a CSV file with a header line.")
            header = header if header.endswith(b"\n") else header + b"\n"
            self.work_file.write_bytes(header)
            self.src["detail"] = f"{src.name}: {rate:g} rows/s, from row {start_row:,}" + (f", {max_rows:,} rows" if max_rows else ", to the end") + (", repeating" if loop else "")

            def play() -> None:
                carry = 0.0
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
                                    chunk.append(line if line.endswith(b"\n") else line + b"\n")
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
                        self.src["detail"] = f"Finished: {self.src['rows']:,} rows were played. No more rows will arrive."
                finally:
                    f.close()
            th = threading.Thread(target=play, name="live-replay", daemon=True)
            th.start()
            return th
        self._begin("simulate", src.name, self.work_file, True, prepare, actor=actor)

    def start_demo(self, d: dict[str, Any], actor: str = "") -> None:
        """A built-in synthetic plant: normal running, then a slow drift on two sensors, then one sensor freezes."""
        rate = float(d.get("rate", 20) or 20)
        self._set_cfg(d.get("interval", 10), rate, d.get("baseline_cycles", 3))
        per_cycle = rate * self.cfg["interval"]
        drift_at, freeze_at, total = int(per_cycle * 6), int(per_cycle * 9), int(per_cycle * 12)

        def prepare(stop: threading.Event) -> threading.Thread:
            names = ["temperature", "pressure", "flow", "level", "vibration", "current"]
            means = {"temperature": 75.0, "pressure": 2.4, "flow": 32.0, "level": 60.0, "vibration": 0.03, "current": 1.0}
            sigmas = {"temperature": 0.15, "pressure": 0.01, "flow": 0.12, "level": 0.2, "vibration": 0.0002, "current": 0.005}
            self.work_file.write_text("datetime," + ",".join(names) + "\n", encoding="utf-8")
            self.src["detail"] = f"Built-in demo plant: {rate:g} rows/s, normal, then a drift, then one frozen sensor."

            def play() -> None:
                rng, x, i, carry = random.Random(7), {k: 0.0 for k in names}, 0, 0.0
                t0, frozen = datetime.now(), None
                with open(self.work_file, "a", encoding="utf-8") as out:
                    while not stop.is_set() and i < total:
                        carry += rate
                        lines = []
                        for _ in range(int(carry)):
                            row = []
                            for k in names:
                                x[k] = 0.9 * x[k] + rng.gauss(0, sigmas[k] * 0.45)          # smooth wander around the normal level
                                v = means[k] + x[k]
                                if i >= drift_at and k in ("temperature", "flow"):
                                    ramp = min(1.0, (i - drift_at) / max(1, freeze_at - drift_at))
                                    v += means[k] * (0.06 if k == "temperature" else -0.08) * ramp
                                if k == "level" and i >= freeze_at:
                                    frozen = v if frozen is None else frozen
                                    v = frozen
                                row.append(f"{v:.4f}")
                            lines.append(f"{(t0 + timedelta(seconds=i)).isoformat(timespec='seconds')},{','.join(row)}\n")
                            i += 1
                        carry -= int(carry)
                        if lines:
                            out.write("".join(lines))
                            out.flush()
                            self.src["rows"] += len(lines)
                        stop.wait(1.0)
                if not stop.is_set():
                    self.src["detail"] = f"Demo finished after {self.src['rows']:,} rows."
            th = threading.Thread(target=play, name="live-demo", daemon=True)
            th.start()
            return th
        self._begin("demo", "demo plant", self.work_file, True, prepare, actor=actor)

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
            return self._begin("live-file", p.name, p, False, detail=f"Watching {p} for new rows.", actor=actor)
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
            self.src["detail"] = f"Connected to {url}. Waiting for new rows (checking every second)."

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
                                self.src["detail"] = f"Connected to {url}. Last check {datetime.now():%H:%M:%S}."
                            except Exception as e:
                                self.src["error"] = f"Could not fetch new data: {e}"
                finally:
                    client.close()
            th = threading.Thread(target=poll, name="live-link", daemon=True)
            th.start()
            return th
        self._begin("live-url", url, self.work_file, True, prepare, actor=actor)
