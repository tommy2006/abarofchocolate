"""Tool-agent for the operator "why" chat and the assessor chat.

Model-agnostic JSON-action loop (no native tool calling): the model answers with one JSON object per
turn, either {"thought", "action": "<tool>", "args"} or {"thought", "action": "final", "answer", "citations",
...}. Every tool runs locally (DuckDB over dataset.parquet, workspace artifacts, the local index) and is
logged. When no model is available the chat still answers deterministically from the workspace
objects and their evidence, so the UI always gets something useful.

The mode is decided once per turn. Local (default): the local model may look at raw data through the tools.
External (the task routes external and the route is usable): the model gets no sql tool, stats / series return
aggregates over enough rows only, the loaded context and every tool result pass the egress guard before they are
put into a message, and the aliases in the final answer get their local names back on this machine. When the
external model fails mid-turn the turn is restarted on the local agent, then answered deterministically.

    from tpm.llm.agent import chat
    out = chat(ws, settings, "Why was FLAG-000001 raised?", context={"flag_id": "FLAG-000001"})
    out -> {"answer", "citations", "tool_trace", "source", "route", "external_calls", "suggested_followups", "series", "turn_id"}
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from typing import Any, Callable, Optional

from ..config import Settings, get_settings
from ..contracts import now_iso
from . import guard as guard_mod
from . import ledger as ledger_mod
from . import prompts as prompts_mod
from . import router as router_mod
from .embeddings import LocalIndex

SQL_LIMIT = 200
TOOL_RESULT_CHARS = 4000
CONTEXT_CHARS = 7000
EXTERNAL_EXCLUDED_TOOLS = {"sql"}  # a free-form query can return raw rows: never offered to an external model
EXTERNAL_STATS_KEYS = ["n", "mean", "std", "q05", "median", "q95", "n_null", "row_start", "row_end"]  # no min / max: single readings

TOOL_SPECS: list[dict[str, str]] = [
    {"name": "sql", "args": "query: str", "description": "read-only SELECT over views `dataset` (raw rows incl. __group__) and `scores` (per-row anomaly scores). A LIMIT is enforced. Use aggregates (avg, stddev, quantile_cont, count) rather than pulling rows."},
    {"name": "describe_signal", "args": "id: str", "description": "signal catalog entry for an alias (S07): role, hypotheses, fingerprint aggregates, related signals, evidence statements."},
    {"name": "stats", "args": "signal: str, group_id?: str, row_start?: int, row_end?: int", "description": "aggregates (n, mean, std, min, q05, median, q95, max, n_null) of one signal, optionally restricted to a group and/or a row range."},
    {"name": "series", "args": "signal: str, row_start: int, row_end: int, max_points?: int", "description": "downsampled series (row, mean, min, max per bucket) for a chart; local only, never sent anywhere."},
    {"name": "get_flag", "args": "id: str", "description": "one flag with ranked signals, trust context and its evidence statements."},
    {"name": "get_diagnosis", "args": "id: str", "description": "one diagnosis with steps, propagation, uncertainty, critique and evidence statements."},
    {"name": "get_evidence", "args": "id: str", "description": "one evidence item (statement, values, n_samples, signals)."},
    {"name": "list_checks", "args": "batch_id?: str, signal?: str", "description": "data-quality checks (pass/warn/fail) with statements, optionally filtered by batch or signal."},
    {"name": "search", "args": "query: str", "description": "semantic/keyword search over evidence, inferences, flags, diagnoses, checks, rules, patterns and the docs."},
    {"name": "assessor_evaluate", "args": "action_text: str", "description": "ask the data-quality assessor to evaluate a proposed action in natural language (bounded experiments; nothing is applied)."},
]

_FORBIDDEN_SQL = re.compile(r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|export|import|pragma|install|load|call|set|reset|truncate|merge|grant|vacuum|checkpoint|force)\b", re.I)
_FILE_SQL = re.compile(r"\b(read_(csv|parquet|json|text|blob)\w*|glob|parquet_scan|csv_scan|sniff_csv|read_ndjson\w*)\s*\(", re.I)
_ID_RE = re.compile(r"\b(FLAG-\d{3,}|DIAG-\d{3,}|EV-\d{3,}|CHK-\d{3,}|INF-\d{3,}|RULE-\d{2,}|PATTERN-[A-Z0-9]+|S\d{2,4})\b")
_ALIAS_TOKEN_RE = re.compile(r"\bS\d{2,5}\b")


def tool_specs(settings: Settings, external: bool = False) -> list[dict[str, str]]:
    """Tools offered to the model. An external model gets no sql, and stats / series described as the reductions they are."""
    if not external:
        return TOOL_SPECS
    g = settings.guard
    text = {
        "stats": {"description": f"aggregates (n, mean, std, q05, median, q95, n_null) of one signal, optionally restricted to a group and/or a row range; needs at least {g.min_aggregate_n} values."},
        "series": {"args": "signal: str, row_start: int, row_end: int", "description": f"coarse shape of one signal over a row range: at most {g.max_series_points} bucket means (each over at least {g.min_aggregate_n} rows), also given in standard deviations from the mean of the range."},
    }
    return [{**t, **text.get(t["name"], {})} for t in TOOL_SPECS if t["name"] not in EXTERNAL_EXCLUDED_TOOLS]


_T = {
    "en": {"flag": "Flag", "diagnosis": "Diagnosis", "signal": "Signal", "batch": "Batch", "evidence": "Evidence", "severity": "severity", "confidence": "confidence", "top_signals": "Signals contributing most", "steps": "Explanation steps", "uncertainty": "Uncertainty", "trust": "Trust verdict", "checks": "Data-quality checks", "related": "Related findings for your question", "nothing": "I could not link your question to a specific flag, diagnosis or signal. Here is what the run contains and the closest findings.", "no_model": "No local language model is available, so this answer was composed by code from the workspace objects and their evidence (source: template).", "contains": "This run contains", "flags": "flags", "diagnoses": "diagnoses", "checks_n": "checks", "signals_n": "signals", "assessor": "Assessor evaluation", "assessor_missing": "The assessor module is not available in this build, so the action could not be evaluated. Nothing was applied.", "rows": "rows"},
    "fi": {"flag": "Hälytys", "diagnosis": "Diagnoosi", "signal": "Signaali", "batch": "Erä", "evidence": "Todisteet", "severity": "vakavuus", "confidence": "luottamus", "top_signals": "Eniten vaikuttaneet signaalit", "steps": "Selityksen vaiheet", "uncertainty": "Epävarmuus", "trust": "Luotettavuusarvio", "checks": "Laatutarkistukset", "related": "Kysymykseesi liittyvät löydökset", "nothing": "En pystynyt liittämään kysymystäsi tiettyyn hälytykseen, diagnoosiin tai signaaliin. Tässä ajon sisältö ja lähimmät löydökset.", "no_model": "Paikallista kielimallia ei ole käytettävissä, joten tämän vastauksen kokosi koodi työtilan objekteista ja todisteista (lähde: template).", "contains": "Tämä ajo sisältää", "flags": "hälytystä", "diagnoses": "diagnoosia", "checks_n": "tarkistusta", "signals_n": "signaalia", "assessor": "Arvioijan tulos", "assessor_missing": "Arvioijamoduuli ei ole käytettävissä tässä versiossa, joten toimenpidettä ei voitu arvioida. Mitään ei tehty.", "rows": "riviä"},
    "sv": {"flag": "Flagga", "diagnosis": "Diagnos", "signal": "Signal", "batch": "Batch", "evidence": "Bevis", "severity": "allvarlighet", "confidence": "konfidens", "top_signals": "Signaler som bidrog mest", "steps": "Förklaringssteg", "uncertainty": "Osäkerhet", "trust": "Tillförlitlighetsbedömning", "checks": "Datakvalitetskontroller", "related": "Relaterade fynd för din fråga", "nothing": "Jag kunde inte koppla din fråga till en specifik flagga, diagnos eller signal. Här är vad körningen innehåller och de närmaste fynden.", "no_model": "Ingen lokal språkmodell är tillgänglig, så detta svar sattes ihop av kod från arbetsytans objekt och deras bevis (källa: template).", "contains": "Denna körning innehåller", "flags": "flaggor", "diagnoses": "diagnoser", "checks_n": "kontroller", "signals_n": "signaler", "assessor": "Bedömarens utvärdering", "assessor_missing": "Bedömarmodulen är inte tillgänglig i denna version, så åtgärden kunde inte utvärderas. Inget tillämpades.", "rows": "rader"},
}


def _t(lang: str) -> dict[str, str]:
    return _T.get((lang or "en").lower(), _T["en"])


def _dump(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, dict):
        return {k: _dump(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_dump(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            pass
    return obj


def _finite(col: str) -> str:
    """SQL expression for a column's finite values: stddev_samp raises on NaN / inf, so they count as missing."""
    return f'(CASE WHEN isfinite(TRY_CAST("{col}" AS DOUBLE)) THEN "{col}" END)'


