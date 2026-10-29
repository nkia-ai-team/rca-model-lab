"""Long-context memory fix for the per-turn Flash training path (2026-10-15, runtime fast7).

Data split v3 brought turns of 67k-95k tokens; the per-turn path (peak 99 GiB at 61k) ran out of memory at ~67k. The
quadratic terms were:
  * three dense T x T boolean masks: the Transformers causal mask (built once, alive for the whole forward), the QSA
    indexer selection mask and their conjunction (each 9 GB at 95k tokens);
  * the causal-chunked SDPA calling transformers' sdpa_attention_forward per query chunk, which expands the 2 KV heads
    to 24 (repeat_kv) for every chunk; checkpoint recomputation keeps every chunk's expansion alive (>10 GB per layer).

This module computes the same quantities without them, keeping every numeric operation identical:
  * the full-attention forward is replaced (batch 1, no cache) by a query-chunked version: CHUNK=4096 queries, keys
    [0, e), exactly the chunks of chunked_attention; the chunk's mask is causal & QSA selection built for [C, e] only;
  * QSA scores use the same 2048-query matmuls as fast_indexer (identical shapes, so identical rounding and top-k);
  * K/V are expanded to 24 heads once per call and every chunk reads a view (same values as repeat_kv of the slice);
  * the backbone receives a mask dict so Transformers does not build the T x T causal mask (the GatedDeltaNet/PLE
    conv mask is None, as for unpadded batch-1 input).
Everything else (projections, norms, RoPE, gate, o_proj, SDPA call) is the stock module code path.
"""
from __future__ import annotations

import math
import types

import torch
import torch.nn.functional as F

CHUNK = 4096            # = chunked_attention.CHUNK
MIN_LENGTH = 8192       # = chunked_attention.MIN_LENGTH: shorter turns were one SDPA call in the stock path
INDEX_QUERY_CHUNK = 2048  # = fast_indexer.QUERY_CHUNK


class _LazyCausal:
    """Sentinel passed as the indexed-attention mask: 'plain causal, batch 1, no padding' (built per chunk)."""
    dtype = torch.bool

    def __repr__(self):
        return "LazyCausal"


LAZY = _LazyCausal()


def _selection(indexer, hidden_states, position_embeddings, apply_rope):
    """Per-call indexer state: rotated queries and rotated pooled block keys (same ops as fast_indexer)."""
    batch, T, _ = hidden_states.shape
    hd, c = indexer.index_head_dim, indexer.compress_ratio
    full_cos, full_sin = position_embeddings
    current_cos, current_sin = full_cos[:, -T:, :], full_sin[:, -T:, :]
    qk = indexer.index_qk_proj(hidden_states)
    q, token_k = torch.split(qk, [indexer.index_n_heads * hd, indexer.index_kv_heads * hd], dim=-1)
    q, raw_keys = q.reshape(batch, T, -1, hd), token_k.reshape(batch, T, -1, hd).squeeze(2)
    q = indexer.q_layernorm(q)
    q = apply_rope(q, cos=current_cos, sin=current_sin, unsqueeze_dim=2)
    NB = T // c
    bkT = None
    if NB > 0:
        blocks = raw_keys[0, :NB * c].view(NB, c, hd)
        pooled = indexer.k_layernorm(blocks.float().mean(dim=1).to(raw_keys.dtype))
        starts = torch.arange(NB, device=hidden_states.device) * c
        bk = apply_rope(pooled.unsqueeze(1), cos=full_cos[0].index_select(0, starts),
                        sin=full_sin[0].index_select(0, starts)).squeeze(1)
        bkT = bk.float().transpose(-1, -2)
    return q, bkT, NB


def _chunk_mask(indexer, q, bkT, NB, s, e, device):
    """Causal & QSA-selected keys for queries [s, e) over keys [0, e): rows equal fast_indexer's mask rows [s:e, :e]."""
    hd, c = indexer.index_head_dim, indexer.compress_ratio
    mask = torch.zeros((e - s, e), dtype=torch.bool, device=device)
    positions = torch.arange(e, device=device)
    if NB > 0:
        block_ids = torch.arange(NB, device=device)
        for a in range(s, e, INDEX_QUERY_CHUNK):
            b = min(a + INDEX_QUERY_CHUNK, e)
            scores = torch.matmul(q[0, a:b].float(), bkT)
            scores = torch.relu(scores).transpose(-1, -2).sum(dim=-1) / math.sqrt(hd)
            nb = (positions[a:b] + 1) // c
            scores = scores.masked_fill(block_ids[None, :] >= nb[:, None], float("-inf"))
            top = scores.topk(min(indexer.block_topk, NB), dim=-1)
            picked = torch.where(torch.isfinite(top.values), top.indices, NB)
            chosen = torch.zeros((b - a, NB + 1), dtype=torch.bool, device=device).scatter_(1, picked, True)[:, :NB]
            width = min(NB * c, e)
            mask[a - s:b - s, :width] = chosen.repeat_interleave(c, dim=1)[:, :width]
    nb_all = (positions[s:e] + 1) // c
    tail = (positions[None, :] >= (nb_all * c)[:, None]) & (positions[None, :] <= positions[s:e, None])
    mask |= tail
    return mask.view(1, 1, e - s, e)


