"""Vectorized routed-expert forward for the Flash training policy (2026-10-15 speed fix).

The original `ExpertBank.forward` loops over every hit expert in Python (512 per layer), re-dequantizes each expert's
INT4 matrices per call and synchronizes on `torch.where`; a 40k-token turn issues millions of small kernels and the
GPU sits at ~20% utilization with one CPU core at 100%. This module keeps the same parameters, buffers, names and
math (bitwise-identical dequantization, same activation/accumulation dtypes) but:
  * stacks each projection's packed INT4 tensors once per bank (per-expert buffers become views: no extra memory),
  * dequantizes a whole projection per call in expert chunks,
  * runs frozen and LoRA projections with `torch._grouped_mm` over expert-sorted rows (offsets stay on the GPU),
  * keeps frozen weights out of autograd (input gradient re-dequantizes in backward, as FrozenPacked does).
Install AFTER pipeline_split.install so stacking happens on each bank's final device.
"""
from __future__ import annotations

import types

import torch
from torch.nn import functional as F

PROJECTIONS = ("gate_proj", "up_proj", "down_proj")
RANK_PAD = 8          # grouped GEMM needs 16-byte aligned inner dims; zero padding leaves LoRA math unchanged
DEQUANT_CHUNK = 64    # experts per dequantization chunk (bounds int32 temporaries, torch fallback only)
EXPERT_CHUNK = 128    # experts per grouped call: bounds row temporaries and dequantized weights (2026-10-15 OOM fix)
try:
    import triton_dequant
except ImportError:  # pragma: no cover - CPU-only environments
    triton_dequant = None


def _dequant_stacked(bank, proj, device, dtype, e0=0, e1=None):
    q = getattr(bank, f"grouped_{proj}_qweight")[e0:e1].to(device)     # [E, ni/8, no] int32
    z = getattr(bank, f"grouped_{proj}_qzeros")[e0:e1].to(device)      # [E, G, no/8] int32
    s = getattr(bank, f"grouped_{proj}_scales")[e0:e1].to(device)      # [E, G, no]
    if triton_dequant is not None and q.is_cuda:
        return triton_dequant.dequant_stacked(q, z, s, dtype)          # bitwise identical, one pass
    E, rows, no = q.shape
    ni, G = rows * 8, s.shape[1]
    out = torch.empty((E, ni, no), device=device, dtype=dtype)
    shifts = torch.arange(0, 32, 4, device=device, dtype=torch.int32)
    for start in range(0, E, DEQUANT_CHUNK):
        end = min(start + DEQUANT_CHUNK, E)
        w = ((q[start:end, :, None, :] >> shifts[None, None, :, None]) & 15).reshape(end - start, ni, no).to(dtype)
        zero = (((z[start:end, :, :, None] >> shifts[None, None, None, :]) & 15).reshape(end - start, G, no) + 1).to(dtype)
        scale = s[start:end].to(dtype)
        out[start:end] = ((w.view(end - start, G, 128, no) - zero[:, :, None, :]) * scale[:, :, None, :]).view(end - start, ni, no)
    return out


class _FrozenGrouped(torch.autograd.Function):
    @staticmethod
    def forward(ctx, xs, offs, bank, proj, e0, e1):
        ctx.bank, ctx.proj, ctx.e0, ctx.e1 = bank, proj, e0, e1
        ctx.save_for_backward(offs)
        weight = _dequant_stacked(bank, proj, xs.device, xs.dtype, e0, e1)
        return torch._grouped_mm(xs, weight, offs=offs)

    @staticmethod
    def backward(ctx, dy):
        (offs,) = ctx.saved_tensors
        weight = _dequant_stacked(ctx.bank, ctx.proj, dy.device, dy.dtype, ctx.e0, ctx.e1)
        return torch._grouped_mm(dy.contiguous(), weight.transpose(-2, -1), offs=offs), None, None, None, None, None


