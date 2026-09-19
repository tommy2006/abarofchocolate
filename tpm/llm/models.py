"""Local model management: which Ollama models this machine has, which one the app uses, pulling more with
progress, and getting Ollama itself onto a machine that does not have it.

The app no longer expects one particular model. Order of choice for the chat model:
  1. the model somebody picked in the UI / CLI (if it is still installed),
  2. the configured default or one of its fallbacks (if installed),
  3. the best installed chat model that fits this machine's memory (`local_llm.auto_select`).
The embedding model follows the same idea; without any embedding model, search falls back to TF-IDF.

Everything here talks to the local Ollama server only. The two downloads (Ollama itself from ollama.com, models
from the Ollama registry) fetch software; no data of the operator is sent anywhere.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx

from ..config import Settings, get_settings

MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,80}(:[A-Za-z0-9][A-Za-z0-9._-]{0,60})?$")
OLLAMA_WINDOWS_INSTALLER = "https://ollama.com/download/OllamaSetup.exe"
OLLAMA_DOWNLOAD_PAGE = "https://ollama.com/download"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Suggestions shown next to the free-text box (any name from ollama.com/library works). Sizes are download sizes.
SUGGESTED = [
    {"name": "gemma4:e4b-it-qat", "kind": "chat", "size_gb": 6.1, "note": "default of this app: best answers that still fit a 16 GB laptop"},
    {"name": "qwen3:8b", "kind": "chat", "size_gb": 5.2, "note": "strong general model, good at structured answers"},
    {"name": "qwen3:4b", "kind": "chat", "size_gb": 2.6, "note": "smaller and faster; fine for 8 GB machines"},
    {"name": "llama3.2:3b", "kind": "chat", "size_gb": 2.0, "note": "small and quick"},
    {"name": "gemma3:1b", "kind": "chat", "size_gb": 0.8, "note": "very small; quick download for a demo, simple answers"},
    {"name": "nomic-embed-text", "kind": "embedding", "size_gb": 0.27, "note": "default search model of this app"},
    {"name": "all-minilm", "kind": "embedding", "size_gb": 0.05, "note": "tiny search model"},
]
EMBED_HINTS = ("embed", "minilm", "bge-", "bge:", "e5-", "gte-", "arctic-embed", "paraphrase")
EMBED_PREFERENCE = ("nomic-embed-text", "embeddinggemma", "mxbai-embed-large", "bge-m3", "snowflake-arctic-embed", "all-minilm")


def _base_url(settings: Settings) -> str:
    from .providers import _loopback

    return _loopback(settings.local_llm.base_url.rstrip("/"))


def valid_name(name: str) -> bool:
    return bool(name) and bool(MODEL_NAME_RE.match(name)) and ".." not in name


# ----------------------------------------------------------------------------------------------
# Ollama itself
# ----------------------------------------------------------------------------------------------


def ollama_binary() -> Optional[str]:
    found = shutil.which("ollama")
    if found:
        return found
    cands: list[Path] = []
    if sys.platform == "win32":
        for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                cands += [Path(base) / "Programs" / "Ollama" / "ollama.exe", Path(base) / "Ollama" / "ollama.exe"]
    else:
        cands += [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama"), Path("/opt/homebrew/bin/ollama"), Path("/Applications/Ollama.app/Contents/Resources/ollama")]
    for c in cands:
        if c.exists():
            return str(c)
    return None


def ollama_state(settings: Optional[Settings] = None) -> dict[str, Any]:
    settings = settings or get_settings()
    base = _base_url(settings)
    version = None
    try:
        r = httpx.get(f"{base}/api/version", timeout=2.0)
        if r.status_code == 200:
            version = str(r.json().get("version", "")) or "unknown"
    except Exception:
        version = None
    binary = ollama_binary()
    return {"installed": bool(binary) or version is not None, "running": version is not None, "version": version, "binary": binary, "base_url": base, "platform": sys.platform}


def start_ollama(settings: Optional[Settings] = None, wait_s: float = 20.0) -> dict[str, Any]:
    """Start the local Ollama server when it is installed but not running."""
    st = ollama_state(settings)
    if st["running"]:
        return {**st, "started": False}
    if not st["binary"]:
        return {**st, "started": False, "error": "Ollama is not installed on this computer."}
    try:
        flags = (0x00000008 | NO_WINDOW) if sys.platform == "win32" else 0  # DETACHED_PROCESS
        subprocess.Popen([st["binary"], "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    except OSError as e:
        return {**st, "started": False, "error": f"could not start Ollama: {e}"}
    deadline = time.time() + wait_s
    while time.time() < deadline:
        st = ollama_state(settings)
        if st["running"]:
            return {**st, "started": True}
        time.sleep(0.5)
    return {**st, "started": False, "error": "Ollama was started but did not answer in time; try again in a moment."}


def machine() -> dict[str, Any]:
    out: dict[str, Any] = {"ram_gb": None, "ram_free_gb": None, "gpu": None, "disk_free_gb": None}
    try:
        import psutil

        vm = psutil.virtual_memory()
        out["ram_gb"] = round(vm.total / 1e9, 1)
        out["ram_free_gb"] = round(vm.available / 1e9, 1)
    except Exception:
        pass
    try:
        home = Path(os.environ.get("OLLAMA_MODELS") or Path.home())
        out["disk_free_gb"] = round(shutil.disk_usage(home if home.exists() else Path.home()).free / 1e9, 1)
    except Exception:
        pass
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            r = subprocess.run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=4, creationflags=NO_WINDOW)
            line = (r.stdout or "").strip().splitlines()[0]
            name, mem = [x.strip() for x in line.rsplit(",", 1)]
            out["gpu"] = {"name": name, "vram_gb": round(float(mem) / 1024, 1)}
        except Exception:
            pass
    return out


_MACHINE: dict[str, Any] = {}


def machine_cached(max_age_s: float = 60.0) -> dict[str, Any]:
    if not _MACHINE or time.time() - _MACHINE.get("_t", 0) > max_age_s:
        _MACHINE.clear()
        _MACHINE.update(machine())
        _MACHINE["_t"] = time.time()
    return {k: v for k, v in _MACHINE.items() if k != "_t"}


# ----------------------------------------------------------------------------------------------
# installed models and the choice between them
# ----------------------------------------------------------------------------------------------

_SHOW_CACHE: dict[str, dict[str, Any]] = {}  # digest -> /api/show excerpt


def _show(base: str, name: str, digest: str) -> dict[str, Any]:
    key = digest or name
    if key in _SHOW_CACHE:
        return _SHOW_CACHE[key]
    info: dict[str, Any] = {}
    try:
        r = httpx.post(f"{base}/api/show", json={"model": name}, timeout=5.0)
        if r.status_code == 200:
            d = r.json()
            info = {"capabilities": list(d.get("capabilities") or []), "details": d.get("details") or {}}
    except Exception:
        info = {}
    if info:
        _SHOW_CACHE[key] = info
    return info


def _params_b(text: Any) -> Optional[float]:
    """'8.0B' -> 8.0, '137M' -> 0.137."""
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([BbMmKk])", str(text or ""))
    if not m:
        return None
    v = float(m.group(1))
    return v * {"b": 1.0, "m": 1e-3, "k": 1e-6}[m.group(2).lower()]


def classify(name: str, capabilities: list[str], family: str = "") -> str:
    caps = {c.lower() for c in capabilities or []}
    if "embedding" in caps and "completion" not in caps:
        return "embedding"
    if "completion" in caps:
        return "chat"
    low = name.lower()
    if any(h in low for h in EMBED_HINTS) or "bert" in (family or "").lower():
        return "embedding"
    return "chat"


def installed_models(settings: Optional[Settings] = None) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    base = _base_url(settings)
    try:
        r = httpx.get(f"{base}/api/tags", timeout=3.0)
        r.raise_for_status()
        tags = r.json().get("models", []) or []
    except Exception:
        return []
    out = []
    for m in tags:
        name = str(m.get("name") or m.get("model") or "")
        if not name:
            continue
        details = m.get("details") or {}
        show = _show(base, name, str(m.get("digest") or ""))
        caps = list(m.get("capabilities") or show.get("capabilities") or [])
        details = {**(show.get("details") or {}), **details}
        size_gb = round(float(m.get("size") or 0) / 1e9, 2)
        out.append({
            "name": name,
            "kind": classify(name, caps, str(details.get("family") or "")),
            "size_gb": size_gb,
            "parameters": details.get("parameter_size"),
            "parameters_b": _params_b(details.get("parameter_size")),
            "quantization": details.get("quantization_level"),
            "family": details.get("family"),
            "capabilities": caps,
            "modified_at": m.get("modified_at"),
        })
    return out


def memory_limits(mach: dict[str, Any]) -> tuple[float, float]:
    """(fast_gb, max_gb): a model up to fast_gb runs fully on the GPU; up to max_gb it still runs (partly on CPU)."""
    ram = float(mach.get("ram_gb") or 8.0)
    vram = float((mach.get("gpu") or {}).get("vram_gb") or 0.0)
    return (vram * 0.95 if vram else ram * 0.35), max(ram * 0.7, vram * 0.95)


def _need_gb(size_gb: float) -> float:
    """Memory a model takes while answering: its weights plus working memory for the conversation."""
    return size_gb * 1.1 + 0.5


def rank_chat(models: list[dict[str, Any]], mach: dict[str, Any]) -> list[dict[str, Any]]:
    """Best first. Bigger models answer better, as long as they fit; a model that needs more memory than the
    machine has is last, however good it is."""
    fast, mx = memory_limits(mach)
    ranked = []
    for m in models:
        if m.get("kind") != "chat":
            continue
        need = _need_gb(float(m.get("size_gb") or 0))
        fit = "fast" if need <= fast else ("slow" if need <= mx else "too_big")
        quality = math.log2(max(0.1, float(m.get("parameters_b") or (m.get("size_gb") or 1) / 0.7)))
        caps = {c.lower() for c in m.get("capabilities") or []}
        score = {"fast": 20.0, "slow": 10.0, "too_big": 0.0}[fit] + quality + (0.5 if "tools" in caps else 0.0)
        if fit == "too_big":
            score -= need  # among models that do not fit, the smallest hurts least
        why = {"fast": "fits this computer's memory comfortably", "slow": "fits, but will answer slowly on this computer", "too_big": "needs more memory than this computer has"}[fit]
        ranked.append({**m, "fit": fit, "score": round(score, 2), "why": why})
    return sorted(ranked, key=lambda x: (-x["score"], x["name"]))


def _find(models: list[dict[str, Any]], wanted: str, kind: Optional[str] = None) -> Optional[str]:
    from .providers import _same_model

    for m in models:
        if (kind is None or m.get("kind") == kind) and _same_model(m["name"], wanted):
            return m["name"]
    return None


def choose(settings: Optional[Settings] = None, models: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """Which installed chat / embedding model the app uses right now, and why (shown in the UI)."""
    settings = settings or get_settings()
    cfg = settings.local_llm
    models = installed_models(settings) if models is None else models
    out: dict[str, Any] = {"chat": None, "chat_reason": "no chat model is installed", "chat_source": "none", "embedding": None, "embedding_reason": "no search model is installed: search uses word matching instead", "embedding_source": "none"}
    if not models:
        return out
    by_user = getattr(cfg, "model_selected_by", "config") == "user"
    hit = _find(models, cfg.model)
    if hit:
        out.update(chat=hit, chat_source="user" if by_user else "configured", chat_reason="chosen by you" if by_user else "the app's default model")
    else:
        for fb in cfg.fallback_models:
            hit = _find(models, fb)
            if hit:
                out.update(chat=hit, chat_source="fallback", chat_reason=f"{cfg.model} is not installed; using the configured alternative")
                break
    if not out["chat"] and getattr(cfg, "auto_select", True):
        ranked = rank_chat(models, machine_cached())
        if ranked:
            best = ranked[0]
            out.update(chat=best["name"], chat_source="auto", chat_reason=f"picked automatically: the best installed model for this computer ({best['why']})")
    emb_user = getattr(cfg, "embedding_selected_by", "config") == "user"
    hit = _find(models, cfg.embedding_model)
    if hit:
        out.update(embedding=hit, embedding_source="user" if emb_user else "configured", embedding_reason="chosen by you" if emb_user else "the app's default search model")
    elif getattr(cfg, "auto_select", True):
        embs = [m["name"] for m in models if m.get("kind") == "embedding"]
        if embs:
            pref = sorted(embs, key=lambda n: next((i for i, p in enumerate(EMBED_PREFERENCE) if n.lower().startswith(p)), len(EMBED_PREFERENCE)))
            out.update(embedding=pref[0], embedding_source="auto", embedding_reason="picked automatically from the installed search models")
    return out


def overview(settings: Optional[Settings] = None) -> dict[str, Any]:
    """Everything the Models panel needs in one call."""
    settings = settings or get_settings()
    st = ollama_state(settings)
    mach = machine_cached()
    models = installed_models(settings) if st["running"] else []
    ranked = {m["name"]: m for m in rank_chat(models, mach)}
    chosen = choose(settings, models)
    for m in models:
        r = ranked.get(m["name"])
        m["fit"] = r["fit"] if r else None
        m["why"] = r["why"] if r else None
        m["in_use"] = m["name"] in (chosen["chat"], chosen["embedding"])
    have = {m["name"] for m in models}
    from .providers import _same_model

    fast, mx = memory_limits(mach)
    suggestions = []
    for s in SUGGESTED:
        need = _need_gb(s["size_gb"])
        suggestions.append({**s, "installed": any(_same_model(h, s["name"]) for h in have), "fit": "fast" if need <= fast else ("slow" if need <= mx else "too_big") if s["kind"] == "chat" else "fast"})
    if not st["installed"]:
        step = "install_ollama"
    elif not st["running"]:
        step = "start_ollama"
    elif not chosen["chat"]:
        step = "pull_chat_model"
    elif not chosen["embedding"]:
        step = "pull_embedding_model"
    else:
        step = "ready"
    return {
        "ollama": st, "machine": mach, "models": models, "selected": chosen, "suggested": suggestions, "next_step": step,
        "configured": {"model": settings.local_llm.model, "embedding_model": settings.local_llm.embedding_model, "auto_select": getattr(settings.local_llm, "auto_select", True),
                       "model_selected_by": getattr(settings.local_llm, "model_selected_by", "config"), "embedding_selected_by": getattr(settings.local_llm, "embedding_selected_by", "config")},
        "defaults": {"chat": "gemma4:e4b-it-qat", "embedding": "nomic-embed-text"},
        "pulls": PULLS.list(), "install": INSTALL.status(),
        "download_page": OLLAMA_DOWNLOAD_PAGE,
    }


def select(kind: str, name: str, settings: Optional[Settings] = None, settings_path: Optional[Path] = None) -> dict[str, Any]:
    """Persist the user's choice ("auto" returns to automatic selection). Returns the override that was saved."""
    from ..config import save_settings_overrides

    settings = settings or get_settings()
    if kind not in ("chat", "embedding"):
        raise ValueError("kind must be 'chat' or 'embedding'")
    key, by = ("model", "model_selected_by") if kind == "chat" else ("embedding_model", "embedding_selected_by")
    if name == "auto":
        default = "gemma4:e4b-it-qat" if kind == "chat" else "nomic-embed-text"
        override = {"local_llm": {key: default, by: "config", "auto_select": True}}
    else:
        if not valid_name(name):
            raise ValueError("that is not a valid model name")
        models = installed_models(settings)
        hit = _find(models, name)
        if not hit:
            raise ValueError(f"{name} is not installed on this computer; download it first")
        if kind == "embedding" and next(m for m in models if m["name"] == hit)["kind"] != "embedding":
            raise ValueError(f"{hit} is a chat model, not a search (embedding) model")
        if kind == "chat" and next(m for m in models if m["name"] == hit)["kind"] != "chat":
            raise ValueError(f"{hit} is a search (embedding) model and cannot answer questions")
        override = {"local_llm": {key: hit, by: "user"}}
    save_settings_overrides(override, settings_path)
    return override


