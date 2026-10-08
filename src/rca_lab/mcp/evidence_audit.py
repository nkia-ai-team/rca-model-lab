"""Evidence-bounded audit items for RCA rationale judging.

The budget in this module is measured in serialized JSON characters, not tokens.  Audit
items intentionally use the same rolling view as inference/training, then select visible
prior context deterministically from newest to oldest until the item fits the configured
character budget.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Literal

from rca_lab.mcp.context import OLD_CHARS, RECENT_FULL, rolling_view

POLICY_VERSION = "mcp-evidence-audit-v1"
SUPPORTED_STATUSES = {"supported", "contradicted", "insufficient_evidence"}
VerdictStatus = Literal["supported", "contradicted", "insufficient_evidence"]


def _json_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _content_sha(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _tool_call_ids(message: dict[str, Any]) -> list[str]:
    return [str(call.get("id")) for call in message.get("tool_calls") or [] if call.get("id")]


def _call_lookup(view: list[dict[str, Any]]) -> dict[str, tuple[int, dict[str, Any]]]:
    lookup: dict[str, tuple[int, dict[str, Any]]] = {}
    for position, message in enumerate(view):
        if message.get("role") != "assistant":
            continue
        for call_id in _tool_call_ids(message):
            lookup[call_id] = (position, message)
    return lookup


def _message_for_context(position: int, message: dict[str, Any]) -> dict[str, Any]:
    if _eligible_evidence(message):
        return {"message_position": position, "role": message.get("role"), "evidence_ref": f"m{position}"}
    item = copy.deepcopy(message)
    item.pop("supervise", None)
    item.pop("reasoning_content", None)
    item.pop("reasoning", None)
    if item.get("role") == "assistant":
        item["content"] = ""
    item["message_position"] = position
    return item


def _eligible_evidence(message: dict[str, Any]) -> bool:
    return message.get("role") in {"user", "tool"} and str(message.get("content", "")) != ""


def _evidence_ref(
    *,
    position: int,
    visible: dict[str, Any],
    source: dict[str, Any],
    call_lookup: dict[str, tuple[int, dict[str, Any]]],
) -> dict[str, Any]:
    content = str(visible.get("content", ""))
    original = str(source.get("content", ""))
    ref: dict[str, Any] = {
        "ref_id": f"m{position}",
        "message_position": position,
        "role": visible.get("role"),
        "source_sha256": _content_sha(original),
        "visible_sha256": _content_sha(content),
        "source_chars": len(original),
        "visible_chars": len(content),
        "rolling_view_truncated": content != original,
        "snippet": content,
    }
    if visible.get("role") == "tool":
        tool_call_id = visible.get("tool_call_id")
        ref["tool_call_id"] = tool_call_id
        if visible.get("name") is not None:
            ref["tool_name"] = visible.get("name")
        if tool_call_id in call_lookup:
            call_position, call_message = call_lookup[str(tool_call_id)]
            matching = [call for call in call_message.get("tool_calls") or [] if call.get("id") == tool_call_id]
            ref["paired_call_position"] = call_position
            if matching:
                function = matching[0].get("function") or {}
                ref["paired_call_name"] = function.get("name")
                ref["paired_call_arguments"] = function.get("arguments")
    return ref


def _target_turn(index: int, message: dict[str, Any]) -> dict[str, Any]:
    reasoning = str(message.get("reasoning_content") or "")
    return {
        "message_position": index,
        "role": message.get("role"),
        "content": copy.deepcopy(message.get("content", "")),
        "reasoning_content": reasoning,
        "reasoning_truncated": False,
        "reasoning_omitted_chars": 0,
        "tool_calls": copy.deepcopy(message.get("tool_calls") or []),
    }


def _make_item(
    *,
    messages: list[dict[str, Any]],
    index: int,
    max_chars: int,
    selected_positions: set[int],
) -> dict[str, Any]:
    prior = messages[:index]
    view = rolling_view(prior)
    calls = _call_lookup(view)
    ordered_positions = sorted(selected_positions)
    evidence_refs = [
        _evidence_ref(position=position, visible=view[position], source=prior[position], call_lookup=calls)
        for position in ordered_positions
        if _eligible_evidence(view[position])
    ]
    total_eligible = sum(1 for message in view if _eligible_evidence(message))
    visible_eligible = len(evidence_refs)
    omitted_evidence_positions = [
        position
        for position, message in enumerate(view)
        if _eligible_evidence(message) and position not in selected_positions
    ]
    item = {
        "policy_version": POLICY_VERSION,
        "budget": {"max_chars": max_chars, "unit": "json_chars"},
        "input": {
            "target_index": index,
            "prior_message_count": len(prior),
            "rolling_view_policy": {"recent_full": RECENT_FULL, "old_chars": OLD_CHARS},
        },
        "target_turn": _target_turn(index, messages[index]),
        "prior_context": {
            "source": "rolling_view(messages[:target_index])",
            "messages": [_message_for_context(position, view[position]) for position in ordered_positions],
            "total_messages": len(view),
            "visible_messages": len(ordered_positions),
            "omitted_messages": len(view) - len(ordered_positions),
            "omitted_positions": [position for position in range(len(view)) if position not in selected_positions],
        },
        "evidence": {
            "refs": evidence_refs,
            "total_eligible": total_eligible,
            "visible": visible_eligible,
            "omitted": total_eligible - visible_eligible,
            "omitted_positions": omitted_evidence_positions,
        },
        "truncation": {
            "input_truncated": any(
                str(view[position].get("content", "")) != str(prior[position].get("content", ""))
                for position in range(len(view))
                if view[position].get("role") == "tool"
            ),
            "selection_policy": "recent_first_prior_rolling_view",
            "serialized_chars": 0,
        },
    }
    previous = -1
    while previous != item["truncation"]["serialized_chars"]:
        previous = item["truncation"]["serialized_chars"]
        item["truncation"]["serialized_chars"] = _json_chars(item)
    return item


def _selection_group(position: int, view: list[dict[str, Any]], calls: dict[str, tuple[int, dict[str, Any]]]) -> set[int]:
    group = {position}
    message = view[position]
    if message.get("role") == "tool":
        call_id = message.get("tool_call_id")
        if call_id in calls:
            group.add(calls[str(call_id)][0])
    return group


def build_evidence_item(messages: list[dict[str, Any]], index: int, *, max_chars: int = 16000) -> dict[str, Any]:
    """Build a JSON-ready audit item for one assistant turn.

    Only messages before `index` are eligible as evidence.  The current turn's reasoning
    and tool calls are included as the claim being audited; later messages and the tool
    result of the current call are never visible.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive JSON character budget")
    if index < 0 or index >= len(messages):
        raise IndexError("target index is outside messages")
    target = messages[index]
    if target.get("role") != "assistant":
        raise ValueError("target index must point to an assistant turn")

    prior_view = rolling_view(messages[:index])
    calls = _call_lookup(prior_view)
    selected: set[int] = set()
    item = _make_item(messages=messages, index=index, max_chars=max_chars, selected_positions=selected)
    if _json_chars(item) > max_chars:
        raise ValueError("non-evidence fields exceed max_chars JSON character budget")

    for position in range(len(prior_view) - 1, -1, -1):
        group = _selection_group(position, prior_view, calls)
        if group <= selected:
            continue
        trial = selected | group
        candidate = _make_item(messages=messages, index=index, max_chars=max_chars, selected_positions=trial)
        if _json_chars(candidate) <= max_chars:
            selected = trial

    item = _make_item(messages=messages, index=index, max_chars=max_chars, selected_positions=selected)
    if _json_chars(item) > max_chars:
        raise AssertionError("evidence item exceeded max_chars after bounded selection")
    return item