def _attention_forward(apply_rope, original):
    def forward(self, hidden_states, position_embeddings, attention_mask, past_key_values=None, **kwargs):
        if attention_mask is not LAZY or past_key_values is not None or hidden_states.shape[0] != 1:
            if attention_mask is LAZY:
                raise ValueError("LazyCausal mask reached an unsupported attention call")
            return original(hidden_states, position_embeddings, attention_mask, past_key_values, **kwargs)
        T = hidden_states.shape[1]
        device = hidden_states.device
        idx = self.indexer
        with torch.no_grad():
            iq, bkT, NB = _selection(idx, hidden_states, position_embeddings, apply_rope)
        position_embeddings = tuple(x[:, -T:, :] for x in position_embeddings)
        input_shape = hidden_states.shape[:-1]
        hidden_shape = (*input_shape, -1, self.head_dim)
        query_states, gate = torch.chunk(self.q_proj(hidden_states).view(*input_shape, -1, self.head_dim * 2), 2, dim=-1)
        gate = gate.reshape(*input_shape, -1)
        query_states = self.q_norm(query_states.view(hidden_shape)).transpose(1, 2)
        key_states = self.k_norm(self.k_proj(hidden_states).view(hidden_shape)).transpose(1, 2)
        value_states = self.v_proj(hidden_states).view(hidden_shape).transpose(1, 2)
        cos, sin = position_embeddings
        query_states, key_states = apply_rope(query_states, key_states, cos, sin)
        groups = getattr(self, "num_key_value_groups", 1)
        from transformers.integrations.sdpa_attention import repeat_kv
        k_all = repeat_kv(key_states, groups) if groups > 1 else key_states
        v_all = repeat_kv(value_states, groups) if groups > 1 else value_states
        dropout = 0.0 if not self.training else self.attention_dropout
        outputs = []
        step = CHUNK if T >= MIN_LENGTH else T
        for s in range(0, T, step):
            e = min(s + step, T)
            with torch.no_grad():
                m = _chunk_mask(idx, iq, bkT, NB, s, e, device)
            out = F.scaled_dot_product_attention(query_states[:, :, s:e], k_all[:, :, :e], v_all[:, :, :e], attn_mask=m,
                                                 dropout_p=dropout, scale=self.scaling, is_causal=False)
            outputs.append(out.transpose(1, 2))
        attn_output = torch.cat(outputs, dim=1).reshape(*input_shape, -1).contiguous()
        attn_output = attn_output * torch.sigmoid(gate)
        attn_output = self.o_proj(attn_output)
        return attn_output, None
    return forward


def install(policy) -> dict:
    """Replace full-attention forwards (self_attn) and make the backbone skip the dense causal mask for plain
    batch-1 calls. Install after the other attention/indexer patches."""
    from transformers.models.qwen4_exp import modeling_qwen4_exp as mod
    apply_rope = mod.apply_rotary_pos_emb
    count = 0
    for layer in policy.backbone.layers:
        attn = getattr(layer, "self_attn", None)
        if attn is None:
            continue
        attn.forward = types.MethodType(_attention_forward(apply_rope, attn.forward), attn)
        count += 1
    backbone = policy.backbone
    original = backbone.forward

    def forward(*args, attention_mask=None, past_key_values=None, use_cache=None, **kwargs):
        if attention_mask is None and past_key_values is None and not use_cache:
            attention_mask = {"indexed_attention": LAZY, "linear_attention": None}
        return original(*args, attention_mask=attention_mask, past_key_values=past_key_values, use_cache=use_cache, **kwargs)
    backbone.forward = forward
    return {"mode": "long_context_chunked_attention", "attention_layers": count, "chunk": CHUNK, "index_query_chunk": INDEX_QUERY_CHUNK}


def compact_small_tensors(policy, limit_bytes: int = 1 << 20) -> dict:
    """Release caching-allocator memory pinned by load-time fragmentation (2026-10-15).

    load_policy allocates ~74k per-expert packed tensors (<1 MB, small-pool 2 MB segments) interleaved with the LoRA
    parameters and small norm weights. grouped_experts later stacks the expert tensors and frees the originals, but
    every 2 MB segment still holding a LoRA parameter cannot be returned: ~28.7 GiB per GPU stayed "reserved but
    unallocated" at all four long-turn OOMs. Moving the remaining small non-view tensors to the host, emptying the
    cache and moving them back packs them into fresh segments; values are copied unchanged."""
    import torch
    moved = []
    for module in policy.modules():
        for store in (module._parameters, module._buffers):
            for name, t in list(store.items()):
                if t is None or not t.is_cuda or t._base is not None or t.numel() * t.element_size() >= limit_bytes:
                    continue
                moved.append((store, name, t, t.device))
    before = {d: torch.cuda.memory_reserved(d) for d in range(torch.cuda.device_count())}
    host = []
    for store, name, t, device in moved:
        h = t.detach().to("cpu")
        if isinstance(t, torch.nn.Parameter):
            t.data = h
        else:
            store[name] = h
        host.append((store, name, t, device))
    torch.cuda.empty_cache()
    for store, name, t, device in host:
        if isinstance(t, torch.nn.Parameter):
            t.data = t.data.to(device)
        else:
            store[name] = store[name].to(device)
    torch.cuda.empty_cache()
    after = {d: torch.cuda.memory_reserved(d) for d in range(torch.cuda.device_count())}
    return {"moved_tensors": len(moved), "reserved_gib_before": {d: round(v / 2**30, 1) for d, v in before.items()},
            "reserved_gib_after": {d: round(v / 2**30, 1) for d, v in after.items()}}
