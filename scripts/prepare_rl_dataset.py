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

from rca_lab.data.sft import SFTRecord
from rca_lab.eval.scoring import (
    EvalContract,
    load_episode,
    score_episode,
)
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
                ],
                "action": raw.get("action", "answer"),
            }
        )
    return turns


def _progressive_step_rewards(
    actor_turns: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    *,
    outcome_weight: float,
) -> tuple[list[float], tuple[str, ...]]:
    """Credit new evidence only in proportion to a grounded final outcome.

    Gold root identities are deliberately absent from this function. Rewarding
    a query merely because it touched the expected target teaches answer-key
    navigation rather than causal investigation. Invalid and duplicate actions
    remain local penalties; positive route credit is gated by the terminal
    diagnosis score.
    """
    rewards: list[float] = []
    signatures: list[str] = []
    ledger_index = 0
    seen_signatures: set[str] = set()
    seen_refs: set[str] = set()
    for turn in actor_turns:
        action = str(turn["action"])
        if action == "answer":
            rewards.append(0.0)
            signatures.append("answer")
            continue
        observation: dict[str, Any] = {}
        while ledger_index < len(ledger):
            candidate = ledger[ledger_index]
            ledger_index += 1
            if str(candidate.get("action", "")) == action:
                observation = candidate
                break
        target = str(observation.get("target") or observation.get("arg1") or "")
        signature = f"{action}:{target}"
        signatures.append(signature)
        reward = 0.0
        if not bool(observation.get("ok")):
            reward -= 0.04
        elif bool(observation.get("progress")):
            if action == "env_top" and not any(
                value.startswith("env_top:") for value in seen_signatures
            ):
                reward += 0.01 * outcome_weight
            new_refs = set(map(str, observation.get("evidence_refs", []))) - seen_refs
            reward += outcome_weight * min(0.05, 0.02 * len(new_refs))
            seen_refs.update(new_refs)
        if signature in seen_signatures:
            reward -= 0.04
        seen_signatures.add(signature)
        rewards.append(max(-0.08, min(0.30, reward)))
    return rewards, tuple(signatures)


def _turn_credit(
    episode_advantage: float,
    step_rewards: list[float],
    *,
    optimization_reward: float,
) -> list[float]:
    """Redistribute bounded local credit without reversing terminal outcomes.

    Every turn first inherits the case-relative episode advantage. A small local
    adjustment then rewards new evidence or penalizes invalid/duplicate actions.
    A terminal-zero episode can never receive positive turn credit: avoiding a
    duplicate call is not evidence that an incorrect diagnosis should become
    more likely.
    """
    credits: list[float] = []
    for step_reward in step_rewards:
        local_adjustment = max(-0.25, min(0.25, 0.25 * step_reward / 0.08))
        credit = episode_advantage + local_adjustment
        if optimization_reward <= 0.0:
            credit = min(0.0, credit)
        credits.append(max(-3.0, min(3.0, credit)))
    return credits


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


def _optimization_reward(score: dict[str, Any], expected: dict[str, Any]) -> float:
    """Return a diagnosis-only learning signal.

    The sealed evaluator intentionally reports efficiency and tool-success
    diagnostics, but those weak signals must not rank equally incorrect RCA
    episodes. Otherwise group normalization turns harmless turn-count noise
    into a full-strength policy gradient toward premature termination.
    """
    root_f1 = float(score["root_f1"])
    root_exact = float(root_f1 == 1.0)
    proof_rate = float(score["proof_rate"])
    status_correct = float(score["status"] == expected["expected_status"])
    strict_correct = float(score["strict_correct"])
    unsupported = float(score["unsupported_confirmation"])
    format_errors = float(min(1, int(score["format_errors"])))
    return max(
        0.0,
        min(
            1.0,
            0.55 * root_f1
            + 0.15 * root_exact
            # Proof quality is useful only for a matching root. A perfectly
            # cited but unrelated culprit must not become a positive signal.
            + 0.15 * proof_rate * root_f1
            + 0.10 * status_correct * root_f1
            + 0.05 * strict_correct
            - 0.60 * unsupported
            - 0.10 * format_errors,
        ),
    )


