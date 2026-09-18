"""Structural roles per signal (domain-agnostic, from fingerprints + relations) and instrument /
unit-operation HYPOTHESES (heuristics at low confidence, optional LLM enhancement through tpm.llm).

    role, conf, reasoning, alternatives = structural_role(fp, redundancy_record)
    hyps = heuristic_hypotheses(descriptor, relations)                # gated by domain likelihood in run_profile
    llm_hypotheses(ws, settings, descriptors, relations, domain, hint)  # never raises, never a fact
    apply_override(ws, settings, decision)                            # object_type signal | inference
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..contracts import SignalDescriptor

STAGE = "profile"
ACTOR = "system:profile"
HYPOTHESIS_CAP = 0.5
LLM_HYPOTHESIS_CAP = 0.6
VALID_ROLES = {"continuous_measured", "actuator_like", "held_sampled", "constant", "derived_redundant", "counter", "timestamp", "categorical", "text", "identifier", "unknown"}

SENSOR_HYPOTHESES_SCHEMA = {
    "type": "object",
    "properties": {
        "hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "signal": {"type": "string"},
                    "instrument": {"type": "string"},
                    "unit_operation": {"type": "string"},
                    "confidence": {"type": "number"},
                    "reasoning": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["signal", "instrument", "confidence"],
            },
        }
    },
    "required": ["hypotheses"],
}


def _g(fp: dict[str, Any], key: str, default: float = 0.0) -> float:
    v = fp.get(key)
    return default if v is None else float(v)


# --------------------------------------------------------------------------------------------------
# structural roles
# --------------------------------------------------------------------------------------------------
def structural_role(fp: dict[str, Any], redundancy: Optional[dict[str, Any]] = None) -> tuple[str, float, str, list[str]]:
    """Returns (role, confidence, reasoning, alternatives)."""
    n_unique = int(fp.get("n_unique") or 0)
    std = fp.get("std")
    count = int(fp.get("count") or 0)
    if count == 0:
        return "unknown", 0.3, "no non-missing values", []
    if n_unique <= 1 or (std is not None and std == 0) or (fp.get("min") is not None and fp.get("min") == fp.get("max")):
        return "constant", 0.99, "a single distinct value over the whole dataset", []
    if redundancy and redundancy.get("derived"):
        conf = 0.85 if redundancy.get("r2", 0) >= 0.9999 else 0.65
        alt = [f"{a} may be the derived one instead" for a in redundancy.get("alternatives", [])[:3]]
        return "derived_redundant", conf, f"reproduced by {' + '.join(redundancy['partners'])} with R2={redundancy['r2']:.5f}", alt
    ac1 = _g(fp, "autocorr_lag1")
    stuck = _g(fp, "stuck_fraction")
    hold = _g(fp, "hold_period", 1.0)
    hold_reg = _g(fp, "hold_regularity")
    lv_ac = fp.get("level_autocorr")
    jump = fp.get("jump_ratio")
    big = _g(fp, "large_jump_share")
    trend = _g(fp, "trend_strength")
    bounded = bool(fp.get("range_0_100"))
    integer_valued = bool(fp.get("integer_valued"))
    if integer_valued and n_unique > 20 and trend > 0.95 and ac1 > 0.99 and stuck < 0.05 and (jump is not None and jump < 0.05):
        return "counter", 0.7, "integer-valued, monotone (trend ~1) with tiny constant steps", ["continuous_measured"]
    if integer_valued and n_unique <= 10 and not bounded:
        return "categorical", 0.6, f"integer-coded with only {n_unique} distinct values", ["actuator_like (if the values are set points)"]
    if stuck >= 0.4:
        strictly_regular = hold >= 2 and hold <= 20 and hold_reg >= 0.9  # every run has (almost) the same length: a sampling cadence
        loosely_regular = hold >= 2 and hold <= 20 and hold_reg >= 0.6 and lv_ac is not None and lv_ac >= 0.5
        if strictly_regular or loosely_regular:
            conf = min(0.85, 0.5 + 0.2 * hold_reg + (0.15 * min(1.0, lv_ac) if lv_ac is not None and lv_ac > 0 else 0.0))
            why = f"value held for {hold:.0f} samples at a {'strictly' if strictly_regular else 'fairly'} regular cadence (regularity {hold_reg:.2f})"
            if lv_ac is not None:
                why += f"; successive levels autocorrelated {lv_ac:.2f}"
            return "held_sampled", conf, why + " -> slowly sampled measurement", ["actuator_like (if the levels are set points)"]
        if bounded or big >= 0.3 or (jump is not None and jump >= 0.5) or hold > 20:
            conf = min(0.8, 0.45 + 0.15 * min(1.0, big) + (0.1 if bounded else 0.0) + 0.1 * hold_reg)
            return "actuator_like", conf, f"step-like: {stuck:.0%} zero differences in runs of ~{hold:.0f} samples with large jumps (median jump {jump if jump is None else round(jump, 2)} std)" + (", bounded 0-100" if bounded else ""), ["held_sampled (if the steps are a slow analyzer)"]
        if hold >= 2:
            return "held_sampled", 0.45, f"value held for ~{hold:.0f} samples ({stuck:.0%} zero differences) but the update cadence is irregular", ["actuator_like", "continuous_measured with a stuck sensor"]
        return "continuous_measured", 0.45, f"{stuck:.0%} zero differences (quantized or frequently frozen) but no regular hold pattern", ["held_sampled"]
    if bounded and big >= 0.5 and hold >= 1.5:
        return "actuator_like", 0.55, "bounded 0-100 with mostly large step changes", ["continuous_measured"]
    conf = 0.55 + 0.25 * max(0.0, min(1.0, ac1)) + (0.05 if n_unique > 100 else 0.0)
    return "continuous_measured", min(0.9, conf), f"continuously varying measurement (lag-1 autocorrelation {ac1:.2f}, noise level {_g(fp, 'noise_level'):.2f}, {n_unique} distinct values)", ["actuator_like" if bounded else "held_sampled" if stuck > 0.2 else "unknown"]


# --------------------------------------------------------------------------------------------------
# hypotheses (heuristic layer)
# --------------------------------------------------------------------------------------------------
def heuristic_hypotheses(d: SignalDescriptor, relations: dict[str, Any]) -> list[dict[str, Any]]:
    """Low-confidence instrument / unit-operation hypotheses from structure only."""
    fp = d.fingerprint
    out: list[dict[str, Any]] = []
    role = d.structural_role
    bounded = bool(fp.get("range_0_100"))
    ac1 = _g(fp, "autocorr_lag1")
    noise = _g(fp, "noise_level", 1.0)
    if role in ("constant", "derived_redundant", "counter", "categorical", "unknown"):
        pass
    elif role == "actuator_like":
        out.append({"kind": "instrument", "value": "valve / actuator position", "confidence": 0.45 if bounded else 0.35, "reasoning": "step-like signal" + (" bounded to 0-100" if bounded else "")})
    elif role == "held_sampled":
        out.append({"kind": "instrument", "value": "analyzer / composition (slow sampled measurement)", "confidence": 0.4, "reasoning": f"sample-and-hold with period ~{fp.get('hold_period')} samples"})
    else:
        if bounded:
            out.append({"kind": "instrument", "value": "controller output / valve position", "confidence": 0.3, "reasoning": "continuous but bounded to 0-100"})
        elif noise < 0.2 and ac1 > 0.98:
            out.append({"kind": "instrument", "value": "temperature-like (slow, smooth)", "confidence": 0.3, "reasoning": f"low noise ({noise:.2f}) and very high autocorrelation ({ac1:.3f})"})
        elif noise > 0.6:
            out.append({"kind": "instrument", "value": "flow-like (fast, noisy)", "confidence": 0.3, "reasoning": f"high noise level ({noise:.2f}) relative to its variance"})
        else:
            out.append({"kind": "instrument", "value": "pressure / level-like (intermediate dynamics)", "confidence": 0.2, "reasoning": f"noise level {noise:.2f}, autocorrelation {ac1:.2f}"})
    if d.cluster_id:
        members = relations.get("clusters", {}).get(d.cluster_id, [])
        out.append({"kind": "unit_operation", "value": f"shared unit operation of cluster {d.cluster_id}", "confidence": min(0.35, 0.15 + 0.05 * len(members)), "reasoning": f"moves with {', '.join(m for m in members if m != d.id)[:120]}"})
    for h in out:
        h["confidence"] = min(HYPOTHESIS_CAP, h["confidence"])
        h["source"] = "code"
    return out


# --------------------------------------------------------------------------------------------------
# LLM enhancement
# --------------------------------------------------------------------------------------------------
_FP_KEYS = ["mean", "std", "min", "max", "q05", "q50", "q95", "missing_rate", "n_unique", "autocorr_lag1", "autocorr_lag5", "noise_level", "stuck_fraction", "hold_period", "quantization_rel", "dominant_period", "trend_strength", "distribution_shape", "boundedness", "integer_valued"]


_FP_KEYS_COMPACT = ["mean", "std", "min", "max", "autocorr_lag1", "noise_level", "stuck_fraction", "hold_period", "boundedness"]


def build_llm_payload(descriptors: list[SignalDescriptor], relations: dict[str, Any], domain: dict[str, float], hint: Optional[str]) -> dict[str, Any]:
    active = [d for d in descriptors if not d.excluded]
    keys = _FP_KEYS if len(active) <= 25 else _FP_KEYS_COMPACT  # keep the payload inside a small local context window
    sig = []
    for d in active:
        sig.append({"signal": d.id, "structural_role": d.structural_role, "structural_confidence": round(d.structural_confidence, 2), "fingerprint": {k: d.fingerprint.get(k) for k in keys if d.fingerprint.get(k) is not None}, "heuristic_instrument": d.instrument_hypothesis, "cluster_id": d.cluster_id, "related": d.related_signals[:2 if len(active) > 25 else 4], "evidence_ids": d.evidence_ids[:2]})
    return {
        "domain_likelihood": domain,
        "domain_hint": hint,
        "signals": sig,
        "relations": {"clusters": relations.get("clusters", {}), "pairs": [{k: p.get(k) for k in ("a", "b", "r", "lag")} for p in relations.get("pairs", [])[:40]], "redundancy": [{"signal": r["signal"], "partners": r["partners"], "derived": r.get("derived")} for r in relations.get("redundancy", [])[:20]]},
        "instructions": "Signals are anonymised (S01..). Using only the structure above, propose for each signal an instrument type (flow, pressure, temperature, level, composition, valve, power, speed, unknown) and a plausible unit operation, each with a confidence in [0, 0.6] and the evidence ids you relied on. These are hypotheses, not facts.",
    }


def _extract_json(text: str) -> Optional[Any]:
    if not text:
        return None
    text = text.strip()
    for cand in (text, re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()):
        try:
            return json.loads(cand)
        except Exception:
            pass
    m = re.search(r"\{.*\}", text, flags=re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    m = re.search(r"\[.*\]", text, flags=re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def llm_hypotheses(ws, settings, descriptors: list[SignalDescriptor], relations: dict[str, Any], domain: dict[str, float], hint: Optional[str] = None) -> dict[str, Any]:
    """Optional enhancement. Returns {"ok", "source", "n_accepted", "error"}; never raises."""
    from ..llm import complete

    payload = build_llm_payload(descriptors, relations, domain, hint)
    try:
        res = complete("sensor_hypotheses", payload, purpose="propose instrument/unit-operation roles", ws=ws, settings=settings, schema=SENSOR_HYPOTHESES_SCHEMA, max_tokens=1500)
    except Exception as e:  # complete() already guards, belt and braces
        return {"ok": False, "source": "template", "n_accepted": 0, "error": str(e)}
    if not res.ok:
        ws.log.record(ACTOR, "llm_unavailable", "dataset", ws.run_id, {"task": "sensor_hypotheses", "error": res.error, "route": res.route})
        return {"ok": False, "source": res.source, "n_accepted": 0, "error": res.error}
    data = res.data if isinstance(res.data, (dict, list)) else _extract_json(res.text)
    items = data.get("hypotheses") if isinstance(data, dict) else data
    if not isinstance(items, list):
        ws.log.record(ACTOR, "llm_unparseable", "dataset", ws.run_id, {"task": "sensor_hypotheses", "source": res.source, "preview": (res.text or "")[:300]})
        return {"ok": False, "source": res.source, "n_accepted": 0, "error": "unparseable response"}
    by_id = {d.id: d for d in descriptors}
    n_ok = 0
    src = res.source or "llm"
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = str(it.get("signal", "")).strip()
        d = by_id.get(sid)
        if d is None or d.excluded:
            continue
        instrument = str(it.get("instrument") or "").strip()[:80]
        unit_op = str(it.get("unit_operation") or "").strip()[:80] or None
        try:
            conf = float(it.get("confidence", 0.3))
        except (TypeError, ValueError):
            conf = 0.3
        conf = max(0.0, min(LLM_HYPOTHESIS_CAP, conf))
        if not instrument:
            continue
        ev_ids = [e for e in (it.get("evidence_ids") or []) if isinstance(e, str) and ws.evidence.get(e) is not None]
        if not ev_ids:
            ev_ids = d.evidence_ids[:3]
        reasoning = str(it.get("reasoning") or "")[:500]
        inf = ws.inferences.add(d.id, f"instrument hypothesis: {instrument}" + (f"; unit operation: {unit_op}" if unit_op else ""), status="uncertain", confidence=conf, evidence_ids=ev_ids, reasoning=reasoning or "language-model hypothesis from the anonymised signal catalog", source=src, stage=STAGE, alternatives=[f"heuristic: {d.instrument_hypothesis}"] if d.instrument_hypothesis else [])
        d.inference_ids.append(inf.id)
        ws.log.record(f"llm:{'external' if 'external' in src else 'local'}:{res.model or ''}", "inference", "inference", inf.id, {"subject": d.id, "claim": inf.claim, "confidence": conf, "ledger_id": res.ledger_id}, evidence_ids=ev_ids)
        if conf > d.instrument_confidence:
            d.instrument_hypothesis = instrument
            d.instrument_confidence = conf
        if unit_op and conf > d.unit_operation_confidence:
            d.unit_operation_hypothesis = unit_op
            d.unit_operation_confidence = conf
        n_ok += 1
    return {"ok": True, "source": src, "n_accepted": n_ok, "error": None, "ledger_id": res.ledger_id}


# --------------------------------------------------------------------------------------------------
# overrides
# --------------------------------------------------------------------------------------------------
def apply_override(ws, settings, decision) -> dict[str, Any]:
    """Human decisions on a signal (object_id = alias) or an inference (object_id = INF-...)."""
    from . import write_catalog  # late import: avoids a cycle

    descriptors = ws.signals()
    by_id = {d.id: d for d in descriptors}
    nv = dict(decision.new_value or {})
    target: Optional[SignalDescriptor] = None
    if decision.object_type == "inference":
        inf = ws.inferences.get(decision.object_id)
        if inf is None:
            return {"error": f"unknown inference {decision.object_id}"}
        inf.human_status = {"accept": "accepted", "question": "questioned", "override": "overridden", "set_role": "overridden"}.get(decision.action, inf.human_status)
        inf.human_note = decision.note
        ws.inferences.update(inf)
        target = by_id.get(inf.subject)
    else:
        target = by_id.get(decision.object_id)
    if target is None:
        return {"updated": False, "reason": "no matching signal"}
    changed: dict[str, Any] = {}
    role = nv.get("structural_role") or nv.get("role") or nv.get("human_role_override")
    if role:
        role = str(role)
        target.human_role_override = role
        if role in VALID_ROLES:
            target.structural_role = role
            target.structural_confidence = 1.0
        changed["human_role_override"] = role
        for iid in target.inference_ids:
            inf = ws.inferences.get(iid)
            if inf and inf.stage == STAGE and "structural role" in inf.claim and decision.action in ("override", "set_role"):
                inf.human_status = "overridden"
                inf.human_note = decision.note
                ws.inferences.update(inf)
    if "instrument_hypothesis" in nv:
        target.instrument_hypothesis = nv["instrument_hypothesis"]
        target.instrument_confidence = 1.0 if nv["instrument_hypothesis"] else 0.0
        changed["instrument_hypothesis"] = nv["instrument_hypothesis"]
    if "unit_operation_hypothesis" in nv:
        target.unit_operation_hypothesis = nv["unit_operation_hypothesis"]
        target.unit_operation_confidence = 1.0 if nv["unit_operation_hypothesis"] else 0.0
        changed["unit_operation_hypothesis"] = nv["unit_operation_hypothesis"]
    if "excluded" in nv:
        target.excluded = bool(nv["excluded"])
        target.excluded_reason = "operator" if target.excluded else None
        changed["excluded"] = target.excluded
    if decision.action == "accept":
        for iid in target.inference_ids:
            inf = ws.inferences.get(iid)
            if inf and inf.human_status is None and decision.object_type == "signal":
                inf.human_status = "accepted"
                ws.inferences.update(inf)
        changed["accepted"] = True
    hinf = ws.inferences.add(target.id, f"operator set {changed}" if changed else f"operator {decision.action}", status="inferred", confidence=1.0, evidence_ids=[], reasoning=decision.note or "", source="human", stage=STAGE)
    target.inference_ids.append(hinf.id)
    write_catalog(ws, descriptors)
    ws.log.record(ACTOR, "signal_override_applied", "signal", target.id, {"actor": f"human:{decision.actor_name}({decision.role})", "changed": changed, "note": decision.note, "inference": hinf.id})
    return {"updated": True, "signal": target.id, "changed": changed}
