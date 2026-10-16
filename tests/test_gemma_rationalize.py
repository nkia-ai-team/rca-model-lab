"""Gemma writer: on-policy native thoughts accepted only when Gemma's own calls lead to the trajectory's."""
import json

from rca_lab.mcp import rationalize
from rca_lab.mcp.rationalize import (
    RationalizeConfig,
    gemma_turn,
    native_thought_accepted,
    rationalize_infra_failures,
)


def _call(name, **arguments):
    return {"id": f"c-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _messages():
    return [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "seed"},
        {"role": "assistant", "content": "", "reasoning_content": "teacher rationale", "tool_calls": [_call("describe_target", target="T2")]},
        {"role": "tool", "tool_call_id": "c-describe_target", "name": "describe_target", "content": "{}"},
        {"role": "assistant", "content": "", "reasoning_content": "teacher rationale 2", "gemma_rationale": {"match": "names"}, "tool_calls": [_call("sample_logs", target="T2", mode="map")]},
    ]


def _reply(*choices):
    return {"choices": [{"finish_reason": finish, "message": {"reasoning_content": text, "tool_calls": calls}} for text, calls, finish in choices]}


def _config():
    return RationalizeConfig(endpoint="http://gemma.invalid/v1", model="gemma", writer="gemma", max_tokens=8192, temperature=1.0)


def test_exact_call_accepts_gemma_thought_even_when_empty(monkeypatch):
    sent = {}

    def fake_post(config, body):
        sent.update(body)
        return _reply(("other plan", [_call("list_changes")], "tool_calls"), ("", [_call("sample_logs", mode="map", target="T2")], "tool_calls"))

    monkeypatch.setattr(rationalize, "_post_chat", fake_post)
    out, stats = _messages(), {}
    assert gemma_turn(_config(), out, 4, [], stats)
    assert out[4]["reasoning_content"] == "" and native_thought_accepted(out[4])
    assert stats == {"gemma_exact": 1}
    # clean inference context: no stored reasoning and no writer stamps reach the server; no hint, no grammar
    assert all("reasoning_content" not in m and "gemma_rationale" not in m for m in sent["messages"])
    assert sent["n"] == 4 and "structured_outputs" not in sent and "guided_json" not in sent
    assert all("call" not in str(m.get("content", "")).lower() for m in sent["messages"] if m["role"] == "user")
    assert rationalize_infra_failures(out, {}, judge_expected=False) == []


def test_same_tools_different_arguments_keeps_trajectory_call_for_judging(monkeypatch):
    monkeypatch.setattr(rationalize, "_post_chat", lambda config, body: _reply(("look at T3 logs", [_call("sample_logs", target="T3")], "tool_calls")))
    out, stats = _messages(), {}
    assert gemma_turn(_config(), out, 4, [], stats)
    assert out[4]["reasoning_content"] == "look at T3 logs"
    assert out[4]["gemma_rationale"]["match"] == "names" and not native_thought_accepted(out[4])
    assert json.loads(out[4]["tool_calls"][0]["function"]["arguments"]) == {"target": "T2", "mode": "map"}


def test_no_sample_leading_to_trajectory_tools_leaves_turn_unsupervised(monkeypatch):
    monkeypatch.setattr(rationalize, "_post_chat", lambda config, body: _reply(("loop", [], "length"), ("plan", [_call("describe_target", target="T2")], "tool_calls")))
    out, stats = _messages(), {}
    assert not gemma_turn(_config(), out, 4, [], stats)
    assert "reasoning_content" not in out[4] and out[4]["gemma_rationale"]["match"] == "none"
    assert rationalize_infra_failures(out, {}, judge_expected=False) == [4]


def test_length_truncated_sample_is_never_accepted(monkeypatch):
    monkeypatch.setattr(rationalize, "_post_chat", lambda config, body: _reply(("cut", [_call("sample_logs", mode="map", target="T2")], "length")))
    out = _messages()
    assert not gemma_turn(_config(), out, 4, [], {})
