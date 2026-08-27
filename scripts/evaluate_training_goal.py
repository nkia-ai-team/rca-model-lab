#!/usr/bin/env python3
"""Fail-closed evaluator for the RCA SFT -> RL performance loop."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _require(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def _stage(report: dict[str, Any], name: str) -> dict[str, Any]:
    value = report.get(name)
    if not isinstance(value, dict):
        raise TypeError(f"missing stage: {name}")
    return value


def evaluate(report: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
    failures: list[str] = []
    contract = report.get("contract", {})
    baseline = _stage(report, "baseline")
    sft = _stage(report, "sft")
    rl = _stage(report, "rl")

    sealed_cases = int(contract.get("sealed_cases", 0))
    runs_per_case = int(contract.get("runs_per_case", 0))
    expected_runs = sealed_cases * runs_per_case
    _require(sealed_cases > 0, "sealed_cases must be positive", failures)
    _require(runs_per_case >= 3, "runs_per_case must be at least 3", failures)
    _require(report.get("schema", {}).get("invalid_actions") == 0, "invalid actions", failures)
    _require(not report.get("split", {}).get("family_overlap"), "family leakage", failures)

    for name, stage in (("baseline", baseline), ("sft", sft), ("rl", rl)):
        _require(stage.get("completed_runs") == expected_runs, f"{name}: incomplete runs", failures)
        _require(stage.get("format_errors") == 0, f"{name}: format errors", failures)
        _require(
            stage.get("unsupported_confirmations") == 0,
            f"{name}: unsupported confirmations",
            failures,
        )

    _require(
        int(sft.get("majority_strict_correct", -1))
        >= int(baseline.get("majority_strict_correct", 0)),
        "SFT regressed below baseline",
        failures,
    )
    _require(
        int(rl.get("majority_strict_correct", -1))
        >= int(sft.get("majority_strict_correct", 0)),
        "RL regressed below SFT",
        failures,
    )
    _require(
        float(sft.get("mean_reward", -1)) >= float(baseline.get("mean_reward", 0)),
        "SFT mean reward regressed below baseline",
        failures,
    )
    _require(
        float(rl.get("mean_reward", -1)) >= float(sft.get("mean_reward", 0)),
        "RL mean reward regressed below SFT",
        failures,
    )
    return not failures, tuple(failures)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report", type=Path, default=Path("outputs/goal-eval/final-report.json")
    )
    args = parser.parse_args()
    if not args.report.is_file():
        raise SystemExit(f"FAIL: missing report: {args.report}")
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        passed, failures = evaluate(report)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"FAIL: invalid report: {error}") from error
    if not passed:
        raise SystemExit("FAIL: " + "; ".join(failures))
    print("PASS: schema/split/baseline/SFT/RL gates satisfied")


if __name__ == "__main__":
    main()
