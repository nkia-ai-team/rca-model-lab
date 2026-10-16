"""Recipe-level tests for dataset-informed MCP SFT controls."""

from __future__ import annotations

import json
from typing import Any

import pytest

from rca_lab.mcp.context import rolling_view
from rca_lab.train.mcp_sft import IGNORE_INDEX, tokenize_turns
from rca_lab.train.recipe import (
    SourceCaseBalancedSampler,
    prompt_prefix_length,
    select_turn_indices,
    validate_source_keys,
)


class PrefixTokenizer:
    def apply_chat_template(self, messages: list[dict[str, Any]], *, tokenize: bool, tools: Any = None, add_generation_prompt: bool = False, **_: Any) -> list[int]:
        assert tokenize
        ids = [1]
        for index, message in enumerate(messages):
            ids.append({"system": 10, "user": 11, "assistant": 12, "tool": 13}[message["role"]])
            body = json.dumps({key: value for key, value in message.items() if key != "role"}, sort_keys=True)
            ids.extend(20 + (ord(char) % 50) for char in body[:24])
            if message["role"] == "assistant":
                ids.append(2 if index + 1 < len(messages) else 3)
        if add_generation_prompt:
            ids.append(12)
        return ids


def call(name: str, index: int) -> dict[str, Any]:
    return {"id": f"c{index}", "type": "function", "function": {"name": name, "arguments": "{}"}}


def test_tool_coverage_keeps_final_submit_and_does_not_invent_final() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [call("describe_target", 1)]},
        {"role": "tool", "tool_call_id": "c1", "name": "describe_target", "content": "o"},
        {"role": "assistant", "content": "", "tool_calls": [call("sample_logs", 2)]},
        {"role": "tool", "tool_call_id": "c2", "name": "sample_logs", "content": "o"},
        {"role": "assistant", "content": "", "tool_calls": [call("describe_target", 3)]},
        {"role": "tool", "tool_call_id": "c3", "name": "describe_target", "content": "o"},
        {"role": "assistant", "content": "", "tool_calls": [call("submit_rca", 4)]},
    ]
    assert select_turn_indices(messages, 3, policy="tool_coverage", seed="x") == [2, 4, 8]
    no_submit = messages[:-1]
    assert select_turn_indices(no_submit, 2, policy="tool_coverage", seed="x") == [2, 4]


def test_selected_completion_preserves_original_runtime_prefix() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "reasoning_content": "r1", "tool_calls": [call("describe_target", 1)]},
        {"role": "tool", "tool_call_id": "c1", "name": "describe_target", "content": "o1"},
        {"role": "assistant", "content": "", "reasoning_content": "r2", "tool_calls": [call("submit_rca", 2)]},
    ]
    [example] = tokenize_turns(PrefixTokenizer(), messages, [], selected_message_indices={4})
    assert example["original_message_index"] == 4
    expected_prefix = PrefixTokenizer().apply_chat_template(rolling_view(messages[:4]), tokenize=True, tools=[], add_generation_prompt=True)
    assert example["input_ids"][: len(expected_prefix)] == expected_prefix
    assert prompt_prefix_length(example["labels"], IGNORE_INDEX) == len(expected_prefix)


def test_source_balanced_sampler_emits_equal_sources_per_accumulation_group() -> None:
    examples = []
    for source in ("tt4", "ob4", "openrca2", "polestar"):
        for case in ("a", "b"):
            examples.append({"source_key": source, "case_id": f"{source}-{case}", "original_message_index": 2})
    sampler = SourceCaseBalancedSampler(examples, source_keys=["tt4", "ob4", "openrca2", "polestar"], group_size=4, seed=7)
    order = list(sampler)
    assert len(order) == 8
    for start in range(0, len(order), 4):
        assert [examples[index]["source_key"] for index in order[start : start + 4]] == ["tt4", "ob4", "openrca2", "polestar"]
    audit = sampler.audit()
    assert audit["draws_by_source"] == {"ob4": 2, "openrca2": 2, "polestar": 2, "tt4": 2}
    assert audit["repeated_draws_by_source"] == {"tt4": 0, "ob4": 0, "openrca2": 0, "polestar": 0}


