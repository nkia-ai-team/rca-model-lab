from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    spec = importlib.util.spec_from_file_location(
        "compare_eval_reports", Path("scripts/compare_eval_reports.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def report() -> dict:
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
        "majority_strict_correct": 1,
        "mean_reward": 0.5,
        "mean_root_f1": 0.5,
        "strict_correct_runs": 1,
        "evidence_complete_runs": 3,
        "evaluation_provenance": provenance,
    }


def test_comparator_accepts_like_for_like_non_regression() -> None:
    module = _module()
    reference = report()
    candidate = report()
    candidate["mean_reward"] = 0.6

    passed, failures = module.compare(reference, candidate)

    assert passed
    assert not failures


def test_comparator_rejects_metric_regression_and_provenance_drift() -> None:
    module = _module()
    reference = report()
    candidate = report()
    candidate["mean_root_f1"] = 0.4
    candidate["majority_strict_correct"] = 0
    candidate["evidence_complete_runs"] = 2
    candidate["evaluation_provenance"]["agent_sha256"] = "changed"

    passed, failures = module.compare(reference, candidate)

    assert not passed
    assert "candidate regressed: root F1" in failures
    assert "candidate regressed: majority strict cases" in failures
    assert "candidate regressed: evidence coverage" in failures
    assert "evaluation provenance mismatch: agent_sha256" in failures
