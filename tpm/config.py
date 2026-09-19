"""Settings: config/settings.yaml + environment overrides. Profiles decide what may leave the machine."""
from __future__ import annotations

import os
from fnmatch import fnmatch
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, PrivateAttr

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SETTINGS_PATH = ROOT / "config" / "settings.yaml"


def _settings_path() -> Path:
    """TPM_SETTINGS points the app at a settings file outside the program folder (the installed desktop app keeps
    the user's copy under %LOCALAPPDATA%, so an upgrade does not reset choices made in the UI)."""
    override = os.environ.get("TPM_SETTINGS", "").strip()
    return Path(override) if override else DEFAULT_SETTINGS_PATH

# TPM_NO_DOTENV=1 (set by the test suite) keeps a developer's real keys and mail settings out of the process
if os.environ.get("TPM_NO_DOTENV", "").strip().lower() not in ("1", "true", "yes"):
    load_dotenv(ROOT / ".env", override=False)


class Profile(BaseModel):
    description: str = ""
    allow_external: bool = False
    guard_strict: bool = True
    require_custom_endpoint: bool = False  # eu-hosted: the external route needs external_llm.base_url (not anthropic.com)
    # eu-hosted: host names (fnmatch patterns) of services that run the model in the EU; any other host is refused
    eu_hosts: list[str] = Field(default_factory=list)
    # external_llm keys this profile replaces while it is active (eu-hosted brings its own endpoint, model and key)
    external_llm: dict[str, Any] = Field(default_factory=dict)
    routing: dict[str, str] = Field(default_factory=dict)


class LocalLLMConfig(BaseModel):
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"
    model: str = "gemma4:e4b-it-qat"
    fallback_models: list[str] = Field(default_factory=list)
    embedding_model: str = "nomic-embed-text"
    # "user" once somebody picked the model in the UI / CLI: that choice then beats TPM_LOCAL_MODEL from .env.
    model_selected_by: str = "config"
    embedding_selected_by: str = "config"
    # When the configured model (and every fallback) is not installed, use the best installed model that fits this
    # machine instead of running without a model: the app does not depend on one particular model being present.
    auto_select: bool = True
    keep_alive: str = "5m"
    num_ctx: int = 8192
    temperature: float = 0.2
    timeout_s: int = 180
    max_tool_steps: int = 8


# Model families that may never be used as the external model, whatever the config says (30-day data retention).
ALWAYS_BLOCKED_MODEL_PATTERNS = ("fable", "mythos")
# external_llm.provider values: the Anthropic Messages API (Anthropic, Bedrock) or an OpenAI-compatible endpoint
EXTERNAL_PROVIDERS = ("anthropic", "openai-compatible")
# Bedrock cross-region inference profiles that may run the model outside the EU (an "eu." profile stays in the EU)
NON_EU_MODEL_PREFIXES = ("global.", "us.", "us-gov.", "apac.", "au.", "jp.", "ca.")


