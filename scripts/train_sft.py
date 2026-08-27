#!/usr/bin/env python3
"""Run full-trajectory assistant-only SFT."""

from __future__ import annotations

import argparse
from pathlib import Path

from rca_lab.train.sft import train_sft


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "config", type=Path, nargs="?", default=Path("configs/sft/muse-glimmer-30b-teacher-v1.yaml")
    )
    args = parser.parse_args()
    train_sft(args.config)


if __name__ == "__main__":
    main()
