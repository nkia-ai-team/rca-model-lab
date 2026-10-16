"""LoRA SFT on rca-mcp agent episodes (student message format, native Muse tool channels).

Dataset rows (JSONL, produced by scripts/mcp_export_sft.py):
  {"case_id", "run", "source", "messages": [...], "tools": [...]}
`messages` is exactly what the student saw and emitted (system, user seed, assistant with
reasoning_content + tool_calls, tool results, final submit_rca). The tokenizer chat template
renders reasoning as `<|start|>assistant to=self<|message|>…<|eom|>` and every call as an ATEM
block, i.e. the same wire format the guided rollout grammar enforces. Loss is taken only on
assistant tokens; the closing end-of-message token of each assistant span is excluded because
the template writes `<|eot|>` before a tool result while rollouts are forced to `<|eom|>`.
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from rca_lab.mcp.context import rolling_view
from rca_lab.mcp.muse_grammar import MUSE_START_TOKEN_ID
from rca_lab.train.checkpoint import (
    TrainingCheckpointContract,
    file_sha256,
    load_training_model,
    load_training_tokenizer,
    save_training_tokenizer_artifacts,
    verify_training_checkpoint,
)
from rca_lab.train.precision import build_fp32_master_adamw, configure_lora_precision
from rca_lab.train.recipe import (
    SourceCaseBalancedSampler,
    SourceSamplingPolicy,
    TurnSelectionPolicy,
    assert_finite_number,
    build_preprocess_manifest,
    prompt_prefix_length,
    select_turn_indices,
    tool_names,
    validate_source_keys,
    write_preprocess_manifest,
)
from rca_lab.train.tracking import init_wandb_run, require_wandb

IGNORE_INDEX = -100


class LoraSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rank: int = 8
    alpha: int = 16
    dropout: float = 0.05
    target_modules: str | list[str] = "all-linear"
    target_parameters: list[str] | None = None
    freeze_a: bool = False  # LoRA-FA: train only B. A is kaiming-small (~0.012/entry) so per-coordinate optimizer steps of 1e-5..1e-4 move it 0.1..1%/step; measured: freezing A delays the descent-path reversal 2-3x


    @model_validator(mode="after")
    def validate_parameter_adapters(self) -> LoraSpec:
        if self.target_parameters and self.dropout != 0:
            raise ValueError("PEFT target_parameters adapters require dropout=0")
        return self


class WandbSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: str
    project: str
    version: str


class MCPSFTConfig(TrainingCheckpointContract):
    name: str
    dataset: str
    dataset_sha256: str = ""
    expected_examples: int = Field(ge=1)
    expected_cases: int = Field(ge=1)
    output_dir: str
    max_length: int = 49152
    epochs: int = 3
    max_steps: int = 0  # >0 overrides epochs: the schedule (warmup+cosine) spans exactly this many optimizer steps. (The earlier "optimum at ~1e-3 cumulative displacement" was an artifact of the activation-fake-quant gradient cut fixed on 2026-10-02.)
    learning_rate: float = 5e-5
    lr_scheduler_type: str = "cosine"
    warmup_ratio: float = 0.03
    max_grad_norm: float = 1.0
    batch_size: int = 1
    gradient_accumulation_steps: int = 1
    save_total_limit: int = 8
    save_steps: int = 0  # 0 = save per epoch; otherwise every N optimizer steps (early validation signal)
    chat_template_kwargs: dict[str, Any] = Field(default_factory=dict)
    reasoning_strength: str | None = None  # None = template default, which is what rollouts used
    case_balanced_loss: bool = True
    reasoning_loss_weight: float = 1.0  # per-token weight of the to=self reasoning span (call tokens stay 1.0); <1 damps off-policy reasoning text (e.g. Claude-written rationales: student NLL 1.9 vs 0.4 on its own)
    # Relative weight of the whole submit_rca turn, normalized to preserve case mass.
    final_rca_loss_weight: float = Field(default=1.0, gt=0)
    use_liger_kernel: bool = True
    supervise_end_token: bool = False
    sequence_mode: Literal["turns", "episode"] = "turns"
    lora: LoraSpec = LoraSpec()
    wandb: WandbSpec
    method: Literal["sft", "rl"] = "sft"
    # method="rl" (on-policy GRPO, scripts/mcp_rl_build.py): each row carries a group-normalized
    # `advantage`; the per-example loss is advantage x assistant-token CE (advantage < 0 lowers the
    # likelihood of a below-group-mean rollout). One pass over freshly collected on-policy rollouts,
    # so the importance ratio is 1 and no clipping is needed. Case balancing does not apply.
    init_adapter: str = ""  # continue from an existing LoRA adapter (the SFT policy, or the previous RL iteration)
    rl_negative_scale: float = 1.0  # multiplier on negative advantages (<1 damps "push down" updates)
    # Train on a subset of each episode's turns: the final (submit) turn plus a seeded random sample of the
    # rest, N in total (0 = all). Each turn re-encodes its whole prefix, so a ~35-turn episode costs ~35
    # long forwards; measured: 28 episodes = 1300 turn examples = 8.4 h on one H200. Sampling turns keeps
    # the policy-gradient direction (Monte Carlo over turns) at ~N/35 of the cost.
    rl_turns_per_episode: int = 0
    turns_per_episode: int = 0  # SFT: >0 keeps the final turn + a seeded sample of the others per episode (large public sets: ~18 turns/episode at ~25 s/turn)
    turn_selection_policy: TurnSelectionPolicy = "legacy_random"
    source_sampling: SourceSamplingPolicy = "legacy"
    source_keys: list[str] = Field(default_factory=list)
    source_balance_seed: int = 0
    max_prompt_length: int | None = Field(default=None, ge=1)
    preprocess_only: bool = False

    @model_validator(mode="after")
    def validate_recipe_config(self) -> MCPSFTConfig:
        forbidden_template_keys = {"messages", "tools", "tokenize", "add_generation_prompt"} & self.chat_template_kwargs.keys()
        if forbidden_template_keys:
            raise ValueError(f"chat_template_kwargs overrides reserved fields: {sorted(forbidden_template_keys)}")
        if self.chat_template_kwargs and self.sequence_mode != "turns":
            raise ValueError("explicit chat_template_kwargs currently require sequence_mode=turns")
        for name in ("learning_rate", "warmup_ratio", "max_grad_norm", "reasoning_loss_weight", "rl_negative_scale", "final_rca_loss_weight"):
            assert_finite_number(name, getattr(self, name))
        if self.final_rca_loss_weight != 1.0:
            if self.method != "sft" or self.sequence_mode != "turns":
                raise ValueError("final_rca_loss_weight requires turn-level SFT")
            if self.batch_size != 1 or self.source_sampling != "legacy":
                raise ValueError("final_rca_loss_weight requires batch_size=1 and legacy sampling")
        if self.source_sampling == "source_case_balanced":
            if self.case_balanced_loss:
                raise ValueError("source_case_balanced uses unit sample weights; set case_balanced_loss: false")
            if self.method != "sft":
                raise ValueError("source_case_balanced is currently defined for method=sft")
            if not self.source_keys:
                raise ValueError("source_case_balanced requires explicit source_keys")
            if len(self.source_keys) != 4:
                raise ValueError("source_case_balanced requires four explicit source_keys")
            if self.batch_size != 1:
                raise ValueError("source_case_balanced requires batch_size: 1")
            if self.gradient_accumulation_steps != len(self.source_keys):
                raise ValueError("source_case_balanced requires gradient_accumulation_steps == len(source_keys)")
        elif self.source_keys:
            raise ValueError("source_keys are only used with source_sampling: source_case_balanced")
        return self


def load_config(path: Path) -> MCPSFTConfig:
    return MCPSFTConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_rows(config: MCPSFTConfig) -> list[dict[str, Any]]:
    path = Path(config.dataset)
    if config.dataset_sha256 and file_sha256(path) != config.dataset_sha256:
        raise ValueError("dataset SHA-256 does not match the training config")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != config.expected_examples:
        raise ValueError(f"dataset has {len(rows)} examples, config expects {config.expected_examples}")
    cases = {row["case_id"] for row in rows}
    if len(cases) != config.expected_cases:
        raise ValueError(f"dataset covers {len(cases)} cases, config expects {config.expected_cases}")
    for row in rows:
        if not row.get("messages") or not row.get("tools"):
            raise ValueError(f"row {row.get('case_id')}/{row.get('run')} lacks messages or tools")
        if "dataset_key" in row and not isinstance(row["dataset_key"], str):
            raise ValueError(f"row {row.get('case_id')}/{row.get('run')} has non-string dataset_key")
        if config.method == "rl" and not isinstance(row.get("advantage"), (int, float)):
            raise ValueError(f"row {row.get('case_id')}/{row.get('run')} lacks a numeric advantage (method=rl)")
    if config.method == "rl" and config.case_balanced_loss:
        raise ValueError("method=rl uses group-normalized advantages as example weights; set case_balanced_loss: false")
    if config.source_sampling == "source_case_balanced":
        validate_source_keys(rows, config.source_keys)
    return rows


def sample_turns(turns: list[Any], count: int, *, seed: str) -> list[Any]:
    """Final turn (the conclusion the reward scores) + a seeded sample of the others, original order kept."""
    if count <= 0 or len(turns) <= count:
        return list(turns)
    import random

    rest = list(range(len(turns) - 1))
    picked = sorted(random.Random(seed).sample(rest, count - 1)) + [len(turns) - 1]
    return [turns[index] for index in picked]


def rl_example_weight(advantage: float, negative_scale: float) -> float:
    """Example weight for the RL objective: the advantage itself, negatives optionally damped."""
    return float(advantage) * (negative_scale if advantage < 0 else 1.0)


def _template_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chat-template view: tool-call arguments must be dicts; nothing else changes."""
    prepared: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        item.pop("supervise", None)  # export-side flag (see tokenize_turns); not part of the template
        if item.get("tool_calls"):
            calls = []
            for call in item["tool_calls"]:
                function = dict(call["function"])
                if isinstance(function.get("arguments"), str):
                    function["arguments"] = json.loads(function["arguments"] or "{}")
                calls.append({**call, "function": function})
            item["tool_calls"] = calls
        prepared.append(item)
    return prepared


