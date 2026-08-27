#!/usr/bin/env python3
"""Run episode-preserving DAPO LoRA training."""

from __future__ import annotations

import argparse
from pathlib import Path

from rca_lab.train.rl import train_rl


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    train_rl(args.config)


if __name__ == "__main__":
    main()
