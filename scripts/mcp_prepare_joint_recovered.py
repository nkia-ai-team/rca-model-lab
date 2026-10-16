"""Prepare one joint SFT dataset from the recovered chain inputs.

This tool performs no judging and launches no training. It re-validates each
source recovered dataset through mcp_continue_recovered.validate_dataset, copies
rows verbatim, and writes a joint dataset plus provenance manifests for a
single-stage base-start SFT run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import mcp_continue_recovered as recovered

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs/sft/recovered-chain-20261007.yaml"
DEFAULT_OUT_ROOT = ROOT / "outputs/joint-recovered-20261008"
SPLIT_KEYS = ("train", "validation", "sealed_eval", "public_compare", "excluded")
JOINT_TURNS_PER_EPISODE = 6


class JointError(RuntimeError):
    """Joint dataset preparation failed without modifying source data."""


def local_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise JointError(f"invalid jsonl row in {path}:{line_no}: {error}") from error
    return rows


def supervised_turns(rows: list[dict[str, Any]], turns_per_episode: int) -> int:
    return sum(recovered.assistant_turns(row, turns_per_episode) for row in rows)


def stable_write(path: Path, content: str) -> None:
    if path.exists() and path.stat().st_size > 0:
        old = path.read_text(encoding="utf-8")
        if old != content:
            raise JointError(f"refusing to overwrite divergent output: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def stable_write_json(path: Path, payload: dict[str, Any]) -> None:
    stable_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def stable_write_yaml(path: Path, payload: dict[str, Any]) -> None:
    stable_write(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))


def validate_union_split(parts_by_source: dict[str, dict[str, set[str]]]) -> dict[str, list[str]]:
    owner: dict[str, tuple[str, str]] = {}
    overlaps: list[str] = []
    union: dict[str, set[str]] = {key: set() for key in SPLIT_KEYS}
    for source_key, parts in parts_by_source.items():
        for split_key in SPLIT_KEYS:
            for case_id in parts.get(split_key, set()):
                previous = owner.get(case_id)
                if previous and previous != (source_key, split_key):
                    overlaps.append(f"{case_id}:{previous[0]}/{previous[1]}+{source_key}/{split_key}")
                owner[case_id] = (source_key, split_key)
                union[split_key].add(case_id)
    if overlaps:
        raise JointError(f"joint split partitions overlap across sources: {overlaps[:8]}")
    return {key: sorted(values) for key, values in union.items()}


def source_summary(stage: dict[str, Any], dataset_info: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    dataset = Path(dataset_info["dataset"])
    manifest = Path(dataset_info["manifest"])
    complete = Path(dataset_info["complete_marker"])
    return {
        "key": stage["key"],
        "dataset": recovered.rel(dataset),
        "dataset_sha256": dataset_info["sha256"],
        "manifest": recovered.rel(manifest),
        "manifest_sha256": recovered.sha256_file(manifest),
        "complete_marker": recovered.rel(complete),
        "complete_marker_sha256": recovered.sha256_file(complete),
        "split": stage["split"],
        "split_sha256": recovered.sha256_file(local_path(stage["split"])),
        "examples": len(rows),
        "cases": len({row["case_id"] for row in rows}),
        "supervised_turns": supervised_turns(rows, JOINT_TURNS_PER_EPISODE),
        "turns_per_episode": JOINT_TURNS_PER_EPISODE,
    }


def prepare_joint(config_path: Path, out_root: Path) -> dict[str, Any]:
    config = recovered.load_config(config_path)
    stages = config.get("stages") or []
    if not stages:
        raise JointError(f"no stages in config: {config_path}")

    rows: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    parts_by_source: dict[str, dict[str, set[str]]] = {}
    case_owner: dict[str, str] = {}
    collisions: list[str] = []

    expected_recovery = config.get("recovery_expected") or {}
    for raw_stage in stages:
        stage = dict(raw_stage)
        stage["turns_per_episode"] = JOINT_TURNS_PER_EPISODE
        stage.setdefault("recovery_expected", expected_recovery)
        dataset_info = recovered.validate_dataset(stage)
        dataset = Path(dataset_info["dataset"])
        source_rows = read_rows(dataset)
        source_key = stage["key"]
        for row in source_rows:
            case_id = row.get("case_id")
            previous = case_owner.setdefault(case_id, source_key)
            if previous != source_key:
                collisions.append(f"{case_id}:{previous}+{source_key}")
        rows.extend(source_rows)
        sources.append(source_summary(stage, dataset_info, source_rows))
        parts_by_source[source_key] = recovered.split_sets(local_path(stage["split"]))

    if collisions:
        raise JointError(f"case id collisions across source datasets: {collisions[:8]}")

    split = validate_union_split(parts_by_source)
    joint_jsonl = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)

    dataset_path = out_root / "datasets/joint.jsonl"
    stable_write(dataset_path, joint_jsonl)
    dataset_sha = recovered.sha256_file(dataset_path)
    cases = len({row["case_id"] for row in rows})
    turns = supervised_turns(rows, JOINT_TURNS_PER_EPISODE)

    provenance = {
        "aggregation": "verbatim merge of already recovered and judged source datasets",
        "new_judging": False,
        "writer_provider": expected_recovery.get("writer_provider", "codex"),
        "writer_model": expected_recovery.get("writer_model", "gpt-6-astra"),
        "judge_provider": expected_recovery.get("judge_provider", "codex"),
        "judge_model": expected_recovery.get("judge_model", "gpt-6.1-sol"),
    }
    manifest = {
        "dataset": recovered.rel(dataset_path),
        "dataset_sha256": dataset_sha,
        "examples": len(rows),
        "cases": cases,
        "supervised_turns": turns,
        "turns_per_episode": JOINT_TURNS_PER_EPISODE,
        "readiness": {
            "require_judged": True,
            "infrastructure_incomplete": 0,
            "source_datasets_validated": len(sources),
            "source_cases_disjoint": True,
            "union_split_disjoint": True,
        },
        "provenance": provenance,
        "sources": sources,
        "source_sha256": {source["key"]: source["dataset_sha256"] for source in sources},
    }
    complete = {
        "sha256": dataset_sha,
        "writer_provider": provenance["writer_provider"],
        "writer_model": provenance["writer_model"],
        "judge_provider": provenance["judge_provider"],
        "judge_model": provenance["judge_model"],
        "examples": len(rows),
        "cases": cases,
        "supervised_turns": turns,
        "turns_per_episode": JOINT_TURNS_PER_EPISODE,
        "new_judging": False,
        "sources": [
            {
                "key": source["key"],
                "sha256": source["dataset_sha256"],
                "examples": source["examples"],
                "cases": source["cases"],
            }
            for source in sources
        ],
    }
    split_payload = {
        "name": "joint-recovered-20261008",
        "source_config": recovered.rel(config_path),
        "sources": [source["key"] for source in sources],
        **split,
    }

    manifest_path = out_root / "datasets/joint.manifest.json"
    complete_path = out_root / "joint.complete.json"
    split_path = out_root / "joint-split.yaml"
    stable_write_json(manifest_path, manifest)
    stable_write_json(complete_path, complete)
    stable_write_yaml(split_path, split_payload)
    return {
        "dataset": str(dataset_path),
        "manifest": str(manifest_path),
        "complete": str(complete_path),
        "split": str(split_path),
        "dataset_sha256": dataset_sha,
        "examples": len(rows),
        "cases": cases,
        "supervised_turns": turns,
        "sources": [source["key"] for source in sources],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    args = parser.parse_args(argv)
    try:
        result = prepare_joint(args.config, args.out_root)
    except (JointError, recovered.ChainError, recovered.Waiting) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
