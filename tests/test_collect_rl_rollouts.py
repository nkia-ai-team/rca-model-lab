from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

from rca_lab.provenance import model_artifact_identity


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


def test_rollout_manifest_pins_sampling_temperature(tmp_path: Path) -> None:
    module = _module()
    agent = tmp_path / "agent"
    split = tmp_path / "split.yaml"
    eligible = tmp_path / "eligible.jsonl"
    restore = tmp_path / "restore"
    agent.write_bytes(b"agent")
    split.write_text("train: []\n", encoding="utf-8")
    eligible.write_text("", encoding="utf-8")
    restore.write_bytes(b"restore")
    case_root = tmp_path / "cases"
    case_root.mkdir()
    case_dir = case_root / "F01-P"
    case_dir.mkdir()
    (case_dir / "meta.json").write_text('{"case":"F01-P"}\n')
    model_artifact = tmp_path / "model"
    model_artifact.mkdir()
    (model_artifact / "config.json").write_text('{"model_type":"test"}\n')
    args = argparse.Namespace(
        model="actor",
        model_artifact=str(model_artifact),
        model_artifact_sha256="",
        base_url="http://localhost:8003/v1",
        structured_backend="guidance",
        group_size=8,
        temperature=0.7,
        seed=42,
        agent=agent,
        split=split,
        eligible_dataset=eligible,
        restore=restore,
        case_root=case_root,
    )

    contract = module.manifest_contract(args, ["F01-P"])

    assert contract["temperature"] == 0.7
    assert contract["base_seed"] == 42
    assert contract["seed_strategy"].startswith("sha256")
    assert len(contract["restore_sha256"]) == 64
    assert len(contract["case_set_sha256"]) == 64
    assert len(contract["model_artifact_sha256"]) == 64


def test_model_identity_changes_with_model_metadata(tmp_path: Path) -> None:
    artifact = tmp_path / "model"
    artifact.mkdir()
    config = artifact / "config.json"
    config.write_text('{"revision":1}\n')
    first = model_artifact_identity(str(artifact))
    config.write_text('{"revision":2}\n')

    assert first != model_artifact_identity(str(artifact))


def test_rollout_seeds_are_stable_and_distinct() -> None:
    module = _module()

    first = module.rollout_seed(42, "case-a", 1)

    assert first == module.rollout_seed(42, "case-a", 1)
    assert first != module.rollout_seed(42, "case-a", 2)
    assert first != module.rollout_seed(42, "case-b", 1)
    assert 0 <= first < 2**63


def test_default_rl_population_is_the_family_clean_sft_set() -> None:
    module = _module()
    path = module.DEFAULT_ELIGIBLE_DATASET
    eligible = module.load_eligible_scenarios(path)

    assert len(eligible) == 20
    assert "case-f20-r-v3-caf04820" not in eligible
    assert "case-f23-r-v3-04981a78" not in eligible
    assert "case-f25-h-v3-dc3d4fc8" not in eligible


def test_case_set_identity_changes_with_case_inventory(tmp_path: Path) -> None:
    module = _module()
    case = tmp_path / "case-a"
    case.mkdir()
    (case / "meta.json").write_text('{"case":"a"}\n')
    data = case / "data.bin"
    data.write_bytes(b"one")
    first = module.case_set_identity(tmp_path, ["case-a"])
    data.write_bytes(b"longer")

    assert len(first) == 64
    assert first != module.case_set_identity(tmp_path, ["case-a"])
    assert module.case_set_identity(tmp_path, ["missing"]) == ""
