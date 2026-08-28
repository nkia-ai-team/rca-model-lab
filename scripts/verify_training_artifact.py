#!/usr/bin/env python3
"""Fail closed when a trained LoRA adapter differs from its immutable manifest."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from pydantic import Field, field_validator

from rca_lab.harness.models import StrictModel
from rca_lab.provenance import file_sha256, model_artifact_identity

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_ADAPTER_FILES = frozenset({"adapter_config.json", "adapter_model.safetensors"})


class TrainingArtifactManifest(StrictModel):
    config: dict[str, object]
    config_sha256: str
    dataset_sha256: str
    adapter_files_sha256: dict[str, str] = Field(min_length=2)
    runtime: dict[str, object]

    @field_validator("config_sha256", "dataset_sha256")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("must be a lowercase SHA-256 digest")
        return value

    @field_validator("adapter_files_sha256")
    @classmethod
    def valid_adapter_files(cls, value: dict[str, str]) -> dict[str, str]:
        unsafe = sorted(name for name in value if Path(name).name != name)
        if unsafe:
            raise ValueError(f"adapter filenames must be basenames: {unsafe}")
        missing = sorted(_REQUIRED_ADAPTER_FILES - set(value))
        if missing:
            raise ValueError(f"adapter manifest is incomplete: missing={missing}")
        invalid = sorted(name for name, digest in value.items() if not _SHA256.fullmatch(digest))
        if invalid:
            raise ValueError(f"adapter file digest is invalid: {invalid}")
        return value


def load_manifest(adapter: Path) -> TrainingArtifactManifest:
    manifest = adapter / "training_manifest.json"
    if not manifest.is_file():
        raise ValueError(f"missing training manifest: {manifest}")
    return TrainingArtifactManifest.model_validate_json(manifest.read_text(encoding="utf-8"))


def verify_artifact(
    adapter: Path,
    *,
    config: Path | None = None,
    dataset: Path | None = None,
    require_sources: bool = False,
) -> dict[str, object]:
    if require_sources and (config is None or dataset is None):
        raise ValueError("--require-sources needs both --config and --dataset")
    manifest = load_manifest(adapter)
    mismatches: dict[str, dict[str, str]] = {}
    for name, expected in manifest.adapter_files_sha256.items():
        path = adapter / name
        actual = file_sha256(path) if path.is_file() else "missing"
        if actual != expected:
            mismatches[name] = {"actual": actual, "expected": expected}
    if mismatches:
        raise ValueError(f"adapter file checksum mismatch: {mismatches}")
    if config is not None and file_sha256(config) != manifest.config_sha256:
        raise ValueError("training config checksum mismatch")
    if dataset is not None and file_sha256(dataset) != manifest.dataset_sha256:
        raise ValueError("training dataset checksum mismatch")
    identity = model_artifact_identity(str(adapter))
    if not identity:
        raise ValueError("could not derive adapter artifact identity")
    return {
        "adapter": str(adapter),
        "artifact_sha256": identity,
        "verified_files": sorted(manifest.adapter_files_sha256),
        "config_verified": config is not None,
        "dataset_verified": dataset is not None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--require-sources", action="store_true")
    args = parser.parse_args()
    try:
        result = verify_artifact(
            args.adapter,
            config=args.config,
            dataset=args.dataset,
            require_sources=args.require_sources,
        )
    except (OSError, ValueError) as error:
        raise SystemExit(f"FAIL: {error}") from error
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
