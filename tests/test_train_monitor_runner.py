from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


def _module():
    spec = importlib.util.spec_from_file_location(
        "run_train_monitor", Path("scripts/run_train_monitor.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_monitor_command_pins_train_partition_and_every_case() -> None:
    module = _module()
    args = argparse.Namespace(
        split=Path("split.yaml"),
        agent=Path("agent"),
        restore=Path("restore"),
        case_root=Path("cases"),
        output=Path("output"),
        base_url="http://localhost:8002/v1",
        model="rca-actor",
        model_artifact="/remote/model",
        model_artifact_sha256="a" * 64,
        runs=3,
        resume=False,
    )

    command = module.monitor_command(args, ["case-a", "case-b"])

    assert command[command.index("--partition") + 1] == "train"
    assert command.count("--case") == 2
    assert command[-4:] == ["--case", "case-a", "--case", "case-b"]
    assert command[command.index("--model-artifact-sha256") + 1] == "a" * 64


def test_monitor_command_forwards_resume() -> None:
    module = _module()
    args = argparse.Namespace(
        split=Path("split.yaml"),
        agent=Path("agent"),
        restore=Path("restore"),
        case_root=Path("cases"),
        output=Path("output"),
        base_url="http://localhost:8002/v1",
        model="rca-actor",
        model_artifact="/remote/model",
        model_artifact_sha256="a" * 64,
        runs=3,
        resume=True,
    )

    assert module.monitor_command(args, ["case-a"])[-1] == "--resume"
