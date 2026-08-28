import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from rca_lab.eval.scoring import (
    EvalContract,
    count_format_errors,
    diagnosis_optimization_reward,
    root_f1,
    score_directory,
    score_episode,
    target_names,
)


def test_rl_reward_excludes_efficiency_and_tool_success_noise() -> None:
    wrong = diagnosis_optimization_reward(
        root_f1=0.0,
        proof_rate=1.0,
        status_correct=True,
        strict_correct=False,
        unsupported_confirmation=0,
        format_errors=0,
    )

    assert wrong == 0.0


def test_train_and_sealed_contracts_are_typed_and_disjoint() -> None:
    train = EvalContract.model_validate(
        yaml.safe_load(Path("configs/eval/train-family-v2.yaml").read_text())
    )
    sealed = EvalContract.model_validate(
        yaml.safe_load(Path("configs/eval/sealed-family-v2.yaml").read_text())
    )
    assert len(train.cases) == 20
    assert len(sealed.cases) == 12
    assert set(train.cases).isdisjoint(sealed.cases)


def test_train_monitor_is_a_typed_train_only_subset() -> None:
    train = EvalContract.model_validate(
        yaml.safe_load(Path("configs/eval/train-family-v2.yaml").read_text())
    )
    monitor = EvalContract.model_validate(
        yaml.safe_load(Path("configs/eval/train-monitor-v1.yaml").read_text())
    )
    sealed = EvalContract.model_validate(
        yaml.safe_load(Path("configs/eval/sealed-family-v2.yaml").read_text())
    )

    assert len(monitor.cases) == 6
    assert set(monitor.cases) < set(train.cases)
    assert set(monitor.cases).isdisjoint(sealed.cases)


def test_root_identity_must_be_exactly_one_typed_variant() -> None:
    with pytest.raises(ValidationError):
        EvalContract.model_validate(
            {
                "cases": {
                    "bad": {
                        "expected_status": "confirmed",
                        "roots": [
                            {
                                "target_ids": ["service-id"],
                                "target_aliases": ["service"],
                                "pseudo_kind": "external_dependency",
                            }
                        ],
                    }
                }
            }
        )

    with pytest.raises(ValidationError):
        EvalContract.model_validate(
            {
                "cases": {
                    "bad": {
                        "expected_status": "provisional",
                        "roots": [{"pseudo_kind": "external_dependency"}],
                    }
                }
            }
        )


def test_multi_root_f1_penalizes_missing_and_extra_roots() -> None:
    expected = [
        {
            "target_ids": ["inventory-id"],
            "target_aliases": ["inventory-service"],
        },
        {
            "pseudo_kind": "external_dependency",
            "pseudo_ids": ["external:external-pg"],
            "boundary_target_ids": ["payment-id"],
            "boundary_target_aliases": ["payment-service"],
        },
    ]
    assert root_f1(
        expected,
        [
            {
                "variant": "target",
                "target_id": "inventory-id",
                "target_name": "commerce-inventory-service",
            },
            {
                "variant": "pseudo",
                "pseudo_id": "external:external-pg",
                "pseudo_kind": "external_dependency",
                "boundary_target": "payment-id",
            },
        ],
    ) == 1
    partial = root_f1(
        expected,
        [
            {
                "variant": "target",
                "target_id": "inventory-id",
                "target_name": "commerce-inventory-service",
            }
        ],
    )
    extra = root_f1(
        expected,
        [
            {
                "variant": "target",
                "target_id": "inventory-id",
                "target_name": "commerce-inventory-service",
            },
            {
                "variant": "pseudo",
                "pseudo_id": "external:external-pg",
                "pseudo_kind": "external_dependency",
                "boundary_target": "payment-id",
            },
            {
                "variant": "target",
                "target_id": "unrelated-id",
                "target_name": "unrelated",
            },
        ],
    )
    assert 0 < partial < 1
    assert 0 < extra < 1


def test_internal_root_requires_exact_canonical_id_not_matching_name() -> None:
    expected = [
        {
            "target_ids": ["canonical-payment-id"],
            "target_aliases": ["payment-service"],
        }
    ]

    assert root_f1(
        expected,
        [
            {
                "variant": "target",
                "target_id": "canonical-payment-id",
                "target_name": "name-does-not-matter",
            }
        ],
    ) == 1.0
    assert root_f1(
        expected,
        [
            {
                "variant": "target",
                "target_id": "wrong-id",
                "target_name": "payment-service",
            }
        ],
    ) == 0.0


