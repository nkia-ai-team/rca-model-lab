"""Judge failures stay visible and never become cached verdicts."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("mcp_judge_runs", Path(__file__).resolve().parents[1] / "scripts/mcp_judge_runs.py")
judge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(judge)


@pytest.fixture
def run(tmp_path, monkeypatch):
    path = tmp_path / "case-1" / "run1"
    path.mkdir(parents=True)
    (path / "score.json").write_text(json.dumps({"case_id": "case-1", "root_f1": 1}))
    (path / "result.json").write_text(json.dumps({"summary": "cause"}))
    monkeypatch.setattr(judge, "reference_cause", lambda path: "reference")
    monkeypatch.setattr(judge, "reference_version", lambda case: "meta")
    return path


def test_failed_judge_preserves_diagnostic_without_valid_cache(run, monkeypatch):
    monkeypatch.setattr(judge, "judge_mechanism", lambda *a, **k: (None, "timeout after 180s"))
    assert judge.judge_run(run, "model", "v2", False) == "judge-failed"
    assert not (run / "mechanism_judge.json").exists()
    failure = json.loads((run / "mechanism_judge.failure.json").read_text())
    assert failure["detail"] == "timeout after 180s"
    assert failure["model"] == "model"


def test_quota_fails_fast_and_records_diagnostic(run, monkeypatch):
    calls = []
    def exhausted(*a, **k):
        calls.append(1)
        raise judge.SessionLimitError("You've hit your weekly limit · resets Oct 10, 3pm (UTC)")
    monkeypatch.setattr(judge, "judge_mechanism", exhausted)
    if hasattr(judge, "time"):
        monkeypatch.setattr(judge.time, "sleep", lambda seconds: None)
    assert judge.judge_run(run, "model", "v2", False) == "session-limit"
    assert len(calls) == 1
    assert "Oct 10" in json.loads((run / "mechanism_judge.failure.json").read_text())["detail"]
    assert not (run / "mechanism_judge.json").exists()


def test_success_clears_stale_failure(run, monkeypatch):
    failure = run / "mechanism_judge.failure.json"
    failure.write_text("{}")
    monkeypatch.setattr(judge, "judge_mechanism", lambda *a, **k: (1.0, "correct"))
    assert judge.judge_run(run, "model", "v2", False) == "judged"
    assert json.loads((run / "mechanism_judge.json").read_text())["score"] == 1
    assert not failure.exists()


def test_main_stops_scheduling_after_quota(run, monkeypatch, capsys):
    for index in range(2, 9):
        extra = run.parent / f"run{index}"
        extra.mkdir()
        (extra / "score.json").write_text((run / "score.json").read_text())
    calls = []
    def exhausted(*a):
        calls.append(1)
        return "session-limit"
    monkeypatch.setattr(judge, "judge_run", exhausted)
    monkeypatch.setattr("sys.argv", ["judge", str(run.parent.parent), "--workers", "1"])
    assert judge.main() == 1
    assert len(calls) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["session-limit"] == 1
    assert summary["not-started"] == 7


def test_main_returns_failure_for_incomplete_judging(run, monkeypatch):
    monkeypatch.setattr(judge, "judge_run", lambda *a: "judge-failed")
    monkeypatch.setattr("sys.argv", ["judge", str(run.parent.parent)])
    assert judge.main() == 1


def test_main_success(run, monkeypatch):
    monkeypatch.setattr(judge, "judge_run", lambda *a: "cached")
    monkeypatch.setattr("sys.argv", ["judge", str(run.parent.parent)])
    assert judge.main() == 0


def test_missing_result_is_incomplete(run, monkeypatch):
    (run / "result.json").unlink()
    monkeypatch.setattr(judge, "judge_mechanism", lambda *a, **k: pytest.fail("no result to judge"))
    assert judge.judge_run(run, "model", "v2", False) == "judge-failed"
    assert "result.json" in json.loads((run / "mechanism_judge.failure.json").read_text())["detail"]


def test_valid_cache_is_reused(run, monkeypatch):
    cache = {"score": 1, "model": "model", "rubric": "v2", "reference": "meta"}
    (run / "mechanism_judge.json").write_text(json.dumps(cache))
    monkeypatch.setattr(judge, "judge_mechanism", lambda *a, **k: pytest.fail("cached verdict"))
    assert judge.judge_run(run, "model", "v2", False) == "cached"


def test_concurrent_sweeps_only_judge_once(run, monkeypatch):
    import concurrent.futures
    import threading

    started = threading.Event()
    release = threading.Event()
    calls = []

    def evaluate(*args, **kwargs):
        calls.append(1)
        started.set()
        assert release.wait(5)
        return 1.0, "correct"

    monkeypatch.setattr(judge, "judge_mechanism", evaluate)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        first = pool.submit(judge.judge_run, run, "model", "v2", False)
        assert started.wait(5)
        second = pool.submit(judge.judge_run, run, "model", "v2", False)
        release.set()
        assert first.result() == "judged"
        assert second.result() == "cached"
    assert len(calls) == 1
