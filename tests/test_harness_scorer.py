from datetime import UTC, datetime, timedelta

import pytest

from rca_lab.harness.environment import EvidenceEnvironment
from rca_lab.harness.models import (
    AnswerState,
    Cause,
    Evidence,
    ExternalCause,
    Fact,
    FactKind,
    Observation,
    PseudoEntity,
)
from rca_lab.harness.registry import build_registry
from rca_lab.harness.scorer import RootExpectation, ScoreTarget, TypedScorer
from rca_lab.harness.validation import ContractError, HarnessValidator

NOW = datetime.now(UTC)


def test_typed_scorer_is_shared_reward_contract() -> None:
    environment = EvidenceEnvironment([Evidence(id="ev-001", target="db", source="event")])
    registry = build_registry(environment, ("db",))
    observation = Observation(
        id="obs-001",
        turn=1,
        action="probe_db_blocking",
        target="db",
        summary="blocking session with victims",
        ok=True,
        progress=True,
        captured_at=NOW,
        facts=(Fact(kind=FactKind.DB_BLOCKING, target="db", at=NOW, value=3),),
    )
    answer = AnswerState(
        status="confirmed",
        ready=True,
        causes=(
            Cause(
                target="db",
                confidence=0.98,
                proof_type="db_blocking",
                mechanism="blocking session stalled dependent queries",
                support_refs=("obs-001",),
            ),
        ),
    )
    validator = HarnessValidator(
        environment,
        registry,
        (observation,),
        probe_freshness=timedelta(minutes=10),
    )

    score = TypedScorer().score(
        answer,
        (observation,),
        ScoreTarget(
            expected_status="confirmed",
            roots=(RootExpectation(target_ids=("db",), target_aliases=("db",)),),
        ),
        validator,
        turns=1,
        max_turns=12,
    )

    assert score.correctness == 1
    assert score.proof == 1
    assert score.calibration == 1
    assert score.total == 1


def test_unsupported_confirmation_receives_penalty() -> None:
    environment = EvidenceEnvironment([Evidence(id="ev-001", target="db", source="event")])
    registry = build_registry(environment, ("db",))
    answer = AnswerState(
        status="confirmed",
        ready=True,
        causes=(
            Cause(
                target="db",
                confidence=0.9,
                proof_type="db_blocking",
                mechanism="unsupported claim",
                support_refs=("ev-001",),
            ),
        ),
    )
    validator = HarnessValidator(environment, registry)

    score = TypedScorer().score(
        answer,
        (),
        ScoreTarget(
            expected_status="confirmed",
            roots=(RootExpectation(target_ids=("db",), target_aliases=("db",)),),
        ),
        validator,
        turns=1,
        max_turns=12,
    )

    assert score.penalty == 1
    assert "unsupported confirmation" in score.reasons


def test_external_root_is_registered_and_scored_by_identity_and_boundary() -> None:
    environment = EvidenceEnvironment(
        [Evidence(id="ev-001", target="payment", source="trace")]
    )
    pseudo = PseudoEntity(
        id="external:external-pg",
        kind="external_dependency",
        name="external-pg",
        boundary_targets=("payment",),
    )
    registry = build_registry(environment, ("payment",), pseudo_entities=(pseudo,))
    answer = AnswerState(
        status="provisional",
        ready=True,
        causes=(
            Cause(
                target="payment",
                confidence=0.8,
                mechanism="external 429 crossed the payment boundary",
                support_refs=("ev-001",),
            ),
        ),
        external_causes=(
            ExternalCause(
                id="external:external-pg",
                kind="external_dependency",
                name="external-pg",
                boundary_target="payment",
                evidence_refs=("ev-001",),
            ),
        ),
    )
    validator = HarnessValidator(environment, registry)
    validator.validate_answer(answer)

    score = TypedScorer().score(
        answer,
        (),
        ScoreTarget(
            expected_status="provisional",
            roots=(
                RootExpectation(
                    target_ids=("payment",), target_aliases=("payment",)
                ),
                RootExpectation(
                    pseudo_kind="external_dependency",
                    pseudo_ids=("external:external-pg",),
                    boundary_target_aliases=("payment",),
                ),
            ),
        ),
        validator,
        turns=1,
        max_turns=12,
    )

    assert score.correctness == 1.0


def test_external_root_with_same_kind_but_wrong_identity_is_rejected() -> None:
    environment = EvidenceEnvironment(
        [Evidence(id="ev-001", target="payment", source="trace")]
    )
    registry = build_registry(
        environment,
        ("payment",),
        pseudo_entities=(
            PseudoEntity(
                id="external:external-pg",
                kind="external_dependency",
                name="external-pg",
                boundary_targets=("payment",),
            ),
        ),
    )
    answer = AnswerState(
        status="provisional",
        ready=True,
        external_causes=(
            ExternalCause(
                id="external:payment",
                kind="external_dependency",
                name="payment",
                boundary_target="payment",
                evidence_refs=("ev-001",),
            ),
        ),
    )

    with pytest.raises(ContractError, match="external cause"):
        HarnessValidator(environment, registry).validate_answer(answer)
