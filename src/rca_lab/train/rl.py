"""Episode-preserving DAPO/GRPO-style LoRA optimization for RCA actions."""

from __future__ import annotations

import json
import math
import os
import random
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator

from rca_lab.data.sft import SFTMessage
from rca_lab.harness.models import StrictModel
from rca_lab.train.sft import LoRAConfig, WandbConfig, mask_assistant_spans


class EpisodeRLConfig(StrictModel):
    name: str
    model_name: str
    dataset: str
    output_dir: str
    max_length: int = Field(ge=1)
    epochs: int = Field(ge=1)
    learning_rate: float = Field(gt=0)
    gradient_accumulation_steps: int = Field(ge=1)
    clip_low: float = Field(gt=0, lt=1)
    clip_high: float = Field(gt=0, lt=1)
    kl_beta: float = Field(ge=0)
    max_grad_norm: float = Field(gt=0)
    seed: int = 42
    behavior_temperature: Literal[1.0] = 1.0
    algorithm: Literal["episode_dapo_lora"] = "episode_dapo_lora"
    lora: LoRAConfig
    wandb: WandbConfig

    @model_validator(mode="after")
    def asymmetric_clip_contract(self) -> EpisodeRLConfig:
        if self.clip_high < self.clip_low:
            raise ValueError("clip_high must be at least clip_low")
        return self


class RLTurn(StrictModel):
    messages: tuple[SFTMessage, SFTMessage, SFTMessage]


class RLEpisodeRecord(StrictModel):
    scenario_id: str = Field(min_length=1)
    rollout_id: str = Field(min_length=1)
    reward: float = Field(ge=0.0, le=1.0)
    advantage: float
    score: dict[str, Any]
    turns: tuple[RLTurn, ...] = Field(min_length=1)


def load_rl_config(path: Path) -> EpisodeRLConfig:
    return EpisodeRLConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def clipped_surrogate(
    log_ratio: Any, advantage: Any, *, clip_low: float, clip_high: float
) -> Any:
    """Return the tokenwise asymmetric clipped DAPO surrogate."""
    import torch

    ratio = torch.exp(log_ratio)
    clipped = torch.clamp(ratio, 1.0 - clip_low, 1.0 + clip_high)
    return torch.minimum(ratio * advantage, clipped * advantage)


def _selected_logps(logits: Any, targets: Any) -> Any:
    """Compute selected-token log probabilities without materializing log_softmax."""
    import torch

    selected = torch.gather(logits.float(), -1, targets.unsqueeze(-1)).squeeze(-1)
    return selected - torch.logsumexp(logits.float(), dim=-1)


def _response_shape(labels: list[int]) -> tuple[int, int]:
    indices = [index for index, token in enumerate(labels) if token != -100]
    if not indices:
        raise ValueError("turn has no assistant response tokens")
    first, last = indices[0], indices[-1]
    if indices != list(range(first, last + 1)):
        raise ValueError("runtime action response must be one contiguous suffix")
    if last != len(labels) - 1:
        raise ValueError("runtime action response must terminate the sequence")
    if first == 0:
        raise ValueError("assistant response has no causal prefix token")
    return first, len(indices)


def _turn_loss(model: Any, turn: dict[str, Any], advantage: float, config: EpisodeRLConfig) -> Any:
    import torch

    input_ids = turn["input_ids"]
    attention_mask = turn["attention_mask"]
    first, response_tokens = _response_shape(turn["labels"][0].tolist())
    # One extra suffix logit is requested because the first response token is
    # predicted by the prefix position immediately before it.
    logits_to_keep = response_tokens + 1
    targets = input_ids[:, first:]

    disable = getattr(model, "disable_adapter", None)
    reference_context = disable() if disable is not None else nullcontext()
    # Keep gradient checkpointing active while making both old/current policy
    # likelihoods deterministic and comparable.
    model.train()
    for module in model.modules():
        if isinstance(module, torch.nn.Dropout):
            module.eval()
    with torch.no_grad(), reference_context:
        reference = model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            logits_to_keep=logits_to_keep,
            use_cache=False,
        ).logits[:, :-1]
        old_logps = _selected_logps(reference, targets).detach()
    del reference

    current = model(
        input_ids=input_ids,
        attention_mask=attention_mask,
        logits_to_keep=logits_to_keep,
        use_cache=False,
    ).logits[:, :-1]
    current_logps = _selected_logps(current, targets)
    del current
    log_ratio = current_logps - old_logps
    scalar_advantage = torch.as_tensor(advantage, device=log_ratio.device, dtype=log_ratio.dtype)
    policy = -clipped_surrogate(
        log_ratio, scalar_advantage, clip_low=config.clip_low, clip_high=config.clip_high
    ).mean()
    # K3 is non-negative and zero at the behavior policy. Here old policy is
    # the frozen merged SFT model obtained by temporarily disabling the RL adapter.
    reverse_log_ratio = -log_ratio
    kl = (torch.exp(reverse_log_ratio) - reverse_log_ratio - 1.0).mean()
    return policy + config.kl_beta * kl, policy.detach(), kl.detach(), response_tokens


