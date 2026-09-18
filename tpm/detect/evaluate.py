"""Evaluation against label-like columns -- ONLY when schema.label_columns is non-empty.

This is the single place in the detect package that reads label columns. Labels are never used for
detection; the metrics are reported as "evaluation only". The majority label value is assumed to mean
"normal" (recorded as an assumption).
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

from ._common import _arrow, qident


def _auroc(scores: np.ndarray, positive: np.ndarray) -> Optional[float]:
    from scipy.stats import rankdata

    pos = positive.astype(bool)
    n_pos, n_neg = int(pos.sum()), int((~pos).sum())
    if n_pos == 0 or n_neg == 0:
        return None
    r = rankdata(scores.astype(np.float64))
    return float((r[pos].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def _score_table(store):
    import pyarrow as pa

    return pa.table({"__row__": pa.array(store.rows.astype(np.int64)), "ens": pa.array(store.ens.astype(np.float32))})


def run_evaluation(ws, inputs, store, flags, settings) -> Optional[dict[str, Any]]:
    labels = list(inputs.label_columns or [])
    if not labels:
        return None
    con = ws.duckdb()
    window = int(settings.detect.window)
    out: dict[str, Any] = {"note": "evaluation only; labels never used for detection", "assumption": "the label value with the lowest mean anomaly score is treated as 'normal' (see normal_choice)", "columns": {}}
    order = np.argsort(store.rows, kind="stable")
    rows_sorted = store.rows[order]
    ens_sorted = store.ens[order]
    codes_sorted = store.grp_codes[order]
    ev_flags = [f for f in flags if f.kind in ("anomaly", "drift", "changepoint")]
    flagged_groups = {f.group_id for f in ev_flags}
    for col in labels:
        try:
            top = con.execute(f"SELECT CAST({qident(col)} AS VARCHAR) AS v, COUNT(*) AS n FROM dataset GROUP BY v ORDER BY n DESC LIMIT 200").fetchall()
        except Exception as e:
            out["columns"][col] = {"error": str(e)[:200]}
            continue
        if not top or len(top) < 2:
            out["columns"][col] = {"note": "single-valued label column; nothing to evaluate"}
            continue
        n_distinct = len(top)
        # "normal" = the label value whose rows have the LOWEST mean out-of-fold score (evaluation-only heuristic;
        # the most frequent value is wrong for balanced class sets, and names like normal/0/ok cannot be relied on).
        # Ties/near-ties fall back to the most frequent value. Assumption is stated in the output.
        normal = top[0][0]
        try:
            con.register("__sc", _score_table(store))
            means = con.execute(f"SELECT CAST(d.{qident(col)} AS VARCHAR) AS v, AVG(s.ens) AS m, COUNT(*) AS n FROM dataset d JOIN __sc s ON d.__row__ = s.__row__ GROUP BY v HAVING COUNT(*) >= 100 ORDER BY m ASC").fetchall()
            con.unregister("__sc")
            if means:
                normal = means[0][0]
                out["columns"].setdefault(col, {})
                out.setdefault("normal_choice", {})[col] = {"rule": "lowest mean score", "mean_score": round(float(means[0][1]), 4), "n_rows": int(means[0][2]), "alternatives": [(v, round(float(m), 4)) for v, m, _ in means[1:4]]}
        except Exception as e:  # keep the frequency heuristic
            out.setdefault("normal_choice", {})[col] = {"rule": "most frequent (score lookup failed)", "error": str(e)[:160]}
        tbl = _arrow(con.execute(f"SELECT __row__, CASE WHEN CAST({qident(col)} AS VARCHAR) IS DISTINCT FROM {_lit(normal)} THEN 1 ELSE 0 END AS abn FROM dataset ORDER BY __row__"))
        lab_rows = tbl.column("__row__").to_numpy().astype(np.int64)
        abn = tbl.column("abn").to_numpy().astype(np.int8)
        pos = np.searchsorted(lab_rows, rows_sorted)
        pos = np.clip(pos, 0, len(lab_rows) - 1)
        ok = lab_rows[pos] == rows_sorted
        y = abn[pos][ok]
        s = ens_sorted[ok]
        g = codes_sorted[ok]
        res: dict[str, Any] = {"normal_value": normal, "n_distinct": n_distinct, "abnormal_fraction": round(float(y.mean()), 4) if len(y) else None}
        res["auroc_ensemble"] = None if len(y) == 0 else (None if _auroc(s, y) is None else round(_auroc(s, y), 4))
        flagged = s >= 1.0
        tp = int((flagged & (y == 1)).sum())
        fp = int((flagged & (y == 0)).sum())
        fn = int((~flagged & (y == 1)).sum())
        res["row_level"] = {"precision": round(tp / max(1, tp + fp), 4), "recall": round(tp / max(1, tp + fn), 4), "flagged_fraction": round(float(flagged.mean()), 4) if len(flagged) else None}
        # per-group detection / false alarms / delay
        det, fa, delays = [], [], []
        classes_by_group: dict[str, str] = {}
        # one stable sort by group keeps row order inside each group; slices replace 15M-row masks per group
        r_all = rows_sorted[ok]
        og = np.argsort(g, kind="stable")
        gs, ys, ss, rs = g[og], y[og], s[og], r_all[og]
        cut = np.flatnonzero(gs[1:] != gs[:-1]) + 1
        starts = np.concatenate([[0], cut]) if len(gs) else np.zeros(0, dtype=int)
        ends = np.concatenate([cut, [len(gs)]]) if len(gs) else np.zeros(0, dtype=int)
        for a, b in zip(starts, ends):
            group = store.groups[int(gs[a])]
            ym, sm, r = ys[a:b], ss[a:b], rs[a:b]
            has_abn = bool(ym.any())
            fm = sm >= 1.0
            is_flagged = group in flagged_groups or bool(fm.any())
            if has_abn:
                det.append(is_flagged)
                onset_i = int(np.argmax(ym == 1))
                onset_row = int(r[onset_i])
                hit = np.flatnonzero(fm[onset_i:])
                if len(hit):
                    delays.append(int(r[onset_i + hit[0]] - onset_row))
            else:
                fa.append(is_flagged)
        res["group_level"] = {"n_groups_abnormal": len(det), "detection_rate": round(float(np.mean(det)), 4) if det else None, "n_groups_normal": len(fa), "false_alarm_rate": round(float(np.mean(fa)), 4) if fa else None, "median_detection_delay_rows": (None if not delays else float(np.median(delays))), "delay_note": "delay is measured from the first labelled-abnormal row of the group; when labels are group-level (abnormal from row 0) it reflects time-to-first-flag, not true onset delay"}
        # patterns vs label classes (group level)
        if n_distinct >= 2:
            try:
                gt = con.execute(f"SELECT CAST({qident(inputs.group_col)} AS VARCHAR) AS g, CAST({qident(col)} AS VARCHAR) AS v, COUNT(*) AS n FROM dataset WHERE CAST({qident(col)} AS VARCHAR) IS DISTINCT FROM {_lit(normal)} GROUP BY g, v ORDER BY g, n DESC").fetchall() if inputs.group_col else []
                for gname, v, _n in gt:
                    classes_by_group.setdefault(str(gname), str(v))
                pat_by_group: dict[str, str] = {}
                for f in flags:
                    if f.pattern_id and f.group_id and f.group_id not in pat_by_group:
                        pat_by_group[f.group_id] = f.pattern_id
                common = [gname for gname in classes_by_group if gname in pat_by_group]
                if len(common) >= 3:
                    from sklearn.metrics import adjusted_mutual_info_score

                    a = [classes_by_group[x] for x in common]
                    b = [pat_by_group[x] for x in common]
                    ami = float(adjusted_mutual_info_score(a, b))
                    # purity of patterns wrt classes
                    from collections import Counter

                    per_pat: dict[str, Counter] = {}
                    for cls, pid in zip(a, b):
                        per_pat.setdefault(pid, Counter())[cls] += 1
                    purity = sum(c.most_common(1)[0][1] for c in per_pat.values()) / len(common)
                    res["patterns_vs_labels"] = {"n_groups": len(common), "adjusted_mutual_information": round(ami, 4), "purity": round(float(purity), 4), "pattern_class_table": {pid: dict(c) for pid, c in per_pat.items()}}
                else:
                    res["patterns_vs_labels"] = {"note": "fewer than 3 groups with both a pattern and an abnormal label"}
            except Exception as e:
                res["patterns_vs_labels"] = {"error": str(e)[:200]}
        out["columns"][col] = res
    ws.write_json("evaluation", out)
    ws.log.record("system:detect", "evaluation", "dataset", "evaluation", {k: v for k, v in out.items() if k != "columns"} | {"summary": {c: {k: v for k, v in r.items() if k in ('auroc_ensemble', 'group_level')} for c, r in out["columns"].items()}})
    return out


def _lit(v: Any) -> str:
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"
