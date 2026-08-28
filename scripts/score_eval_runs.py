#!/usr/bin/env python3
"""Deterministically score sealed runs from typed episode artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from rca_lab.eval.scoring import EvalContract
from rca_lab.eval.scoring import score_directory as score
from rca_lab.provenance import file_sha256


def evaluation_provenance(output: Path) -> dict[str, object]:
    manifest_path = output / "run-manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"missing evaluation run manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = (
        "model_artifact_sha256",
        "agent_sha256",
        "restore_sha256",
        "split_sha256",
        "case_set_sha256",
        "structured_output_backend",
        "actor_temperature",
        "actor_seed",
        "runs",
        "cases",
    )
    missing = [
        key
        for key in required
        if key not in manifest or manifest[key] is None or manifest[key] == "" or manifest[key] == []
    ]
    if missing:
        raise ValueError(f"evaluation run manifest missing provenance: {missing}")
    return {
        "run_manifest_sha256": file_sha256(manifest_path),
        **{key: manifest[key] for key in required},
        "model_artifact": manifest.get("model_artifact", ""),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--contract", type=Path, default=Path("configs/eval/sealed-family-v2.yaml")
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    contract = EvalContract.model_validate(
        yaml.safe_load(args.contract.read_text(encoding="utf-8"))
    ).model_dump(mode="json", exclude_none=True)
    report = score(args.output, contract, args.runs)
    report["evaluation_provenance"] = evaluation_provenance(args.output)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "runs"}))


if __name__ == "__main__":
    main()