def _render_ids(tokenizer: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]], reasoning_strength: str | None, chat_template_kwargs: dict[str, Any] | None = None) -> list[int]:
    kwargs: dict[str, Any] = {**(chat_template_kwargs or {}), "tokenize": True, "tools": tools}
    if reasoning_strength:
        kwargs["reasoning_strength"] = reasoning_strength
    encoded = tokenizer.apply_chat_template(messages, **kwargs)
    return list(encoded["input_ids"] if hasattr(encoded, "keys") else encoded)


def tokenize_episode(
    tokenizer: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    reasoning_strength: str | None = None,
    supervise_end_token: bool = False,
) -> dict[str, list[int]]:
    """Return input_ids and assistant-only labels for one episode.

    Spans are located by rendering growing prefixes of the conversation: tokens between the
    render of messages[:k] and messages[:k+1] belong to assistant turn k. The template's
    end-of-message token depends on the following message, so the final token of each span is
    checked loosely and dropped from supervision unless `supervise_end_token` is set.
    """
    prepared = _template_messages(messages)
    full = _render_ids(tokenizer, prepared, tools, reasoning_strength)
    labels = [IGNORE_INDEX] * len(full)
    supervised = 0
    for index, message in enumerate(prepared):
        if message.get("role") != "assistant":
            continue
        before = _render_ids(tokenizer, prepared[:index], tools, reasoning_strength)
        through = _render_ids(tokenizer, prepared[: index + 1], tools, reasoning_strength)
        if full[: len(before)] != before or full[len(before) : len(through) - 1] != through[len(before) : -1]:
            raise ValueError(f"chat template is not prefix-stable at assistant turn {index}")
        end = len(through) if supervise_end_token else len(through) - 1
        for position in range(len(before), end):
            labels[position] = full[position]
            supervised += 1
    if supervised == 0:
        raise ValueError("episode has no supervised assistant tokens")
    return {"input_ids": full, "labels": labels}


