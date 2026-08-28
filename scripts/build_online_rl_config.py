#!/usr/bin/env python3
"""Build a fail-closed online GRPO config from immutable rollout provenance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from rca_lab.provenance import file_sha256
from rca_lab.train.rl import (
    EpisodeRLConfig,
    RLEpisodeRecord,
    validate_behavior_policy,
)

LANGUAGE_ONLY_TARGETS = (
    r"^model\.language_model\..*\."
    r"(q_proj|k_proj|v_proj|o_proj|up_proj|down_proj|gate_proj)$"
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def _dataset_rows(path: Path) -> list[dict[str, Any]]:
    rows = [
        RLEpisodeRecord.model_validate(json.loads(line)).model_dump(mode="json")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise ValueError("RL dataset is empty")
    return rows


def build_config(
    *,
    rollout_manifest: Path,
    dataset: Path,
    output_dir: Path,
    name: str,
    learning_rate: float = 1e-6,
    gradient_accumulation_steps: int = 4,
) -> EpisodeRLConfig:
    manifest = _read_json(rollout_manifest)
    behavior_artifact = str(manifest.get("model_artifact", ""))
    behavior_sha = str(manifest.get("model_artifact_sha256", ""))
    base_artifact = str(manifest.get("base_model_artifact", ""))
    base_sha = str(manifest.get("base_model_artifact_sha256", ""))
    temperature = float(manifest.get("temperature", 0.0))
    if temperature != 1.0:
        raise ValueError("online GRPO requires rollout temperature 1.0")

    payload = {
        "name": name,
        "algorithm": "episode_online_progressive_grpo_lora",
        "model_name": base_artifact,
        "initial_adapter": behavior_artifact,
        "initial_adapter_sha256": behavior_sha,
        "behavior_model_sha256": behavior_sha,
        "base_model_sha256": base_sha,
        "behavior_temperature": 1.0,
        "dataset": str(dataset),
        "dataset_sha256": file_sha256(dataset),
        "output_dir": str(output_dir),
        "max_length": 32768,
        "epochs": 1,
        "learning_rate": learning_rate,
        "gradient_accumulation_steps": gradient_accumulation_steps,
        "clip_low": 0.20,
        "clip_high": 0.28,
        "kl_beta": 0.10,
        "max_grad_norm": 1.0,
        "seed": 42,
        "lora": {
            "rank": 8,
            "alpha": 16,
            "dropout": 0.0,
            "target_modules": LANGUAGE_ONLY_TARGETS,
        },
        "wandb": {"entity": "nkia-ai", "project": "rca-actor-rl"},
    }
    config = EpisodeRLConfig.model_validate(payload)
    validate_behavior_policy(_dataset_rows(dataset), config)
    return config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rollout-manifest", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--learning-rate", type=float, default=1e-6)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=4)
    args = parser.parse_args()
    try:
        config = build_config(
            rollout_manifest=args.rollout_manifest,
            dataset=args.dataset,
            output_dir=args.output_dir,
            name=args.name,
            learning_rate=args.learning_rate,
            gradient_accumulation_steps=args.gradient_accumulation_steps,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"FAIL: {error}") from error
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    print(args.output)


if __name__ == "__main__":
    main()
