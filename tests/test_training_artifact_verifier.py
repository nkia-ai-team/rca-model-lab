from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from rca_lab.provenance import file_sha256


def _module():
    spec = importlib.util.spec_from_file_location(
        "verify_training_artifact", Path("scripts/verify_training_artifact.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _artifact(tmp_path: Path) -> tuple[Path, Path, Path]:
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    config = tmp_path / "config.yaml"
    dataset = tmp_path / "dataset.jsonl"
    config.write_text("name: test\n")
    dataset.write_text('{"sample": 1}\n')
    (adapter / "adapter_config.json").write_text("{}\n")
    (adapter / "adapter_model.safetensors").write_bytes(b"weights")
    files = {
        name: file_sha256(adapter / name)
        for name in ("adapter_config.json", "adapter_model.safetensors")
    }
    (adapter / "training_manifest.json").write_text(
        json.dumps(
            {
                "config": {"name": "test"},
                "config_sha256": file_sha256(config),
                "dataset_sha256": file_sha256(dataset),
                "adapter_files_sha256": files,
                "runtime": {"python": "test"},
            }
        )
    )
    return adapter, config, dataset


def test_verifier_binds_adapter_config_weights_and_training_sources(tmp_path: Path) -> None:
    module = _module()
    adapter, config, dataset = _artifact(tmp_path)

    result = module.verify_artifact(
        adapter, config=config, dataset=dataset, require_sources=True
    )

    assert len(result["artifact_sha256"]) == 64
    assert result["config_verified"] is True
    assert result["dataset_verified"] is True


def test_verifier_rejects_mutated_adapter_weights(tmp_path: Path) -> None:
    module = _module()
    adapter, config, dataset = _artifact(tmp_path)
    (adapter / "adapter_model.safetensors").write_bytes(b"mutated")

    with pytest.raises(ValueError, match="checksum mismatch"):
        module.verify_artifact(
            adapter, config=config, dataset=dataset, require_sources=True
        )


def test_verifier_requires_sources_when_requested(tmp_path: Path) -> None:
    module = _module()
    adapter, _, _ = _artifact(tmp_path)

    with pytest.raises(ValueError, match="both --config and --dataset"):
        module.verify_artifact(adapter, require_sources=True)


def test_verifier_rejects_path_traversal_in_manifest(tmp_path: Path) -> None:
    module = _module()
    adapter, _, _ = _artifact(tmp_path)
    manifest = json.loads((adapter / "training_manifest.json").read_text())
    manifest["adapter_files_sha256"]["../escape"] = "0" * 64
    (adapter / "training_manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="basenames"):
        module.verify_artifact(adapter)
