"""Vectorized QSA indexer for the Flash training policy (2026-10-15 speed fix).

The Transformers reference indexer loops over every query token in Python: per query it calls nonzero (a GPU sync),
re-pools every visible key block, runs a matmul and a topk. A 40k-token turn costs 40k iterations per full-attention
layer, twice per turn (forward + checkpoint recompute), with O(T^2) re-pooling. The indexer output is a boolean mask
with no gradient path (top-k indices), so it runs under no_grad here.

For the training case (batch 1, no cache, boolean causal SDPA mask) this computes the same quantities once:
block keys are pooled/normalized/rotated once with the same ops, scores are FP32 matmuls of the same operands, the
per-query block count is (i+1)//compress_ratio, and the same min(block_topk, complete_blocks) blocks plus the tail
tokens are selected. Any other case falls back to the reference implementation. Install with install(policy).
"""
from __future__ import annotations

import math
import types

import torch

QUERY_CHUNK = 2048


def _causal_ok(attention_mask, T):
    if attention_mask.dim() != 4 or attention_mask.shape[0] != 1 or attention_mask.shape[-1] != T or attention_mask.shape[-2] != T:
        return False
    rows = attention_mask[0, 0].sum(-1)
    return bool(torch.equal(rows, torch.arange(1, T + 1, device=rows.device, dtype=rows.dtype)))


def make_forward(reference, apply_rope):
    def forward(self, hidden_states, position_embeddings, attention_mask, past_key_values):
        batch, T, _ = hidden_states.shape
        if past_key_values is not None or batch != 1 or attention_mask.dtype != torch.bool or not _causal_ok(attention_mask, T):
            return reference(hidden_states, position_embeddings, attention_mask, past_key_values)
        with torch.no_grad():
            hd, c = self.index_head_dim, self.compress_ratio
            full_cos, full_sin = position_embeddings
            current_cos, current_sin = full_cos[:, -T:, :], full_sin[:, -T:, :]
            qk = self.index_qk_proj(hidden_states)
            q, token_k = torch.split(qk, [self.index_n_heads * hd, self.index_kv_heads * hd], dim=-1)
            q, raw_keys = q.reshape(batch, T, -1, hd), token_k.reshape(batch, T, -1, hd).squeeze(2)
            q = self.q_layernorm(q)
            q = apply_rope(q, cos=current_cos, sin=current_sin, unsqueeze_dim=2)
            device = hidden_states.device
            NB = T // c
            mask = torch.zeros((T, T), dtype=torch.bool, device=device)
            positions = torch.arange(T, device=device)
            if NB > 0:
                blocks = raw_keys[0, :NB * c].view(NB, c, hd)
                pooled = self.k_layernorm(blocks.float().mean(dim=1).to(raw_keys.dtype))
                starts = torch.arange(NB, device=device) * c
                bk = apply_rope(pooled.unsqueeze(1), cos=full_cos[0].index_select(0, starts),
                                sin=full_sin[0].index_select(0, starts)).squeeze(1)
                bkT = bk.float().transpose(-1, -2)
                block_ids = torch.arange(NB, device=device)
                for s in range(0, T, QUERY_CHUNK):
                    e = min(s + QUERY_CHUNK, T)
                    scores = torch.matmul(q[0, s:e].float(), bkT)                      # [C, heads, NB]
                    scores = torch.relu(scores).transpose(-1, -2).sum(dim=-1) / math.sqrt(hd)  # [C, NB]
                    nb = (positions[s:e] + 1) // c
                    scores = scores.masked_fill(block_ids[None, :] >= nb[:, None], float("-inf"))
                    top = scores.topk(min(self.block_topk, NB), dim=-1)
                    picked = torch.where(torch.isfinite(top.values), top.indices, NB)
                    chosen = torch.zeros((e - s, NB + 1), dtype=torch.bool, device=device).scatter_(1, picked, True)[:, :NB]
                    mask[s:e, :NB * c] = chosen.repeat_interleave(c, dim=1)
            nb_all = (positions + 1) // c
            for s in range(0, T, QUERY_CHUNK):
                e = min(s + QUERY_CHUNK, T)
                tail = (positions[None, :] >= (nb_all[s:e] * c)[:, None]) & (positions[None, :] <= positions[s:e, None])
                mask[s:e] |= tail
        return mask.view(1, 1, T, T)
    return forward


def install(policy) -> dict:
    from transformers.models.qwen4_exp import modeling_qwen4_exp as mod
    count = 0
    for layer in policy.backbone.layers:
        attn = getattr(layer, "self_attn", None)
        if attn is None or not hasattr(attn, "indexer"):
            continue
        idx = attn.indexer
        idx.forward = types.MethodType(make_forward(idx.forward, mod.apply_rotary_pos_emb), idx)
        count += 1
    return {"mode": "vectorized_qsa_indexer", "indexers": count, "query_chunk": QUERY_CHUNK}
