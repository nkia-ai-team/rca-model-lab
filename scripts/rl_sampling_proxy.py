#!/usr/bin/env python3
"""OpenAI-compatible proxy that enables diverse, reproducible RL rollouts."""

from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer

from rca_lab.openai_proxy import proxy_handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", type=int, default=8003)
    parser.add_argument("--upstream", default="http://localhost:8002")
    # DAPO recomputes behavior-policy log probabilities from the frozen SFT
    # model. Temperature 1 keeps that denominator exactly reproducible.
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=1000)
    parser.add_argument(
        "--reasoning-strength",
        choices=("low", "medium", "high"),
        default="low",
        help="Muse-Glimmer chat-template branch; must match the SFT contract",
    )
    args = parser.parse_args()
    handler = proxy_handler(
        upstream=args.upstream,
        temperature=args.temperature,
        initial_seed=args.seed,
        reasoning_strength=args.reasoning_strength,
        increment_seed=True,
    )
    ThreadingHTTPServer(("127.0.0.1", args.listen), handler).serve_forever()


if __name__ == "__main__":
    main()
