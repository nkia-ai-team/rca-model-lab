import copy
import json

import pytest

from rca_lab.mcp.context import rolling_view
from rca_lab.mcp.evidence_audit import build_evidence_item, validate_evidence_verdict


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def _messages() -> list[dict]:
    return [
        {"role": "system", "content": "system policy must not become evidence"},
        {"role": "user", "content": json.dumps({"alarms": [{"target": "T1", "metric": "latency"}]}, ensure_ascii=False)},
        {"role": "assistant", "content": "", "reasoning_content": "old private chain", "tool_calls": [_call("c1", "describe_target", {"target": "T1"})]},
        {"role": "tool", "tool_call_id": "c1", "name": "describe_target", "content": json.dumps({"target": "T1", "service": "checkout"}, ensure_ascii=False)},
        {"role": "assistant", "content": "", "reasoning_content": "current claim", "tool_calls": [_call("c2", "sample_logs", {"target": "T1"})]},
        {"role": "tool", "tool_call_id": "c2", "name": "sample_logs", "content": "future result must not leak"},
    ]


def _dump_chars(value: dict) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def test_build_evidence_item_uses_only_prior_rolling_view_and_no_future_result() -> None:
    item = build_evidence_item(_messages(), 4, max_chars=12000)
    view = rolling_view(_messages()[:4])

    assert item["target_turn"]["reasoning_content"] == "current claim"
    assert item["target_turn"]["tool_calls"][0]["id"] == "c2"
    assert [message["message_position"] for message in item["prior_context"]["messages"]] == list(range(len(view)))
    refs = {ref["message_position"]: ref for ref in item["evidence"]["refs"]}
    assert refs[1]["snippet"] == view[1]["content"]
    assert refs[3]["snippet"] == view[3]["content"]
    assert next(message for message in item["prior_context"]["messages"] if message["message_position"] == 3) == {
        "message_position": 3,
        "role": "tool",
        "evidence_ref": "m3",
    }
    serialized = json.dumps(item, ensure_ascii=False)
    assert "future result must not leak" not in serialized
    assert "old private chain" not in serialized


def test_evidence_refs_are_user_or_tool_only_with_stable_provenance_and_call_pairing() -> None:
    item = build_evidence_item(_messages(), 4, max_chars=12000)
    refs = {ref["message_position"]: ref for ref in item["evidence"]["refs"]}

    assert set(refs) == {1, 3}
    assert refs[1]["role"] == "user"
    assert refs[3]["role"] == "tool"
    assert refs[3]["tool_call_id"] == "c1"
    assert refs[3]["tool_name"] == "describe_target"
    assert refs[3]["paired_call_position"] == 2
    assert refs[3]["paired_call_name"] == "describe_target"
    assert refs[3]["source_sha256"] == refs[3]["visible_sha256"]
    assert "system" not in {ref["role"] for ref in item["evidence"]["refs"]}


def test_build_evidence_item_is_bounded_in_unicode_json_chars_and_reports_omissions() -> None:
    messages = _messages()
    messages[1]["content"] = "알람" * 200
    messages[3]["content"] = "관측" * 200

    tiny = build_evidence_item(messages, 4, max_chars=900)
    assert _dump_chars(tiny) <= 900
    assert tiny["prior_context"]["omitted_messages"] > 0
    assert tiny["evidence"]["omitted"] > 0
    assert tiny["budget"] == {"max_chars": 900, "unit": "json_chars"}


def test_build_evidence_item_does_not_mutate_input_messages() -> None:
    messages = _messages()
    before = copy.deepcopy(messages)
    build_evidence_item(messages, 4, max_chars=12000)
    assert messages == before


@pytest.mark.parametrize("supervise", [True, False])
def test_export_supervision_metadata_cannot_influence_judge_input(supervise) -> None:
    messages = _messages()
    expected = build_evidence_item(messages, 4, max_chars=12000)
    for message in messages:
        message["supervise"] = supervise
    actual = build_evidence_item(messages, 4, max_chars=12000)
    assert actual == expected
    assert all("supervise" not in message for message in actual["prior_context"]["messages"])


