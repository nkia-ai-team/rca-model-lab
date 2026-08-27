from __future__ import annotations

from pathlib import Path

import pytest

from rca_lab.train.rl import _response_shape, clipped_surrogate, load_rl_config


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


def test_asymmetric_clipping_handles_positive_and_negative_advantage() -> None:
    torch = pytest.importorskip("torch")
    ratios = torch.tensor([2.0, 0.2]).log()
    positive = clipped_surrogate(ratios, torch.tensor(1.0), clip_low=0.2, clip_high=0.28)
    negative = clipped_surrogate(ratios, torch.tensor(-1.0), clip_low=0.2, clip_high=0.28)
    assert positive.tolist() == pytest.approx([1.28, 0.2])
    assert negative.tolist() == pytest.approx([-2.0, -0.8])