class ExternalLLMConfig(BaseModel):
    provider: str = "anthropic"  # anthropic | openai-compatible
    model: str = "claude-sonnet-5"
    model_by_task: dict[str, str] = Field(default_factory=dict)  # optional, e.g. {critique: claude-opus-5}
    allowed_model_patterns: list[str] = Field(default_factory=lambda: ["sonnet", "opus"])
    blocked_model_patterns: list[str] = Field(default_factory=lambda: ["fable", "mythos"])
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: Optional[str] = None
    max_tokens: int = 4096  # output cap per call; thinking tokens count toward it on Sonnet 5 / Opus 5
    effort: str = "low"  # output_config.effort: low | medium | high | xhigh | max (low = fastest)
    thinking: str = "default"  # "disabled" sends thinking: disabled (fewer output tokens, faster answers)
    timeout_s: int = 60
    max_calls_per_run: int = 200  # successful external calls per run workspace (pipeline + chat)
    max_calls_per_chat_turn: int = 6
    max_output_tokens_per_run: int = 120_000
    max_parallel: int = 6  # concurrent external calls
    max_narratives_per_run: int = 12  # diagnoses that get narrative + critique when the route is external
    temperature: Optional[float] = None  # openai-compatible only (None = 0.2); Anthropic models keep their default
    operator: str = ""  # who runs the endpoint, for the data-flow statement (e.g. "the hackathon organisers on Verda")
    location: str = ""  # where the model runs, for the data-flow statement (e.g. "Finland (EU)")

    workspace_id_env: str = "ANTHROPIC_WORKSPACE_ID"  # only for API keys that are not tied to one workspace

    @property
    def api_key(self) -> Optional[str]:
        return os.environ.get(self.api_key_env)

    @property
    def workspace_id(self) -> Optional[str]:
        """Organisation-level API keys must name the workspace on every request (header anthropic-workspace-id)."""
        return (os.environ.get(self.workspace_id_env) or "").strip() or None

    def model_allowed(self, model: Optional[str]) -> tuple[bool, str]:
        """(ok, reason). The lower-cased id must contain an allowed pattern and none of the blocked ones."""
        m = (model or "").strip().lower()
        if not m:
            return False, "no external model configured"
        blocked = [p.lower() for p in list(self.blocked_model_patterns) + list(ALWAYS_BLOCKED_MODEL_PATTERNS) if p]
        hit = next((p for p in blocked if p in m), None)
        if hit:
            return False, f"model '{model}' is blocked ('{hit}' models keep data for 30 days); use claude-sonnet-5 or claude-opus-5"
        allowed = [p.lower() for p in self.allowed_model_patterns if p]
        if allowed and not any(p in m for p in allowed):
            return False, f"model '{model}' is not in the allowed families ({', '.join(allowed)})"
        return True, "ok"

    @property
    def host(self) -> str:
        """Host name of base_url, lower-case ("" without a base_url)."""
        if not self.base_url:
            return ""
        return (urlparse(self.base_url if "//" in self.base_url else "//" + self.base_url).hostname or "").lower()

    @property
    def provider_label(self) -> str:
        """Provider name for the egress ledger: with a custom endpoint its host is part of it, so every recorded call
        says where it went ("openai-compatible @ containers.datacrunch.io")."""
        return f"{self.provider} @ {self.host}" if self.host else self.provider

    def endpoint_is_first_party(self) -> bool:
        """True when calls go to Anthropic's own API (no base_url, or an anthropic.com host)."""
        if not self.base_url:
            return True
        host = self.host
        return host == "anthropic.com" or host.endswith(".anthropic.com")


class GuardConfig(BaseModel):
    min_aggregate_n: int = 30
    max_series_points: int = 20
    max_numeric_values_per_payload: int = 4000
    max_payload_bytes: int = 200_000
    allow_column_names: bool = True
    alias_column_names_in_strict: bool = True
    forbid_categorical_values: bool = True
    forbid_row_like_structures: bool = True
    external_sig_digits: int = 3  # every float that leaves is rounded to this many significant digits
    alias_names_external: bool = True  # original column names never leave, in any external profile
    drop_keys_external: list[str] = Field(default_factory=lambda: [
        "min", "max", "first", "last", "value", "values_at", "observed", "reading", "readings", "raw", "sample", "samples",
        "rows", "points", "series", "time", "timestamp", "start_time", "end_time", "t_start", "t_end", "source_path",
        "file", "filename", "path", "evaluation", "label", "labels"])


class IngestConfig(BaseModel):
    blind_mode: bool = True
    max_memory_fraction: float = 0.35
    chunk_rows: int = 200_000
    sample_rows_for_typing: int = 50_000
    max_columns: int = 5000
    parquet_compression: str = "zstd"
    dtype_downcast: str = "float32"


class BatchConfig(BaseModel):
    window_seconds: float = 300.0
    fallback_fraction: float = 0.10
    min_rows: int = 100
    max_rows: int = 2_000_000


class QualityConfig(BaseModel):
    stuck_min_run: int = 30
    stuck_fraction_warn: float = 0.5
    missing_warn: float = 0.02
    missing_fail: float = 0.20
    range_sigma: float = 6.0
    spike_sigma: float = 10.0  # local spike: distance from the rolling median, in units of local noise (residuals are heavy-tailed: 6 gave false alarms)
    spike_min_scale: float = 0.15  # ...and at least this share of the signal's own robust spread
    spike_window: int = 7  # rolling-median window (samples)
    spike_max_len: int = 2  # a local spike is at most this many consecutive readings
    gap_factor: float = 3.0
    trust_fail_threshold: float = 0.5
    critical_signal_fraction: float = 0.3


