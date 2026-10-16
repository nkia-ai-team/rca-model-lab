import importlib.util
import json
from pathlib import Path

import pytest

from rca_lab.mcp import codex_cli, rl_reward


def script(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_codex_judge_is_explicit_and_rejects_boolean_score(monkeypatch):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return '{"score": true}', ""

    monkeypatch.setattr(codex_cli, "run_codex", run)
    value, diagnostic = rl_reward.judge_mechanism("reference", "candidate", provider="codex", model="configured")
    assert value is None and "bad score" in diagnostic
    assert calls[0]["model"] == "configured"
    assert calls[0]["system_prompt"] == rl_reward.JUDGE_SYSTEM_V2
    with pytest.raises(ValueError, match="unsupported"):
        rl_reward.judge_mechanism("reference", "candidate", provider="guess", model="configured")


def test_codex_cache_preserves_claude_and_reports_separately(tmp_path, monkeypatch):
    judge = script("mcp_judge_runs")
    reporter = script("mcp_eval_report")
    run = tmp_path / "case-example" / "run1"
    run.mkdir(parents=True)
    (run / "score.json").write_text(json.dumps({"case_id": "case-example", "root_f1": 1}))
    (run / "result.json").write_text('{"summary":"cause"}')
    old = {"score": 0.5, "model": "configured", "rubric": "v2", "reference": "meta"}
    (run / "mechanism_judge.json").write_text(json.dumps(old))
    monkeypatch.setattr(judge, "reference_cause", lambda _: "reference")
    monkeypatch.setattr(judge, "judge_mechanism", lambda *a, **kw: (1, "matched"))
    assert reporter.report(tmp_path, provider="codex", model="configured")["strict"] is None
    assert judge.judge_run(run, "configured", "v2", False, "codex") == "judged"
    assert json.loads((run / "mechanism_judge.json").read_text()) == old
    assert json.loads((run / "mechanism_judge.codex.configured.json").read_text())["provider"] == "codex"
    assert reporter.report(tmp_path, provider="codex", model="configured")["strict"] == 1
    assert reporter.report(tmp_path, provider="claude", model="configured")["strict"] == 0
    assert not rl_reward.judge_cache_valid(old, "case-example", provider="codex", model="configured", rubric="v2")


def test_judge_model_switch_keeps_distinct_files(tmp_path):
    astra = rl_reward.judge_cache_path(tmp_path, "codex", model="gpt-6-astra")
    sol = rl_reward.judge_cache_path(tmp_path, "codex", model="gpt-6.1-sol")
    assert astra != sol
    assert sol.name == "mechanism_judge.codex.gpt-6.1-sol.json"
    assert rl_reward.judge_cache_path(tmp_path, "codex", model="../../external").parent == tmp_path


def test_codex_quota_returns_resumable_status(tmp_path, monkeypatch):
    judge = script("mcp_judge_runs")
    run = tmp_path / "case-example" / "run1"
    run.mkdir(parents=True)
    (run / "score.json").write_text("{}")
    monkeypatch.setattr(judge, "judge_run", lambda *a: "session-limit")
    monkeypatch.setattr("sys.argv", ["judge", str(tmp_path), "--provider", "codex", "--model", "configured"])
    assert judge.main() == 3
