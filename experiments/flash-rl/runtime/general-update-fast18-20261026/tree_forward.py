"""Exact prefix-shared gradient accumulation for the Flash training policy (2026-10-15 speedup).

The per-turn path forwards every turn's whole context (prompt_j + completion_j) from position 0. Turns of one episode
share long prefixes, so u2 forwarded 7.58M tokens for 2.62M distinct prefix positions. This module computes each shared
prefix once and still produces exactly the sum of the per-turn gradients:

* tree_plan builds a trie of the turns; each node is a segment [start, end) whose context is its ancestors' tokens.
* Every decoder module already reads/writes per-layer state through the Transformers cache API (GatedDeltaNet conv and
  recurrent state, PLE conv state and n-gram id context, full-attention K/V and QSA indexer keys). TreeCache implements
  that API functionally: a segment receives its ancestors' state as fixed input tensors, update_* return
  cat(prefix, new) without mutating anything, and the first value written per (layer, kind) is captured as the
  segment's own output state. Non-reentrant checkpoint recomputation calls update_* again; those writes are ignored.
* Gradients: the loss of a turn depends on its ancestors' states. Children run first (post-order); the gradient each
  child produces on its prefix-state inputs is accumulated onto the ancestor that produced that state, and the ancestor's
  own backward injects the accumulated cotangents into its captured output states. By linearity this equals the sum of
  the per-turn backward passes (up to floating-point summation order).
* Internal nodes are first forwarded without gradient (phase 1) only to produce the state their children need; their
  differentiable forward + backward (phase 2) happens after all their descendants.

Only the computation schedule changes: same modules, same kernels, same weights, same adapter format.
"""
from __future__ import annotations

import functools
import math
import os
import time
import types

import torch
from torch import nn
import torch.nn.functional as F

import tree_plan


# ----------------------------------------------------------------------------------------------- functional cache
class _LayerView:
    """What modules read via cache.layers[i]: conv_states[state_idx], recurrent_states[0], record_past."""
    record_past = True   # never route a segment through the single-token decode kernels

    def __init__(self, prefix):
        self.conv_states = prefix.get("conv", {}) if prefix else {}
        self.recurrent_states = {0: prefix["rec"]} if prefix and "rec" in prefix else {}


class _Layers:
    def __init__(self, cache):
        self.cache = cache

    def __getitem__(self, idx):
        return _LayerView(self.cache.prefix.get(idx) if self.cache.start > 0 else None)


class TreeCache:
    """Stand-in for a Transformers cache, valid for ONE segment forward at absolute positions [start, start + L)."""

    is_tree_cache = True

    def __init__(self, start: int, prefix: dict):
        self.start = start
        self.prefix = prefix          # layer_idx -> {"k","v","idx","rec", "conv": {state_idx: t}}
        self.out = {}                 # (layer_idx, kind[, state_idx]) -> captured segment output
        self.layers = _Layers(self)

    def get_seq_length(self, layer_idx: int = 0) -> int:
        return self.start

    def has_previous_state(self, layer_idx=None, state_idx=None) -> bool:
        return self.start > 0

    def _capture(self, key, value):
        self.out.setdefault(key, value)

    def update_conv_state(self, x, layer_idx, state_idx=0, conv_kernel_size=None, **kwargs):
        prev = self.prefix[layer_idx]["conv"][state_idx] if self.start > 0 else None
        if prev is None:   # Transformers prefill semantics (record_past=False)
            full = x if x.shape[-1] >= conv_kernel_size else F.pad(x, (conv_kernel_size - x.shape[-1], 0), value=0)
        else:
            full = torch.cat([prev.to(x.dtype) if prev.dtype != x.dtype and x.is_floating_point() else prev, x], dim=-1)
        self._capture((layer_idx, "conv", state_idx), full[..., -conv_kernel_size:])
        return full

    def update_recurrent_state(self, state, layer_idx, state_idx=0, **kwargs):
        self._capture((layer_idx, "rec"), state)
        return state

    def update(self, key_states, value_states, layer_idx, *args, **kwargs):
        self._capture((layer_idx, "k"), key_states)
        self._capture((layer_idx, "v"), value_states)
        if self.start == 0:
            return key_states, value_states
        p = self.prefix[layer_idx]
        return torch.cat([p["k"], key_states], dim=-2), torch.cat([p["v"], value_states], dim=-2)

    def update_indexer(self, raw_keys, layer_idx):
        self._capture((layer_idx, "idx"), raw_keys)
        if self.start == 0:
            return raw_keys
        return torch.cat([self.prefix[layer_idx]["idx"], raw_keys], dim=-2)


