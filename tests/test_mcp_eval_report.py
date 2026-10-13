import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("mcp_eval_report", "scripts/mcp_eval_report.py")
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


def episode(root: Path, run: int, f1: float, verdict=None):
    path = root / "case-example" / f"run{run}"
    path.mkdir(parents=True)
    (path / "score.json").write_text(json.dumps({"case_id": "case-example", "root_f1": f1}))
    if verdict is not None:
        (path / "mechanism_judge.json").write_text(json.dumps(verdict))
    return path


def verdict(score):
    return {"model": "claude-opus-5-5", "rubric": "v2", "reference": "meta", "score": score}


def test_missing_judges_are_unknown_not_zero(tmp_path):
    episode(tmp_path, 1, 1)
    episode(tmp_path, 2, 0.5)
    result = reporter.report(tmp_path)
    assert result["hit"] == 1
    assert result["strict"] is None
    assert result["strict_rate"] is None
    assert result["pending_hits"] == 1
    assert result["strict_bounds"] == [0, 1]


def test_real_failures_and_completed_strict(tmp_path):
    for run, score in enumerate([1, 0.5, 0]):
        episode(tmp_path, run, 1, verdict(score))
    episode(tmp_path, 3, 0)
    result = reporter.report(tmp_path)
    assert result["total"] == 4
    assert result["judged_hits"] == 3
    assert result["strict"] == 1
    assert result["strict_rate"] == 0.25
    assert result["strict_bounds"] == [1, 1]


@pytest.mark.parametrize("field,value", [
    ("model", "different"), ("rubric", "v1"), ("reference", "stale"),
    ("score", None), ("score", True), ("score", 0.7),
])
def test_stale_or_invalid_verdict_is_unresolved(tmp_path, field, value):
    cache = verdict(1)
    cache[field] = value
    episode(tmp_path, 1, 1, cache)
    result = reporter.report(tmp_path)
    assert result["strict"] is None
    assert result["invalid_cache_hits"] == 1
    assert result["strict_bounds"] == [0, 1]


def test_malformed_cache_and_partial_bounds(tmp_path):
    episode(tmp_path, 1, 1, verdict(1))
    path = episode(tmp_path, 2, 1)
    (path / "mechanism_judge.json").write_text("{broken")
    episode(tmp_path, 3, 1)
    result = reporter.report(tmp_path)
    assert result["strict"] is None
    assert result["strict_bounds"] == [1, 3]
    assert result["pending_hits"] == result["invalid_cache_hits"] == 1


def test_empty_report_has_no_accuracy(tmp_path):
    result = reporter.report(tmp_path)
    assert result["total"] == 0
    assert result["hit_rate"] is result["strict"] is result["strict_bounds"] is None


def test_non_hits_need_no_judge_and_unscored_runs_are_excluded(tmp_path):
    episode(tmp_path, 1, 0)
    path = episode(tmp_path, 2, 1)
    (path / "score.json").write_text(json.dumps({"unscored": True, "root_f1": 1}))
    result = reporter.report(tmp_path)
    assert result["total"] == 1
    assert result["strict"] == 0
    assert result["unresolved_hits"] == 0
