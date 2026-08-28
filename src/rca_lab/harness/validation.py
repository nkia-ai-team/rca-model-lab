"""Dynamic execution-boundary validation and deterministic proof rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict

from rca_lab.harness.environment import EvidenceEnvironment
from rca_lab.harness.models import (
    ActionName,
    ActionRequest,
    AnswerState,
    CapabilityRegistry,
    Cause,
    Fact,
    FactKind,
    Observation,
    ProofType,
)


class ContractError(ValueError):
    pass


class ProofReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    satisfied: bool
    reasons: tuple[str, ...] = ()


_TARGET_ACTIONS = {
    ActionName.ENV_ENTITY,
    ActionName.PROBE_METRIC,
    ActionName.METRIC_DESCRIBE,
    ActionName.METRIC_OVERVIEW,
    ActionName.METRIC_FETCH_RAW,
    ActionName.METRIC_FETCH_WINDOW,
    ActionName.METRIC_COMPARE,
    ActionName.PROBE_EVENTS,
    ActionName.PROBE_COHORT,
    ActionName.PROBE_DB_BLOCKING,
    ActionName.PROBE_TOPSQL,
    ActionName.PROBE_LOGS,
    ActionName.PROBE_TRACES,
    ActionName.PROBE_CHANGES,
    ActionName.PROBE_DB_QUERY_EVENTS,
    ActionName.PROBE_REPLICA_LAG,
    ActionName.PROBE_HOST_PEERS,
    ActionName.PROBE_SNMP_TRAPS,
    ActionName.DISCOVER_METRICS,
}
_METRIC_ACTIONS = {
    ActionName.PROBE_METRIC,
    ActionName.METRIC_DESCRIBE,
    ActionName.METRIC_OVERVIEW,
    ActionName.METRIC_FETCH_RAW,
    ActionName.METRIC_FETCH_WINDOW,
    ActionName.METRIC_COMPARE,
}


class HarnessValidator:
    def __init__(
        self,
        environment: EvidenceEnvironment,
        registry: CapabilityRegistry,
        ledger: tuple[Observation, ...] = (),
        *,
        first_event_at: datetime | None = None,
        probe_freshness: timedelta = timedelta(minutes=5),
    ):
        self.environment = environment
        self.registry = registry
        self.ledger = ledger
        self.first_event_at = first_event_at
        self.probe_freshness = probe_freshness

    def validate_action(self, request: ActionRequest) -> None:
        if not self.registry.allows_action(request.action):
            raise ContractError(f"action {request.action!s} is not in capability registry")
        if request.action in _TARGET_ACTIONS and request.arg1 not in self.registry.candidates:
            raise ContractError(f"target {request.arg1!r} is not a candidate")
        if request.action in _METRIC_ACTIONS and not self.registry.allows_metric(
            request.arg1, request.metric
        ):
            raise ContractError(
                f"metric {request.metric!r} is not registered for target {request.arg1!r}"
            )
        if request.action is ActionName.METRIC_COMPARE:
            if request.arg2 not in self.registry.candidates:
                raise ContractError(f"comparison target {request.arg2!r} is not a candidate")
            if not self.registry.allows_metric(request.arg2, request.metric):
                raise ContractError("comparison metric is not registered for both targets")
        if request.action in {ActionName.ENV_QUERY, ActionName.ENV_AGGREGATE}:
            if request.query is None:
                raise ContractError(f"{request.action!s} requires query")
            self._validate_query(request)
        if request.action in {ActionName.ENV_SLICE, ActionName.SUB_SUMMARIZE}:
            refs = tuple(part.strip() for part in request.arg1.split(",") if part.strip())
            if not refs:
                raise ContractError(f"{request.action!s} requires refs")
            self._require_known_refs(refs)
        self.validate_answer(request.answer)

    def validate_answer(self, answer: AnswerState) -> None:
        for cause in answer.causes:
            if cause.target not in self.registry.candidates:
                raise ContractError(f"cause target {cause.target!r} is not a candidate")
            if not answer.ready:
                continue
            if not cause.mechanism.strip():
                raise ContractError("ready cause requires mechanism")
            if not cause.support_refs:
                raise ContractError("ready cause requires support_refs")
            self._require_known_refs(cause.support_refs + cause.counter_refs)
        for cause in answer.external_causes:
            if not self.registry.allows_external_cause(cause):
                raise ContractError(
                    f"external cause {cause.id!r} is not registered for "
                    f"{cause.boundary_target!r}"
                )
            self._require_known_refs(cause.evidence_refs)
            if answer.ready and not any(
                internal.target == cause.boundary_target for internal in answer.causes
            ):
                raise ContractError(
                    "ready external cause requires a linked boundary cause"
                )
        if answer.status == "confirmed":
            if not answer.causes or answer.external_causes:
                raise ContractError("confirmed requires internal causes only")
            failures = [self.evaluate_proof(cause) for cause in answer.causes]
            if any(not report.satisfied for report in failures):
                reasons = [reason for report in failures for reason in report.reasons]
                raise ContractError("confirmed proof failed: " + "; ".join(reasons))

    def evaluate_proof(self, cause: Cause) -> ProofReport:
        try:
            if cause.target not in self.registry.candidates:
                raise ContractError("unknown cause target")
            if not cause.mechanism.strip() or not cause.support_refs:
                raise ContractError("cause contract incomplete")
            self._require_known_refs(cause.support_refs + cause.counter_refs)
        except ContractError as error:
            return ProofReport(satisfied=False, reasons=(str(error),))

        facts = self._supporting_facts(cause)
        kinds = {fact.kind for fact in facts if fact.target == cause.target}
        if cause.proof_type is ProofType.DB_BLOCKING:
            return self._requires(kinds, {FactKind.DB_BLOCKING}, "blocking session absent")
        if cause.proof_type is ProofType.CHANGE_PRECEDES:
            matching = [fact for fact in facts if fact.kind is FactKind.CHANGE and fact.target == cause.target]
            ok = bool(matching) and bool(self.first_event_at) and min(f.at for f in matching) <= self.first_event_at
            return ProofReport(satisfied=ok, reasons=() if ok else ("preceding change absent",))
        if cause.proof_type is ProofType.METRIC_CAUSAL:
            matching = [f for f in facts if f.kind is FactKind.METRIC_BREACH and f.target == cause.target]
            ok = bool(matching) and bool(self.first_event_at) and min(f.at for f in matching) <= self.first_event_at
            ok = ok and self._has_complete_metric_reference(cause)
            return ProofReport(satisfied=ok, reasons=() if ok else ("causal full-series breach absent",))
        if cause.proof_type is ProofType.RESOURCE_SATURATION:
            ok = FactKind.RESOURCE_SATURATION in kinds and self._has_complete_metric_reference(cause)
            return ProofReport(satisfied=ok, reasons=() if ok else ("full-series saturation absent",))
        if cause.proof_type is ProofType.RUNTIME_DEATH:
            return self._runtime_death(cause, facts)
        if cause.proof_type is ProofType.EVENT_DIRECT:
            return self._requires(kinds, {FactKind.DIRECT_EVENT}, "direct event absent")
        if cause.proof_type is ProofType.TRACE_BOUNDARY:
            return self._requires(kinds, {FactKind.TRACE_ERROR}, "boundary trace error absent")
        if cause.proof_type is ProofType.EXTERNAL_DEPENDENCY:
            return self._requires(
                kinds, {FactKind.EXTERNAL_BOUNDARY_FAILURE}, "external boundary failure absent"
            )
        return ProofReport(satisfied=False, reasons=("unknown proof cannot confirm",))

    def _validate_query(self, request: ActionRequest) -> None:
        assert request.query is not None
        query = request.query
        if query.target and query.target not in self.registry.candidates:
            raise ContractError(f"query target {query.target!r} is not a candidate")
        if query.source and query.source not in self.registry.sources:
            raise ContractError(f"query source {query.source!r} is not registered")
        if query.kind and query.kind not in self.registry.kinds:
            raise ContractError(f"query kind {query.kind!r} is not registered")
        if query.limit > self.registry.query_result_limit:
            raise ContractError("query limit exceeds registry limit")

    def _known_refs(self) -> set[str]:
        refs = set(self.environment.ids)
        for observation in self.ledger:
            refs.add(observation.id)
            refs.update(observation.evidence_refs)
        return refs

    def _require_known_refs(self, refs: tuple[str, ...]) -> None:
        unknown = sorted(set(refs) - self._known_refs())
        if unknown:
            raise ContractError(f"unknown refs: {', '.join(unknown)}")

    def _supporting_facts(self, cause: Cause) -> tuple[Fact, ...]:
        wanted = set(cause.support_refs)
        return tuple(
            fact
            for observation in self.ledger
            if observation.id in wanted and observation.ok and self._fresh(observation)
            for fact in observation.facts
        )

    def _fresh(self, observation: Observation) -> bool:
        now = datetime.now(UTC)
        captured_at = observation.captured_at
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=UTC)
        return now - captured_at <= self.probe_freshness

    def _has_complete_metric_reference(self, cause: Cause) -> bool:
        wanted = set(cause.support_refs)
        return any(
            observation.id in wanted
            and observation.metric_access is not None
            and observation.metric_access.full_series_retained
            for observation in self.ledger
        )

    @staticmethod
    def _requires(kinds: set[FactKind], required: set[FactKind], reason: str) -> ProofReport:
        ok = required <= kinds
        return ProofReport(satisfied=ok, reasons=() if ok else (reason,))

    def _runtime_death(self, cause: Cause, facts: tuple[Fact, ...]) -> ProofReport:
        required = {
            FactKind.OOM_KILLED_PROCESS,
            FactKind.TASK_OOM,
            FactKind.CRASH_LOOP,
            FactKind.RESTART_DELTA,
        }
        matching = [fact for fact in facts if fact.target == cause.target and fact.kind in required]
        kinds = {fact.kind for fact in matching}
        if kinds != required:
            return ProofReport(satisfied=False, reasons=("OOM/TaskOOM/CrashLoop/restart chain incomplete",))
        times = [fact.at for fact in matching]
        if max(times) - min(times) > timedelta(minutes=2):
            return ProofReport(satisfied=False, reasons=("runtime death chain exceeds two minutes",))
        restart = next(fact for fact in matching if fact.kind is FactKind.RESTART_DELTA)
        if restart.value is None or restart.value <= 0:
            return ProofReport(satisfied=False, reasons=("restart delta is not positive",))
        return ProofReport(satisfied=True)
