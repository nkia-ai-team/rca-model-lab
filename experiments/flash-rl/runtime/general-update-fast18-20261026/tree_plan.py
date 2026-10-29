"""Prefix-sharing plan for one episode's turns (2026-10-15 speedup, exact-gradient tree accumulation).

Every turn j is the token sequence prompt_j + completion_j. Consecutive prompts share long prefixes but are not prefixes of
each other (the harness truncates older tool results and the chat template drops earlier reasoning), so the plan is a
trie over the turns' shared-prefix keys:

* key_j = prompt_j[:-1]. The last prompt token and the completion form the turn's own "tail" node, so every position
  whose logits score turn j's completion is computed in that turn's own segment (and nothing in a tail is shared).
* trie nodes are maximal shared runs of key tokens; a node ends exactly where its children fork.
* nodes shorter than MIN_SEGMENT tokens are merged into their children (the children recompute those few tokens), so
  no segment is shorter than MIN_SEGMENT except a turn's tail of length >= 2. Single-token segments would route the
  linear-attention layers through the decode kernels instead of the chunked prefill kernels.

The plan only rearranges which positions are computed together; position p of turn j always sees exactly
prompt_j[:p] as context, as in the per-turn computation.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MIN_SEGMENT = 64


@dataclass
class Node:
    start: int                     # absolute position of the first token
    tokens: list[int]
    parent: int | None             # node index; None for a root
    children: list[int] = field(default_factory=list)
    turn: int | None = None        # set for a tail node: the turn whose completion it scores
    prompt_len: int = 0            # tail only: len(prompt_j)

    @property
    def end(self) -> int:
        return self.start + len(self.tokens)


def _lcp(a, b) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def build(turns: list[dict], min_segment: int = MIN_SEGMENT) -> list[Node]:
    """turns: dicts with prompt_token_ids / completion_token_ids. Returns the merged plan (parents before children)."""
    nodes: list[Node] = []
    roots: list[int] = []

    def insert(key: list[int]) -> int:
        """Insert key into the trie; return the index of the node whose end is exactly the end of key."""
        owner, pos = None, 0
        while True:
            level = roots if owner is None else nodes[owner].children
            match = next((i for i in level if nodes[i].tokens[0] == key[pos]), None)
            if match is None:
                idx = len(nodes)
                nodes.append(Node(start=pos, tokens=list(key[pos:]), parent=owner))
                level.append(idx)
                return idx
            node = nodes[match]
            n = _lcp(node.tokens, key[pos:])
            if n < len(node.tokens):       # the key diverges (or ends) inside this node: split it
                rest = len(nodes)
                nodes.append(Node(start=node.start + n, tokens=node.tokens[n:], parent=match, children=node.children))
                for c in nodes[rest].children:
                    nodes[c].parent = rest
                node.tokens, node.children = node.tokens[:n], [rest]
            pos += n
            if pos == len(key):
                return match
            owner = match

    def locate(key: list[int]) -> int:
        """Node whose end is the end of key. Later insertions may split the node a key first ended in, but a split
        keeps every boundary, so the key still ends exactly at some node's end."""
        owner, pos = None, 0
        while True:
            level = roots if owner is None else nodes[owner].children
            match = next(i for i in level if nodes[i].tokens[0] == key[pos])
            pos += len(nodes[match].tokens)
            if pos == len(key):
                return match
            if pos > len(key) or nodes[match].tokens != key[pos - len(nodes[match].tokens):pos]:
                raise AssertionError("key does not end at a node boundary")
            owner = match

    keys = []
    for t in turns:
        key = list(t["prompt_token_ids"][:-1])
        if not key:
            raise ValueError("prompt must have at least 2 tokens")
        insert(key)
        keys.append(key)
    ends = [locate(k) for k in keys]
    for j, t in enumerate(turns):
        p = t["prompt_token_ids"]
        idx = len(nodes)
        nodes.append(Node(start=len(p) - 1, tokens=[p[-1]] + list(t["completion_token_ids"]), parent=ends[j],
                          turn=j, prompt_len=len(p)))
        nodes[ends[j]].children.append(idx)
    return _order(_merge_short(nodes, min_segment))


