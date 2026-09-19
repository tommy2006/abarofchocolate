"""HTTP API of the live monitor page (``/api/live/*``). Independent of the run pipeline."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from .monitor import LiveMonitor


def create_router(get_monitor: Callable[[], LiveMonitor]) -> APIRouter:
    router = APIRouter(prefix="/api/live", tags=["live"])

    def guard(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except ValueError as e:
            raise HTTPException(400, str(e))

    def actor(body: dict[str, Any]) -> str:
        return str(body.get("actor") or "")[:80]

    @router.get("/state")
    def state() -> dict[str, Any]:
        return get_monitor().snapshot()

    @router.get("/log")
    def log(limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
        return {"entries": get_monitor().read_log(limit)}

    @router.post("/settings")
    def settings(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        m = get_monitor()
        return {"ok": True, "settings": guard(lambda: m.apply_settings(body, by="operator", name=actor(body)))}

    @router.post("/ai-settings")
    def ai_settings(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        m = get_monitor()
        return {"ok": True, **guard(lambda: m.ai_decide(str(body.get("lang") or "en")))}

    @router.post("/source/simulate")
    def simulate(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        guard(lambda: get_monitor().start_simulation(body, actor(body)))
        return {"ok": True}

    @router.post("/source/demo")
    def demo(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        guard(lambda: get_monitor().start_demo(body, actor(body)))
        return {"ok": True}

    @router.post("/source/live")
    def live(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        guard(lambda: get_monitor().start_live(body, actor(body)))
        return {"ok": True}

    @router.post("/source/stop")
    def stop(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        get_monitor().stop_source(actor(body))
        return {"ok": True}

    @router.post("/upload")
    async def upload(request: Request, name: str = Query("upload.csv")) -> dict[str, Any]:
        """A file dropped on the page, streamed to disk in pieces (it can be many gigabytes)."""
        m = get_monitor()
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name) or "upload.csv"
        m.uploads.mkdir(parents=True, exist_ok=True)
        dest = m.uploads / safe
        with open(dest, "wb") as f:
            async for chunk in request.stream():
                await run_in_threadpool(f.write, chunk)
        return {"ok": True, "path": str(dest), "size": dest.stat().st_size}

    return router
