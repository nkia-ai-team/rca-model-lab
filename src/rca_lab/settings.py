from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("RCA_LAB_DATA_ROOT", REPO_ROOT))
SCENARIO_RUNNER_DIR = Path(
    os.environ.get("RCA_SCENARIO_RUNNER_DIR", REPO_ROOT.parent / "rca-scenario-runner")
)

RAW_DIR = DATA_ROOT / "data" / "raw"
SYNTH_DIR = DATA_ROOT / "data" / "synth"
PROCESSED_DIR = DATA_ROOT / "data" / "processed"
MODELS_DIR = DATA_ROOT / "models"
OUTPUTS_DIR = DATA_ROOT / "outputs"
