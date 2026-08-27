from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _module():
    spec = importlib.util.spec_from_file_location(
        "collect_rl_rollouts", Path("scripts/collect_rl_rollouts.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_episode(path: Path, event: str = "episode_completed") -> None:
    path.mkdir(parents=True)
    (path / "agent-one.jsonl").write_text(json.dumps({"event": event}) + "\n", encoding="utf-8")


def test_legacy_completed_case_can_resume_without_regeneration(tmp_path: Path) -> None:
    module = _module()
    for index in range(1, 5):
        _write_episode(tmp_path / f"rollout-{index:02d}")

    completed = module.completed_trajectory_dirs(tmp_path, 4)

    assert [path.name for path in completed] == [
        "rollout-01",
        "rollout-02",
        "rollout-03",
        "rollout-04",
    ]


def test_completion_marker_selects_one_atomic_attempt(tmp_path: Path) -> None:
    module = _module()
    selected = []
    for index in range(1, 5):
        path = tmp_path / "attempts" / "attempt-2" / f"rollout-{index:02d}"
        _write_episode(path)
        selected.append(str(path.relative_to(tmp_path)))
    _write_episode(tmp_path / "attempts" / "attempt-1" / "rollout-01")
    (tmp_path / "case-complete.json").write_text(
        json.dumps({"trajectory_dirs": selected}) + "\n", encoding="utf-8"
    )

    completed = module.completed_trajectory_dirs(tmp_path, 4)

    assert [str(path.relative_to(tmp_path)) for path in completed] == selected


def test_truncated_episode_is_not_complete(tmp_path: Path) -> None:
    module = _module()
    _write_episode(tmp_path / "rollout-01", event="turn_completed")

    assert not module.trajectory_completed(tmp_path / "rollout-01")