class RulesConfig(BaseModel):
    allow_role_names: bool = False
    auto_activate_on_approve: bool = True


class DetectConfig(BaseModel):
    n_folds: int = 5
    max_fit_rows: int = 300_000
    window: int = 20
    time_budget_s: int = 450
    detectors: list[str] = Field(default_factory=lambda: ["pca", "robust_z", "ewma", "cusum", "corr_break", "iforest", "autoencoder"])
    use_autoencoder: bool = True
    autoencoder_max_rows: int = 60_000
    contamination_prior: float = 0.1
    min_event_len: int = 5
    pattern_min_events: int = 3
    leakage_guard: bool = True


class AssessorConfig(BaseModel):
    learning_curve_fractions: list[float] = Field(default_factory=lambda: [0.1, 0.2, 0.4, 0.7, 1.0])
    holdback_fraction: float = 0.2
    experiment_time_budget_s: int = 90


class SmtpConfig(BaseModel):
    host_env: str = "TPM_SMTP_HOST"
    port_env: str = "TPM_SMTP_PORT"
    user_env: str = "TPM_SMTP_USER"
    password_env: str = "TPM_SMTP_PASSWORD"
    from_env: str = "TPM_SMTP_FROM"


class ReportConfig(BaseModel):
    default_language: str = "en"
    languages: list[str] = Field(default_factory=lambda: ["en", "fi", "sv"])
    smtp: SmtpConfig = Field(default_factory=SmtpConfig)


