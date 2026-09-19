"""Unnamed fault patterns: cluster flagged events by their attribution signature (ranked signals, directions,
lag order) into PATTERN-A, PATTERN-B, ... The operator may name a pattern later (apply_override).

A pseudo-label LightGBM classifier is trained on the event signatures with cross-validation; its balanced
accuracy is reported as `classifier_reliability` so nobody mistakes cluster ids for ground truth.
"""
from __future__ import annotations

import string
from typing import Any, Optional

import numpy as np

from ..contracts import FaultPattern, Flag

DIRS = ("up", "down", "noisy", "stuck", "shifted")
CAUSES = ("process", "sensor", "data", "mixed", "unknown")
DIR_WEIGHT = 0.6
CAUSE_WEIGHT = 0.3


def signature_vector(flag: Flag, aliases: list[str]) -> np.ndarray:
    """[shares per signal | DIR_WEIGHT * shares split by direction | CAUSE_WEIGHT * cause one-hot].
    Which signals deviate matters most; how they deviate and the cause class refine the grouping."""
    idx = {a: i for i, a in enumerate(aliases)}
    p = len(aliases)
    v = np.zeros(p + len(DIRS) * p + len(CAUSES), dtype=np.float32)
    for sc in flag.signals_ranked:
        j = idx.get(sc.signal)
        if j is None:
            continue
        d = DIRS.index(sc.direction) if sc.direction in DIRS else DIRS.index("shifted")
        v[j] += float(sc.contribution)
        v[p + d * p + j] += DIR_WEIGHT * float(sc.contribution)
    c = CAUSES.index(flag.likely_cause_class) if flag.likely_cause_class in CAUSES else CAUSES.index("unknown")
    v[p + len(DIRS) * p + c] = CAUSE_WEIGHT
    return v


def _split(v: np.ndarray, p: int) -> tuple[np.ndarray, np.ndarray]:
    """(per-signal shares, direction-split shares (len(DIRS), p)) from a signature vector."""
    return v[:p], v[p : p + len(DIRS) * p].reshape(len(DIRS), p) / DIR_WEIGHT


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(a @ b / (na * nb))


def _cluster(V: np.ndarray, min_events: int, seed: int = 0) -> tuple[np.ndarray, dict[str, Any]]:
    from sklearn.cluster import AgglomerativeClustering
    from sklearn.metrics import silhouette_score

    n = len(V)
    kmax = int(min(8, n // max(1, min_events)))
    if n < 2 * min_events or kmax < 2:
        return np.zeros(n, dtype=int), {"k": 1, "note": "too few events to cluster"}
    best_k, best_s, best_lab = None, -2.0, None
    sil: dict[int, float] = {}
    for k in range(2, kmax + 1):
        try:
            lab = AgglomerativeClustering(n_clusters=k, metric="cosine", linkage="average").fit_predict(V)
            s = float(silhouette_score(V, lab, metric="cosine")) if len(set(lab)) > 1 else -1.0
        except Exception:
            continue
        sil[k] = round(s, 3)
        if s > best_s:
            best_k, best_s, best_lab = k, s, lab
    if best_lab is None:
        return np.zeros(n, dtype=int), {"k": 1, "note": "clustering failed"}
    return best_lab, {"k": int(best_k), "silhouette": round(best_s, 3), "silhouette_by_k": sil}


def _classifier_reliability(V: np.ndarray, y: np.ndarray, seed: int = 0) -> tuple[Optional[float], str]:
    classes, counts = np.unique(y, return_counts=True)
    if len(classes) < 2 or counts.min() < 2:
        return None, "not enough labelled events per pattern for cross-validation"
    n_splits = int(min(3, counts.min()))
    try:
        from lightgbm import LGBMClassifier
        from sklearn.metrics import balanced_accuracy_score
        from sklearn.model_selection import StratifiedKFold, cross_val_predict

        clf = LGBMClassifier(n_estimators=60, num_leaves=7, min_child_samples=2, learning_rate=0.1, verbose=-1, random_state=seed)
        pred = cross_val_predict(clf, V, y, cv=StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=seed))
        return float(balanced_accuracy_score(y, pred)), f"LightGBM, {n_splits}-fold stratified CV, balanced accuracy"
    except Exception as e:
        try:
            from sklearn.linear_model import LogisticRegression
            from sklearn.metrics import balanced_accuracy_score
            from sklearn.model_selection import StratifiedKFold, cross_val_predict

            pred = cross_val_predict(LogisticRegression(max_iter=500), V, y, cv=StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=seed))
            return float(balanced_accuracy_score(y, pred)), f"logistic regression fallback ({e})"
        except Exception as e2:
            return None, f"classifier failed: {e2}"


