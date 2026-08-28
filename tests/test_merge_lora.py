from __future__ import annotations

import runpy
from pathlib import Path

import pytest

scaled_lora_alpha = runpy.run_path(
    Path(__file__).parents[1] / "scripts" / "merge_lora.py"
)["scaled_lora_alpha"]


def test_scaled_lora_alpha_shrinks_policy_delta() -> None:
    assert scaled_lora_alpha(8, 0.25) == 2
    assert scaled_lora_alpha(8, 1.0) == 8


@pytest.mark.parametrize("scale", [0, -0.1, 1.01])
def test_scaled_lora_alpha_rejects_non_conservative_scale(scale: float) -> None:
    with pytest.raises(ValueError, match="adapter scale"):
        scaled_lora_alpha(8, scale)