def test_source_balanced_sampler_rounds_epoch_up_to_group_size() -> None:
    examples = [
        {"source_key": "tt4", "case_id": "t", "original_message_index": 2},
        {"source_key": "ob4", "case_id": "o", "original_message_index": 2},
        {"source_key": "openrca2", "case_id": "r", "original_message_index": 2},
        {"source_key": "polestar", "case_id": "p", "original_message_index": 2},
        {"source_key": "tt4", "case_id": "t2", "original_message_index": 4},
    ]
    sampler = SourceCaseBalancedSampler(examples, source_keys=["tt4", "ob4", "openrca2", "polestar"], group_size=4)
    assert len(sampler) == 8
    assert sampler.audit()["repeated_draws_by_source"]["ob4"] == 1


def test_source_label_validation_requires_configured_dataset_keys() -> None:
    rows = [{"dataset_key": "tt4"}, {"dataset_key": "ob4"}]
    with pytest.raises(ValueError, match="not present"):
        validate_source_keys(rows, ["tt4", "ob4", "openrca2", "polestar"])
    with pytest.raises(ValueError, match="outside source_keys"):
        validate_source_keys([{"dataset_key": "tt4"}, {"dataset_key": "surprise"}], ["tt4", "ob4"])
    with pytest.raises(ValueError, match="outside source_keys"):
        validate_source_keys([{"dataset_key": None}], ["tt4"])


def test_build_examples_enforces_prompt_length_boundary(monkeypatch) -> None:
    from types import SimpleNamespace

    from rca_lab.train import mcp_sft

    turns = [
        {"input_ids": [1] * 8, "labels": [IGNORE_INDEX, IGNORE_INDEX, 7, 7], "prompt_tokens": 2},
        {"input_ids": [1] * 8, "labels": [IGNORE_INDEX, 7, 7, 7], "prompt_tokens": 6},
    ]
    monkeypatch.setattr(mcp_sft, "tokenize_turns", lambda *args, **kwargs: turns)
    config = SimpleNamespace(
        sequence_mode="turns",
        method="sft",
        max_length=10,
        max_prompt_length=4,
        reasoning_strength=None,
        supervise_end_token=False,
        reasoning_loss_weight=1.0,
        turns_per_episode=0,
        case_balanced_loss=False,
    )
    examples = mcp_sft.build_examples(config, None, [{"case_id": "case", "run": "run", "dataset_key": "tt4", "messages": [], "tools": []}])
    assert len(examples) == 1
    assert examples[0]["prompt_tokens"] == 2


def test_source_balanced_build_records_empty_rows_without_aborting(monkeypatch) -> None:
    from types import SimpleNamespace

    from rca_lab.train import mcp_sft

    def fake_tokenize(_tokenizer, messages, *_args, **_kwargs):
        length = messages[0]["length"]
        return [{"input_ids": [1] * length, "labels": [IGNORE_INDEX, 7], "prompt_tokens": 1}]

    monkeypatch.setattr(mcp_sft, "tokenize_turns", fake_tokenize)
    config = SimpleNamespace(
        sequence_mode="turns",
        method="sft",
        max_length=10,
        max_prompt_length=None,
        reasoning_strength=None,
        supervise_end_token=False,
        reasoning_loss_weight=1.0,
        turns_per_episode=0,
        case_balanced_loss=False,
        source_sampling="source_case_balanced",
    )
    rows = [
        {"case_id": "too-long", "run": "r1", "dataset_key": "tt4", "messages": [{"length": 30}], "tools": []},
        {"case_id": "ok", "run": "r2", "dataset_key": "tt4", "messages": [{"length": 4}], "tools": []},
    ]
    examples, manifest = mcp_sft.build_examples(config, None, rows, return_manifest=True)
    assert len(examples) == 1 and examples[0]["case_id"] == "ok"
    assert manifest["drops"][0]["selected_turns"] == 0


def test_balanced_epoch_covers_every_target_even_with_unequal_case_lengths():
    sources = ["tt4", "ob4", "o2", "polestar"]
    examples = []
    for source in sources:
        for case, count in [("a", 17 if source == "o2" else 2), ("b", 1)]:
            examples.extend({"source_key": source, "case_id": case} for _ in range(count))
    sampler = SourceCaseBalancedSampler(examples, source_keys=sources, group_size=4, seed=13)
    assert set(sampler) == set(range(len(examples)))
    assert len(set(sampler.audit()["draws_by_source"].values())) == 1
