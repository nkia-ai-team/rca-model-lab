#!/usr/bin/env python3
"""Export accepted teacher trajectories into completion-only SFT JSONL."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import yaml

from rca_lab.data.sft import build_sft_dataset, write_sft_dataset
from rca_lab.eval.scoring import EvalContract
from rca_lab.scenarios.split import load_teacher_split


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", type=Path, default=Path("configs/teacher/codex-blind-v1.yaml"))
    parser.add_argument("--synth-root", type=Path, default=Path("data/synth/teacher-v1"))
    parser.add_argument("--output", type=Path, default=Path("data/processed/sft-teacher-v1.jsonl"))
    parser.add_argument("--expected-scenarios", type=int)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--curation-manifest", type=Path)
    args = parser.parse_args()

    split = load_teacher_split(args.split)
    contract = (
        EvalContract.model_validate(yaml.safe_load(args.contract.read_text(encoding="utf-8")))
        if args.contract
        else None
    )
    records, manifest = build_sft_dataset(
        synth_root=args.synth_root,
        split=split,
        expected_scenarios=args.expected_scenarios,
        contract=contract,
        terminal_contract_sha256=(
            hashlib.sha256(args.contract.read_bytes()).hexdigest() if args.contract else None
        ),
        curation_manifest_sha256=(
            hashlib.sha256(args.curation_manifest.read_bytes()).hexdigest()
            if args.curation_manifest
            else None
        ),
    )
    write_sft_dataset(records=records, manifest=manifest, output=args.output)
    print(
        f"scenarios={manifest.scenario_count} trajectories={manifest.trajectory_count} "
        f"turns={manifest.turn_count} output={args.output}"
    )


if __name__ == "__main__":
    main()
