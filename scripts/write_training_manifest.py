#!/usr/bin/env python3
"""Validate and backfill an immutable manifest for a completed RL adapter."""

from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path

from rca_lab.provenance import file_sha256
from rca_lab.train.rl import load_rl_config, validate_dataset_artifact


def adapter_file_hashes(adapter_dir: Path) -> dict[str, str]:
    if not adapter_dir.is_dir():
        raise ValueError(f"adapter directory does not exist: {adapter_dir}")
    adapter_files = sorted(
        path
        for path in adapter_dir.iterdir()
        if path.is_file() and path.name != "training_manifest.json"
    )
    required = {"adapter_config.json", "adapter_model.safetensors"}
    missing = sorted(required - {path.name for path in adapter_files})
    if missing:
        raise ValueError(f"adapter is incomplete: missing={missing}")
    return {path.name: file_sha256(path) for path in adapter_files}


def build_manifest(config_path: Path, adapter_dir: Path) -> dict[str, object]:
    import torch
    import transformers
    from peft import __version__ as peft_version

    config = load_rl_config(config_path)
    dataset = Path(config.dataset)
    validate_dataset_artifact(dataset, config)
    adapter_hashes = adapter_file_hashes(adapter_dir)
    return {
        "config": config.model_dump(mode="json"),
        "config_sha256": file_sha256(config_path),
        "dataset_sha256": file_sha256(dataset),
        "adapter_files_sha256": adapter_hashes,
        "runtime": {
            "python": platform.python_version(),
            "torch": torch.__version__,
            "cuda": torch.version.cuda,
            "transformers": transformers.__version__,
            "peft": peft_version,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--adapter", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = build_manifest(args.config, args.adapter)
    except (OSError, ValueError) as error:
        raise SystemExit(f"FAIL: {error}") from error
    output = args.adapter / "training_manifest.json"
    output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