# ----------------------------------------------------------------------------------------------
# Toolbox: every tool is local, bounded and logged
# ----------------------------------------------------------------------------------------------


class Toolbox:
    def __init__(self, ws: Any, settings: Settings, index: Optional[LocalIndex] = None, external: bool = False):
        self.ws = ws
        self.settings = settings
        self._index = index
        self._schema = None
        self._signals: Optional[dict[str, Any]] = None
        self.last_series: Optional[dict[str, Any]] = None
        self.external = bool(external)  # results feed an external model: no sql, aggregates over enough rows only
        self.names = [t["name"] for t in tool_specs(settings, self.external)]

    # ---- helpers ----
    @property
    def schema(self):
        if self._schema is None and self.ws is not None:
            try:
                self._schema = self.ws.schema()
            except Exception:
                self._schema = None
        return self._schema

    @property
    def signals(self) -> dict[str, Any]:
        if self._signals is None:
            self._signals = {}
            if self.ws is not None:
                try:
                    self._signals = {s.id: s for s in self.ws.signals()}
                except Exception:
                    self._signals = {}
        return self._signals

    @property
    def index(self) -> LocalIndex:
        if self._index is None:
            self._index = LocalIndex(self.ws, self.settings)
        return self._index

    def external_view(self) -> "Toolbox":
        """The same workspace through the tools an external model may use (shares what is already loaded)."""
        tb = Toolbox(self.ws, self.settings, self._index, external=True)
        tb._schema, tb._signals = self._schema, self._signals
        return tb

    def dataset_columns(self) -> list[str]:
        sch = self.schema
        return list(sch.columns) if sch else []

    def _signal_column(self, signal: str) -> tuple[Optional[str], Optional[str]]:
        """resolve_column for stats / series. For an external model only catalogued signals resolve: label, id and time
        columns are not summarised for it."""
        col, alias = self.resolve_column(signal)
        if self.external and col:
            sch = self.schema
            known = set(sch.signal_alias.values()) if sch else set()
            if not (alias in known or alias in self.signals):
                return None, None
        return col, alias

    def resolve_column(self, signal: str) -> tuple[Optional[str], Optional[str]]:
        """alias or original name -> (parquet column, alias)."""
        sch = self.schema
        if not signal:
            return None, None
        if sch:
            inv = {v: k for k, v in sch.signal_alias.items()}
            if signal in inv:
                return inv[signal], signal
            if signal in sch.columns:
                return signal, sch.signal_alias.get(signal, signal)
            for orig, alias in sch.signal_alias.items():
                if orig == signal or alias.lower() == signal.lower():
                    return orig, alias
        s = self.signals.get(signal)
        if s is not None and s.source_column:
            return s.source_column, s.id
        return None, None

    def _base_sql(self) -> str:
        cols = self.dataset_columns()
        if "__row__" in cols:
            return "(SELECT *, __row__ AS __rn FROM dataset)"
        return "(SELECT *, row_number() OVER () - 1 AS __rn FROM dataset)"

    def _group_col(self) -> Optional[str]:
        sch = self.schema
        if sch and sch.group_column in sch.columns:
            return sch.group_column
        return None

    def evidence_statements(self, ids: list[str], limit: int = 12) -> list[dict[str, Any]]:
        out = []
        if self.ws is None:
            return out
        for eid in list(dict.fromkeys(ids))[:limit]:
            e = self.ws.evidence.get(eid)
            if e:
                out.append({"id": e.id, "kind": e.kind, "signals": e.signals, "statement": e.statement, "n_samples": e.n_samples})
        return out

    # ---- dispatcher ----
    def call(self, name: str, args: Optional[dict[str, Any]]) -> dict[str, Any]:
        args = args or {}
        if name not in self.names:
            return {"error": f"unknown tool '{name}'. Available: {', '.join(self.names)}"}
        fn = getattr(self, f"tool_{name}")
        try:
            res = fn(**{k: v for k, v in args.items() if isinstance(k, str)})
        except TypeError as e:
            return {"error": f"bad arguments for {name}: {e}"}
        except Exception as e:
            return {"error": f"{name} failed: {str(e)[:300]}"}
        return _dump(res)

    # ---- tools ----
    def tool_sql(self, query: str = "", limit: int = SQL_LIMIT, **_: Any) -> dict[str, Any]:
        if self.ws is None or not self.ws.exists("dataset"):
            return {"error": "no dataset in this workspace"}
        safe, err = safe_sql(query, min(int(limit or SQL_LIMIT), SQL_LIMIT))
        if err:
            return {"error": err}
        con = self.ws.duckdb()
        t0 = time.time()
        cur = con.execute(safe)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return {"columns": cols, "rows": [[_dump(v) for v in r] for r in rows[:SQL_LIMIT]], "n_rows": len(rows), "sql": safe, "ms": int((time.time() - t0) * 1000)}

    def tool_describe_signal(self, id: str = "", **_: Any) -> dict[str, Any]:
        s = self.signals.get(id)
        if s is None:
            col, alias = self.resolve_column(id)
            s = self.signals.get(alias or "")
        if s is None:
            return {"error": f"signal '{id}' not found in the catalog", "known": list(self.signals)[:50]}
        d = s.model_dump()
        d["evidence"] = self.evidence_statements(s.evidence_ids)
        try:
            d["inferences"] = [{"id": i.id, "claim": i.claim, "status": i.status, "confidence": i.confidence} for i in self.ws.inferences.all() if i.subject == s.id][:6]
        except Exception:
            d["inferences"] = []
        return d

    def tool_stats(self, signal: str = "", group_id: Optional[str] = None, row_start: Optional[int] = None, row_end: Optional[int] = None, **_: Any) -> dict[str, Any]:
        if self.ws is None or not self.ws.exists("dataset"):
            return {"error": "no dataset in this workspace"}
        col, alias = self._signal_column(signal)
        if not col:
            return {"error": f"unknown signal '{signal}'"}
        v = _finite(col)
        q = f"SELECT count({v}) AS n, avg({v}) AS mean, stddev_samp({v}) AS std, min({v}) AS min, quantile_cont({v}, 0.05) AS q05, median({v}) AS median, quantile_cont({v}, 0.95) AS q95, max({v}) AS max, count(*) - count({v}) AS n_null, min(__rn) AS row_start, max(__rn) AS row_end FROM {self._base_sql()}"
        conds, params = [], []
        gcol = self._group_col()
        if group_id is not None and gcol:
            conds.append(f'CAST("{gcol}" AS VARCHAR) = ?')
            params.append(str(group_id))
        if row_start is not None:
            conds.append("__rn >= ?")
            params.append(int(row_start))
        if row_end is not None:
            conds.append("__rn <= ?")
            params.append(int(row_end))
        if conds:
            q += " WHERE " + " AND ".join(conds)
        row = self.ws.duckdb().execute(q, params).fetchone()
        keys = ["n", "mean", "std", "min", "q05", "median", "q95", "max", "n_null", "row_start", "row_end"]
        out = {k: _dump(v) for k, v in zip(keys, row)}
        if self.external:
            min_n = int(self.settings.guard.min_aggregate_n)
            if int(out.get("n") or 0) < min_n:
                return {"error": f"window too short to summarise: {int(out.get('n') or 0)} values, at least {min_n} are needed"}
            out = {k: out[k] for k in EXTERNAL_STATS_KEYS}
            out.update({"signal": alias, "group_id": group_id, "n_samples": out["n"]})
            return out
        out.update({"signal": alias or signal, "column": col, "group_id": group_id})
        return out

    def tool_series(self, signal: str = "", row_start: int = 0, row_end: Optional[int] = None, max_points: int = 400, **_: Any) -> dict[str, Any]:
        if self.ws is None or not self.ws.exists("dataset"):
            return {"error": "no dataset in this workspace"}
        col, alias = self._signal_column(signal)
        if not col:
            return {"error": f"unknown signal '{signal}'"}
        con = self.ws.duckdb()
        n_total = int(con.execute("SELECT count(*) FROM dataset").fetchone()[0])
        a = max(0, int(row_start or 0))
        b = int(row_end) if row_end is not None else min(n_total - 1, a + 2000)
        b = min(b, n_total - 1)
        if b < a:
            return {"error": "row_end < row_start"}
        max_points = max(10, min(int(max_points or 400), 2000))
        n = b - a + 1
        bucket = max(1, int(math.ceil(n / max_points)))
        q = f'SELECT min(__rn) AS row, avg("{col}") AS mean, min("{col}") AS lo, max("{col}") AS hi FROM {self._base_sql()} WHERE __rn BETWEEN ? AND ? GROUP BY (__rn - ?) // ? ORDER BY row'
        rows = con.execute(q, [a, b, a, bucket]).fetchall()
        pts = [[int(r[0]), _dump(r[1]), _dump(r[2]), _dump(r[3])] for r in rows]
        out = {"signal": alias or signal, "column": col, "row_start": a, "row_end": b, "n_raw": n, "bucket": bucket, "columns": ["row", "mean", "min", "max"], "points": pts, "local_only": True}
        self.last_series = out
        if self.external:
            return self._coarse_series(col, alias or signal, a, b)  # the full series above is for the UI only
        return {**out, "points": pts if len(pts) <= 60 else pts[:60], "note": f"{len(pts)} points computed; the UI receives the full series, the model sees the first 60"}

    def _coarse_series(self, col: str, alias: str, a: int, b: int) -> dict[str, Any]:
        """What an external model may see of a series: at most guard.max_series_points bucket means, each over at least
        guard.min_aggregate_n values, and the same means in standard deviations from the mean of the range (3 significant
        digits of a level such as 2700 would hide the shape)."""
        g = self.settings.guard
        min_n, max_points = max(1, int(g.min_aggregate_n)), max(1, int(g.max_series_points))
        n = b - a + 1
        bucket = max(min_n, int(math.ceil(n / max_points)))
        v = f"CAST({_finite(col)} AS DOUBLE)"
        q = f"SELECT min(__rn) AS row, avg({v}) AS mean, count({v}) AS n, sum({v}) AS s, sum({v} * {v}) AS ss FROM {self._base_sql()} WHERE __rn BETWEEN ? AND ? GROUP BY (__rn - ?) // ? ORDER BY row"
        rows = self.ws.duckdb().execute(q, [a, b, a, bucket]).fetchall()
        kept = [r for r in rows if int(r[2] or 0) >= min_n and r[1] is not None]
        out: dict[str, Any] = {"signal": alias, "row_start": a, "row_end": b, "n_rows": n, "bucket_rows": bucket}
        if not kept:
            return {**out, "note": f"window too short to summarise: a bucket mean needs at least {min_n} values"}
        out.update({"bucket_row_start": [int(r[0]) for r in kept], "bucket_mean": [_dump(float(r[1])) for r in kept]})
        # mean and spread of the whole range from the bucket sums: one pass over the rows instead of two
        n_all = sum(int(r[2] or 0) for r in rows)
        mean = sum(float(r[3] or 0.0) for r in rows) / n_all
        var = (sum(float(r[4] or 0.0) for r in rows) - n_all * mean * mean) / max(1, n_all - 1)
        if var > 1e-12 * max(1.0, mean * mean):  # a constant signal has no spread to scale by (only rounding noise)
            out["bucket_mean_in_std"] = [_dump((float(r[1]) - mean) / math.sqrt(var)) for r in kept]
        out["note"] = f"{len(kept)} bucket means of {bucket} rows each" + (f"; {len(rows) - len(kept)} bucket(s) with fewer than {min_n} values left out" if len(rows) > len(kept) else "")
        return out

    def tool_get_flag(self, id: str = "", **_: Any) -> dict[str, Any]:
        for f in self.ws.flags() if self.ws else []:
            if f.id == id:
                d = f.model_dump()
                d["evidence"] = self.evidence_statements(f.evidence_ids + [e for c in f.signals_ranked for e in c.evidence_ids])
                return d
        return {"error": f"flag '{id}' not found"}

    def tool_get_diagnosis(self, id: str = "", **_: Any) -> dict[str, Any]:
        for d in self.ws.diagnoses() if self.ws else []:
            if d.id == id:
                out = d.model_dump()
                out["evidence"] = self.evidence_statements(d.evidence_ids)
                return out
        return {"error": f"diagnosis '{id}' not found"}

    def tool_get_evidence(self, id: str = "", **_: Any) -> dict[str, Any]:
        e = self.ws.evidence.get(id) if self.ws else None
        return e.model_dump() if e else {"error": f"evidence '{id}' not found"}

    def tool_list_checks(self, batch_id: Optional[str] = None, signal: Optional[str] = None, **_: Any) -> dict[str, Any]:
        out = []
        for c in self.ws.checks() if self.ws else []:
            if batch_id and c.batch_id != batch_id:
                continue
            if signal and signal not in c.signals:
                continue
            out.append({"check_id": c.check_id, "check_type": c.check_type, "status": c.status, "severity": c.severity, "signals": c.signals, "batch_id": c.batch_id, "statement": c.statement, "evidence_ids": c.evidence_ids})
        return {"n": len(out), "checks": out[:50]}

    def tool_search(self, query: str = "", k: int = 6, **_: Any) -> dict[str, Any]:
        hits = self.index.search(query, k=int(k or 6))
        return {"method": self.index.method, "hits": hits}

    def tool_assessor_evaluate(self, action_text: str = "", **_: Any) -> dict[str, Any]:
        fn = _assessor_fn()
        if fn is None:
            return {"error": "assessor module not available (tpm.assessor.ask / evaluate_action missing)"}
        res = fn(self.ws, self.settings, action_text)
        return {"result": _dump(res)}


