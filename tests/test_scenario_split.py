from pathlib import Path

import pytest

from rca_lab.scenarios.split import TeacherSplit, load_teacher_split

ROOT = Path(__file__).parents[1]


def test_teacher_manifest_covers_current_36_cases_without_overlap() -> None:
    split = load_teacher_split(ROOT / "configs/teacher/codex-blind-v1.yaml")

    assert len(split.train) == 26
    assert len(split.sealed_eval) == 9
    assert len(split.excluded) == 1
    assert len(split.all_cases) == 36
    split.validate_case_root(Path("/data/eval-cases"))


def test_sealed_case_cannot_overlap_training() -> None:
    with pytest.raises(ValueError, match="overlap"):
        TeacherSplit(
            name="bad",
            teacher="codex",
            accepted_trajectories_per_case=1,
            max_attempts_per_case=1,
            max_turns=1,
            train=("case-a",),
            sealed_eval=("case-a",),
            excluded=(),
        )
