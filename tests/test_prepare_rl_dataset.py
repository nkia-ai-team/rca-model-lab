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
        path.write_text(json.dumps(episode) + "\n")
    contract = {
        "cases": {
            case: {
                "expected_status": "confirmed",
                "roots": [{"target_aliases": ["service-a"]}],
            }
        }
    }
    records = module.build_records(tmp_path, contract)
    assert len(records) == 2
    assert sorted(record["advantage"] for record in records) == pytest.approx([-1.0, 1.0])
    assert all(len(record["turns"]) == 1 for record in records)


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
                "roots": [{"target_aliases": ["service-a"]}],
            }
        }
    }

    records = module.build_records(tmp_path, contract)

    assert len(records) == 1
