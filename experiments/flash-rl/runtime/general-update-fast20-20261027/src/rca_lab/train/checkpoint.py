"""One immutable checkpoint/tokenizer contract for every training stage (ported from thinkfl)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

CheckpointFormat = Literal["dense", "compressed_tensors_fp8_block", "finegrained_fp8_block"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_TOKENIZER_IDENTITY_FILES = ("tokenizer.json", "tokenizer_config.json", "chat_template.jinja")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class TrainingCheckpointContract(BaseModel):
    """Bind source weights, prompt rendering, and training compute semantics."""

    model_config = ConfigDict(extra="forbid")

    model_name: str
    tokenizer_name: str = ""
    checkpoint_format: CheckpointFormat = "dense"
    model_revision: str = ""
    model_config_sha256: str = ""
    model_index_sha256: str = ""
    tokenizer_sha256: str = ""
    training_compute_dtype: Literal["bfloat16"] = "bfloat16"
    adapter_parameter_dtype: Literal["bfloat16", "float32"] = "bfloat16"
    optimizer_state_dtype: Literal["float32"] = "float32"
    dequantize_for_training: bool = False

    @model_validator(mode="after")
    def compressed_checkpoint_is_reproducible_and_trainable(self) -> TrainingCheckpointContract:
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
            raise ValueError("compressed FP8 training requires dequantize_for_training=true; optimized compressed-tensors kernels are inference-only")
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
        "adapter_parameter_dtype": contract.adapter_parameter_dtype,
        "optimizer_state_dtype": contract.optimizer_state_dtype,
        "dequantize_for_training": contract.dequantize_for_training,
        "source_weight_dtype": "float8_e4m3fn" if contract.checkpoint_format != "dense" else "model_declared",
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
        weights = quantization.get("config_groups", {}).get("FP8_BLOCK", {}).get("weights", {})
        if contract.checkpoint_format == "finegrained_fp8_block":
            if (quantization.get("quant_method") != "fp8"
                    or quantization.get("weight_block_size") != [128, 128]
                    or quantization.get("fmt") != "e4m3"):
                raise ValueError("checkpoint is not the declared finegrained FP8[128,128] model")
        elif (
            quantization.get("quant_method") != "compressed-tensors"
            or quantization.get("quantization_status") != "compressed"
            or weights.get("type") != "float"
            or weights.get("num_bits") != 8
            or weights.get("strategy") != "block"
            or weights.get("block_structure") != [128, 128]
        ):
            raise ValueError("checkpoint is not the declared compressed FP8_BLOCK[128,128] model")
    if tokenizer_path.is_dir() and tokenizer_artifact_identity(tokenizer_path) != contract.tokenizer_sha256:
        raise ValueError("tokenizer SHA-256 does not match the training contract")
    return evidence


def load_training_tokenizer(contract: TrainingCheckpointContract) -> Any:
    from transformers import AutoTokenizer

    verify_training_checkpoint(contract)
    return AutoTokenizer.from_pretrained(contract.resolved_tokenizer_name)


def save_training_tokenizer_artifacts(contract: TrainingCheckpointContract, output: Path) -> str:
    """Save a usable tokenizer while preserving its identity-critical files exactly."""
    load_training_tokenizer(contract).save_pretrained(output)
    source = Path(contract.resolved_tokenizer_name)
    if source.is_dir():
        for name in _TOKENIZER_IDENTITY_FILES:
            shutil.copy2(source / name, output / name)
    identity = tokenizer_artifact_identity(output)
    if contract.tokenizer_sha256 and identity != contract.tokenizer_sha256:
        raise ValueError("saved tokenizer identity differs from the training contract")
    return identity


def training_device_map(environment: dict[str, str]) -> str | None:
    """Keep single-process sharding; let DDP load on CPU before rank placement."""
    world_size = int(environment.get("WORLD_SIZE", "1"))
    if world_size <= 1:
        return "auto"
    if "LOCAL_RANK" not in environment:
        raise RuntimeError("distributed training requires LOCAL_RANK")
    if int(environment["LOCAL_RANK"]) < 0:
        raise RuntimeError("LOCAL_RANK must be non-negative")
    return None


def initialize_distributed_cuda(torch_module: Any, environment: dict[str, str]) -> None:
    if int(environment.get("WORLD_SIZE", "1")) <= 1:
        return
    local_rank = int(environment["LOCAL_RANK"])
    torch_module.cuda.set_device(local_rank)
    torch_module.cuda.init()


def load_training_model(contract: TrainingCheckpointContract, *, torch_module: Any, attention_implementation: str) -> Any:
    """Load dense compute weights from the pinned FP8 source for LoRA training."""
    from transformers import AutoConfig, AutoModelForCausalLM, AutoModelForImageTextToText

    verify_training_checkpoint(contract)
    environment = dict(os.environ)
    device_map = training_device_map(environment)
    initialize_distributed_cuda(torch_module, environment)
    load_kwargs: dict[str, Any] = {"dtype": torch_module.bfloat16, "attn_implementation": attention_implementation}
    if device_map is not None:
        load_kwargs["device_map"] = device_map
    if contract.checkpoint_format == "compressed_tensors_fp8_block":
        from transformers import CompressedTensorsConfig

        load_kwargs["quantization_config"] = CompressedTensorsConfig(dequantize=True)
    elif contract.checkpoint_format == "finegrained_fp8_block":
        from transformers import FineGrainedFP8Config

        load_kwargs["quantization_config"] = FineGrainedFP8Config(dequantize=True)
    architecture = AutoConfig.from_pretrained(contract.model_name).architectures or []
    loader = AutoModelForImageTextToText if any(name.endswith("ForConditionalGeneration") for name in architecture) else AutoModelForCausalLM
    model = loader.from_pretrained(contract.model_name, **load_kwargs)
    if contract.checkpoint_format == "compressed_tensors_fp8_block":
        disable_activation_fake_quant(model)
    assert_input_gradient_flows(model, torch_module=torch_module)
    return model


def disable_activation_fake_quant(model: Any) -> None:
    """Turn off the input-activation fake quantization compressed-tensors leaves on every decompressed Linear.

    The fp8-block scheme also declares dynamic fp8 input activations; after `dequantize=True` the weights are
    plain bf16 but each Linear's forward stays wrapped in an activation quantize-dequantize that has no
    straight-through gradient. With it enabled no gradient reaches a layer's input through the base weights:
    LoRA only sees its own branch and the residual stream (2026-10-02 diagnosis — SFT never learned, single-example
    overfits blew up). Training therefore runs the dequantized weights without activation quantization; serving
    (vLLM fp8) keeps it, a ~2-3% per-layer output difference.
    """
    from compressed_tensors.quantization import disable_quantization

    model.apply(disable_quantization)


def assert_input_gradient_flows(model: Any, *, torch_module: Any) -> None:
    """Fail fast if a frozen base Linear does not propagate gradient to its input (see disable_activation_fake_quant)."""
    linear = next((module for name, module in model.named_modules() if isinstance(module, torch_module.nn.Linear) and "language_model" in name), None)
    if linear is None:
        linear = next((module for module in model.modules() if isinstance(module, torch_module.nn.Linear)), None)
    if linear is None:
        return
    probe = torch_module.zeros(1, linear.in_features, device=linear.weight.device, dtype=linear.weight.dtype, requires_grad=True)
    with torch_module.enable_grad():
        if not linear(probe).requires_grad:
            raise RuntimeError("base Linear output does not require grad: a forward wrapper (e.g. activation fake quantization) blocks gradient to layer inputs")
