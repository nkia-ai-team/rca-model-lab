from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    spec = importlib.util.spec_from_file_location(
        "prepare_rl_dataset", Path("scripts/prepare_rl_dataset.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_premature_penalty_only_applies_to_incorrect_episode() -> None:
    module = _module()
    events = [{"turn": 1, "cand": {"conf": 0.95}}, {"turn": 2, "cand": {"conf": 0.9}}]
    assert module._premature_penalty(events, True) == 0
    assert module._premature_penalty(events, False) > 0


def test_proof_signal_is_gated_by_root_match() -> None:
    module = _module()
    expected = {"expected_status": "confirmed"}
    wrong_root = {
        "root_f1": 0.0,
        "proof_rate": 1.0,
        "status": "provisional",
        "strict_correct": False,
        "unsupported_confirmation": 0,
        "format_errors": 0,
    }
    assert module._optimization_reward(wrong_root, expected) == 0.0


def test_progressive_credit_uses_outcome_grounded_new_evidence_not_gold_target() -> None:
    module = _module()
    root = "11111111-1111-1111-1111-111111111111"
    other = "22222222-2222-2222-2222-222222222222"
    turns = [
        {"action": "env_top"},
        {"action": "probe_logs"},
        {"action": "probe_logs"},
    ]
    ledger = [
        {"action": "env_top", "target": "10", "ok": True, "progress": True},
        {
            "action": "probe_logs",
            "target": root,
            "ok": True,
            "progress": True,
            "evidence_refs": ["ev-root"],
        },
        {
            "action": "probe_logs",
            "target": other,
            "ok": False,
            "progress": False,
        },
    ]

    rewards, signatures = module._progressive_step_rewards(turns, ledger, outcome_weight=0.8)

    assert rewards[1] > rewards[0] > rewards[2]
    assert signatures == ("env_top:10", f"probe_logs:{root}", f"probe_logs:{other}")

    swapped, _ = module._progressive_step_rewards(turns, ledger, outcome_weight=0.0)
    assert swapped[1] == 0.0


def test_equal_terminal_outcomes_suppress_route_only_policy_gradient(tmp_path: Path) -> None:
    module = _module()
    case = "case-a"
    actions = [
        {
            "thought": "inspect",
            "action": "env_top",
            "arg1": "3",
            "arg2": "",
            "query": {},
            "refresh": False,
            "answer": {
                "status": "insufficient",
                "causes": [],
                "external_causes": [],
                "culprits": [],
                "ready": False,
                "text": "continue",
            },
        },
        {
            "thought": "repeat",
            "action": "env_top",
            "arg1": "3",
            "arg2": "",
            "query": {},
            "refresh": False,
            "answer": {
                "status": "insufficient",
                "causes": [],
                "external_causes": [],
                "culprits": [],
                "ready": False,
                "text": "continue",
            },
        },
    ]
    for index, action_count in enumerate((1, 2), 1):
        path = tmp_path / case / f"rollout-{index:02d}" / "agent-a.jsonl"
        path.parent.mkdir(parents=True)
        episode = {
            "event": "episode_completed",
            "result": {
                "status": "provisional",
                "causes": [],
                "external_causes": [],
                "turns": action_count,
            },
            "ledger": [
                {
                    "action": "env_top",
                    "target": "3",
                    "ok": True,
                    "progress": True,
                    "evidence_refs": [f"ev-{index}"],
                }
                for _ in range(action_count)
            ],
            "prompts": [
                {
                    "Messages": [
                        {"role": "system", "content": "RCA"},
                        {"role": "user", "content": "targets unavailable"},
                    ],
                    "Output": json.dumps(action),
                }
                for action in actions[:action_count]
            ],
        }
        path.write_text(json.dumps(episode) + "\n")
    contract = {
        "cases": {
            case: {
                "expected_status": "provisional",
                "roots": [
                    {
                        "target_ids": ["service-a-id"],
                        "target_aliases": ["service-a"],
                    }
                ],
            }
        }
    }

    records = module.build_records(tmp_path, contract)

    assert {record["optimization_reward"] for record in records} == {0.0}
    assert {record["advantage"] for record in records} == {0.0}
    assert all(set(record["turn_advantages"]) == {0.0} for record in records)


def test_turn_credit_keeps_episode_signal_and_penalizes_bad_actions() -> None:
    module = _module()

    credits = module._turn_credit(
        1.0,
        [0.0, 0.08, -0.08],
        optimization_reward=0.8,
    )

    assert credits == pytest.approx([1.0, 1.25, 0.75])


def test_terminal_zero_episode_never_receives_positive_turn_credit() -> None:
    module = _module()

    credits = module._turn_credit(
        -0.01,
        [0.0, 0.08, -0.08],
        optimization_reward=0.0,
    )

    assert credits == pytest.approx([-0.01, 0.0, -0.26])
    assert max(credits) == 0.0


def test_group_advantages_are_normalized_within_case(tmp_path: Path) -> None:
    module = _module()
    case = "case-a"
    target = "11111111-1111-1111-1111-111111111111"
    action = {
        "thought": "inspect",
        "action": "env_top",
        "arg1": "3",
        "arg2": "",
        "query": {},
        "refresh": False,
        "answer": {
            "status": "insufficient",
            "causes": [],
            "external_causes": [],
            "culprits": [],
            "ready": False,
            "text": "continue",
        },
    }
    for index, correct in enumerate((False, True), 1):
        path = tmp_path / case / f"rollout-{index:02d}" / "agent-a.jsonl"
        path.parent.mkdir(parents=True)
        result = {
            "status": "confirmed" if correct else "insufficient",
            "causes": (
                [
                        {
                            "target": target,
                            "mechanism": "causal mechanism",
                            "proof_valid": True,
                            "support_refs": ["obs-001"],
                    }
                ]
                if correct
                else []
            ),
            "external_causes": [],
            "turns": 1,
        }
        episode = {
            "event": "episode_completed",
            "result": result,
            "ledger": [
                {
                    "id": "obs-001",
                    "ok": True,
                    "evidence_refs": ["obs-001"],
                }
            ],
            "prompts": [
                {
                    "Messages": [
                        {"role": "system", "content": "RCA"},
                        {"role": "user", "content": f"- {target} (service-a): n=1"},
                    ],
                    "Output": json.dumps(action),
                }
            ],
        }
        path.write_text(json.dumps(episode) + "\n")
    contract = {
        "cases": {
            case: {
                "expected_status": "confirmed",
                "roots": [
                    {"target_ids": [target], "target_aliases": ["service-a"]}
                ],
            }
        }
    }
    records = module.build_records(tmp_path, contract)
    assert len(records) == 2
    assert sorted(record["advantage"] for record in records) == pytest.approx([-1.0, 1.0])
    assert sorted(record["optimization_reward"] for record in records) == pytest.approx([0.0, 1.0])
    assert all(len(record["turns"]) == 1 for record in records)
    losing = next(record for record in records if record["optimization_reward"] == 0.0)
    winning = next(record for record in records if record["optimization_reward"] == 1.0)
    assert max(losing["turn_advantages"]) <= 0.0
    assert min(winning["turn_advantages"]) > 0.0


def test_equally_incorrect_episodes_do_not_learn_efficiency_noise(tmp_path: Path) -> None:
    module = _module()
    case = "case-a"
    action = {
        "thought": "inspect",
        "action": "env_top",
        "arg1": "3",
        "arg2": "",
        "query": {},
        "refresh": False,
        "answer": {
            "status": "insufficient",
            "causes": [],
            "external_causes": [],
            "culprits": [],
            "ready": False,
            "text": "continue",
        },
    }
    for index, turns in enumerate((1, 12), 1):
        path = tmp_path / case / f"rollout-{index:02d}" / "agent-a.jsonl"
        path.parent.mkdir(parents=True)
        episode = {
            "event": "episode_completed",
            "result": {
                "status": "provisional",
                "causes": [],
                "external_causes": [],
                "turns": turns,
            },
            "ledger": [{"ok": index == 1}],
            "prompts": [
                {
                    "Messages": [
                        {"role": "system", "content": "RCA"},
                        {"role": "user", "content": "targets unavailable"},
                    ],
                    "Output": json.dumps(action),
                }
            ],
        }
        path.write_text(json.dumps(episode) + "\n")
    contract = {
        "cases": {
            case: {
                "expected_status": "provisional",
                "roots": [
                    {
                        "target_ids": ["service-a-id"],
                        "target_aliases": ["service-a"],
                    }
                ],
            }
        }
    }

    records = module.build_records(tmp_path, contract)

    assert len({record["reward"] for record in records}) == 2
    assert {record["optimization_reward"] for record in records} == {0.0}
    assert {record["advantage"] for record in records} == {0.0}


def test_completed_attempt_marker_excludes_abandoned_attempts(tmp_path: Path) -> None:
    module = _module()
    case = "case-a"
    target = "11111111-1111-1111-1111-111111111111"
    action = {
        "thought": "inspect",
        "action": "env_top",
        "arg1": "3",
        "arg2": "",
        "query": {},
        "refresh": False,
        "answer": {
            "status": "insufficient",
            "causes": [],
            "external_causes": [],
            "culprits": [],
            "ready": False,
            "text": "continue",
        },
    }
    episode = {
        "event": "episode_completed",
        "result": {
            "status": "insufficient",
            "causes": [],
            "external_causes": [],
            "turns": 1,
        },
        "ledger": [{"ok": True}],
        "prompts": [
            {
                "Messages": [
                    {"role": "system", "content": "RCA"},
                    {"role": "user", "content": f"- {target} (service-a): n=1"},
                ],
                "Output": json.dumps(action),
            }
        ],
    }
    selected = tmp_path / case / "attempts" / "selected" / "rollout-01"
    selected.mkdir(parents=True)
    (selected / "agent-selected.jsonl").write_text(json.dumps(episode) + "\n")
    abandoned = tmp_path / case / "attempts" / "abandoned" / "rollout-01"
    abandoned.mkdir(parents=True)
    (abandoned / "agent-abandoned.jsonl").write_text(json.dumps(episode) + "\n")
    (tmp_path / case / "case-complete.json").write_text(
        json.dumps({"trajectory_dirs": ["attempts/selected/rollout-01"]}) + "\n"
    )
    contract = {
        "cases": {
            case: {
                "expected_status": "confirmed",
                "roots": [
                    {"target_ids": [target], "target_aliases": ["service-a"]}
                ],
            }
        }
    }

    records = module.build_records(tmp_path, contract)

    assert len(records) == 1


def test_teacher_anchors_supply_positive_signal_when_all_rollouts_are_wrong(
    tmp_path: Path,
) -> None:
    module = _module()
    records = [
        {
            "scenario_id": "case-a",
            "rollout_id": "rollout-01",
            "reward": 0.1,
            "optimization_reward": 0.1,
            "advantage": 0.0,
            "score": {},
            "turns": [
                {
                    "messages": [
                        {"role": "system", "content": "RCA"},
                        {"role": "user", "content": "incident"},
                        {"role": "assistant", "content": "bad"},
                    ]
                }
            ],
        }
    ]
    teacher = tmp_path / "teacher.jsonl"
    teacher.write_text(
        json.dumps(
            {
                "scenario_id": "case-a",
                "trajectory_id": "episode-1",
                "turn_count": 1,
                "turns": [
                    {
                        "turn": 1,
                        "messages": [
                            {"role": "system", "content": "RCA"},
                            {"role": "user", "content": "incident"},
                            {"role": "assistant", "content": "good"},
                        ],
                    }
                ],
            }
        )
        + "\n"
    )

    pairs = module.build_teacher_anchor_pairs(records, teacher)

    assert [record["advantage"] for record in pairs] == [-1.0, 1.0]
    assert pairs[1]["turns"][0]["messages"][-1]["content"] == "good"


def test_reward_filtered_replay_never_penalizes_partly_useful_failed_episode(
    tmp_path: Path,
) -> None:
    module = _module()
    base = {
        "scenario_id": "case-a",
        "reward": 0.1,
        "optimization_reward": 0.1,
        "advantage": 0.0,
        "turns": [
            {
                "messages": [
                    {"role": "system", "content": "RCA"},
                    {"role": "user", "content": "incident"},
                    {"role": "assistant", "content": "probe"},
                ]
            }
        ],
    }
    records = [
        {
            **base,
            "rollout_id": "wrong",
            "score": {
                "root_f1": 0.0,
                "format_errors": 0,
                "unsupported_confirmation": 0,
            },
        },
        {
            **base,
            "rollout_id": "exact",
            "score": {
                "root_f1": 1.0,
                "format_errors": 0,
                "unsupported_confirmation": 0,
            },
        },
    ]
    teacher = tmp_path / "teacher.jsonl"
    teacher.write_text(
        json.dumps(
            {
                "scenario_id": "case-a",
                "trajectory_id": "episode-1",
                "turn_count": 1,
                "turns": [
                    {
                        "turn": 1,
                        "messages": [
                            {"role": "system", "content": "RCA"},
                            {"role": "user", "content": "incident"},
                            {"role": "assistant", "content": "expert"},
                        ],
                    }
                ],
            }
        )
        + "\n"
    )

    replay = module.build_reward_filtered_replay(records, teacher)

    assert [record["rollout_id"] for record in replay] == [
        "exact-reward-filtered",
        "teacher-replay-1",
    ]
    assert [record["advantage"] for record in replay] == [0.5, 1.0]


def test_mixed_outcomes_excludes_all_failure_groups() -> None:
    module = _module()

    def record(case: str, rollout: str, success: bool, reward: float) -> dict:
        return {
            "scenario_id": case,
            "rollout_id": rollout,
            "reward": reward,
            "optimization_reward": reward,
            "advantage": 0.0,
            "score": {
                "strict_correct": success,
                "root_f1": 1.0 if success else 0.0,
                "evidence_complete": success,
                "format_errors": 0,
                "unsupported_confirmation": 0,
            },
            "turns": [],
        }

    records = [
        record("mixed", "good", True, 1.0),
        record("mixed", "bad", False, 0.2),
        record("all-bad", "bad-1", False, 0.1),
        record("all-bad", "bad-2", False, 0.3),
    ]

    selected = module.build_mixed_outcome_records(records)

    assert {row["scenario_id"] for row in selected} == {"mixed"}


def test_rank_refinement_uses_verified_success_and_hardest_failure(tmp_path: Path) -> None:
    module = _module()
    base_score = {
        "root_f1": 0.0,
        "strict_correct": False,
        "evidence_complete": False,
        "format_errors": 0,
        "unsupported_confirmation": 0,
    }
    records = [
        {
            "scenario_id": "case-a",
            "rollout_id": "easy-failure",
            "reward": 0.1,
            "optimization_reward": 0.1,
            "score": base_score,
            "turns": [{"messages": [{"role": "assistant", "content": "easy"}]}],
        },
        {
            "scenario_id": "case-a",
            "rollout_id": "hard-failure",
            "reward": 0.7,
            "optimization_reward": 0.7,
            "score": base_score,
            "turns": [{"messages": [{"role": "assistant", "content": "hard"}]}],
        },
        {
            "scenario_id": "case-a",
            "rollout_id": "verified",
            "reward": 0.9,
            "optimization_reward": 0.9,
            "score": {
                **base_score,
                "root_f1": 1.0,
                "strict_correct": True,
                "evidence_complete": True,
            },
            "turns": [{"messages": [{"role": "assistant", "content": "good"}]}],
        },
    ]
    teacher = tmp_path / "teacher.jsonl"
    teacher.write_text(
        json.dumps(
            {
                "scenario_id": "case-a",
                "trajectory_id": "teacher-1",
                "turn_count": 1,
                "turns": [
                    {
                        "turn": 1,
                        "messages": [
                            {"role": "system", "content": "RCA"},
                            {"role": "user", "content": "incident"},
                            {"role": "assistant", "content": "teacher"},
                        ],
                    }
                ],
            }
        )
        + "\n"
    )

    pairs = module.build_rank_refinement_pairs(records, teacher)

    assert len(pairs) == 1
    assert pairs[0]["chosen_source"] == "student:verified"
    assert pairs[0]["rejected_source"] == "student:hard-failure"
