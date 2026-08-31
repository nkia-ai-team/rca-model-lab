"""Whole-trajectory preference refinement for multi-turn RCA policies."""

from __future__ import annotations

import json
import os
import platform
import random
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field

from rca_lab.data.sft import SFTMessage
from rca_lab.harness.models import StrictModel
from rca_lab.provenance import file_sha256, resolve_model_identity
from rca_lab.train.rl import _response_shape, _selected_logps
from rca_lab.train.sft import (
    LoRAConfig,
    ReasoningStrength,
    WandbConfig,
    mask_assistant_spans,
    tokenize_runtime_messages,
)


class TrajectoryPreferenceConfig(StrictModel):
    name: str
    algorithm: Literal["whole_trajectory_rpo_lora"]
    model_name: str
    model_sha256: str = Field(min_length=64, max_length=64)
    dataset: str
    dataset_sha256: str = Field(min_length=64, max_length=64)
    output_dir: str
    max_length: int = Field(ge=1)
    epochs: int = Field(ge=1)
    learning_rate: float = Field(gt=0)
    gradient_accumulation_steps: int = Field(ge=1)
    beta: float = Field(gt=0)
    imitation_eta: float = Field(ge=0)
    max_grad_norm: float = Field(gt=0)
    seed: int = 42
    reasoning_strength: ReasoningStrength = "low"
    lora: LoRAConfig
    wandb: WandbConfig


class PreferenceTurn(StrictModel):
    messages: tuple[SFTMessage, SFTMessage, SFTMessage]


class TrajectoryPreferenceRecord(StrictModel):
    scenario_id: str = Field(min_length=1)
    pair_id: str = Field(min_length=1)
    chosen_source: str = Field(min_length=1)
    rejected_source: str = Field(min_length=1)
    chosen_turns: tuple[PreferenceTurn, ...] = Field(min_length=1)
    rejected_turns: tuple[PreferenceTurn, ...] = Field(min_length=1)
    chosen_score: dict[str, Any]
    rejected_score: dict[str, Any]


def load_preference_config(path: Path) -> TrajectoryPreferenceConfig:
    return TrajectoryPreferenceConfig.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def validate_preference_dataset(path: Path, expected_sha256: str) -> None:
    actual = file_sha256(path)
    if actual != expected_sha256:
        raise ValueError(f"preference dataset sha256 mismatch: got={actual} want={expected_sha256}")


def preference_loss_and_slope(margin: Any, beta: float) -> tuple[Any, Any]:
    """Return DPO loss and d(loss)/d(margin) for a full trajectory pair."""
    import torch

    scaled = beta * margin
    loss = -torch.nn.functional.logsigmoid(scaled)
    slope = -beta * torch.sigmoid(-scaled)
    return loss, slope


def regularized_preference_loss_and_slopes(
    margin: Any,
    chosen_mean_logp: Any,
    *,
    beta: float,
    imitation_eta: float,
) -> tuple[Any, Any, Any, Any]:
    """Return trajectory RPO loss and coefficients for chosen/rejected log-probs.

    RPO adds ``eta * beta * -log(pi(chosen))`` to DPO.  Keeping the imitation
    coefficient tied to beta follows the paper's objective and prevents the
    small preference set from erasing the successful investigation policy.
    """
    dpo_loss, margin_slope = preference_loss_and_slope(margin, beta)
    imitation_weight = imitation_eta * beta
    imitation_loss = -chosen_mean_logp
    loss = dpo_loss + imitation_weight * imitation_loss
    chosen_slope = margin_slope - imitation_weight
    rejected_slope = -margin_slope
    return loss, chosen_slope, rejected_slope, imitation_loss


def _turn_logp_sums(model: Any, turn: dict[str, Any]) -> tuple[Any, Any, int]:
    """Return current/reference response log-prob sums for one actor turn."""
    import torch

    input_ids = turn["input_ids"]
    attention_mask = turn["attention_mask"]
    first, response_tokens = _response_shape(turn["labels"][0].tolist())
    targets = input_ids[:, first:]
    logits_to_keep = response_tokens + 1
    disable = getattr(model, "disable_adapter", None)
    reference_context = disable() if disable is not None else nullcontext()
    with torch.no_grad(), reference_context:
        reference_logits = model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            logits_to_keep=logits_to_keep,
            use_cache=False,
        ).logits[:, :-1]
        reference_sum = _selected_logps(reference_logits, targets).sum().detach()
    del reference_logits
    with torch.no_grad():
        current_logits = model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            logits_to_keep=logits_to_keep,
            use_cache=False,
        ).logits[:, :-1]
        current_sum = _selected_logps(current_logits, targets).sum().detach()
    del current_logits
    return current_sum, reference_sum, response_tokens


def _trajectory_log_ratio(model: Any, turns: list[dict[str, Any]]) -> tuple[Any, Any, int]:
    current_total = None
    reference_total = None
    token_total = 0
    for turn in turns:
        current, reference, tokens = _turn_logp_sums(model, turn)
        current_total = current if current_total is None else current_total + current
        reference_total = reference if reference_total is None else reference_total + reference
        token_total += tokens
    if current_total is None or reference_total is None or token_total == 0:
        raise ValueError("trajectory has no response tokens")
    current_mean_logp = current_total / token_total
    return (current_total - reference_total) / token_total, current_mean_logp, token_total


