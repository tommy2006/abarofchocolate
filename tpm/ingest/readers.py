"""Format detection and streaming conversion of any supported source into ``dataset.parquet``.

Supported: delimited text (CSV/TSV/;/|), whitespace-delimited ``.dat``/``.txt`` (with or without header,
transposed detection for headerless numeric matrices), Parquet, Excel (.xlsx/.xlsm/.xls via openpyxl),
JSON (array of records) and JSONL. Everything row-shaped goes through DuckDB (out-of-core) so 15+ GB files
convert with bounded memory; Excel and transposed matrices go through bounded chunks.

Invariant on every path: a floating-point column of ``dataset.parquet`` holds finite values or NULL. Missing-value
tokens (NAN_TOKENS) and non-finite values (NaN, +/-inf) are stored as NULL, never as IEEE NaN/inf, which DuckDB
aggregates would treat as ordinary values (``stddev_samp`` raises on them).

Public functions
    detect_format(path, options=None) -> dict          sniffed format + options + evidence statements
    convert_to_parquet(path, fmt, out_path, settings, progress=None, con=None) -> dict
    dataframe_to_parquet(df, out_path, settings) -> dict   small-data path (API uploads / batches)
    read_small(path, fmt=None, max_rows=None) -> pandas.DataFrame   bounded read for stream/watch-folder batches
    quote_ident(name) -> str                           DuckDB identifier quoting
"""
from __future__ import annotations

import csv
import io
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from ..memory import budget_bytes, chunk_rows as mem_chunk_rows

NAN_TOKENS = ["", "NA", "N/A", "n/a", "NaN", "nan", "NULL", "null", "None", "none", "-", "?", "#N/A", "#NA"]
_NAN_SET = set(NAN_TOKENS)
CANDIDATE_DELIMITERS = [",", "\t", ";", "|", "whitespace"]
COMMENT_PREFIXES = ("#", "%", "//")

_NUM_DOT = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_NUM_COMMA = re.compile(r"^[+-]?(\d+,\d*|,\d+)([eE][+-]?\d+)?$")
_INT = re.compile(r"^[+-]?\d+$")
_DATE_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$|^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}")

ProgressFn = Optional[Callable[[float, str], None]]


