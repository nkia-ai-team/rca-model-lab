#!/usr/bin/env python3
"""Fail-closed Student parity gate against both frontier teachers."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any


def _load_comparator():
    path = Path(__file__).with_name("compare_eval_reports.py")
    spec = importlib.util.spec_from_file_location("compare_eval_reports", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load evaluation comparator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.compare


def evaluate(
    student: dict[str, Any],
    claude: dict[str, Any],
    codex: dict[str, Any],
) -> tuple[bool, tuple[str, ...]]:
    compare = _load_comparator()
    failures: list[str] = []
    for teacher_name, teacher in (("claude", claude), ("codex", codex)):
        passed, comparison_failures = compare(teacher, student)
        if not passed:
            failures.extend(f"below {teacher_name}: {failure}" for failure in comparison_failures)
    return not failures, tuple(failures)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"report must be an object: {path}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--student", type=Path, required=True)
    parser.add_argument("--claude", type=Path, required=True)
    parser.add_argument("--codex", type=Path, required=True)
    args = parser.parse_args()
    try:
        passed, failures = evaluate(
            _read(args.student),
            _read(args.claude),
            _read(args.codex),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"FAIL: invalid parity input: {error}") from error
    if not passed:
        raise SystemExit("FAIL: " + "; ".join(failures))
    print("PASS: Student is non-regressing against both Claude and Codex")


if __name__ == "__main__":
    main()
