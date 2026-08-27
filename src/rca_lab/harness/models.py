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


class BranchVerdict(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RETRYABLE = "retryable"


class CorrectionKind(StrEnum):
    EVIDENCE_GAP = "evidence_gap"
    NAVIGATION_HINT = "navigation_hint"
    SCHEMA_REPAIR = "schema_repair"
    CALIBRATION = "calibration"


class EvidenceBlockerKind(StrEnum):
    """Why a teacher cannot produce an evidence-grounded accepted branch."""

    REQUIRED_SIGNAL_MISSING = "required_signal_missing"
    GOLD_EVIDENCE_CONFLICT = "gold_evidence_conflict"
    TARGET_MAPPING_MISSING = "target_mapping_missing"
    CAPABILITY_NOT_EXPOSED = "capability_not_exposed"


class EvidenceBlocker(StrictModel):
    """Typed data defect; never treat it as a model reasoning failure."""

    kind: EvidenceBlockerKind
    required_evidence: tuple[str, ...] = Field(min_length=1)
    observed_evidence: tuple[str, ...] = Field(min_length=1)
    remediation: str = Field(min_length=1)


class TrajectoryArtifact(StrictModel):
    """Content-addressed rollout produced by any teacher implementation."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    format: Literal["episode_json", "episode_jsonl"]


class BranchAssessment(StrictModel):
    """Typed critic/scorer result. Rejected work remains useful recursive data."""

    scorer: str = Field(min_length=1)
    verdict: BranchVerdict
    reward: float = Field(ge=0.0, le=1.0)
    reasons: tuple[str, ...] = Field(min_length=1)
    missing_evidence: tuple[str, ...] = ()


class TeacherCorrection(StrictModel):
    """Hint applied between branches; never a hidden-label answer injection."""

    kind: CorrectionKind
    message: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()
    exposes_hidden_answer: bool = False

    @model_validator(mode="after")
    def hidden_answer_must_stay_sealed(self) -> TeacherCorrection:
        if self.exposes_hidden_answer:
            raise ValueError("teacher correction must not expose the hidden answer")
        return self


class ThoughtBranch(StrictModel):
    """One rollout in a recursive investigation tree."""

    branch_id: str = Field(pattern=r"^branch-[0-9]{3,}$")
    parent_id: str | None = None
    depth: int = Field(ge=0)
    teacher: str = Field(min_length=1)
    artifact: TrajectoryArtifact | None = None
    episode: Episode | None = None
    assessment: BranchAssessment
    correction_from_parent: TeacherCorrection | None = None
    include_in_sft: bool = False
    include_in_recursive_training: bool = True

    @model_validator(mode="after")
    def has_exactly_one_trajectory_source(self) -> ThoughtBranch:
        if (self.artifact is None) == (self.episode is None):
            raise ValueError("branch requires exactly one of artifact or episode")
        if self.assessment.verdict is not BranchVerdict.ACCEPTED and self.include_in_sft:
            raise ValueError("rejected/retryable branches cannot enter SFT")
        return self


class RecursiveEpisode(StrictModel):
    """Failure → critique → correction → retry tree used by ThinkFL/RLM training."""

    version: int = 1
    scenario_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    root_branch_id: str
    selected_branch_id: str | None = None
    complete: bool = False
    evidence_blockers: tuple[EvidenceBlocker, ...] = ()
    branches: tuple[ThoughtBranch, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_tree_and_selected_branch(self) -> RecursiveEpisode:
        by_id = {branch.branch_id: branch for branch in self.branches}
        if len(by_id) != len(self.branches):
            raise ValueError("branch_id must be unique")
        if self.root_branch_id not in by_id:
            raise ValueError("root_branch_id does not exist")
        root = by_id[self.root_branch_id]
        if root.parent_id is not None or root.depth != 0 or root.correction_from_parent is not None:
            raise ValueError("root branch must have no parent/correction and depth=0")

        for branch in self.branches:
            if branch.branch_id == self.root_branch_id:
                continue
            if branch.parent_id not in by_id:
                raise ValueError(f"missing parent for {branch.branch_id}")
            parent = by_id[branch.parent_id]
            if branch.depth != parent.depth + 1:
                raise ValueError(f"invalid depth for {branch.branch_id}")
            if branch.correction_from_parent is None:
                raise ValueError(f"retry branch {branch.branch_id} requires correction")

        if self.selected_branch_id is None:
            if self.complete:
                raise ValueError("complete recursive episode requires selected branch")
            return self
        if self.selected_branch_id not in by_id:
            raise ValueError("selected_branch_id does not exist")
        selected = by_id[self.selected_branch_id]
        if selected.assessment.verdict is not BranchVerdict.ACCEPTED:
            raise ValueError("selected branch must be accepted")
        if not selected.include_in_sft:
            raise ValueError("selected branch must enter SFT")
        if not self.complete:
            raise ValueError("episode with selected branch must be complete")
        if self.evidence_blockers:
            raise ValueError("complete recursive episode cannot retain evidence blockers")
        return self

    def selected_branch(self) -> ThoughtBranch:
        if self.selected_branch_id is None:
            raise ValueError("recursive episode is not complete")
        return next(branch for branch in self.branches if branch.branch_id == self.selected_branch_id)
