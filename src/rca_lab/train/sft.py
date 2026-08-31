"""Full-trajectory LoRA SFT with loss restricted to assistant action spans."""

from __future__ import annotations

import hashlib
import json
import platform
from collections import Counter
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator

from rca_lab.data.sft import SFTDatasetManifest
from rca_lab.harness.models import StrictModel
from rca_lab.provenance import file_sha256
from rca_lab.train.checkpoint import (
    TrainingCheckpointContract,
    load_training_model,
    load_training_tokenizer,
    verify_training_checkpoint,
)

ReasoningStrength = Literal["low", "high"]


class LoRAConfig(StrictModel):
    rank: int = Field(ge=1)
    alpha: int = Field(ge=1)
    dropout: float = Field(ge=0.0, lt=1.0)
    target_modules: str


class WandbConfig(StrictModel):
    entity: str
    project: str


class FullTrajectorySFTConfig(TrainingCheckpointContract):
    name: str
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
    reasoning_strength: ReasoningStrength = "low"
    trajectory_mode: Literal["episode_exact_runtime"] = "episode_exact_runtime"
    assistant_only_loss: Literal[True] = True
    case_balanced_loss: Literal[True] = True
    use_liger_kernel: bool = True
    loss_backend: Literal["selective_fused_linear_ce", "model_full_logits"] = (
        "model_full_logits"
    )
    adapter_scope: Literal["language_model", "all_linear"] = "all_linear"
    lora: LoRAConfig
    wandb: WandbConfig

    @model_validator(mode="after")
    def lora_scope_matches_declared_contract(self) -> FullTrajectorySFTConfig:
        if self.adapter_scope == "language_model" and self.lora.target_modules == "all-linear":
            raise ValueError("language_model adapter scope requires an explicit module pattern")
        if self.loss_backend == "selective_fused_linear_ce" and not self.use_liger_kernel:
            raise ValueError("selective fused loss requires use_liger_kernel=true")
        return self


def training_subprocess_environment(current: dict[str, str]) -> dict[str, str]:
    """Return an isolated CUDA training environment for a fresh Python process."""
    environment = dict(current)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    return environment


def load_sft_config(path: Path) -> FullTrajectorySFTConfig:
    return FullTrajectorySFTConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def case_balanced_weights(scenario_ids: list[str]) -> list[float]:
    """Give every scenario equal total mass while retaining all trajectories."""
    if not scenario_ids:
        raise ValueError("cannot balance an empty scenario population")
    counts = Counter(scenario_ids)
    return [len(scenario_ids) / (len(counts) * counts[item]) for item in scenario_ids]


def tokenize_runtime_messages(
    tokenizer: Any,
    messages: Any,
    *,
    reasoning_strength: ReasoningStrength,
) -> Any:
    """Render training turns with the same reasoning contract used at rollout."""
    return tokenizer.apply_chat_template(
        messages,
        tokenize=True,
        reasoning_strength=reasoning_strength,
    )


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


def build_sft_training_manifest(
    *,
    config_path: Path,
    config: FullTrajectorySFTConfig,
    adapter_dir: Path,
    runtime: dict[str, Any],
) -> dict[str, Any]:
    adapter_files = sorted(
        path
        for path in adapter_dir.iterdir()
        if path.is_file() and path.name != "training_manifest.json"
    )
    required = {"adapter_config.json", "adapter_model.safetensors"}
    missing = sorted(required - {path.name for path in adapter_files})
    if missing:
        raise ValueError(f"SFT adapter is incomplete: missing={missing}")
    return {
        "config": config.model_dump(mode="json"),
        "config_sha256": file_sha256(config_path),
        "dataset_sha256": file_sha256(Path(config.dataset)),
        "adapter_files_sha256": {
            path.name: file_sha256(path) for path in adapter_files
        },
        "runtime": runtime,
    }


def train_sft(config_path: Path) -> None:  # pragma: no cover - GPU entrypoint
    import os

    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import Trainer, TrainingArguments

    def selective_fused_causal_loss(
        model: torch.nn.Module, turn: dict[str, torch.Tensor]
    ) -> torch.Tensor:
        """Compute exact assistant-token CE without materializing full-sequence logits."""
        from liger_kernel.transformers import LigerFusedLinearCrossEntropyLoss

        base_model = model.get_base_model()
        outputs = base_model.model(
            input_ids=turn["input_ids"],
            attention_mask=turn["attention_mask"],
            use_cache=False,
        )
        shifted_labels = turn["labels"][:, 1:]
        selected = shifted_labels != -100
        if not bool(selected.any()):
            raise ValueError("runtime turn has no shifted assistant target tokens")
        hidden = outputs.last_hidden_state[:, :-1, :][selected]
        targets = shifted_labels[selected]
        text_config = base_model.config.text_config
        hidden = hidden * float(text_config.output_multiplier)
        criterion = LigerFusedLinearCrossEntropyLoss(
            ignore_index=-100,
            reduction="mean",
            softcap=float(text_config.final_logit_softcapping),
        )
        return criterion(base_model.lm_head.weight, hidden, targets)

    config = load_sft_config(config_path)
    if config.batch_size != 1:
        raise ValueError("episode_exact_runtime requires batch_size=1")
    tokenizer = load_training_tokenizer(config)
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
            tokenized = tokenize_runtime_messages(
                tokenizer,
                turn["messages"],
                reasoning_strength=config.reasoning_strength,
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
            token_counts = [int((turn["labels"][:, 1:] != -100).sum().item()) for turn in turns]
            total_tokens = sum(token_counts)
            if total_tokens == 0:
                raise ValueError("episode has no assistant tokens")
            detached = torch.zeros((), device=turns[0]["input_ids"].device)
            for turn, token_count in zip(turns, token_counts, strict=True):
                with self.compute_loss_context_manager():
                    if config.loss_backend == "selective_fused_linear_ce":
                        loss = selective_fused_causal_loss(model, turn)
                    else:
                        loss = model(**turn).loss
                weight = token_count / total_tokens
                # Accelerate owns gradient-accumulation scaling. Dividing here
                # as well shrinks gradients twice on current Trainer versions.
                self.accelerator.backward(loss * weight * sample_weight)
                detached += loss.detach() * weight * sample_weight
            return detached

    model = load_training_model(
        config,
        torch_module=torch,
        attention_implementation=os.environ.get("ATTN", "sdpa"),
    )
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
    adapter_dir = Path(config.output_dir) / "adapter"
    trainer.save_model(str(adapter_dir))
    tokenizer.save_pretrained(adapter_dir)
    manifest = build_sft_training_manifest(
        config_path=config_path,
        config=config,
        adapter_dir=adapter_dir,
        runtime={
            "python": platform.python_version(),
            "torch": torch.__version__,
            "cuda": torch.version.cuda,
            "transformers": __import__("transformers").__version__,
            "peft": __import__("peft").__version__,
            "checkpoint": verify_training_checkpoint(config),
        },
    )
    (adapter_dir / "training_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
