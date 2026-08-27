from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from rca_lab.eval.scoring import EvalContract, count_format_errors, root_f1, target_names


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


def test_root_identity_must_be_exactly_one_typed_variant() -> None:
    with pytest.raises(ValidationError):
        EvalContract.model_validate(
            {
                "cases": {
                    "bad": {
                        "expected_status": "confirmed",
                        "roots": [
                            {
                                "target_aliases": ["service"],
                                "pseudo_kind": "external_dependency",
                            }
                        ],
                    }
                }
            }
        )


def test_multi_root_f1_penalizes_missing_and_extra_roots() -> None:
    expected = [
        {"target_aliases": ["inventory-service"]},
        {"pseudo_kind": "external_dependency"},
    ]
    assert root_f1(
        expected, [("target", "commerce-inventory-service"), ("pseudo", "external_dependency")]
    ) == 1
    partial = root_f1(expected, [("target", "commerce-inventory-service")])
    extra = root_f1(
        expected,
        [
            ("target", "commerce-inventory-service"),
            ("pseudo", "external_dependency"),
            ("target", "unrelated"),
        ],
    )
    assert 0 < partial < 1
    assert 0 < extra < 1


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