# --------------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------------
def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def sql_lit(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def finite_sql(expr: str) -> str:
    """NULL where a floating-point expression is NaN or +/-inf. dataset.parquet holds finite values or NULL only:
    DuckDB treats NaN/inf as ordinary values and stddev_samp / var_samp raise 'out of range' on them."""
    return f"(CASE WHEN isfinite({expr}) THEN {expr} END)"


def sanitize_columns(names: list[Any]) -> list[str]:
    """Stable, unique, quotable column names. Empty -> col_i; reserved names renamed; duplicates suffixed."""
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, raw in enumerate(names):
        n = "" if raw is None else str(raw).strip().replace("\n", " ").replace("\r", " ").replace('"', "'")
        if n == "" or n.lower() in ("__row__", "__group__") or n.lower().startswith("unnamed:"):
            n = f"col_{i}"
        base = n
        k = seen.get(base.lower(), 0)
        while n.lower() in seen:
            k += 1
            n = f"{base}_{k}"
        seen[base.lower()] = k
        seen[n.lower()] = seen.get(n.lower(), 0)
        out.append(n)
    return out


def _is_num_token(tok: str, decimal: str = ".") -> bool:
    t = tok.strip()
    if t in _NAN_SET:
        return False
    if decimal == ",":
        return bool(_NUM_COMMA.match(t) or _INT.match(t))
    return bool(_NUM_DOT.match(t))


def _to_float(tok: str, decimal: str = ".") -> float:
    t = tok.strip()
    if t in _NAN_SET:
        return np.nan
    if decimal == ",":
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return np.nan


def _split_line(line: str, delim: str) -> list[str]:
    if delim == "whitespace":
        return line.split()
    try:
        return next(csv.reader([line], delimiter=delim, quotechar='"'))
    except Exception:
        return line.split(delim)


def _read_head(path: Path, max_bytes: int = 2_000_000, max_lines: int = 3000) -> tuple[list[str], bool, str, int]:
    """First lines of a text file. Returns (lines, file_complete, encoding, n_comment_lines_skipped)."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        raw = f.read(max_bytes)
    complete = len(raw) >= size
    encoding = "utf-8"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("utf-16")
            encoding = "utf-16"
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
            encoding = "latin-1"
    lines = text.splitlines()
    if not complete and lines:
        lines = lines[:-1]  # drop a possibly truncated last line
    skipped = 0
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith(COMMENT_PREFIXES)):
        lines.pop(0)
        skipped += 1
    lines = [ln for ln in lines if ln.strip()]
    return lines[:max_lines], complete, encoding, skipped


def _mode(values: list[int]) -> tuple[int, float]:
    if not values:
        return 0, 0.0
    c = Counter(values)
    m, cnt = c.most_common(1)[0]
    return m, cnt / len(values)


def _lag1_autocorr_matrix(M: np.ndarray, axis: int) -> float:
    """Mean lag-1 autocorrelation of standardized series along `axis` (0: down rows, 1: along columns)."""
    A = M if axis == 0 else M.T
    if A.shape[0] < 4:
        return float("nan")
    A = A.astype(np.float64)
    mu = np.nanmean(A, axis=0)
    sd = np.nanstd(A, axis=0)
    ok = np.isfinite(sd) & (sd > 0)
    if not ok.any():
        return float("nan")
    Z = (A[:, ok] - mu[ok]) / sd[ok]
    Z = np.where(np.isfinite(Z), Z, 0.0)
    num = np.nansum(Z[1:] * Z[:-1], axis=0)
    den = np.nansum(Z * Z, axis=0)
    ac = num / np.where(den > 0, den, np.nan)
    return float(np.nanmean(ac))


# --------------------------------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------------------------------
def _sniff_text(path: Path, options: dict[str, Any]) -> dict[str, Any]:
    lines, complete, encoding, n_skip = _read_head(path)
    ev: list[dict[str, Any]] = []
    fmt: dict[str, Any] = {"format": "csv", "encoding": options.get("encoding", encoding), "skip_lines": n_skip, "file_complete_in_sample": complete, "n_lines_sampled": len(lines), "nan_tokens": NAN_TOKENS, "evidence": ev, "notes": []}
    if not lines:
        fmt["delimiter"] = ","
        fmt["has_header"] = False
        fmt["n_cols"] = 0
        fmt["notes"].append("empty file")
        return fmt

    # ---- delimiter
    forced = options.get("delimiter")
    if forced:
        delim = "whitespace" if forced in ("whitespace", r"\s+", " +") else forced
        scores = {delim: 1.0}
    else:
        best: Optional[str] = None
        best_score = -1.0
        scores = {}
        for d in CANDIDATE_DELIMITERS:
            counts = [len(_split_line(ln, d)) for ln in lines[:300]]
            m, consistency = _mode(counts)
            if m <= 1:
                scores[d] = 0.0
                continue
            data_tokens = [t for ln in lines[1:200] for t in _split_line(ln, d)]
            if data_tokens:
                num_frac = sum(1 for t in data_tokens if _is_num_token(t, ".") or _is_num_token(t, ",")) / len(data_tokens)
            else:
                num_frac = 0.0
            score = 2.0 * consistency + num_frac + 0.001 * min(m, 500)
            scores[d] = round(score, 4)
            if score > best_score + 1e-9:
                best, best_score = d, score
        delim = best or ","
    fmt["delimiter"] = delim
    fmt["delimiter_scores"] = scores
    rows = [_split_line(ln, delim) for ln in lines]
    n_cols, consistency = _mode([len(r) for r in rows[1:]] or [len(rows[0])])
    fmt["n_cols"] = int(n_cols)
    fmt["column_count_consistency"] = round(consistency, 4)
    ev.append({"kind": "format", "statement": f"delimiter {'whitespace' if delim == 'whitespace' else repr(delim)} chosen: {n_cols} columns in {consistency:.0%} of sampled lines", "values": {"delimiter": delim, "n_cols": int(n_cols), "consistency": consistency, "scores": scores}})

    # ---- decimal separator
    decimal = options.get("decimal")
    if not decimal:
        data_tokens = [t for r in rows[1:400] for t in r]
        n_dot = sum(1 for t in data_tokens if _NUM_DOT.match(t.strip()) and "." in t)
        n_comma = sum(1 for t in data_tokens if _NUM_COMMA.match(t.strip()))
        decimal = "," if (delim != "," and n_comma > 2 * max(1, n_dot)) else "."
        if decimal == ",":
            ev.append({"kind": "format", "statement": f"decimal comma detected ({n_comma} comma-decimal vs {n_dot} dot-decimal tokens)", "values": {"n_comma": n_comma, "n_dot": n_dot}})
    fmt["decimal"] = decimal

    # ---- header
    def num_frac(r: list[str]) -> float:
        return (sum(1 for t in r if _is_num_token(t, decimal)) / len(r)) if r else 0.0

    nf0 = num_frac(rows[0])
    nfd = float(np.mean([num_frac(r) for r in rows[1:200]])) if len(rows) > 1 else 0.0
    if "has_header" in options and options["has_header"] is not None:
        has_header = bool(options["has_header"])
        reason = "operator option"
    elif nfd >= 0.5:
        has_header = nf0 < 0.5
        reason = f"first row {nf0:.0%} numeric tokens vs {nfd:.0%} in data rows"
    else:
        toks0 = [t.strip() for t in rows[0]]
        unique_short = len(set(toks0)) == len(toks0) and all(0 < len(t) <= 64 for t in toks0) and not any(_DATE_LIKE.match(t) for t in toks0)
        try:
            sniff = csv.Sniffer().has_header("\n".join(lines[:50])) if delim != "whitespace" else False
        except Exception:
            sniff = False
        has_header = bool(unique_short and (sniff or nf0 == 0.0))
        reason = f"text-heavy data; first-row tokens unique={unique_short}, csv.Sniffer={sniff}"
    fmt["has_header"] = has_header
    fmt["header_names"] = sanitize_columns(rows[0]) if has_header else None
    if has_header and len(rows[0]) != n_cols:
        fmt["notes"].append(f"header has {len(rows[0])} tokens but data rows have {n_cols}")
    ev.append({"kind": "format", "statement": f"header {'present' if has_header else 'absent'}: {reason}", "values": {"numeric_fraction_row0": nf0, "numeric_fraction_data": nfd, "has_header": has_header}})

    # ---- transposed orientation (headerless numeric matrices only)
    fmt["transposed"] = False
    all_numeric = nfd >= 0.9 and nf0 >= 0.9
    if options.get("transposed") is not None:
        fmt["transposed"] = bool(options["transposed"])
        ev.append({"kind": "format", "statement": f"orientation forced by operator option: transposed={fmt['transposed']}", "values": {}})
    elif not has_header and all_numeric and n_cols >= 2:
        max_tok = 2000
        M = np.array([[_to_float(t, decimal) for t in r[:max_tok]] for r in rows[:2000] if len(r) >= min(n_cols, max_tok)], dtype=np.float64)
        if M.ndim == 2 and M.shape[0] >= 3:
            ac_down = _lag1_autocorr_matrix(M, axis=0)  # signals in columns hypothesis
            ac_along = _lag1_autocorr_matrix(M, axis=1)  # signals in rows hypothesis
            n_rows_known = complete
            n_rows = len(rows)
            shape_hint = bool(n_rows_known and n_rows * 2 < n_cols)
            transposed = False
            if np.isfinite(ac_down) and np.isfinite(ac_along):
                if ac_along - ac_down > 0.25 and ac_along > 0.5:
                    transposed = True
                elif shape_hint and ac_along > ac_down:
                    transposed = True
            elif shape_hint:
                transposed = True
            fmt["transposed"] = transposed
            fmt["orientation_stats"] = {"autocorr_down_columns": None if not np.isfinite(ac_down) else round(ac_down, 4), "autocorr_along_rows": None if not np.isfinite(ac_along) else round(ac_along, 4), "n_rows_sampled": n_rows, "n_rows_known": n_rows_known, "n_cols": int(n_cols)}
            ev.append({"kind": "orientation", "statement": (f"headerless numeric matrix: lag-1 autocorrelation down columns={ac_down:.2f}, along rows={ac_along:.2f}; " f"{'rows are signals -> transposed' if transposed else 'columns are signals'}" + (f" (only {n_rows} rows for {n_cols} columns)" if shape_hint else "")), "values": fmt["orientation_stats"]})
    return fmt


def _sniff_excel(path: Path, options: dict[str, Any]) -> dict[str, Any]:
    import openpyxl

    ev: list[dict[str, Any]] = []
    fmt: dict[str, Any] = {"format": "excel", "evidence": ev, "notes": [], "transposed": False}
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = options.get("sheet")
    ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb[wb.sheetnames[0]]
    fmt["sheet"] = ws.title
    rows = []
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        rows.append(list(r))
        if i >= 200:
            break
    wb.close()
    rows = [r for r in rows if any(v is not None and str(v).strip() != "" for v in r)]
    if not rows:
        fmt.update({"has_header": False, "n_cols": 0})
        return fmt
    n_cols = max(len(r) for r in rows)
    fmt["n_cols"] = n_cols

    def num_frac(r: list[Any]) -> float:
        vals = [v for v in r if v is not None and str(v).strip() != ""]
        return (sum(1 for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)) / len(vals)) if vals else 0.0

    nf0 = num_frac(rows[0])
    nfd = float(np.mean([num_frac(r) for r in rows[1:]])) if len(rows) > 1 else 0.0
    if options.get("has_header") is not None:
        has_header = bool(options["has_header"])
    elif nfd >= 0.5:
        has_header = nf0 < 0.5
    else:
        toks0 = [str(v).strip() for v in rows[0] if v is not None]
        has_header = len(set(toks0)) == len(toks0) and all(isinstance(v, str) for v in rows[0] if v is not None)
    fmt["has_header"] = has_header
    fmt["header_names"] = sanitize_columns([rows[0][i] if i < len(rows[0]) else None for i in range(n_cols)]) if has_header else None
    ev.append({"kind": "format", "statement": f"Excel sheet '{ws.title}': {n_cols} columns, header {'present' if has_header else 'absent'} (first row {nf0:.0%} numeric vs {nfd:.0%} in data rows)", "values": {"sheet": ws.title, "n_cols": n_cols, "numeric_fraction_row0": nf0, "numeric_fraction_data": nfd}})
    return fmt


def detect_format(path: str | Path, options: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Sniff the container format and the options needed to read it. Never reads more than ~2 MB."""
    path = Path(path)
    options = options or {}
    if not path.exists():
        raise FileNotFoundError(str(path))
    ext = path.suffix.lower()
    with open(path, "rb") as f:
        magic = f.read(8)
    forced = options.get("format")
    if forced:
        kind = forced
    elif ext in (".parquet", ".pq") or magic.startswith(b"PAR1"):
        kind = "parquet"
    elif ext in (".xlsx", ".xlsm", ".xls") or (magic.startswith(b"PK\x03\x04") and ext in (".xlsx", ".xlsm")):
        kind = "excel"
    elif ext in (".jsonl", ".ndjson"):
        kind = "jsonl"
    elif ext == ".json":
        kind = "json"
    else:
        kind = "text"

    if kind == "parquet":
        return {"format": "parquet", "has_header": True, "transposed": False, "evidence": [{"kind": "format", "statement": "Parquet container detected", "values": {}}], "notes": []}
    if kind == "excel":
        return _sniff_excel(path, options)
    if kind in ("json", "jsonl"):
        lines, complete, encoding, _ = _read_head(path, max_bytes=200_000, max_lines=50)
        first = lines[0].lstrip() if lines else ""
        if kind == "json" and first.startswith("{") and len(lines) > 1 and lines[1].lstrip().startswith("{"):
            kind = "jsonl"
        fmt = {"format": kind, "has_header": True, "transposed": False, "encoding": encoding, "evidence": [{"kind": "format", "statement": f"JSON {'lines' if kind == 'jsonl' else 'array'} detected", "values": {}}], "notes": []}
        return fmt
    fmt = _sniff_text(path, options)
    if fmt.get("delimiter") == "whitespace":
        fmt["format"] = "whitespace"
    return fmt


# --------------------------------------------------------------------------------------------------
# conversion
# --------------------------------------------------------------------------------------------------
def _progress_poller(out_path: Path, in_size: int, progress: ProgressFn, lo: float, hi: float, stop: threading.Event, message: str) -> None:
    ratio_guess = 0.30  # zstd parquet size vs text size (rough)
    while not stop.wait(2.0):
        try:
            sz = out_path.stat().st_size if out_path.exists() else 0
        except OSError:
            sz = 0
        frac = min(0.95, sz / max(1.0, in_size * ratio_guess))
        if progress:
            progress(lo + (hi - lo) * frac, message)


def _run_with_progress(fn: Callable[[], Any], out_path: Path, in_size: int, progress: ProgressFn, lo: float, hi: float, message: str) -> Any:
    stop = threading.Event()
    t = threading.Thread(target=_progress_poller, args=(out_path, in_size, progress, lo, hi, stop, message), daemon=True)
    t.start()
    try:
        return fn()
    finally:
        stop.set()
        t.join(timeout=3)


def _describe(con, sql: str) -> list[tuple[str, str]]:
    rows = con.execute(f"DESCRIBE {sql}").fetchall()
    return [(r[0], r[1]) for r in rows]


def _keep_double_columns(con, sql: str, cols: list[tuple[str, str]], sample_rows: int = 5000) -> set[str]:
    """DOUBLE columns that must stay float64: integer-valued, epoch-like or large-magnitude values."""
    doubles = [c for c, t in cols if t in ("DOUBLE", "FLOAT", "DECIMAL") or t.startswith("DECIMAL")]
    if not doubles:
        return set()
    parts = []
    types = dict(cols)
    for c in doubles:
        q = finite_sql(quote_ident(c)) if types[c] in ("DOUBLE", "FLOAT") else quote_ident(c)  # NaN sorts above every number
        parts.append(f"max(abs({q})) AS {quote_ident(c + '__max')}, sum(CASE WHEN {q} <> floor({q}) THEN 1 ELSE 0 END) AS {quote_ident(c + '__frac')}")
    row = con.execute(f"SELECT {', '.join(parts)} FROM (SELECT * FROM ({sql}) LIMIT {int(sample_rows)})").fetchone()
    keep = set()
    for i, c in enumerate(doubles):
        mx, frac = row[2 * i], row[2 * i + 1]
        if mx is None:
            continue
        if (frac or 0) == 0:
            keep.add(c)  # integer-valued
        elif mx >= 1e7:
            keep.add(c)  # float32 resolution would be > 1
        elif 1e9 <= mx <= 4e12:
            keep.add(c)  # epoch-like seconds / milliseconds
    return keep


def _projection(cols: list[tuple[str, str]], new_names: list[str], downcast: bool, keep_double: set[str], varchar_json: bool = False) -> str:
    sel = []
    for (c, t), n in zip(cols, new_names):
        q = quote_ident(c)
        tt = t.upper()
        if tt in ("DOUBLE", "FLOAT"):
            q = finite_sql(q)  # 'NAN', 'inf', ... parse as non-finite doubles; store them as missing like the nullstr tokens
        if downcast and (tt == "DOUBLE" or tt.startswith("DECIMAL")) and c not in keep_double:
            sel.append(f"CAST({q} AS FLOAT) AS {quote_ident(n)}")
        elif varchar_json and (tt.startswith("STRUCT") or tt.startswith("MAP") or "[]" in tt or tt.startswith("LIST") or tt == "JSON"):
            sel.append(f"to_json({q}) AS {quote_ident(n)}")
        elif tt in ("HUGEINT", "UHUGEINT", "UBIGINT"):
            sel.append(f"CAST({q} AS DOUBLE) AS {quote_ident(n)}")
        else:
            sel.append(f"{q} AS {quote_ident(n)}")
    return ", ".join(sel)


def _copy(con, select_sql: str, out_path: Path, compression: str) -> None:
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    con.execute(f"COPY ({select_sql}) TO {sql_lit(tmp.as_posix())} (FORMAT PARQUET, COMPRESSION {compression})")
    os.replace(tmp, out_path)


def _verify_row_order(con, out_path: Path) -> bool:
    bad = con.execute(f"SELECT count(*) FROM (SELECT __row__, row_number() OVER () - 1 AS rn FROM read_parquet({sql_lit(out_path.as_posix())})) WHERE __row__ <> rn").fetchone()[0]
    return int(bad) == 0


def _csv_read_sql(path: Path, fmt: dict[str, Any], sample_size: int, extra: str = "") -> str:
    opts = [f"delim={sql_lit(fmt.get('delimiter', ','))}", f"header={'true' if fmt.get('has_header') else 'false'}", f"sample_size={int(sample_size)}", f"nullstr=[{', '.join(sql_lit(t) for t in NAN_TOKENS if t != '')}, '']", "quote='\"'", "null_padding=true"]
    if fmt.get("decimal") == ",":
        opts.append("decimal_separator=','")
    enc = fmt.get("encoding", "utf-8")
    if enc and enc.lower() not in ("utf-8", "utf8"):
        opts.append(f"encoding={sql_lit(enc)}")
    if fmt.get("skip_lines"):
        opts.append(f"skip={int(fmt['skip_lines'])}")
    if extra:
        opts.append(extra)
    return f"SELECT * FROM read_csv({sql_lit(path.as_posix())}, {', '.join(opts)})"


def _convert_delimited(con, path: Path, fmt: dict[str, Any], out_path: Path, settings, progress: ProgressFn, lo: float, hi: float) -> dict[str, Any]:
    sample_size = int(settings.ingest.sample_rows_for_typing)
    notes: list[str] = []
    attempts = [("strict", ""), ("promote", None), ("ignore_errors", "ignore_errors=true"), ("all_varchar", "all_varchar=true, ignore_errors=true")]
    last_err: Optional[Exception] = None
    for name, extra in attempts:
        try:
            src = _csv_read_sql(path, fmt, sample_size, extra or "")
            cols = _describe(con, src)
            if name == "promote":
                ints = [c for c, t in cols if t.upper() in ("BIGINT", "INTEGER", "SMALLINT", "TINYINT", "HUGEINT", "UBIGINT", "UINTEGER")]
                if not ints:
                    continue
                types = "types={" + ", ".join(f"{sql_lit(c)}: 'DOUBLE'" for c in ints) + "}"
                src = _csv_read_sql(path, fmt, sample_size, types)
                cols = _describe(con, src)
            names = sanitize_columns([c for c, _ in cols])
            if not fmt.get("has_header"):
                names = [f"col_{i}" for i in range(len(cols))]
            keep = _keep_double_columns(con, src, cols)
            proj = _projection(cols, names, settings.ingest.dtype_downcast == "float32", keep)
            sel = f"SELECT {proj}, row_number() OVER () - 1 AS __row__ FROM ({src})"
            _run_with_progress(lambda: _copy(con, sel, out_path, settings.ingest.parquet_compression), out_path, path.stat().st_size, progress, lo, hi, f"converting ({name})")
            if name != "strict":
                notes.append(f"conversion needed fallback '{name}': {str(last_err)[:300]}")
            return {"columns": names, "source_types": dict(zip(names, [t for _, t in cols])), "notes": notes, "attempt": name}
        except Exception as e:  # try the next, more tolerant strategy
            last_err = e
            continue
    raise RuntimeError(f"could not convert {path.name}: {last_err}")


def _convert_whitespace(con, path: Path, fmt: dict[str, Any], out_path: Path, settings, progress: ProgressFn, lo: float, hi: float) -> dict[str, Any]:
    n_cols = int(fmt["n_cols"])
    lines, _, _, _ = _read_head(path)
    if fmt.get("has_header"):
        names = fmt.get("header_names") or [f"col_{i}" for i in range(n_cols)]
        data_rows = [ln.split() for ln in lines[1:]]
    else:
        names = [f"col_{i}" for i in range(n_cols)]
        data_rows = [ln.split() for ln in lines]
    names = sanitize_columns(names[:n_cols] + [f"col_{i}" for i in range(len(names), n_cols)])
    decimal = fmt.get("decimal", ".")
    # per-column kind from the sample: int | float | text
    kinds = []
    for j in range(n_cols):
        toks = [r[j] for r in data_rows if len(r) > j and r[j] not in _NAN_SET]
        if not toks:
            kinds.append("float")
            continue
        n_int = sum(1 for t in toks if _INT.match(t))
        n_num = sum(1 for t in toks if _is_num_token(t, decimal))
        if n_num / len(toks) < 0.9:
            kinds.append("text")
        elif n_int == len(toks):
            kinds.append("int")
        else:
            mx = max(abs(_to_float(t, decimal)) for t in toks)
            kinds.append("double" if (mx >= 1e7 or 1e9 <= mx <= 4e12) else "float")
    sel_parts = []
    downcast = settings.ingest.dtype_downcast == "float32"
    nan_list = ", ".join(sql_lit(t) for t in NAN_TOKENS)
    for j, (n, k) in enumerate(zip(names, kinds)):
        # missing-value tokens become NULL in every column, as nullstr does on the delimited path. They must go
        # before the cast: TRY_CAST('NaN' AS DOUBLE) succeeds and yields an IEEE NaN, not NULL.
        tok = f"(CASE WHEN parts[{j + 1}] IN ({nan_list}) THEN NULL ELSE parts[{j + 1}] END)"
        if k == "text":
            sel_parts.append(f"{tok} AS {quote_ident(n)}")
            continue
        if decimal == ",":
            tok = f"replace({tok}, ',', '.')"
        num = finite_sql(f"TRY_CAST({tok} AS DOUBLE)")  # also 'NAN', 'inf', '1e999', ...
        if k == "int":
            sel_parts.append(f"COALESCE(TRY_CAST({tok} AS BIGINT), TRY_CAST({num} AS BIGINT)) AS {quote_ident(n)}")
        elif k == "double" or not downcast:
            sel_parts.append(f"{num} AS {quote_ident(n)}")
        else:
            sel_parts.append(f"CAST({num} AS FLOAT) AS {quote_ident(n)}")
    skip = int(fmt.get("skip_lines", 0)) + (1 if fmt.get("has_header") else 0)
    src = f"SELECT regexp_split_to_array(trim(line), '\\s+') AS parts FROM read_csv({sql_lit(path.as_posix())}, delim='\\x01', header=false, columns={{'line': 'VARCHAR'}}, quote='', escape='', skip={skip}) WHERE trim(line) <> ''"
    sel = f"SELECT {', '.join(sel_parts)}, row_number() OVER () - 1 AS __row__ FROM ({src})"
    _run_with_progress(lambda: _copy(con, sel, out_path, settings.ingest.parquet_compression), out_path, path.stat().st_size, progress, lo, hi, "converting whitespace-delimited")
    return {"columns": names, "source_types": dict(zip(names, kinds)), "notes": [], "attempt": "whitespace"}


def _convert_parquet(con, path: Path, out_path: Path, settings, progress: ProgressFn, lo: float, hi: float) -> dict[str, Any]:
    src = f"SELECT * FROM read_parquet({sql_lit(path.as_posix())})"
    cols = _describe(con, src)
    names = sanitize_columns([c for c, _ in cols])
    keep = _keep_double_columns(con, src, cols)
    proj = _projection(cols, names, settings.ingest.dtype_downcast == "float32", keep, varchar_json=True)
    sel = f"SELECT {proj}, row_number() OVER () - 1 AS __row__ FROM ({src})"
    _run_with_progress(lambda: _copy(con, sel, out_path, settings.ingest.parquet_compression), out_path, path.stat().st_size, progress, lo, hi, "converting parquet")
    return {"columns": names, "source_types": dict(zip(names, [t for _, t in cols])), "notes": [], "attempt": "parquet"}


def _convert_json(con, path: Path, fmt: dict[str, Any], out_path: Path, settings, progress: ProgressFn, lo: float, hi: float) -> dict[str, Any]:
    jf = "newline_delimited" if fmt["format"] == "jsonl" else "auto"
    src = f"SELECT * FROM read_json({sql_lit(path.as_posix())}, format={sql_lit(jf)}, records='auto', sample_size={int(settings.ingest.sample_rows_for_typing)}, maximum_object_size=67108864)"
    cols = _describe(con, src)
    names = sanitize_columns([c for c, _ in cols])
    keep = _keep_double_columns(con, src, cols)
    proj = _projection(cols, names, settings.ingest.dtype_downcast == "float32", keep, varchar_json=True)
    sel = f"SELECT {proj}, row_number() OVER () - 1 AS __row__ FROM ({src})"
    _run_with_progress(lambda: _copy(con, sel, out_path, settings.ingest.parquet_compression), out_path, path.stat().st_size, progress, lo, hi, "converting json")
    return {"columns": names, "source_types": dict(zip(names, [t for _, t in cols])), "notes": [], "attempt": fmt["format"]}


def _arrow_schema_from_pandas(df) -> Any:
    import pyarrow as pa

    return pa.Schema.from_pandas(df, preserve_index=False)


def _write_chunks_parquet(chunks, out_path: Path, settings, downcast: bool = True) -> dict[str, Any]:
    """Write an iterator of pandas DataFrames (same columns) to parquet with __row__ appended."""
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    writer = None
    schema = None
    n = 0
    names: list[str] = []
    try:
        for df in chunks:
            if df is None or len(df) == 0:
                continue
            df = df.copy()
            if not names:
                names = sanitize_columns(list(df.columns))
            df.columns = names
            for c in names:
                s = df[c]
                if pd.api.types.is_float_dtype(s):
                    # finite or missing only: from_pandas below writes NaN as NULL but would keep +/-inf as a value
                    inf = np.isinf(s.to_numpy(dtype="float64", na_value=np.nan))
                    if inf.any():
                        s = df[c] = s.mask(inf)
                if downcast and pd.api.types.is_float_dtype(s):
                    mx = float(np.nanmax(np.abs(s.to_numpy(dtype="float64")))) if s.notna().any() else 0.0
                    is_int_valued = bool(s.dropna().apply(lambda v: float(v).is_integer()).all()) if len(s.dropna()) and len(s.dropna()) < 200_000 else False
                    if mx < 1e7 and not (1e9 <= mx <= 4e12) and not is_int_valued:
                        df[c] = s.astype("float32")
                elif pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s):
                    df[c] = s.astype("string")
            df["__row__"] = np.arange(n, n + len(df), dtype=np.int64)
            tbl = pa.Table.from_pandas(df, preserve_index=False)
            if writer is None:
                schema = tbl.schema
                writer = pq.ParquetWriter(tmp, schema, compression=settings.ingest.parquet_compression)
            else:
                try:
                    tbl = tbl.cast(schema)
                except Exception:
                    # type drift between chunks: fall back to strings for offending columns
                    cols = []
                    for f in schema:
                        col = tbl.column(f.name)
                        try:
                            cols.append(col.cast(f.type))
                        except Exception:
                            cols.append(col.cast(pa.string()) if pa.types.is_string(f.type) else col)
                    tbl = pa.Table.from_arrays(cols, schema=schema)
            writer.write_table(tbl)
            n += len(df)
    finally:
        if writer is not None:
            writer.close()
    if writer is None:
        raise RuntimeError("no rows to write")
    os.replace(tmp, out_path)
    return {"columns": names, "n_rows": n}


