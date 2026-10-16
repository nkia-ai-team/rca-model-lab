import argparse
import importlib.util
import json
from pathlib import Path

import pytest

from rca_lab.mcp import judge, rationale_writer
from rca_lab.mcp import rationalize as rz

SPEC = importlib.util.spec_from_file_location("mcp_rationalize", Path(__file__).resolve().parents[1] / "scripts/mcp_rationalize.py")
mcp_rationalize = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mcp_rationalize)


def _call(name: str, arguments: dict) -> dict:
    return {"id": f"call-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _messages(reasoning: str = "old reasoning") -> list[dict]:
    return [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "reasoning_content": reasoning, "tool_calls": [_call("describe_target", {"target": "T1"})]},
        {"role": "tool", "tool_call_id": "call-describe_target", "name": "describe_target", "content": "o1"},
    ]


def _messages3(reasoning1: str = "old one", reasoning2: str = "old two", reasoning3: str = "old three") -> list[dict]:
    messages = _messages(reasoning1)
    messages.extend(
        [
            {"role": "assistant", "content": "", "reasoning_content": reasoning2, "tool_calls": [_call("sample_logs", {"target": "T1"})]},
            {"role": "tool", "tool_call_id": "call-sample_logs", "name": "sample_logs", "content": "o2"},
            {"role": "assistant", "content": "", "reasoning_content": reasoning3, "tool_calls": [_call("submit_rca", {"root_causes": []})]},
            {"role": "tool", "tool_call_id": "call-submit_rca", "name": "submit_rca", "content": "accepted"},
        ]
    )
    return messages


def _write_cached(run_dir: Path, stats: dict, reasoning: str = "old reasoning") -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "trajectory.rationalized.jsonl").write_text("".join(json.dumps(m) + "\n" for m in _messages(reasoning)), encoding="utf-8")
    (run_dir / "rationalize.json").write_text(json.dumps(stats), encoding="utf-8")


