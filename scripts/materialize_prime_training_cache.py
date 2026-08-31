#!/usr/bin/env python3
"""Materialize a dense Prime-RL loading cache from the pinned FP8 source."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rca_lab.provenance import file_sha256
from rca_lab.train.checkpoint import (
    load_training_model,
    save_training_tokenizer_artifacts,
    tokenizer_artifact_identity,
    verify_training_checkpoint,
)
from rca_lab.train.sft import load_sft_config


def build_cache_manifest(
    *,
    config_path: Path,
    output: Path,
    source: dict[str, object],
    removed_weight_scale_tensors: int,
) -> dict[str, object]:
    return {
        "purpose": "prime_rl_dense_loading_cache",
        "source": source,
        "source_config": str(config_path),
        "source_config_sha256": file_sha256(config_path),
        "cache_config_sha256": file_sha256(output / "config.json"),
        "cache_index_sha256": file_sha256(output / "model.safetensors.index.json"),
        "cache_tokenizer_sha256": tokenizer_artifact_identity(output),
        "cache_weight_dtype": "bfloat16",
        "removed_weight_scale_tensors": removed_weight_scale_tensors,
        "disposable": True,
    }


def main() -> None:  # pragma: no cover - GPU entrypoint
    import os

    import torch

    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error(f"refusing to overwrite non-empty cache: {args.output}")
    config = load_sft_config(args.config)
    source = verify_training_checkpoint(config)
    if config.checkpoint_format != "compressed_tensors_fp8_block":
        parser.error("cache materialization requires a compressed FP8 source")
    args.output.mkdir(parents=True, exist_ok=True)
    incomplete = args.output / ".incomplete"
    incomplete.write_text("materializing\n", encoding="utf-8")
    model = load_training_model(
        config,
        torch_module=torch,
        attention_implementation=os.environ.get("ATTN", "sdpa"),
    )
    # The cache stores the already-dequantized tensors. Leaving the source
    # quantization_config would cause downstream loaders to interpret BF16
    # tensors as compressed weights a second time.
    if hasattr(model.config, "quantization_config"):
        delattr(model.config, "quantization_config")
    state_dict = model.state_dict()
    scale_keys = {key for key in state_dict if key.endswith(".weight_scale")}
    dense_state_dict = {key: tensor for key, tensor in state_dict.items() if key not in scale_keys}
    model.save_pretrained(
        args.output,
        state_dict=dense_state_dict,
        safe_serialization=True,
        max_shard_size="10GB",
    )
    save_training_tokenizer_artifacts(config, args.output)
    manifest = build_cache_manifest(
        config_path=args.config,
        output=args.output,
        source=source,
        removed_weight_scale_tensors=len(scale_keys),
    )
    (args.output / "training_cache_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    incomplete.unlink()


if __name__ == "__main__":
    main()
