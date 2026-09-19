"""GET /api/runs/{id}/advice: why a problem is a problem and what to do about it (tpm/api/advice.py), for one
check, flag, diagnosis or batch of a run. Deterministic templates in en / fi / sv; never a model call."""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query

KINDS = ("check", "diagnosis", "flag", "batch")


def register(app: FastAPI, state: Any) -> None:
    from .advice import advice_for_object, find_object, kind_of_id

    @app.get("/api/runs/{run_id}/advice")
    def get_advice(run_id: str, id: str = Query(..., min_length=1, max_length=64), kind: str = Query(""), lang: str = Query("en")) -> dict[str, Any]:
        ws = state.ws(run_id)
        k = {"diag": "diagnosis", "trust": "batch"}.get(kind.strip().lower(), kind.strip().lower()) or (kind_of_id(id) or "")
        if k not in KINDS:
            raise HTTPException(400, f"unknown kind {kind!r}; choose one of {list(KINDS)}")
        obj = find_object(ws, k, id)
        if obj is None:
            raise HTTPException(404, f"{id} was not found in run {run_id}")
        adv = advice_for_object(ws, k, obj, lang)
        oid = obj.get("check_id") or obj.get("id") or obj.get("batch_id")
        return {"id": oid, "kind": k, "subtype": adv["key"].split(".", 1)[1], "language": (lang or "en").lower()[:2] if (lang or "en").lower()[:2] in ("en", "fi", "sv") else "en",
                "why": adv["why"], "fix": adv["fix"], "can_use_rows": adv["can_use_rows"], "source": "template"}
