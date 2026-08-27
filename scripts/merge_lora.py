#!/usr/bin/env python3
"""Merge a LoRA adapter into a standalone inference model."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoModelForImageTextToText, AutoTokenizer

    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--device-map",
        default="cpu",
        help="Transformers device_map used for merge (default: cpu, safe beside vLLM)",
    )
    args = parser.parse_args()
    kwargs = {"torch_dtype": torch.bfloat16, "device_map": args.device_map}
    try:
        base = AutoModelForCausalLM.from_pretrained(args.base, **kwargs)
    except ValueError:
        base = AutoModelForImageTextToText.from_pretrained(args.base, **kwargs)
    merged = PeftModel.from_pretrained(base, args.adapter).merge_and_unload()
    args.output.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(args.output, safe_serialization=True, max_shard_size="5GB")
    AutoTokenizer.from_pretrained(args.base).save_pretrained(args.output)


if __name__ == "__main__":
    main()
