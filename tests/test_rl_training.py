from __future__ import annotations

from pathlib import Path

import pytest

from rca_lab.provenance import file_sha256
from rca_lab.train.rl import (
    RLEpisodeRecord,
    _response_shape,
    clipped_surrogate,
    load_rl_config,
    validate_behavior_policy,
    validate_dataset_artifact,
)


def test_rl_config_locks_episode_dapo_contract() -> None:
    config = load_rl_config(Path("configs/rl/muse-glimmer-30b-dapo-v1.yaml"))
    assert config.algorithm == "episode_dapo_lora"
    assert config.clip_high > config.clip_low
    assert config.lora.rank == 8
    assert config.behavior_temperature == 1.0


def test_response_must_be_a_contiguous_suffix() -> None:
    assert _response_shape([-100, -100, 7, 8]) == (2, 2)
    with pytest.raises(ValueError, match="contiguous suffix"):
        _response_shape([-100, 7, -100, 8])


def test_turn_advantages_must_align_with_whole_episode() -> None:
    base = {
        "scenario_id": "case-a",
        "rollout_id": "rollout-1",
        "reward": 0.5,
        "optimization_reward": 0.5,
        "advantage": 0.0,
        "score": {},
        "turns": [
            {
                "messages": [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                    {"role": "assistant", "content": "a"},
                ]
            }
        ],
    }
    with pytest.raises(ValueError, match="turn_advantages"):
        RLEpisodeRecord.model_validate({**base, "turn_advantages": [0.1, 0.2]})


def test_progressive_training_rejects_off_policy_rollouts() -> None:
    config = load_rl_config(Path("configs/rl/muse-glimmer-30b-dapo-v1.yaml")).model_copy(
        update={
            "algorithm": "episode_progressive_dapo_lora",
            "behavior_temperature": 0.7,
            "behavior_model_sha256": "a" * 64,
        }
    )
    row = {
        "turn_advantages": [1.0],
        "behavior_model_artifact": config.model_name,
        "behavior_model_sha256": "b" * 64,
        "behavior_temperature": 0.7,
    }

    with pytest.raises(ValueError, match="does not match"):
        validate_behavior_policy([row], config)


def test_progressive_training_rejects_changed_dataset(tmp_path: Path) -> None:
    dataset = tmp_path / "rl.jsonl"
    dataset.write_text("original\n")
    digest = file_sha256(dataset)
    config = load_rl_config(Path("configs/rl/muse-glimmer-30b-dapo-v1.yaml")).model_copy(
        update={
            "algorithm": "episode_progressive_dapo_lora",
            "dataset_sha256": digest,
        }
    )
    validate_dataset_artifact(dataset, config)
    dataset.write_text("changed\n")

    with pytest.raises(ValueError, match="dataset sha256 mismatch"):
        validate_dataset_artifact(dataset, config)


def test_asymmetric_clipping_handles_positive_and_negative_advantage() -> None:
    torch = pytest.importorskip("torch")
    ratios = torch.tensor([2.0, 0.2]).log()
    positive = clipped_surrogate(ratios, torch.tensor(1.0), clip_low=0.2, clip_high=0.28)
    negative = clipped_surrogate(ratios, torch.tensor(-1.0), clip_low=0.2, clip_high=0.28)
    assert positive.tolist() == pytest.approx([1.28, 0.2])
    assert negative.tolist() == pytest.approx([-2.0, -0.8])
