#!/usr/bin/env python3
"""Run full-trajectory assistant-only SFT."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _reexec_with_isolated_training_environment() -> None:
    """Keep container-level user packages out and configure CUDA before torch loads."""
    if os.environ.get("RCA_TRAIN_ENV_READY") == "1":
        return
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    environment["RCA_TRAIN_ENV_READY"] = "1"
    project_python = Path(__file__).resolve().parents[1] / ".venv/bin/python"
    executable = str(project_python) if project_python.is_file() else sys.executable
    os.execve(executable, [executable, *sys.argv], environment)


def main() -> None:
    _reexec_with_isolated_training_environment()
    from rca_lab.train.sft import train_sft

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "config", type=Path, nargs="?", default=Path("configs/sft/muse-glimmer-30b-teacher-v1.yaml")
    )
    args = parser.parse_args()
    train_sft(args.config)


if __name__ == "__main__":
    main()
