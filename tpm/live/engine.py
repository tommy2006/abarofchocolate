"""Live monitor analysis: what "normal" looks like, and how far one analysis cycle is from it.

Pure functions, no global state and no I/O apart from ``Feed`` (which reads a growing CSV file). Each sensor of
a cycle is judged on four checks (level, trend, noise, range) plus dead / missing, so a decision never rests on a
single number. Every sentence is returned as a message key plus variables (``msg``): the UI translates the key
(``live.msg.<key>`` in i18n/*.json), the English text is kept for logs and for the language model.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

LEVELS = ["ok", "watch", "alarm"]
MODE_DEFAULTS = {"percent": {"watch": 1.0, "alarm": 2.0}, "spreads": {"watch": 1.5, "alarm": 3.0}}
DEFAULT_SETTINGS = {"mode": "percent", "watch": 1.0, "alarm": 2.0, "noise_ratio": 2.0, "outside_pct": 20.0}
NOT_SENSORS = {"faultnumber", "simulationrun", "sample", "source", "fault_status", "anomaly", "changepoint", "id", "index"}

# English wording of every message. The UI has the same keys as ``live.msg.<key>``; a test keeps both in step.
MSG = {
    "level.above": "Level: {v}{u} above normal.",
    "level.below": "Level: {v}{u} below normal.",
    "level.fine": "Level: {v}{u} from normal (fine).",
    "trend.rising.cycle": "Trend: rising steadily, {v}{u} within this cycle.",
    "trend.rising.three": "Trend: rising steadily, {v}{u} over the last 3 cycles.",
    "trend.falling.cycle": "Trend: falling steadily, {v}{u} within this cycle.",
    "trend.falling.three": "Trend: falling steadily, {v}{u} over the last 3 cycles.",
    "trend.steady": "Trend: steady.",
    "noise.jumpy": "Noise: readings are {r}x more jumpy than normal.",
    "noise.quiet": "Noise: readings vary only {r}x as much as normal (unusually quiet).",
    "noise.normal": "Noise: normal.",
    "range.out": "Range: {v}% of readings are outside the min-max seen while learning.",
    "range.ok": "Range: inside what was seen while learning.",
    "missing": "{v}% of its readings were empty this cycle.",
    "dead.changes": "Value did not change at all this cycle, but it normally does. Check the sensor.",
    "dead.flat": "Has been completely flat since monitoring began. Check the sensor.",
    "ok": "Behaving normally.",
    "kind.constant": "Constant value",
    "kind.onoff": "On/off signal",
    "kind.stepped": "Stepped signal (only a few states)",
    "kind.whole": "Whole-number reading (counter or level)",
    "kind.slow": "Slow analyzer (updates only every few readings)",
    "kind.continuous": "Continuous measurement",
    "prof.values": "{n} different values seen while learning",
    "prof.changes": "changes at {p}% of readings",
    "prof.smooth": "moves smoothly",
    "prof.moderate": "moves moderately smoothly",
    "prof.jumpy": "is jumpy / noisy from reading to reading",
    "prof.still": "does not move",
    "prof.level.stable": "normal level very stable (spread is {rel}% of its level)",
    "prof.level.fairly": "normal level fairly stable (spread is {rel}% of its level)",
    "prof.level.varies": "normal level varies a lot (spread is {rel}% of its level)",
    "prof.level.zero": "level is around zero",
    "prof.unit": "unit unknown (not stated in the file)",
    "verdict.ok": "Everything looks normal.",
    "verdict.watch": "{n} sensor(s) are slightly off normal. Keep an eye on them.",
    "verdict.drift": "The process is drifting away from normal. {n} sensor(s) are in alarm, most affected first: {list}.",
    "verdict.quality": "{n} sensor(s) look broken: {list}. This is a sensor problem, not a process problem, and these sensors are left out of the drift analysis.",
    "verdict.quality.alarm": "Separately, {n} other sensor(s) are in alarm, so the process may also be drifting: {list}.",
    "verdict.untrusted": "{n} of {total} sensors look broken (dead or missing data). The data can no longer be trusted, so no process diagnosis is made. Fix the data source first.",
    "verdict.gap": "Only {rows} rows arrived in this cycle (about {expected} expected). The data stream has gaps or has stopped, so nothing below can be trusted until it recovers.",
    "verdict.learn": "Learning what normal looks like ({done} of {total} cycles). Alerts start after that.",
    "verdict.wait": "Waiting for the first analysis cycle to finish.",
    "verdict.nosource": "No data source yet. Choose one on the Settings tab.",
    "verdict.stopped": "The data source was stopped. Choose another one on the Settings tab.",
    "sig.imminent": "Drifting towards a known failure type: {name} (match {score}%) on {sensors}.",
    "sig.occurring": "A known failure type is occurring: {name} (match {score}%) on {sensors}.",
    "sig.cleared": "{name}: no longer matching (match {score}%).",
}
CHECKS = ("level", "trend", "noise", "range")
CHECK_LABEL_EN = {"level": "Level", "trend": "Trend", "noise": "Noise", "range": "Range"}


def msg(key: str, **vars: Any) -> dict[str, Any]:
    """A translatable sentence: key + variables + the English text."""
    v = {k: (f"{val:g}" if isinstance(val, float) else str(val)) for k, val in vars.items()}      # variables are always text
    text_vars = dict(v)
    if text_vars.get("u") == "sp":
        text_vars["u"] = " normal-spreads"
    return {"key": key, "vars": v, "text": MSG[key].format(**text_vars)}


# ------------------------------------------------------------------ operator settings
def check_settings(d: dict[str, Any]) -> dict[str, Any]:
    """Validate the thresholds. Raises ValueError with a readable message."""
    try:
        s = {"mode": str(d["mode"]), "watch": float(d["watch"]), "alarm": float(d["alarm"]),
             "noise_ratio": float(d["noise_ratio"]), "outside_pct": float(d["outside_pct"])}
    except (KeyError, TypeError, ValueError):
        raise ValueError("Settings are incomplete or not numbers.")
    if s["mode"] not in MODE_DEFAULTS:
        raise ValueError("Unknown mode.")
    if not 0 < s["watch"] < s["alarm"]:
        raise ValueError("Watch must be above 0 and lower than Alarm.")
    if s["alarm"] > (1000 if s["mode"] == "percent" else 100):
        raise ValueError("Alarm level is too large.")
    if not 1.2 <= s["noise_ratio"] <= 50:
        raise ValueError("Noise factor must be between 1.2 and 50.")
    if not 1 <= s["outside_pct"] <= 100:
        raise ValueError("Out-of-range share must be between 1 and 100.")
    return s


# ------------------------------------------------------------------ reading the growing file
def sensor_columns(df: pd.DataFrame) -> list[str]:
    """Any numeric column that is not a time stamp, an id or a label. No sensor names are assumed."""
    return [c for c in df.columns if c.lower() not in NOT_SENSORS and not re.search(r"time|date", c, re.I)
            and pd.to_numeric(df[c], errors="coerce").notna().mean() > 0.9]


class Feed:
    """Reads only the new, complete lines of a CSV that keeps growing (it remembers where it stopped)."""

    def __init__(self, path: str | Path, from_start: bool):
        self.path, self.from_start = Path(path), from_start
        self.pos, self.hdr_end, self.header, self.sep = 0, 0, None, ","
        self._init()

    def _init(self) -> None:
        try:
            with open(self.path, "rb") as f:
                size = f.seek(0, 2)
                f.seek(0)
                line = f.readline()
                if not line.endswith(b"\n"):             # header not completely written yet
                    return
                first = line.decode("utf-8-sig", errors="replace").strip()
                self.sep = ";" if first.count(";") > first.count(",") else ","      # ; or , separated files
                self.header, self.hdr_end = first.split(self.sep), f.tell()
                self.pos = self.hdr_end if self.from_start else size
        except FileNotFoundError:
            pass

    def read_new(self) -> pd.DataFrame:
        if self.header is None:
            self._init()
            if self.header is None:
                return pd.DataFrame()
        try:
            with open(self.path, "rb") as f:
                if f.seek(0, 2) < self.pos:               # file was replaced / truncated
                    self.pos = self.hdr_end
                f.seek(self.pos)
                data = f.read()
        except FileNotFoundError:
            return pd.DataFrame(columns=self.header)
        end = data.rfind(b"\n")                          # only whole lines, the last one may be half written
        if end < 0:
            return pd.DataFrame(columns=self.header)
        self.pos += end + 1
        return pd.read_csv(io.BytesIO(data[: end + 1]), names=self.header, header=None, sep=self.sep)


# ------------------------------------------------------------------ statistics
def frame_stats(v: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Per sensor: fitted change across the frame (its trend) and the noise left after removing that trend."""
    n = len(v)
    if n < 3:
        z = pd.Series(0.0, index=v.columns)
        return z, z
    tc = np.arange(n, dtype=float) - (n - 1) / 2
    vc = v - v.mean()
    k = vc.mul(tc, axis=0).sum() / (tc ** 2).sum()
    resid = vc - pd.DataFrame(np.outer(tc, k.values), index=v.index, columns=v.columns)
    return k * (n - 1), resid.std(ddof=0)


