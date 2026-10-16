"""Cached rationalization recovery stays resumable and does not overwrite good turns."""

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

from rca_lab.mcp import rationalize as rz

SPEC = importlib.util.spec_from_file_location("mcp_rationalize", Path(__file__).resolve().parents[1] / "scripts/mcp_rationalize.py")
mcp_rationalize = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mcp_rationalize)


def _call(name: str, arguments: dict) -> dict:
    return {"id": f"call-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _messages(reasoning1: str = "good reasoning", reasoning2: str = "needs repair") -> list[dict]:
    return [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "reasoning_content": reasoning1, "tool_calls": [_call("describe_target", {"target": "T1"})]},
        {"role": "tool", "tool_call_id": "call-describe_target", "name": "describe_target", "content": "o1"},
        {"role": "assistant", "content": "", "reasoning_content": reasoning2, "tool_calls": [_call("sample_logs", {"target": "T1"})]},
        {"role": "tool", "tool_call_id": "call-sample_logs", "name": "sample_logs", "content": "o2"},
    ]


def _messages3(
    reasoning1: str = "good reasoning",
    reasoning2: str = "needs repair",
    reasoning3: str = "also needs repair",
) -> list[dict]:
    messages = _messages(reasoning1, reasoning2)
    messages.extend(
        [
            {"role": "assistant", "content": "", "reasoning_content": reasoning3, "tool_calls": [_call("submit_rca", {"root_causes": []})]},
            {"role": "tool", "tool_call_id": "call-submit_rca", "name": "submit_rca", "content": "accepted"},
        ]
    )
    return messages


def _write_run(run_dir: Path, messages: list[dict], stats: dict) -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "trajectory.rationalized.jsonl").write_text("".join(json.dumps(m) + "\n" for m in messages), encoding="utf-8")
    (run_dir / "rationalize.json").write_text(json.dumps(stats), encoding="utf-8")


