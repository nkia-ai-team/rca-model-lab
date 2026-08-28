#!/usr/bin/env python3
"""Train whole-trajectory RCA preference refinement LoRA."""

import sys
from pathlib import Path

from rca_lab.train.preference import train_preference

if __name__ == "__main__":
    train_preference(Path(sys.argv[1]))