def describe(s: pd.Series, cr: float, sigma: float, mu: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Plain-language type of a signal, worked out from how it behaves (the file has no documentation)."""
    v = s.dropna()
    nu = int(v.nunique())
    if nu <= 1:
        kind = "kind.constant"
    elif nu == 2:
        kind = "kind.onoff"
    elif nu <= 8:
        kind = "kind.stepped"
    elif (v % 1 == 0).all():
        kind = "kind.whole"
    elif cr < 0.6:
        kind = "kind.slow"
    else:
        kind = "kind.continuous"
    ac = s.autocorr(1) if nu > 1 else float("nan")
    smooth = ("prof.smooth" if ac >= 0.9 else "prof.moderate" if ac >= 0.5 else "prof.jumpy") if pd.notna(ac) else "prof.still"
    rel = sigma / abs(mu) * 100 if mu else float("inf")
    level = msg("prof.level.stable" if rel < 1 else "prof.level.fairly" if rel < 5 else "prof.level.varies",
                rel=f"{rel:.1f}") if np.isfinite(rel) else msg("prof.level.zero")
    return msg(kind), [msg("prof.values", n=nu), msg("prof.changes", p=f"{cr * 100:.0f}"), msg(smooth), level, msg("prof.unit")]


def step_threshold(b: dict[str, Any]) -> float:
    """A reading-to-reading change bigger than this is a "jump" for that sensor (used by the jumpy / valve-sticking check)."""
    return float(max(4 * b["noise_ref"], 0.5 * b["sigma"]))


def _step_frac(s: pd.Series, thr: float) -> float:
    d = s.diff().iloc[1:].abs()
    return float((d > thr).mean()) if len(d) else 0.0


def typical_moves(v: pd.DataFrame) -> pd.Series:
    """Per sensor: the typical (median) size of a reading-to-reading change, counting only readings that changed, so
    an analyzer that repeats its value between updates is judged on its updates. More jitter makes every move bigger;
    a valve that sticks and then jumps keeps its typical move and only adds a few big jumps."""
    d = v.diff().iloc[1:].abs()
    return d.where(d > 0).median().fillna(0.0)


def learn(frames: list[pd.DataFrame], sensors: list[str]) -> dict[str, dict[str, Any]]:
    """What normal looks like for every sensor. Raw rows are not kept, only these statistics."""
    vs = [f[sensors].apply(pd.to_numeric, errors="coerce") for f in frames]
    v = pd.concat(vs)
    mu, sd = v.mean(), v.std(ddof=0)
    cr = (v.diff().iloc[1:].abs() > 0).sum() / max(len(v) - 1, 1)
    stats = [frame_stats(x) for x in vs]
    moves = [typical_moves(x) for x in vs]
    drift_ref = pd.concat([s[0] for s in stats], axis=1).abs().mean(axis=1)      # how much it wanders inside a normal cycle
    noise_ref = pd.concat([s[1] for s in stats], axis=1).mean(axis=1)            # normal jitter after removing the trend
    out: dict[str, dict[str, Any]] = {}
    for c in sensors:
        sigma = float(sd[c]) if sd[c] > 0 else max(abs(mu[c]) * 1e-3, 1e-6)
        lo, hi = float(v[c].min()), float(v[c].max())
        kind, profile = describe(v[c], float(cr[c]), sigma, float(mu[c]))
        out[c] = dict(mu=float(mu[c]), sigma=sigma, change_rate=float(cr[c]), lo=lo, hi=hi, kind=kind, profile=profile,
                      basis=float(abs(mu[c]) if abs(mu[c]) >= 2 * sigma else max(hi - lo, sigma)),
                      drift_ref=float(max(drift_ref[c], sigma * 0.05)), noise_ref=float(max(noise_ref[c], sigma * 1e-3, 1e-9)))
        out[c]["step_ref"] = float(np.mean([_step_frac(x[c], step_threshold(out[c])) for x in vs]))   # normal share of jumps
        out[c]["move_ref"] = float(max(np.mean([float(m[c]) for m in moves]), sigma * 1e-3, 1e-12))   # normal typical move
    return out


def cycle_stats(df: pd.DataFrame, sensors: list[str], base: dict[str, dict[str, Any]]) -> dict[str, Any]:
    v = df[sensors].apply(pd.to_numeric, errors="coerce")
    change, noise = frame_stats(v)
    outside = pd.Series({c: float(((v[c] < base[c]["lo"]) | (v[c] > base[c]["hi"])).mean() * 100) for c in sensors})
    steps = pd.Series({c: _step_frac(v[c], step_threshold(base[c])) for c in sensors})
    return dict(n=len(v), mean=v.mean(), changes=(v.diff().iloc[1:].abs() > 0).sum(), miss=v.isna().mean() * 100,
                lo=v.min(), hi=v.max(), change=change, noise=noise, outside=outside, steps=steps, std=v.std(ddof=0),
                moves=typical_moves(v))


def features(st: dict[str, Any], base: dict[str, dict[str, Any]], past: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """How each sensor behaves this cycle against its learned normal, in units the known-failure signatures use
    (``tpm.live.signatures``): z = how many normal spreads the cycle average is from normal (signed), var_ratio = jitter
    versus normal, slope = fitted change within the cycle in spreads, jump_ratio = share of big reading-to-reading
    jumps versus normal, move_ratio = typical reading-to-reading move versus normal, z_hist = z of the previous cycles
    (oldest first), mono = how steadily the last cycles moved in one direction (0..1). Statistics only; no readings."""
    out: dict[str, dict[str, Any]] = {}
    for c, b in base.items():
        if c not in st["mean"].index:
            continue
        sigma = b["sigma"] if b["sigma"] > 0 else 1e-9
        mean = float(st["mean"][c]) if pd.notna(st["mean"][c]) else b["mu"]
        z = (mean - b["mu"]) / sigma
        noise = float(st["noise"][c]) if pd.notna(st["noise"][c]) else b["noise_ref"]
        var_ratio = noise / b["noise_ref"] if b["noise_ref"] > 0 else 1.0
        slope = (float(st["change"][c]) if pd.notna(st["change"][c]) else 0.0) / sigma
        steps = float(st.get("steps", {}).get(c, 0.0)) if "steps" in st else 0.0
        jump_ratio = (steps + 0.002) / (float(b.get("step_ref", 0.0)) + 0.002)
        moves = st.get("moves")
        move = float(moves[c]) if moves is not None and c in moves.index and pd.notna(moves[c]) else None
        move_ratio = move / b["move_ref"] if move is not None and b.get("move_ref") else 1.0
        z_hist = [(float(h["mean"][c]) - b["mu"]) / sigma for h in past[-6:] if c in h.get("mean", {}) and h["mean"][c] is not None]
        seq = z_hist + [z]
        diffs = [seq[i + 1] - seq[i] for i in range(len(seq) - 1)]
        sig = [d for d in diffs if abs(d) >= 0.15][-4:]
        mono = max(sum(d > 0 for d in sig), sum(d < 0 for d in sig)) / 4.0 if sig else 0.0
        out[c] = dict(z=float(z), var_ratio=float(var_ratio), slope=float(slope), jump_ratio=float(jump_ratio), move_ratio=float(move_ratio),
                      steps=steps, step_ref=float(b.get("step_ref", 0.0)),
                      z_hist=[float(x) for x in z_hist], prev_z=float(z_hist[-1]) if z_hist else None, mono=float(mono),
                      mean=mean, missing=float(st["miss"][c]) if pd.notna(st["miss"][c]) else 0.0)
    return out


def grade(val: float, warn: float, alarm: float) -> int:
    return 2 if val >= alarm else 1 if val >= warn else 0


def judge(st: dict[str, Any], base: dict[str, dict[str, Any]], past: list[dict[str, Any]], S: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per sensor. Broken sensors are decided first; otherwise the worst of four checks decides."""
    pct = S["mode"] == "percent"
    u = "%" if pct else "sp"
    rows: list[dict[str, Any]] = []
    for c, b in base.items():
        if c not in st["mean"].index:
            continue
        conv = lambda x, b=b: abs(x) / b["basis"] * 100 if pct else abs(x) / b["sigma"]
        mean = float(st["mean"][c]) if pd.notna(st["mean"][c]) else b["mu"]
        dev = mean - b["mu"]

        lv = conv(dev)                                                             # 1. level
        L = grade(lv, S["watch"], S["alarm"])
        m = msg("level.fine" if not L else "level.above" if dev > 0 else "level.below", v=f"{lv:.1f}", u=u)
        el = [dict(name="level", level=L, short=f"{'▲' if dev > 0 else '▼'} {lv:.1f}{'%' if pct else 'σ'}", **m)]

        means = [h["mean"].get(c) for h in past[-3:]] + [mean]                     # 2. trend
        cross, cdir = 0.0, 0
        if len(means) == 4 and None not in means:
            dif = np.diff(means)
            # must be a steady move away from normal, and bigger than half a normal spread (not chance wobble)
            if (((dif > 0).all() or (dif < 0).all()) and abs(means[-1] - b["mu"]) > abs(means[0] - b["mu"])
                    and abs(means[-1] - means[0]) >= 0.5 * b["sigma"]):
                cross, cdir = conv(means[-1] - means[0]), 1 if dif[0] > 0 else -1
        slope = float(st["change"][c]) if pd.notna(st["change"][c]) else 0.0
        chance = 3 * b["noise_ref"] * (12 / max(st["n"], 3)) ** 0.5               # size a pure-noise sensor could fake
        within = conv(slope) if abs(slope) >= max(2 * b["drift_ref"], chance) else 0.0
        if within and dev * slope < 0 and lv >= S["watch"]:                        # recovering towards normal is not a problem
            within = 0.0
        tv = max(cross, within)
        tdir = (1 if slope > 0 else -1) if within >= cross and within else cdir
        T = grade(tv, S["watch"], S["alarm"])
        span = "cycle" if within >= cross else "three"
        m = msg(f"trend.{'rising' if tdir > 0 else 'falling'}.{span}", v=f"{tv:.1f}", u=u) if T else msg("trend.steady")
        el.append(dict(name="trend", level=T, short=("▲" if tdir > 0 else "▼") if tv else "–", **m))

        ratio = float(st["noise"][c]) / b["noise_ref"] if pd.notna(st["noise"][c]) else 1.0   # 3. noise
        N = 0
        if ratio >= S["noise_ratio"]:
            N = 2 if ratio >= 2 * S["noise_ratio"] else 1
        elif ratio <= 1 / S["noise_ratio"] and b["change_rate"] > 0:
            N = 1
        m = msg("noise.jumpy", r=f"{ratio:.1f}") if N and ratio > 1 else msg("noise.quiet", r=f"{ratio:.2f}") if N else msg("noise.normal")
        el.append(dict(name="noise", level=N, short=f"{ratio:.1f}×", **m))

        out = float(st["outside"][c])                                              # 4. range
        R = grade(out, S["outside_pct"], 2 * S["outside_pct"])
        m = msg("range.out", v=f"{out:.0f}") if R else msg("range.ok")
        el.append(dict(name="range", level=R, short=f"{out:.0f}%", **m))

        expected = b["change_rate"] * (st["n"] - 1)
        if st["miss"][c] > 1:
            status, notes = "missing", [msg("missing", v=f"{st['miss'][c]:.1f}")]
        elif st["changes"][c] == 0 and (expected >= 3 or b["change_rate"] == 0):
            status, notes = "dead", [msg("dead.changes" if b["change_rate"] > 0 else "dead.flat")]
        else:
            status = LEVELS[max(L, T, N, R)]
            notes = [{k: e[k] for k in ("key", "vars", "text")} for e in el if e["level"]] or [msg("ok")]
        rows.append(dict(sensor=c, status=status, shift=round(abs(dev) / b["sigma"], 2), mean=round(mean, 4),
                         worse=T > 0, notes=notes, note=" ".join(n["text"] for n in notes), kind=b["kind"], mu=round(b["mu"], 4),
                         sigma=round(b["sigma"], 4), basis=round(b["basis"], 4), dev=round(dev, 4),
                         dev_pct=round(dev / b["basis"] * 100, 2), level_val=round(lv, 2),
                         cur_lo=round(float(st["lo"][c]), 4), cur_hi=round(float(st["hi"][c]), 4),
                         elements=el, score=max(L, T, N, R) * 1000 + lv))
    return rows


def verdict(rows: list[dict[str, Any]], n_sensors: int) -> tuple[str, dict[str, Any], list[dict], list[dict]]:
    """Overall level + one translatable sentence. A broken sensor is reported as a sensor problem, not a process one."""
    by = lambda s: [r for r in rows if r["status"] == s]
    broken, watch = by("dead") + by("missing"), by("watch")
    alarms = sorted(by("alarm"), key=lambda r: -r["score"])
    items = lambda rs: [{"sensor": r["sensor"], "checks": [e["name"] for e in r["elements"] if e["level"] == 2]} for r in rs[:5]]
    listing = lambda rs: ", ".join(f"{i['sensor']} ({', '.join(CHECK_LABEL_EN[c] for c in i['checks'])})" for i in items(rs))
    names = lambda rs: ", ".join(r["sensor"] for r in rs)

    def build(key: str, **vars: Any) -> dict[str, Any]:
        m = msg(key, **vars)
        return m

    if len(broken) > 0.25 * n_sensors and len(broken) >= 2:          # one broken sensor of a small set is a sensor problem
        m = build("verdict.untrusted", n=len(broken), total=n_sensors)
        return "untrusted", m, broken, alarms
    if broken:
        m = build("verdict.quality", n=len(broken), list=names(broken))
        m["parts"] = [{"key": "verdict.quality", "vars": {"n": str(len(broken)), "list": names(broken)}}]
        if alarms:
            extra = build("verdict.quality.alarm", n=len(alarms), list=listing(alarms))
            m["text"] += " " + extra["text"]
            m["parts"].append({"key": "verdict.quality.alarm", "vars": {"n": str(len(alarms))}, "items": items(alarms)})
        return "quality", m, broken, alarms
    if alarms:
        m = build("verdict.drift", n=len(alarms), list=listing(alarms))
        m["items"] = items(alarms)
        return "drift", m, broken, alarms
    if watch:
        return "watch", build("verdict.watch", n=len(watch)), broken, alarms
    return "ok", build("verdict.ok"), broken, alarms
