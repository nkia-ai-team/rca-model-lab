"""Deterministic sampling seeds shared by Prime-RL environment adapters."""

from __future__ import annotations

import hashlib


def actor_seed(trace_id: str) -> int:
    """Derive a stable seed accepted by the Go actor's signed int64 flag."""
    raw = int.from_bytes(hashlib.sha256(trace_id.encode()).digest()[:8], "big")
    return raw & ((1 << 63) - 1)