def build_records(rollouts: Path, contract: dict[str, Any]) -> list[dict[str, Any]]:
    manifest_path = rollouts / "run-manifest.json"
    rollout_manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    )
    behavior_policy = {
        "behavior_model_artifact": str(rollout_manifest.get("model_artifact", "")),
        "behavior_model_sha256": str(rollout_manifest.get("model_artifact_sha256", "")),
        "base_model_artifact": str(rollout_manifest.get("base_model_artifact", "")),
        "base_model_sha256": str(rollout_manifest.get("base_model_artifact_sha256", "")),
        "behavior_temperature": float(rollout_manifest.get("temperature", 0.0)),
    }
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
            optimization_reward = _optimization_reward(score, expected)
            turns = _actor_turns(episode)
            if turns:
                step_rewards, path_signature = _progressive_step_rewards(
                    turns,
                    episode.get("ledger", []),
                    outcome_weight=optimization_reward,
                )
                grouped[case_id].append(
                    {
                        **behavior_policy,
                        "scenario_id": case_id,
                        "rollout_id": path.parent.name,
                        "reward": score["reward"],
                        "optimization_reward": optimization_reward,
                        "score": score,
                        "turns": [{"messages": turn["messages"]} for turn in turns],
                        "step_rewards": step_rewards,
                        "path_signature": path_signature,
                    }
                )

    records: list[dict[str, Any]] = []
    for rows in grouped.values():
        mean = sum(row["optimization_reward"] for row in rows) / len(rows)
        variance = sum((row["optimization_reward"] - mean) ** 2 for row in rows) / len(rows)
        stddev = math.sqrt(variance)
        for row in rows:
            episode_advantage = (
                0.0 if stddev < 1e-8 else (row["optimization_reward"] - mean) / stddev
            )
            row["advantage"] = episode_advantage
            row["turn_advantages"] = (
                [0.0] * len(row["step_rewards"])
                if stddev < 1e-8
                else _turn_credit(
                    episode_advantage,
                    row["step_rewards"],
                    optimization_reward=row["optimization_reward"],
                )
            )
            row.pop("step_rewards")
            row.pop("path_signature")
            records.append(row)
    return records


