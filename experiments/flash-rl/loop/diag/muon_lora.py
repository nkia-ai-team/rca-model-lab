"""Muon for LoRA factors (2026-10-27, user: "muon도 쓰는게좋겠는데").

The base (Qwen3.8-Next family) was reportedly pre-trained with Muon (arXiv 2608.30320 abstract), while the loop fine-tunes
its LoRA with AdamW. This optimizer applies Muon to the 2-D LoRA factors:
  momentum m <- mu*m + g; direction = g + mu*m (Nesterov); O = NewtonSchulz5(direction) (approximate orthogonalisation);
  p <- p - lr * (rms_scale * sqrt(max(rows, cols)) * O + weight_decay * p)
with rms_scale 0.2 so the update RMS matches a typical AdamW update (Moonlight, arXiv 2502.16982), letting the AdamW learning
rate carry over. Matrices of the same shape are orthogonalised in one batched call (the 512 routed-expert factors of a
layer/projection share a shape). Factors whose smaller dimension is below `min_dim` can be routed to an AdamW fallback
(`adamw_params`), because orthogonalising a rank-2 factor mostly equalises its two singular values.

state_dict()/load_state_dict() follow torch.optim conventions (per-parameter momentum buffers + param_groups); the loop
re-applies the configured lr after loading, as for AdamW.
"""
from __future__ import annotations

import math
from collections import defaultdict

import torch

NS_COEFFS = (3.4445, -4.7750, 2.0315)   # Keller Jordan's quintic iteration


@torch.no_grad()
def newton_schulz5(g: torch.Tensor, steps: int = 5, eps: float = 1e-7) -> torch.Tensor:
    """Batched approximate orthogonalisation of [B, m, n] (or [m, n]) matrices; returns the same shape."""
    squeeze = g.dim() == 2
    x = g.unsqueeze(0) if squeeze else g
    x = x.float()
    transpose = x.shape[-2] > x.shape[-1]
    if transpose:
        x = x.transpose(-2, -1)
    x = x / (x.flatten(1).norm(dim=1).view(-1, 1, 1) + eps)
    a, b, c = NS_COEFFS
    for _ in range(steps):
        s = x @ x.transpose(-2, -1)
        x = a * x + (b * s + c * (s @ s)) @ x
    if transpose:
        x = x.transpose(-2, -1)
    return x.squeeze(0) if squeeze else x


class MuonLoRA(torch.optim.Optimizer):
    def __init__(self, params, lr=1e-4, momentum=0.95, nesterov=True, weight_decay=0.0, rms_scale=0.2, ns_steps=5):
        defaults = dict(lr=lr, momentum=momentum, nesterov=nesterov, weight_decay=weight_decay, rms_scale=rms_scale,
                        ns_steps=ns_steps)
        super().__init__(params, defaults)
        for group in self.param_groups:
            for p in group["params"]:
                if p.dim() != 2:
                    raise ValueError("MuonLoRA handles 2-D LoRA factors only")

    @torch.no_grad()
    def step(self, closure=None):
        loss = closure() if closure is not None else None
        for group in self.param_groups:
            buckets = defaultdict(list)   # (shape, device, dtype) -> params with a gradient
            for p in group["params"]:
                if p.grad is not None:
                    buckets[(tuple(p.shape), p.device, p.dtype)].append(p)
            for (shape, _, _), ps in buckets.items():
                grads = torch.stack([p.grad.float() for p in ps])
                bufs = []
                for p in ps:
                    st = self.state[p]
                    if "momentum_buffer" not in st:
                        st["momentum_buffer"] = torch.zeros_like(p, dtype=torch.float32)
                        st["step"] = torch.tensor(0.)
                    st["step"] += 1
                    bufs.append(st["momentum_buffer"])
                m = torch.stack(bufs)
                m.mul_(group["momentum"]).add_(grads)
                direction = grads + group["momentum"] * m if group["nesterov"] else m
                o = newton_schulz5(direction, steps=group["ns_steps"])
                scale = group["rms_scale"] * math.sqrt(max(shape))
                for i, p in enumerate(ps):
                    self.state[p]["momentum_buffer"].copy_(m[i])
                    if group["weight_decay"]:
                        p.mul_(1 - group["lr"] * group["weight_decay"])
                    p.add_(o[i].to(p.dtype), alpha=-group["lr"] * scale)
        return loss


class HybridMuonAdamW:
    """MuonLoRA for factors with min(shape) >= min_dim, AdamW for the rest; one step()/state_dict() surface."""

    def __init__(self, params, lr, min_dim=4, adam_betas=(0.9, 0.999), adam_eps=1e-8, weight_decay=0.0, **muon_kw):
        params = list(params)
        self.muon_params = [p for p in params if min(p.shape) >= min_dim]
        self.adam_params = [p for p in params if min(p.shape) < min_dim]
        self.muon = MuonLoRA(self.muon_params, lr=lr, weight_decay=weight_decay, **muon_kw) if self.muon_params else None
        self.adam = torch.optim.AdamW(self.adam_params, lr=lr, betas=adam_betas, eps=adam_eps, weight_decay=weight_decay) if self.adam_params else None

    @property
    def param_groups(self):
        return (self.muon.param_groups if self.muon else []) + (self.adam.param_groups if self.adam else [])

    def step(self):
        if self.muon:
            self.muon.step()
        if self.adam:
            self.adam.step()

    def state_dict(self):
        return {"muon": self.muon.state_dict() if self.muon else None, "adam": self.adam.state_dict() if self.adam else None}

    def load_state_dict(self, sd):
        if self.muon and sd.get("muon"):
            self.muon.load_state_dict(sd["muon"])
        if self.adam and sd.get("adam"):
            self.adam.load_state_dict(sd["adam"])