# ----------------------------------------------------------------------- offset-aware indexer and chunked attention
def install_tree_patches(policy) -> dict:
    """Extend the vectorized QSA indexer and the causal-chunked SDPA to segments whose queries start at an offset.
    Call after fast_indexer.install and chunked_attention.install (the plain per-turn path is unchanged)."""
    from transformers.models.qwen4_exp import modeling_qwen4_exp as mod
    from transformers.modeling_utils import ALL_ATTENTION_FUNCTIONS
    apply_rope = mod.apply_rotary_pos_emb
    count = 0
    for layer in policy.backbone.layers:
        attn = getattr(layer, "self_attn", None)
        if attn is None or not hasattr(attn, "indexer"):
            continue
        idx = attn.indexer
        previous = idx.forward
        idx.forward = types.MethodType(_tree_indexer(previous, apply_rope), idx)
        count += 1
    original = ALL_ATTENTION_FUNCTIONS["sdpa"]
    if not getattr(original, "_tree_offset", False):
        ALL_ATTENTION_FUNCTIONS["sdpa"] = _offset_chunked(original)
    return {"tree_indexers": count, "offset_chunked_sdpa": True}


INDEXER_QUERY_CHUNK = 2048
ATTN_CHUNK = 1024     # offset segments attend to up to ~60k keys; keep per-chunk SDPA temporaries small
KV_ALIGN = 64         # pad keys to an aligned length (masked) so SDPA keeps its memory-efficient kernel


def _tree_indexer(previous, apply_rope):
    def forward(self, hidden_states, position_embeddings, attention_mask, past_key_values):
        if not getattr(past_key_values, "is_tree_cache", False):
            return previous(hidden_states, position_embeddings, attention_mask, past_key_values)
        with torch.no_grad():   # top-k selection: boolean mask, no gradient path (same as the reference)
            batch, L, _ = hidden_states.shape
            assert batch == 1
            start = past_key_values.start
            T = start + L
            hd, c = self.index_head_dim, self.compress_ratio
            full_cos, full_sin = position_embeddings
            current_cos, current_sin = full_cos[:, -L:, :], full_sin[:, -L:, :]
            qk = self.index_qk_proj(hidden_states)
            q, token_k = torch.split(qk, [self.index_n_heads * hd, self.index_kv_heads * hd], dim=-1)
            q, raw_keys = q.reshape(batch, L, -1, hd), token_k.reshape(batch, L, -1, hd).squeeze(2)
            q = self.q_layernorm(q)
            q = apply_rope(q, cos=current_cos, sin=current_sin, unsqueeze_dim=2)
            keys = past_key_values.update_indexer(raw_keys, self.layer_idx)      # [1, T, hd]
            device = hidden_states.device
            NB = T // c
            mask = torch.zeros((L, T), dtype=torch.bool, device=device)
            qpos = torch.arange(start, T, device=device)
            kpos = torch.arange(T, device=device)
            if NB > 0:
                blocks = keys[0, :NB * c].view(NB, c, hd)
                pooled = self.k_layernorm(blocks.float().mean(dim=1).to(keys.dtype))
                starts = torch.arange(NB, device=device) * c
                bk = apply_rope(pooled.unsqueeze(1), cos=full_cos[0].index_select(0, starts),
                                sin=full_sin[0].index_select(0, starts)).squeeze(1)
                bkT = bk.float().transpose(-1, -2)
                block_ids = torch.arange(NB, device=device)
                for s in range(0, L, INDEXER_QUERY_CHUNK):
                    e = min(s + INDEXER_QUERY_CHUNK, L)
                    scores = torch.matmul(q[0, s:e].float(), bkT)
                    scores = torch.relu(scores).transpose(-1, -2).sum(dim=-1) / math.sqrt(hd)
                    nb = (qpos[s:e] + 1) // c
                    scores = scores.masked_fill(block_ids[None, :] >= nb[:, None], float("-inf"))
                    top = scores.topk(min(self.block_topk, NB), dim=-1)
                    picked = torch.where(torch.isfinite(top.values), top.indices, NB)
                    chosen = torch.zeros((e - s, NB + 1), dtype=torch.bool, device=device).scatter_(1, picked, True)[:, :NB]
                    mask[s:e, :NB * c] = chosen.repeat_interleave(c, dim=1)
            nb_all = (qpos + 1) // c
            for s in range(0, L, INDEXER_QUERY_CHUNK):
                e = min(s + INDEXER_QUERY_CHUNK, L)
                tail = (kpos[None, :] >= (nb_all[s:e] * c)[:, None]) & (kpos[None, :] <= qpos[s:e, None])
                mask[s:e] |= tail
        return mask.view(1, 1, L, T)
    return forward


