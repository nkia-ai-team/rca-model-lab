#!/usr/bin/env python3
"""Relay Prime-RL LoRA broadcasts between isolated KT Cloud sessions."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from rca_lab.prime_rl.weight_relay import (
    OpenSshBroadcastStore,
    PrimeLoraWeightRelay,
    WeightRelayConfig,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = WeightRelayConfig.model_validate(
        yaml.safe_load(args.config.read_text(encoding="utf-8"))
    )
    trainer = OpenSshBroadcastStore(config.trainer)
    inference = OpenSshBroadcastStore(config.inference)
    relay = PrimeLoraWeightRelay(config.local_broadcast_dir, trainer, inference)
    if args.once:
        print(f"committed={relay.sync_once()}")
    else:
        relay.run(config.poll_interval_seconds)


if __name__ == "__main__":
    main()