class _WeightedScatter(torch.autograd.Function):
    """out.index_add(0, tok, y.float() * w[:, None]) without keeping an FP32 copy of y for backward.
    Forward values are identical; backward uses the exact FP32 conversion of the saved BF16 y."""
    @staticmethod
    def forward(ctx, out, y, w, tok):
        ctx.save_for_backward(y, w, tok)
        return out.index_add(0, tok, y.float() * w[:, None])   # out-of-place: no dirty-input bookkeeping

    @staticmethod
    def backward(ctx, grad_out):
        y, w, tok = ctx.saved_tensors
        g = grad_out.index_select(0, tok)
        grad_y = (g * w[:, None]).to(y.dtype) if ctx.needs_input_grad[1] else None
        grad_w = (g * y.float()).sum(-1) if ctx.needs_input_grad[2] else None
        return grad_out, grad_y, grad_w, None


def _lora(bank, proj, xs, offs, e0=0, e1=None):
    mods = [e[proj] for e in bank.experts[e0:e1]]
    scale = mods[0].scale
    if scale == 0.:
        return None
    a = torch.stack([m.lora_A for m in mods]).to(xs.dtype)          # [E, r, ni]
    b = torch.stack([m.lora_B for m in mods]).to(xs.dtype)          # [E, no, r]
    r = a.shape[1]
    at = F.pad(a.transpose(1, 2), (0, RANK_PAD - r))                 # [E, ni, 8]
    bt = F.pad(b.transpose(1, 2), (0, 0, 0, RANK_PAD - r))           # [E, 8, no]
    return torch._grouped_mm(torch._grouped_mm(xs, at, offs=offs), bt, offs=offs) * scale


def grouped_forward(self, x, indices, weights):
    T, K = indices.shape
    E = len(self.experts)
    flat = indices.reshape(-1)
    order = torch.argsort(flat, stable=True)
    tok, slot = order // K, order % K
    offs = torch.cumsum(torch.bincount(flat, minlength=E), 0, dtype=torch.int32)
    bounds = [0] + offs.tolist()        # one host sync per layer call
    out = torch.zeros(x.shape, device=x.device, dtype=torch.float32)
    for e0 in range(0, E, EXPERT_CHUNK):
        e1 = min(e0 + EXPERT_CHUNK, E)
        r0, r1 = bounds[e0], bounds[e1]
        if r1 == r0:
            continue
        rows = slice(r0, r1)
        offs_c = offs[e0:e1] - r0
        xs = x[tok[rows]]

        def proj(name, inputs):
            y = _FrozenGrouped.apply(inputs, offs_c, self, name, e0, e1)
            lora = _lora(self, name, inputs, offs_c, e0, e1)
            return y if lora is None else y + lora

        gate, up = proj("gate_proj", xs), proj("up_proj", xs)
        activation = (F.silu(gate.float()) * up.float()).to(xs.dtype)
        y = proj("down_proj", activation)
        out = _WeightedScatter.apply(out, y, weights[tok[rows], slot[rows]].float(), tok[rows])
    return out.to(x.dtype)


def install_bank(bank) -> int:
    """Stack packed tensors per projection; per-expert buffers become views of the stacked buffer."""
    stacked_bytes = 0
    for proj in PROJECTIONS:
        mods = [e[proj] for e in bank.experts]
        for name in ("qweight", "qzeros", "scales"):
            parts = [m._buffers[name] for m in mods]
            device = parts[0].device
            if any(p.device != device for p in parts):
                raise ValueError("expert bank spans devices")
            stacked = torch.stack(parts)
            bank.register_buffer(f"grouped_{proj}_{name}", stacked)
            for i, m in enumerate(mods):
                m._buffers[name] = stacked[i]
            stacked_bytes += stacked.numel() * stacked.element_size()
    bank.forward = types.MethodType(grouped_forward, bank)
    return stacked_bytes


def install(policy) -> dict:
    banks = [layer.mlp.experts for layer in policy.backbone.layers]
    total = sum(install_bank(bank) for bank in banks)
    return {"mode": "grouped_mm_experts", "banks": len(banks), "stacked_bytes": total, "rank_pad": RANK_PAD,
            "dequant_chunk": DEQUANT_CHUNK, "expert_chunk": EXPERT_CHUNK, "triton_dequant": triton_dequant is not None}
