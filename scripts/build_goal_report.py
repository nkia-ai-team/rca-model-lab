#!/usr/bin/env python3
"""Assemble the fail-closed training-goal report from scored stage artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from rca_lab.eval.scoring import EvalContract
from rca_lab.scenarios.split import load_teacher_split

_FAMILY = re.compile(r"^case-(f[0-9]+)-", re.IGNORECASE)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _family(case_id: str) -> str:
    match = _FAMILY.match(case_id)
    return match.group(1).casefold() if match else case_id.casefold()


def build_report(
    *,
    split_path: Path,
    contract_path: Path,
    baseline_path: Path,
    sft_path: Path,
    rl_path: Path,
    runs: int,
) -> dict[str, Any]:
    split = load_teacher_split(split_path)
    contract = EvalContract.model_validate(yaml.safe_load(contract_path.read_text()))
    if set(contract.cases) != set(split.sealed_eval):
        raise ValueError("sealed scoring contract does not exactly match sealed split")
    overlap = sorted({_family(item) for item in split.train} & {_family(item) for item in split.sealed_eval})
    stages = {
        "baseline": json.loads(baseline_path.read_text()),
        "sft": json.loads(sft_path.read_text()),
        "rl": json.loads(rl_path.read_text()),
    }
    return {
        "contract": {"sealed_cases": len(contract.cases), "runs_per_case": runs},
        "schema": {
            "invalid_actions": sum(int(stage.get("format_errors", 0)) for stage in stages.values())
        },
        "split": {"family_overlap": overlap},
        "provenance": {
            "split_sha256": _digest(split_path),
            "contract_sha256": _digest(contract_path),
            "stage_report_sha256": {
                name: _digest(path)
                for name, path in {
                    "baseline": baseline_path,
                    "sft": sft_path,
                    "rl": rl_path,
                }.items()
            },
        },
        **stages,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", type=Path, default=Path("configs/teacher/codex-blind-v1.yaml"))
    parser.add_argument("--contract", type=Path, default=Path("configs/eval/sealed-family-v2.yaml"))
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--sft", type=Path, required=True)
    parser.add_argument("--rl", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--output", type=Path, default=Path("outputs/goal-eval/final-report.json")
    )
    args = parser.parse_args()
    report = build_report(
        split_path=args.split,
        contract_path=args.contract,
        baseline_path=args.baseline,
        sft_path=args.sft,
        rl_path=args.rl,
        runs=args.runs,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