def build_patterns(ws, settings, flags: list[Flag], aliases: list[str], seed: int = 0) -> tuple[list[FaultPattern], dict[str, str], dict[str, Any]]:
    min_events = int(settings.detect.pattern_min_events)
    events = [f for f in flags if f.kind in ("anomaly", "drift") and f.signals_ranked]
    meta: dict[str, Any] = {"n_events": len(events), "min_events_per_pattern": min_events}
    if len(events) < max(2, min_events):
        meta["note"] = "too few events for patterns"
        ws.write_json("patterns", [])
        return [], {}, meta
    V = np.stack([signature_vector(f, aliases) for f in events])
    lab, cmeta = _cluster(V, min_events, seed)
    meta.update(cmeta)
    # order clusters by size, drop too-small ones
    ids, counts = np.unique(lab, return_counts=True)
    order = [int(i) for i in ids[np.argsort(-counts)]]
    letters = list(string.ascii_uppercase) + [f"A{c}" for c in string.ascii_uppercase]
    patterns: list[FaultPattern] = []
    assign: dict[str, str] = {}
    label_of_cluster: dict[int, str] = {}
    p = len(aliases)
    for k, c in enumerate(order):
        members = [events[i] for i in np.flatnonzero(lab == c)]
        if len(members) < min_events:
            continue
        # cohesion gate: "recurring pattern" only when the events share their lead signals. Share of members whose
        # top signal is the cluster's most common top signal, and mean overlap of the members' top-3 sets.
        tops = [f.signals_ranked[0].signal for f in members if f.signals_ranked]
        top3 = [frozenset(sc.signal for sc in f.signals_ranked[:3]) for f in members if f.signals_ranked]
        lead_share = (max(tops.count(t) for t in set(tops)) / len(tops)) if tops else 0.0
        common3 = frozenset(a for a in set().union(*top3) if sum(a in s3 for s3 in top3) >= 0.5 * len(top3)) if top3 else frozenset()
        overlap = float(np.mean([len(s3 & common3) / max(1, len(s3 | common3)) for s3 in top3])) if top3 and common3 else 0.0
        if lead_share < 0.5 and overlap < 0.5:
            meta.setdefault("rejected_clusters", []).append({"n_events": len(members), "lead_share": round(lead_share, 3), "top3_overlap": round(overlap, 3), "reason": "events have different lead signals: not a recurring pattern"})
            continue
        pid = f"PATTERN-{letters[len(patterns)]}"
        label_of_cluster[c] = pid
        centroid = V[lab == c].mean(axis=0)
        per_signal, by_dir = _split(centroid, p)
        top = np.argsort(-per_signal)[:5]
        top = [int(j) for j in top if per_signal[j] > 0.02]
        directions = {aliases[j]: DIRS[int(np.argmax(by_dir[:, j]))] for j in top}
        lags: dict[str, list[int]] = {}
        for f in members:
            for sc in f.signals_ranked:
                if sc.lag is not None:
                    lags.setdefault(sc.signal, []).append(int(sc.lag))
        lag_order = sorted(((a, float(np.median(v))) for a, v in lags.items() if a in directions), key=lambda t: t[1])
        groups = sorted({f.group_id for f in members if f.group_id})
        causes = {}
        for f in members:
            causes[f.likely_cause_class] = causes.get(f.likely_cause_class, 0) + 1
        desc = f"{len(members)} events in {len(groups)} group(s); leading signals " + ", ".join(f"{aliases[j]} ({directions[aliases[j]]}, {per_signal[j]:.0%})" for j in top[:3])
        if lag_order and len(lag_order) > 1:
            desc += "; onset order " + " -> ".join(f"{a} ({lg:+.0f})" for a, lg in lag_order[:4])
        desc += f"; dominant cause class {max(causes, key=causes.get)}."
        ev = ws.evidence.add("pattern", f"{pid}: {desc}", signals=[aliases[j] for j in top], values={"n_events": len(members), "groups": groups[:50], "mean_shares": {aliases[j]: round(float(per_signal[j]), 3) for j in top}, "directions": directions, "lag_order": lag_order, "cause_classes": causes}, computed_by="detect.patterns.build_patterns", n_samples=len(members))
        sig = {"ranked_signals": [aliases[j] for j in top], "directions": directions, "lag_order": [[a, lg] for a, lg in lag_order], "mean_shares": {aliases[j]: round(float(per_signal[j]), 3) for j in top}, "centroid": [round(float(x), 4) for x in centroid.tolist()], "aliases": aliases, "cause_classes": causes}
        conf = float(np.clip(0.35 + 0.4 * max(0.0, cmeta.get("silhouette", 0.0)) + 0.05 * min(5, len(members)), 0.1, 0.9))
        pat = FaultPattern(id=pid, signature=sig, n_events=len(members), groups_affected=groups, description=desc, confidence=round(conf, 3), evidence_ids=[ev.id])
        patterns.append(pat)
        for f in members:
            assign[f.id] = pid
    # classifier reliability on the assigned events
    y = np.array([label_of_cluster.get(int(c), "") for c in lab])
    keep = y != ""
    rel, how = (None, "no patterns")
    if keep.sum() >= 4 and len(set(y[keep])) >= 2:
        rel, how = _classifier_reliability(V[keep], y[keep], seed)
    for pat in patterns:
        pat.classifier_reliability = None if rel is None else round(rel, 3)
    meta.update({"n_patterns": len(patterns), "n_unassigned_events": int((~keep).sum()), "classifier_reliability": None if rel is None else round(rel, 3), "classifier": how})
    ws.write_json("patterns", [p.model_dump() for p in patterns])
    for pat in patterns:
        ws.log.record("system:detect", "pattern", "pattern", pat.id, {"n_events": pat.n_events, "description": pat.description, "classifier_reliability": pat.classifier_reliability}, pat.evidence_ids)
    return patterns, assign, meta


def assign_to_pattern(flag: Flag, patterns: list[FaultPattern], min_similarity: float = 0.6) -> Optional[str]:
    """Nearest-centroid assignment for streaming flags."""
    best, best_s = None, min_similarity
    for pat in patterns:
        cen = pat.signature.get("centroid")
        aliases = pat.signature.get("aliases")
        if not cen or not aliases:
            continue
        s = _cosine_sim(signature_vector(flag, aliases), np.asarray(cen, dtype=np.float32))
        if s > best_s:
            best, best_s = pat.id, s
    return best


def name_pattern(ws, pattern_id: str, name: str) -> dict[str, Any]:
    pats = ws.read_json("patterns", [])
    found = False
    for p in pats:
        if p.get("id") == pattern_id:
            p["name"] = name
            found = True
    if found:
        ws.write_json("patterns", pats)
    return {"pattern_id": pattern_id, "name": name, "found": found}
