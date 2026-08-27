"""Typed student harness contracts shared by synthesis, evaluation, and RL."""

from rca_lab.harness.environment import EvidenceEnvironment
from rca_lab.harness.models import (
    ActionName,
    ActionRequest,
    AnswerState,
    BranchAssessment,
    BranchVerdict,
    CapabilityRegistry,
    Cause,
    CorrectionKind,
    Episode,
    Evidence,
    Fact,
    FactKind,
    Observation,
    ProofType,
    RecursiveEpisode,
    TeacherCorrection,
    ThoughtBranch,
    TrajectoryArtifact,
)
from rca_lab.harness.scorer import ScoreBreakdown, ScoreTarget, TypedScorer
from rca_lab.harness.validation import ContractError, HarnessValidator, ProofReport

__all__ = [
    "ActionName",
    "ActionRequest",
    "AnswerState",
    "BranchAssessment",
    "BranchVerdict",
    "CapabilityRegistry",
    "Cause",
    "ContractError",
    "CorrectionKind",
    "Episode",
    "Evidence",
    "EvidenceEnvironment",
    "Fact",
    "FactKind",
    "HarnessValidator",
    "Observation",
    "ProofReport",
    "ProofType",
    "RecursiveEpisode",
    "ScoreBreakdown",
    "ScoreTarget",
    "TeacherCorrection",
    "ThoughtBranch",
    "TrajectoryArtifact",
    "TypedScorer",
]
