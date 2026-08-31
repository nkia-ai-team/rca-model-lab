from __future__ import annotations

from rca_lab.prime_rl.seeding import actor_seed


def test_actor_seed_is_stable_and_fits_signed_int64() -> None:
    trace_id = "trace-that-hashes-above-the-signed-boundary"

    seed = actor_seed(trace_id)

    assert seed == actor_seed(trace_id)
    assert 0 <= seed <= (1 << 63) - 1


def test_actor_seed_preserves_trace_diversity() -> None:
    seeds = {actor_seed(f"trace-{index}") for index in range(32)}

    assert len(seeds) == 32
