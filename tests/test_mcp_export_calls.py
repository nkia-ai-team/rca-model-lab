from __future__ import annotations

import json
from pathlib import Path

from rca_lab.mcp.answer import SUBMIT_TOOL_NAME
from rca_lab.mcp.export import apply_call_supervision, build_messages, export_teacher_run

UUID_A = "414db206-d121-43b1-886c-78b240d23a7a"


def _seed() -> dict:
    return {
        "time_window": {"first_event": "2026-01-01T00:10:00Z", "last_event": "2026-01-01T00:20:00Z"},
        "alarms": [],
        "affected_targets": [UUID_A],
    }


def _submit_args() -> dict:
    return {
        "status": "provisional",
        "summary": "s",
        "causes": [{"target": UUID_A, "mechanism": "m", "support_refs": [f"pg:targets:{UUID_A}"]}],
        "external_causes": [],
    }


def _call(call_id: str, name: str, arguments: dict | str = "{}") -> dict:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": raw}}


def test_build_messages_preserves_failed_tool_result_and_unsupervises_call() -> None:
    turns = [
        {"thought": "lookup", "name": "describe_target", "arguments": {"target": UUID_A}, "response": json.dumps({"error": "no such target"}), "is_error": True},
        {"thought": "retry", "name": "describe_target", "arguments": {"target": UUID_A}, "response": json.dumps({"refs": [f"pg:targets:{UUID_A}"]}), "is_error": False},
    ]
    messages, _ = build_messages(_seed(), turns, max_turns=30, alias=True, tool_result_max_chars=6000)

    assert messages[2]["supervise"] is False
    assert messages[3]["is_error"] is True
    assert json.loads(messages[3]["content"]) == {"error": "no such target"}
    assert "supervise" not in messages[4]


def test_apply_call_supervision_uses_structured_error_only_and_preserves_existing_marks() -> None:
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_call("c1", "describe_target")]},
        {"role": "tool", "tool_call_id": "c1", "name": "describe_target", "content": "plain text mentions error but is not JSON"},
        {"role": "assistant", "content": "", "tool_calls": [_call("c2", "describe_target")]},
        {"role": "tool", "tool_call_id": "c2", "name": "describe_target", "content": json.dumps({"error": "structured"})},
        {"role": "assistant", "content": "", "supervise": False, "tool_calls": [_call("c3", "describe_target")]},
        {"role": "tool", "tool_call_id": "c3", "name": "describe_target", "content": json.dumps({"error": "already marked"})},
    ]

    assert apply_call_supervision(messages) == 1
    assert "supervise" not in messages[0]
    assert messages[2]["supervise"] is False
    assert messages[4]["supervise"] is False


def test_export_rejected_submit_preserves_error_and_arguments(tmp_path: Path) -> None:
    run = tmp_path / "run1"
    run.mkdir()
    (run / "seed.json").write_text(json.dumps(_seed()), encoding="utf-8")
    invalid = {"status": "provisional", "summary": "s", "causes": json.dumps(_submit_args()["causes"])}
    records = [{"kind": "submit", "thought": "bad", "name": SUBMIT_TOOL_NAME, "arguments": invalid}]
    (run / "ledger.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    messages, _ = export_teacher_run(run, max_turns=30, alias=False)

    assert messages[2]["supervise"] is False
    assert json.loads(messages[2]["tool_calls"][0]["function"]["arguments"]) == invalid
    assert messages[3]["is_error"] is True
    assert "causes must be a list" in json.loads(messages[3]["content"])["error"]


def test_valid_aliased_submit_remains_supervised() -> None:
    turns = [{"thought": "done", "name": SUBMIT_TOOL_NAME, "arguments": _submit_args(), "response": "accepted", "is_error": False}]

    messages, mapping = build_messages(_seed(), turns, max_turns=30, alias=True, tool_result_max_chars=6000)

    args = json.loads(messages[2]["tool_calls"][0]["function"]["arguments"])
    assert mapping == {UUID_A: "T1"}
    assert args["causes"][0]["target"] == "T1"
    assert messages[3]["content"] == "accepted"
    assert "supervise" not in messages[2]


def test_apply_call_supervision_reuses_runtime_submit_candidate_limit() -> None:
    args = _submit_args()
    args["causes"] = [
        {"target": f"T{index}", "mechanism": "m", "support_refs": [f"tool:r{index}"]}
        for index in range(1, 5)
    ]
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_call("c1", SUBMIT_TOOL_NAME, args)]},
        {"role": "tool", "tool_call_id": "c1", "name": SUBMIT_TOOL_NAME, "content": "accepted"},
    ]

    assert apply_call_supervision(messages) == 1
    assert messages[0]["supervise"] is False


def test_apply_call_supervision_rejects_submit_target_that_is_not_uuid_or_alias() -> None:
    args = _submit_args()
    args["causes"][0]["target"] = "checkout-api"
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_call("c1", SUBMIT_TOOL_NAME, args)]},
        {"role": "tool", "tool_call_id": "c1", "name": SUBMIT_TOOL_NAME, "content": "accepted"},
    ]

    assert apply_call_supervision(messages) == 1
    assert messages[0]["supervise"] is False
