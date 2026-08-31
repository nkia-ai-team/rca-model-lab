from __future__ import annotations

import pytest

from rca_lab.train.precision import build_fp32_master_adamw, configure_lora_precision

torch = pytest.importorskip("torch")


class TinyPeftModel(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.base = torch.nn.Linear(2, 2, bias=False, dtype=torch.bfloat16)
        self.base.weight.requires_grad_(False)
        self.lora_A = torch.nn.Linear(2, 1, bias=False, dtype=torch.float32)
        self.lora_B = torch.nn.Linear(1, 2, bias=False, dtype=torch.float32)


def test_precision_contract_casts_only_lora_and_freezes_base() -> None:
    model = TinyPeftModel()

    evidence = configure_lora_precision(model, torch_module=torch)

    assert model.base.weight.requires_grad is False
    assert model.lora_A.weight.dtype == torch.bfloat16
    assert model.lora_B.weight.dtype == torch.bfloat16
    assert evidence["base_weights_frozen"] is True
    assert evidence["optimizer_state_dtype"] == "float32"


def test_fp32_master_adamw_updates_bf16_adapter_with_fp32_state() -> None:
    model = TinyPeftModel()
    configure_lora_precision(model, torch_module=torch)
    optimizer = build_fp32_master_adamw(
        model.parameters(),
        torch_module=torch,
        lr=0.01,
    )
    before = model.lora_B.weight.detach().clone()
    loss = model.lora_B(model.lora_A(torch.ones(1, 2, dtype=torch.bfloat16))).sum()
    loss.backward()

    optimizer.step()

    assert not torch.equal(before, model.lora_B.weight)
    state = optimizer.state_dict()["state"]
    assert state
    for values in state.values():
        assert values["exp_avg"].dtype == torch.float32
        assert values["exp_avg_sq"].dtype == torch.float32


def test_precision_contract_rejects_unexpected_trainable_base() -> None:
    model = TinyPeftModel()
    model.base.weight.requires_grad_(True)

    with pytest.raises(ValueError, match="non-LoRA parameters"):
        configure_lora_precision(model, torch_module=torch)
