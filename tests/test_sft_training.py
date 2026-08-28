from pathlib import Path

import pytest

from rca_lab.train.sft import (
    case_balanced_weights,
    load_sft_config,
    load_verified_training_rows,
    mask_assistant_spans,
)


def test_production_config_preserves_episodes_with_exact_runtime_turns() -> None:
    config = load_sft_config(Path("configs/sft/muse-glimmer-30b-teacher-v3.yaml"))

    assert config.trajectory_mode == "episode_exact_runtime"
    assert config.assistant_only_loss is True
    assert config.case_balanced_loss is True
    assert config.use_liger_kernel is True
    assert config.save_total_limit == 1
    rows = load_verified_training_rows(config)
    assert config.terminal_contract == "configs/eval/train-family-v2.yaml"
    assert config.curation_manifest == (
        "data/synth/teacher-v3-contract-clean/curation-manifest.json"
    )
    assert config.gradient_accumulation_steps == 1
    assert len(rows) == 23
    assert len({row["scenario_id"] for row in rows}) == 20


def test_case_balance_preserves_all_trajectories_with_equal_case_mass() -> None:
    scenarios = ["a", "a", "a", "b"]
    weights = case_balanced_weights(scenarios)
    assert sum(weight for case, weight in zip(scenarios, weights) if case == "a") == 2
    assert sum(weight for case, weight in zip(scenarios, weights) if case == "b") == 2


def test_mask_assistant_spans_keeps_all_assistant_turns_only() -> None:
    start = 10
    assistant = (11,)
    message = 12
    eot = 99
    ids = [
        1,
        2,
        start,
        *assistant,
        77,
        78,
        message,
        20,
        21,
        eot,
        3,
        4,
        start,
        *assistant,
        message,
        30,
        eot,
    ]

    labels = mask_assistant_spans(
        ids,
        start_id=start,
        assistant_role_ids=assistant,
        message_id=message,
        eot_id=eot,
    )

    assert labels == [
        -100,
        -100,
        -100,
        -100,
        -100,
        -100,
        -100,
        20,
        21,
        99,
        -100,
        -100,
        -100,
        -100,
        -100,
        30,
        99,
    ]


def test_mask_assistant_spans_rejects_missing_eot() -> None:
    with pytest.raises(ValueError, match="closing EOT"):
        mask_assistant_spans(
            [1, 10, 11, 12, 20],
            start_id=10,
            assistant_role_ids=(11,),
            message_id=12,
            eot_id=99,
        )
