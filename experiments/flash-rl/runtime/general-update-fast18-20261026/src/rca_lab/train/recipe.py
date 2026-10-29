"""Dataset-informed controls for MCP SFT preprocessing and sampling."""

from __future__ import annotations

import json
import math
import random
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

TurnSelectionPolicy = Literal["legacy_random", "tool_coverage"]
SourceSamplingPolicy = Literal["legacy", "source_case_balanced"]


def assert_finite_number(name: str, value: float) -> None:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")


def validate_source_keys(rows: list[dict[str, Any]], source_keys: list[str]) -> None:
    if not source_keys:
        raise ValueError("source_case_balanced requires explicit source_keys")
    if len(set(source_keys)) != len(source_keys):
        raise ValueError("source_keys must be unique")
    configured = set(source_keys)
    observed = {row.get("dataset_key") for row in rows}
    bad = sorted((source for source in observed if not isinstance(source, str) or source not in configured), key=repr)
    if bad:
        raise ValueError(f"dataset row has source label outside source_keys: {bad}")
    missing = sorted(configured - observed)
    if missing:
        raise ValueError(f"source_keys not present in dataset rows: {missing}")


def tool_names(message: dict[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        name = function.get("name")
        if isinstance(name, str):
            names.append(name)
    return tuple(names)


def eligible_assistant_indices(messages: list[dict[str, Any]]) -> list[int]:
    return [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "assistant" and message.get("supervise") is not False
    ]


def select_turn_indices(
    messages: list[dict[str, Any]],
    count: int,
    *,
    policy: TurnSelectionPolicy,
    seed: str,
) -> list[int]:
    eligible = eligible_assistant_indices(messages)
    if count <= 0 or len(eligible) <= count:
        return eligible
    if policy == "legacy_random":
        return _legacy_random_indices(eligible, count, seed=seed)
    if policy != "tool_coverage":
        raise ValueError(f"unknown turn_selection_policy: {policy}")
    selected: set[int] = set()
    submit_indices = [index for index in eligible if "submit_rca" in tool_names(messages[index])]
    if submit_indices:
        selected.add(submit_indices[-1])
    seen_tools: set[str] = set()
    for index in sorted(selected):
        seen_tools.update(tool_names(messages[index]))
    while len(selected) < count:
        candidates = [index for index in eligible if index not in selected]
        if not candidates:
            break
        best = max(
            candidates,
            key=lambda index: (
                len(set(tool_names(messages[index])) - seen_tools),
                -index,
            ),
        )
        selected.add(best)
        seen_tools.update(tool_names(messages[best]))
        if all(len(set(tool_names(messages[index])) - seen_tools) == 0 for index in eligible if index not in selected):
            break
    if len(selected) < count:
        rng = random.Random(seed)
        fillers = [index for index in eligible if index not in selected]
        selected.update(sorted(rng.sample(fillers, count - len(selected))))
    return sorted(selected)


def _legacy_random_indices(indices: list[int], count: int, *, seed: str) -> list[int]:
    rest = list(indices[:-1])
    picked = sorted(random.Random(seed).sample(rest, count - 1)) + [indices[-1]]
    return picked


def prompt_prefix_length(labels: list[int], ignore_index: int) -> int:
    for index, label in enumerate(labels):
        if label != ignore_index:
            return index
    raise ValueError("example has no supervised labels")


@dataclass(frozen=True)
class PreprocessStats:
    manifest: dict[str, Any]


def build_preprocess_manifest(
    *,
    config: Any,
    rows: list[dict[str, Any]],
    examples: list[dict[str, Any]],
    drop_stats: list[dict[str, Any]],
) -> PreprocessStats:
    source_counts = Counter(str(example.get("source_key")) for example in examples)
    row_counts = Counter(str(row.get("dataset_key", row.get("source", "unknown"))) for row in rows)
    repeated = Counter((example.get("source_key"), example.get("case_id"), example.get("original_message_index")) for example in examples)
    repeats_by_source: Counter[str] = Counter()
    for (source, _case, _turn), count in repeated.items():
        if count > 1 and isinstance(source, str):
            repeats_by_source[source] += count - 1
    manifest = {
        "recipe": {
            "turn_selection_policy": getattr(config, "turn_selection_policy", "legacy_random"),
            "source_sampling": getattr(config, "source_sampling", "legacy"),
            "source_keys": list(getattr(config, "source_keys", [])),
            "max_length": config.max_length,
            "max_prompt_length": getattr(config, "max_prompt_length", None),
            "turns_per_episode": getattr(config, "turns_per_episode", 0),
            "rl_turns_per_episode": getattr(config, "rl_turns_per_episode", 0),
            "final_rca_loss_weight": getattr(config, "final_rca_loss_weight", 1.0),
        },
        "rows": {
            "total": len(rows),
            "by_source": dict(sorted(row_counts.items())),
        },
        "examples": {
            "total": len(examples),
            "by_source": dict(sorted(source_counts.items())),
            "repeated_draws_by_source": dict(sorted(repeats_by_source.items())),
        },
        "selected": [
            {
                "case_id": example.get("case_id"),
                "run": example.get("run"),
                "source_key": example.get("source_key"),
                "row_index": example.get("row_index"),
                "original_message_index": example.get("original_message_index"),
                "full_tokens": len(example["input_ids"]),
                "sample_weight": example.get("sample_weight", 1.0),
                "is_final_rca": example.get("is_final_rca", False),
                "prompt_tokens": example.get("prompt_tokens"),
            }
            for example in examples
        ],
        "drops": drop_stats,
    }
    if getattr(config, "method", "sft") == "sft":
        weight_sum = sum(example.get("sample_weight", 1.0) for example in examples)
        final_weight = sum(
            example.get("sample_weight", 1.0)
            for example in examples if example.get("is_final_rca", False)
        )
        manifest["loss_weight_audit"] = {
            "contract": "whole submit_rca turn CE; includes its reasoning; not mechanism-only tokens",
            "sample_weight_sum": weight_sum,
            "final_rca_turns": sum(bool(e.get("is_final_rca")) for e in examples),
            "final_rca_weight_sum": final_weight,
            "final_rca_weight_fraction": final_weight / weight_sum if weight_sum else 0.0,
        }
    return PreprocessStats(manifest=manifest)


def write_preprocess_manifest(output_dir: str, manifest: dict[str, Any]) -> Path:
    path = Path(output_dir) / "preprocess_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class SourceCaseBalancedSampler:
    """Deterministic source-balanced sample order for batch1 x grad-accum source groups."""

    def __init__(self, examples: list[dict[str, Any]], *, source_keys: list[str], group_size: int, seed: int = 0) -> None:
        if group_size != len(source_keys):
            raise ValueError("source_case_balanced requires gradient_accumulation_steps == len(source_keys)")
        buckets: dict[str, dict[str, list[int]]] = {source: defaultdict(list) for source in source_keys}
        for index, example in enumerate(examples):
            source = example.get("source_key")
            case_id = example.get("case_id")
            if not isinstance(source, str) or source not in buckets:
                raise ValueError(f"example {index} source_key is not configured: {source!r}")
            if not isinstance(case_id, str):
                raise TypeError(f"example {index} lacks string case_id")
            buckets[source][case_id].append(index)
        missing = [source for source, cases in buckets.items() if not cases]
        if missing:
            raise ValueError(f"no examples for source_keys: {missing}")
        self.source_keys = list(source_keys)
        self.buckets = {
            source: {case_id: sorted(indices) for case_id, indices in sorted(cases.items())}
            for source, cases in buckets.items()
        }
        self.index_to_source = {
            index: source
            for source, cases in self.buckets.items()
            for indices in cases.values()
            for index in indices
        }
        self.seed = seed
        # Complete every case's turn cycle at least once; an epoch must not
        # silently omit targets from a larger source when source draws are equal.
        draws = max(len(cases) * max(map(len, cases.values())) for cases in self.buckets.values())
        self.length = draws * group_size

    def __len__(self) -> int:
        return self.length

    def __iter__(self) -> Iterator[int]:
        rng = random.Random(self.seed)
        source_offsets = {source: rng.randrange(10_000_000) for source in self.source_keys}
        draws = self.length // len(self.source_keys)
        for draw in range(draws):
            for source in self.source_keys:
                cases = self.buckets[source]
                case_ids = list(cases)
                case_id = case_ids[(draw + source_offsets[source]) % len(case_ids)]
                indices = cases[case_id]
                yield indices[(draw // len(case_ids)) % len(indices)]

    def audit(self) -> dict[str, Any]:
        ordered = list(iter(self))
        by_source = Counter()
        unique_by_source: dict[str, set[int]] = {source: set() for source in self.source_keys}
        for index in ordered:
            source = self.index_to_source[index]
            by_source[source] += 1
            unique_by_source[source].add(index)
        return {
            "length": len(ordered),
            "draws_by_source": dict(sorted(by_source.items())),
            "unique_draws_by_source": {source: len(indices) for source, indices in sorted(unique_by_source.items())},
            "repeated_draws_by_source": {
                source: by_source[source] - len(unique_by_source[source]) for source in self.source_keys
            },
        }
