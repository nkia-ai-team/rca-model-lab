from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    spec = importlib.util.spec_from_file_location(
        "run_sealed_eval", Path("scripts/run_sealed_eval.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_eval_manifest_seals_model_restore_and_case_artifacts(tmp_path: Path) -> None:
    module = _module()
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text('{"model_type":"test"}\n')
    agent = tmp_path / "agent"
    restore = tmp_path / "restore"
    split = tmp_path / "split.yaml"
    agent.write_bytes(b"agent")
    restore.write_bytes(b"restore")
    split.write_text("sealed_eval: [case-a]\n")
    case = tmp_path / "cases" / "case-a"
    case.mkdir(parents=True)
    (case / "meta.json").write_text('{"case":"a"}\n')
    args = argparse.Namespace(
        model="actor",
        model_artifact=str(model),
        model_artifact_sha256="",
        base_url="http://localhost:8002/v1",
        structured_backend="guidance",
        reasoning_strength="low",
        restore_timeout=900,
        runs=3,
        partition="sealed_eval",
        agent=agent,
        restore=restore,
        split=split,
        case_root=tmp_path / "cases",
    )

    manifest = module.eval_manifest_contract(args, ["case-a"])

    assert len(manifest["model_artifact_sha256"]) == 64
    assert len(manifest["agent_sha256"]) == 64
    assert len(manifest["restore_sha256"]) == 64
    assert len(manifest["case_set_sha256"]) == 64
    assert manifest["actor_temperature"] == 0.0
    assert manifest["actor_seed"] == 0
    assert manifest["reasoning_strength"] == "low"
    assert manifest["request_contract_enforced"] is True
    assert manifest["restore_timeout_seconds"] == 900


def test_served_model_ids_rejects_stale_or_malformed_endpoint() -> None:
    module = _module()

    assert module.served_model_ids({"data": [{"id": "rca-actor"}]}) == {"rca-actor"}
    assert module.served_model_ids({"data": [{"id": "other"}]}) == {"other"}
    assert module.served_model_ids({"unexpected": []}) == set()


def test_case_is_complete_requires_every_log_and_terminal_episode(tmp_path: Path) -> None:
    module = _module()
    case_dir = tmp_path / "case-a"
    for run in (1, 2, 3):
        trajectory = case_dir / f"traj-run{run}" / f"agent-{run}.jsonl"
        trajectory.parent.mkdir(parents=True)
        trajectory.write_text(
            json.dumps({"event": "episode_completed", "run": run}) + "\n",
            encoding="utf-8",
        )
        (case_dir / f"agent-run{run}.log").write_text("OUTPUT | ok\n", encoding="utf-8")

    assert module.case_is_complete(case_dir, 3)
    (case_dir / "agent-run3.log").unlink()
    assert not module.case_is_complete(case_dir, 3)


def test_resume_manifest_must_match_every_immutable_field(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "run-manifest.json"
    path.write_text(json.dumps({"created_at": "old", "model": "sft", "runs": 3}) + "\n")

    assert module.validate_resume_manifest(path, {"model": "sft", "runs": 3})["model"] == "sft"
    with pytest.raises(SystemExit, match="resume manifest mismatch"):
        module.validate_resume_manifest(path, {"model": "rl", "runs": 3})


def test_archive_incomplete_case_preserves_partial_artifacts(tmp_path: Path) -> None:
    module = _module()
    case_dir = tmp_path / "case-a"
    case_dir.mkdir()
    (case_dir / "restore.log").write_text("partial\n")

    module.archive_incomplete_case(case_dir, tmp_path / ".interrupted")

    assert not case_dir.exists()
    archived = list((tmp_path / ".interrupted").glob("case-a-*"))
    assert len(archived) == 1
    assert (archived[0] / "restore.log").read_text() == "partial\n"


def test_output_lock_rejects_concurrent_evaluator(tmp_path: Path) -> None:
    module = _module()
    output = tmp_path / "eval"
    output.mkdir()

    lock = module.acquire_output_lock(output)
    try:
        with pytest.raises(SystemExit, match="already active"):
            module.acquire_output_lock(output)
    finally:
        lock.close()