def _assessor_fn():
    try:
        import importlib

        mod = importlib.import_module("tpm.assessor")
    except Exception:
        return None
    for name in ("ask", "evaluate_action"):
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    return None


def safe_sql(query: str, limit: int = SQL_LIMIT) -> tuple[str, Optional[str]]:
    """Read-only, single-statement SELECT with an enforced LIMIT. Returns (sql, error)."""
    if not query or not isinstance(query, str):
        return "", "empty query"
    q = re.sub(r"--[^\n]*", " ", query)
    q = re.sub(r"/\*.*?\*/", " ", q, flags=re.S)
    q = q.strip().rstrip(";").strip()
    if ";" in q:
        return "", "only one statement is allowed"
    if not re.match(r"^(select|with)\b", q, re.I):
        return "", "only SELECT / WITH queries are allowed"
    m = _FORBIDDEN_SQL.search(q)
    if m:
        return "", f"forbidden keyword '{m.group(1)}' (read-only)"
    if _FILE_SQL.search(q):
        return "", "file access functions are not allowed; use the views dataset / scores"
    m = re.search(r"\blimit\s+(\d+)\s*$", q, re.I)
    if m and int(m.group(1)) <= limit:
        return q, None
    return f"SELECT * FROM ({q}) AS __q LIMIT {limit}", None


