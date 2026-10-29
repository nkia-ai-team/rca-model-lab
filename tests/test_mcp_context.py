from __future__ import annotations

import json
from itertools import pairwise

import pytest

from rca_lab.mcp.context import rolling_view


def _episode(turns: int, chars: int) -> list[dict]:
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for index in range(turns):
        call = {"id": f"c{index}", "type": "function", "function": {"name": "read", "arguments": json.dumps({"i": index})}}
        messages.append({"role": "assistant", "content": "", "reasoning_content": "think", "tool_calls": [call]})
        messages.append({"role": "tool", "tool_call_id": f"c{index}", "content": f"{index:04d}" + "x" * chars})
    return messages


def _prompt_views(messages: list[dict], **kwargs) -> list[list[dict]]:
    """The view the server sees before each assistant turn and after the last tool result."""
    ends = [index for index, message in enumerate(messages) if message["role"] == "assistant"] + [len(messages)]
    return [rolling_view(messages[:end], **kwargs) for end in ends]


def test_budget_keeps_everything_whole_under_budget() -> None:
    messages = _episode(12, 500)
    view = rolling_view(messages, policy="budget", budget_chars=10**6, old_chars=100)
    assert [m.get("content") for m in view if m["role"] == "tool"] == [m["content"] for m in messages if m["role"] == "tool"]
    assert all("reasoning_content" not in m for m in view)


def test_budget_compacts_all_but_recent_at_once_and_sticks() -> None:
    messages = _episode(30, 1000)
    views = _prompt_views(messages, policy="budget", budget_chars=20_000, old_chars=100, recent_full=8)
    compacted = [{i for i, m in enumerate(v) if m["role"] == "tool" and len(m["content"]) < 1000} for v in views]
    for before, after in pairwise(compacted):
        assert before <= after  # once compacted, a result stays compacted
    events = sum(1 for before, after in pairwise(compacted) if after != before)
    assert 0 < events < len(views) // 4  # compaction happens in a few batches, not every turn
    last = views[-1]
    tools = [i for i, m in enumerate(last) if m["role"] == "tool"]
    assert all(len(last[i]["content"]) > 1000 for i in tools[-8:])


def test_budget_history_is_append_only_between_compactions() -> None:
    messages = _episode(30, 1000)
    views = _prompt_views(messages, policy="budget", budget_chars=20_000, old_chars=100, recent_full=8)
    appended = sum(1 for before, after in pairwise(views) if after[: len(before)] == before)
    rolling = _prompt_views(messages, old_chars=100, recent_full=8)
    rolling_appended = sum(1 for before, after in pairwise(rolling) if after[: len(before)] == before)
    assert appended > rolling_appended
    assert appended >= len(views) - 1 - len(views) // 4


def test_rolling8_default_unchanged_and_unknown_policy_rejected() -> None:
    messages = _episode(10, 500)
    assert rolling_view(messages, old_chars=100) == rolling_view(messages, old_chars=100, policy="rolling8")
    tools = [m for m in rolling_view(messages, old_chars=100) if m["role"] == "tool"]
    assert [len(m["content"]) > 500 for m in tools] == [False, False] + [True] * 8
    with pytest.raises(ValueError):
        rolling_view(messages, policy="nope")
