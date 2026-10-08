"""Deterministic scoring of a `submit_rca` result against the golden contract."""

from __future__ import annotations

from typing import Any

from rca_lab.mcp.golden import ExpectedCase, candidates_from_result, root_f1, root_precision_recall


def score_result(
    expected: ExpectedCase,
    result: dict[str, Any] | None,
    *,
    turns: int,
    tool_calls: int,
    tool_errors: int,
    observed_refs: set[str],
) -> dict[str, Any]:
    if result is None:
        return {
            "status": "missing",
            "status_correct": False,
            "root_f1": 0.0,
            "root_precision": 0.0,
            "root_recall": 0.0,
            "strict_correct": False,
            "unsupported_confirmation": 0,
            "ref_grounding": 0.0,
            "reward": 0.0,
            "turns": turns,
            "tool_calls": tool_calls,
            "tool_errors": tool_errors,
        }
    candidates = candidates_from_result(result)
    f1 = root_f1(expected.roots, candidates, expected.allowed_target_ids)
    precision, recall = root_precision_recall(expected.roots, candidates, expected.allowed_target_ids)
    status_correct = result.get("status") == expected.expected_status
    unsupported = int(result.get("status") == "confirmed" and f1 < 1.0)
    cited = [ref for cause in result.get("causes", []) for ref in cause.get("support_refs", [])]
    cited += [ref for cause in result.get("external_causes", []) for ref in cause.get("evidence_refs", [])]
    grounding = (sum(ref in observed_refs for ref in cited) / len(cited)) if cited else 0.0
    strict = bool(f1 == 1.0 and status_correct and grounding == 1.0 and unsupported == 0)
    reward = max(0.0, min(1.0, 0.55 * f1 + 0.15 * float(f1 == 1.0) + 0.15 * grounding * f1 + 0.10 * float(status_correct) * f1 + 0.05 * float(strict) - 0.60 * unsupported))
    return {
        "status": result.get("status"),
        "status_correct": status_correct,
        "root_f1": f1,
        "root_precision": precision,
        "root_recall": recall,
        "strict_correct": strict,
        "unsupported_confirmation": unsupported,
        "ref_grounding": grounding,
        "reward": reward,
        "turns": turns,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
    }
