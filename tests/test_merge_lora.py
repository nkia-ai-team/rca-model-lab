from __future__ import annotations

import runpy
from pathlib import Path

import pytest

module = runpy.run_path(Path(__file__).parents[1] / "scripts" / "merge_lora.py")
scaled_lora_alpha = module["scaled_lora_alpha"]
build_merge_manifest = module["build_merge_manifest"]


def test_scaled_lora_alpha_shrinks_policy_delta() -> None:
    assert scaled_lora_alpha(8, 0.25) == 2
    assert scaled_lora_alpha(8, 1.0) == 8


@pytest.mark.parametrize("scale", [0, -0.1, 1.01])
def test_scaled_lora_alpha_rejects_non_conservative_scale(scale: float) -> None:
    with pytest.raises(ValueError, match="adapter scale"):
        scaled_lora_alpha(8, scale)


def test_merge_manifest_binds_base_and_adapter_artifacts(tmp_path: Path) -> None:
    base = tmp_path / "base"
    adapter = tmp_path / "adapter"
    base.mkdir()
    adapter.mkdir()
    (base / "config.json").write_text('{"base": true}\n')
    (adapter / "adapter_config.json").write_text('{"rank": 8}\n')
    (adapter / "training_manifest.json").write_text('{"weights": "sha"}\n')

    manifest = build_merge_manifest(
        str(base),
        str(adapter),
        adapter_scale=0.25,
        original_lora_alpha=8,
    )

    assert len(manifest["base_sha256"]) == 64
    assert len(manifest["adapter_sha256"]) == 64
    assert manifest["effective_lora_alpha"] == 2