def _convert_excel(path: Path, fmt: dict[str, Any], out_path: Path, settings, progress: ProgressFn, lo: float, hi: float) -> dict[str, Any]:
    import openpyxl
    import pandas as pd

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[fmt["sheet"]] if fmt.get("sheet") in wb.sheetnames else wb[wb.sheetnames[0]]
    n_cols = int(fmt.get("n_cols") or ws.max_column or 0)
    names = fmt.get("header_names") or [f"col_{i}" for i in range(n_cols)]
    chunk = max(10_000, min(int(settings.ingest.chunk_rows), mem_chunk_rows(n_cols)))
    total = ws.max_row or 0

    def gen():
        buf: list[list[Any]] = []
        for i, r in enumerate(ws.iter_rows(values_only=True)):
            if i == 0 and fmt.get("has_header"):
                continue
            row = list(r)[:n_cols] + [None] * max(0, n_cols - len(r))
            if all(v is None for v in row):
                continue
            buf.append(row)
            if len(buf) >= chunk:
                if progress and total:
                    progress(lo + (hi - lo) * min(0.95, i / total), "converting excel")
                yield pd.DataFrame(buf, columns=names)
                buf = []
        if buf:
            yield pd.DataFrame(buf, columns=names)

    try:
        res = _write_chunks_parquet(gen(), out_path, settings, downcast=settings.ingest.dtype_downcast == "float32")
    finally:
        wb.close()
    return {"columns": res["columns"], "source_types": {}, "notes": [f"excel sheet {ws.title}"], "attempt": "excel"}


