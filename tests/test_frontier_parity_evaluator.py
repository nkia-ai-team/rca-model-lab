from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    spec = importlib.util.spec_from_file_location(
        "evaluate_frontier_parity", Path("scripts/evaluate_frontier_parity.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def report(*, reward: float = 0.5, strict: int = 3, majority: int = 1) -> dict:
    provenance = {
        "agent_sha256": "a",
        "restore_sha256": "b",
        "split_sha256": "c",
        "case_set_sha256": "d",
        "scoring_contract_sha256": "e",
        "structured_output_backend": "guidance",
        "actor_temperature": 0.0,
        "actor_seed": 0,
        "partition": "sealed_eval",
        "runs": 3,
        "cases": ["case-a"],
    }
    return {
        "completed_runs": 3,
        "format_errors": 0,
        "unsupported_confirmations": 0,
        "majority_strict_correct": majority,
        "strict_correct_runs": strict,
        "evidence_complete_runs": 3,
        "mean_reward": reward,
        "mean_root_f1": reward,
        "evaluation_provenance": provenance,
    }


def test_parity_requires_student_to_match_both_teachers() -> None:
    module = _module()
    student = report(reward=0.6, strict=3, majority=1)

    passed, failures = module.evaluate(
        student,
        report(reward=0.5, strict=2, majority=1),
        report(reward=0.6, strict=3, majority=1),
    )

    assert passed
    assert not failures


def test_parity_reports_each_teacher_regression_and_provenance_drift() -> None:
    module = _module()
    student = report(reward=0.5, strict=2, majority=0)
    codex = report(reward=0.7, strict=3, majority=1)
    student["evaluation_provenance"]["agent_sha256"] = "different"

    passed, failures = module.evaluate(student, report(), codex)

    assert not passed
    assert "below claude: evaluation provenance mismatch: agent_sha256" in failures
    assert "below codex: candidate regressed: majority strict cases" in failures
    assert "below codex: candidate regressed: mean reward" in failures