def _strip_prior_reasoning(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Context view used at rollout time: the canonical rolling observation view (recent tool
    results in full, older ones truncated, no earlier reasoning_content)."""
    return rolling_view(messages)


def _gemma_reasoning_body(tokenizer: Any, suffix: list[int], message: dict[str, Any]) -> tuple[int, int] | None:
    """Resolve Gemma's rendered thought body; never use Muse IDs on this template.

    Return None for other tokenizers. A recognized but incomplete Gemma schema
    fails closed. Structural channel/action tokens always retain full weight.
    """
    names = ("<|channel>", "<channel|>", "<|tool_call>", "<tool_call|>")
    convert = getattr(tokenizer, "convert_tokens_to_ids", None)
    ids = [convert(name) if convert else None for name in names]
    known = [isinstance(value, int) and value >= 0 and value != getattr(tokenizer, "unk_token_id", None) for value in ids]
    template = str(getattr(tokenizer, "chat_template", ""))
    if not any(known) and not any(name in template for name in names):
        return None
    if not all(known) or len(set(ids)) != len(ids):
        raise ValueError("Gemma reasoning weighting requires distinct known channel/action markers")
    channel, channel_end, call, call_end = ids
    calls = message.get("tool_calls") or []
    reasoning = message.get("reasoning") or message.get("reasoning_content")
    body_start = body_end = 0
    if reasoning:
        # Match the actual template header, not words inside the reasoning text.
        header = list(tokenizer.encode("<|channel>thought\n", add_special_tokens=False))
        if not header or header[0] != channel or suffix[:len(header)] != header:
            raise ValueError("Gemma thought header does not match the rendered completion")
        if suffix.count(channel) != 1 or suffix.count(channel_end) != 1:
            raise ValueError("Gemma thought channel must have exactly one opening and closing marker")
        body_start, body_end = len(header), suffix.index(channel_end)
        if body_end <= body_start or any(token in (call, call_end) for token in suffix[body_start:body_end]):
            raise ValueError("Gemma thought body has invalid action boundaries")
        action = suffix[body_end + 1:]
    else:
        if channel in suffix or channel_end in suffix:
            raise ValueError("Gemma rendered thought channel has no source reasoning")
        action = suffix
    if calls:
        boundaries = [token for token in action if token in (call, call_end)]
        if not action or action[0] != call or boundaries != [call, call_end] * len(calls) or action[-1] != call_end:
            raise ValueError("Gemma tool-call boundaries do not match the source calls")
    elif reasoning:
        raise ValueError("Gemma thought supervision requires a rendered tool action")
    return body_start, body_end


def tokenize_turns(
    tokenizer: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    reasoning_strength: str | None = None,
    supervise_end_token: bool = False,
    reasoning_weight: float = 1.0,
    call_header_id: int | None = None,
    selected_message_indices: set[int] | None = None,
    chat_template_kwargs: dict[str, Any] | None = None,
) -> list[dict[str, list[int]]]:
    """One training example per assistant turn, with the exact rollout context.

    The student loop never sends earlier reasoning back to the server, so each turn's prompt is
    the conversation so far with prior reasoning removed plus the generation header; the
    completion is that turn's reasoning + tool call. This mirrors what the model conditions on
    at inference (unlike a single full-episode sequence, which would interleave all reasoning).
    A turn marked `"supervise": false` (its reasoning failed the judge) stays in the context of
    later turns but yields no example of its own. Non-unit reasoning weighting uses Gemma's
    typed thought-body boundaries when present, retaining full channel/action boundary weight.
    Other tokenizers preserve the Muse contract: completion tokens before `call_header_id`
    receive the reasoning weight, with the call span at 1.0.
    """
    prepared = _template_messages(messages)
    examples: list[dict[str, list[int]]] = []
    for index, message in enumerate(prepared):
        if message.get("role") != "assistant" or messages[index].get("supervise") is False:
            continue
        if selected_message_indices is not None and index not in selected_message_indices:
            continue
        context = _strip_prior_reasoning(prepared[:index])
        prompt_kwargs: dict[str, Any] = {**(chat_template_kwargs or {}), "tokenize": True, "tools": tools, "add_generation_prompt": True}
        if reasoning_strength:
            prompt_kwargs["reasoning_strength"] = reasoning_strength
        prompt = tokenizer.apply_chat_template(context, **prompt_kwargs)
        prompt = list(prompt["input_ids"] if hasattr(prompt, "keys") else prompt)
        full = _render_ids(tokenizer, [*context, message], tools, reasoning_strength, chat_template_kwargs)
        # Gemma templates append a synthetic tool-response opener: environment output, not an action.
        if message.get("tool_calls") and hasattr(tokenizer, "convert_tokens_to_ids"):
            response_id = tokenizer.convert_tokens_to_ids("<|tool_response>")
            if response_id is not None and response_id != getattr(tokenizer, "unk_token_id", None) and full[-1:] == [response_id]:
                full = full[:-1]
        if full[: len(prompt)] != prompt or len(full) <= len(prompt):
            raise ValueError(f"generation prompt is not a prefix of the rendered assistant turn {index}")
        end = len(full) if supervise_end_token else len(full) - 1
        labels = [IGNORE_INDEX] * len(prompt) + full[len(prompt) : end] + ([IGNORE_INDEX] * (len(full) - end))
        token_weight = [1.0] * len(full)
        if reasoning_weight != 1.0:
            body = _gemma_reasoning_body(tokenizer, full[len(prompt):], message)
            if body is not None:
                for position in range(len(prompt) + body[0], len(prompt) + body[1]):
                    token_weight[position] = reasoning_weight
            elif call_header_id is not None:
                header = next((position for position in range(len(prompt), len(full)) if full[position] == call_header_id), len(full))
                for position in range(len(prompt), header):
                    token_weight[position] = reasoning_weight
        examples.append({"input_ids": full, "labels": labels, "token_weight": token_weight, "original_message_index": index, "prompt_tokens": len(prompt)})
    if not examples:
        raise ValueError("episode has no assistant turns")
    return examples


def case_weights(rows: list[dict[str, Any]], *, balanced: bool) -> list[float]:
    """Per-example weights so every case contributes equally per epoch (mean weight 1)."""
    if not balanced:
        return [1.0] * len(rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["case_id"]] = counts.get(row["case_id"], 0) + 1
    per_case = len(rows) / len(counts)
    return [per_case / counts[row["case_id"]] for row in rows]


def build_examples(config: MCPSFTConfig, tokenizer: Any, rows: list[dict[str, Any]], *, return_manifest: bool = False) -> Any:
    """Turn-level examples; a case's total weight is shared across all its episodes' turns."""
    per_episode: list[list[dict[str, list[int]]]] = []
    kept_rows: list[dict[str, Any]] = []
    drop_stats: list[dict[str, Any]] = []
    turn_selection_policy = getattr(config, "turn_selection_policy", "legacy_random")
    max_prompt_length = getattr(config, "max_prompt_length", None)
    source_sampling = getattr(config, "source_sampling", "legacy")
    for row_index, row in enumerate(rows):
        selected_indices: set[int] | None = None
        per_episode_cap = getattr(config, "rl_turns_per_episode", 0) if config.method == "rl" else getattr(config, "turns_per_episode", 0)
        if config.sequence_mode == "turns" and turn_selection_policy == "tool_coverage" and per_episode_cap > 0:
            selected_indices = set(select_turn_indices(row["messages"], per_episode_cap, policy="tool_coverage", seed=f"{row['case_id']}/{row['run']}"))
        if config.sequence_mode == "episode":
            turns = [tokenize_episode(tokenizer, row["messages"], row["tools"], reasoning_strength=config.reasoning_strength, supervise_end_token=config.supervise_end_token)]
        else:
            turns = tokenize_turns(tokenizer, row["messages"], row["tools"], reasoning_strength=config.reasoning_strength, supervise_end_token=config.supervise_end_token, reasoning_weight=config.reasoning_loss_weight, call_header_id=MUSE_START_TOKEN_ID, selected_message_indices=selected_indices, chat_template_kwargs=getattr(config, "chat_template_kwargs", None))
        for turn in turns:
            turn["prompt_tokens"] = turn.get("prompt_tokens", prompt_prefix_length(turn["labels"], IGNORE_INDEX) if "labels" in turn else 0)
        kept = [
            turn
            for turn in turns
            if len(turn["input_ids"]) <= config.max_length
            and (max_prompt_length is None or turn["prompt_tokens"] <= max_prompt_length)
        ]
        length_dropped = len(turns) - len(kept)
        before_sampling = len(kept)
        if per_episode_cap > 0 and turn_selection_policy == "legacy_random":
            kept = sample_turns(kept, per_episode_cap, seed=f"{row['case_id']}/{row['run']}")
        if length_dropped:
            print(f"[mcp_sft] {row['case_id']}/{row['run']}: dropped {length_dropped} turn(s) over max_length {config.max_length}", flush=True)
        if before_sampling > len(kept):
            print(f"[mcp_sft] {row['case_id']}/{row['run']}: sampled {len(kept)} of {before_sampling} eligible turns", flush=True)
        drop_stats.append({
            "case_id": row["case_id"],
            "run": row.get("run"),
            "source_key": row.get("dataset_key"),
            "row_index": row_index,
            "tokenized_turns": len(turns),
            "selected_turns": len(kept),
            "dropped_by_length": length_dropped,
            "sampled_from": before_sampling,
        })
        if not kept:
            if source_sampling == "source_case_balanced":
                print(f"[mcp_sft] {row['case_id']}/{row['run']}: no selected turn survived length filters; continuing for source-balanced recipe", flush=True)
                continue
            raise ValueError(f"{row['case_id']}/{row['run']} has no turn within max_length {config.max_length}")
        for turn in kept:
            turn["row_index"] = row_index
            turn["run"] = row.get("run")
            turn["source_key"] = row.get("dataset_key", row.get("source", "unknown"))
            message_index = turn.get("original_message_index")
            turn["is_final_rca"] = (
                isinstance(message_index, int)
                and "submit_rca" in tool_names(row["messages"][message_index])
            )
        per_episode.append(kept)
        kept_rows.append(row)
    case_turns: dict[str, int] = {}
    for row, turns in zip(kept_rows, per_episode, strict=True):
        case_turns[row["case_id"]] = case_turns.get(row["case_id"], 0) + len(turns)
    if not case_turns:
        raise ValueError("no training turns survived length filters")
    total = sum(case_turns.values())
    final_multiplier = getattr(config, "final_rca_loss_weight", 1.0)
    case_mass: dict[str, float] = {}
    for row, turns in zip(kept_rows, per_episode, strict=True):
        case_mass[row["case_id"]] = case_mass.get(row["case_id"], 0.0) + sum(
            final_multiplier if turn["is_final_rca"] else 1.0 for turn in turns
        )
    examples = []
    for row, turns in zip(kept_rows, per_episode, strict=True):
        if config.method == "rl":
            weight = rl_example_weight(row["advantage"], config.rl_negative_scale)
        else:
            # Normalize AFTER final-turn upweighting. Otherwise cases with more submit turns
            # gain unintended weight and increasing the multiplier also changes the LR scale.
            weight = (
                (total / len(case_turns)) / case_mass[row["case_id"]]
                if config.case_balanced_loss else total / sum(case_mass.values())
            )
        examples.extend({
            **turn,
            "sample_weight": weight * (
                final_multiplier if config.method == "sft" and turn["is_final_rca"] else 1.0
            ),
            "case_id": row["case_id"],
        } for turn in turns)
    manifest = build_preprocess_manifest(config=config, rows=rows, examples=examples, drop_stats=drop_stats).manifest
    return (examples, manifest) if return_manifest else examples


def weighted_assistant_ce(per_token: Any, span_weight: Any, sample_weight: Any) -> tuple[Any, Any]:
    """Batch-one weighted CE; sample weight scales gradients without cancelling in denominator."""
    weighted_ce = (per_token * span_weight).sum() / span_weight.sum().clamp_min(1e-6)
    return weighted_ce * sample_weight.mean().to(weighted_ce.device), weighted_ce


def train(config_path: Path, *, resume_from_checkpoint: Path | None = None) -> None:  # pragma: no cover - GPU entrypoint
    config = load_config(config_path)
    rows = load_rows(config)
    tokenizer = load_training_tokenizer(config)
    examples, preprocess_manifest = build_examples(config, tokenizer, rows, return_manifest=True)
    if config.source_sampling == "source_case_balanced":
        sampler_audit = SourceCaseBalancedSampler(examples, source_keys=config.source_keys, group_size=config.gradient_accumulation_steps, seed=config.source_balance_seed).audit()
        preprocess_manifest["sampler"] = sampler_audit
    manifest_path = write_preprocess_manifest(config.output_dir, preprocess_manifest)
    print(f"[mcp_sft] wrote preprocessing manifest {manifest_path}", flush=True)
    if config.preprocess_only:
        print("[mcp_sft] preprocess_only: stopping before wandb/model load", flush=True)
        return
    verify_training_checkpoint(config)
    wandb = require_wandb()

    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import Trainer, TrainingArguments
    lengths = [len(example["input_ids"]) for example in examples]
    supervised = [sum(label != IGNORE_INDEX for label in example["labels"]) for example in examples]
    pad_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id

    class EpisodeDataset(torch.utils.data.Dataset):
        def __len__(self) -> int:
            return len(examples)

        def __getitem__(self, index: int) -> dict[str, Any]:
            return examples[index]

    def collate(batch: list[dict[str, Any]]) -> dict[str, torch.Tensor]:
        width = max(len(item["input_ids"]) for item in batch)
        input_ids = torch.full((len(batch), width), pad_id, dtype=torch.long)
        labels = torch.full((len(batch), width), IGNORE_INDEX, dtype=torch.long)
        attention = torch.zeros((len(batch), width), dtype=torch.long)
        token_weight = torch.zeros((len(batch), width), dtype=torch.float32)
        for row_index, item in enumerate(batch):
            length = len(item["input_ids"])
            input_ids[row_index, :length] = torch.tensor(item["input_ids"], dtype=torch.long)
            labels[row_index, :length] = torch.tensor(item["labels"], dtype=torch.long)
            attention[row_index, :length] = 1
            token_weight[row_index, :length] = torch.tensor(item.get("token_weight") or [1.0] * length, dtype=torch.float32)
        return {"input_ids": input_ids, "labels": labels, "attention_mask": attention, "token_weight": token_weight, "sample_weight": torch.tensor([item["sample_weight"] for item in batch], dtype=torch.float32)}

    class WeightedTrainer(Trainer):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            # compute_loss below returns a per-micro-batch weighted MEAN and ignores
            # num_items_in_batch. Muse's forward takes **kwargs, so the Trainer would otherwise
            # assume a sum/num_items loss and skip the division by gradient_accumulation_steps —
            # summing 4 means, i.e. a 4x gradient (reported grad_norm 4x, max_grad_norm 0.5
            # acting as 0.125). Runs v1..v1f trained with that scale.
            self.model_accepts_loss_kwargs = False

        def _get_train_sampler(self, train_dataset: Any | None = None) -> Any:
            if config.source_sampling != "source_case_balanced":
                try:
                    return super()._get_train_sampler(train_dataset)
                except TypeError:
                    return super()._get_train_sampler()
            return SourceCaseBalancedSampler(examples, source_keys=config.source_keys, group_size=config.gradient_accumulation_steps, seed=config.source_balance_seed)

        def compute_loss(self, model: Any, inputs: dict[str, Any], return_outputs: bool = False, num_items_in_batch: int | None = None) -> Any:
            """Exact assistant-token CE without materializing full-sequence logits.

            Muse has a 262k vocabulary: a 30k-token turn would need ~31 GB of fp32 logits, which
            is what OOMed the first launch. Run the decoder, keep only the hidden states at
            supervised positions, and project those alone (fp32, with the model's logit softcap).
            """
            from torch.nn import functional

            weight = inputs.pop("sample_weight")
            token_weight = inputs.pop("token_weight")
            base_model = model
            while hasattr(base_model, "module"):
                base_model = base_model.module
            base_model = base_model.get_base_model() if hasattr(base_model, "get_base_model") else base_model
            outputs = base_model.model(input_ids=inputs["input_ids"], attention_mask=inputs["attention_mask"], use_cache=False)
            shifted_labels = inputs["labels"][:, 1:]
            selected = shifted_labels != IGNORE_INDEX
            if not bool(selected.any()):
                raise ValueError("training example has no shifted assistant target tokens")
            shifted_hidden = outputs.last_hidden_state[:, :-1, :]
            selected = selected.to(shifted_hidden.device)
            hidden = shifted_hidden[selected]
            targets = shifted_labels.to(shifted_hidden.device)[selected]
            text_config = getattr(base_model.config, "text_config", base_model.config)
            hidden = hidden * float(getattr(text_config, "output_multiplier", 1.0))
            output_weight = base_model.lm_head.weight
            logits = functional.linear(hidden.to(output_weight.device).float(), output_weight.float())
            softcap = float(getattr(text_config, "final_logit_softcapping", 0.0) or 0.0)
            if softcap > 0:
                logits = torch.tanh(logits / softcap) * softcap
            per_token = functional.cross_entropy(logits, targets.to(logits.device), reduction="none")
            token_ce = per_token.mean()
            if not bool(torch.isfinite(token_ce)):
                raise FloatingPointError("assistant-token cross entropy is non-finite")
            # Reasoning-span damping: weighted mean over supervised tokens (weights 1.0 on the call
            # span, `reasoning_loss_weight` on the reasoning span), so the objective is dominated by
            # the action tokens when the reasoning text is off-policy.
            span_weight = token_weight[:, 1:].to(per_token.device)[selected.to(per_token.device)]
            loss, weighted_ce = weighted_assistant_ce(per_token, span_weight, weight)
            if self.state.global_step % max(1, self.args.logging_steps) == 0:
                is_call = span_weight >= 1.0
                log = {"objective/token_ce": float(token_ce.detach().item()), "objective/weighted_ce": float(weighted_ce.detach().item()), "objective/supervised_tokens": float(targets.numel())}
                if config.method == "rl":
                    log["rl/advantage"] = float(weight.mean().item())
                if bool(is_call.any()) and bool((~is_call).any()):
                    log["objective/call_ce"] = float(per_token[is_call].mean().detach().item())
                    log["objective/reasoning_ce"] = float(per_token[~is_call].mean().detach().item())
                self.log(log)
            return (loss, {"loss": loss}) if return_outputs else loss

    model = load_training_model(config, torch_module=torch, attention_implementation=os.environ.get("ATTN", "sdpa"))
    model.config.use_cache = False
    if config.init_adapter:
        from peft import PeftModel

        model = PeftModel.from_pretrained(model, config.init_adapter, is_trainable=True)
        print(f"[mcp_sft] continuing from adapter {config.init_adapter}", flush=True)
    else:
        model = get_peft_model(model, LoraConfig(r=config.lora.rank, lora_alpha=config.lora.alpha, lora_dropout=config.lora.dropout, target_modules=config.lora.target_modules, target_parameters=config.lora.target_parameters, task_type="CAUSAL_LM"))
    if config.lora.freeze_a:
        frozen = 0
        for name, parameter in model.named_parameters():
            if "lora_A" in name:
                parameter.requires_grad_(False)
                frozen += 1
        print(f"[mcp_sft] LoRA-FA: froze {frozen} lora_A tensors", flush=True)
    precision = configure_lora_precision(model, torch_module=torch, adapter_parameter_dtype=config.adapter_parameter_dtype)
    optimizer = build_fp32_master_adamw(model.parameters(), torch_module=torch, lr=config.learning_rate, weight_decay=0.0)

    init_wandb_run(
        wandb,
        entity=config.wandb.entity,
        project=config.wandb.project,
        method=config.method,
        model_name=config.model_name,
        version=config.wandb.version,
        config={
            **config.model_dump(),
            "examples": len(examples),
            "episodes": len(rows),
            "sequence_mode": config.sequence_mode,
            "cases": len({example["case_id"] for example in examples}),
            "min_tokens": min(lengths),
            "max_tokens": max(lengths),
            "mean_supervised_tokens": sum(supervised) / len(supervised),
            "precision_contract": precision,
            "loss_contract": ("on-policy GRPO: per-turn examples with rollout-exact context; loss = group-normalized advantage x assistant-token CE; end-of-message token excluded" if config.method == "rl" else "per-turn examples with rollout-exact context (prior reasoning stripped); assistant-only token CE; end-of-message token excluded; case-balanced weights"),
            "preprocess_manifest": str(manifest_path),
            "resume_from_checkpoint": str(resume_from_checkpoint) if resume_from_checkpoint else None,
        },
    )
    arguments = TrainingArguments(
        output_dir=config.output_dir,
        num_train_epochs=config.epochs,
        max_steps=config.max_steps if config.max_steps > 0 else -1,
        per_device_train_batch_size=config.batch_size,
        gradient_accumulation_steps=config.gradient_accumulation_steps,
        learning_rate=config.learning_rate,
        lr_scheduler_type=config.lr_scheduler_type,
        warmup_steps=config.warmup_ratio,  # transformers 5: float in [0, 1) is a ratio of total steps
        max_grad_norm=config.max_grad_norm,
        bf16=True,
        gradient_checkpointing=True,
        use_liger_kernel=False,  # muse_glimmer is not a liger-patched architecture; the selective loss above avoids full logits
        logging_steps=1,
        save_strategy="steps" if config.save_steps else "epoch",
        save_steps=config.save_steps or 500,
        save_total_limit=config.save_total_limit,
        report_to=["wandb"],
        remove_unused_columns=False,
    )
    trainer = WeightedTrainer(model=model, args=arguments, train_dataset=EpisodeDataset(), data_collator=collate, optimizers=(optimizer, None))
    trainer.train(resume_from_checkpoint=str(resume_from_checkpoint) if resume_from_checkpoint else None)
    adapter_dir = Path(config.output_dir) / "adapter"
    trainer.save_model(str(adapter_dir))
    save_training_tokenizer_artifacts(config, adapter_dir)
    manifest = {
        "training_global_step": int(trainer.state.global_step),
        "config": config.model_dump(mode="json"),
        "config_sha256": file_sha256(config_path),
        "dataset_sha256": file_sha256(Path(config.dataset)),
        "adapter_files_sha256": {path.name: file_sha256(path) for path in sorted(adapter_dir.iterdir()) if path.is_file()},
        "runtime": {
            "python": platform.python_version(),
            "torch": torch.__version__,
            "transformers": __import__("transformers").__version__,
            "peft": __import__("peft").__version__,
            "precision": precision,
        },
    }
    (adapter_dir / "training_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    wandb.finish()