def _backward_trajectory(
    model: Any,
    turns: list[dict[str, Any]],
    coefficient: Any,
    token_total: int,
) -> None:
    """Backpropagate a detached trajectory-level DPO slope one turn at a time."""
    for turn in turns:
        input_ids = turn["input_ids"]
        attention_mask = turn["attention_mask"]
        first, response_tokens = _response_shape(turn["labels"][0].tolist())
        targets = input_ids[:, first:]
        logits = model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            logits_to_keep=response_tokens + 1,
            use_cache=False,
        ).logits[:, :-1]
        current_sum = _selected_logps(logits, targets).sum()
        (coefficient * current_sum / token_total).backward()
        del logits, current_sum


def train_preference(config_path: Path) -> None:  # pragma: no cover - GPU entrypoint
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoModelForImageTextToText, AutoTokenizer

    config = load_preference_config(config_path)
    resolve_model_identity(config.model_name, config.model_sha256)
    dataset_path = Path(config.dataset)
    validate_preference_dataset(dataset_path, config.dataset_sha256)
    rows = [
        TrajectoryPreferenceRecord.model_validate(json.loads(line)).model_dump(mode="json")
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise ValueError("preference dataset is empty")
    random.seed(config.seed)
    torch.manual_seed(config.seed)
    tokenizer = AutoTokenizer.from_pretrained(config.model_name)
    start_id = tokenizer.convert_tokens_to_ids("<|start|>")
    assistant_role_ids = tuple(tokenizer.encode("assistant", add_special_tokens=False))
    message_id = tokenizer.convert_tokens_to_ids("<|message|>")
    eot_id = tokenizer.convert_tokens_to_ids("<|eot|>")

    def encode_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        encoded = []
        for turn in turns:
            tokenized = tokenize_runtime_messages(
                tokenizer,
                turn["messages"],
                reasoning_strength=config.reasoning_strength,
            )
            input_ids = list(tokenized["input_ids"])
            if len(input_ids) > config.max_length:
                raise ValueError(f"preference turn exceeds max_length: tokens={len(input_ids)}")
            labels = mask_assistant_spans(
                input_ids,
                start_id=start_id,
                assistant_role_ids=assistant_role_ids,
                message_id=message_id,
                eot_id=eot_id,
            )
            _response_shape(labels)
            encoded.append(
                {
                    "input_ids": torch.tensor([input_ids], dtype=torch.long),
                    "attention_mask": torch.ones((1, len(input_ids)), dtype=torch.long),
                    "labels": torch.tensor([labels], dtype=torch.long),
                }
            )
        return encoded

    encoded = [
        {
            **row,
            "chosen_turns": encode_turns(row["chosen_turns"]),
            "rejected_turns": encode_turns(row["rejected_turns"]),
        }
        for row in rows
    ]
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
    model.train()
    for module in model.modules():
        if isinstance(module, torch.nn.Dropout):
            module.eval()
    device = next(model.parameters()).device
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
            config={**config.model_dump(), "pairs": len(encoded)},
        )
    global_step = 0
    for epoch in range(config.epochs):
        random.shuffle(encoded)
        for offset in range(0, len(encoded), config.gradient_accumulation_steps):
            group = encoded[offset : offset + config.gradient_accumulation_steps]
            optimizer.zero_grad(set_to_none=True)
            loss_total = 0.0
            margin_total = 0.0
            tokens_total = 0
            imitation_total = 0.0
            for pair in group:
                chosen = [
                    {key: value.to(device) for key, value in turn.items()}
                    for turn in pair["chosen_turns"]
                ]
                rejected = [
                    {key: value.to(device) for key, value in turn.items()}
                    for turn in pair["rejected_turns"]
                ]
                chosen_ratio, chosen_mean_logp, chosen_tokens = _trajectory_log_ratio(model, chosen)
                rejected_ratio, _, rejected_tokens = _trajectory_log_ratio(model, rejected)
                margin = chosen_ratio - rejected_ratio
                loss, chosen_slope, rejected_slope, imitation_loss = (
                    regularized_preference_loss_and_slopes(
                        margin,
                        chosen_mean_logp,
                        beta=config.beta,
                        imitation_eta=config.imitation_eta,
                    )
                )
                scale = 1.0 / len(group)
                _backward_trajectory(model, chosen, chosen_slope.detach() * scale, chosen_tokens)
                _backward_trajectory(
                    model, rejected, rejected_slope.detach() * scale, rejected_tokens
                )
                loss_total += float(loss) * scale
                margin_total += float(margin) * scale
                imitation_total += float(imitation_loss) * scale
                tokens_total += chosen_tokens + rejected_tokens
            torch.nn.utils.clip_grad_norm_(model.parameters(), config.max_grad_norm)
            optimizer.step()
            global_step += 1
            record = {
                "epoch": epoch + 1,
                "step": global_step,
                "train/loss": loss_total,
                "train/preference_margin": margin_total,
                "train/chosen_nll": imitation_total,
                "train/response_tokens": tokens_total,
            }
            print(json.dumps(record))
            if wandb_enabled:
                wandb.log(record, step=global_step)

    adapter_dir = Path(config.output_dir) / "adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)
    adapter_files = sorted(
        path
        for path in adapter_dir.iterdir()
        if path.is_file() and path.name != "training_manifest.json"
    )
    (adapter_dir / "training_manifest.json").write_text(
        json.dumps(
            {
                "config": config.model_dump(mode="json"),
                "config_sha256": file_sha256(config_path),
                "dataset_sha256": file_sha256(dataset_path),
                "adapter_files_sha256": {path.name: file_sha256(path) for path in adapter_files},
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
