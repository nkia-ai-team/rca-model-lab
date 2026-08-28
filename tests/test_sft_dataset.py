import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from rca_lab.data.sft import build_sft_dataset
from rca_lab.eval.scoring import EvalContract
from rca_lab.harness.models import ActionRequest
from rca_lab.scenarios.split import TeacherSplit


def split() -> TeacherSplit:
    return TeacherSplit(
        name="test",
        teacher="codex",
        accepted_trajectories_per_case=1,
        max_attempts_per_case=2,
        max_turns=12,
        train=("case-train",),
        sealed_eval=("case-eval",),
        excluded=("case-excluded",),
    )


def write_steps(path: Path, *, ok: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "turn": turn,
                    "system": "system",
                    "user": f"full-state-{turn}",
                    "action": {
                        "action": "probe_logs",
                        "arg1": "target-1",
                        "answer": {"status": "insufficient", "causes": [], "ready": False},
                    },
                    "ok": ok,
                    "obs": "no logs" if not ok else "logs",
                }
            )
            for turn in (1, 2)
        )
        + "\n",
        encoding="utf-8",
    )


def test_build_sft_dataset_keeps_recovery_attempts_and_excludes_recursive_failures(
    tmp_path: Path,
) -> None:
    write_steps(tmp_path / "case-train" / "accepted.episode.jsonl", ok=False)
    write_steps(tmp_path / "case-train" / "branches" / "branch-001" / "episode.rec.jsonl")

    records, manifest = build_sft_dataset(synth_root=tmp_path, split=split(), expected_scenarios=1)

    assert len(records) == 1
    assert manifest.trajectory_count == 1
    assert manifest.failed_observation_count == 2
    assert len(manifest.trajectory_sha256) == 1
    assert records[0].turn_count == 2
    assert [turn.messages[1].content for turn in records[0].turns] == [
        "full-state-1",
        "full-state-2",
    ]
    second_action = json.loads(records[0].turns[1].messages[2].content)
    assert second_action["query"] == {}
    assert second_action["refresh"] is False
    assert second_action["answer"]["external_causes"] == []
    assert ActionRequest.model_validate(second_action).action.value == "probe_logs"


def test_build_sft_dataset_rejects_sealed_eval_leakage(tmp_path: Path) -> None:
    write_steps(tmp_path / "case-eval" / "leak.episode.jsonl")

    with pytest.raises(ValueError, match="sealed eval"):
        build_sft_dataset(synth_root=tmp_path, split=split())


def test_build_sft_dataset_migrates_legacy_cause_and_external_fields(tmp_path: Path) -> None:
    path = tmp_path / "case-train" / "legacy.episode.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "turn": 1,
                "system": "system",
                "user": "visible ev001 obs-001",
                "action": {
                    "action": "answer",
                    "answer": {
                        "status": "provisional",
                        "summary": "external rate limit",
                        "causes": [
                            {
                                "target_id": "target-1",
                                "mechanism": "429 propagated",
                                "proof_type": "trace_boundary",
                                "confidence": 0.9,
                                "support_refs": ["ev001"],
                            }
                        ],
                        "external_causes": [
                            {
                                "type": "external_http_dependency",
                                "name": "external-pg",
                                "boundary_target_id": "target-1",
                                "support_refs": ["obs-001"],
                            }
                        ],
                        "ready": True,
                    },
                },
                "ok": True,
                "obs": "done",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    records, _ = build_sft_dataset(synth_root=tmp_path, split=split())
    action = json.loads(records[0].turns[0].messages[2].content)

    assert action["thought"] == "external rate limit"
    assert action["answer"]["causes"][0]["target"] == "target-1"
    assert action["answer"]["external_causes"][0] == {
        "id": "external:external-pg",
        "kind": "external_dependency",
        "name": "external-pg",
        "boundary_target": "target-1",
        "evidence_refs": ["obs-001"],
    }


def test_build_sft_dataset_enforces_typed_terminal_contract(tmp_path: Path) -> None:
    path = tmp_path / "case-train" / "accepted.episode.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "turn": 1,
                "system": "system",
                "user": "visible obs-001",
                "action": {
                    "action": "answer",
                    "answer": {
                        "status": "confirmed",
                        "ready": True,
                        "causes": [
                            {
                                "target": "wrong-target",
                                "mechanism": "wrong",
                                "support_refs": ["obs-001"],
                            }
                        ],
                    },
                },
                "ok": True,
                "obs": "done",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    contract = EvalContract.model_validate(
        {
            "version": 1,
            "cases": {
                "case-train": {
                    "expected_status": "confirmed",
                    "roots": [{"target_ids": ["canonical-target"]}],
                }
            },
        }
    )

    with pytest.raises(ValueError, match="terminal root_f1=0.000"):
        build_sft_dataset(synth_root=tmp_path, split=split(), contract=contract)


def test_split_rejects_family_leakage() -> None:
    with pytest.raises(ValidationError, match="families overlap"):
        TeacherSplit(
            name="leaky",
            teacher="codex",
            accepted_trajectories_per_case=1,
            max_attempts_per_case=2,
            max_turns=12,
            train=("case-f04-r-v3-a",),
            sealed_eval=("case-f04-h-v3-b",),
            excluded=(),
        )


def test_real_teacher_dataset_is_family_clean_and_actions_match_runtime() -> None:
    teacher_split = TeacherSplit.model_validate(
        __import__("yaml").safe_load(
            Path("configs/teacher/codex-blind-v1.yaml").read_text(encoding="utf-8")
        )
    )
    records, manifest = build_sft_dataset(
        synth_root=Path("data/synth/teacher-v1"),
        split=teacher_split,
        expected_scenarios=20,
    )

    assert manifest.trajectory_count == 24
    assert manifest.turn_count > 0
    assert not {"case-f20-r-v3-caf04820", "case-f23-r-v3-04981a78", "case-f25-h-v3-dc3d4fc8"} & set(
        manifest.scenarios
    )
    for record in records:
        for turn in record.turns:
            ActionRequest.model_validate_json(turn.messages[2].content)
    reward_cases = set(
        yaml.safe_load(Path("configs/eval/train-family-v2.yaml").read_text(encoding="utf-8"))[
            "cases"
        ]
    )
    assert set(manifest.scenarios) == reward_cases
