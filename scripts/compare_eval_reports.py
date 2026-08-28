#!/usr/bin/env python3
"""Fail-closed non-regression comparator for like-for-like RCA evaluations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from rca_lab.eval.provenance import provenance_failures


def compare(reference: dict[str, Any], candidate: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
    failures: list[str] = []
    failures.extend(provenance_failures(reference, candidate))
    if reference.get("completed_runs") != candidate.get("completed_runs"):
        failures.append("completed run count mismatch")
    if int(candidate.get("format_errors", -1)) != 0:
        failures.append("candidate format errors")
    if int(candidate.get("unsupported_confirmations", -1)) != 0:
        failures.append("candidate unsupported confirmations")
    monotonic = (
        ("majority_strict_correct", int, "majority strict cases"),
        ("mean_reward", float, "mean reward"),
        ("mean_root_f1", float, "root F1"),
        ("strict_correct_runs", int, "strict runs"),
        ("evidence_complete_runs", int, "evidence coverage"),
    )
    for key, cast, label in monotonic:
        if key not in reference or key not in candidate:
            failures.append(f"missing comparison metric: {key}")
        elif cast(candidate[key]) < cast(reference[key]):
            failures.append(f"candidate regressed: {label}")
    return not failures, tuple(failures)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    try:
        reference = json.loads(args.reference.read_text(encoding="utf-8"))
        candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
        passed, failures = compare(reference, candidate)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"FAIL: invalid comparison input: {error}") from error
    if not passed:
        raise SystemExit("FAIL: " + "; ".join(failures))
    print("PASS: candidate is non-regressing under identical evaluation provenance")


if __name__ == "__main__":
    main()
