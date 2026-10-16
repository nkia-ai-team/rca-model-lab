"""Rejected calls must not poison the next strict provider history parse."""

import copy
import json

import pytest

from rca_lab.mcp import student
from rca_lab.mcp.context import rolling_view


def call(arguments, identifier="bad", name="probe"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


@pytest.mark.parametrize("raw", [
    '{"query":"한글에서 "인용"이 깨짐"}',
    '{"query":"경로\\잘못된 이스케이프"}',
    '{"query":"부분 생성 한국어 🔎',
    '{"query":"끝난 역슬래시' + "\\",
])
def test_invalid_json_history_preserves_full_raw_and_error_pair(raw):
    good = call('{ "query": "정상 🔎" }', "good")
    messages = [
        {"role": "assistant", "reasoning_content": "stored reasoning", "tool_calls": [call(raw), good]},
        {"role": "tool", "tool_call_id": "bad", "name": "probe", "content": '{"error":"arguments are not valid JSON"}'},
        {"role": "tool", "tool_call_id": "good", "name": "probe", "content": '{"rows":["evidence"]}'},
    ]
    original = copy.deepcopy(messages)
    view = rolling_view(messages)
    rejected = view[0]["tool_calls"][0]
    assert json.loads(rejected["function"]["arguments"]) == {
        "_harness_invalid_arguments": {"raw": raw, "error": "invalid JSON; not executed"}
    }
    assert rejected["id"] == "bad" and rejected["function"]["name"] == "probe"
    assert view[0]["tool_calls"][1] is good
    assert view[1:] == messages[1:]
    assert "reasoning_content" not in view[0]
    assert messages == original
    assert rolling_view(messages) == view
    assert rolling_view(view) == view


@pytest.mark.parametrize("raw", ['[]', 'null', '1', 'true', '"한국어"', [], None, 1, True])
def test_non_object_arguments_get_explicit_rejected_history_envelope(raw):
    messages = [{"role": "assistant", "tool_calls": [call(raw)]}]
    original = copy.deepcopy(messages)
    argument = rolling_view(messages)[0]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(argument) == {
        "_harness_invalid_arguments": {"raw": raw, "error": "arguments must be an object; not executed"}
    }
    assert messages == original


@pytest.mark.parametrize("raw", [' { "query": "정상" } ', {"query": "정상"}, '{}'])
def test_valid_arguments_keep_original_containers_and_formatting(raw):
    calls = [call(raw)]
    messages = [{"role": "assistant", "tool_calls": calls}]
    assert rolling_view(messages)[0]["tool_calls"] is calls


def test_student_recovers_truncated_call_without_executing_it(monkeypatch):
    raw = '{"query":"긴 쿼리 🔎' + "\\"
    bodies = []

    class MCP:
        calls = []

        def list_tools(self):
            return [{"name": "probe", "inputSchema": {"type": "object", "properties": {}}}]

        def call(self, name, args):
            self.calls.append((name, args))
            raise AssertionError("invalid call must never reach MCP")

    def strict_post(config, body):
        # llama.cpp's template renders history arguments as parsed JSON objects.
        for message in body["messages"]:
            for prior_call in message.get("tool_calls", []):
                assert isinstance(json.loads(prior_call["function"]["arguments"]), dict)
        bodies.append(body)
        if len(bodies) == 1:
            response_call = call(raw)
            finish = "length"
        else:
            assert "arguments are not valid JSON" in body["messages"][-1]["content"]
            response_call = call(json.dumps({"status": "insufficient", "summary": "근거 부족", "causes": []}), "submit", "submit_rca")
            finish = "tool_calls"
        return {"choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": "", "tool_calls": [response_call]}}], "usage": {"prompt_tokens": 100}}

    monkeypatch.setattr(student, "_post_chat", strict_post)
    mcp = MCP()
    config = student.StudentConfig(endpoint="http://unused", model="test", guided=False, max_turns=3, max_prompt_tokens=0)
    run = student.run_student(config, {"affected_targets": [], "alarms": []}, mcp)
    assert run.stop_reason == "submitted"
    assert run.tool_errors == 1 and run.output_limit_hits == 1
    assert run.tool_calls == 0 and mcp.calls == []
    assert len(bodies) == 2
    assert run.messages[2]["tool_calls"][0]["function"]["arguments"] == raw
    assert "_harness_invalid_arguments" not in json.dumps(run.messages, ensure_ascii=False)