def test_pseudo_root_requires_identity_and_boundary_not_just_kind() -> None:
    expected = [
        {
            "pseudo_kind": "external_dependency",
            "pseudo_ids": ["external:external-pg"],
            "boundary_target_ids": ["payment-id"],
            "boundary_target_aliases": ["commerce-payment"],
        }
    ]

    assert root_f1(
        expected,
        [
            {
                "variant": "pseudo",
                "pseudo_id": "external:external-pg",
                "pseudo_kind": "external_dependency",
                "boundary_target": "payment-id",
            }
        ],
    ) == 1.0
    assert root_f1(
        expected,
        [
            {
                "variant": "pseudo",
                "pseudo_id": "external:payment",
                "pseudo_kind": "external_dependency",
                "boundary_target": "payment-id",
            }
        ],
    ) == 0.0
    assert root_f1(
        expected,
        [
            {
                "variant": "pseudo",
                "pseudo_id": "external:external-pg",
                "pseudo_kind": "external_dependency",
                "boundary_target": "unrelated-service",
            }
        ],
    ) == 0.0


def test_target_name_extraction_uses_runtime_prompt_registry() -> None:
    target_id = "11111111-1111-1111-1111-111111111111"
    episode = {
        "prompts": [
            {
                "Messages": [
                    {
                        "content": f"- {target_id} (commerce-inventory): n=2 max=3.0"
                    }
                ]
            }
        ]
    }
    assert target_names(episode)[target_id] == "commerce-inventory"


def test_format_errors_exclude_typed_semantic_proof_rejections() -> None:
    ledger = [
        {"error_kind": "schema_format", "summary": "invalid JSON"},
        {"error_kind": "semantic_policy", "summary": "proof rejected"},
        {
            "summary": (
                "structured response validation failed after 2 attempts: "
                "causes[0] proof not satisfied: 확정 불가"
            )
        },
    ]

    assert count_format_errors(ledger) == 1


def test_legacy_untyped_structured_failure_remains_a_format_error() -> None:
    ledger = [{"summary": "structured response validation failed after 2 attempts: bad JSON"}]

    assert count_format_errors(ledger) == 1


def test_exact_provisional_root_requires_grounded_evidence_for_strict_success() -> None:
    target = "11111111-1111-1111-1111-111111111111"
    expected = {
        "expected_status": "provisional",
        "roots": [{"target_ids": [target], "target_aliases": ["service-a"]}],
    }
    prompt = {"Messages": [{"content": f"- {target} (service-a): n=1"}]}
    result = {
        "status": "provisional",
        "causes": [{"target": target, "mechanism": "", "support_refs": []}],
    }

    score = score_episode("case-a", expected, {"result": result, "prompts": [prompt]})

    assert score["root_f1"] == 1.0
    assert score["evidence_complete"] is False
    assert score["strict_correct"] is False


def test_directory_report_exposes_evidence_and_strict_coverage(tmp_path: Path) -> None:
    target = "11111111-1111-1111-1111-111111111111"
    path = tmp_path / "case-a" / "traj-run1" / "agent-a.jsonl"
    path.parent.mkdir(parents=True)
    episode = {
        "event": "episode_completed",
        "result": {
            "status": "provisional",
            "causes": [
                {
                    "target": target,
                    "mechanism": "observed causal path",
                    "support_refs": ["obs-1"],
                    "proof_valid": False,
                }
            ],
        },
        "prompts": [
            {"Messages": [{"content": f"- {target} (service-a): n=1"}]}
        ],
        "ledger": [
            {
                "id": "obs-1",
                "action": "env_entity",
                "ok": True,
                "evidence_refs": ["ev-1"],
            },
            {"action": "discover_metrics", "ok": True},
            {"action": "metric_fetch_raw", "ok": True},
            {"action": "probe_traces", "ok": True},
            {"ok": False, "summary": "repeated action rejected"},
        ],
    }
    path.write_text(json.dumps(episode) + "\n")
    contract = {
        "cases": {
            "case-a": {
                "expected_status": "provisional",
                "roots": [
                    {"target_ids": [target], "target_aliases": ["service-a"]}
                ],
            }
        }
    }

    report = score_directory(tmp_path, contract, runs_per_case=1)

    assert report["strict_correct_runs"] == 1
    assert report["evidence_complete_runs"] == 1
    assert report["mean_proof_rate"] == 0.0
    assert report["behavior"] == {
        "action_counts": {
            "discover_metrics": 1,
            "env_entity": 1,
            "metric_fetch_raw": 1,
            "probe_traces": 1,
        },
        "runs_using_metric_actions": 1,
        "runs_using_specialized_probes": 1,
        "mean_distinct_actions": 4.0,
        "mean_rejected_actions": 1.0,
        "mean_turns": 5.0,
    }


