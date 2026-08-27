from datetime import UTC, datetime, timedelta

from rca_lab.harness.environment import EvidenceEnvironment
from rca_lab.harness.models import AnswerState, Cause, Evidence, Fact, FactKind, Observation
from rca_lab.harness.registry import build_registry
from rca_lab.harness.scorer import ScoreTarget, TypedScorer
from rca_lab.harness.validation import HarnessValidator

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
        ScoreTarget(expected_status="confirmed", expected_target="db"),
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
        ScoreTarget(expected_status="confirmed", expected_target="db"),
        validator,
        turns=1,
        max_turns=12,
    )

    assert score.penalty == 1
    assert "unsupported confirmation" in score.reasons