# ----------------------------------------------------------------------------------------------
# context loading
# ----------------------------------------------------------------------------------------------


def load_context(ws: Any, settings: Settings, context: Optional[dict[str, Any]], message: str, tb: Optional[Toolbox] = None) -> dict[str, Any]:
    """Objects the operator is looking at (from the UI click) plus IDs mentioned in the message."""
    tb = tb or Toolbox(ws, settings)
    ctx = dict(context or {})
    ids = set(_ID_RE.findall(message or ""))
    for i in ids:
        if i.startswith("FLAG-") and not ctx.get("flag_id"):
            ctx["flag_id"] = i
        elif i.startswith("DIAG-") and not ctx.get("diagnosis_id"):
            ctx["diagnosis_id"] = i
        elif re.match(r"^S\d{2,4}$", i) and not ctx.get("signal"):
            ctx["signal"] = i
    out: dict[str, Any] = {"context": ctx, "flag": None, "diagnosis": None, "signal": None, "trust": None, "checks": [], "evidence": [], "mentioned": []}
    if ws is None:
        return out
    if ctx.get("flag_id"):
        f = tb.tool_get_flag(ctx["flag_id"])
        if "error" not in f:
            out["flag"] = f
            if not ctx.get("diagnosis_id"):
                for d in ws.diagnoses():
                    if ctx["flag_id"] in d.flag_ids:
                        ctx["diagnosis_id"] = d.id
                        break
            if not ctx.get("batch_id") and f.get("batch_id"):
                ctx["batch_id"] = f["batch_id"]
    if ctx.get("diagnosis_id"):
        d = tb.tool_get_diagnosis(ctx["diagnosis_id"])
        if "error" not in d:
            out["diagnosis"] = d
            if out["flag"] is None and d.get("flag_ids"):
                f = tb.tool_get_flag(d["flag_ids"][0])
                if "error" not in f:
                    out["flag"] = f
                    ctx.setdefault("flag_id", f["id"])
                    ctx.setdefault("batch_id", f.get("batch_id"))
    if ctx.get("row") is not None:
        try:
            row = int(ctx["row"])
            susp = ws.read_json("suspicious_rows.json", None) or {}
            hit = next((r for r in (susp.get("rows") or []) if int(r.get("row", -9)) - 1 <= row <= int(r.get("row_end", r.get("row", -9))) + 1), None)
            if hit:
                out["suspicious_row"] = {**{k: hit.get(k) for k in ("row", "row_end", "group_id", "batch_id", "signals", "sources", "strength", "statement", "flag_ids", "check_ids", "evidence_ids")}, "wording": susp.get("wording")}
                if not ctx.get("flag_id") and hit.get("flag_ids"):
                    ctx["flag_id"] = hit["flag_ids"][0]
                    f = tb.tool_get_flag(ctx["flag_id"])
                    if "error" not in f:
                        out["flag"] = f
                if not ctx.get("signal") and hit.get("signals"):
                    ctx["signal"] = hit["signals"][0].get("signal")
                ctx.setdefault("batch_id", hit.get("batch_id"))
        except Exception:
            pass
    if ctx.get("signal"):
        s = tb.tool_describe_signal(ctx["signal"])
        if "error" not in s:
            out["signal"] = s
    if ctx.get("batch_id"):
        try:
            out["trust"] = next((t.model_dump() for t in ws.trust() if t.batch_id == ctx["batch_id"]), None)
        except Exception:
            out["trust"] = None
        out["checks"] = tb.tool_list_checks(batch_id=ctx["batch_id"]).get("checks", [])[:10]
    for i in ids:
        if i.startswith("EV-"):
            e = tb.tool_get_evidence(i)
            if "error" not in e:
                out["mentioned"].append(e)
        elif i.startswith("CHK-"):
            for c in ws.checks():
                if c.check_id == i:
                    out["mentioned"].append(c.model_dump())
    ev_ids: list[str] = []
    for obj in (out["flag"], out["diagnosis"], out["signal"]):
        if obj:
            ev_ids += [e["id"] for e in obj.get("evidence", [])]
    for c in out["checks"]:
        ev_ids += c.get("evidence_ids", [])
    if out.get("suspicious_row"):
        ev_ids = list(out["suspicious_row"].get("evidence_ids") or []) + ev_ids
    out["evidence"] = tb.evidence_statements(ev_ids, limit=20)
    return out


