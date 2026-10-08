import json

import pytest

from rca_lab.mcp import judge


@pytest.mark.parametrize("verdict", [
    {"id": 2, "consistent": "false", "grounded": True},
    {"id": 2, "consistent": True},
    {"id": 2, "consistent": True, "grounded": 1},
])
def test_malformed_judge_fields_never_become_success(monkeypatch, verdict):
    monkeypatch.setattr(judge, "run_claude", lambda **kwargs: (json.dumps([verdict]), ""))
    result = judge.claude_judge(judge.JudgeConfig(retries=0))([{"id": 2}])
    assert result[0].get("error")
    assert judge.flagged(result) == [2]


def test_explicit_negative_is_content_verdict_not_transport_error(monkeypatch):
    verdict = {"id": 2, "consistent": False, "grounded": True, "issue": "wrong target"}
    monkeypatch.setattr(judge, "run_claude", lambda **kwargs: (json.dumps([verdict]), ""))
    result = judge.claude_judge(judge.JudgeConfig(retries=0))([{"id": 2}])
    assert result == [verdict]
    assert judge.flagged(result) == [2]