def _args(**overrides) -> argparse.Namespace:
    defaults = {
        "overwrite": False,
        "redo_failed_fraction": None,
        "redo_stale_view": False,
        "rejudge": False,
        "judge_rounds": 0,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _config() -> rz.RationalizeConfig:
    return rz.RationalizeConfig(endpoint="unused", model="unused")


def test_cached_quota_reasoning_is_regenerated_before_judge_and_good_turn_is_preserved(tmp_path, monkeypatch):
    quota = "You've hit your weekly limit · resets Oct 10, 3pm (UTC)"
    run_dir = tmp_path / "case-a" / "run1"
    _write_run(
        run_dir,
        _messages(reasoning1="already verified", reasoning2=quota),
        {
            "turns": 2,
            "regenerated": 2,
            "failed": [],
            "judge": {
                "2": {"id": 2, "consistent": True, "grounded": True, "issue": ""},
                "4": {"id": 4, "consistent": False, "grounded": False, "issue": "quota notice"},
            },
            "inconsistent": [4],
        },
    )

    def regenerate(config, out, index, tools, stats, *, seed_offset=0):
        assert index == 4
        out[index]["reasoning_content"] = "fresh recovered reasoning"
        return True

    def judge(items):
        assert [item["id"] for item in items] == [4]
        return [{"id": 4, "consistent": True, "grounded": True, "issue": ""}]

    monkeypatch.setattr(rz, "regenerate_turn", regenerate)
    totals = {"turns": 0, "inconsistent": 0, "resampled": 0}
    mcp_rationalize._process([(run_dir, _messages(), "collected")], _args(), _config(), [], judge, totals)

    repaired = [json.loads(line) for line in (run_dir / "trajectory.rationalized.jsonl").read_text(encoding="utf-8").splitlines()]
    stats = json.loads((run_dir / "rationalize.json").read_text(encoding="utf-8"))
    assert repaired[2]["reasoning_content"] == "already verified"
    assert repaired[4]["reasoning_content"] == "fresh recovered reasoning"
    assert stats["judge"]["2"]["consistent"] is True
    assert stats["judge"]["4"]["consistent"] is True
    assert stats["inconsistent"] == []
    assert stats["infra_failed"] == []
    assert totals["infra_failed"] == 0


def test_cached_writer_failure_retries_only_missing_reasoning(tmp_path, monkeypatch):
    run_dir = tmp_path / "case-a" / "run1"
    messages = _messages(reasoning1="keep me", reasoning2="")
    _write_run(run_dir, messages, {"turns": 2, "regenerated": 1, "failed": [4]})

    def regenerate(config, out, index, tools, stats, *, seed_offset=0):
        assert index == 4
        out[index]["reasoning_content"] = "writer recovered"
        return True

    monkeypatch.setattr(rz, "regenerate_turn", regenerate)
    totals = {"turns": 0, "inconsistent": 0, "resampled": 0}
    mcp_rationalize._process([(run_dir, _messages(), "collected")], _args(), _config(), [], None, totals)

    repaired = [json.loads(line) for line in (run_dir / "trajectory.rationalized.jsonl").read_text(encoding="utf-8").splitlines()]
    stats = json.loads((run_dir / "rationalize.json").read_text(encoding="utf-8"))
    assert repaired[2]["reasoning_content"] == "keep me"
    assert repaired[4]["reasoning_content"] == "writer recovered"
    assert stats["failed"] == []
    assert stats["infra_failed"] == []


def test_content_rejection_is_not_reported_as_infrastructure_failure(tmp_path, monkeypatch):
    run_dir = tmp_path / "case-a" / "run1"
    _write_run(
        run_dir,
        _messages(reasoning1="bad plan", reasoning2="good plan"),
        {
            "turns": 2,
            "failed": [],
            "judge": {
                "2": {"id": 2, "consistent": False, "grounded": True, "issue": "wrong target"},
                "4": {"id": 4, "consistent": True, "grounded": True, "issue": ""},
            },
            "inconsistent": [2],
        },
    )

    monkeypatch.setattr(rz, "regenerate_turn", lambda *a, **k: pytest.fail("content rejection is not an infra retry"))
    totals = {"turns": 0, "inconsistent": 0, "resampled": 0}
    mcp_rationalize._process([(run_dir, _messages(), "collected")], _args(), _config(), [], lambda items: pytest.fail("valid cached verdicts"), totals)
    assert totals["infra_failed"] == 0
    assert totals["inconsistent"] == 1


def test_judge_error_cache_is_retried_and_keeps_nonzero_infra_signal(tmp_path):
    run_dir = tmp_path / "case-a" / "run1"
    _write_run(
        run_dir,
        _messages(),
        {
            "turns": 2,
            "failed": [],
            "judge": {
                "2": {"id": 2, "error": "judge failed: timeout"},
                "4": {"id": 4, "consistent": True, "grounded": True, "issue": ""},
            },
            "inconsistent": [2],
        },
    )

    def still_fails(items):
        assert [item["id"] for item in items] == [2]
        return [{"id": 2, "error": "judge failed: backend unavailable"}]

    totals = {"turns": 0, "inconsistent": 0, "resampled": 0}
    mcp_rationalize._process([(run_dir, _messages(), "collected")], _args(), _config(), [], still_fails, totals)
    stats = json.loads((run_dir / "rationalize.json").read_text(encoding="utf-8"))
    assert stats["infra_failed"] == [2]
    assert totals["infra_failed"] == 1


def test_infrastructure_pending_reuses_exact_cache_completeness_contract():
    stats = {
        "failed": [4],
        "judge": {
            "2": {"id": 2, "error": "judge failed: timeout"},
        },
        "inconsistent": [2, 4],
    }
    assert rz.infrastructure_pending(_messages(), stats) == [2, 4]


def test_malformed_judge_verdict_fields_are_infrastructure_pending():
    assert rz.infrastructure_pending(
        _messages(),
        {
            "failed": [],
            "judge": {
                "2": {"id": 2, "consistent": "true", "grounded": True},
                "4": {"id": 4, "consistent": True},
            },
        },
    ) == [2, 4]


def test_quota_during_second_repair_persists_first_and_retry_judges_only_pending(tmp_path, monkeypatch):
    quota = "You've hit your weekly limit · resets Oct 10, 3pm (UTC)"
    run_dir = tmp_path / "case-a" / "run1"
    _write_run(
        run_dir,
        _messages3(reasoning1="verified good", reasoning2=quota, reasoning3=quota),
        {
            "turns": 3,
            "regenerated": 3,
            "failed": [],
            "judge": {
                "2": {"id": 2, "consistent": True, "grounded": True, "issue": ""},
                "4": {"id": 4, "consistent": True, "grounded": True, "issue": "stale positive"},
                "6": {"id": 6, "consistent": True, "grounded": True, "issue": "stale positive"},
            },
            "inconsistent": [],
        },
    )
    first_calls = []

    def interrupted_regenerate(config, out, index, tools, stats, *, seed_offset=0):
        first_calls.append(index)
        if index == 4:
            out[index]["reasoning_content"] = "first repair survived"
            return True
        raise rz.SessionLimitError("quota")

    monkeypatch.setattr(rz, "regenerate_turn", interrupted_regenerate)
    totals = {"turns": 0, "inconsistent": 0, "resampled": 0}
    with pytest.raises(mcp_rationalize.SessionLimitError):
        mcp_rationalize._process([(run_dir, _messages3(), "collected")], _args(), _config(), [], lambda items: [], totals)

    partial = [json.loads(line) for line in (run_dir / "trajectory.rationalized.jsonl").read_text(encoding="utf-8").splitlines()]
    partial_stats = json.loads((run_dir / "rationalize.json").read_text(encoding="utf-8"))
    assert first_calls == [4, 6]
    assert partial[4]["reasoning_content"] == "first repair survived"
    assert partial_stats["judge"] == {"2": {"id": 2, "consistent": True, "grounded": True, "issue": ""}}
    assert partial_stats["infra_failed"] == [4, 6]

    retry_calls = []

    def retry_regenerate(config, out, index, tools, stats, *, seed_offset=0):
        retry_calls.append(index)
        out[index]["reasoning_content"] = "second repair survived"
        return True

    judged_batches = []

    def judge(items):
        judged_batches.append([item["id"] for item in items])
        return [{"id": item["id"], "consistent": True, "grounded": True, "issue": ""} for item in items]

    monkeypatch.setattr(rz, "regenerate_turn", retry_regenerate)
    totals = {"turns": 0, "inconsistent": 0, "resampled": 0}
    mcp_rationalize._process([(run_dir, _messages3(), "collected")], _args(), _config(), [], judge, totals)

    finished = [json.loads(line) for line in (run_dir / "trajectory.rationalized.jsonl").read_text(encoding="utf-8").splitlines()]
    finished_stats = json.loads((run_dir / "rationalize.json").read_text(encoding="utf-8"))
    assert retry_calls == [6]
    assert judged_batches == [[4, 6]]
    assert finished[2]["reasoning_content"] == "verified good"
    assert finished[4]["reasoning_content"] == "first repair survived"
    assert finished[6]["reasoning_content"] == "second repair survived"
    assert finished_stats["infra_failed"] == []


def test_main_returns_nonzero_when_infrastructure_failures_remain(tmp_path, monkeypatch):
    catalog = tmp_path / "tools.json"
    catalog.write_text("[]", encoding="utf-8")

    def incomplete(jobs, args, config, tools, judge, totals):
        totals["infra_failed"] = 1

    monkeypatch.setattr(mcp_rationalize, "_process", incomplete)
    monkeypatch.setattr("sys.argv", ["mcp_rationalize", "--catalog", str(catalog)])
    assert mcp_rationalize.main() == 1