def build_teacher_anchor_pairs(
    records: list[dict[str, Any]], teacher_dataset: Path
) -> list[dict[str, Any]]:
    """Pair every expert episode with a deterministic hard-negative rollout.

    Group-relative optimization cannot learn a missing behavior when every
    sampled rollout is wrong. Accepted teacher episodes supply a train-only
    positive anchor without exposing sealed evaluation families.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record["scenario_id"])].append(record)

    teachers = [
        SFTRecord.model_validate(json.loads(line))
        for line in teacher_dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    teacher_scenarios = {record.scenario_id for record in teachers}
    missing = sorted(set(grouped) - teacher_scenarios)
    extra = sorted(teacher_scenarios - set(grouped))
    if missing or extra:
        raise ValueError(f"teacher/rollout scenario mismatch: missing={missing} extra={extra}")

    pairs: list[dict[str, Any]] = []
    teacher_counts: dict[str, int] = defaultdict(int)
    for teacher in teachers:
        teacher_counts[teacher.scenario_id] += 1
        pair_index = teacher_counts[teacher.scenario_id]
        negative = min(
            grouped[teacher.scenario_id],
            key=lambda row: (
                float(row["optimization_reward"]),
                float(row["reward"]),
                str(row["rollout_id"]),
            ),
        )
        pairs.append(
            {
                **negative,
                "rollout_id": f"{negative['rollout_id']}-anchor-pair-{pair_index}",
                "advantage": -1.0,
            }
        )
        pairs.append(
            {
                "scenario_id": teacher.scenario_id,
                "rollout_id": f"teacher-anchor-{pair_index}",
                "reward": 1.0,
                "optimization_reward": 1.0,
                "advantage": 1.0,
                "score": {
                    "source": "accepted_train_teacher",
                    "trajectory_id": teacher.trajectory_id,
                },
                "turns": [
                    {"messages": [message.model_dump(mode="json") for message in turn.messages]}
                    for turn in teacher.turns
                ],
            }
        )
    return pairs


def build_reward_filtered_replay(
    records: list[dict[str, Any]], teacher_dataset: Path
) -> list[dict[str, Any]]:
    """Replay expert episodes plus only exact-root student successes.

    A failed RCA episode usually still contains useful discovery actions. Giving
    every token in that episode a negative advantage suppresses those shared
    skills and caused sealed-family regressions. Reward-filtered replay keeps
    the whole-episode contract while applying policy gradients only to
    trajectories that actually reached the typed root-cause objective.
    """
    grouped = {str(record["scenario_id"]) for record in records}
    teachers = [
        SFTRecord.model_validate(json.loads(line))
        for line in teacher_dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    teacher_scenarios = {record.scenario_id for record in teachers}
    missing = sorted(grouped - teacher_scenarios)
    extra = sorted(teacher_scenarios - grouped)
    if missing or extra:
        raise ValueError(f"teacher/rollout scenario mismatch: missing={missing} extra={extra}")

    replay: list[dict[str, Any]] = []
    for record in records:
        score = record["score"]
        if (
            float(score["root_f1"]) == 1.0
            and int(score["format_errors"]) == 0
            and int(score["unsupported_confirmation"]) == 0
        ):
            replay.append(
                {
                    **record,
                    "rollout_id": f"{record['rollout_id']}-reward-filtered",
                    # Student successes add exploration signal, but accepted
                    # expert trajectories remain the dominant replay anchor.
                    "advantage": 0.5,
                }
            )

    teacher_counts: dict[str, int] = defaultdict(int)
    for teacher in teachers:
        teacher_counts[teacher.scenario_id] += 1
        replay.append(
            {
                "scenario_id": teacher.scenario_id,
                "rollout_id": f"teacher-replay-{teacher_counts[teacher.scenario_id]}",
                "reward": 1.0,
                "optimization_reward": 1.0,
                "advantage": 1.0,
                "score": {
                    "source": "accepted_train_teacher_replay",
                    "trajectory_id": teacher.trajectory_id,
                },
                "turns": [
                    {"messages": [message.model_dump(mode="json") for message in turn.messages]}
                    for turn in teacher.turns
                ],
            }
        )
    return replay


def _verified_success(record: dict[str, Any]) -> bool:
    score = record["score"]
    return bool(
        score.get("strict_correct")
        and float(score.get("root_f1", 0.0)) == 1.0
        and bool(score.get("evidence_complete"))
        and int(score.get("format_errors", 0)) == 0
        and int(score.get("unsupported_confirmation", 0)) == 0
    )


def build_mixed_outcome_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only scenario groups with verified successes and failures.

    Group-relative objectives have no terminal diagnostic signal when every
    trajectory has the same outcome. All-failure groups are routed to guided
    teacher refinement instead of receiving heuristic policy gradients.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record["scenario_id"])].append(record)
    mixed: list[dict[str, Any]] = []
    for rows in grouped.values():
        outcomes = {_verified_success(row) for row in rows}
        rewards = {float(row["optimization_reward"]) for row in rows}
        if outcomes == {False, True} and len(rewards) > 1:
            mixed.extend(rows)
    return mixed


def build_rank_refinement_pairs(
    records: list[dict[str, Any]], teacher_dataset: Path
) -> list[dict[str, Any]]:
    """Build whole-trajectory chosen/rejected pairs for conservative refinement.

    The chosen side is a verified student success when available, otherwise an
    accepted teacher trajectory. The rejected side is the highest-reward failed
    student trajectory, making it a hard negative rather than an easy failure.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record["scenario_id"])].append(record)
    teachers = [
        SFTRecord.model_validate(json.loads(line))
        for line in teacher_dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    teacher_by_scenario: dict[str, list[SFTRecord]] = defaultdict(list)
    for teacher in teachers:
        teacher_by_scenario[teacher.scenario_id].append(teacher)
    missing = sorted(set(grouped) - set(teacher_by_scenario))
    extra = sorted(set(teacher_by_scenario) - set(grouped))
    if missing or extra:
        raise ValueError(f"teacher/rollout scenario mismatch: missing={missing} extra={extra}")

    pairs: list[dict[str, Any]] = []
    for scenario_id, rows in sorted(grouped.items()):
        failures = [row for row in rows if not _verified_success(row)]
        if not failures:
            continue
        rejected = max(
            failures,
            key=lambda row: (
                float(row["optimization_reward"]),
                float(row["reward"]),
                str(row["rollout_id"]),
            ),
        )
        successes = [row for row in rows if _verified_success(row)]
        if successes:
            chosen = max(
                successes,
                key=lambda row: (
                    float(row["optimization_reward"]),
                    float(row["reward"]),
                    str(row["rollout_id"]),
                ),
            )
            chosen_source = f"student:{chosen['rollout_id']}"
            chosen_turns = chosen["turns"]
        else:
            teacher = teacher_by_scenario[scenario_id][0]
            chosen_source = f"teacher:{teacher.trajectory_id}"
            chosen_turns = [
                {"messages": [message.model_dump(mode="json") for message in turn.messages]}
                for turn in teacher.turns
            ]
        pairs.append(
            {
                "scenario_id": scenario_id,
                "pair_id": f"{scenario_id}:rank-01",
                "chosen_source": chosen_source,
                "rejected_source": f"student:{rejected['rollout_id']}",
                "chosen_turns": chosen_turns,
                "rejected_turns": rejected["turns"],
                "chosen_score": (
                    chosen["score"] if successes else {"source": "accepted_train_teacher"}
                ),
                "rejected_score": rejected["score"],
            }
        )
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rollouts", type=Path, required=True)
    parser.add_argument("--contract", type=Path, default=Path("configs/eval/sealed-family-v2.yaml"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--teacher-anchors",
        type=Path,
        help="emit expert-positive/hard-negative whole-episode pairs",
    )
    parser.add_argument(
        "--reward-filtered-replay",
        type=Path,
        help="emit expert replay plus exact-root successful student episodes",
    )
    parser.add_argument(
        "--rank-refinement",
        type=Path,
        help="emit whole-trajectory chosen/rejected pairs",
    )
    parser.add_argument(
        "--mixed-outcomes",
        action="store_true",
        help="keep only groups containing verified successes and failures",
    )
    args = parser.parse_args()
    contract = EvalContract.model_validate(
        yaml.safe_load(args.contract.read_text(encoding="utf-8"))
    ).model_dump(mode="json", exclude_none=True)
    records = build_records(args.rollouts, contract)
    selected = sum(
        bool(value)
        for value in (args.teacher_anchors, args.reward_filtered_replay, args.rank_refinement)
    )
    if selected > 1:
        parser.error("choose only one teacher replay/ranking strategy")
    if args.mixed_outcomes and selected:
        parser.error("mixed-outcomes cannot be combined with replay/ranking strategies")
    if args.teacher_anchors:
        records = build_teacher_anchor_pairs(records, args.teacher_anchors)
    elif args.reward_filtered_replay:
        records = build_reward_filtered_replay(records, args.reward_filtered_replay)
    elif args.rank_refinement:
        records = build_rank_refinement_pairs(records, args.rank_refinement)
    elif args.mixed_outcomes:
        records = build_mixed_outcome_records(records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records),
        encoding="utf-8",
    )
    nonzero = sum(abs(row.get("advantage", 0.0)) > 1e-8 for row in records)
    print(f"records={len(records)} nonzero_advantages={nonzero} output={args.output}")


if __name__ == "__main__":
    main()
