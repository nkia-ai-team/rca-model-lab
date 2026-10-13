import json

import pytest

from rca_lab.mcp.codex_completion import parse_action


def test_action_preserves_grounded_rationale_and_arguments():
    body = {"tools": [{"function": {"name": "inspect"}}]}
    result = parse_action(json.dumps({"reasoning_content": "Check the observed target.",
                         "tool_calls": [{"name": "inspect", "arguments": {"id": "T1"}}]}), body)
    message = result["choices"][0]["message"]
    assert message["reasoning_content"] == "Check the observed target."
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"id": "T1"}


def test_unknown_tool_refused_before_execution():
    with pytest.raises(ValueError, match="unknown tool"):
        parse_action(json.dumps({"reasoning_content": "x", "tool_calls": [
            {"name": "shell", "arguments": {}}]}), {"tools": []})


DNS_ERROR = "exit 0, codex error: 'stream disconnected before completion: failed to lookup address information: Try again'"


def test_temporary_dns_retries_identical_request_before_returning_one_action(monkeypatch):
    from rca_lab.mcp import codex_completion as module
    body = {"model": "teacher", "messages": [{"role": "tool", "content": "observed"}],
            "tools": [{"function": {"name": "inspect"}}]}
    success = json.dumps({"reasoning_content": "Observed evidence", "tool_calls": [
        {"name": "inspect", "arguments": {"id": "T1"}}]})
    calls, sleeps = [], []
    def run(**kwargs):
        calls.append(kwargs)
        return (None, DNS_ERROR) if len(calls) < 3 else (success, "")
    monkeypatch.setattr(module, "run_codex", run)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    monkeypatch.setattr(module.time, "monotonic", lambda: 100)
    result = module.complete(body, timeout_seconds=30)
    assert len(calls) == 3 and calls[0] == calls[1] == calls[2]
    assert sleeps == [1, 2]
    assert len(result["choices"][0]["message"]["tool_calls"]) == 1


@pytest.mark.parametrize("diagnostic", ["exit 1, codex error: unauthorized", "timeout after 30s",
    "exit 0, unexpected codex tool event: failed to lookup address information: Try again"])
def test_non_dns_or_forbidden_tool_failure_is_not_retried(monkeypatch, diagnostic):
    from rca_lab.mcp import codex_completion as module
    calls = []
    def run(**kwargs):
        calls.append(kwargs)
        return None, diagnostic
    monkeypatch.setattr(module, "run_codex", run)
    with pytest.raises(RuntimeError):
        module.complete({"model": "teacher"}, timeout_seconds=30)
    assert len(calls) == 1


def test_dns_retry_is_bounded(monkeypatch):
    from rca_lab.mcp import codex_completion as module
    calls = []
    monkeypatch.setattr(module, "run_codex", lambda **kw: (calls.append(kw) or (None, DNS_ERROR)))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(module.time, "monotonic", lambda: 100)
    with pytest.raises(RuntimeError, match="Codex completion failed"):
        module.complete({"model": "teacher"}, timeout_seconds=30)
    assert len(calls) == 3


def test_dns_retry_does_not_extend_original_deadline(monkeypatch):
    from rca_lab.mcp import codex_completion as module
    times = iter([100, 100, 130])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times))
    calls = []
    monkeypatch.setattr(module, "run_codex", lambda **kw: (calls.append(kw) or (None, DNS_ERROR)))
    with pytest.raises(RuntimeError):
        module.complete({"model": "teacher"}, timeout_seconds=30)
    assert len(calls) == 1
