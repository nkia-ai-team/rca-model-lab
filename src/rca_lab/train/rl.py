"""Episode-preserving DAPO/GRPO-style LoRA optimization for RCA actions."""

from __future__ import annotations

import json
import math
import os
import platform
import random
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator

from rca_lab.data.sft import SFTMessage
from rca_lab.harness.models import StrictModel
from rca_lab.provenance import file_sha256, resolve_model_identity
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
    # Online GRPO snapshots the exact pre-update behavior policy and recomputes
    # its token log-probabilities. Temperature 1 therefore preserves ratio=1 at
    # initialization until rollout-time token logprobs are persisted directly.
    behavior_temperature: Literal[1.0] = 1.0
    behavior_model_sha256: str = ""
    base_model_sha256: str = ""
    initial_adapter: str = ""
    initial_adapter_sha256: str = ""
    dataset_sha256: str = ""
    algorithm: Literal[
        "episode_dapo_lora",
        "episode_anchor_dapo_lora",
        "episode_rft_replay_lora",
        "episode_progressive_dapo_lora",
        "episode_online_progressive_grpo_lora",
    ] = "episode_dapo_lora"
    lora: LoRAConfig
    wandb: WandbConfig

    @model_validator(mode="after")
    def asymmetric_clip_contract(self) -> EpisodeRLConfig:
        if self.clip_high < self.clip_low:
            raise ValueError("clip_high must be at least clip_low")
        if self.initial_adapter and not self.initial_adapter_sha256:
            raise ValueError("initial_adapter_sha256 is required for adapter continuation")
        if self.initial_adapter_sha256 and not self.initial_adapter:
            raise ValueError("initial_adapter is required with initial_adapter_sha256")
        if self.initial_adapter and self.initial_adapter_sha256 != self.behavior_model_sha256:
            raise ValueError("continued adapter must be the rollout behavior policy")
        if (
            self.algorithm == "episode_online_progressive_grpo_lora"
            and self.lora.target_modules == "all-linear"
        ):
            raise ValueError("online GRPO requires language-only LoRA target modules")
        if self.algorithm == "episode_online_progressive_grpo_lora" and not self.base_model_sha256:
            raise ValueError("online GRPO requires base_model_sha256")
        return self


class RLTurn(StrictModel):
    messages: tuple[SFTMessage, SFTMessage, SFTMessage]