def _args(**overrides) -> argparse.Namespace:
    defaults = {
        "overwrite": False,
        "redo_failed_fraction": None,
        "redo_stale_view": False,
        "redo_provider": False,
        "rejudge": False,
        "judge_rounds": 0,
        "judge_provider": "codex",
        "judge_model": "gpt-6-astra",
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _config() -> rz.RationalizeConfig:
    return rz.RationalizeConfig(endpoint="unused", model="unused", writer="codex", writer_model="gpt-6-astra")


def test_codex_writer_provider_uses_codex_backend(monkeypatch):
    calls = []
    monkeypatch.setattr(rationale_writer, "run_claude", lambda **kwargs: pytest.fail("claude backend should not run"))

    def run_codex(**kwargs):
        calls.append(kwargs)
        return (json.dumps({"reasoning": "We have T1 context and need its details next. Let's describe T1 now."}), "")

    monkeypatch.setattr(rationale_writer, "run_codex", run_codex)
    text = rationale_writer.write_rationale(
        rationale_writer.WriterConfig(provider="codex", model="gpt-6-astra", retries=0),
        _messages()[:2],
        "describe_target",
        {"target": "T1"},
    )
    assert text == "We have T1 context and need its details next. Let's describe T1 now."
    assert calls[0]["model"] == "gpt-6-astra"


def test_codex_judge_provider_uses_codex_backend(monkeypatch):
    monkeypatch.setattr(judge, "run_claude", lambda **kwargs: pytest.fail("claude backend should not run"))
    monkeypatch.setattr(judge, "run_codex", lambda **kwargs: (json.dumps([{"id": 2, "consistent": True, "grounded": True, "issue": ""}]), ""))
    result = judge.claude_judge(judge.JudgeConfig(provider="codex", model="gpt-6-astra", retries=0))([{"id": 2}])
    assert result == [{"id": 2, "consistent": True, "grounded": True, "issue": ""}]


def test_redo_provider_regenerates_claude_cache_and_stamps_codex(tmp_path, monkeypatch):
    run_dir = tmp_path / "case-a" / "run1"
    _write_cached(
        run_dir,
        {
            "turns": 1,
            "failed": [],
            "writer_provider": "claude",
            "writer_model": "claude-sonnet-5",
            "judge_provider": "claude",
            "judge_model": "claude-sonnet-5",
            "judge": {"2": {"id": 2, "consistent": True, "grounded": True, "issue": ""}},
            "inconsistent": [],
        },
    )

    def rationalize_messages(config, messages, tools, *, judge, judge_rounds):
        assert config.writer == "codex"
        assert judge is not None
        return _messages("new codex reasoning"), {"turns": 1, "regenerated": 1, "failed": [], "seconds": 0.0, "judge": {"2": {"id": 2, "consistent": True, "grounded": True, "issue": ""}}, "inconsistent": []}

    monkeypatch.setattr(mcp_rationalize, "rationalize_messages", rationalize_messages)
    totals = {"turns": 0, "inconsistent": 0, "resampled": 0}
    mcp_rationalize._process([(run_dir, _messages(), "collected")], _args(redo_provider=True), _config(), [], lambda items: [], totals)

    messages = [json.loads(line) for line in (run_dir / "trajectory.rationalized.jsonl").read_text(encoding="utf-8").splitlines()]
    stats = json.loads((run_dir / "rationalize.json").read_text(encoding="utf-8"))
    assert messages[2]["reasoning_content"] == "new codex reasoning"
    assert stats["writer_provider"] == "codex"
    assert stats["writer_model"] == "gpt-6-astra"
    assert stats["judge_provider"] == "codex"
    assert stats["judge_model"] == "gpt-6-astra"


def test_redo_provider_rejudges_without_regenerating_when_only_judge_model_changed(tmp_path, monkeypatch):
    run_dir = tmp_path / "case-a" / "run1"
    _write_cached(
        run_dir,
        {
            "turns": 1,
            "failed": [],
            "writer_provider": "codex",
            "writer_model": "gpt-6-astra",
            "judge_provider": "codex",
            "judge_model": "gpt-6-astra",
            "judge": {"2": {"id": 2, "consistent": False, "grounded": True, "issue": "old judge"}},
            "inconsistent": [2],
        },
    )
    monkeypatch.setattr(rz, "regenerate_turn", lambda *args, **kwargs: pytest.fail("writer must not regenerate when only judge changed"))
    judged_batches = []

    def judge_sol(items):
        judged_batches.append([item["id"] for item in items])
        return [{"id": item["id"], "consistent": True, "grounded": True, "issue": ""} for item in items]

    totals = {"turns": 0, "inconsistent": 0, "resampled": 0}
    mcp_rationalize._process(
        [(run_dir, _messages(), "collected")],
        _args(redo_provider=True, judge_model="gpt-6.1-sol"),
        _config(),
        [],
        judge_sol,
        totals,
    )

    messages = [json.loads(line) for line in (run_dir / "trajectory.rationalized.jsonl").read_text(encoding="utf-8").splitlines()]
    stats = json.loads((run_dir / "rationalize.json").read_text(encoding="utf-8"))
    assert messages[2]["reasoning_content"] == "old reasoning"
    assert judged_batches == [[2]]
    assert stats["writer_provider"] == "codex"
    assert stats["writer_model"] == "gpt-6-astra"
    assert stats["judge_provider"] == "codex"
    assert stats["judge_model"] == "gpt-6.1-sol"
    assert stats["judge"]["2"] == {"id": 2, "consistent": True, "grounded": True, "issue": ""}
    assert stats["inconsistent"] == []
    assert stats["infra_failed"] == []


def test_interrupted_judge_model_recheck_discards_old_verdict_without_overwriting_writer(tmp_path):
    run_dir = tmp_path / "case-a" / "run1"
    _write_cached(
        run_dir,
        {
            "turns": 1,
            "failed": [],
            "writer_provider": "codex",
            "writer_model": "gpt-6-astra",
            "judge_provider": "codex",
            "judge_model": "gpt-6-astra",
            "judge": {"2": {"id": 2, "consistent": True, "grounded": True, "issue": "old"}},
            "inconsistent": [],
        },
    )

    def interrupted_judge(items):
        assert [item["id"] for item in items] == [2]
        raise rz.SessionLimitError("quota")

    totals = {"turns": 0, "inconsistent": 0, "resampled": 0}
    with pytest.raises(mcp_rationalize.SessionLimitError):
        mcp_rationalize._process(
            [(run_dir, _messages(), "collected")],
            _args(redo_provider=True, judge_model="gpt-6.1-sol"),
            _config(),
            [],
            interrupted_judge,
            totals,
        )

    stats = json.loads((run_dir / "rationalize.json").read_text(encoding="utf-8"))
    assert stats["writer_provider"] == "codex"
    assert stats["writer_model"] == "gpt-6-astra"
    assert stats["judge_provider"] == "codex"
    assert stats["judge_model"] == "gpt-6.1-sol"
    assert "judge" not in stats
    assert stats["infra_failed"] == [2]


def test_provider_mismatch_without_redo_provider_does_not_overwrite_old_provenance(tmp_path):
    run_dir = tmp_path / "case-a" / "run1"
    old_stats = {
        "turns": 1,
        "failed": [],
        "writer_provider": "claude",
        "writer_model": "claude-sonnet-5",
        "judge_provider": "claude",
        "judge_model": "claude-sonnet-5",
        "judge": {"2": {"id": 2, "consistent": True, "grounded": True, "issue": ""}},
        "inconsistent": [],
    }
    _write_cached(run_dir, old_stats)

    totals = {"turns": 0, "inconsistent": 0, "resampled": 0}
    mcp_rationalize._process([(run_dir, _messages(), "collected")], _args(redo_provider=False), _config(), [], None, totals)

    assert json.loads((run_dir / "rationalize.json").read_text(encoding="utf-8")) == old_stats


def test_interrupted_codex_generation_persists_progress_with_provenance(tmp_path, monkeypatch):
    run_dir = tmp_path / "case-a" / "run1"

    def interrupted(config, messages, tools, *, judge, judge_rounds):
        raise rz.RationalizeInterrupted(
            "quota",
            _messages("partial codex reasoning"),
            {"turns": 1, "regenerated": 1, "failed": [], "seconds": 0.0, "judge": {"2": {"id": 2, "consistent": True, "grounded": True, "issue": ""}}, "inconsistent": []},
        )

    monkeypatch.setattr(mcp_rationalize, "rationalize_messages", interrupted)
    totals = {"turns": 0, "inconsistent": 0, "resampled": 0}
    with pytest.raises(mcp_rationalize.SessionLimitError):
        mcp_rationalize._process([(run_dir, _messages(), "collected")], _args(redo_provider=True), _config(), [], lambda items: [], totals)

    messages = [json.loads(line) for line in (run_dir / "trajectory.rationalized.jsonl").read_text(encoding="utf-8").splitlines()]
    stats = json.loads((run_dir / "rationalize.json").read_text(encoding="utf-8"))
    assert messages[2]["reasoning_content"] == "partial codex reasoning"
    assert stats["writer_provider"] == "codex"
    assert stats["writer_model"] == "gpt-6-astra"
    assert stats["judge_provider"] == "codex"
    assert stats["judge_model"] == "gpt-6-astra"
    assert stats["infra_failed"] == []


def test_interrupted_redo_provider_does_not_stamp_untouched_claude_reasoning_as_codex(tmp_path, monkeypatch):
    run_dir = tmp_path / "case-a" / "run1"
    _write_cached(
        run_dir,
        {
            "turns": 3,
            "failed": [],
            "writer_provider": "claude",
            "writer_model": "claude-sonnet-5",
            "judge_provider": "claude",
            "judge_model": "claude-sonnet-5",
            "judge": {
                "2": {"id": 2, "consistent": True, "grounded": True, "issue": ""},
                "4": {"id": 4, "consistent": True, "grounded": True, "issue": ""},
                "6": {"id": 6, "consistent": True, "grounded": True, "issue": ""},
            },
            "inconsistent": [],
        },
    )

    first_calls = []

    def first_run(config, out, index, tools, stats, *, seed_offset=0):
        first_calls.append(index)
        if index == 2:
            out[index]["reasoning_content"] = "codex first"
            return True
        raise rz.SessionLimitError("quota")

    monkeypatch.setattr(rz, "regenerate_turn", first_run)
    totals = {"turns": 0, "inconsistent": 0, "resampled": 0}
    with pytest.raises(mcp_rationalize.SessionLimitError):
        mcp_rationalize._process([(run_dir, _messages3(), "collected")], _args(redo_provider=True), _config(), [], lambda items: [], totals)

    partial = [json.loads(line) for line in (run_dir / "trajectory.rationalized.jsonl").read_text(encoding="utf-8").splitlines()]
    partial_stats = json.loads((run_dir / "rationalize.json").read_text(encoding="utf-8"))
    assert first_calls == [2, 4]
    assert partial[2]["reasoning_content"] == "codex first"
    assert "reasoning_content" not in partial[4]
    assert "reasoning_content" not in partial[6]
    assert partial_stats["writer_provider"] == "codex"
    assert partial_stats["infra_failed"] == [2, 4, 6]

    retry_calls = []

    def retry(config, out, index, tools, stats, *, seed_offset=0):
        retry_calls.append(index)
        out[index]["reasoning_content"] = f"codex retry {index}"
        return True

    judged_batches = []

    def judge_ok(items):
        judged_batches.append([item["id"] for item in items])
        return [{"id": item["id"], "consistent": True, "grounded": True, "issue": ""} for item in items]

    monkeypatch.setattr(rz, "regenerate_turn", retry)
    totals = {"turns": 0, "inconsistent": 0, "resampled": 0}
    mcp_rationalize._process([(run_dir, _messages3(), "collected")], _args(redo_provider=True), _config(), [], judge_ok, totals)

    finished = [json.loads(line) for line in (run_dir / "trajectory.rationalized.jsonl").read_text(encoding="utf-8").splitlines()]
    finished_stats = json.loads((run_dir / "rationalize.json").read_text(encoding="utf-8"))
    assert retry_calls == [4, 6]
    assert judged_batches == [[2, 4, 6]]
    assert [finished[i]["reasoning_content"] for i in (2, 4, 6)] == ["codex first", "codex retry 4", "codex retry 6"]
    assert finished_stats["writer_provider"] == "codex"
    assert finished_stats["judge_provider"] == "codex"
    assert finished_stats["infra_failed"] == []
