"""Settings: config/settings.yaml + environment overrides. Profiles decide what may leave the machine."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SETTINGS_PATH = ROOT / "config" / "settings.yaml"

load_dotenv(ROOT / ".env", override=False)


class Profile(BaseModel):
    description: str = ""
    allow_external: bool = False
    guard_strict: bool = True
    routing: dict[str, str] = Field(default_factory=dict)


class LocalLLMConfig(BaseModel):
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"
    model: str = "gemma4:e4b-it-qat"
    fallback_models: list[str] = Field(default_factory=list)
    embedding_model: str = "nomic-embed-text"
    keep_alive: str = "5m"
    num_ctx: int = 8192
    temperature: float = 0.2
    timeout_s: int = 180
    max_tool_steps: int = 8


class ExternalLLMConfig(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-sonnet-5"
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: Optional[str] = None
    max_tokens: int = 2048
    timeout_s: int = 120

    @property
    def api_key(self) -> Optional[str]:
        return os.environ.get(self.api_key_env)


class GuardConfig(BaseModel):
    min_aggregate_n: int = 30
    max_series_points: int = 20
    max_numeric_values_per_payload: int = 4000
    max_payload_bytes: int = 200_000
    allow_column_names: bool = True
    alias_column_names_in_strict: bool = True
    forbid_categorical_values: bool = True
    forbid_row_like_structures: bool = True


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

    @property
    def workspace_path(self) -> Path:
        p = Path(self.workspace_dir)
        return p if p.is_absolute() else ROOT / p

    def with_profile(self, profile: str) -> "Settings":
        s = self.model_copy(deep=True)
        if profile in s.profiles:
            s.profile = profile
        return s


def _apply_env(data: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("TPM_PROFILE"):
        data["profile"] = os.environ["TPM_PROFILE"]
    if os.environ.get("TPM_LOCAL_MODEL"):
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
    if os.environ.get("TPM_WORKSPACE"):
        data["workspace_dir"] = os.environ["TPM_WORKSPACE"]
    if os.environ.get("TPM_TIME_BUDGET_S"):
        data["time_budget_s"] = int(os.environ["TPM_TIME_BUDGET_S"])
    return data


def load_settings(path: Optional[str | Path] = None, profile: Optional[str] = None) -> Settings:
    p = Path(path) if path else DEFAULT_SETTINGS_PATH
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
    p = Path(path) if path else DEFAULT_SETTINGS_PATH
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