def _order(nodes: list[Node]) -> list[Node]:
    """Renumber depth-first (parents before children)."""
    order, stack = [], [i for i, n in enumerate(nodes) if n.parent is None][::-1]
    while stack:
        i = stack.pop()
        order.append(i)
        stack.extend(nodes[i].children[::-1])
    remap = {old: new for new, old in enumerate(order)}
    return [Node(start=nodes[o].start, tokens=nodes[o].tokens, parent=None if nodes[o].parent is None else remap[nodes[o].parent],
                 children=[remap[c] for c in nodes[o].children], turn=nodes[o].turn, prompt_len=nodes[o].prompt_len) for o in order]


def _merge_short(nodes: list[Node], min_segment: int) -> list[Node]:
    """Push the tokens of short internal nodes down into each child (repeat until stable), then renumber."""
    changed = True
    while changed:
        changed = False
        for i, n in enumerate(nodes):
            if n.turn is not None or n.tokens is None or len(n.tokens) >= min_segment or not n.children:
                continue
            for c in n.children:
                nodes[c].tokens = n.tokens + nodes[c].tokens
                nodes[c].start = n.start
                nodes[c].parent = n.parent
            if n.parent is not None:
                sib = nodes[n.parent].children
                k = sib.index(i)
                nodes[n.parent].children = sib[:k] + n.children + sib[k + 1:]
            n.tokens, n.children = None, []
            changed = True
    keep = [i for i, n in enumerate(nodes) if n.tokens is not None]
    remap = {old: new for new, old in enumerate(keep)}
    out = []
    for old in keep:
        n = nodes[old]
        out.append(Node(start=n.start, tokens=n.tokens, parent=None if n.parent is None else remap[n.parent],
                        children=[remap[c] for c in n.children], turn=n.turn, prompt_len=n.prompt_len))
    return out


def sequence_of(nodes: list[Node], idx: int) -> list[int]:
    """Full token sequence from the root down to the end of node idx."""
    chain = []
    while idx is not None:
        chain.append(idx)
        idx = nodes[idx].parent
    seq = []
    for i in reversed(chain):
        assert nodes[i].start == len(seq), (i, nodes[i].start, len(seq))
        seq += nodes[i].tokens
    return seq


def check(nodes: list[Node], turns: list[dict]) -> dict:
    """Every turn's root-to-tail sequence must equal prompt + completion exactly."""
    tails = {n.turn: i for i, n in enumerate(nodes) if n.turn is not None}
    assert sorted(tails) == list(range(len(turns)))
    for j, t in enumerate(turns):
        seq = sequence_of(nodes, tails[j])
        assert seq == list(t["prompt_token_ids"]) + list(t["completion_token_ids"]), f"turn {j} sequence mismatch"
        tail = nodes[tails[j]]
        assert tail.start <= len(t["prompt_token_ids"]) - 1, "loss positions must lie in the turn's own tail"
        assert not tail.children
    for i, n in enumerate(nodes):
        if n.turn is None:
            assert n.children, f"internal node {i} without children"
            assert len(n.tokens) >= 1
    computed = sum(len(n.tokens) for n in nodes)
    per_turn = sum(len(t["prompt_token_ids"]) + len(t["completion_token_ids"]) for t in turns)
    return {"nodes": len(nodes), "internal": sum(n.turn is None for n in nodes), "computed_tokens": computed,
            "per_turn_tokens": per_turn, "ratio": per_turn / computed,
            "min_internal": min((len(n.tokens) for n in nodes if n.turn is None), default=None),
            "min_tail": min(len(n.tokens) for n in nodes if n.turn is not None)}