def _context_compact(loaded: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    if loaded.get("flag"):
        f = loaded["flag"]
        compact["flag"] = {k: f.get(k) for k in ("id", "kind", "batch_id", "group_id", "row_start", "row_end", "severity", "score", "threshold", "detector", "statement", "likely_cause_class", "confidence", "pattern_id", "trust_context", "human_status")}
        compact["flag"]["signals_ranked"] = [{"signal": c.get("signal"), "contribution": c.get("contribution"), "direction": c.get("direction"), "lag": c.get("lag")} for c in f.get("signals_ranked", [])[:6]]
    if loaded.get("diagnosis"):
        d = loaded["diagnosis"]
        compact["diagnosis"] = {k: d.get(k) for k in ("id", "flag_ids", "fault_type", "cause_class", "steps", "summary", "confidence", "uncertainty", "assumptions", "propagation", "critique")}
    if loaded.get("signal"):
        s = loaded["signal"]
        compact["signal"] = {k: s.get(k) for k in ("id", "structural_role", "structural_confidence", "instrument_hypothesis", "instrument_confidence", "unit_operation_hypothesis", "cluster_id", "related_signals", "fingerprint", "excluded")}
    if loaded.get("suspicious_row"):
        compact["suspicious_row"] = loaded["suspicious_row"]
    if loaded.get("trust"):
        compact["trust"] = loaded["trust"]
    if loaded.get("checks"):
        compact["checks"] = [{k: c.get(k) for k in ("check_id", "status", "severity", "signals", "statement")} for c in loaded["checks"]]
    if loaded.get("evidence"):
        compact["evidence"] = loaded["evidence"]
    if loaded.get("mentioned"):
        compact["mentioned"] = loaded["mentioned"]
    return compact


def _context_text(loaded: dict[str, Any]) -> str:
    text = json.dumps(_context_compact(loaded), ensure_ascii=False, default=str)
    return text[:CONTEXT_CHARS]


# ----------------------------------------------------------------------------------------------
# external mode: what goes into a message passes the egress guard first; names come back locally
# ----------------------------------------------------------------------------------------------


def _guarded(key: str, obj: Any, ws: Any, settings: Settings) -> tuple[Any, Optional[str]]:
    """One structured object (the loaded context, a tool result) as it may be shown to the external model: the guard's
    sanitised version and None, or ({"withheld": true, "reason"}, reason) when the guard refuses it, so that the model can
    continue without it. The reason is the guard's own text: it names paths and rules, never values."""
    try:
        g = guard_mod.check({key: _dump(obj)}, settings, ws=ws)
        if g.allowed and key in g.sanitized_payload:
            return g.sanitized_payload[key], None
        reason = guard_mod.sanitize_text(g.reason, settings, ws=ws)[0][:300]
    except Exception:
        reason = "the egress guard could not check this result"
    return {"withheld": True, "reason": reason}, reason


def _external_sent(ws: Any) -> int:
    """Calls of this run that went out to the external model (answered or failed), from the egress ledger."""
    if ws is None:
        return 0
    try:
        return len([r for r in ledger_mod.read(ws) if r.route == "external" and r.guard_result == "allowed"])
    except Exception:
        return 0


def alias_labels(tb: Toolbox) -> dict[str, str]:
    """alias -> the name the operator knows the signal by: the display name they gave it, else the dataset's column
    name (only when the file had a header)."""
    sch = tb.schema
    named = bool(sch.had_header) if sch else True
    out: dict[str, str] = {}
    if sch and named:
        out = {alias: orig for orig, alias in sch.signal_alias.items() if orig and orig != alias}
    for s in tb.signals.values():
        if s.display_name:
            out[s.id] = str(s.display_name)
        elif named and s.source_column and s.source_column != s.id:
            out.setdefault(s.id, s.source_column)
    return out


def expand_aliases(text: str, labels: dict[str, str]) -> str:
    """First mention of each alias gets its local name: "S05" -> "S05 (press_r)". Done on this machine after the external
    model has answered; the names were never sent."""
    seen: set[str] = set()

    def repl(m: re.Match) -> str:
        alias = m.group(0)
        label = labels.get(alias)
        if not label or alias in seen:
            return alias
        seen.add(alias)
        return alias if text[m.end():].startswith(f" ({label})") else f"{alias} ({label})"

    return _ALIAS_TOKEN_RE.sub(repl, text or "")


def _collapse_aliases(text: str, labels: dict[str, str]) -> str:
    """Undo expand_aliases in an earlier answer before it goes back to the external model as history."""
    for alias, label in labels.items():
        text = text.replace(f"{alias} ({label})", alias)
    return text


# ----------------------------------------------------------------------------------------------
# deterministic answer (no model) and followups
# ----------------------------------------------------------------------------------------------


def _fmt(v: Any, nd: int = 3) -> str:
    try:
        if isinstance(v, bool) or v is None:
            return str(v)
        if isinstance(v, (int,)):
            return str(v)
        return f"{float(v):.{nd}g}"
    except Exception:
        return str(v)


def deterministic_answer(ws: Any, settings: Settings, message: str, loaded: dict[str, Any], language: str = "en", tb: Optional[Toolbox] = None, task: str = "why_chat") -> dict[str, Any]:
    t = _t(language)
    tb = tb or Toolbox(ws, settings)
    lines: list[str] = []
    cites: list[str] = []
    found = False
    if task == "assessor_chat":
        res = tb.tool_assessor_evaluate(message)
        lines.append(f"{t['assessor']}:")
        if "error" in res:
            lines.append(t["assessor_missing"])
        else:
            lines.append(json.dumps(res.get("result"), ensure_ascii=False, default=str)[:1500])
            found = True
    sr = loaded.get("suspicious_row")
    if sr:
        found = True
        lines.append(str(sr.get("statement") or ""))
        if sr.get("wording"):
            lines.append(str(sr["wording"]))
        lines.append("What to check: compare this row with maintenance, calibration and operator records, and with the readings just before and after it; if the same signal keeps appearing on this list, inspect that instrument and its transmission.")
        cites += list(sr.get("evidence_ids") or [])[:6]
    f = loaded.get("flag")
    if f:
        found = True
        lines.append(f"{t['flag']} {f['id']} ({f.get('kind')}, {t['severity']} {_fmt(f.get('severity'))}, {t['confidence']} {_fmt(f.get('confidence'))}, {f.get('detector')}): {f.get('statement')}")
        if f.get("signals_ranked"):
            lines.append(f"{t['top_signals']}: " + ", ".join(f"{c.get('signal')} ({_fmt(c.get('contribution'), 2)}{', ' + str(c.get('direction')) if c.get('direction') else ''}{', lag ' + str(c.get('lag')) if c.get('lag') else ''})" for c in f["signals_ranked"][:5]))
        if f.get("trust_context"):
            lines.append(f"{t['trust']}: {json.dumps(f['trust_context'], ensure_ascii=False)}")
        cites += [e["id"] for e in f.get("evidence", [])]
    d = loaded.get("diagnosis")
    if d:
        found = True
        lines.append(f"{t['diagnosis']} {d['id']} ({d.get('cause_class')}, {t['confidence']} {_fmt(d.get('confidence'))}): {d.get('summary')}")
        if d.get("steps"):
            lines.append(f"{t['steps']}:")
            lines += [f"  {s}" for s in d["steps"][:7]]
        if d.get("uncertainty"):
            lines.append(f"{t['uncertainty']}: " + "; ".join(d["uncertainty"][:5]))
        if d.get("critique") and isinstance(d["critique"], dict):
            lines.append(f"Critique: {d['critique'].get('verdict')} " + "; ".join(str(o) for o in (d['critique'].get('objections') or [])[:3]))
        cites += [e["id"] for e in d.get("evidence", [])]
    s = loaded.get("signal")
    if s:
        found = True
        fp = s.get("fingerprint") or {}
        lines.append(f"{t['signal']} {s['id']}: {s.get('structural_role')} ({t['confidence']} {_fmt(s.get('structural_confidence'))})" + (f", {s.get('instrument_hypothesis')} ({_fmt(s.get('instrument_confidence'))})" if s.get("instrument_hypothesis") else "") + (f"; mean {_fmt(fp.get('mean'))}, std {_fmt(fp.get('std'))}, range [{_fmt(fp.get('min'))}, {_fmt(fp.get('max'))}]" if fp.get("mean") is not None else "") + (f"; related: {', '.join(r.get('signal', '') + ' r=' + _fmt(r.get('r'), 2) for r in s.get('related_signals', [])[:3])}" if s.get("related_signals") else ""))
        cites += [e["id"] for e in s.get("evidence", [])]
    if loaded.get("trust") and not f:
        tr = loaded["trust"]
        found = True
        lines.append(f"{t['trust']} {tr.get('batch_id')}: {tr.get('statement') or ('trusted' if tr.get('trusted') else 'untrusted')} ({_fmt(tr.get('trust_score'))})")
        cites += tr.get("check_ids", [])
    if loaded.get("checks") and (not f or any(c.get("status") != "pass" for c in loaded["checks"])):
        lines.append(f"{t['checks']}: " + "; ".join(f"{c['check_id']} {c.get('status')}: {c.get('statement')}" for c in loaded["checks"][:5]))
        cites += [c["check_id"] for c in loaded["checks"][:5]]
    for m in loaded.get("mentioned", [])[:4]:
        found = True
        lines.append(f"{m.get('id') or m.get('check_id')}: {m.get('statement')}")
        cites.append(m.get("id") or m.get("check_id"))
    ev = loaded.get("evidence", [])
    if ev:
        lines.append(f"{t['evidence']}:")
        lines += [f"  [{e['id']}] {e['statement']}" + (f" (n={e['n_samples']})" if e.get("n_samples") else "") for e in ev[:8]]
    # keyword retrieval for the question itself
    hits = []
    try:
        hits = tb.index.search(message, k=4, types=["evidence", "check", "diagnosis", "flag", "inference", "rule", "pattern"])
    except Exception:
        hits = []
    hits = [h for h in hits if h["id"] not in cites]
    if not found:
        n_flags = len(ws.flags()) if ws else 0
        n_diag = len(ws.diagnoses()) if ws else 0
        n_chk = len(ws.checks()) if ws else 0
        n_sig = len(ws.signals()) if ws else 0
        lines.insert(0, t["nothing"])
        lines.append(f"{t['contains']}: {n_flags} {t['flags']}, {n_diag} {t['diagnoses']}, {n_chk} {t['checks_n']}, {n_sig} {t['signals_n']}.")
    if hits:
        lines.append(f"{t['related']}:")
        lines += [f"  [{h['id']}] {h['text'][:200]}" for h in hits[:4]]
        cites += [h["id"] for h in hits[:4]]
    lines.append(t["no_model"])
    return {"answer": "\n".join(lines), "citations": list(dict.fromkeys(c for c in cites if c)), "confidence": 0.5 if found else 0.2}


def suggest_followups(loaded: dict[str, Any], language: str = "en") -> list[str]:
    ctx = loaded.get("context", {})
    f, d, s = loaded.get("flag"), loaded.get("diagnosis"), loaded.get("signal")
    out: list[str] = []
    lang = (language or "en").lower()
    if f:
        top = (f.get("signals_ranked") or [{}])[0].get("signal")
        out.append({"fi": f"Mitkä todisteet tukevat hälytystä {f['id']}?", "sv": f"Vilka bevis stöder flaggan {f['id']}?"}.get(lang, f"What evidence supports {f['id']}?"))
        if top:
            out.append({"fi": f"Näytä {top} rivien {f.get('row_start')}-{f.get('row_end')} ympärillä", "sv": f"Visa {top} runt raderna {f.get('row_start')}-{f.get('row_end')}"}.get(lang, f"Show {top} around rows {f.get('row_start')}-{f.get('row_end')}"))
        out.append({"fi": f"Oliko erä {f.get('batch_id')} luotettava?", "sv": f"Var batchen {f.get('batch_id')} tillförlitlig?"}.get(lang, f"Was batch {f.get('batch_id')} trusted?"))
    if d:
        out.append({"fi": f"Voisiko {d['id']} olla anturivika prosessivian sijaan?", "sv": f"Kan {d['id']} vara ett sensorfel i stället för ett processfel?"}.get(lang, f"Could {d['id']} be a sensor fault instead of a process fault?"))
    if s:
        out.append({"fi": f"Mihin signaaleihin {s['id']} korreloi?", "sv": f"Vilka signaler korrelerar {s['id']} med?"}.get(lang, f"Which signals does {s['id']} correlate with?"))
    if not out:
        out = {"fi": ["Mitkä hälytykset ovat vakavimpia?", "Mitkä signaalit ovat epäluotettavia?", "Mitä oletuksia ajossa tehtiin?"], "sv": ["Vilka flaggor är allvarligast?", "Vilka signaler är opålitliga?", "Vilka antaganden gjordes i körningen?"]}.get(lang, ["Which flags are most severe?", "Which signals are untrusted?", "What assumptions were made in this run?"])
    return out[:4]


# ----------------------------------------------------------------------------------------------
# the JSON-action loop
# ----------------------------------------------------------------------------------------------


_PLACEHOLDER_RE = re.compile(r"^\s*(<[^>]*>|answer(\s+for)?\s+the\s+(question|operator)|your\s+answer\s*(here)?|final\s+answer|answer|n/?a|todo|\.\.\.)\s*[.!]?\s*$", re.I)

# ----------------------------------------------------------------------------------------------
# stop flags: the UI's "Stop" button ends a turn between agent steps so the local model is not kept busy
# ----------------------------------------------------------------------------------------------
_STOP_LOCK = threading.Lock()
_STOP: dict[str, threading.Event] = {}          # turn_id -> set when a stop was requested
_ACTIVE: dict[str, str] = {}                    # turn_id -> chat_id of the turns running right now
_TURN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
DEFAULT_CHAT_ID = "default"


def stop_event(turn_id: str) -> threading.Event:
    """The stop flag of a turn (created on first use, so a stop that arrives before the turn started still counts)."""
    with _STOP_LOCK:
        ev = _STOP.get(turn_id)
        if ev is None:
            if len(_STOP) > 500:                                      # forgotten flags of turns that never ran
                for k in list(_STOP)[:250]:
                    if k not in _ACTIVE:
                        _STOP.pop(k, None)
            ev = _STOP[turn_id] = threading.Event()
        return ev


def request_stop(turn_id: Optional[str] = None, chat_id: Optional[str] = None) -> list[str]:
    """Ask a running turn (by id) or every running turn of a chat to stop. Returns the ids that were flagged."""
    flagged: list[str] = []
    if turn_id:
        stop_event(str(turn_id)).set()
        flagged.append(str(turn_id))
    if chat_id:
        with _STOP_LOCK:
            ids = [t for t, c in _ACTIVE.items() if c == chat_id]
        for t in ids:
            stop_event(t).set()
            if t not in flagged:
                flagged.append(t)
    return flagged


def is_stopped(turn_id: Optional[str]) -> bool:
    if not turn_id:
        return False
    with _STOP_LOCK:
        ev = _STOP.get(turn_id)
    return bool(ev and ev.is_set())


def _turn_begin(turn_id: str, chat_id: str) -> None:
    with _STOP_LOCK:
        _ACTIVE[turn_id] = chat_id


def _turn_end(turn_id: str) -> None:
    with _STOP_LOCK:
        _ACTIVE.pop(turn_id, None)
        _STOP.pop(turn_id, None)


def clean_turn_id(turn_id: Any) -> Optional[str]:
    """A client-chosen turn id is used as a dictionary key and persisted: letters, digits, '_', '-', '.', ':' only."""
    s = str(turn_id or "").strip()
    return s if s and _TURN_ID_RE.match(s) else None


def clean_chat_id(chat_id: Any) -> str:
    s = str(chat_id or "").strip()
    return s if s and _TURN_ID_RE.match(s) else DEFAULT_CHAT_ID


def _is_placeholder_answer(answer: str) -> bool:
    """True for schema echoes like 'answer the question' or '<answer>' and for answers too short to be useful."""
    a = (answer or "").strip()
    return len(a) < 8 or bool(_PLACEHOLDER_RE.match(a))


def run_agent(ws: Any, settings: Settings, message: str, loaded: dict[str, Any], tb: Toolbox, *, task: str = "why_chat", purpose: str = "operator chat", language: str = "en", history: Optional[list[dict[str, str]]] = None, max_steps: Optional[int] = None, stop: Optional[Callable[[], bool]] = None) -> Optional[dict[str, Any]]:
    """Returns {"answer","citations","confidence","suggested_followups","tool_trace","model","external_calls"} or None if
    the local model is unavailable / never produced a final answer. The mode comes from the toolbox: with tb.external the
    turn runs on the external model; a turn that model could not finish comes back with "incomplete" and
    "external_error", and chat() restarts it on the local toolbox. ``stop()`` is asked before every model call and
    after every tool call; when it says True the turn ends at once with "stopped" (the UI's Stop button)."""
    external = bool(tb.external)
    stopped = lambda: bool(stop and stop())
    max_steps = int(max_steps if max_steps is not None else settings.local_llm.max_tool_steps)
    common = dict(language=(language or "en").lower(), language_name=prompts_mod.language_name(language), has_schema=False, schema_json="null", task=task)
    parts: list[dict[str, Any]] = []  # external mode: the sanitised objects the messages are built from (agent_chat checks them again)
    labels: dict[str, str] = {}
    call_cap = 0
    if external:
        call_cap = max(1, int(settings.external_llm.max_calls_per_chat_turn))
        max_steps = min(max_steps, call_cap - 1)  # the last external call of a turn has to be the answer
        labels = alias_labels(tb)
        context_text = ""
        compact = _context_compact(loaded)
        if compact:
            safe, _ = _guarded("context", compact, ws, settings)
            parts.append({"context": safe})
            context_text = json.dumps(safe, ensure_ascii=False, default=str)[:CONTEXT_CHARS]
        system = prompts_mod.render_template("agent.system.external.j2", tools=tool_specs(settings, True), max_steps=max_steps, sig_digits=int(settings.guard.external_sig_digits), context_text=context_text, **common)
    else:
        system = prompts_mod.render_template("agent.system.j2", tools=TOOL_SPECS, max_steps=max_steps, dataset_columns=", ".join(tb.dataset_columns()[:60]) or "(no dataset)", sql_limit=SQL_LIMIT, context_text=_context_text(loaded), **common)
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for turn in (history or [])[-6:]:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        content = str(turn.get("content", ""))
        messages.append({"role": role, "content": (_collapse_aliases(content, labels) if external else content)[:2000]})
    messages.append({"role": "user", "content": f"Question: {message}\nRespond with one JSON object (tool call or final)."})
    trace: list[dict[str, Any]] = []
    seen: set[str] = set()
    model = ""
    steps_used = 0
    placeholder_retried = False
    ext_calls = 0
    ext_error: Optional[str] = None
    mark = {"route": "external"} if external else {}
    sent_before = _external_sent(ws) if external else 0

    def ext_used() -> int:
        """External calls of this turn: the agent's own plus the ones a tool made through the router (assessor)."""
        return max(ext_calls, _external_sent(ws) - sent_before) if external else 0

    was_stopped = False
    for step in range(max_steps + 3):
        if stopped():                                                   # before each model call
            was_stopped = True
            break
        if external:
            # agent_chat answers a call it may not send on the local model, with these external-mode messages: ask first,
            # and treat anything that was not sent as the end of the external turn
            ready, why = (False, f"external call cap of one chat turn reached ({call_cap}; external_llm.max_calls_per_chat_turn)") if ext_used() >= call_cap else router_mod.external_ready(task, ws, settings)
            if not ready:
                ext_error = why
                break
            res = router_mod.agent_chat(messages, task=task, purpose=purpose, ws=ws, settings=settings, schema=prompts_mod.AGENT_STEP_SCHEMA, max_tokens=900, payload_parts=parts, artifact_types=["chat", "context", "tool_result"])
            if res.route != "external":
                ext_error = "the call was stopped before sending (see the egress ledger)"
                break
            ext_calls += 1
            if not res.ok or not isinstance(res.data, dict):
                ext_error = res.error or "no JSON from the external model"
                break
        else:
            res = router_mod.local_chat(messages, task=task, purpose=purpose, ws=ws, settings=settings, schema=prompts_mod.AGENT_STEP_SCHEMA, max_tokens=900, artifact_types=["chat", "tool_results"])
            if not res.ok or not isinstance(res.data, dict):
                trace.append({"step": step, "tool": None, "ok": False, "error": res.error or "no JSON from model"})
                if res.route == "none":
                    return None
                break
        model = res.model or model
        data = res.data
        if stopped():                                                   # the model answered after the person gave up
            was_stopped = True
            break
        action = str(data.get("action") or "final").strip()
        if action == "final" or not action:
            answer = str(data.get("answer") or data.get("thought") or "").strip()
            if not answer:
                break
            if _is_placeholder_answer(answer) and not placeholder_retried:
                # Small models sometimes echo the schema ("answer the question"). One nudge, then fall through.
                placeholder_retried = True
                messages.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False)})
                messages.append({"role": "user", "content": "That was a placeholder, not an answer. Write the actual answer for the operator in at least two full sentences, in plain language, citing evidence IDs. Respond with action \"final\"."})
                continue
            return {"answer": answer, "citations": [str(c) for c in (data.get("citations") or []) if c], "confidence": data.get("confidence"), "suggested_followups": [str(x) for x in (data.get("suggested_followups") or [])][:4], "tool_trace": trace, "model": model, "external": external, "external_calls": ext_used()}
        if steps_used >= max_steps:
            messages.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False)})
            messages.append({"role": "user", "content": "No tool calls left. Respond now with action \"final\" and your best answer with citations."})
            continue
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        key = action + json.dumps(args, sort_keys=True, default=str)
        t0 = time.time()
        if key in seen:
            result = {"error": "identical call already made; use its result or finish"}
        else:
            seen.add(key)
            result = tb.call(action, args)
        steps_used += 1
        ok = "error" not in result
        withheld: Optional[str] = None
        if external:
            result, withheld = _guarded("tool_result", result, ws, settings)
            parts.append({"tool_result": result})
        ms = int((time.time() - t0) * 1000)
        result_text = json.dumps(result, ensure_ascii=False, default=str)
        trace.append({"step": step, "tool": action, "args": args, "ok": ok, "ms": ms, "summary": result_text[:300], "thought": str(data.get("thought", ""))[:300], **mark, **({"withheld": True} if withheld else {})})
        try:
            if ws is not None:
                ws.log.record("system:llm.agent", "tool_call", "chat_tool", action, {"args": args, "ok": ok, "ms": ms, "result_chars": len(result_text), **mark, **({"withheld": withheld} if withheld else {})})
        except Exception:
            pass
        left = min(max_steps - steps_used, call_cap - ext_used() - 1) if external else max_steps - steps_used
        messages.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False)})
        messages.append({"role": "user", "content": f"Tool result for {action}: {result_text[:TOOL_RESULT_CHARS]}\n({left} tool calls left.) " + ("Respond now with action \"final\" and your best answer with citations." if external and left <= 0 else "Continue with another tool call or finish with action \"final\".")})
    out = {"answer": "", "citations": [], "confidence": None, "suggested_followups": [], "tool_trace": trace, "model": model, "incomplete": True, "external": external, "external_calls": ext_used()}
    if was_stopped:
        out["stopped"] = True
        trace.append({"step": len(trace), "tool": None, "ok": False, "error": "stopped by the operator", **mark})
        return out
    if external:
        out["external_error"] = ext_error or "the external model did not reach a final answer"
        trace.append({"step": len(trace), "tool": None, "ok": False, "error": out["external_error"], **mark})
    return out