class RLEpisodeRecord(StrictModel):
    scenario_id: str = Field(min_length=1)
    rollout_id: str = Field(min_length=1)
    reward: float = Field(ge=0.0, le=1.0)
    optimization_reward: float = Field(ge=0.0, le=1.0)
    advantage: float
    turn_advantages: tuple[float, ...] = ()
    behavior_model_artifact: str = ""
    behavior_model_sha256: str = ""
    base_model_artifact: str = ""
    base_model_sha256: str = ""
    behavior_temperature: float = Field(default=0.0, ge=0, le=2)
    score: dict[str, Any]
    turns: tuple[RLTurn, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def turn_credit_matches_episode(self) -> RLEpisodeRecord:
        if self.turn_advantages and len(self.turn_advantages) != len(self.turns):
            raise ValueError("turn_advantages must match turns exactly")
        return self


def load_rl_config(path: Path) -> EpisodeRLConfig:
    return EpisodeRLConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def behavior_policy_artifact(config: EpisodeRLConfig) -> str:
    return config.initial_adapter or config.model_name


def validate_behavior_policy(rows: list[dict[str, Any]], config: EpisodeRLConfig) -> None:
    if config.algorithm not in {
        "episode_progressive_dapo_lora",
        "episode_online_progressive_grpo_lora",
    }:
        return
    if any(not row["turn_advantages"] for row in rows):
        raise ValueError("progressive DAPO requires turn_advantages for every episode")
    provenance = {
        (
            row["behavior_model_artifact"],
            row["behavior_model_sha256"],
            row["behavior_temperature"],
        )
        for row in rows
    }
    expected = {
        (
            behavior_policy_artifact(config),
            config.behavior_model_sha256,
            config.behavior_temperature,
        )
    }
    if provenance != expected or not config.behavior_model_sha256:
        raise ValueError(
            f"rollout behavior policy does not match training base: "
            f"dataset={provenance} config={expected}"
        )
    if config.algorithm == "episode_online_progressive_grpo_lora":
        base_provenance = {
            (row["base_model_artifact"], row["base_model_sha256"]) for row in rows
        }
        expected_base = {(config.model_name, config.base_model_sha256)}
        if base_provenance != expected_base:
            raise ValueError(
                f"rollout base model does not match training base: "
                f"dataset={base_provenance} config={expected_base}"
            )


def validate_dataset_artifact(path: Path, config: EpisodeRLConfig) -> None:
    if config.algorithm not in {
        "episode_progressive_dapo_lora",
        "episode_online_progressive_grpo_lora",
    }:
        return
    if not config.dataset_sha256:
        raise ValueError("progressive DAPO requires dataset_sha256")
    actual = file_sha256(path)
    if actual != config.dataset_sha256:
        raise ValueError(f"RL dataset sha256 mismatch: got={actual} want={config.dataset_sha256}")


def clipped_surrogate(log_ratio: Any, advantage: Any, *, clip_low: float, clip_high: float) -> Any:
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


def _k3_reference_kl(current_logps: Any, reference_logps: Any) -> Any:
    """Return the non-negative K3 estimator against the fixed SFT reference."""
    import torch

    if reference_logps.shape != current_logps.shape:
        raise ValueError("reference log-probabilities do not match response tokens")
    reference_minus_current = reference_logps - current_logps
    return (
        torch.exp(reference_minus_current) - reference_minus_current - 1.0
    ).mean()


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


def _policy_logps(model: Any, turn: dict[str, Any]) -> Any:
    """Return selected response-token log-probabilities for the current policy."""
    import torch

    input_ids = turn["input_ids"]
    attention_mask = turn["attention_mask"]
    first, response_tokens = _response_shape(turn["labels"][0].tolist())
    logits_to_keep = response_tokens + 1
    targets = input_ids[:, first:]
    model.train()
    for module in model.modules():
        if isinstance(module, torch.nn.Dropout):
            module.eval()
    logits = model(
        input_ids=input_ids,
        attention_mask=attention_mask,
        logits_to_keep=logits_to_keep,
        use_cache=False,
    ).logits[:, :-1]
    selected = _selected_logps(logits, targets)
    del logits
    return selected


def _turn_loss(
    model: Any,
    turn: dict[str, Any],
    old_logps: Any,
    reference_logps: Any,
    advantage: float,
    config: EpisodeRLConfig,
) -> Any:
    import torch

    current_logps = _policy_logps(model, turn)
    response_tokens = int(current_logps.shape[-1])
    old_logps = old_logps.to(device=current_logps.device, dtype=current_logps.dtype)
    if old_logps.shape != current_logps.shape:
        raise ValueError("frozen behavior log-probabilities do not match response tokens")
    reference_logps = reference_logps.to(
        device=current_logps.device, dtype=current_logps.dtype
    )
    log_ratio = current_logps - old_logps
    scalar_advantage = torch.as_tensor(advantage, device=log_ratio.device, dtype=log_ratio.dtype)
    policy = -clipped_surrogate(
        log_ratio, scalar_advantage, clip_low=config.clip_low, clip_high=config.clip_high
    ).mean()
    # PPO clipping compares with the exact rollout behavior policy, while KL
    # remains anchored to the immutable SFT base across online iterations.
    kl = _k3_reference_kl(current_logps, reference_logps)
    return policy + config.kl_beta * kl, policy.detach(), kl.detach(), response_tokens


def train_rl(config_path: Path) -> None:  # pragma: no cover - GPU entrypoint
    import torch
    from peft import LoraConfig, PeftModel, get_peft_model
    from transformers import AutoModelForCausalLM, AutoModelForImageTextToText, AutoTokenizer

    config = load_rl_config(config_path)
    dataset_path = Path(config.dataset)
    validate_dataset_artifact(dataset_path, config)
    random.seed(config.seed)
    torch.manual_seed(config.seed)
    rows = [
        RLEpisodeRecord.model_validate(json.loads(line)).model_dump(mode="json")
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = [row for row in rows if math.isfinite(float(row["advantage"]))]
    validate_behavior_policy(rows, config)
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
    if config.algorithm == "episode_online_progressive_grpo_lora":
        resolved_base_sha = resolve_model_identity(config.model_name, config.base_model_sha256)
        if resolved_base_sha != config.base_model_sha256:
            raise ValueError("base model identity mismatch")
    if config.initial_adapter:
        resolved_adapter_sha = resolve_model_identity(
            config.initial_adapter, config.initial_adapter_sha256
        )
        if resolved_adapter_sha != config.initial_adapter_sha256:
            raise ValueError("initial adapter identity mismatch")
        if Path(config.initial_adapter).resolve() == Path(config.output_dir).resolve():
            raise ValueError("output_dir must not overwrite the behavior adapter")
        model = PeftModel.from_pretrained(model, config.initial_adapter, is_trainable=True)
    else:
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
    # Snapshot the exact rollout policy before any optimizer update. This is
    # equivalent to rollout-time log-probabilities at temperature 1 while the
    # immutable behavior-policy artifact is required to match the dataset.
    for episode in encoded:
        for turn in episode["turns"]:
            gpu_turn = {key: value.to(device) for key, value in turn.items()}
            with torch.no_grad():
                turn["old_logps"] = _policy_logps(model, gpu_turn).detach().cpu()
                if config.initial_adapter:
                    with model.disable_adapter():
                        turn["reference_logps"] = (
                            _policy_logps(model, gpu_turn).detach().cpu()
                        )
                else:
                    # Iteration 0 starts from a zero-initialized LoRA, so the
                    # rollout policy and immutable SFT reference are identical.
                    turn["reference_logps"] = turn["old_logps"].clone()
    optimizer = torch.optim.AdamW(
        (parameter for parameter in model.parameters() if parameter.requires_grad),
        lr=config.learning_rate,
    )

    wandb_enabled = bool(os.environ.get("WANDB_API_KEY")) or Path("~/.netrc").expanduser().exists()
    if wandb_enabled:
        import wandb

        wandb.init(
            entity=config.wandb.entity,
            project=config.wandb.project,
            name=config.name,
            config={
                **config.model_dump(),
                "episodes": len(encoded),
                "contract": (
                    "whole episode; behavior policy=frozen pre-update snapshot; "
                    "KL reference=immutable SFT base; "
                    f"algorithm={config.algorithm}; asymmetric clipped DAPO; "
                    "typed terminal reward"
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
                advantages = episode["turn_advantages"] or [episode["advantage"]] * len(
                    episode["turns"]
                )
                for turn, token_count, turn_advantage in zip(
                    episode["turns"], token_counts, advantages, strict=True
                ):
                    gpu_turn = {key: value.to(device) for key, value in turn.items()}
                    loss, policy, kl, response_tokens = _turn_loss(
                        model,
                        gpu_turn,
                        turn["old_logps"],
                        turn["reference_logps"],
                        float(turn_advantage),
                        config,
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
    adapter_files = sorted(
        path for path in adapter_dir.iterdir() if path.is_file() and path.name != "training_manifest.json"
    )
    (adapter_dir / "training_manifest.json").write_text(
        json.dumps(
            {
                "config": config.model_dump(mode="json"),
                "dataset_sha256": file_sha256(dataset_path),
                "parent_adapter": config.initial_adapter,
                "parent_adapter_sha256": config.initial_adapter_sha256,
                "base_model_sha256": config.base_model_sha256,
                "adapter_files_sha256": {
                    path.name: file_sha256(path) for path in adapter_files
                },
                "runtime": {
                    "python": platform.python_version(),
                    "torch": torch.__version__,
                    "cuda": torch.version.cuda,
                    "transformers": __import__("transformers").__version__,
                    "peft": __import__("peft").__version__,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if wandb_enabled:
        wandb.finish()
