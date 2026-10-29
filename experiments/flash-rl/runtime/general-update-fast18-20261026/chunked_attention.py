"""Causal query-chunked SDPA for the Flash training policy (2026-10-15 speed fix).

With an arbitrary boolean mask (QSA selection & causal) cuDNN/flash SDPA computes every key tile, including the upper
triangle that the causal mask removes entirely; on a 61k-token turn attention was ~46% of fwd+bwd. Splitting queries
into chunks and giving chunk [s, e) only keys [0, e) skips those fully masked tiles (work ~ (C+1)/2C of dense) while
each query still attends to exactly the same unmasked keys through the unchanged Transformers SDPA path.
Applies only to batch-1, boolean-mask, q_len == kv_len calls longer than MIN_LENGTH; everything else passes through.
"""
from __future__ import annotations

import torch

CHUNK = 4096
MIN_LENGTH = 8192


def make_wrapper(original):
    def chunked(module, query, key, value, attention_mask, *args, **kwargs):
        T = query.shape[2]
        if (attention_mask is None or attention_mask.dtype != torch.bool or query.shape[0] != 1 or T < MIN_LENGTH
                or key.shape[2] != T or attention_mask.shape[-1] != T or attention_mask.shape[-2] != T):
            return original(module, query, key, value, attention_mask, *args, **kwargs)
        outputs = []
        for s in range(0, T, CHUNK):
            e = min(s + CHUNK, T)
            out, _ = original(module, query[:, :, s:e], key[:, :, :e], value[:, :, :e],
                              attention_mask[..., s:e, :e], *args, **kwargs)
            outputs.append(out)
        return torch.cat(outputs, dim=1), None
    chunked._chunked_sdpa = True
    return chunked


def install() -> dict:
    from transformers.modeling_utils import ALL_ATTENTION_FUNCTIONS
    original = ALL_ATTENTION_FUNCTIONS["sdpa"]
    if getattr(original, "_chunked_sdpa", False):
        raise ValueError("chunked SDPA already installed")
    ALL_ATTENTION_FUNCTIONS["sdpa"] = make_wrapper(original)
    if not getattr(ALL_ATTENTION_FUNCTIONS["sdpa"], "_chunked_sdpa", False):
        raise RuntimeError("attention registry override did not take effect")
    return {"mode": "causal_chunked_sdpa", "chunk": CHUNK, "min_length": MIN_LENGTH}
