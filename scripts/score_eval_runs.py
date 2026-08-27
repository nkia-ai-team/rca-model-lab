#!/usr/bin/env python3
"""Deterministically score sealed runs from typed episode artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from rca_lab.eval.scoring import EvalContract
from rca_lab.eval.scoring import score_directory as score


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--contract", type=Path, default=Path("configs/eval/sealed-family-v2.yaml")
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    contract = EvalContract.model_validate(
        yaml.safe_load(args.contract.read_text(encoding="utf-8"))
    ).model_dump(mode="json", exclude_none=True)
    report = score(args.output, contract, args.runs)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "runs"}))


if __name__ == "__main__":
    main()