def _convert_transposed(path: Path, fmt: dict[str, Any], out_path: Path, settings, progress: ProgressFn, lo: float, hi: float) -> dict[str, Any]:
    """Signals-in-rows text matrix -> parquet with signals in columns, via a disk memmap (bounded RAM)."""
    import pandas as pd

    decimal = fmt.get("decimal", ".")
    skip = int(fmt.get("skip_lines", 0))
    n_lines = 0
    n_samples = 0
    with open(path, "r", encoding=fmt.get("encoding", "utf-8"), errors="replace") as f:
        for i, ln in enumerate(f):
            if i < skip or not ln.strip():
                continue
            if n_samples == 0:
                n_samples = len(ln.split())
            n_lines += 1
    if n_lines == 0 or n_samples == 0:
        raise RuntimeError("empty transposed matrix")
    mm_path = out_path.with_suffix(".transpose.tmp")
    mm = np.memmap(mm_path, dtype=np.float32, mode="w+", shape=(n_lines, n_samples))
    with open(path, "r", encoding=fmt.get("encoding", "utf-8"), errors="replace") as f:
        r = 0
        for i, ln in enumerate(f):
            if i < skip or not ln.strip():
                continue
            toks = ln.split()
            if decimal == ",":
                toks = [t.replace(",", ".") for t in toks]
            vals = np.array([_to_float(t) for t in toks[:n_samples]], dtype=np.float32)
            if len(vals) < n_samples:
                vals = np.concatenate([vals, np.full(n_samples - len(vals), np.nan, dtype=np.float32)])
            mm[r, :] = vals
            r += 1
            if progress and r % 50 == 0:
                progress(lo + (hi - lo) * 0.5 * r / n_lines, "reading transposed matrix")
    mm.flush()
    names = [f"col_{i}" for i in range(n_lines)]
    step = max(10_000, mem_chunk_rows(n_lines, bytes_per_value=4))

    def gen():
        for a in range(0, n_samples, step):
            b = min(n_samples, a + step)
            block = np.ascontiguousarray(mm[:, a:b].T)
            if progress:
                progress(lo + (hi - lo) * (0.5 + 0.5 * b / n_samples), "writing transposed matrix")
            yield pd.DataFrame(block, columns=names)

    try:
        res = _write_chunks_parquet(gen(), out_path, settings, downcast=False)
    finally:
        del mm
        try:
            mm_path.unlink()
        except OSError:
            pass
    return {"columns": res["columns"], "source_types": {n: "float" for n in names}, "notes": ["transposed on ingest: rows in the file are signals"], "attempt": "transposed"}


