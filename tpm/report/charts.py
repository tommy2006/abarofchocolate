"""Inline SVG charts for the HTML report. No JavaScript, no external assets; print-friendly."""
from __future__ import annotations

import math
from html import escape
from typing import Any, Iterable, Optional, Sequence

PALETTE = {
    "ink": "#1f2937",
    "muted": "#6b7280",
    "grid": "#e5e7eb",
    "primary": "#1a4d8f",
    "primary_soft": "#c7d7ee",
    "pass": "#2e7d32",
    "warn": "#ed6c02",
    "fail": "#c62828",
    "flag": "#fde2e2",
    "external": "#7b1fa2",
    "local": "#1a4d8f",
    "box": "#f3f4f6",
}


def _fmt(v: float) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return ""
    return f"{v:.3g}"


def sparkline(
    values: Sequence[float],
    threshold: Optional[float] = None,
    flagged: Optional[Iterable[tuple[int, int]]] = None,
    width: int = 640,
    height: int = 96,
    label: str = "",
    x_labels: Optional[tuple[str, str]] = None,
) -> str:
    """Score timeline: line, threshold rule, shaded flagged spans (indices into values)."""
    vals = [float(v) if v is not None and not (isinstance(v, float) and math.isnan(v)) else 0.0 for v in values]
    if not vals:
        return ""
    pad_l, pad_r, pad_t, pad_b = 44, 8, 14, 18
    w = width - pad_l - pad_r
    h = height - pad_t - pad_b
    vmax = max(max(vals), threshold or 0.0, 1e-9)
    vmin = min(min(vals), 0.0)
    span = (vmax - vmin) or 1.0
    n = len(vals)

    def x(i: int) -> float:
        return pad_l + (w * i / max(1, n - 1))

    def y(v: float) -> float:
        return pad_t + h - (v - vmin) / span * h

    parts = [f'<svg class="spark" viewBox="0 0 {width} {height}" width="100%" preserveAspectRatio="none" role="img" aria-label="{escape(label)}">']
    parts.append(f'<rect x="{pad_l}" y="{pad_t}" width="{w}" height="{h}" fill="#fff" stroke="{PALETTE["grid"]}"/>')
    for a, b in (flagged or []):
        a = max(0, min(n - 1, int(a)))
        b = max(a, min(n - 1, int(b)))
        parts.append(f'<rect x="{x(a):.1f}" y="{pad_t}" width="{max(1.5, x(b) - x(a)):.1f}" height="{h}" fill="{PALETTE["flag"]}"/>')
    if threshold is not None:
        parts.append(f'<line x1="{pad_l}" x2="{pad_l + w}" y1="{y(threshold):.1f}" y2="{y(threshold):.1f}" stroke="{PALETTE["fail"]}" stroke-dasharray="4 3" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 4}" y="{y(threshold) + 4:.1f}" font-size="10" text-anchor="end" fill="{PALETTE["fail"]}">{_fmt(threshold)}</text>')
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    parts.append(f'<polyline points="{pts}" fill="none" stroke="{PALETTE["primary"]}" stroke-width="1.4" vector-effect="non-scaling-stroke"/>')
    parts.append(f'<text x="{pad_l - 4}" y="{pad_t + 4}" font-size="10" text-anchor="end" fill="{PALETTE["muted"]}">{_fmt(vmax)}</text>')
    parts.append(f'<text x="{pad_l - 4}" y="{pad_t + h}" font-size="10" text-anchor="end" fill="{PALETTE["muted"]}">{_fmt(vmin)}</text>')
    if x_labels:
        parts.append(f'<text x="{pad_l}" y="{height - 4}" font-size="10" fill="{PALETTE["muted"]}">{escape(str(x_labels[0]))}</text>')
        parts.append(f'<text x="{pad_l + w}" y="{height - 4}" font-size="10" text-anchor="end" fill="{PALETTE["muted"]}">{escape(str(x_labels[1]))}</text>')
    if label:
        parts.append(f'<text x="{pad_l + 4}" y="{pad_t + 12}" font-size="11" fill="{PALETTE["ink"]}" font-weight="600">{escape(label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def stacked_bars(rows: Sequence[tuple[str, dict[str, int]]], width: int = 640, labels: Optional[dict[str, str]] = None) -> str:
    """Horizontal stacked pass/warn/fail bars, one per category."""
    if not rows:
        return ""
    labels = labels or {}
    row_h, gap, pad_l, pad_r = 22, 8, 130, 60
    height = len(rows) * (row_h + gap) + gap
    w = width - pad_l - pad_r
    parts = [f'<svg class="bars" viewBox="0 0 {width} {height}" width="100%" role="img">']
    for i, (name, counts) in enumerate(rows):
        total = max(1, sum(int(counts.get(k, 0)) for k in ("pass", "warn", "fail", "not_testable")))
        y0 = gap + i * (row_h + gap)
        parts.append(f'<text x="{pad_l - 8}" y="{y0 + row_h * 0.7:.1f}" font-size="12" text-anchor="end" fill="{PALETTE["ink"]}">{escape(str(name))}</text>')
        x0 = pad_l
        for k in ("pass", "warn", "fail", "not_testable"):  # not testable: grey, neither a pass nor a failure
            c = int(counts.get(k, 0))
            if c <= 0:
                continue
            ww = w * c / total
            parts.append(f'<rect x="{x0:.1f}" y="{y0}" width="{ww:.1f}" height="{row_h}" fill="{PALETTE.get(k, PALETTE["muted"])}"><title>{escape(labels.get(k, k))}: {c}</title></rect>')
            if ww > 22:
                parts.append(f'<text x="{x0 + ww / 2:.1f}" y="{y0 + row_h * 0.7:.1f}" font-size="11" text-anchor="middle" fill="#fff">{c}</text>')
            x0 += ww
        parts.append(f'<text x="{pad_l + w + 6}" y="{y0 + row_h * 0.7:.1f}" font-size="11" fill="{PALETTE["muted"]}">{total}</text>')
    parts.append("</svg>")
    return "".join(parts)


def hbars(items: Sequence[tuple[str, float, str]], width: int = 520, color: str = PALETTE["primary"]) -> str:
    """Contribution bars: (label, value in [0,1], right-hand text)."""
    if not items:
        return ""
    row_h, gap, pad_l, pad_r = 16, 6, 60, 70
    height = len(items) * (row_h + gap) + gap
    w = width - pad_l - pad_r
    vmax = max(max(float(v) for _, v, _ in items), 1e-9)
    parts = [f'<svg class="hbars" viewBox="0 0 {width} {height}" width="100%" role="img">']
    for i, (name, v, txt) in enumerate(items):
        y0 = gap + i * (row_h + gap)
        ww = w * max(0.0, float(v)) / vmax
        parts.append(f'<text x="{pad_l - 6}" y="{y0 + row_h * 0.78:.1f}" font-size="11" text-anchor="end" fill="{PALETTE["ink"]}" font-weight="600">{escape(str(name))}</text>')
        parts.append(f'<rect x="{pad_l}" y="{y0}" width="{w}" height="{row_h}" fill="{PALETTE["box"]}"/>')
        parts.append(f'<rect x="{pad_l}" y="{y0}" width="{ww:.1f}" height="{row_h}" fill="{color}"/>')
        parts.append(f'<text x="{pad_l + w + 6}" y="{y0 + row_h * 0.78:.1f}" font-size="11" fill="{PALETTE["muted"]}">{escape(str(txt))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def line_chart(points: Sequence[tuple[float, float]], width: int = 420, height: int = 160, x_label: str = "", y_label: str = "") -> str:
    """Simple x/y line (learning curve)."""
    pts = [(float(a), float(b)) for a, b in points if a is not None and b is not None]
    if len(pts) < 2:
        return ""
    pad_l, pad_r, pad_t, pad_b = 40, 10, 10, 26
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(0.0, min(ys)), max(1e-9, max(ys))
    xs_span = (xmax - xmin) or 1.0
    ys_span = (ymax - ymin) or 1.0
    X = lambda a: pad_l + (a - xmin) / xs_span * w  # noqa: E731
    Y = lambda b: pad_t + h - (b - ymin) / ys_span * h  # noqa: E731
    parts = [f'<svg class="line" viewBox="0 0 {width} {height}" width="{width}" role="img">']
    parts.append(f'<rect x="{pad_l}" y="{pad_t}" width="{w}" height="{h}" fill="#fff" stroke="{PALETTE["grid"]}"/>')
    parts.append(f'<polyline points="{" ".join(f"{X(a):.1f},{Y(b):.1f}" for a, b in pts)}" fill="none" stroke="{PALETTE["primary"]}" stroke-width="2"/>')
    for a, b in pts:
        parts.append(f'<circle cx="{X(a):.1f}" cy="{Y(b):.1f}" r="3" fill="{PALETTE["primary"]}"><title>{_fmt(a)}, {_fmt(b)}</title></circle>')
    parts.append(f'<text x="{pad_l - 4}" y="{pad_t + 4}" font-size="10" text-anchor="end" fill="{PALETTE["muted"]}">{_fmt(ymax)}</text>')
    parts.append(f'<text x="{pad_l - 4}" y="{pad_t + h}" font-size="10" text-anchor="end" fill="{PALETTE["muted"]}">{_fmt(ymin)}</text>')
    parts.append(f'<text x="{pad_l}" y="{height - 8}" font-size="10" fill="{PALETTE["muted"]}">{_fmt(xmin)}</text>')
    parts.append(f'<text x="{pad_l + w}" y="{height - 8}" font-size="10" text-anchor="end" fill="{PALETTE["muted"]}">{_fmt(xmax)}</text>')
    if x_label:
        parts.append(f'<text x="{pad_l + w / 2:.1f}" y="{height - 8}" font-size="10" text-anchor="middle" fill="{PALETTE["muted"]}">{escape(x_label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def dataflow_diagram(
    profile: str,
    allow_external: bool,
    local_model: str,
    external_model: str,
    n_local: int,
    n_external: int,
    n_blocked: int,
    t: Any,
    width: int = 760,
) -> str:
    """What stays inside vs. what may leave: two boxes, a guard, and arrows."""
    height = 300
    P = PALETTE
    inside_w = 440
    parts = [f'<svg class="dataflow" viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="data flow">']
    parts.append('<defs><marker id="arr" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#374151"/></marker></defs>')
    # inside box
    parts.append(f'<rect x="10" y="20" width="{inside_w}" height="{height - 40}" rx="10" fill="#eef4fb" stroke="{P["primary"]}" stroke-width="1.5"/>')
    parts.append(f'<text x="24" y="42" font-size="13" font-weight="700" fill="{P["primary"]}">{escape(t("s8_stays"))}</text>')
    items = [("dataset.parquet / raw rows", 70), ("evidence · signals · relations · checks", 100), ("scores · flags · diagnoses", 130), ("decision_log.sqlite (hash chain)", 160), ("egress_ledger.jsonl", 190)]
    for txt, yy in items:
        parts.append(f'<rect x="24" y="{yy - 14}" width="230" height="22" rx="4" fill="#fff" stroke="{P["grid"]}"/>')
        parts.append(f'<text x="32" y="{yy + 1}" font-size="11" fill="{P["ink"]}">{escape(txt)}</text>')
    # local model
    parts.append(f'<rect x="280" y="66" width="156" height="60" rx="8" fill="#fff" stroke="{P["local"]}" stroke-width="1.5"/>')
    parts.append(f'<text x="358" y="88" font-size="11" text-anchor="middle" font-weight="700" fill="{P["local"]}">{escape(t("s8_local_model"))} (Ollama)</text>')
    parts.append(f'<text x="358" y="106" font-size="10" text-anchor="middle" fill="{P["ink"]}">{escape(local_model[:26])}</text>')
    parts.append(f'<text x="358" y="120" font-size="10" text-anchor="middle" fill="{P["muted"]}">{n_local} {escape(t("calls_local").lower())}</text>')
    parts.append(f'<line x1="254" y1="100" x2="278" y2="96" stroke="#374151" marker-end="url(#arr)"/>')
    # guard
    parts.append(f'<rect x="300" y="170" width="118" height="54" rx="8" fill="#fff8e1" stroke="{P["warn"]}" stroke-width="1.5"/>')
    parts.append(f'<text x="359" y="192" font-size="11" text-anchor="middle" font-weight="700" fill="{P["warn"]}">{escape(t("guard_result"))}</text>')
    parts.append(f'<text x="359" y="210" font-size="10" text-anchor="middle" fill="{P["muted"]}">{n_blocked} {escape(t("calls_blocked").lower())}</text>')
    parts.append(f'<line x1="254" y1="160" x2="298" y2="190" stroke="#374151" marker-end="url(#arr)"/>')
    # outside
    ext_x = inside_w + 60
    dash = "" if allow_external else ' stroke-dasharray="6 4"'
    parts.append(f'<rect x="{ext_x}" y="150" width="{width - ext_x - 10}" height="94" rx="8" fill="#fff" stroke="{P["external"]}" stroke-width="1.5"{dash}/>')
    parts.append(f'<text x="{ext_x + (width - ext_x - 10) / 2:.0f}" y="172" font-size="11" text-anchor="middle" font-weight="700" fill="{P["external"]}">{escape(t("s8_external_model"))}</text>')
    parts.append(f'<text x="{ext_x + (width - ext_x - 10) / 2:.0f}" y="190" font-size="10" text-anchor="middle" fill="{P["ink"]}">{escape(external_model[:28])}</text>')
    parts.append(f'<text x="{ext_x + (width - ext_x - 10) / 2:.0f}" y="206" font-size="10" text-anchor="middle" fill="{P["muted"]}">{n_external} {escape(t("calls_external").lower())}</text>')
    parts.append(f'<text x="{ext_x + (width - ext_x - 10) / 2:.0f}" y="226" font-size="10" text-anchor="middle" fill="{P["muted"]}">{escape(t("s8_allow_external"))}: {escape(t("yes") if allow_external else t("no"))}</text>')
    arrow_style = f'stroke="{P["external"]}"' + ("" if allow_external else ' stroke-dasharray="6 4"')
    parts.append(f'<line x1="420" y1="197" x2="{ext_x - 3}" y2="197" {arrow_style} stroke-width="1.5" marker-end="url(#arr)"/>')
    parts.append(f'<text x="{(420 + ext_x) / 2:.0f}" y="188" font-size="9" text-anchor="middle" fill="{P["muted"]}">{escape(t("s8_leaves_items")[:52])}…</text>')
    parts.append(f'<text x="{width / 2:.0f}" y="{height - 6}" font-size="10" text-anchor="middle" fill="{P["muted"]}">{escape(t("s8_profile"))}: {escape(profile)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def badge(text: str, kind: str = "muted") -> str:
    color = PALETTE.get(kind, PALETTE["muted"])
    return f'<span class="badge" style="background:{color}">{escape(str(text))}</span>'


def heatmap(labels: Sequence[str], matrix: Sequence[Sequence[Optional[float]]], cell: int = 11, title: str = "") -> str:
    """Signed correlation heatmap (blue negative, red positive, white none). Labels on both axes; readable up to
    about 60 signals; the value is in each cell's tooltip."""
    n = len(labels)
    if n == 0:
        return ""
    pad = 46
    w = pad + n * cell + 8
    h = pad + n * cell + 26
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{escape(title or "correlation heatmap")}">']
    fs = max(6, min(10, cell - 2))
    for i, lab in enumerate(labels):
        y = pad + i * cell + cell - 2
        out.append(f'<text x="{pad - 3}" y="{y}" font-size="{fs}" text-anchor="end" fill="{PALETTE["ink"]}">{escape(str(lab))}</text>')
        x = pad + i * cell + cell - 2
        out.append(f'<text x="{x}" y="{pad - 4}" font-size="{fs}" text-anchor="start" transform="rotate(-90 {x} {pad - 4})" fill="{PALETTE["ink"]}">{escape(str(lab))}</text>')
    for i in range(n):
        for j in range(n):
            v = matrix[i][j] if i < len(matrix) and j < len(matrix[i]) else None
            if v is None or (isinstance(v, float) and math.isnan(v)):
                col = "#f3f4f6"
            else:
                a = max(-1.0, min(1.0, float(v)))
                k = int(255 * (1 - abs(a)))
                col = f"rgb(255,{k},{k})" if a >= 0 else f"rgb({k},{k},255)"
            out.append(f'<rect x="{pad + j * cell}" y="{pad + i * cell}" width="{cell}" height="{cell}" fill="{col}"><title>{escape(str(labels[i]))} ~ {escape(str(labels[j]))}: {"" if v is None else f"{float(v):.2f}"}</title></rect>')
    ly = pad + n * cell + 14
    out.append(f'<text x="{pad}" y="{ly}" font-size="9" fill="{PALETTE["muted"]}">blue = move in opposite directions, red = move together, white = unrelated</text>')
    out.append("</svg>")
    return "".join(out)


def trend_plot(values: Sequence[float], band: Optional[tuple[float, float]], marks: Sequence[int] = (), width: int = 640, height: int = 110, label: str = "", x_labels: Optional[tuple[str, str]] = None) -> str:
    """A signal's level over an event with its normal band (shaded) and markers where it is outside the band."""
    vals = [float(v) for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if len(vals) < 2:
        return ""
    lo_b, hi_b = band if band else (None, None)
    vmin = min(vals + ([lo_b] if lo_b is not None else []))
    vmax = max(vals + ([hi_b] if hi_b is not None else []))
    span = (vmax - vmin) or 1.0
    pad_l, pad_r, pad_t, pad_b = 52, 8, 14, 18
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(vals)
    x = lambda i: pad_l + w * i / max(1, n - 1)  # noqa: E731
    y = lambda v: pad_t + h - (v - vmin) / span * h  # noqa: E731
    parts = [f'<svg class="spark" viewBox="0 0 {width} {height}" width="100%" preserveAspectRatio="none" role="img" aria-label="{escape(label)}">']
    parts.append(f'<rect x="{pad_l}" y="{pad_t}" width="{w}" height="{h}" fill="#fff" stroke="{PALETTE["grid"]}"/>')
    if lo_b is not None and hi_b is not None:
        parts.append(f'<rect x="{pad_l}" y="{y(hi_b):.1f}" width="{w}" height="{max(1.0, y(lo_b) - y(hi_b)):.1f}" fill="{PALETTE["primary_soft"]}" opacity="0.55"><title>normal band</title></rect>')
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    parts.append(f'<polyline points="{pts}" fill="none" stroke="{PALETTE["primary"]}" stroke-width="1.4" vector-effect="non-scaling-stroke"/>')
    for i in marks:
        if 0 <= i < n:
            parts.append(f'<circle cx="{x(i):.1f}" cy="{y(vals[i]):.1f}" r="2.2" fill="{PALETTE["fail"]}"/>')
    parts.append(f'<text x="{pad_l - 4}" y="{pad_t + 4}" font-size="10" text-anchor="end" fill="{PALETTE["muted"]}">{_fmt(vmax)}</text>')
    parts.append(f'<text x="{pad_l - 4}" y="{pad_t + h}" font-size="10" text-anchor="end" fill="{PALETTE["muted"]}">{_fmt(vmin)}</text>')
    if x_labels:
        parts.append(f'<text x="{pad_l}" y="{height - 4}" font-size="10" fill="{PALETTE["muted"]}">{escape(str(x_labels[0]))}</text>')
        parts.append(f'<text x="{pad_l + w}" y="{height - 4}" font-size="10" text-anchor="end" fill="{PALETTE["muted"]}">{escape(str(x_labels[1]))}</text>')
    if label:
        parts.append(f'<text x="{pad_l + 4}" y="{pad_t + 12}" font-size="11" fill="{PALETTE["ink"]}" font-weight="600">{escape(label)}</text>')
    parts.append("</svg>")
    return "".join(parts)
