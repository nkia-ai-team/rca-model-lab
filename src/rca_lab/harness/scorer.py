"""One deterministic scorer for offline evaluation and RL reward."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from rca_lab.harness.models import AnswerState, Observation, ProofType
from rca_lab.harness.validation import HarnessValidator


class ScoreTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_status: str
    expected_target: str = ""
    expected_pseudo_kind: str = ""
    expected_targets: tuple[str, ...] = ()
    expected_pseudo_kinds: tuple[str, ...] = ()


class ScoreBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    correctness: float = Field(ge=0, le=1)
    proof: float = Field(ge=0, le=1)
    calibration: float = Field(ge=0, le=1)
    efficiency: float = Field(ge=0, le=1)
    tool_success: float = Field(ge=0, le=1)
    penalty: float = Field(ge=0, le=1)
    total: float = Field(ge=0, le=1)
    reasons: tuple[str, ...] = ()


class TypedScorer:
    """Exact port of the v4 Go harness reward weights."""

    def score(
        self,
        answer: AnswerState,
        ledger: tuple[Observation, ...],
        target: ScoreTarget,
        validator: HarnessValidator,
        *,
        turns: int,
        max_turns: int,
    ) -> ScoreBreakdown:
        reasons: list[str] = []
        calibration = float(answer.status == target.expected_status)
        if not calibration:
            reasons.append("status mismatch")

        expected = set(target.expected_targets or ((target.expected_target,) if target.expected_target else ()))
        expected_pseudo = set(
            target.expected_pseudo_kinds
            or ((target.expected_pseudo_kind,) if target.expected_pseudo_kind else ())
        )
        wanted = {f"target:{item}" for item in expected} | {
            f"pseudo:{item}" for item in expected_pseudo
        }
        actual = {f"target:{item.target}" for item in answer.causes} | {
            f"pseudo:{item.kind}" for item in answer.external_causes
        }
        if not wanted:
            correctness = float(not actual)
        else:
            hits = len(wanted & actual)
            precision = hits / max(1, len(actual))
            recall = hits / len(wanted)
            correctness = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
        if correctness < 1:
            reasons.append("root cause mismatch")

        reports = [validator.evaluate_proof(cause) for cause in answer.causes]
        proof = sum(report.satisfied for report in reports) / len(reports) if reports else 0.0
        efficiency = max(0.0, 1.0 - max(0, turns - 1) / max_turns) if max_turns > 0 else 0.0
        tool_success = sum(item.ok for item in ledger) / len(ledger) if ledger else 0.0

        penalty = 0.0
        if answer.status == "confirmed" and (proof < 1 or correctness < 1):
            penalty = 1.0
            reasons.append("unsupported confirmation")
        for cause, report in zip(answer.causes, reports, strict=True):
            if cause.proof_type in {ProofType.METRIC_CAUSAL, ProofType.RESOURCE_SATURATION} and not report.satisfied:
                penalty = 1.0
                reasons.append("metric proof lacks retained full series")

        total = min(
            1.0,
            max(
                0.0,
                0.45 * correctness
                + 0.25 * proof
                + 0.15 * calibration
                + 0.10 * efficiency
                + 0.05 * tool_success
                - 0.50 * penalty,
            ),
        )
        return ScoreBreakdown(
            correctness=correctness,
            proof=proof,
            calibration=calibration,
            efficiency=efficiency,
            tool_success=tool_success,
            penalty=penalty,
            total=total,
            reasons=tuple(reasons),
        )