def convert_to_parquet(path: str | Path, fmt: dict[str, Any], out_path: str | Path, settings, progress: ProgressFn = None, con=None, lo: float = 0.0, hi: float = 1.0) -> dict[str, Any]:
    """Stream the source into ``out_path`` (parquet) with an appended ``__row__`` (0-based file order)."""
    import duckdb

    path = Path(path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    own_con = con is None
    if own_con:
        from ..memory import duckdb_memory_limit, duckdb_threads

        con = duckdb.connect(database=":memory:")
        con.execute(f"SET memory_limit='{duckdb_memory_limit()}'")
        con.execute(f"SET threads={duckdb_threads()}")
        con.execute(f"SET temp_directory='{(out_path.parent / 'duck_tmp').as_posix()}'")
    try:
        f = fmt.get("format")
        if fmt.get("transposed"):
            res = _convert_transposed(path, fmt, out_path, settings, progress, lo, hi)
        elif f == "parquet":
            res = _convert_parquet(con, path, out_path, settings, progress, lo, hi)
        elif f == "excel":
            res = _convert_excel(path, fmt, out_path, settings, progress, lo, hi)
        elif f in ("json", "jsonl"):
            res = _convert_json(con, path, fmt, out_path, settings, progress, lo, hi)
        elif f == "whitespace":
            res = _convert_whitespace(con, path, fmt, out_path, settings, progress, lo, hi)
        else:
            res = _convert_delimited(con, path, fmt, out_path, settings, progress, lo, hi)
        n_rows = int(con.execute(f"SELECT count(*) FROM read_parquet({sql_lit(out_path.as_posix())})").fetchone()[0])
        ordered = _verify_row_order(con, out_path)
        types = con.execute(f"DESCRIBE SELECT * FROM read_parquet({sql_lit(out_path.as_posix())})").fetchall()
        res.update({"n_rows": n_rows, "n_cols": len(res["columns"]), "parquet_types": {r[0]: r[1] for r in types}, "row_order_verified": ordered, "seconds": round(time.time() - t0, 2)})
        if not ordered:
            res["notes"].append("parquet row order does not match __row__; downstream must ORDER BY __row__")
        return res
    finally:
        if own_con:
            con.close()


def dataframe_to_parquet(df, out_path: str | Path, settings) -> dict[str, Any]:
    """Small-data path (API uploads, batches): a pandas DataFrame -> dataset.parquet with __row__."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    df = df.copy()
    df.columns = sanitize_columns(list(df.columns))
    if "__row__" in df.columns:
        df = df.drop(columns=["__row__"])
    if "__group__" in df.columns:
        df = df.drop(columns=["__group__"])
    step = max(10_000, mem_chunk_rows(df.shape[1]))
    res = _write_chunks_parquet((df.iloc[a : a + step] for a in range(0, len(df), step)), out_path, settings, downcast=settings.ingest.dtype_downcast == "float32")
    import duckdb

    con = duckdb.connect()
    types = con.execute(f"DESCRIBE SELECT * FROM read_parquet({sql_lit(out_path.as_posix())})").fetchall()
    con.close()
    return {"columns": res["columns"], "n_rows": res["n_rows"], "n_cols": len(res["columns"]), "parquet_types": {r[0]: r[1] for r in types}, "source_types": {}, "notes": [], "attempt": "dataframe", "row_order_verified": True, "seconds": round(time.time() - t0, 2)}


def read_small(path: str | Path, fmt: Optional[dict[str, Any]] = None, max_rows: Optional[int] = None):
    """Bounded read of a (small) file into pandas, e.g. one incoming batch file in a watch folder."""
    import duckdb
    import pandas as pd

    path = Path(path)
    fmt = fmt or detect_format(path)
    con = duckdb.connect()
    try:
        lim = f" LIMIT {int(max_rows)}" if max_rows else ""
        f = fmt.get("format")
        if f == "parquet":
            return con.execute(f"SELECT * FROM read_parquet({sql_lit(path.as_posix())}){lim}").df()
        if f in ("json", "jsonl"):
            jf = "newline_delimited" if f == "jsonl" else "auto"
            return con.execute(f"SELECT * FROM read_json({sql_lit(path.as_posix())}, format={sql_lit(jf)}, records='auto'){lim}").df()
        if f == "excel":
            df = pd.read_excel(path, sheet_name=fmt.get("sheet", 0), header=0 if fmt.get("has_header") else None, nrows=max_rows)
            if not fmt.get("has_header"):
                df.columns = [f"col_{i}" for i in range(df.shape[1])]
            return df
        if f == "whitespace" or fmt.get("transposed"):
            df = pd.read_csv(path, sep=r"\s+", header=0 if fmt.get("has_header") else None, nrows=max_rows, engine="python", skiprows=fmt.get("skip_lines", 0), na_values=NAN_TOKENS, decimal=fmt.get("decimal", "."))
            if fmt.get("transposed"):
                df = df.T.reset_index(drop=True)
            if not fmt.get("has_header"):
                df.columns = [f"col_{i}" for i in range(df.shape[1])]
            return df
        src = _csv_read_sql(path, fmt, 20_000)
        df = con.execute(src + lim).df()
        if not fmt.get("has_header"):
            df.columns = [f"col_{i}" for i in range(df.shape[1])]
        else:
            df.columns = sanitize_columns(list(df.columns))
        return df
    finally:
        con.close()
