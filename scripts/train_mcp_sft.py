"""Train a LoRA adapter on rca-mcp episodes.

  uv run python scripts/train_mcp_sft.py configs/sft/muse-glimmer-30b-mcp-v1.yaml [--resume-from-checkpoint DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rca_lab.train.mcp_sft import train


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--resume-from-checkpoint", type=Path, default=None)
    args = parser.parse_args()
    train(args.config, resume_from_checkpoint=args.resume_from_checkpoint)


if __name__ == "__main__":
    main()