# ----------------------------------------------------------------------------------------------
# public entry point
# ----------------------------------------------------------------------------------------------


def chat(ws: Any, settings: Optional[Settings] = None, message: str = "", context: Optional[dict[str, Any]] = None, history: Optional[list[dict[str, str]]] = None, actor: str = "human", *, task: str = "why_chat", language: str = "en", max_steps: Optional[int] = None, use_model: bool = True, chat_id: Optional[str] = None, client_turn_id: Optional[str] = None) -> dict[str, Any]:
    """Operator chat entry point (why chat and assessor chat). Always returns an answer.

    ``chat_id`` tags both persisted turns (one conversation of the drawer; old turns count as "default").
    ``client_turn_id`` lets the UI name the turn before it starts, so its Stop button can flag it through
    ``request_stop`` while the model is still working; a stopped turn is persisted with status "stopped"."""
    settings = settings or get_settings()
    t_start = time.time()
    turn_id = clean_turn_id(client_turn_id) or ("CHAT-" + uuid.uuid4().hex[:10])
    chat_id = clean_chat_id(chat_id)
    actor_str = actor if ":" in (actor or "") else f"human:{actor or 'operator'}(operator)"
    tb = Toolbox(ws, settings)
    loaded = load_context(ws, settings, context, message, tb)
    _persist(ws, {"turn_id": turn_id, "chat_id": chat_id, "ts": now_iso(), "role": "user", "actor": actor_str, "content": message, "context": loaded.get("context"), "task": task})
    _turn_begin(turn_id, chat_id)
    try:
        return _chat_turn(ws, settings, message, history, actor_str, task, language, max_steps, use_model, chat_id, turn_id, t_start, tb, loaded)
    finally:
        _turn_end(turn_id)


