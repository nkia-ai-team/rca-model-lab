"""Full-trajectory LoRA SFT with loss restricted to assistant action spans."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field

from rca_lab.data.sft import SFTDatasetManifest
from rca_lab.harness.models import StrictModel


class LoRAConfig(StrictModel):
    rank: int = Field(ge=1)
    alpha: int = Field(ge=1)
    dropout: float = Field(ge=0.0, lt=1.0)
    target_modules: str


class WandbConfig(StrictModel):
    entity: str
    project: str


class FullTrajectorySFTConfig(StrictModel):
    name: str
    model_name: str
    dataset: str
    dataset_manifest: str
    terminal_contract: str | None = None
    curation_manifest: str | None = None
    expected_scenarios: int = Field(ge=1)
    expected_trajectories: int = Field(ge=1)
    output_dir: str
    max_length: int = Field(ge=1)
    epochs: int = Field(ge=1)
    learning_rate: float = Field(gt=0)
    batch_size: int = Field(ge=1)
    gradient_accumulation_steps: int = Field(ge=1)
    save_total_limit: int = Field(ge=1)
    trajectory_mode: Literal["episode_exact_runtime"] = "episode_exact_runtime"
    assistant_only_loss: Literal[True] = True
    case_balanced_loss: Literal[True] = True
    use_liger_kernel: bool = True
    lora: LoRAConfig
    wandb: WandbConfig


def load_sft_config(path: Path) -> FullTrajectorySFTConfig:
    return FullTrajectorySFTConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def case_balanced_weights(scenario_ids: list[str]) -> list[float]:
    """Give every scenario equal total mass while retaining all trajectories."""
    if not scenario_ids:
        raise ValueError("cannot balance an empty scenario population")
    counts = Counter(scenario_ids)
    return [len(scenario_ids) / (len(counts) * counts[item]) for item in scenario_ids]


def load_verified_training_rows(config: FullTrajectorySFTConfig) -> list[dict[str, Any]]:
    dataset_path = Path(config.dataset)
    manifest = SFTDatasetManifest.model_validate_json(
        Path(config.dataset_manifest).read_text(encoding="utf-8")
    )
    payload = dataset_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != manifest.dataset_sha256:
        raise ValueError("SFT dataset SHA-256 does not match its manifest")
    provenance_paths = (
        (config.terminal_contract, manifest.terminal_contract_sha256, "terminal contract"),
        (config.curation_manifest, manifest.curation_manifest_sha256, "curation manifest"),
    )
    for configured_path, expected_digest, label in provenance_paths:
        if configured_path is None and expected_digest is None:
            continue
        if configured_path is None or expected_digest is None:
            raise ValueError(f"SFT {label} provenance is incomplete")
        actual_digest = hashlib.sha256(Path(configured_path).read_bytes()).hexdigest()
        if actual_digest != expected_digest:
            raise ValueError(f"SFT {label} SHA-256 does not match its dataset manifest")
    rows = [json.loads(line) for line in payload.decode().splitlines() if line]
    scenarios = {str(row["scenario_id"]) for row in rows}
    if len(rows) != manifest.trajectory_count or len(rows) != config.expected_trajectories:
        raise ValueError("SFT trajectory count does not match config/manifest")
    if scenarios != set(manifest.scenarios) or len(scenarios) != config.expected_scenarios:
        raise ValueError("SFT scenario population does not match config/manifest")
    return rows


def mask_assistant_spans(
    input_ids: list[int],
    *,
    start_id: int,
    assistant_role_ids: tuple[int, ...],
    message_id: int,
    eot_id: int,
) -> list[int]:
    """Mask everything except assistant bodies, independent of header metadata."""
    labels = [-100] * len(input_ids)
    index = 0
    spans = 0
    while index < len(input_ids):
        role_end = index + 1 + len(assistant_role_ids)
        if (
            input_ids[index] != start_id
            or tuple(input_ids[index + 1 : role_end]) != assistant_role_ids
        ):
            index += 1
            continue
        try:
            body_start = input_ids.index(message_id, role_end) + 1
            body_end = input_ids.index(eot_id, body_start)
        except ValueError as error:
            raise ValueError("assistant span has no message boundary or closing EOT") from error
        labels[body_start : body_end + 1] = input_ids[body_start : body_end + 1]
        spans += 1
        index = body_end + 1
    if spans == 0:
        raise ValueError("trajectory has no assistant span")
    return labels


def train_sft(config_path: Path) -> None:  # pragma: no cover - GPU entrypoint
    import os

    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import (
        AutoModelForCausalLM,
        AutoModelForImageTextToText,
        AutoTokenizer,
        Trainer,
        TrainingArguments,
    )

    config = load_sft_config(config_path)
    if config.batch_size != 1:
        raise ValueError("episode_exact_runtime requires batch_size=1")
    tokenizer = AutoTokenizer.from_pretrained(config.model_name)
    rows = load_verified_training_rows(config)
    if len(rows) % config.gradient_accumulation_steps:
        raise ValueError(
            "episode count must be divisible by gradient_accumulation_steps; "
            "partial groups receive different scaling"
        )
    sample_weights = case_balanced_weights([str(row["scenario_id"]) for row in rows])
    start_id = tokenizer.convert_tokens_to_ids("<|start|>")
    assistant_role_ids = tuple(tokenizer.encode("assistant", add_special_tokens=False))
    message_id = tokenizer.convert_tokens_to_ids("<|message|>")
    eot_id = tokenizer.convert_tokens_to_ids("<|eot|>")
    encoded: list[dict[str, Any]] = []
    lengths: list[int] = []
    for row, sample_weight in zip(rows, sample_weights, strict=True):
        episode_turns: list[dict[str, Any]] = []
        for turn in row["turns"]:
            tokenized = tokenizer.apply_chat_template(
                turn["messages"], tokenize=True, reasoning_strength="low"
            )
            input_ids = list(tokenized["input_ids"])
            if len(input_ids) > config.max_length:
                raise ValueError(
                    f"runtime turn exceeds max_length without safe truncation: "
                    f"{row['trajectory_id']} turn={turn['turn']} "
                    f"tokens={len(input_ids)} max={config.max_length}"
                )
            labels = mask_assistant_spans(
                input_ids,
                start_id=start_id,
                assistant_role_ids=assistant_role_ids,
                message_id=message_id,
                eot_id=eot_id,
            )
            episode_turns.append(
                {
                    "input_ids": input_ids,
                    "attention_mask": [1] * len(input_ids),
                    "labels": labels,
                }
            )
            lengths.append(len(input_ids))
        encoded.append({"episode_turns": episode_turns, "sample_weight": sample_weight})

    class EpisodeCollator:
        def __call__(self, features: list[dict[str, Any]]) -> dict[str, torch.Tensor]:
            if len(features) != 1:
                raise ValueError("episodic training requires per_device_train_batch_size=1")
            return {
                "episode_turns": [
                    {key: torch.tensor([value], dtype=torch.long) for key, value in turn.items()}
                    for turn in features[0]["episode_turns"]
                ],
                "sample_weight": torch.tensor(features[0]["sample_weight"], dtype=torch.float32),
            }

    class EpisodeDataset(torch.utils.data.Dataset):
        def __len__(self) -> int:
            return len(encoded)

        def __getitem__(self, index: int) -> dict[str, Any]:
            return encoded[index]

    class EpisodicTrainer(Trainer):
        """Backprop exact runtime turns sequentially, with one episode-level loss."""

        def training_step(
            self,
            model: torch.nn.Module,
            inputs: dict[str, Any],
            num_items_in_batch: int | None = None,
        ) -> torch.Tensor:
            del num_items_in_batch
            model.train()
            prepared = self._prepare_inputs(inputs)
            turns = prepared["episode_turns"]
            sample_weight = prepared["sample_weight"]
            token_counts = [int((turn["labels"] != -100).sum().item()) for turn in turns]
            total_tokens = sum(token_counts)
            if total_tokens == 0:
                raise ValueError("episode has no assistant tokens")
            detached = torch.zeros((), device=turns[0]["input_ids"].device)
            for turn, token_count in zip(turns, token_counts, strict=True):
                with self.compute_loss_context_manager():
                    loss = model(**turn).loss
                weight = token_count / total_tokens
                # Accelerate owns gradient-accumulation scaling. Dividing here
                # as well shrinks gradients twice on current Trainer versions.
                self.accelerator.backward(loss * weight * sample_weight)
                detached += loss.detach() * weight * sample_weight
            return detached

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

    wandb_enabled = bool(os.environ.get("WANDB_API_KEY")) or Path("~/.netrc").expanduser().exists()
    if wandb_enabled:
        import wandb

        wandb.init(
            entity=config.wandb.entity,
            project=config.wandb.project,
            name=config.name,
            config={
                **config.model_dump(),
                "samples": len(encoded),
                "turns": sum(row["turn_count"] for row in rows),
                "min_tokens": min(lengths),
                "max_tokens": max(lengths),
                "loss_contract": (
                    "episode grouped; each turn uses exact runtime system+state prompt; "
                    "assistant only; token-weighted episode loss"
                ),
                "case_balance_contract": (
                    "each scenario contributes equal total loss per epoch; "
                    "multiple accepted trajectories share that scenario weight"
                ),
            },
        )

    arguments = TrainingArguments(
        output_dir=config.output_dir,
        num_train_epochs=config.epochs,
        per_device_train_batch_size=config.batch_size,
        gradient_accumulation_steps=config.gradient_accumulation_steps,
        learning_rate=config.learning_rate,
        lr_scheduler_type="cosine",
        bf16=True,
        gradient_checkpointing=True,
        use_liger_kernel=config.use_liger_kernel,
        logging_steps=1,
        save_strategy="epoch",
        save_total_limit=config.save_total_limit,
        report_to=["wandb"] if wandb_enabled else [],
        run_name=config.name,
        remove_unused_columns=False,
    )
    trainer = EpisodicTrainer(
        model=model,
        args=arguments,
        train_dataset=EpisodeDataset(),
        data_collator=EpisodeCollator(),
    )
    trainer.train()
    trainer.save_model(str(Path(config.output_dir) / "adapter"))
