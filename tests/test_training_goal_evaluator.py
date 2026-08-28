import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "evaluate_training_goal", Path("scripts/evaluate_training_goal.py")
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def report() -> dict:
    stage = {
        "completed_runs": 36,
        "format_errors": 0,
        "unsupported_confirmations": 0,
        "majority_strict_correct": 3,
        "strict_correct_runs": 9,
        "evidence_complete_runs": 30,
        "mean_reward": 0.5,
        "mean_root_f1": 0.5,
    }
    return {
        "contract": {"sealed_cases": 12, "runs_per_case": 3},
        "schema": {"invalid_actions": 0},
        "split": {"family_overlap": []},
        "baseline": dict(stage),
        "sft": dict(stage),
        "rl": dict(stage),
    }


def test_goal_evaluator_accepts_non_regressing_clean_loop() -> None:
    passed, failures = MODULE.evaluate(report())
    assert passed
    assert not failures


def test_goal_evaluator_rejects_leakage_and_rl_regression() -> None:
    candidate = report()
    candidate["split"]["family_overlap"] = ["f17"]
    candidate["rl"]["majority_strict_correct"] = 2
    candidate["rl"]["mean_reward"] = 0.4
    candidate["rl"]["mean_root_f1"] = 0.4
    candidate["rl"]["strict_correct_runs"] = 8
    candidate["rl"]["evidence_complete_runs"] = 29

    passed, failures = MODULE.evaluate(candidate)

    assert not passed
    assert "family leakage" in failures
    assert "RL regressed below SFT" in failures
    assert "RL mean reward regressed below SFT" in failures
    assert "RL root F1 regressed below SFT" in failures
    assert "RL strict runs regressed below SFT" in failures
    assert "RL evidence coverage regressed below SFT" in failures


def test_goal_evaluator_fails_closed_when_evidence_metrics_are_missing() -> None:
    candidate = report()
    del candidate["rl"]["evidence_complete_runs"]

    passed, failures = MODULE.evaluate(candidate)

    assert not passed
    assert "rl: missing evidence coverage" in failures
