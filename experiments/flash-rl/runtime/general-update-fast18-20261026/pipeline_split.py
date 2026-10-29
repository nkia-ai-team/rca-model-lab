"""Two-GPU layer split for the Flash training policy (2026-10-15).

The single-GPU runner ran out of memory at a ~50k-token turn (125 GiB allocated on one H200). This module keeps the
embeddings, rotary/mask construction, final norm and LM head on the first device and moves decoder layers
[split_at, num_layers) to the second device. It does not change any arithmetic: every module keeps its dtype and
kernels, only storage moves. Each moved layer's forward moves its inputs to that layer's device and runs under that device as the current CUDA
device (device-implicit Triton kernels launch there); the last layer returns the hidden state to the first device. Non-grad inputs (masks, rotary tables, PLE ids) are cached per source tensor so a
dense mask is copied once per forward, not once per layer.

CPU-resident tensors (PLE shards and the empty CPU marker buffer of ShardedEmbedding) are left on CPU.
"""
from __future__ import annotations

import weakref

import torch
from torch import nn


class _Mover:
    def __init__(self, device: torch.device, cache_size: int = 16):
        self.device = device
        self.cache: dict[int, tuple[weakref.ref, torch.Tensor]] = {}
        self.cache_size = cache_size
        self.copied_bytes = 0

    def move(self, value):
        if isinstance(value, torch.Tensor):
            if value.device == self.device or value.device.type == "cpu":
                return value
            if value.requires_grad:
                self.copied_bytes += value.numel() * value.element_size()
                return value.to(self.device)
            key = id(value)
            hit = self.cache.get(key)
            if hit is not None and hit[0]() is value and hit[1].shape == value.shape:
                return hit[1]
            moved = value.to(self.device)
            self.copied_bytes += value.numel() * value.element_size()
            if len(self.cache) >= self.cache_size:
                self.cache = {k: v for k, v in self.cache.items() if v[0]() is not None}
                while len(self.cache) >= self.cache_size:
                    self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (weakref.ref(value), moved)
            return moved
        if isinstance(value, tuple):
            return tuple(self.move(v) for v in value)
        if isinstance(value, list):
            return [self.move(v) for v in value]
        if isinstance(value, dict):
            return {k: self.move(v) for k, v in value.items()}
        return value


def _move_module(module: nn.Module, source: torch.device, target: torch.device) -> int:
    """Move every parameter/buffer/plain tensor attribute that lives on `source` to `target`; keep CPU tensors."""
    moved = 0
    for sub in module.modules():
        for name, param in list(sub._parameters.items()):
            if param is not None and param.device == source:
                param.data = param.data.to(target)
                moved += param.numel() * param.element_size()
        for name, buf in list(sub._buffers.items()):
            if buf is not None and buf.device == source:
                sub._buffers[name] = buf.to(target)
                moved += buf.numel() * buf.element_size()
        for name, value in list(vars(sub).items()):
            if name.startswith("_"):
                continue
            if isinstance(value, torch.Tensor) and not isinstance(value, nn.Parameter) and value.device == source:
                setattr(sub, name, value.to(target))
                moved += value.numel() * value.element_size()
    return moved


def leftover_tensors(module: nn.Module, device: torch.device) -> list[str]:
    """Names of tensors under `module` still on `device` (must be empty for a moved layer)."""
    names = []
    for mname, sub in module.named_modules():
        for name, t in list(sub._parameters.items()) + list(sub._buffers.items()):
            if t is not None and t.device == device:
                names.append(f"{mname}.{name}")
        for name, value in vars(sub).items():
            if not name.startswith("_") and isinstance(value, torch.Tensor) and value.device == device:
                names.append(f"{mname}.{name}")
    return names


def install(policy, *, split_at: int, devices=("cuda:0", "cuda:1")) -> dict:
    """Call after every forward patch (FLA, hyperconnections) and before activation offload installation.

    Each moved layer's bound `forward` is wrapped: inputs move to the layer device and the forward runs under
    `torch.cuda.device(...)` (a context manager, so the previous current device is restored in whichever thread
    runs it, including checkpoint recomputation inside the autograd engine). The last layer returns its output on
    the first device."""
    first, second = (torch.device(d) for d in devices)
    layers = policy.backbone.layers
    if not 0 < split_at < len(layers):
        raise ValueError("split_at must leave at least one layer on each device")
    if getattr(policy, "_pipeline_split", None) is not None:
        raise ValueError("pipeline split already installed")
    moved_bytes = 0
    mover = _Mover(second)
    originals = {}

    def wrap(index, original, last):
        def forward(*args, **kwargs):
            with torch.cuda.device(second):
                out = original(*mover.move(args), **mover.move(kwargs))
            if last:
                out = out.to(first) if isinstance(out, torch.Tensor) else type(out)(
                    o.to(first) if isinstance(o, torch.Tensor) else o for o in out)
            return out
        return forward

    for index in range(split_at, len(layers)):
        layer = layers[index]
        moved_bytes += _move_module(layer, first, second)
        left = leftover_tensors(layer, first)
        if left:
            raise RuntimeError(f"layer {index} kept tensors on {first}: {left[:5]}")
        originals[index] = layer.forward
        layer.forward = wrap(index, layer.forward, index == len(layers) - 1)
    policy._pipeline_split = dict(originals=originals, mover=mover)
    return {"mode": "two_gpu_layer_split", "split_at": split_at, "layers": len(layers),
            "devices": [str(first), str(second)], "moved_bytes": moved_bytes,
            "first_device_layers": split_at, "second_device_layers": len(layers) - split_at}


def stats(policy) -> dict:
    state = getattr(policy, "_pipeline_split", None)
    return {} if state is None else {"copied_bytes": state["mover"].copied_bytes}
