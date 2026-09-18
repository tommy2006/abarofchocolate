"""Shared data contracts. Every stage reads and writes these; keep changes backward compatible.

IDs: EV-000001 (evidence), INF-000001 (inference), CHK-..., FLAG-..., PATTERN-A, DIAG-..., RULE-...,
LOG seq integers, EGR-... (egress records).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat()


class Evidence(BaseModel):
    """A concrete, reproducible observation computed by code."""

    id: str
    kind: str  # distribution | correlation | lag | stuck | range | gap | duplicate | changepoint | contribution | name_hint | ...
    signals: list[str] = Field(default_factory=list)  # signal aliases involved (S01...)
    statement: str  # plain language: "S07 and S13 correlate r=0.92 at lag 0"
    values: dict[str, Any] = Field(default_factory=dict)  # numeric details (aggregates only)
    computed_by: str = ""  # "profile.relations.lagged_xcorr"
    n_samples: Optional[int] = None  # sample size behind the statistic (egress guard uses it)
    group_id: Optional[str] = None
    batch_id: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


InferenceStatus = Literal["inferred", "assumed", "uncertain"]
Source = Literal["code", "llm-local", "llm-external", "human", "template"]


class Inference(BaseModel):
    """A claim about the data, grounded in evidence, with explicit status and confidence."""

    id: str
    subject: str  # "S07" | "dataset" | "group:12" | "batch:B0003"
    claim: str
    status: InferenceStatus = "inferred"
    confidence: float = 0.5  # 0..1
    evidence_ids: list[str] = Field(default_factory=list)
    reasoning: str = ""
    source: str = "code"  # Source, possibly suffixed with model name e.g. "llm-local:gemma4"
    alternatives: list[str] = Field(default_factory=list)
    stage: str = ""  # ingest | profile | quality | detect | diagnose | assess
    human_status: Optional[Literal["accepted", "questioned", "overridden"]] = None
    human_note: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


StructuralRole = Literal[
    "continuous_measured",  # noisy, continuously varying measurement
    "actuator_like",  # step-like / saturating (0-100), manipulated variable
    "held_sampled",  # sample-and-hold, updates every k samples (analyzer-like)
    "constant",
    "derived_redundant",  # near-exact function of other signals
    "counter",  # monotone index
    "timestamp",
    "categorical",
    "text",
    "identifier",
    "unknown",
]


class SignalDescriptor(BaseModel):
    """One entry of the signal catalog (signals.json). Aggregates only, never raw values."""

    id: str  # alias "S07"
    source_column: Optional[str] = None  # original header if any (weak evidence)
    column_index: int
    dtype: str
    structural_role: str = "unknown"  # StructuralRole
    structural_confidence: float = 0.0
    instrument_hypothesis: Optional[str] = None  # flow | pressure | temperature | level | composition | valve | power | speed | ...
    instrument_confidence: float = 0.0
    unit_operation_hypothesis: Optional[str] = None  # reactor | separator | stripper | compressor | feed | utility | ...
    unit_operation_confidence: float = 0.0
    cluster_id: Optional[str] = None
    related_signals: list[dict[str, Any]] = Field(default_factory=list)  # [{signal, r, lag}]
    fingerprint: dict[str, Any] = Field(default_factory=dict)
    units_hypothesis: Optional[str] = None
    confidence: float = 0.0
    inference_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    excluded: bool = False  # excluded from detection (label/meta/constant)
    excluded_reason: Optional[str] = None
    human_role_override: Optional[str] = None


class GroupingCandidate(BaseModel):
    method: str  # key_columns | counter_reset | changepoint | none
    columns: list[str] = Field(default_factory=list)
    n_groups: int = 1
    score: float = 0.0
    rationale: str = ""


class DatasetSchema(BaseModel):
    dataset_id: str
    source_path: str
    format: str
    n_rows: int
    n_cols: int
    had_header: bool
    transposed: bool = False
    delimiter: Optional[str] = None
    columns: list[str]  # names as stored in dataset.parquet
    time_column: Optional[str] = None
    sample_period_seconds: Optional[float] = None  # None => unknown, work in sample units
    order_column: Optional[str] = None  # explicit counter/index used for ordering within groups
    group_columns: list[str] = Field(default_factory=list)
    group_column: str = "__group__"  # materialized group id column in dataset.parquet
    grouping_method: str = "none"
    grouping_candidates: list[GroupingCandidate] = Field(default_factory=list)
    label_columns: list[str] = Field(default_factory=list)  # excluded from detection, evaluation only
    meta_columns: list[str] = Field(default_factory=list)  # ids, split markers, constants, etc.
    signal_columns: list[str] = Field(default_factory=list)  # original column names used as signals
    signal_alias: dict[str, str] = Field(default_factory=dict)  # original -> "S07"
    n_groups: int = 1
    group_sizes: dict[str, int] = Field(default_factory=dict)  # summary: min/median/max
    domain_likelihood: dict[str, float] = Field(default_factory=dict)  # {"sensor_stream":0.8,"business_records":0.1,...}
    assumptions: list[str] = Field(default_factory=list)
    inference_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    options_used: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_iso)


CheckCategory = Literal["completeness", "validity", "consistency", "timeliness", "rule"]
CheckStatus = Literal["pass", "warn", "fail"]


class CheckResult(BaseModel):
    check_id: str  # "CHK-000012"
    check_type: str  # "missing" | "stuck" | "out_of_range" | "gap" | "duplicate" | "unit_shift" | "rule:<rule_id>" | ...
    category: str  # CheckCategory
    signals: list[str] = Field(default_factory=list)
    batch_id: str
    group_id: Optional[str] = None
    status: str  # CheckStatus
    severity: float = 0.0  # 0..1
    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    rule_id: Optional[str] = None  # traceability to the originating rule
    values: dict[str, Any] = Field(default_factory=dict)
    row_start: Optional[int] = None
    row_end: Optional[int] = None
    created_at: str = Field(default_factory=now_iso)


class TrustVerdict(BaseModel):
    batch_id: str
    trusted: bool
    trust_score: float  # 0..1
    untrusted_signals: list[str] = Field(default_factory=list)  # unreliable for a meaningful share of the batch
    local_untrusted: list[dict[str, Any]] = Field(default_factory=list)  # [{signal,row_start,row_end,check_type,severity}] unreliable only in those rows
    n_rows: Optional[int] = None  # batch rows the exposure weighting used (what-if recomputations reuse it)
    reasons: list[str] = Field(default_factory=list)
    check_ids: list[str] = Field(default_factory=list)
    statement: str = ""
    created_at: str = Field(default_factory=now_iso)


RuleStatus = Literal["draft", "approved", "rejected", "active", "retired"]


class Rule(BaseModel):
    id: str  # "RULE-003"
    text: str  # plain-language original
    author: str = "human"
    status: str = "draft"  # RuleStatus
    compiled: Optional[dict[str, Any]] = None  # JSON check spec (see quality/rules.py schema)
    compile_source: str = "template"  # llm-local:<m> | llm-external:<m> | template | human
    compile_explanation: str = ""
    compile_confidence: float = 0.0
    inference_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


FlagKind = Literal["anomaly", "drift", "changepoint", "dq", "rule", "cascade"]
CauseClass = Literal["process", "sensor", "data", "mixed", "unknown"]


class SignalContribution(BaseModel):
    signal: str
    contribution: float  # share in [0,1] or normalized score
    direction: Optional[str] = None  # "up" | "down" | "noisy" | "stuck" | "shifted"
    lag: Optional[int] = None
    explanation: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class Flag(BaseModel):
    id: str  # "FLAG-000001"
    kind: str  # FlagKind
    batch_id: Optional[str] = None
    group_id: Optional[str] = None
    row_start: int
    row_end: int
    time_start: Optional[str] = None
    time_end: Optional[str] = None
    severity: float  # 0..1
    score: float
    threshold: Optional[float] = None
    detector: str
    statement: str
    signals_ranked: list[SignalContribution] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    likely_cause_class: str = "unknown"  # CauseClass
    confidence: float = 0.5
    pattern_id: Optional[str] = None
    trust_context: Optional[dict[str, Any]] = None  # trust verdict summary of the batch
    human_status: Optional[str] = None  # accepted | questioned | overridden | dismissed
    human_note: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


class FaultPattern(BaseModel):
    id: str  # "PATTERN-A"
    name: Optional[str] = None  # human-given name
    signature: dict[str, Any] = Field(default_factory=dict)  # ranked signals, directions, lag order
    n_events: int = 0
    groups_affected: list[str] = Field(default_factory=list)
    description: str = ""
    confidence: float = 0.5
    evidence_ids: list[str] = Field(default_factory=list)
    classifier_reliability: Optional[float] = None


class PropagationStep(BaseModel):
    from_signal: str
    to_signal: str
    lag: Optional[int] = None
    strength: float = 0.0
    explanation: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class Critique(BaseModel):
    verdict: str  # "supported" | "weakened" | "rejected"
    objections: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)  # code checks: {name, passed, detail}
    adjusted_confidence: Optional[float] = None
    source: str = "template"


class Diagnosis(BaseModel):
    id: str  # "DIAG-000001"
    flag_ids: list[str] = Field(default_factory=list)
    group_id: Optional[str] = None
    pattern_id: Optional[str] = None
    fault_type: str  # pattern name or "PATTERN-A (unnamed)" or "sensor fault" ...
    cause_class: str = "unknown"  # CauseClass
    ranked_signals: list[SignalContribution] = Field(default_factory=list)
    propagation: list[PropagationStep] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)  # step-by-step explanation for a non-expert
    summary: str = ""
    confidence: float = 0.5
    uncertainty: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    critique: Optional[Critique] = None
    narrative_source: str = "template"
    human_status: Optional[str] = None
    human_note: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


class LogEntry(BaseModel):
    seq: int
    ts: str
    actor: str  # system:<module> | human:<name>(<role>) | llm:local:<model> | llm:external:<model>
    action: str  # inference | flag | diagnosis | check | trust | rule | accept | question | override | dismiss | egress | stage | assess | ...
    object_type: str
    object_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    prev_hash: str
    hash: str


class EgressRecord(BaseModel):
    id: str  # "EGR-000001"
    ts: str = Field(default_factory=now_iso)
    task: str
    purpose: str
    route: str  # local | external
    provider: str
    model: str
    artifact_types: list[str] = Field(default_factory=list)
    payload_bytes: int = 0
    payload_hash: str = ""
    payload_preview: str = ""  # first 500 chars of what was sent (external only)
    guard_result: str = "allowed"  # allowed | blocked | fallback | n/a
    guard_reason: str = ""
    response_hash: str = ""
    latency_ms: Optional[int] = None
    ok: bool = True
    error: Optional[str] = None


class HumanDecision(BaseModel):
    actor_name: str
    role: str  # operator | engineer | reviewer
    action: str  # accept | question | override | dismiss | name_pattern | approve_rule | reject_rule | set_role | set_reference | apply_assessor_action
    object_type: str  # inference | flag | diagnosis | rule | pattern | signal | schema | assessor
    object_id: str
    note: Optional[str] = None
    new_value: Optional[dict[str, Any]] = None
    ts: str = Field(default_factory=now_iso)


class StageStatus(BaseModel):
    stage: str
    state: str  # pending | running | done | failed | skipped
    progress: float = 0.0
    message: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None


class RunStatus(BaseModel):
    run_id: str
    source_path: str
    profile: str
    state: str = "pending"  # pending | running | done | failed
    stages: list[StageStatus] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    options: dict[str, Any] = Field(default_factory=dict)


class LLMResult(BaseModel):
    text: str = ""
    data: Optional[dict[str, Any]] = None
    source: str = "template"  # template | llm-local:<model> | llm-external:<model>
    model: str = ""
    route: str = "none"  # none | local | external
    ledger_id: Optional[str] = None
    ok: bool = True
    error: Optional[str] = None
    latency_ms: Optional[int] = None