def _item_incomplete(item: dict[str, Any]) -> bool:
    return bool(
        item.get("prior_context", {}).get("omitted_messages")
        or item.get("evidence", {}).get("omitted")
        or item.get("truncation", {}).get("input_truncated")
    )


def validate_evidence_verdict(item: dict[str, Any], verdict: dict[str, Any]) -> dict[str, Any]:
    """Validate an evidence-audit verdict and return a normalized copy.

    `supported` and `contradicted` verdicts must cite visible evidence refs.  When the
    item is incomplete or rolling-view-truncated, a verdict explicitly marked
    `absence_based` cannot be accepted as proof that evidence does not exist elsewhere.
    """
    status = verdict.get("status")
    if status not in SUPPORTED_STATUSES:
        raise ValueError("verdict status must be supported, contradicted, or insufficient_evidence")
    explanation_value = verdict.get("explanation")
    if not isinstance(explanation_value, str):
        raise TypeError("verdict requires a string claim-level explanation")
    explanation = explanation_value.strip()
    if not explanation:
        raise ValueError("verdict requires a nonempty claim-level explanation")

    refs = verdict.get("evidence_refs", [])
    if refs is None:
        refs = []
    if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
        raise ValueError("evidence_refs must be a list of ref_id strings")
    if status in {"supported", "contradicted"} and not refs:
        raise ValueError("supported and contradicted verdicts require nonempty evidence_refs")

    available = {ref.get("ref_id") for ref in item.get("evidence", {}).get("refs", [])}
    missing = sorted(set(refs) - available)
    if missing:
        raise ValueError(f"verdict references unavailable evidence refs: {missing}")
    absence_based = verdict.get("absence_based", None)
    if status in {"supported", "contradicted"} and not isinstance(absence_based, bool):
        raise ValueError("supported and contradicted verdicts require boolean absence_based")
    if absence_based and status in {"supported", "contradicted"} and _item_incomplete(item):
        raise ValueError("incomplete or truncated evidence items cannot prove absence")

    normalized = dict(verdict)
    normalized["status"] = status
    normalized["explanation"] = explanation
    normalized["evidence_refs"] = refs
    return normalized
