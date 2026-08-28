#!/usr/bin/env python3
"""Run and score the fixed train-only monitor under sealed evaluation mechanics."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml


def monitor_command(args: argparse.Namespace, cases: list[str]) -> list[str]:
    command = [
        sys.executable,
        "scripts/run_sealed_eval.py",
        "--split",
        str(args.split),
        "--partition",
        "train",
        "--agent",
        str(args.agent),
        "--restore",
        str(args.restore),
        "--case-root",
        str(args.case_root),
        "--output",
        str(args.output),
        "--base-url",
        args.base_url,
        "--model",
        args.model,
        "--model-artifact",
        args.model_artifact,
        "--model-artifact-sha256",
        args.model_artifact_sha256,
        "--structured-backend",
        "guidance",
        "--runs",
        str(args.runs),
    ]
    for case in cases:
        command.extend(("--case", case))
    if args.resume:
        command.append("--resume")
    return command


def score_command(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        "scripts/score_eval_runs.py",
        "--output",
        str(args.output),
        "--contract",
        str(args.contract),
        "--runs",
        str(args.runs),
        "--report",
        str(args.report),
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--restore", type=Path, required=True)
    parser.add_argument("--case-root", type=Path, default=Path("/data/eval-cases"))
    parser.add_argument("--model-artifact", required=True)
    parser.add_argument("--model-artifact-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--base-url", default="http://localhost:8002/v1")
    parser.add_argument("--model", default="rca-actor")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume only after the immutable evaluation manifest matches exactly",
    )
    parser.add_argument(
        "--split", type=Path, default=Path("configs/teacher/codex-blind-v1.yaml")
    )
    parser.add_argument(
        "--contract", type=Path, default=Path("configs/eval/train-monitor-v1.yaml")
    )
    args = parser.parse_args()
    contract = yaml.safe_load(args.contract.read_text(encoding="utf-8"))
    cases = list(contract["cases"])
    if not cases:
        parser.error("train monitor contract is empty")
    subprocess.run(monitor_command(args, cases), check=True)
    subprocess.run(score_command(args), check=True)


if __name__ == "__main__":
    main()
