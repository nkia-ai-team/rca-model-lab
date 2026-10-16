"""Guard against forward wrappers that cut gradient to layer inputs (2026-10-02: compressed-tensors activation fake quant)."""
import pytest

torch = pytest.importorskip("torch")

from rca_lab.train.checkpoint import assert_input_gradient_flows  # noqa: E402


class _Wrapped(torch.nn.Module):
    def __init__(self, blocked: bool) -> None:
        super().__init__()
        self.language_model = torch.nn.Sequential(torch.nn.Linear(4, 3, bias=False))
        for parameter in self.parameters():
            parameter.requires_grad_(False)
        if blocked:
            linear = self.language_model[0]
            original = linear.forward

            def no_grad_forward(x, _original=original):
                with torch.no_grad():
                    return _original(x)

            linear.forward = no_grad_forward


def test_plain_frozen_linear_passes():
    assert_input_gradient_flows(_Wrapped(blocked=False), torch_module=torch)


def test_gradient_blocking_wrapper_is_rejected():
    with pytest.raises(RuntimeError, match="does not require grad"):
        assert_input_gradient_flows(_Wrapped(blocked=True), torch_module=torch)
