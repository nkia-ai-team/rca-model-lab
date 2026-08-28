#!/usr/bin/env python3
"""Merge a LoRA adapter into a standalone inference model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rca_lab.provenance import resolve_model_identity


def scaled_lora_alpha(alpha: float, scale: float) -> float:
    """Return the LoRA alpha that applies ``scale`` to the learned delta."""
    if not 0 < scale <= 1:
        raise ValueError("adapter scale must be in (0, 1]")
    return alpha * scale


def build_merge_manifest(
    base: str,
    adapter: str,
    *,
    adapter_scale: float,
    original_lora_alpha: float,
) -> dict[str, object]:
    """Bind a merged model to immutable base and adapter identities."""
    return {
        "base": base,
        "base_sha256": resolve_model_identity(base),
        "adapter": adapter,
        "adapter_sha256": resolve_model_identity(adapter),
        "adapter_scale": adapter_scale,
        "original_lora_alpha": original_lora_alpha,
        "effective_lora_alpha": scaled_lora_alpha(original_lora_alpha, adapter_scale),
    }


def main() -> None:
    import torch
    from peft import PeftConfig, PeftModel
    from transformers import AutoModelForCausalLM, AutoModelForImageTextToText, AutoTokenizer

    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--adapter-scale",
        type=float,
        default=1.0,
        help="Multiplier for the learned LoRA delta before merge (0 < scale <= 1)",
    )
    parser.add_argument(
        "--device-map",
        default="cpu",
        help="Transformers device_map used for merge (default: cpu, safe beside vLLM)",
    )
    args = parser.parse_args()
    if not 0 < args.adapter_scale <= 1:
        parser.error("--adapter-scale must be in (0, 1]")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error(f"refusing to overwrite non-empty output: {args.output}")
    try:
        merge_manifest = build_merge_manifest(
            args.base,
            args.adapter,
            adapter_scale=args.adapter_scale,
            original_lora_alpha=float(PeftConfig.from_pretrained(args.adapter).lora_alpha),
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    kwargs = {"torch_dtype": torch.bfloat16, "device_map": args.device_map}
    try:
        base = AutoModelForCausalLM.from_pretrained(args.base, **kwargs)
    except ValueError:
        base = AutoModelForImageTextToText.from_pretrained(args.base, **kwargs)
    adapter_config = PeftConfig.from_pretrained(args.adapter)
    original_alpha = float(adapter_config.lora_alpha)
    adapter_config.lora_alpha = scaled_lora_alpha(original_alpha, args.adapter_scale)
    merged = PeftModel.from_pretrained(
        base,
        args.adapter,
        config=adapter_config,
    ).merge_and_unload()
    args.output.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(args.output, safe_serialization=True, max_shard_size="5GB")
    AutoTokenizer.from_pretrained(args.base).save_pretrained(args.output)
    (args.output / "merge_manifest.json").write_text(
        json.dumps(merge_manifest, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