def train_rl(config_path: Path) -> None:  # pragma: no cover - GPU entrypoint
    import torch
    import wandb
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoModelForImageTextToText, AutoTokenizer

    config = load_rl_config(config_path)
    random.seed(config.seed)
    torch.manual_seed(config.seed)
    rows = [
        RLEpisodeRecord.model_validate(json.loads(line)).model_dump(mode="json")
        for line in Path(config.dataset).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = [row for row in rows if math.isfinite(float(row["advantage"]))]
    if not rows:
        raise ValueError("RL dataset has no finite-advantage episodes")

    tokenizer = AutoTokenizer.from_pretrained(config.model_name)
    start_id = tokenizer.convert_tokens_to_ids("<|start|>")
    assistant_role_ids = tuple(tokenizer.encode("assistant", add_special_tokens=False))
    message_id = tokenizer.convert_tokens_to_ids("<|message|>")
    eot_id = tokenizer.convert_tokens_to_ids("<|eot|>")
    encoded: list[dict[str, Any]] = []
    for row in rows:
        turns: list[dict[str, Any]] = []
        for turn in row["turns"]:
            tokenized = tokenizer.apply_chat_template(
                turn["messages"], tokenize=True, reasoning_strength="low"
            )
            input_ids = list(tokenized["input_ids"])
            if len(input_ids) > config.max_length:
                raise ValueError(
                    f"runtime turn exceeds max_length without safe truncation: "
                    f"{row['scenario_id']}/{row['rollout_id']} tokens={len(input_ids)}"
                )
            labels = mask_assistant_spans(
                input_ids,
                start_id=start_id,
                assistant_role_ids=assistant_role_ids,
                message_id=message_id,
                eot_id=eot_id,
            )
            _response_shape(labels)
            turns.append(
                {
                    "input_ids": torch.tensor([input_ids], dtype=torch.long),
                    "attention_mask": torch.ones((1, len(input_ids)), dtype=torch.long),
                    "labels": torch.tensor([labels], dtype=torch.long),
                }
            )
        encoded.append({**row, "turns": turns})

    load_kwargs = {
        "torch_dtype": torch.bfloat16,
        "device_map": "auto",
        "attn_implementation": os.environ.get("ATTN", "sdpa"),
    }
    try:
        model = AutoModelForCausalLM.from_pretrained(config.model_name, **load_kwargs)
    except ValueError:
        model = AutoModelForImageTextToText.from_pretrained(config.model_name, **load_kwargs)
    model.config.use_cache = False
    model = get_peft_model(
        model,
        LoraConfig(
            r=config.lora.rank,
            lora_alpha=config.lora.alpha,
            lora_dropout=config.lora.dropout,
            target_modules=config.lora.target_modules,
            task_type="CAUSAL_LM",
        ),
    )
    model.enable_input_require_grads()
    model.gradient_checkpointing_enable()
    device = next(model.parameters()).device
    optimizer = torch.optim.AdamW(
        (parameter for parameter in model.parameters() if parameter.requires_grad),
        lr=config.learning_rate,
    )

    wandb_enabled = bool(os.environ.get("WANDB_API_KEY")) or Path("~/.netrc").expanduser().exists()
    if wandb_enabled:
        wandb.init(
            entity=config.wandb.entity,
            project=config.wandb.project,
            name=config.name,
            config={
                **config.model_dump(),
                "episodes": len(encoded),
                "contract": (
                    "whole episode grouped; behavior policy=frozen merged SFT; "
                    "asymmetric clipped DAPO; typed terminal reward"
                ),
            },
        )

    global_step = 0
    for epoch in range(config.epochs):
        random.shuffle(encoded)
        for offset in range(0, len(encoded), config.gradient_accumulation_steps):
            group = encoded[offset : offset + config.gradient_accumulation_steps]
            optimizer.zero_grad(set_to_none=True)
            metrics = {"loss": 0.0, "policy": 0.0, "kl": 0.0, "tokens": 0}
            for episode in group:
                token_counts = [int((turn["labels"] != -100).sum()) for turn in episode["turns"]]
                total_tokens = sum(token_counts)
                for turn, token_count in zip(episode["turns"], token_counts, strict=True):
                    gpu_turn = {key: value.to(device) for key, value in turn.items()}
                    loss, policy, kl, response_tokens = _turn_loss(
                        model, gpu_turn, float(episode["advantage"]), config
                    )
                    weight = token_count / total_tokens / len(group)
                    (loss * weight).backward()
                    metrics["loss"] += float(loss.detach()) * weight
                    metrics["policy"] += float(policy) * weight
                    metrics["kl"] += float(kl) * weight
                    metrics["tokens"] += response_tokens
            torch.nn.utils.clip_grad_norm_(model.parameters(), config.max_grad_norm)
            optimizer.step()
            global_step += 1
            record = {
                "epoch": epoch + 1,
                "step": global_step,
                "train/loss": metrics["loss"],
                "train/policy_loss": metrics["policy"],
                "train/kl": metrics["kl"],
                "train/response_tokens": metrics["tokens"],
            }
            print(json.dumps(record))
            if wandb_enabled:
                wandb.log(record, step=global_step)

    adapter_dir = Path(config.output_dir) / "adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)
    if wandb_enabled:
        wandb.finish()
