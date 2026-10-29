import pytest

from rca_lab.mcp.decisive_evidence import DECISIVE_RUBRIC_VERSION, decisive_item, validate_decisive_verdict

MESSAGES = [
    {"role": "user", "content": "seed"},
    {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "sample_logs", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "sample_logs", "content": "ORA-04043 object does not exist"},
    {"role": "assistant", "tool_calls": [{"id": "c2", "function": {"name": "submit_rca", "arguments": "{}"}}]},
]
RESULT = {"status": "confirmed", "summary": "table dropped", "causes": []}
SCORE = {"stop_reason": "submitted"}


def _row(status, refs):
    return {"evidence": "e", "status": status, "evidence_refs": refs, "explanation": "x"}


def test_recall_and_observed_rates():
    item = decisive_item(MESSAGES, RESULT, SCORE, ["error text", "tablespace drop", "other tables fine"])
    assert item["rubric_version"] == DECISIVE_RUBRIC_VERSION and item["decisive_items"][1] == "tablespace drop"
    v = validate_decisive_verdict(item, {"items": [_row("cited", ["m2"]), _row("observed", ["m2"]), _row("missing", [])], "reason": "r"})
    assert v["recall"] == pytest.approx(1 / 3, abs=1e-4) and v["observed"] == pytest.approx(2 / 3, abs=1e-4)


@pytest.mark.parametrize("items", [
    [],                                         # nothing extracted
    [_row("cited", [])],                        # cited without a ref
    [_row("observed", ["m9"])],                 # ref that does not exist
    [_row("found", ["m2"])],                    # unknown status
    [_row("missing", [])] * 2,                  # count differs from the fixed item list
])
def test_invalid_verdicts_are_rejected(items):
    with pytest.raises((ValueError, TypeError)):
        validate_decisive_verdict(decisive_item(MESSAGES, RESULT, SCORE, ["x"]), {"items": items, "reason": "r"})
