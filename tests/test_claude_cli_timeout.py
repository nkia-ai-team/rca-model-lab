"""A hung claude CLI call must come back as a failed call, not an exception (2026-10-06 rationalize shard crash)."""
import subprocess

from rca_lab.mcp import claude_cli


def test_timeout_is_a_failed_call(monkeypatch):
    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr(claude_cli.subprocess, "run", hang)
    text, diagnostic = claude_cli.run_claude(system_prompt="s", prompt="p", model="m", timeout_seconds=5)
    assert text is None
    assert "timeout" in diagnostic


import json

import pytest


def cli_reply(monkeypatch, envelope):
    monkeypatch.setattr(
        claude_cli.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, json.dumps(envelope), ""),
    )
    return claude_cli.run_claude(system_prompt="s", prompt="p", model="m", timeout_seconds=5)


@pytest.mark.parametrize("notice", [
    "You've hit your weekly limit · resets Oct 10, 3pm (UTC)",
    "You've hit your session limit · resets 5:20am (UTC)",
    "You've hit your usage limit · resets 5:20am (UTC)",
    "Rate limit exceeded",
])
def test_quota_notice_raises(monkeypatch, notice):
    with pytest.raises(claude_cli.SessionLimitError, match="limit"):
        cli_reply(monkeypatch, {"result": notice, "is_error": True})


def test_legacy_notice_without_error_metadata(monkeypatch):
    with pytest.raises(claude_cli.SessionLimitError):
        cli_reply(monkeypatch, {"result": "You've hit your session limit · resets 5:20am (UTC)"})


@pytest.mark.parametrize("result", [
    '{"score": 1, "reason": "Rate limit exceeded caused the incident"}',
    "The service resets 5:20 and recovers.",
    "You've hit your session limit · resets 5:20am (UTC)",
])
def test_success_metadata_prevents_quota_false_positive(monkeypatch, result):
    assert cli_reply(monkeypatch, {"result": result, "is_error": False}) == (result, "")


def test_legacy_explanation_is_not_a_quota_notice(monkeypatch):
    result = "The service reports rate limit exceeded when overloaded."
    assert cli_reply(monkeypatch, {"result": result}) == (result, "")


def test_error_envelope_cannot_be_valid_judge_text(monkeypatch):
    text, diagnostic = cli_reply(monkeypatch, {"result": "Backend unavailable", "is_error": True})
    assert text is None
    assert "Backend unavailable" in diagnostic
