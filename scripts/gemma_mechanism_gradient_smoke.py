#!/usr/bin/env python3
"""CPU-only gradient contract check; does not claim a full model backward pass."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import torch
from torch.nn import functional as F

from rca_lab.train.mcp_sft import load_config, weighted_assistant_ce


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    assert config.final_rca_loss_weight == 3.0
    # Same target logits: one masked prompt token and two supervised assistant tokens.
    initial = torch.tensor([[0.1, 0.4, -0.3], [0.2, -0.1, 0.7], [0.0, 0.5, 0.1]])
    labels = torch.tensor([-100, 2, 1])
    gradients, losses = [], []
    for sample_weight in (0.5, 1.5):
        logits = initial.clone().requires_grad_(True)
        selected = labels != -100
        per_token = F.cross_entropy(logits[selected], labels[selected], reduction="none")
        loss, ce = weighted_assistant_ce(per_token, torch.ones_like(per_token), torch.tensor([sample_weight]))
        assert torch.isfinite(loss) and torch.isfinite(ce)
        loss.backward()
        assert torch.isfinite(logits.grad).all() and logits.grad.abs().sum() > 0
        assert logits.grad[0].abs().sum() == 0
        gradients.append(logits.grad)
        losses.append(loss.item())
    assert torch.allclose(gradients[1], gradients[0] * 3, atol=1e-7, rtol=1e-6)
    import rca_lab.train.mcp_sft as implementation
    result = {
        "status": "passed",
        "scope": "synthetic CPU logits through production weighted CE reducer; not model backward",
        "config_sha256": digest(args.config),
        "source_sha256": digest(Path(implementation.__file__)),
        "script_sha256": digest(Path(__file__)),
        "torch_version": torch.__version__,
        "losses": losses,
        "final_to_other_gradient_ratio": 3.0,
        "masked_prompt_gradient": 0.0,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
