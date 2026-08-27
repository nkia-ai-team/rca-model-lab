from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from rca_lab.harness.environment import EvidenceEnvironment
from rca_lab.harness.models import (
    ActionRequest,
    AnswerState,
    Cause,
    Evidence,
    EvidenceQuery,
    Fact,
    FactKind,
    MetricAccess,
    Observation,
    ProofType,
)
from rca_lab.harness.registry import build_registry
from rca_lab.harness.validation import ContractError, HarnessValidator

NOW = datetime.now(UTC)


def setup_validator(ledger: tuple[Observation, ...] = ()) -> HarnessValidator:
    environment = EvidenceEnvironment(
        [
            Evidence(
                id="ev-001",
                target="payment",
                source="metric",
                kind="timeseries",
                metric="kcm.pod.restart_count",
                anomaly_score=1,
            )
        ]
    )
    registry = build_registry(environment, ("payment", "order"))
    return HarnessValidator(
        environment,
        registry,
        ledger,
        first_event_at=NOW,
        probe_freshness=timedelta(minutes=10),
    )


def test_pydantic_rejects_unknown_action_enum() -> None:
    with pytest.raises(ValidationError):
        ActionRequest.model_validate({"action": "run_shell"})


def test_dynamic_registry_rejects_unseen_metric() -> None:
    request = ActionRequest(
        action="probe_metric",
        target="payment",
        metric="invented.metric",
    )

    with pytest.raises(ContractError, match="not registered"):
        setup_validator().validate_action(request)


def test_dynamic_registry_rejects_unseen_query_source() -> None:
    request = ActionRequest(
        action="env_query",
        query=EvidenceQuery(source="invented", limit=1),
    )

    with pytest.raises(ContractError, match="source"):
        setup_validator().validate_action(request)


def test_final_answer_rejects_unobserved_reference() -> None:
    answer = AnswerState(
        status="provisional",
        ready=True,
        causes=(
            Cause(
                target="payment",
                confidence=0.7,
                proof_type="event_direct",
                mechanism="event precedes impact",
                support_refs=("obs-999",),
            ),
        ),
    )

    with pytest.raises(ContractError, match="unknown refs"):
        setup_validator().validate_answer(answer)


def test_runtime_death_requires_complete_typed_chain() -> None:
    facts = tuple(
        Fact(kind=kind, target="payment", at=NOW, value=1 if kind is FactKind.RESTART_DELTA else None)
        for kind in (
            FactKind.OOM_KILLED_PROCESS,
            FactKind.TASK_OOM,
            FactKind.CRASH_LOOP,
            FactKind.RESTART_DELTA,
        )
    )
    observation = Observation(
        id="obs-001",
        turn=1,
        action="probe_logs",
        target="payment",
        summary="typed runtime lifecycle",
        ok=True,
        progress=True,
        captured_at=NOW,
        facts=facts,
    )
    cause = Cause(
        target="payment",
        confidence=0.99,
        proof_type="runtime_death",
        mechanism="OOM kill caused restart and crash loop",
        support_refs=("obs-001",),
    )

    report = setup_validator((observation,)).evaluate_proof(cause)

    assert report.satisfied


def test_runtime_death_rejects_zero_restart_delta() -> None:
    facts = tuple(
        Fact(kind=kind, target="payment", at=NOW, value=0 if kind is FactKind.RESTART_DELTA else None)
        for kind in (
            FactKind.OOM_KILLED_PROCESS,
            FactKind.TASK_OOM,
            FactKind.CRASH_LOOP,
            FactKind.RESTART_DELTA,
        )
    )
    observation = Observation(
        id="obs-001",
        turn=1,
        action="probe_logs",
        target="payment",
        summary="typed runtime lifecycle",
        ok=True,
        progress=True,
        captured_at=NOW,
        facts=facts,
    )
    cause = Cause(
        target="payment",
        confidence=0.9,
        proof_type=ProofType.RUNTIME_DEATH,
        mechanism="claimed runtime death",
        support_refs=("obs-001",),
    )

    report = setup_validator((observation,)).evaluate_proof(cause)

    assert not report.satisfied
    assert "restart delta" in report.reasons[0]


def test_metric_access_cannot_hide_truncation() -> None:
    with pytest.raises(ValidationError, match="truncated"):
        MetricAccess(
            mode="overview",
            total_points=1000,
            returned_points=300,
            truncated=False,
            full_series_retained=True,
        )