def test_behavior_diagnostics_do_not_require_metric_actions_for_success() -> None:
    target = "11111111-1111-1111-1111-111111111111"
    expected = {
        "expected_status": "provisional",
        "roots": [{"target_ids": [target], "target_aliases": ["service-a"]}],
    }
    episode = {
        "result": {
            "status": "provisional",
            "causes": [
                {
                    "target": target,
                    "mechanism": "trace boundary failure",
                    "support_refs": ["obs-1"],
                }
            ],
        },
        "prompts": [{"Messages": [{"content": f"- {target} (service-a): n=1"}]}],
        "ledger": [
            {
                "id": "obs-1",
                "action": "probe_traces",
                "ok": True,
                "evidence_refs": ["ev-1"],
            }
        ],
    }

    score = score_episode("case-a", expected, episode)

    assert score["strict_correct"] is True
    assert score["used_metric_action"] is False
    assert score["used_specialized_probe"] is True


def test_score_directory_rejects_incomplete_run_sets(tmp_path: Path) -> None:
    contract = {
        "cases": {
            "case-a": {
                "expected_status": "provisional",
                "roots": [{"target_ids": ["root-a"]}],
            },
            "case-b": {
                "expected_status": "provisional",
                "roots": [{"target_ids": ["root-b"]}],
            },
        }
    }
    episode_dir = tmp_path / "case-a" / "traj-run1"
    episode_dir.mkdir(parents=True)
    (episode_dir / "agent-a.jsonl").write_text(
        json.dumps(
            {
                "event": "episode_completed",
                "result": {
                    "status": "provisional",
                    "causes": [],
                    "external_causes": [],
                    "turns": 1,
                },
                "ledger": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="case-b/traj-run1=0/1"):
        score_directory(tmp_path, contract, runs_per_case=1)

    duplicate = episode_dir / "agent-duplicate.jsonl"
    duplicate.write_text((episode_dir / "agent-a.jsonl").read_text(), encoding="utf-8")
    case_b = tmp_path / "case-b" / "traj-run1"
    case_b.mkdir(parents=True)
    (case_b / "agent-b.jsonl").write_text(
        (episode_dir / "agent-a.jsonl").read_text(), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="case-a/traj-run1=2/1"):
        score_directory(tmp_path, contract, runs_per_case=1)


def test_external_episode_requires_canonical_identity_linked_boundary_and_known_refs() -> None:
    target = "11111111-1111-1111-1111-111111111111"
    expected = {
        "expected_status": "provisional",
        "roots": [
            {"target_ids": [target], "target_aliases": ["payment-service"]},
            {
                "pseudo_kind": "external_dependency",
                "pseudo_ids": ["external:external-pg"],
                "boundary_target_ids": [target],
                "boundary_target_aliases": ["payment-service"],
            },
        ],
    }
    prompt = {"Messages": [{"content": f"- {target} (payment-service): n=2"}]}
    base_result = {
        "status": "provisional",
        "causes": [
            {
                "target": target,
                "mechanism": "external 429 crossed the payment boundary",
                "support_refs": ["ev-1"],
                "proof_valid": False,
            }
        ],
        "external_causes": [
            {
                "id": "external:external-pg",
                "kind": "external_dependency",
                "name": "external-pg",
                "boundary_target": target,
                "evidence_refs": ["ev-1"],
            }
        ],
    }
    episode = {
        "result": base_result,
        "prompts": [prompt],
        "ledger": [
            {
                "id": "obs-1",
                "action": "probe_traces",
                "ok": True,
                "evidence_refs": ["ev-1"],
            }
        ],
    }

    exact = score_episode("case-external", expected, episode)
    assert exact["root_f1"] == 1.0
    assert exact["evidence_complete"] is True
    assert exact["strict_correct"] is True

    wrong_identity = json.loads(json.dumps(episode))
    wrong_identity["result"]["external_causes"][0]["id"] = "external:payment"
    wrong = score_episode("case-external", expected, wrong_identity)
    assert wrong["root_f1"] == pytest.approx(0.5)
    assert wrong["strict_correct"] is False

    unknown_ref = json.loads(json.dumps(episode))
    unknown_ref["result"]["external_causes"][0]["evidence_refs"] = ["ev-missing"]
    incomplete = score_episode("case-external", expected, unknown_ref)
    assert incomplete["evidence_complete"] is False
    assert incomplete["strict_correct"] is False