class Settings(BaseModel):
    profile: str = "no-egress"
    profiles: dict[str, Profile] = Field(default_factory=dict)
    local_llm: LocalLLMConfig = Field(default_factory=LocalLLMConfig)
    external_llm: ExternalLLMConfig = Field(default_factory=ExternalLLMConfig)
    guard: GuardConfig = Field(default_factory=GuardConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    batch: BatchConfig = Field(default_factory=BatchConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)
    detect: DetectConfig = Field(default_factory=DetectConfig)
    assessor: AssessorConfig = Field(default_factory=AssessorConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    workspace_dir: str = "workspace"
    time_budget_s: int = 1200
    settings_path: Optional[str] = None

    # external_llm as the file (and the TPM_EXTERNAL_* variables) set it, before the active profile's overrides
    _external_llm_base: Optional[ExternalLLMConfig] = PrivateAttr(default=None)

    def model_post_init(self, __context: Any) -> None:
        self._apply_profile_llm()

    def _apply_profile_llm(self) -> None:
        """external_llm = the file's external_llm with the active profile's `external_llm` keys on top. eu-hosted brings
        its own endpoint, model and key variable this way, while hybrid keeps the file's values."""
        if self._external_llm_base is None:
            self._external_llm_base = self.external_llm.model_copy(deep=True)
        over = dict(self.active_profile.external_llm or {})
        base = self._external_llm_base
        self.external_llm = ExternalLLMConfig(**{**base.model_dump(), **over}) if over else base.model_copy(deep=True)

    @property
    def base_external_llm(self) -> ExternalLLMConfig:
        """external_llm as settings.yaml's top-level block has it (what the UI's model choice writes), without the active
        profile's overrides."""
        return self._external_llm_base or self.external_llm

    @property
    def active_profile(self) -> Profile:
        return self.profiles.get(self.profile, Profile())

    def route_for(self, task: str) -> str:
        """'local' | 'external' for a task key; unknown tasks stay local."""
        prof = self.active_profile
        route = prof.routing.get(task, "local")
        if route == "external" and not prof.allow_external:
            return "local"
        return route

    def external_model_for(self, task: Optional[str] = None) -> str:
        """External model id for a task: external_llm.model_by_task[task], else external_llm.model."""
        by_task = self.external_llm.model_by_task or {}
        return str(by_task.get(task or "") or self.external_llm.model)

    def external_block_reason(self, task: Optional[str] = None) -> Optional[str]:
        """Why the external route cannot be used under these settings (None = usable; the API key is checked by the
        provider). A blocked model or a missing EU endpoint makes the route unavailable, never an error."""
        prof = self.active_profile
        if not prof.allow_external:
            return f"profile '{self.profile}' does not allow external models"
        cfg = self.external_llm
        if str(cfg.provider or "").strip().lower() not in EXTERNAL_PROVIDERS:
            return f"external_llm.provider '{cfg.provider}' is unknown (use one of: {', '.join(EXTERNAL_PROVIDERS)})"
        model = self.external_model_for(task)
        ok, why = cfg.model_allowed(model)
        if not ok:
            return why
        if prof.require_custom_endpoint or self.profile == "eu-hosted":
            if cfg.endpoint_is_first_party():
                return ("profile 'eu-hosted' needs external_llm.base_url set to an EU-hosted endpoint: the first-party Anthropic API "
                        "has no EU-only processing")
            # only services known to run the model in the EU; worded without "endpoint" so the UI shows these words
            if prof.eu_hosts and not any(fnmatch(cfg.host, str(p).strip().lower()) for p in prof.eu_hosts if p):
                return (f"profile '{self.profile}' refuses {cfg.host}: it is not on the list of EU-hosted services "
                        f"(profiles.{self.profile}.eu_hosts)")
            prefix = next((p for p in NON_EU_MODEL_PREFIXES if model.strip().lower().startswith(p)), None)
            if prefix:
                return (f"model id '{model}' is a cross-region profile ('{prefix}') that may run outside the EU; use an in-region "
                        f"or an 'eu.' model id")
        return None

    @property
    def workspace_path(self) -> Path:
        p = Path(self.workspace_dir)
        return p if p.is_absolute() else ROOT / p

    def with_profile(self, profile: str) -> "Settings":
        s = self.model_copy(deep=True)
        if profile in s.profiles and profile != s.profile:
            s.profile = profile
            s._apply_profile_llm()
        return s


def _apply_env(data: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("TPM_PROFILE"):
        data["profile"] = os.environ["TPM_PROFILE"]
    if os.environ.get("TPM_LOCAL_MODEL") and (data.get("local_llm") or {}).get("model_selected_by") != "user":
        data.setdefault("local_llm", {})["model"] = os.environ["TPM_LOCAL_MODEL"]
    if os.environ.get("OLLAMA_HOST"):
        host = os.environ["OLLAMA_HOST"]
        if not host.startswith("http"):
            host = "http://" + host
        data.setdefault("local_llm", {})["base_url"] = host
    if os.environ.get("TPM_EXTERNAL_MODEL"):
        data.setdefault("external_llm", {})["model"] = os.environ["TPM_EXTERNAL_MODEL"]
    if os.environ.get("TPM_EXTERNAL_BASE_URL"):
        data.setdefault("external_llm", {})["base_url"] = os.environ["TPM_EXTERNAL_BASE_URL"]
    # the eu-hosted profile's own endpoint (its key variable is profiles.eu-hosted.external_llm.api_key_env, TPM_EU_API_KEY)
    eu = {k: os.environ[v].strip() for k, v in (("base_url", "TPM_EU_BASE_URL"), ("model", "TPM_EU_MODEL"), ("provider", "TPM_EU_PROVIDER"))
          if os.environ.get(v, "").strip()}
    if eu:
        profiles = data["profiles"] = data.get("profiles") or {}
        eu_prof = profiles["eu-hosted"] = profiles.get("eu-hosted") or {}
        eu_prof["external_llm"] = {**(eu_prof.get("external_llm") or {}), **eu}
    if os.environ.get("TPM_WORKSPACE"):
        data["workspace_dir"] = os.environ["TPM_WORKSPACE"]
    if os.environ.get("TPM_TIME_BUDGET_S"):
        data["time_budget_s"] = int(os.environ["TPM_TIME_BUDGET_S"])
    return data


def load_settings(path: Optional[str | Path] = None, profile: Optional[str] = None) -> Settings:
    p = Path(path) if path else _settings_path()
    data: dict[str, Any] = {}
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    data = _apply_env(data)
    s = Settings(**data)
    s.settings_path = str(p)
    if profile:
        s = s.with_profile(profile)
    return s


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


def save_settings_overrides(overrides: dict[str, Any], path: Optional[str | Path] = None) -> Settings:
    """Persist top-level overrides (e.g. {'profile': 'hybrid'}) into settings.yaml and reload."""
    p = Path(path) if path else _settings_path()
    data: dict[str, Any] = {}
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    def merge(dst: dict, src: dict) -> dict:
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = v
        return dst

    merge(data, overrides)
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    return reload_settings()
