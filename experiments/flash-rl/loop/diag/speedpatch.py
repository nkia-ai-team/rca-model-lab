"""Training-speed patches for the Flash tree runner (2026-10-26, user: "학습 병렬화같은걸로 학습시간이나 업데이트시간도 좀 줄여봐").

Profiling showed both GPUs under 40% SM for 43% of training time with little PCIe traffic and ~1.5 CPU cores busy, i.e.
host-side overhead. Each patch keeps the arithmetic and is switched on separately (install(policy, flags)):
  empty    — torch.cuda.empty_cache() between tree segments only when >20 GiB sits reserved-but-unallocated on a device
  chunk    — LM-head log-prob chunks of 256 tokens instead of 16 (same checkpointed projection, fewer calls)
  stack    — routed-expert LoRA computed from one stacked leaf per (layer, projection) instead of torch.stack over 512
             expert parameters on every call; gradients are scattered back to the per-expert parameters before any use
             (flush) and the stacked leaves are refreshed after every optimizer step, so adapter format and optimizer
             state are unchanged
  gpustore — tree prefix states kept on each layer's own GPU instead of host memory (no host cat + pageable H2D per child)
"""
from __future__ import annotations

import functools

import torch

import grouped_experts
import tree_forward

_STACKS = []          # (bank, proj, A_leaf, B_leaf, mods)
_EMPTY_THRESHOLD = 20 * 2**30


def _install_empty():
    original = tree_forward.EpisodeTree._tidy

    def tidy(self, v, phase, t0):
        if self.first.type == "cuda" and any(torch.cuda.memory_reserved(d) - torch.cuda.memory_allocated(d) > _EMPTY_THRESHOLD
                                             for d in range(torch.cuda.device_count())):
            return original(self, v, phase, t0)
        if self.debug:
            return original(self, v, phase, t0)
    tree_forward.EpisodeTree._tidy = tidy


def _install_chunk(size=256):
    original = tree_forward.tail_logprobs
    tree_forward.tail_logprobs = functools.partial(original, chunk_size=size)


def _lora_stacked(bank, proj, xs, offs, e0=0, e1=None):
    entry = bank._stacked_lora[proj]
    a_leaf, b_leaf, scale = entry
    if scale == 0.:
        return None
    a = a_leaf[e0:e1].to(xs.dtype)
    b = b_leaf[e0:e1].to(xs.dtype)
    r = a.shape[1]
    F = torch.nn.functional
    at = F.pad(a.transpose(1, 2), (0, grouped_experts.RANK_PAD - r))
    bt = F.pad(b.transpose(1, 2), (0, 0, 0, grouped_experts.RANK_PAD - r))
    return torch._grouped_mm(torch._grouped_mm(xs, at, offs=offs), bt, offs=offs) * scale


def _install_stack(policy):
    for layer in policy.backbone.layers:
        bank = layer.mlp.experts
        bank._stacked_lora = {}
        for proj in grouped_experts.PROJECTIONS:
            mods = [e[proj] for e in bank.experts]
            a = torch.stack([m.lora_A.detach() for m in mods]).requires_grad_()
            b = torch.stack([m.lora_B.detach() for m in mods]).requires_grad_()
            bank._stacked_lora[proj] = (a, b, mods[0].scale)
            _STACKS.append((a, b, mods))
    grouped_experts._lora = _lora_stacked


@torch.no_grad()
def flush():
    """Move the stacked leaves' gradients onto the per-expert LoRA parameters (exact: d stack = split)."""
    for a, b, mods in _STACKS:
        for leaf, attr in ((a, "lora_A"), (b, "lora_B")):
            if leaf.grad is None:
                continue
            g = leaf.grad
            for i, m in enumerate(mods):
                p = getattr(m, attr)
                p.grad = g[i].clone() if p.grad is None else p.grad.add_(g[i])
            leaf.grad = None


@torch.no_grad()
def refresh():
    """Copy the per-expert LoRA values into the stacked leaves (after an optimizer step or a start-adapter load)."""
    for a, b, mods in _STACKS:
        a.copy_(torch.stack([m.lora_A for m in mods]))
        b.copy_(torch.stack([m.lora_B for m in mods]))


def _install_gpustore():
    def store(self, v, cache):
        dev = lambda l: self.devices[l]
        kv = {l: (cache.out[(l, "k")].detach().to(dev(l)), cache.out[(l, "v")].detach().to(dev(l))) for l in self.full}
        idx = {l: cache.out[(l, "idx")].detach().to(dev(l)) for l in self.full}
        end = {key: t.detach().to(dev(key[0])) for key, t in cache.out.items() if key[1] in ("conv", "rec")}
        self.saved[v] = {"kv": kv, "idx": idx, "end": end}
    tree_forward.EpisodeTree._store = store


def wrap_update(update_cls):
    """Flush stacked expert gradients before GeneralUpdate reads .grad, refresh the stacked values after a step."""
    for name in ("step_groups", "step_group", "release", "state"):
        original = getattr(update_cls, name, None)
        if original is None or getattr(original, "_speedpatch", False):
            continue

        def wrapped(self, *args, _original=original, _name=name, **kwargs):
            flush()
            out = _original(self, *args, **kwargs)
            if _name in ("step_groups", "step_group"):
                refresh()
            return out
        wrapped._speedpatch = True
        setattr(update_cls, name, wrapped)


def install(policy, flags) -> dict:
    flags = set(flags or ())
    unknown = flags - {"empty", "chunk", "stack", "gpustore"}
    if unknown:
        raise ValueError(f"unknown speed flags {sorted(unknown)}")
    if "empty" in flags:
        _install_empty()
    if "chunk" in flags:
        _install_chunk()
    if "stack" in flags:
        _install_stack(policy)
    if "gpustore" in flags:
        _install_gpustore()
    return {"flags": sorted(flags)}
