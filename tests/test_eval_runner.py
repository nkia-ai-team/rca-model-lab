from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


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
        base_url="http://localhost:8002/v1",
        structured_backend="guidance",
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
