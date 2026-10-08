"""Canonical context policy shared by rollout, rationalization and training.

Legacy trajectories retain responses up to the rollout truncation budget. Structured-v1
trajectories contain bounded JSON views backed by run-local complete response artifacts.
Recent results remain as stored; older structured views keep whole fields and retrieval
metadata, while older legacy responses retain the historical head-slice policy. This function
is shared by rollout, rationalization and training; it never mutates stored trajectories.
"""

from __future__ import annotations

from typing import Any

from rca_lab.mcp.observations import compact_observation

RECENT_FULL = 8
OLD_CHARS = 1200
_TRUNCATED_NOTE = "\n…[이전 관측 요약 절단 — 필요하면 같은 인자로 다시 조회]"


def rolling_view(messages: list[dict[str, Any]], *, recent_full: int = RECENT_FULL, old_chars: int = OLD_CHARS) -> list[dict[str, Any]]:
    """Return the inference-time view of `messages` (a new list; inputs are not mutated)."""
    tool_positions = [index for index, message in enumerate(messages) if message.get("role") == "tool"]
    old = set(tool_positions[:-recent_full]) if recent_full > 0 else set(tool_positions)
    view: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        item = {key: value for key, value in message.items() if key != "reasoning_content"}
        if index in old:
            content = str(item.get("content", ""))
            if len(content) > old_chars:
                compact = compact_observation(content, old_chars)
                item["content"] = compact if compact is not None else content[:old_chars] + _TRUNCATED_NOTE
        view.append(item)
    return view
