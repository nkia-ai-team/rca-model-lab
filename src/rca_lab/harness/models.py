"""Strict schemas at the student/tool/scorer boundaries.

The model may reason freely. Only executable actions, observations, evidence
references, and final answers cross these typed boundaries.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ActionName(StrEnum):
    ENV_TOP = "env_top"
    ENV_ENTITY = "env_entity"
    ENV_QUERY = "env_query"
    ENV_AGGREGATE = "env_aggregate"
    ENV_GREP = "env_grep"
    ENV_SLICE = "env_slice"
    PROBE_METRIC = "probe_metric"
    METRIC_DESCRIBE = "metric_describe"
    METRIC_OVERVIEW = "metric_overview"
    METRIC_FETCH_RAW = "metric_fetch_raw"
    METRIC_FETCH_WINDOW = "metric_fetch_window"
    METRIC_COMPARE = "metric_compare"
    PROBE_EVENTS = "probe_events"
    PROBE_COHORT = "probe_cohort"
    PROBE_DB_BLOCKING = "probe_db_blocking"
    PROBE_TOPSQL = "probe_topsql"
    PROBE_LOGS = "probe_logs"
    PROBE_TRACES = "probe_traces"
    PROBE_CHANGES = "probe_changes"
    PROBE_DB_QUERY_EVENTS = "probe_db_query_events"
    PROBE_REPLICA_LAG = "probe_replica_lag"
    PROBE_HOST_PEERS = "probe_host_peers"
    PROBE_SNMP_TRAPS = "probe_snmp_traps"
    DISCOVER_METRICS = "discover_metrics"
    SUB_SUMMARIZE = "sub_summarize"
    NOTE = "note"
    ANSWER = "answer"


class ProofType(StrEnum):
    DB_BLOCKING = "db_blocking"
    CHANGE_PRECEDES = "change_precedes"
    METRIC_CAUSAL = "metric_causal"
    RESOURCE_SATURATION = "resource_saturation"
    RUNTIME_DEATH = "runtime_death"
    EVENT_DIRECT = "event_direct"
    TRACE_BOUNDARY = "trace_boundary"
    EXTERNAL_DEPENDENCY = "external_dependency"
    UNKNOWN = "unknown"


class FactKind(StrEnum):
    DB_BLOCKING = "db_blocking"
    CHANGE = "change"
    METRIC_BREACH = "metric_breach"
    RESOURCE_SATURATION = "resource_saturation"
    OOM_KILLED_PROCESS = "oom_killed_process"
    TASK_OOM = "task_oom"
    CRASH_LOOP = "crash_loop"
    RESTART_DELTA = "restart_delta"
    DIRECT_EVENT = "direct_event"
    TRACE_ERROR = "trace_error"
    EXTERNAL_BOUNDARY_FAILURE = "external_boundary_failure"


class TimeWindow(StrictModel):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def ordered(self) -> TimeWindow:
        if self.start > self.end:
            raise ValueError("window.start must be <= window.end")
        return self


class Evidence(StrictModel):
    id: str = Field(min_length=1)
    target: str = ""
    source: str = ""
    kind: str = ""
    metric: str = ""
    window: TimeWindow | None = None
    anomaly_score: float = 0.0
    observation: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)


class EvidenceQuery(StrictModel):
    target: str = ""
    source: str = ""
    kind: str = ""
    start: datetime | None = None
    end: datetime | None = None
    limit: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def ordered(self) -> EvidenceQuery:
        if self.start and self.end and self.start > self.end:
            raise ValueError("query.start must be <= query.end")
        return self


class Fact(StrictModel):
    kind: FactKind
    target: str = Field(min_length=1)
    at: datetime
    value: float | None = None
    related_target: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class MetricAccess(StrictModel):
    mode: Literal["overview", "raw", "window", "compare"]
    total_points: int = Field(ge=0)
    returned_points: int = Field(ge=0)
    truncated: bool
    full_series_retained: bool
    raw_ref: str = ""

    @model_validator(mode="after")
    def counts_are_consistent(self) -> MetricAccess:
        if self.returned_points > self.total_points:
            raise ValueError("returned_points cannot exceed total_points")
        if self.truncated != (self.returned_points < self.total_points):
            raise ValueError("truncated must match returned_points < total_points")
        return self


class Observation(StrictModel):
    id: str = Field(pattern=r"^obs-[0-9]{3,}$")
    turn: int = Field(ge=0)
    action: ActionName
    target: str = ""
    metric: str = ""
    evidence_refs: tuple[str, ...] = ()
    summary: str
    ok: bool
    progress: bool
    captured_at: datetime
    facts: tuple[Fact, ...] = ()
    metric_access: MetricAccess | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class Cause(StrictModel):
    target: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    proof_type: ProofType = ProofType.UNKNOWN
    mechanism: str = ""
    support_refs: tuple[str, ...] = ()
    counter_refs: tuple[str, ...] = ()


class ExternalCause(StrictModel):
    id: str = Field(pattern=r"^external:")
    kind: Literal["external_dependency", "kafka", "redis", "network", "capacity_limit"]
    name: str = Field(min_length=1)
    boundary_target: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)


class AnswerState(StrictModel):
    status: Literal["confirmed", "provisional", "insufficient"]
    ready: bool = False
    causes: tuple[Cause, ...] = Field(default=(), max_length=3)
    external_causes: tuple[ExternalCause, ...] = Field(default=(), max_length=3)


class ActionRequest(StrictModel):
    thought: str = ""
    action: ActionName
    target: str = ""
    other_target: str = ""
    metric: str = ""
    query: EvidenceQuery | None = None
    refs: tuple[str, ...] = ()
    note: str = ""
    refresh: bool = False
    answer: AnswerState | None = None


class ToolCapability(StrictModel):
    name: ActionName
    read_only: bool = True
    refreshable: bool = False


class CapabilityRegistry(StrictModel):
    tools: tuple[ToolCapability, ...]
    candidates: tuple[str, ...]
    metrics: dict[str, tuple[str, ...]]
    sources: tuple[str, ...]
    kinds: tuple[str, ...]
    query_result_limit: int = Field(default=12, ge=1)

    def allows_action(self, action: ActionName) -> bool:
        return any(tool.name == action for tool in self.tools)

    def allows_metric(self, target: str, metric: str) -> bool:
        return metric in self.metrics.get(target, ())


class PromptCall(StrictModel):
    system: str = ""
    user: str
    response: str
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    logprob: float | None = None


class Episode(StrictModel):
    version: int = 1
    incident_id: str = Field(min_length=1)
    evidence_fingerprint: str = Field(min_length=64, max_length=64)
    ledger: tuple[Observation, ...]
    prompts: tuple[PromptCall, ...] = ()
    answer: AnswerState
    reward: float | None = Field(default=None, ge=0.0, le=1.0)
    completed: bool = False

