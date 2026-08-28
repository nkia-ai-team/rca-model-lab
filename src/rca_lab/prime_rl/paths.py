"""Resolve RCA project assets for local or remote Prime-RL processes."""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    configured = os.environ.get("RCA_LAB_ROOT")
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        # .../src/rca_lab/prime_rl/paths.py -> repository root
        root = Path(__file__).resolve().parents[3]
    if not (root / "pyproject.toml").is_file():
        raise ValueError(f"RCA_LAB_ROOT is not an RCA model-lab checkout: {root}")
    return root


def project_path(relative: str) -> Path:
    return project_root() / relative
