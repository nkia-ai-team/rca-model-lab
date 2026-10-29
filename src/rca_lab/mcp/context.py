"""Canonical context policy shared by rollout, rationalization and training.

Legacy trajectories retain responses up to the rollout truncation budget. Structured-v1
trajectories contain bounded JSON views backed by run-local complete response artifacts.
Recent results remain as stored; older structured views keep whole fields and retrieval
metadata. Structured-v2 preserves every delivered envelope even after it ages out of the
recent window, while older legacy responses retain the historical head-slice policy. This function
is shared by rollout, rationalization and training; it never mutates stored trajectories.
"""

from __future__ import annotations

import json
from typing import Any

from rca_lab.mcp.observations import compact_observation

RECENT_FULL = 8
OLD_CHARS = 1200
_TRUNCATED_NOTE = "\n…[이전 관측 요약 절단 — 필요하면 같은 인자로 다시 조회]"
# 2026-10-28 context policies. "rolling8" (default): every turn compacts the tool result that leaves the recent window,
# so the prompt changes mid-history each turn and prefix reuse (vLLM prefix cache, the GRPO tree) restarts there --
# ~3.3x recomputed tokens on Flash RL groups. "budget": tool results stay whole until the view exceeds BUDGET_CHARS at a
# prompt boundary, then everything but the last RECENT_FULL results is compacted at once and stays compacted; between
# those events the history only grows. Same maximum view length as rolling8 on 52 Flash rollouts, 0.39x the recompute.
CONTEXT_POLICIES = ("rolling8", "budget")
BUDGET_CHARS = 140_000


def _history_tool_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep rejected arguments readable by strict chat-template JSON parsers.

    This is a history-only envelope, not an argument repair: the original call
    remains rejected, with its tool error paired by ID and its raw bytes retained
    in both the stored trajectory and this envelope. Execution never uses it.
    Copy nested containers only for calls whose arguments need an envelope.
    """
    result = calls
    for index, call in enumerate(calls):
        function = call.get("function")
        if not isinstance(function, dict) or "arguments" not in function:
            continue
        raw = function["arguments"]
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            error = "invalid JSON; not executed"
        else:
            if isinstance(parsed, dict):
                continue
            error = "arguments must be an object; not executed"
        arguments = json.dumps(
            {"_harness_invalid_arguments": {"raw": raw, "error": error}},
            ensure_ascii=False,
        )
        if result is calls:
            result = list(calls)
        result[index] = {**call, "function": {**function, "arguments": arguments}}
    return result


def _compacted(content: str, old_chars: int) -> str:
    if len(content) <= old_chars:
        return content
    compact = compact_observation(content, old_chars)
    return compact if compact is not None else content[:old_chars] + _TRUNCATED_NOTE


def _item_chars(item: dict[str, Any]) -> int:
    return len(str(item.get("content", ""))) + (len(json.dumps(item["tool_calls"], ensure_ascii=False)) if item.get("tool_calls") else 0)


def _budget_old(items: list[dict[str, Any]], recent_full: int, old_chars: int, budget_chars: int) -> set[int]:
    """Tool positions compacted under the budget policy: replay every prompt boundary (before each assistant message and
    at the end) so the sticky decision is a pure function of the messages, identical in rollout and training."""
    old: set[int] = set()
    tools: list[int] = []
    size = 0
    for index in range(len(items) + 1):
        if (index == len(items) or items[index].get("role") == "assistant") and size > budget_chars:
            for position in tools[:-recent_full] if recent_full > 0 else tools:
                if position not in old:
                    old.add(position)
                    size += len(_compacted(str(items[position].get("content", "")), old_chars)) - len(str(items[position].get("content", "")))
        if index < len(items):
            size += _item_chars(items[index])
            if items[index].get("role") == "tool":
                tools.append(index)
    return old


def rolling_view(messages: list[dict[str, Any]], *, recent_full: int = RECENT_FULL, old_chars: int = OLD_CHARS,
                 policy: str = "rolling8", budget_chars: int = BUDGET_CHARS) -> list[dict[str, Any]]:
    """Return the inference-time view of `messages` (a new list; inputs are not mutated)."""
    if policy not in CONTEXT_POLICIES:
        raise ValueError(f"unknown context policy {policy!r}")
    items: list[dict[str, Any]] = []
    for message in messages:
        item = {key: value for key, value in message.items() if key != "reasoning_content"}
        if item.get("role") == "assistant" and item.get("tool_calls"):
            item["tool_calls"] = _history_tool_calls(item["tool_calls"])
        items.append(item)
    if policy == "budget":
        old = _budget_old(items, recent_full, old_chars, budget_chars)
    else:
        tool_positions = [index for index, item in enumerate(items) if item.get("role") == "tool"]
        old = set(tool_positions[:-recent_full]) if recent_full > 0 else set(tool_positions)
    for index in old:
        content = str(items[index].get("content", ""))
        if len(content) > old_chars:
            items[index]["content"] = _compacted(content, old_chars)
    return items
