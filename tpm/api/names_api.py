"""Names people gave to signals, for the web UI: one small map per run instead of the whole signal catalog.
Renaming itself is a logged human decision (`set_name` on the signal) through the decisions route."""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI


def register(app: FastAPI, state: Any) -> None:
    from ..naming import clean_name

    @app.get("/api/runs/{run_id}/signal-names")
    def signal_names(run_id: str) -> dict[str, Any]:
        ws = state.ws(run_id)
        try:
            raw = ws.read_json("signals") or []
        except Exception:
            raw = []
        items = raw.get("signals", []) if isinstance(raw, dict) else raw
        names: dict[str, str] = {}
        headers: dict[str, str] = {}
        units: dict[str, str] = {}
        for s in items or []:
            if not isinstance(s, dict) or not s.get("id"):
                continue
            sid = str(s["id"])
            headers[sid] = str(s.get("source_column") or "") if s.get("source_column") and s.get("source_column") != sid else ""
            if s.get("display_name"):
                names[sid] = clean_name(s["display_name"])
            if s.get("display_unit"):
                units[sid] = str(s["display_unit"])[:24]
        return {"names": names, "headers": headers, "units": units, "n_signals": len(headers)}
