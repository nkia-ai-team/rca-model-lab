"""One deterministic scorer for offline evaluation and RL reward."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rca_lab.harness.models import AnswerState, Observation, ProofType
from rca_lab.harness.validation import HarnessValidator


class RootExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_ids: tuple[str, ...] = ()
    target_aliases: tuple[str, ...] = ()
    pseudo_kind: Literal[
        "external_dependency", "kafka", "redis", "network", "capacity_limit"
    ] | None = None
    pseudo_ids: tuple[str, ...] = ()
    boundary_target_aliases: tuple[str, ...] = ()

    @model_validator(mode="after")
    def complete_identity(self) -> RootExpectation:
        internal = bool(self.target_ids or self.target_aliases)
        pseudo = bool(self.pseudo_kind or self.pseudo_ids or self.boundary_target_aliases)
        if internal == pseudo:
            raise ValueError("root must define exactly one internal or pseudo identity")
        if internal and not self.target_ids:
            raise ValueError("internal root requires canonical target_ids")
        if pseudo and not (
            self.pseudo_kind and self.pseudo_ids and self.boundary_target_aliases
        ):
            raise ValueError(
                "pseudo root requires kind, canonical id, and boundary target"
            )
        if any(not value.startswith("external:") for value in self.pseudo_ids):
            raise ValueError("pseudo root ids must start with external:")
        return self


class RootCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    variant: Literal["target", "pseudo"]
    target_id: str = ""
    target_name: str = ""
    pseudo_id: str = ""
    pseudo_kind: str = ""
    boundary_target: str = ""


def _alias_matches(aliases: tuple[str, ...], value: str) -> bool:
    folded = value.casefold()
    return any(
        (alias_folded := alias.casefold()) in folded or folded in alias_folded
        for alias in aliases
    )


def root_matches(expected: RootExpectation, actual: RootCandidate) -> bool:
    if expected.target_ids:
        return actual.variant == "target" and actual.target_id in expected.target_ids
    return bool(
        actual.variant == "pseudo"
        and actual.pseudo_kind == expected.pseudo_kind
        and actual.pseudo_id in expected.pseudo_ids
        and _alias_matches(
            expected.boundary_target_aliases, actual.boundary_target
        )
    )


def root_f1(
    expected: tuple[RootExpectation, ...] | list[RootExpectation],
    actual: tuple[RootCandidate, ...] | list[RootCandidate],
) -> float:
    remaining = list(actual)
    hits = 0
    for root in expected:
        match = next(
            (
                index
                for index, candidate in enumerate(remaining)
                if root_matches(root, candidate)
            ),
            None,
        )
        if match is not None:
            hits += 1
            remaining.pop(match)
    if not expected:
        return float(not actual)
    precision = hits / max(1, len(actual))
    recall = hits / len(expected)
    return (
        0.0
        if precision + recall == 0
        else 2 * precision * recall / (precision + recall)
    )


class ScoreTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_status: Literal["confirmed", "provisional", "insufficient"]
    roots: tuple[RootExpectation, ...] = Field(min_length=1)


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

        actual = [
            RootCandidate(variant="target", target_id=item.target)
            for item in answer.causes
        ] + [
            RootCandidate(
                variant="pseudo",
                pseudo_id=item.id,
                pseudo_kind=item.kind,
                boundary_target=item.boundary_target,
            )
            for item in answer.external_causes
        ]
        correctness = root_f1(target.roots, actual)
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