def _offset_chunked(original):
    """Queries [n, n+L) over keys [0, n+L), same math as transformers' sdpa_attention_forward (repeat_kv + SDPA with the
    boolean mask, is_causal=False) but: the GQA key/value expansion is done ONCE per call and every query chunk uses a
    view of it (the per-chunk expansion of the stock path kept one 24-head copy of ~40k keys alive per chunk during
    recomputation: OOM on long segments), chunk [s, e) only reads keys [0, n+e) (the rest is causally masked), and key
    lengths are padded to KV_ALIGN with masked keys so SDPA keeps its memory-efficient kernel. Other shapes pass through."""
    def wrapped(module, query, key, value, attention_mask, *args, **kwargs):
        L, T = query.shape[2], key.shape[2]
        if (attention_mask is None or attention_mask.dtype != torch.bool or query.shape[0] != 1 or T == L
                or attention_mask.shape[-2] != L or attention_mask.shape[-1] != T):
            return original(module, query, key, value, attention_mask, *args, **kwargs)
        from transformers.integrations.sdpa_attention import repeat_kv
        n = T - L
        total = -(-T // KV_ALIGN) * KV_ALIGN
        k, v = key, value
        if total != T:
            k = F.pad(k, (0, 0, 0, total - T)); v = F.pad(v, (0, 0, 0, total - T))
        groups = getattr(module, "num_key_value_groups", 1)
        if groups > 1:
            k, v = repeat_kv(k, groups), repeat_kv(v, groups)
        scaling, dropout = kwargs.get("scaling"), kwargs.get("dropout", 0.0)
        outputs = []
        for s in range(0, L, ATTN_CHUNK):
            e = min(s + ATTN_CHUNK, L)
            kv = n + e
            padded = -(-kv // KV_ALIGN) * KV_ALIGN
            m = attention_mask[..., s:e, :kv]
            if padded != kv:
                m = F.pad(m, (0, padded - kv), value=False)
            out = F.scaled_dot_product_attention(query[:, :, s:e], k[:, :, :padded], v[:, :, :padded], attn_mask=m.contiguous(),
                                                 dropout_p=dropout, scale=scaling, is_causal=False)
            outputs.append(out.transpose(1, 2))
        return torch.cat(outputs, dim=1).contiguous(), None
    wrapped._tree_offset = True
    wrapped._chunked_sdpa = getattr(original, "_chunked_sdpa", False)
    return wrapped


# ------------------------------------------------------------------------------------------------ segment forward
def segment_hidden(policy, ids: torch.Tensor, cache: TreeCache) -> torch.Tensor:
    """Backbone forward of one segment (absolute positions [cache.start, cache.start + L)); returns the mixed hidden
    states on the first device. Mirrors Qwen4ExpTextModel.forward for batch 1, no padding, text positions."""
    bb = policy.backbone
    device = bb.embed_tokens.weight.device
    L, start = ids.shape[1], cache.start
    emb = bb.embed_tokens(ids)
    positions = torch.arange(start + L, device=device)
    position_embeddings = bb.rotary_emb(emb, positions.view(1, 1, -1).expand(3, 1, -1))
    query_positions = torch.arange(start, start + L, device=device)
    mask = (positions[None, :] <= query_positions[:, None]).view(1, 1, L, start + L)
    hidden = emb.repeat(1, 1, bb.config.hc_count)
    grad = torch.is_grad_enabled()
    for layer in bb.layers:
        call = functools.partial(nn.Module.__call__, layer, position_embeddings=position_embeddings, attention_mask=mask,
                                 conv_mask=None, past_key_values=cache, ple_input_ids=ids)
        # GradientCheckpointingLayer.__call__ would drop past_key_values under checkpointing; call the (offload-wrapped)
        # checkpoint function directly. Phase-1 forwards run without gradient and need no checkpoint.
        hidden = layer._gradient_checkpointing_func(call, hidden) if grad and getattr(layer, "gradient_checkpointing", False) else call(hidden)
    return bb.hyper_connection_mixer(hidden)


def tail_logprobs(policy, hidden: torch.Tensor, ids: torch.Tensor, chunk_size: int = 16) -> torch.Tensor:
    """log p(ids[t+1] | positions <= t) for t in [0, L-1): the turn's completion tokens. Same projection and chunked
    checkpointing as FlashPolicy.log_probs."""
    from torch.utils.checkpoint import checkpoint

    def project(h, labels):
        return policy.lm_head(h).float().log_softmax(-1).gather(-1, labels.unsqueeze(-1)).squeeze(-1)
    pieces = []
    for s in range(0, ids.shape[1] - 1, chunk_size):
        e = min(s + chunk_size, ids.shape[1] - 1)
        h, labels = hidden[:, s:e], ids[:, s + 1:e + 1]
        pieces.append(checkpoint(project, h, labels, use_reentrant=False) if torch.is_grad_enabled() else project(h, labels))
    return torch.cat(pieces, dim=1)


# ------------------------------------------------------------------------------------------------- tree schedule
class EpisodeTree:
    """Accumulate the exact gradient of sum_j turn_loss(j, logprobs_j) for one episode into the policy's .grad.

    turn_loss(j, logprobs) must return a scalar (already weighted); it is called once per turn, in tail order, and may
    record bookkeeping (e.g. the detached logprobs for drift audits)."""

    def __init__(self, policy, turns: list[dict], *, layer_devices: list[torch.device], store_device="cpu",
                 min_segment: int = tree_plan.MIN_SEGMENT):
        self.policy = policy
        self.turns = turns
        self.nodes = tree_plan.build(turns, min_segment=min_segment)
        self.stats = tree_plan.check(self.nodes, turns)
        bb = policy.backbone
        self.full = [i for i, l in enumerate(bb.layers) if l.layer_type != "linear_attention"]
        self.devices = layer_devices
        self.first = torch.device(bb.embed_tokens.weight.device)
        self.store = torch.device(store_device)
        self.debug = bool(os.environ.get("TREE_DEBUG"))
        self.saved = {}     # node -> {"kv": {l: (k, v)}, "idx": {l: t}, "end": {key: t}}
        self.acc = {}       # node -> {"kv": {l: (gk, gv)} fp32, "end": {key: g} fp32}

    # -- prefix construction
    def _chain(self, v):
        chain, p = [], self.nodes[v].parent
        while p is not None:
            chain.append(p)
            p = self.nodes[p].parent
        return chain[::-1]          # root ... parent

    def _prefix(self, v, grad: bool):
        node = self.nodes[v]
        if node.start == 0:
            return {}, {}
        chain = self._chain(v)
        parent = chain[-1]
        prefix, leaves = {}, {}
        for l, layer in enumerate(self.policy.backbone.layers):
            dev = self.devices[l]
            entry = {}
            if l in self.full:
                # torch.cat always allocates, so these are fresh leaves even for a single-ancestor chain
                k = torch.cat([self.saved[a]["kv"][l][0] for a in chain], dim=-2).to(dev, non_blocking=True)
                vv = torch.cat([self.saved[a]["kv"][l][1] for a in chain], dim=-2).to(dev, non_blocking=True)
                entry["idx"] = torch.cat([self.saved[a]["idx"][l] for a in chain], dim=-2).to(dev, non_blocking=True)
                if grad:
                    k.requires_grad_(); vv.requires_grad_()
                    leaves[(l, "k")], leaves[(l, "v")] = k, vv
                entry["k"], entry["v"] = k, vv
            conv = {}
            for key, t in self.saved[parent]["end"].items():
                if key[0] != l:
                    continue
                differentiable = grad and t.is_floating_point()
                # a differentiable input must be a fresh leaf per child: .to() returns the stored tensor itself when the
                # device/dtype already match, and siblings would then accumulate into one shared .grad
                t = t.to(dev, non_blocking=True, copy=differentiable)
                if differentiable:
                    t.requires_grad_()
                    leaves[key] = t
                if key[1] == "rec":
                    entry["rec"] = t
                else:
                    conv[key[2]] = t
            if conv:
                entry["conv"] = conv
            prefix[l] = entry
        return prefix, leaves

    # -- phase 1
    def _store(self, v, cache):
        kv = {l: (cache.out[(l, "k")].detach().to(self.store), cache.out[(l, "v")].detach().to(self.store)) for l in self.full}
        idx = {l: cache.out[(l, "idx")].detach().to(self.store) for l in self.full}
        end = {key: t.detach().to(self.store) for key, t in cache.out.items() if key[1] in ("conv", "rec")}
        self.saved[v] = {"kv": kv, "idx": idx, "end": end}

    def _forward_state(self, v):
        node = self.nodes[v]
        prefix, _ = self._prefix(v, grad=False)
        cache = TreeCache(node.start, prefix)
        ids = torch.tensor([node.tokens], dtype=torch.long, device=self.first)
        with torch.no_grad():
            segment_hidden(self.policy, ids, cache)
        self._store(v, cache)

    # -- phase 2
    def _backward(self, v, turn_loss):
        node = self.nodes[v]
        prefix, leaves = self._prefix(v, grad=True)
        cache = TreeCache(node.start, prefix)
        ids = torch.tensor([node.tokens], dtype=torch.long, device=self.first)
        hidden = segment_hidden(self.policy, ids, cache)
        roots, cots = [], []
        if node.turn is not None:
            # a tail may start before its last prompt token when short internal nodes were merged into it; score only
            # the turn's completion: logits at positions [prompt_len - 1, end - 1)
            offset = node.prompt_len - 1 - node.start
            loss = turn_loss(node.turn, tail_logprobs(self.policy, hidden[:, offset:], ids[:, offset:]))
            roots.append(loss); cots.append(torch.ones_like(loss))
        acc = self.acc.pop(v, None)
        hooks = []
        if acc is not None:
            injections = [(cache.out[(l, kind)], g) for l, (gk, gv) in acc["kv"].items() for kind, g in (("k", gk), ("v", gv))]
            injections += [(cache.out[key], g) for key, g in acc["end"].items()]
            # The children's cotangents are added by gradient hooks where the backward naturally reaches each captured
            # tensor, and the backward starts from this segment's output, so autograd walks the checkpointed layers
            # strictly top-down. Passing the captured tensors as separate roots let the per-device autograd threads start
            # recomputing every lower layer at once (all GPU0 layers' recomputed activations alive together: OOM in u6).
            anchor = hidden.float().sum() * 0.0
            for out, g in injections:
                gg = g.to(out.device, out.dtype, non_blocking=True)
                hooks.append(out.register_hook(lambda grad, gg=gg: grad + gg))
                # zero-weighted use: tensors off the output path (final recurrent state, conv windows) still receive a
                # gradient so their hook runs; this use needs no saved activations, so it triggers no recomputation
                anchor = anchor + (out.float().sum() * 0.0).to(anchor.device)
            roots.append(anchor); cots.append(torch.ones_like(anchor))
        del hidden
        if roots:
            torch.autograd.backward(roots, cots)
        for h in hooks:
            h.remove()
        # Break the reference cycle: the checkpoint frames keep the layer call (functools.partial holding this cache),
        # and the cache's captured tensors keep those frames alive through their grad_fn. Python GC cannot see the C++
        # edges, so without clearing, every segment's graph (prefix leaves, grads, recompute buffers) leaked (u6 OOM).
        cache.out.clear(); cache.prefix.clear(); prefix.clear()
        del roots, cots, cache, hooks
        self._distribute(v, leaves)

    def _distribute(self, v, leaves):
        if not leaves:
            return
        chain = self._chain(v)
        parent = chain[-1]
        for key, leaf in leaves.items():
            g = leaf.grad
            if g is None:
                continue
            g = g.detach().to(self.store, torch.float32)
            if key[1] in ("k", "v"):
                l, pos = key[0], 0
                for a in chain:
                    n = self.saved[a]["kv"][l][0].shape[-2]
                    slot = self.acc.setdefault(a, {"kv": {}, "end": {}})["kv"]
                    gk, gv = slot.get(l, (None, None))
                    part = g[..., pos:pos + n, :]
                    if key[1] == "k":
                        gk = part.clone() if gk is None else gk.add_(part)
                    else:
                        gv = part.clone() if gv is None else gv.add_(part)
                    slot[l] = (gk, gv)
                    pos += n
            else:
                end = self.acc.setdefault(parent, {"kv": {}, "end": {}})["end"]
                end[key] = g.clone() if key not in end else end[key].add_(g)

    def _tidy(self, v, phase, t0):
        if self.first.type == "cuda":
            torch.cuda.empty_cache()   # release freed blocks between segments (long-context segments fragment memory)
            if self.debug:
                node = self.nodes[v]
                peak = [round(torch.cuda.max_memory_allocated(d) / 2**30, 1) for d in range(torch.cuda.device_count())]
                live = [round(torch.cuda.memory_allocated(d) / 2**30, 1) for d in range(torch.cuda.device_count())]
                print(f"[tree] node {v} {phase} start {node.start} len {len(node.tokens)} turn {node.turn} "
                      f"{time.time() - t0:.1f}s peak {peak} live {live}", flush=True)

    def run(self, turn_loss):
        roots = [i for i, n in enumerate(self.nodes) if n.parent is None]

        def visit(v):
            node = self.nodes[v]
            if node.turn is None:
                t0 = time.time()
                self._forward_state(v)
                self._tidy(v, "state", t0)
                for c in node.children:
                    visit(c)
            t0 = time.time()
            self._backward(v, turn_loss)
            self._tidy(v, "grad", t0)
            if node.turn is None:
                self.saved.pop(v, None)
        for r in roots:
            visit(r)
        if self.saved or self.acc:
            raise RuntimeError("tree schedule left state behind")
