"""Merge trusted and newly recovered Polestar episodes without changing any turns."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_continue_recovered as recovered
from mcp_prepare_joint_recovered import stable_write, stable_write_json

ROOT = Path(__file__).resolve().parents[1]
PROTECTED = ("validation", "sealed_eval", "excluded", "public_compare", "validation_pending_golden")


def prepare(config_path: Path) -> dict:
    config = yaml.safe_load(config_path.read_text())
    split_path = ROOT / config["split"]
    original = yaml.safe_load((ROOT / config["original_split"]).read_text())
    split = yaml.safe_load(split_path.read_text())
    for key in PROTECTED:
        if set(original.get(key) or []) != set(split.get(key) or []):
            raise ValueError(f"protected partition changed: {key}")
    recovered.validate_split_disjoint(split_path)
    expected = set(split["train"])
    forbidden = set().union(*(set(split.get(k) or []) for k in PROTECTED))
    if expected & forbidden or len(expected) != len(split["train"]):
        raise ValueError("duplicate or protected training case")
    sources, chunks, seen, examples, turns = [], [], set(), 0, 0
    for source in config["sources"]:
        stage = {**source, "turns_per_episode": 0, "recovery_expected": config["recovery_expected"]}
        info = recovered.validate_dataset(stage)
        raw = Path(info["dataset"]).read_text()
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        cases = {row["case_id"] for row in rows}
        if seen & cases or not cases <= expected:
            raise ValueError(f"overlapping or unexpected source cases: {source['key']}")
        identities = [(row["case_id"], row["run"]) for row in rows]
        if len(identities) != len(set(identities)):
            raise ValueError(f"duplicate episodes: {source['key']}")
        seen |= cases
        chunks.append(raw if raw.endswith("\n") else raw + "\n")
        examples += len(rows)
        turns += info["turns"]
        sources.append(info)
    if seen != expected:
        raise ValueError(f"training cases missing: {sorted(expected - seen)}")
    out = ROOT / config["out_root"]
    dataset = out / "datasets/polestar.jsonl"
    stable_write(dataset, "".join(chunks))
    digest = recovered.sha256_file(dataset)
    manifest = {
        "dataset_sha256": digest, "examples": examples, "cases": len(seen),
        "supervised_turns": turns, "turns_per_episode": 0,
        "split": str(split_path), "split_sha256": recovered.sha256_file(split_path),
        "readiness": {"require_judged": True, "infrastructure_incomplete": 0},
        "existing_episodes_modified": False, "public_data_included": False,
        "sources": sources,
    }
    stable_write_json(dataset.with_suffix(".manifest.json"), manifest)
    stable_write_json(out / "dataset.complete.json", {
        **config["recovery_expected"], "sha256": digest,
        "examples": examples, "cases": len(seen), "supervised_turns": turns,
        "turns_per_episode": 0,
    })
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.config), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
