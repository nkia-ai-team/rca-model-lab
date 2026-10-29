import pytest

from rca_lab.mcp.causal_depth import (
    CAUSAL_DEPTH_RUBRIC_VERSION,
    causal_depth_cache_valid,
    depth_item,
    validate_causal_depth_verdict,
)

MESSAGES = [
    {"role": "user", "content": "seed"},
    {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "scan_metrics", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "scan_metrics", "content": "mem_rss rose 3x"},
    {"role": "assistant", "tool_calls": [{"id": "c2", "function": {"name": "list_changes", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c2", "name": "list_changes", "content": "rollout changed MAX_WORKERS"},
    {"role": "assistant", "tool_calls": [{"id": "c3", "function": {"name": "submit_rca", "arguments": "{}"}}]},
]
RESULT = {"status": "confirmed", "summary": "s", "causes": [{"target": "t", "mechanism": "m", "support_refs": []}]}
SCORE = {"stop_reason": "submitted"}


def _row(link, supported, refs):
    return {"link": link, "stated": "x", "supported": supported, "evidence_refs": refs, "explanation": "y"}


def test_depth_counts_supported_link_types():
    item = depth_item(MESSAGES, RESULT, SCORE)
    assert item["rubric_version"] == CAUSAL_DEPTH_RUBRIC_VERSION
    v = validate_causal_depth_verdict(item, {"links": [_row("failure_mode", True, ["m2"]), _row("trigger", True, ["m4"]),
                                                        _row("proximate_cause", False, [])], "reason": "r"})
    assert v["depth"] == pytest.approx(2 / 3, abs=1e-4)
    assert validate_causal_depth_verdict(item, {"links": [], "reason": "r"})["depth"] == 0


@pytest.mark.parametrize("links", [
    [_row("failure_mode", True, [])],                          # supported without refs
    [_row("failure_mode", True, ["m9"])],                      # ref that does not exist
    [_row("failure_mode", True, ["m2"]), _row("failure_mode", True, ["m2"])],   # duplicate link type
    [_row("impact", True, ["m2"])],                            # unknown link
])
def test_invalid_verdicts_are_rejected(links):
    with pytest.raises((ValueError, TypeError)):
        validate_causal_depth_verdict(depth_item(MESSAGES, RESULT, SCORE), {"links": links, "reason": "r"})


def test_cache_record_must_match_item_and_rubric():
    import hashlib

    from rca_lab.mcp.causal_depth import CAUSAL_DEPTH_SYSTEM
    from rca_lab.mcp.rl_reward import sha256_json
    item = depth_item(MESSAGES, RESULT, SCORE)
    verdict = validate_causal_depth_verdict(item, {"links": [_row("failure_mode", True, ["m2"])], "reason": "r"})
    record = {"provider": "claude", "model": "m", "rubric_version": CAUSAL_DEPTH_RUBRIC_VERSION,
              "rubric_prompt_sha256": hashlib.sha256(CAUSAL_DEPTH_SYSTEM.encode()).hexdigest(),
              "item_sha256": sha256_json(item), "item": item, "verdict": verdict}
    assert causal_depth_cache_valid(record, item, model="m", provider="claude")
    assert not causal_depth_cache_valid({**record, "verdict": {**verdict, "depth": 1.0}}, item, model="m", provider="claude")
    assert not causal_depth_cache_valid(record, depth_item(MESSAGES[:3] + MESSAGES[5:], RESULT, SCORE), model="m", provider="claude")