def _chat_turn(ws: Any, settings: Settings, message: str, history: Optional[list[dict[str, str]]], actor_str: str, task: str, language: str, max_steps: Optional[int], use_model: bool, chat_id: str, turn_id: str, t_start: float, tb: Toolbox, loaded: dict[str, Any]) -> dict[str, Any]:
    result: Optional[dict[str, Any]] = None
    source = "template"
    route = "none"
    external_calls = 0
    ext_trace: list[dict[str, Any]] = []
    etb: Optional[Toolbox] = None
    stop = lambda: is_stopped(turn_id)

    def attempt(toolbox: Toolbox) -> Optional[dict[str, Any]]:
        try:
            return run_agent(ws, settings, message, loaded, toolbox, task=task, purpose=f"{task}: {message[:80]}", language=language, history=history, max_steps=max_steps, stop=stop)
        except Exception as e:
            return {"answer": "", "citations": [], "tool_trace": [{"error": str(e)}], "model": "", "incomplete": True}

    if use_model and settings.route_for(task) in ("local", "external"):
        # external or local is decided once per turn; an external turn that does not end in an answer (provider error,
        # budget, call cap) is restarted on the local agent, and only then answered by code
        if _external_turn(task, ws, settings):
            etb = tb.external_view()
            result = attempt(etb)
            external_calls = int((result or {}).get("external_calls") or 0)
            if not (result and result.get("answer") and not result.get("incomplete")) and not (result or {}).get("stopped"):
                ext_trace = list((result or {}).get("tool_trace", []))
                result = attempt(tb)
        else:
            result = attempt(tb)
    trace = ext_trace + list((result or {}).get("tool_trace", []))
    if (result and result.get("stopped")) or stop():
        # the person pressed Stop: no answer is composed, the turn is recorded as stopped
        out = {"turn_id": turn_id, "chat_id": chat_id, "answer": "", "citations": [], "confidence": None, "tool_trace": trace, "source": "stopped", "route": route,
               "external_calls": external_calls, "suggested_followups": [], "series": None, "context": loaded.get("context"), "stopped": True, "status": "stopped",
               "latency_ms": int((time.time() - t_start) * 1000)}
        _persist(ws, {"turn_id": turn_id, "chat_id": chat_id, "ts": now_iso(), "role": "assistant", "actor": "stopped", "content": "", "citations": [], "source": "stopped", "route": route, "status": "stopped", "stopped": True, "external_calls": external_calls, "tool_trace": trace, "task": task, "latency_ms": out["latency_ms"]})
        try:
            if ws is not None:
                ws.log.record(actor_str, "chat_stopped", "chat", turn_id, {"task": task, "chat_id": chat_id, "question": message[:500], "n_tools": len(trace)})
        except Exception:
            pass
        return out
    if result and result.get("answer") and not result.get("incomplete"):
        if result.get("external"):
            route, source = "external", f"llm-external:{result.get('model') or settings.external_model_for(task)}"
            answer = expand_aliases(result["answer"], alias_labels(tb))  # names come back here; they were never sent
        else:
            route, source = "local", f"llm-local:{result.get('model') or settings.local_llm.model}"
            answer = result["answer"]
        citations = _known_ids(ws, result.get("citations", []), loaded)
        followups = result.get("suggested_followups") or suggest_followups(loaded, language)
        confidence = result.get("confidence")
    else:
        det = deterministic_answer(ws, settings, message, loaded, language, tb, task=task)
        answer = det["answer"]
        citations = det["citations"]
        followups = suggest_followups(loaded, language)
        confidence = det["confidence"]
        if result and result.get("incomplete") and trace:
            answer = answer + "\n(The local model ran tools but did not finish; the summary above was composed by code.)"
        elif ext_trace:
            answer = answer + "\n(The external model did not finish this turn; the summary above was composed by code.)"
    series = tb.last_series or (etb.last_series if etb is not None else None)
    if series is None and loaded.get("flag") and ws is not None and ws.exists("dataset"):
        f = loaded["flag"]
        top = (f.get("signals_ranked") or [{}])[0].get("signal")
        if top:
            try:
                span = max(20, int(f["row_end"]) - int(f["row_start"]) + 1)
                pad = min(200, span // 2)
                s = tb.tool_series(top, max(0, int(f["row_start"]) - pad), int(f["row_end"]) + pad, 400)
                series = tb.last_series if "error" not in s else None
            except Exception:
                series = None
    out = {
        "turn_id": turn_id,
        "chat_id": chat_id,
        "answer": answer,
        "citations": citations,
        "confidence": confidence,
        "tool_trace": trace,
        "source": source,
        "route": route,
        "external_calls": external_calls,
        "suggested_followups": followups,
        "series": series,
        "context": loaded.get("context"),
        "latency_ms": int((time.time() - t_start) * 1000),
    }
    _persist(ws, {"turn_id": turn_id, "chat_id": chat_id, "ts": now_iso(), "role": "assistant", "actor": source, "content": answer, "citations": citations, "source": source, "route": route, "external_calls": external_calls, "tool_trace": trace, "task": task, "latency_ms": out["latency_ms"]})
    try:
        if ws is not None:
            ws.log.record(actor_str, "chat", "chat", turn_id, {"task": task, "chat_id": chat_id, "question": message[:500], "source": source, "route": route, "external_calls": external_calls, "n_tools": len(trace), "context": loaded.get("context")}, evidence_ids=[c for c in citations if c.startswith("EV-")])
    except Exception:
        pass
    return out


def _external_turn(task: str, ws: Any, settings: Settings) -> bool:
    """Does this turn run on the external model? The task must route external and the route must be usable right now
    (profile, allowed model, endpoint, API key, run budget). no-egress never gets past the first test."""
    if settings.route_for(task) != "external":
        return False
    try:
        return bool(router_mod.external_ready(task, ws, settings)[0])
    except Exception:
        return False


def _known_ids(ws: Any, ids: list[str], loaded: dict[str, Any]) -> list[str]:
    known: set[str] = set()
    if ws is not None:
        try:
            known |= {e.id for e in ws.evidence.all()}
            known |= {c.check_id for c in ws.checks()}
            known |= {f.id for f in ws.flags()}
            known |= {d.id for d in ws.diagnoses()}
        except Exception:
            pass
    out = [i for i in dict.fromkeys(ids) if i in known]
    if not out:
        out = [e["id"] for e in loaded.get("evidence", [])][:5]
    return out


def _persist(ws: Any, turn: dict[str, Any]) -> None:
    if ws is None:
        return
    try:
        turn.setdefault("chat_id", DEFAULT_CHAT_ID)
        ws.append_jsonl("chat", turn)
    except Exception:
        pass


def turn_chat_id(turn: dict[str, Any]) -> str:
    """The conversation a persisted turn belongs to; turns written before chats existed count as "default"."""
    return str(turn.get("chat_id") or DEFAULT_CHAT_ID)


def chat_history(ws: Any, limit: int = 50, chat_id: Optional[str] = None) -> list[dict[str, Any]]:
    """The last ``limit`` persisted turns, of one chat when ``chat_id`` is given."""
    if ws is None:
        return []
    try:
        turns = ws.read_jsonl("chat")
        if chat_id:
            turns = [t for t in turns if turn_chat_id(t) == chat_id]
        return turns[-limit:]
    except Exception:
        return []


def clear_chat(ws: Any, chat_id: str) -> int:
    """Remove the persisted turns of one chat (the UI's "Clear history" / "Delete chat"). Returns how many were removed."""
    if ws is None or not ws.exists("chat"):
        return 0
    turns = ws.read_jsonl("chat")
    keep = [t for t in turns if turn_chat_id(t) != chat_id]
    if len(keep) != len(turns):
        ws.rewrite_jsonl("chat", keep)
    return len(turns) - len(keep)