# ----------------------------------------------------------------------------------------------
# pulling models (progress is polled by the UI)
# ----------------------------------------------------------------------------------------------


class _Pulls:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public(j) for j in self._jobs.values()]

    def get(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            j = self._jobs.get(job_id)
            return self._public(j) if j else None

    @staticmethod
    def _public(j: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in j.items() if not k.startswith("_")}

    def start(self, name: str, settings: Optional[Settings] = None) -> dict[str, Any]:
        if not valid_name(name):
            raise ValueError("that is not a valid model name (example: qwen3:4b)")
        settings = settings or get_settings()
        with self._lock:
            for j in self._jobs.values():
                if j["name"] == name and j["state"] in ("queued", "running"):
                    return self._public(j)
            job = {"id": "PULL-" + uuid.uuid4().hex[:8], "name": name, "state": "queued", "status": "waiting", "percent": 0.0, "completed_gb": 0.0, "total_gb": None, "error": None, "started_at": time.time(), "finished_at": None, "_cancel": threading.Event()}
            self._jobs[job["id"]] = job
        threading.Thread(target=self._run, args=(job, _base_url(settings)), daemon=True, name=f"pull-{name}").start()
        return self._public(job)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            j = self._jobs.get(job_id)
        if not j or j["state"] not in ("queued", "running"):
            return False
        j["_cancel"].set()
        return True

    def _run(self, job: dict[str, Any], base: str) -> None:
        job["state"] = "running"
        layers: dict[str, tuple[float, float]] = {}
        try:
            with httpx.stream("POST", f"{base}/api/pull", json={"model": job["name"], "stream": True}, timeout=httpx.Timeout(None, connect=5.0)) as r:
                if r.status_code >= 400:
                    raise RuntimeError(f"Ollama answered HTTP {r.status_code}: {r.read().decode('utf-8', 'replace')[:200]}")
                for line in r.iter_lines():
                    if job["_cancel"].is_set():
                        job.update(state="cancelled", status="cancelled", finished_at=time.time())
                        return
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if ev.get("error"):
                        raise RuntimeError(str(ev["error"]))
                    job["status"] = str(ev.get("status") or job["status"])
                    if ev.get("digest") and ev.get("total"):
                        layers[str(ev["digest"])] = (float(ev.get("completed") or 0), float(ev["total"]))
                        done = sum(c for c, _ in layers.values())
                        total = sum(t for _, t in layers.values())
                        job.update(completed_gb=round(done / 1e9, 2), total_gb=round(total / 1e9, 2), percent=round(100.0 * done / total, 1) if total else 0.0)
                    if ev.get("status") == "success":
                        job.update(state="done", status="ready to use", percent=100.0, finished_at=time.time())
                        _SHOW_CACHE.clear()
                        try:
                            from .providers import OllamaProvider

                            OllamaProvider._tags_cache.clear()  # the new model is visible to the app at once
                        except Exception:
                            pass
                        return
            if job["state"] == "running":
                raise RuntimeError("the download ended before Ollama reported success")
        except Exception as e:
            msg = str(e)
            if "pull model manifest" in msg or "file does not exist" in msg or "not found" in msg.lower():
                msg = f"Ollama does not know a model called {job['name']}. Check the spelling on ollama.com/library."
            elif isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
                msg = "Ollama is not running on this computer."
            job.update(state="failed", status="failed", error=msg[:400], finished_at=time.time())


PULLS = _Pulls()


# ----------------------------------------------------------------------------------------------
# installing Ollama (Windows: download the official installer, check its signature, open it)
# ----------------------------------------------------------------------------------------------


def _signature_ok(path: Path) -> tuple[bool, str]:
    """The installer must carry a valid Authenticode signature of Ollama before it is opened."""
    script = f"$s = Get-AuthenticodeSignature -LiteralPath '{str(path).replace(chr(39), chr(39) * 2)}'; $s.Status.ToString() + '|' + $s.SignerCertificate.Subject"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
        status, _, subject = (r.stdout or "").strip().partition("|")
    except Exception as e:
        return False, f"could not check the signature: {e}"
    if status != "Valid":
        return False, f"the downloaded file's signature is '{status or 'missing'}'"
    if "ollama" not in subject.lower():
        return False, f"the file is signed by someone else: {subject[:120]}"
    return True, subject


class _Install:
    def __init__(self) -> None:
        self._job: dict[str, Any] = {"state": "idle", "status": "", "percent": 0.0, "error": None}
        self._lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._job)

    def start(self, settings: Optional[Settings] = None) -> dict[str, Any]:
        if sys.platform != "win32":
            return {"state": "manual", "status": f"Install Ollama from {OLLAMA_DOWNLOAD_PAGE} and start it, then come back to this page.", "percent": 0.0, "error": None, "url": OLLAMA_DOWNLOAD_PAGE}
        with self._lock:
            if self._job["state"] in ("downloading", "verifying", "installing"):
                return dict(self._job)
            self._job = {"state": "downloading", "status": "Downloading the Ollama installer from ollama.com", "percent": 0.0, "error": None}
        threading.Thread(target=self._run, args=(settings or get_settings(),), daemon=True, name="ollama-install").start()
        return self.status()

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self._job.update(kw)

    def _run(self, settings: Settings) -> None:
        target = Path(tempfile.gettempdir()) / "OllamaSetup.exe"
        try:
            with httpx.stream("GET", OLLAMA_WINDOWS_INSTALLER, follow_redirects=True, timeout=httpx.Timeout(60.0, connect=10.0)) as r:
                r.raise_for_status()
                total = float(r.headers.get("content-length") or 0)
                done = 0.0
                with open(target, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        self._set(percent=round(100.0 * done / total, 1) if total else 0.0, status=f"Downloading the Ollama installer ({done / 1e6:.0f} MB" + (f" of {total / 1e6:.0f} MB)" if total else ")"))
            self._set(state="verifying", status="Checking that the installer is really from Ollama", percent=100.0)
            ok, detail = _signature_ok(target)
            if not ok:
                try:
                    target.unlink()
                except OSError:
                    pass
                raise RuntimeError(f"The download was not opened because {detail}. Install Ollama yourself from {OLLAMA_DOWNLOAD_PAGE}.")
            self._set(state="installing", status="The Ollama installer is open: follow its steps. This page continues by itself when Ollama is running.")
            subprocess.Popen([str(target)], close_fds=True)
            deadline = time.time() + 1800
            while time.time() < deadline:
                if ollama_state(settings)["running"]:
                    self._set(state="done", status="Ollama is installed and running.")
                    return
                time.sleep(3.0)
            self._set(state="failed", error="Ollama did not start within 30 minutes. Start it from the Start menu, then reload this page.")
        except Exception as e:
            self._set(state="failed", error=str(e)[:400])


INSTALL = _Install()


def setup_prerequisites(settings: Optional[Settings] = None) -> dict[str, Any]:
    """One click for a fresh machine: pull the two default models that are still missing (Ollama must be running)."""
    settings = settings or get_settings()
    st = ollama_state(settings)
    if not st["running"]:
        return {"ok": False, "error": "Ollama is not running yet.", "pulls": []}
    chosen = choose(settings)
    jobs = []
    if not chosen["chat"]:
        jobs.append(PULLS.start(settings.local_llm.model if valid_name(settings.local_llm.model) else "gemma4:e4b-it-qat", settings))
    if not chosen["embedding"]:
        jobs.append(PULLS.start(settings.local_llm.embedding_model if valid_name(settings.local_llm.embedding_model) else "nomic-embed-text", settings))
    return {"ok": True, "pulls": jobs}
