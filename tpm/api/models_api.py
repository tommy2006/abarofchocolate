"""Routes of the "AI models on this computer" panel: see what is installed, choose the chat / search model,
download another model with progress, install or start Ollama. Thin wrappers over tpm.llm.models.

All handlers are plain `def` routes: they run in the server's thread pool and never block the event loop."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException


def register(app: FastAPI, state: Any) -> None:
    from ..llm import models as mm

    def _settings():
        return state.settings

    @app.get("/api/models")
    def models_overview() -> dict[str, Any]:
        return mm.overview(_settings())

    @app.post("/api/models/select")
    def models_select(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        kind = str(body.get("kind") or "chat")
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name is required (a model name, or 'auto')")
        try:
            saved = mm.select(kind, name, _settings(), state.settings_path)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, f"could not save the choice: {e}")
        s = state.reload_settings()
        try:
            from ..llm.providers import OllamaProvider

            OllamaProvider._tags_cache.clear()
        except Exception:
            pass
        for w in list(getattr(state, "workspaces", {}).values()):
            try:
                w.log.record("human:ui(reviewer)", "settings", "settings", f"local_{kind}_model", {"to": name})
            except Exception:
                pass
        return {"ok": True, "saved": saved, "selected": mm.choose(s)}

    @app.post("/api/models/pull")
    def models_pull(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()
        st = mm.ollama_state(_settings())
        if not st["running"]:
            raise HTTPException(409, "Ollama is not running on this computer. Start or install it first.")
        try:
            return mm.PULLS.start(name, _settings())
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/models/pull/{job_id}")
    def models_pull_status(job_id: str) -> dict[str, Any]:
        job = mm.PULLS.get(job_id)
        if not job:
            raise HTTPException(404, "unknown download")
        return job

    @app.delete("/api/models/pull/{job_id}")
    def models_pull_cancel(job_id: str) -> dict[str, Any]:
        return {"ok": mm.PULLS.cancel(job_id)}

    @app.post("/api/models/setup")
    def models_setup() -> dict[str, Any]:
        return mm.setup_prerequisites(_settings())

    @app.post("/api/ollama/install")
    def ollama_install() -> dict[str, Any]:
        if mm.ollama_state(_settings())["running"]:
            return {"state": "done", "status": "Ollama is already installed and running.", "percent": 100.0, "error": None}
        return mm.INSTALL.start(_settings())

    @app.get("/api/ollama/install")
    def ollama_install_status() -> dict[str, Any]:
        return mm.INSTALL.status()

    @app.post("/api/ollama/start")
    def ollama_start() -> dict[str, Any]:
        return mm.start_ollama(_settings())