def test_old_tool_observation_uses_rolling_view_truncation_and_manifest() -> None:
    messages: list[dict] = [{"role": "user", "content": "alarm"}]
    for index in range(10):
        messages.append({"role": "assistant", "content": "", "reasoning_content": f"r{index}", "tool_calls": [_call(f"c{index}", "sample_metrics", {"target": "T1"})]})
        messages.append({"role": "tool", "tool_call_id": f"c{index}", "name": "sample_metrics", "content": str(index) * 3000})
    messages.append({"role": "assistant", "content": "", "reasoning_content": "judge this", "tool_calls": [_call("final", "submit_rca", {})]})

    item = build_evidence_item(messages, len(messages) - 1, max_chars=120000)
    old_ref = next(ref for ref in item["evidence"]["refs"] if ref["message_position"] == 2)
    assert old_ref["rolling_view_truncated"] is True
    assert old_ref["source_chars"] == 3000
    assert old_ref["visible_chars"] < old_ref["source_chars"]
    assert item["truncation"]["input_truncated"] is True


def test_validate_evidence_verdict_accepts_supported_and_insufficient() -> None:
    item = build_evidence_item(_messages(), 4, max_chars=12000)
    supported = validate_evidence_verdict(
        item,
        {"status": "supported", "evidence_refs": ["m3"], "explanation": "tool observation names checkout", "absence_based": False},
    )
    insufficient = validate_evidence_verdict(
        item,
        {"status": "insufficient_evidence", "evidence_refs": [], "explanation": "visible evidence does not establish mechanism"},
    )
    assert supported["status"] == "supported"
    assert insufficient["status"] == "insufficient_evidence"


@pytest.mark.parametrize(
    "verdict, error_type, match",
    [
        ({"status": "maybe", "evidence_refs": [], "explanation": "x"}, ValueError, "status"),
        ({"status": "supported", "evidence_refs": [], "explanation": "x", "absence_based": False}, ValueError, "require nonempty"),
        ({"status": "supported", "evidence_refs": ["missing"], "explanation": "x", "absence_based": False}, ValueError, "unavailable"),
        ({"status": "contradicted", "evidence_refs": ["m1"], "explanation": "", "absence_based": False}, ValueError, "explanation"),
        ({"status": "supported", "evidence_refs": "m1", "explanation": "x", "absence_based": False}, ValueError, "evidence_refs"),
        ({"status": "supported", "evidence_refs": ["m1"], "explanation": None, "absence_based": False}, TypeError, "string"),
        ({"status": "supported", "evidence_refs": ["m1"], "explanation": "x"}, ValueError, "absence_based"),
    ],
)
def test_validate_evidence_verdict_rejects_malformed_refs(verdict: dict, error_type: type[Exception], match: str) -> None:
    item = build_evidence_item(_messages(), 4, max_chars=12000)
    with pytest.raises(error_type, match=match):
        validate_evidence_verdict(item, verdict)


def test_incomplete_or_truncated_input_cannot_prove_absence() -> None:
    item = build_evidence_item(_messages(), 4, max_chars=1600)
    ref_id = item["evidence"]["refs"][0]["ref_id"]

    with pytest.raises(ValueError, match="cannot prove absence"):
        validate_evidence_verdict(
            item,
            {
                "status": "contradicted",
                "evidence_refs": [ref_id],
                "explanation": "absence of other rows contradicts the claim",
                "absence_based": True,
            },
        )


def test_no_observations_allows_only_insufficient_without_refs() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": "", "reasoning_content": "claim", "tool_calls": [_call("c1", "submit_rca", {})]},
    ]
    item = build_evidence_item(messages, 1, max_chars=2000)
    assert item["evidence"]["total_eligible"] == 0
    assert validate_evidence_verdict(item, {"status": "insufficient_evidence", "explanation": "no prior observations", "evidence_refs": []})
    with pytest.raises(ValueError, match="unavailable"):
        validate_evidence_verdict(item, {"status": "supported", "explanation": "x", "evidence_refs": ["m0"], "absence_based": False})


def test_missing_truncated_evidence_can_explain_insufficient_verdict() -> None:
    item = build_evidence_item(_messages(), 4, max_chars=1600)
    verdict = {"status": "insufficient_evidence", "evidence_refs": [],
               "explanation": "Required observations are absent from the truncated view.",
               "absence_based": True}
    assert validate_evidence_verdict(item, verdict)["status"] == "insufficient_evidence"
