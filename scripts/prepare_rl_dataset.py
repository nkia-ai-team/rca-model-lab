#!/usr/bin/env python3
"""Convert grouped on-policy episodes into episode-preserving DAPO records."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from rca_lab.eval.scoring import EvalContract, load_episode, score_episode
from rca_lab.harness.models import ActionRequest, AnswerState


def _actor_turns(episode: dict[str, Any]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for prompt in episode.get("prompts", []):
        messages = prompt.get("Messages", [])
        if len(messages) != 2 or prompt.get("Error"):
            continue
        output = str(prompt.get("Output", ""))
        try:
            raw = json.loads(output)
            if set(raw) == {"answer"}:
                AnswerState.model_validate(raw["answer"])
            else:
                ActionRequest.model_validate(raw)
        except (json.JSONDecodeError, ValidationError):
            continue
        turns.append(
            {
                "messages": [
                    {"role": str(messages[0]["role"]), "content": str(messages[0]["content"])},
                    {"role": str(messages[1]["role"]), "content": str(messages[1]["content"])},
                    {"role": "assistant", "content": output},
                ]
            }
        )
    return turns


def _premature_penalty(events: list[dict[str, Any]], strict_correct: bool) -> float:
    if strict_correct:
        return 0.0
    penalties: list[float] = []
    max_turn = max(1, max((int(event.get("turn", 0)) for event in events), default=0))
    for event in events:
        cand = event.get("cand") or {}
        confidence = float(cand.get("conf", 0))
        allowed = 0.45 + 0.45 * int(event.get("turn", 0)) / max_turn
        penalties.append(max(0.0, confidence - allowed))
    return sum(penalties) / len(penalties) if penalties else 0.0


def build_records(rollouts: Path, contract: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case_id, expected in contract["cases"].items():
        case_dir = rollouts / case_id
        marker = case_dir / "case-complete.json"
        if marker.exists():
            marker_payload = json.loads(marker.read_text(encoding="utf-8"))
            trajectory_dirs = [case_dir / value for value in marker_payload["trajectory_dirs"]]
            paths = [
                path for directory in trajectory_dirs for path in directory.glob("agent-*.jsonl")
            ]
        else:
            paths = sorted(case_dir.glob("rollout-*/agent-*.jsonl"))
        for path in paths:
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            episode = load_episode(path)
            score = score_episode(case_id, expected, episode)
            progressive_penalty = _premature_penalty(events, score["strict_correct"])
            score["premature_confidence_penalty"] = progressive_penalty
            score["reward"] = max(0.0, score["reward"] - 0.10 * progressive_penalty)
            turns = _actor_turns(episode)
            if turns:
                grouped[case_id].append(
                    {
                        "scenario_id": case_id,
                        "rollout_id": path.parent.name,
                        "reward": score["reward"],
                        "score": score,
                        "turns": turns,
                    }
                )

    records: list[dict[str, Any]] = []
    for rows in grouped.values():
        mean = sum(row["reward"] for row in rows) / len(rows)
        variance = sum((row["reward"] - mean) ** 2 for row in rows) / len(rows)
        stddev = math.sqrt(variance)
        for row in rows:
            row["advantage"] = 0.0 if stddev < 1e-8 else (row["reward"] - mean) / stddev
            records.append(row)
    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rollouts", type=Path, required=True)
    parser.add_argument("--contract", type=Path, default=Path("configs/eval/sealed-family-v2.yaml"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    contract = EvalContract.model_validate(
        yaml.safe_load(args.contract.read_text(encoding="utf-8"))
    ).model_dump(mode="json", exclude_none=True)
    records = build_records(args.rollouts, contract)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records),
        encoding="utf-8",
    )
    nonzero = sum(abs(row["advantage"]) > 1e-8 for row in records)
    print(f"episodes={len(records)} nonzero_advantages={nonzero} output={args.output}")


if __name__ == "__main__":
    main()
