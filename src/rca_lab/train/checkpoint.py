"""One immutable checkpoint/tokenizer contract for every training stage."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import model_validator

from rca_lab.harness.models import StrictModel
from rca_lab.provenance import file_sha256

CheckpointFormat = Literal["dense", "compressed_tensors_fp8_block"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_TOKENIZER_IDENTITY_FILES = (
    "tokenizer.json",
    "tokenizer_config.json",
    "chat_template.jinja",
)


class TrainingCheckpointContract(StrictModel):
    """Bind source weights, prompt rendering, and training compute semantics."""

    model_name: str
    tokenizer_name: str = ""
    checkpoint_format: CheckpointFormat = "dense"
    model_revision: str = ""
    model_config_sha256: str = ""
    model_index_sha256: str = ""
    tokenizer_sha256: str = ""
    training_compute_dtype: Literal["bfloat16"] = "bfloat16"
    dequantize_for_training: bool = False

    @model_validator(mode="after")
    def compressed_checkpoint_is_reproducible_and_trainable(
        self,
    ) -> TrainingCheckpointContract:
        if self.checkpoint_format == "dense":
            if self.dequantize_for_training:
                raise ValueError("dense checkpoints cannot set dequantize_for_training")
            return self
        required = {
            "tokenizer_name": self.tokenizer_name,
            "model_revision": self.model_revision,
            "model_config_sha256": self.model_config_sha256,
            "model_index_sha256": self.model_index_sha256,
            "tokenizer_sha256": self.tokenizer_sha256,
        }
        missing = sorted(key for key, value in required.items() if not value)
        if missing:
            raise ValueError(f"FP8 checkpoint contract is incomplete: missing={missing}")
        if not self.dequantize_for_training:
            raise ValueError(
                "compressed FP8 training requires dequantize_for_training=true; "
                "optimized compressed-tensors kernels are inference-only"
            )
        if not _REVISION.fullmatch(self.model_revision):
            raise ValueError("model_revision must be a 40-character lowercase Git SHA")
        for field in ("model_config_sha256", "model_index_sha256", "tokenizer_sha256"):
            if not _SHA256.fullmatch(getattr(self, field)):
                raise ValueError(f"{field} must be 64 lowercase hexadecimal characters")
        return self

    @property
    def resolved_tokenizer_name(self) -> str:
        return self.tokenizer_name or self.model_name


def tokenizer_artifact_identity(path: Path) -> str:
    """Fingerprint every file that can change token IDs or rendered prompts."""
    import hashlib

    if not path.is_dir():
        return ""
    files = [path / name for name in _TOKENIZER_IDENTITY_FILES]
    if any(not item.is_file() for item in files):
        return ""
    digest = hashlib.sha256()
    for item in files:
        digest.update(item.name.encode())
        digest.update(item.read_bytes())
    return digest.hexdigest()


def verify_training_checkpoint(contract: TrainingCheckpointContract) -> dict[str, Any]:
    """Reject drift before GPU allocation and return manifest-ready evidence."""
    evidence: dict[str, Any] = {
        "checkpoint_format": contract.checkpoint_format,
        "model_revision": contract.model_revision,
        "training_compute_dtype": contract.training_compute_dtype,
        "dequantize_for_training": contract.dequantize_for_training,
        "source_weight_dtype": (
            "float8_e4m3fn"
            if contract.checkpoint_format == "compressed_tensors_fp8_block"
            else "model_declared"
        ),
    }
    if contract.checkpoint_format == "dense":
        return evidence
    model_path = Path(contract.model_name)
    tokenizer_path = Path(contract.resolved_tokenizer_name)
    if model_path.is_dir():
        config_path = model_path / "config.json"
        index_path = model_path / "model.safetensors.index.json"
        if file_sha256(config_path) != contract.model_config_sha256:
            raise ValueError("FP8 model config SHA-256 does not match the training contract")
        if file_sha256(index_path) != contract.model_index_sha256:
            raise ValueError("FP8 model index SHA-256 does not match the training contract")
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        quantization = payload.get("quantization_config", {})
        group = quantization.get("config_groups", {}).get("FP8_BLOCK", {})
        weights = group.get("weights", {})
        if (
            quantization.get("quant_method") != "compressed-tensors"
            or quantization.get("quantization_status") != "compressed"
            or weights.get("type") != "float"
            or weights.get("num_bits") != 8
            or weights.get("strategy") != "block"
            or weights.get("block_structure") != [128, 128]
        ):
            raise ValueError("checkpoint is not the declared compressed FP8_BLOCK[128,128] model")
    if tokenizer_path.is_dir():
        actual_tokenizer = tokenizer_artifact_identity(tokenizer_path)
        if actual_tokenizer != contract.tokenizer_sha256:
            raise ValueError("tokenizer SHA-256 does not match the training contract")
    return evidence


def load_training_tokenizer(contract: TrainingCheckpointContract) -> Any:
    from transformers import AutoTokenizer

    verify_training_checkpoint(contract)
    return AutoTokenizer.from_pretrained(contract.resolved_tokenizer_name)


def load_training_model(
    contract: TrainingCheckpointContract,
    *,
    torch_module: Any,
    attention_implementation: str,
) -> Any:
    """Load dense compute weights from the pinned FP8 source for LoRA training."""
    from transformers import (
        AutoConfig,
        AutoModelForCausalLM,
        AutoModelForImageTextToText,
    )

    verify_training_checkpoint(contract)
    load_kwargs: dict[str, Any] = {
        "dtype": torch_module.bfloat16,
        "device_map": "auto",
        "attn_implementation": attention_implementation,
    }
    if contract.checkpoint_format == "compressed_tensors_fp8_block":
        from transformers import CompressedTensorsConfig

        load_kwargs["quantization_config"] = CompressedTensorsConfig(dequantize=True)
    architecture = AutoConfig.from_pretrained(contract.model_name).architectures or []
    loader = (
        AutoModelForImageTextToText
        if any(name.endswith("ForConditionalGeneration") for name in architecture)
        else AutoModelForCausalLM
    )
    return loader.from_pretrained(contract.model_name, **load_kwargs)
