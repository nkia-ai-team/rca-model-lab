"""Shared precision contract for parameter-efficient training (ported from the thinkfl branch)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def is_lora_parameter(name: str) -> bool:
    """Return whether ``name`` belongs to a PEFT LoRA adapter."""
    return name.startswith("lora_") or ".lora_" in name


def configure_lora_precision(
    model: Any,
    *,
    torch_module: Any,
    adapter_parameter_dtype: str = "bfloat16",
) -> dict[str, Any]:
    """Freeze the base and keep trainable LoRA tensors at declared precision."""
    dtypes = {
        "bfloat16": torch_module.bfloat16,
        "float32": torch_module.float32,
    }
    try:
        adapter_dtype = dtypes[adapter_parameter_dtype]
    except KeyError as error:
        raise ValueError(f"unsupported LoRA parameter dtype: {adapter_parameter_dtype}") from error
    trainable: list[tuple[str, Any]] = []
    unexpected: list[str] = []
    for name, parameter in model.named_parameters():
        if not parameter.requires_grad:
            continue
        if not is_lora_parameter(name):
            unexpected.append(name)
            continue
        parameter.data = parameter.data.to(dtype=adapter_dtype)
        trainable.append((name, parameter))
    if unexpected:
        raise ValueError(f"non-LoRA parameters are trainable: {unexpected[:8]}")
    if not trainable:
        raise ValueError("model has no trainable LoRA parameters")
    wrong_dtype = [name for name, parameter in trainable if parameter.dtype != adapter_dtype]
    if wrong_dtype:
        raise ValueError(f"LoRA parameters are not {adapter_parameter_dtype}: {wrong_dtype[:8]}")
    return {
        "base_weights_frozen": True,
        "adapter_parameter_dtype": adapter_parameter_dtype,
        "gradient_dtype": adapter_parameter_dtype,
        "optimizer_master_weight_dtype": "float32",
        "optimizer_state_dtype": "float32",
        "trainable_parameter_count": sum(parameter.numel() for _, parameter in trainable),
    }


def build_fp32_master_adamw(
    parameters: Iterable[Any],
    *,
    torch_module: Any,
    lr: float,
    betas: tuple[float, float] = (0.9, 0.999),
    eps: float = 1e-8,
    weight_decay: float = 0.01,
) -> Any:
    """Build AdamW with FP32 master weights/state for LoRA parameters."""
    model_parameters = [parameter for parameter in parameters if parameter.requires_grad]
    if not model_parameters:
        raise ValueError("optimizer received no trainable parameters")

    class FP32MasterAdamW(torch_module.optim.Optimizer):
        def __init__(self) -> None:
            defaults = {"lr": lr, "betas": betas, "eps": eps, "weight_decay": weight_decay}
            super().__init__(model_parameters, defaults)
            self._pairs = [
                (
                    parameter,
                    torch_module.nn.Parameter(parameter.detach().to(dtype=torch_module.float32).clone(), requires_grad=True),
                    parameter.dtype,
                )
                for parameter in model_parameters
            ]
            self._master_optimizer = torch_module.optim.AdamW(
                [master for _, master, _ in self._pairs], lr=lr, betas=betas, eps=eps, weight_decay=weight_decay
            )

        def _sync_hyperparameters(self) -> None:
            for model_group, master_group in zip(self.param_groups, self._master_optimizer.param_groups, strict=True):
                for key, value in model_group.items():
                    if key != "params":
                        master_group[key] = value

        @torch_module.no_grad()
        def step(self, closure: Any = None) -> Any:
            self._sync_hyperparameters()
            for model_parameter, master_parameter, _ in self._pairs:
                master_parameter.grad = None if model_parameter.grad is None else model_parameter.grad.detach().to(dtype=torch_module.float32)
            loss = self._master_optimizer.step(closure)
            for model_parameter, master_parameter, _ in self._pairs:
                model_parameter.copy_(master_parameter.to(dtype=model_parameter.dtype))
            return loss

        def state_dict(self) -> dict[str, Any]:
            return self._master_optimizer.state_dict()

        def load_state_dict(self, state_dict: dict[str, Any]) -> None:
            self._master_optimizer.load_state_dict(state_dict)
            with torch_module.no_grad():
                for model_parameter, master_parameter, model_dtype in self._pairs:
                    # Checkpoint adapter values are authoritative after a resume.
                    master_parameter.copy_(model_parameter.detach().to(dtype=torch_module.float32))
                    model_parameter.data = model_parameter.data.to(dtype=model_dtype)

    return FP32MasterAdamW()
