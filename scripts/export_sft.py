#!/usr/bin/env python3
"""Export accepted teacher trajectories into completion-only SFT JSONL."""

from __future__ import annotations

import argparse
from pathlib import Path

from rca_lab.data.sft import build_sft_dataset, write_sft_dataset
from rca_lab.scenarios.split import load_teacher_split


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", type=Path, default=Path("configs/teacher/codex-blind-v1.yaml"))
    parser.add_argument("--synth-root", type=Path, default=Path("data/synth/teacher-v1"))
    parser.add_argument("--output", type=Path, default=Path("data/processed/sft-teacher-v1.jsonl"))
    parser.add_argument("--expected-scenarios", type=int)
    args = parser.parse_args()

    split = load_teacher_split(args.split)
    records, manifest = build_sft_dataset(
        synth_root=args.synth_root,
        split=split,
        expected_scenarios=args.expected_scenarios,
    )
    write_sft_dataset(records=records, manifest=manifest, output=args.output)
    print(
        f"scenarios={manifest.scenario_count} trajectories={manifest.trajectory_count} "
        f"turns={manifest.turn_count} output={args.output}"
    )


if __name__ == "__main__":
    main()
