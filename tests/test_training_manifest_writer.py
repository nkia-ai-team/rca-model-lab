from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _module():
    spec = importlib.util.spec_from_file_location(
        "write_training_manifest", Path("scripts/write_training_manifest.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_writer_rejects_incomplete_adapter(tmp_path: Path) -> None:
    module = _module()
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "adapter_config.json").write_text("{}\n")
    with pytest.raises(ValueError, match="adapter_model.safetensors"):
        module.adapter_file_hashes(adapter)


def test_manifest_writer_hashes_complete_adapter(tmp_path: Path) -> None:
    module = _module()
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "adapter_config.json").write_text("{}\n")
    (adapter / "adapter_model.safetensors").write_bytes(b"weights")

    hashes = module.adapter_file_hashes(adapter)

    assert set(hashes) == {"adapter_config.json", "adapter_model.safetensors"}
    assert all(len(value) == 64 for value in hashes.values())
