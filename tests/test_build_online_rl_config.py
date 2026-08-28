from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "build_online_rl_config", Path("scripts/build_online_rl_config.py")
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
LANGUAGE_ONLY_TARGETS = module.LANGUAGE_ONLY_TARGETS
build_config = module.build_config


def _write_inputs(tmp_path: Path, *, temperature: float = 1.0) -> tuple[Path, Path]:
    behavior_sha = "a" * 64
    base_sha = "b" * 64
    manifest = tmp_path / "run-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "model_artifact": "/models/sft-adapter",
                "model_artifact_sha256": behavior_sha,
                "base_model_artifact": "/models/base",
                "base_model_artifact_sha256": base_sha,
                "temperature": temperature,
            }
        ),
        encoding="utf-8",
    )
    dataset = tmp_path / "rl.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "scenario_id": "case-a",
                "rollout_id": "rollout-1",
                "reward": 1.0,
                "optimization_reward": 1.0,
                "advantage": 1.0,
                "turn_advantages": [1.0],
                "behavior_model_artifact": "/models/sft-adapter",
                "behavior_model_sha256": behavior_sha,
                "base_model_artifact": "/models/base",
                "base_model_sha256": base_sha,
                "behavior_temperature": 1.0,
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
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest, dataset


def test_builds_exact_adapter_continuation_config(tmp_path: Path) -> None:
    manifest, dataset = _write_inputs(tmp_path)
    config = build_config(
        rollout_manifest=manifest,
        dataset=dataset,
        output_dir=tmp_path / "rl-output",
        name="online-v1",
    )

    assert config.algorithm == "episode_online_progressive_grpo_lora"
    assert config.model_name == "/models/base"
    assert config.initial_adapter == "/models/sft-adapter"
    assert config.initial_adapter_sha256 == "a" * 64
    assert config.base_model_sha256 == "b" * 64
    assert config.lora.target_modules == LANGUAGE_ONLY_TARGETS


def test_rejects_non_unit_rollout_temperature(tmp_path: Path) -> None:
    manifest, dataset = _write_inputs(tmp_path, temperature=0.8)

    with pytest.raises(ValueError, match="temperature 1.0"):
        build_config(
            rollout_manifest=manifest,
            dataset=dataset,
            output_dir=tmp_path / "rl-output",
            name="online-v1",
        )


def test_rejects_dataset_from_another_behavior_policy(tmp_path: Path) -> None:
    manifest, dataset = _write_inputs(tmp_path)
    payload = json.loads(dataset.read_text(encoding="utf-8"))
    payload["behavior_model_sha256"] = "c" * 64
    dataset.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="does not match"):
        build_config(
            rollout_manifest=manifest,
            dataset=dataset,
            output_dir=tmp_path / "rl-output",
            name="online-v1",
        )
