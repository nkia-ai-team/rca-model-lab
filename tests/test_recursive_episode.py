from pathlib import Path

import pytest
from pydantic import ValidationError

from rca_lab.harness.models import (
    BranchAssessment,
    EvidenceBlocker,
    RecursiveEpisode,
    TeacherCorrection,
    ThoughtBranch,
    TrajectoryArtifact,
)
from rca_lab.synth.recursive import recursive_training_view, sft_training_view


def artifact(name: str) -> TrajectoryArtifact:
    return TrajectoryArtifact(
        path=str(Path("data/synth") / name),
        sha256="a" * 64,
        format="episode_jsonl",
    )


def test_recursive_episode_preserves_rejected_branch_but_only_success_enters_sft() -> None:
    root = ThoughtBranch(
        branch_id="branch-001",
        depth=0,
        teacher="claude",
        artifact=artifact("f23-a1.jsonl"),
        assessment=BranchAssessment(
            scorer="typed-scorer-v1",
            verdict="rejected",
            reward=0.25,
            reasons=("stopped at HTTP 409 symptom",),
            missing_evidence=("inventory level", "RESTOCK inflow"),
        ),
    )
    retry = ThoughtBranch(
        branch_id="branch-002",
        parent_id="branch-001",
        depth=1,
        teacher="codex",
        artifact=artifact("f23-a2.jsonl"),
        assessment=BranchAssessment(
            scorer="typed-scorer-v1",
            verdict="accepted",
            reward=1.0,
            reasons=("monotonic stock depletion and zero RESTOCK observed",),
        ),
        correction_from_parent=TeacherCorrection(
            kind="evidence_gap",
            message="Distinguish normal stockout from stopped replenishment using both series.",
        ),
        include_in_sft=True,
    )

    tree = RecursiveEpisode(
        scenario_id="case-f23-r-v3-04981a78",
        incident_id="incident-f23",
        root_branch_id="branch-001",
        selected_branch_id="branch-002",
        complete=True,
        branches=(root, retry),
    )

    assert tree.selected_branch().branch_id == "branch-002"
    assert tree.branches[0].include_in_recursive_training
    assert not tree.branches[0].include_in_sft
    assert [branch.branch_id for branch in recursive_training_view(tree)] == [
        "branch-001",
        "branch-002",
    ]
    assert sft_training_view(tree).branch_id == "branch-002"


def test_rejected_branch_cannot_enter_sft() -> None:
    with pytest.raises(ValidationError, match="cannot enter SFT"):
        ThoughtBranch(
            branch_id="branch-001",
            depth=0,
            teacher="claude",
            artifact=artifact("bad.jsonl"),
            assessment=BranchAssessment(
                scorer="typed-scorer-v1",
                verdict="rejected",
                reward=0.0,
                reasons=("wrong root cause",),
            ),
            include_in_sft=True,
        )


def test_retry_requires_typed_correction() -> None:
    root = ThoughtBranch(
        branch_id="branch-001",
        depth=0,
        teacher="claude",
        artifact=artifact("root.jsonl"),
        assessment=BranchAssessment(
            scorer="typed-scorer-v1",
            verdict="rejected",
            reward=0.0,
            reasons=("wrong root cause",),
        ),
    )
    retry = ThoughtBranch(
        branch_id="branch-002",
        parent_id="branch-001",
        depth=1,
        teacher="codex",
        artifact=artifact("retry.jsonl"),
        assessment=BranchAssessment(
            scorer="typed-scorer-v1",
            verdict="accepted",
            reward=1.0,
            reasons=("correct",),
        ),
        include_in_sft=True,
    )

    with pytest.raises(ValidationError, match="requires correction"):
        RecursiveEpisode(
            scenario_id="case-f23",
            incident_id="incident-f23",
            root_branch_id="branch-001",
            selected_branch_id="branch-002",
            complete=True,
            branches=(root, retry),
        )


def test_in_progress_tree_preserves_failed_retries_without_fake_success() -> None:
    root = ThoughtBranch(
        branch_id="branch-001",
        depth=0,
        teacher="claude",
        artifact=artifact("root.jsonl"),
        assessment=BranchAssessment(
            scorer="typed-scorer-v1",
            verdict="rejected",
            reward=0.1,
            reasons=("stopped at symptom",),
        ),
    )

    tree = RecursiveEpisode(
        scenario_id="case-f23",
        incident_id="incident-f23",
        root_branch_id="branch-001",
        branches=(root,),
    )

    assert not tree.complete
    assert tree.selected_branch_id is None
    with pytest.raises(ValueError, match="not complete"):
        sft_training_view(tree)


def test_evidence_blocker_prevents_fake_complete_episode() -> None:
    accepted = ThoughtBranch(
        branch_id="branch-001",
        depth=0,
        teacher="codex",
        artifact=artifact("accepted.jsonl"),
        assessment=BranchAssessment(
            scorer="typed-scorer-v1",
            verdict="accepted",
            reward=1.0,
            reasons=("claimed correct root cause",),
        ),
        include_in_sft=True,
    )
    blocker = EvidenceBlocker(
        kind="required_signal_missing",
        required_evidence=("container termination reason",),
        observed_evidence=("database restart symptom only",),
        remediation="Recapture Kubernetes termination state and restart count.",
    )

    with pytest.raises(ValidationError, match="cannot retain evidence blockers"):
        RecursiveEpisode(
            scenario_id="case-f25",
            incident_id="incident-f25",
            root_branch_id="branch-001",
            selected_branch_id="branch-001",
            complete=True,
            evidence_blockers=(blocker,),
            branches=(accepted,),
        )


def test_correction_cannot_expose_hidden_answer() -> None:
    with pytest.raises(ValidationError, match="hidden answer"):
        TeacherCorrection(
            kind="navigation_hint",
            message="The answer is inventory batch stopped.",
            exposes_hidden_answer=True,
        )


def test_f23_recursive_manifest_keeps_both_failed_branches() -> None:
    manifest = Path("configs/teacher/episodes/case-f23-r-v3-04981a78.json")
    tree = RecursiveEpisode.model_validate_json(manifest.read_text(encoding="utf-8"))

    assert not tree.complete
    assert [branch.assessment.verdict for branch in tree.branches] == ["rejected", "rejected"]
    assert all(branch.include_in_recursive_training for branch in tree.branches)
    assert not any(branch.include_in_sft for branch in tree.branches)


@pytest.mark.parametrize(
    "scenario_id",
    [
        "case-f20-r-v3-caf04820",
        "case-f23-r-v3-04981a78",
        "case-f25-h-v3-dc3d4fc8",
    ],
)
def test_evidence_blocked_teacher_manifests_are_typed_and_not_sft_eligible(
    scenario_id: str,
) -> None:
    manifest = Path("configs/teacher/episodes") / f"{scenario_id}.json"
    tree = RecursiveEpisode.model_validate_json(manifest.read_text(encoding="utf-8"))

    assert tree.evidence_blockers
    assert not tree.complete
    assert tree.selected_branch_id is None
    assert not any(branch.include_in_sft for branch in tree.branches)
