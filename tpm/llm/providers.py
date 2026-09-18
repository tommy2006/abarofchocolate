"""Model providers. OllamaProvider (local, httpx), AnthropicProvider (external, anthropic SDK) and
TemplateProvider (no model). Only tpm.llm.router may instantiate the external provider for real calls.

Message format shared by all providers: [{"role": "system"|"user"|"assistant", "content": str}, ...].
chat() returns (text, parsed_json_or_None, latency_ms) and raises ProviderError on failure.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

import httpx
import numpy as np

from ..config import Settings
from ..contracts import LLMResult


class ProviderError(Exception):
    """Any provider failure the router should handle by falling back."""


class TransientError(ProviderError):
    """Retry once: connection problems, timeouts, HTTP 5xx / 429."""


# ----------------------------------------------------------------------------------------------
# JSON helpers (shared by providers, router and agent)
# ----------------------------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def extract_json(text: str) -> Optional[Any]:
    """Best-effort JSON extraction from model text: fences, <think> blocks, leading/trailing prose."""
    if not text:
        return None
    text = _THINK_RE.sub("", text).strip()
    candidates: list[str] = []
    for m in _FENCE_RE.finditer(text):
        candidates.append(m.group(1).strip())
    candidates.append(text)
    dec = json.JSONDecoder()
    for cand in candidates:
        try:
            return json.loads(cand)
        except Exception:
            pass
        for i, ch in enumerate(cand):
            if ch in "{[":
                try:
                    obj, _ = dec.raw_decode(cand[i:])
                    return obj
                except Exception:
                    continue
    return None


def _is_type(obj: Any, t: str) -> bool:
    if t == "object":
        return isinstance(obj, dict)
    if t == "array":
        return isinstance(obj, list)
    if t == "string":
        return isinstance(obj, str)
    if t == "boolean":
        return isinstance(obj, bool)
    if t == "integer":
        return isinstance(obj, int) and not isinstance(obj, bool)
    if t == "number":
        return isinstance(obj, (int, float)) and not isinstance(obj, bool)
    if t == "null":
        return obj is None
    return True


def validate_schema(obj: Any, schema: Optional[dict[str, Any]], path: str = "$") -> list[str]:
    """Minimal JSON-schema validator (type, properties, required, items, enum, min/max, additionalProperties).
    Returns a list of error strings; empty means valid."""
    if not schema:
        return []
    errors: list[str] = []
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        if not any(_is_type(obj, tt) for tt in types):
            return [f"{path}: expected {t}, got {type(obj).__name__}"]
    if "enum" in schema and obj not in schema["enum"]:
        errors.append(f"{path}: {obj!r} not in enum {schema['enum']}")
    if isinstance(obj, dict):
        for r in schema.get("required", []):
            if r not in obj:
                errors.append(f"{path}: missing required '{r}'")
        props = schema.get("properties", {}) or {}
        for k, v in obj.items():
            if k in props:
                errors.extend(validate_schema(v, props[k], f"{path}.{k}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected key '{k}'")
    elif isinstance(obj, list):
        items = schema.get("items")
        if "minItems" in schema and len(obj) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(obj) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if isinstance(items, dict):
            for i, v in enumerate(obj):
                errors.extend(validate_schema(v, items, f"{path}[{i}]"))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        if "minimum" in schema and obj < schema["minimum"]:
            errors.append(f"{path}: {obj} < minimum {schema['minimum']}")
        if "maximum" in schema and obj > schema["maximum"]:
            errors.append(f"{path}: {obj} > maximum {schema['maximum']}")
    return errors


def split_system(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    """Pull system messages out (Anthropic wants them as a separate argument)."""
    sys_parts = [m["content"] for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    return "\n\n".join(sys_parts), rest


# ----------------------------------------------------------------------------------------------
# Ollama
# ----------------------------------------------------------------------------------------------


class OllamaProvider:
    """Local model over the Ollama HTTP API. Never leaves the machine (base_url is localhost by default)."""

    _tags_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
    TAGS_TTL_S = 5.0

    def __init__(self, settings: Settings):
        self.settings = settings
        self.cfg = settings.local_llm
        self.base_url = self.cfg.base_url.rstrip("/")
        self.timeout = float(self.cfg.timeout_s)

    # ---- discovery ----
    def _tags(self, force: bool = False) -> list[dict[str, Any]]:
        now = time.time()
        cached = OllamaProvider._tags_cache.get(self.base_url)
        if cached and not force and now - cached[0] < self.TAGS_TTL_S:
            return cached[1]
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=3.0)
            r.raise_for_status()
            models = r.json().get("models", []) or []
        except Exception:
            models = []
            OllamaProvider._tags_cache[self.base_url] = (now, models)
            raise
        OllamaProvider._tags_cache[self.base_url] = (now, models)
        return models

    def is_available(self) -> bool:
        try:
            self._tags()
            return True
        except Exception:
            return False

    def list_models(self) -> list[str]:
        try:
            return [m.get("name", "") for m in self._tags()]
        except Exception:
            return []

    def capabilities(self, model: str) -> list[str]:
        try:
            for m in self._tags():
                if _same_model(m.get("name", ""), model):
                    return list(m.get("capabilities", []) or [])
        except Exception:
            pass
        return []

    def has_model(self, name: str) -> bool:
        return any(_same_model(m, name) for m in self.list_models())

    def candidate_models(self) -> list[str]:
        out = [self.cfg.model] + [m for m in self.cfg.fallback_models if m != self.cfg.model]
        return out

    def pick_model(self) -> Optional[str]:
        """Configured model if pulled, else the first pulled fallback, else None."""
        pulled = self.list_models()
        if not pulled:
            return None
        for cand in self.candidate_models():
            for p in pulled:
                if _same_model(p, cand):
                    return p
        return None

    def missing_models(self) -> list[str]:
        pulled = self.list_models()
        wanted = self.candidate_models() + [self.cfg.embedding_model]
        return [w for w in wanted if not any(_same_model(p, w) for p in pulled)]

    def pull_commands(self) -> list[str]:
        return [f"ollama pull {m}" for m in self.missing_models()]

    def has_embedding_model(self) -> bool:
        return self.has_model(self.cfg.embedding_model)

    # ---- chat ----
    def chat(
        self,
        messages: list[dict[str, str]],
        schema: Optional[dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> tuple[str, Optional[Any], int]:
        model = model or self.pick_model()
        if not model:
            raise ProviderError("no local model available (nothing pulled from the configured list)")
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "options": {
                "num_ctx": int(self.cfg.num_ctx),
                "temperature": float(self.cfg.temperature if temperature is None else temperature),
            },
        }
        if max_tokens:
            body["options"]["num_predict"] = int(max_tokens)
        if schema:
            body["format"] = schema
        if "thinking" in self.capabilities(model):
            body["think"] = False  # keep JSON clean for reasoning models (qwen3, deepseek-r1)
        t0 = time.time()
        data = self._post_with_retry("/api/chat", body)
        latency = int((time.time() - t0) * 1000)
        text = (data.get("message") or {}).get("content", "") or ""
        parsed = extract_json(text) if schema else None
        return text, parsed, latency

    def _post_with_retry(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        last: Optional[Exception] = None
        for attempt in range(2):
            try:
                r = httpx.post(f"{self.base_url}{path}", json=body, timeout=self.timeout)
                if r.status_code >= 500 or r.status_code == 429:
                    raise TransientError(f"ollama HTTP {r.status_code}: {r.text[:200]}")
                if r.status_code >= 400:
                    raise ProviderError(f"ollama HTTP {r.status_code}: {r.text[:300]}")
                return r.json()
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as e:
                last = TransientError(f"ollama unreachable: {e}")
            except TransientError as e:
                last = e
            except ProviderError:
                raise
            except Exception as e:
                raise ProviderError(f"ollama error: {e}")
            if attempt == 0:
                time.sleep(0.5)
        raise last or ProviderError("ollama failed")

    # ---- embeddings ----
    def embed(self, texts: list[str], model: Optional[str] = None) -> np.ndarray:
        model = model or self.cfg.embedding_model
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        try:
            data = self._post_with_retry("/api/embed", {"model": model, "input": texts, "keep_alive": self.cfg.keep_alive})
            vecs = data.get("embeddings")
            if vecs:
                return np.asarray(vecs, dtype=np.float32)
        except ProviderError:
            pass
        # legacy endpoint, one text at a time
        out = []
        for t in texts:
            data = self._post_with_retry("/api/embeddings", {"model": model, "prompt": t})
            out.append(data.get("embedding", []))
        return np.asarray(out, dtype=np.float32)


def _same_model(pulled: str, wanted: str) -> bool:
    if not pulled or not wanted:
        return False
    if pulled == wanted:
        return True
    p = pulled[:-7] if pulled.endswith(":latest") else pulled
    w = wanted[:-7] if wanted.endswith(":latest") else wanted
    return p == w


# ----------------------------------------------------------------------------------------------
# Anthropic (external). Only used when the active profile allows external and the guard passed.
# ----------------------------------------------------------------------------------------------


class AnthropicProvider:
    TOOL_NAME = "emit_result"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.cfg = settings.external_llm

    def is_available(self) -> bool:
        return bool(self.cfg.api_key)

    def _client(self):
        try:
            import anthropic
        except Exception as e:  # pragma: no cover
            raise ProviderError(f"anthropic SDK not installed: {e}")
        kwargs: dict[str, Any] = {"api_key": self.cfg.api_key, "timeout": float(self.cfg.timeout_s), "max_retries": 1}
        if self.cfg.base_url:
            kwargs["base_url"] = self.cfg.base_url
        return anthropic.Anthropic(**kwargs)

    def chat(
        self,
        messages: list[dict[str, str]],
        schema: Optional[dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
    ) -> tuple[str, Optional[Any], int]:
        if not self.is_available():
            raise ProviderError(f"no API key in env {self.cfg.api_key_env}")
        system, rest = split_system(messages)
        if not rest:
            raise ProviderError("no user message")
        kwargs: dict[str, Any] = {
            "model": model or self.cfg.model,
            "max_tokens": int(max_tokens or self.cfg.max_tokens),
            "messages": [{"role": m["role"], "content": m["content"]} for m in rest],
        }
        if system:
            kwargs["system"] = system
        wrapped = False
        if schema:
            tool_schema = schema
            if schema.get("type") != "object":
                tool_schema = {"type": "object", "properties": {"result": schema}, "required": ["result"]}
                wrapped = True
            kwargs["tools"] = [{"name": self.TOOL_NAME, "description": "Return the answer as structured JSON.", "input_schema": tool_schema}]
            kwargs["tool_choice"] = {"type": "tool", "name": self.TOOL_NAME}
        client = self._client()
        t0 = time.time()
        try:
            resp = client.messages.create(**kwargs)
        except Exception as e:
            name = type(e).__name__
            if name in ("APIConnectionError", "APITimeoutError", "InternalServerError", "RateLimitError"):
                raise TransientError(f"anthropic {name}: {e}")
            raise ProviderError(f"anthropic {name}: {e}")
        latency = int((time.time() - t0) * 1000)
        text_parts: list[str] = []
        parsed: Optional[Any] = None
        for block in getattr(resp, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif btype == "tool_use" and getattr(block, "name", "") == self.TOOL_NAME:
                parsed = getattr(block, "input", None)
                if wrapped and isinstance(parsed, dict) and "result" in parsed:
                    parsed = parsed["result"]
        text = "\n".join(text_parts)
        if schema and parsed is None:
            parsed = extract_json(text)
        if schema and parsed is not None and not text:
            text = json.dumps(parsed, ensure_ascii=False)
        return text, parsed, latency


# ----------------------------------------------------------------------------------------------
# Template: the "no model" provider. Always ok=False so callers use their code-generated text.
# ----------------------------------------------------------------------------------------------


class TemplateProvider:
    def is_available(self) -> bool:
        return True

    def complete(self, task: str, error: str = "no model available") -> LLMResult:
        return LLMResult(text="", data=None, source="template", model="", route="none", ok=False, error=error)

    def chat(self, messages: list[dict[str, str]], schema: Optional[dict[str, Any]] = None, max_tokens: Optional[int] = None) -> tuple[str, Optional[Any], int]:
        raise ProviderError("template provider has no model")
