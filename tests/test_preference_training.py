from __future__ import annotations

from pathlib import Path

import pytest

from rca_lab.provenance import file_sha256
from rca_lab.train.preference import (
    TrajectoryPreferenceRecord,
    load_preference_config,
    preference_loss_and_slope,
    regularized_preference_loss_and_slopes,
    validate_preference_dataset,
)


def test_rank_refinement_config_is_typed() -> None:
    config = load_preference_config(
        Path("configs/rl/muse-glimmer-30b-rank-refinement-v6.yaml")
    )
    assert config.beta == 0.1
    assert config.imitation_eta == 0.005
    assert config.learning_rate == 5e-7


def test_preference_loss_pushes_chosen_margin_up() -> None:
    torch = pytest.importorskip("torch")
    margin = torch.tensor(0.0, requires_grad=True)
    loss, slope = preference_loss_and_slope(margin, beta=0.1)
    loss.backward()
    assert loss.item() == pytest.approx(0.693147, rel=1e-5)
    assert slope.item() < 0
    assert margin.grad.item() == pytest.approx(slope.item())


def test_regularized_preference_loss_anchors_chosen_trajectory() -> None:
    torch = pytest.importorskip("torch")
    margin = torch.tensor(0.0)
    chosen_mean_logp = torch.tensor(-2.0)
    loss, chosen_slope, rejected_slope, imitation_loss = (
        regularized_preference_loss_and_slopes(
            margin,
            chosen_mean_logp,
            beta=0.1,
            imitation_eta=0.005,
        )
    )
    assert imitation_loss.item() == pytest.approx(2.0)
    assert loss.item() == pytest.approx(0.694147, rel=1e-5)
    assert chosen_slope.item() == pytest.approx(-0.0505)
    assert rejected_slope.item() == pytest.approx(0.05)


def test_preference_dataset_hash_rejects_mutation(tmp_path: Path) -> None:
    dataset = tmp_path / "pairs.jsonl"
    dataset.write_text("one\n")
    expected = file_sha256(dataset)
    validate_preference_dataset(dataset, expected)
    dataset.write_text("two\n")
    with pytest.raises(ValueError, match="sha256 mismatch"):
        validate_preference_dataset(dataset, expected)


def test_preference_pair_keeps_complete_trajectories() -> None:
    turn = {
        "messages": [
            {"role": "system", "content": "RCA"},
            {"role": "user", "content": "incident"},
            {"role": "assistant", "content": "action"},
        ]
    }
    record = TrajectoryPreferenceRecord.model_validate(
        {
            "scenario_id": "case-a",
            "pair_id": "pair-a",
            "chosen_source": "teacher:a",
            "rejected_source": "student:b",
            "chosen_turns": [turn, turn],
            "rejected_turns": [turn],
            "chosen_score": {},
            "rejected_score": {},
        }
    )
    assert len(record.chosen_turns) == 2
    assert len(record.rejected_turns) == 1
