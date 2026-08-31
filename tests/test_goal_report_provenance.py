from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from rca_lab.provenance import file_sha256


def _module():
    spec = importlib.util.spec_from_file_location(
        "build_goal_report", Path("scripts/build_goal_report.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stage(
    path: Path,
    cases: list[str],
    *,
    split_sha256: str,
    contract_sha256: str,
    partition: str = "sealed_eval",
) -> None:
    path.write_text(
        json.dumps(
            {
                "evaluation_provenance": {
                    "agent_sha256": "a",
                    "restore_sha256": "b",
                    "split_sha256": split_sha256,
                    "case_set_sha256": "d",
                    "scoring_contract_sha256": contract_sha256,
                    "structured_output_backend": "guidance",
                    "actor_temperature": 0.0,
                    "actor_seed": 0,
                    "reasoning_strength": "low",
                    "request_contract_enforced": True,
                    "restore_timeout_seconds": 900,
                    "partition": partition,
                    "runs": 3,
                    "cases": cases,
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_goal_report_rejects_nonsealed_or_incomparable_stage(tmp_path: Path) -> None:
    module = _module()
    split = Path("configs/teacher/codex-blind-v1.yaml")
    contract = Path("configs/eval/sealed-family-v2.yaml")
    cases = list(yaml.safe_load(contract.read_text(encoding="utf-8"))["cases"])
    baseline = tmp_path / "baseline.json"
    sft = tmp_path / "sft.json"
    rl = tmp_path / "rl.json"
    stage_kwargs = {
        "split_sha256": file_sha256(split),
        "contract_sha256": file_sha256(contract),
    }
    _stage(baseline, cases, **stage_kwargs)
    _stage(sft, cases, **stage_kwargs)
    _stage(rl, cases, partition="train", **stage_kwargs)

    with pytest.raises(ValueError, match="requires sealed_eval"):
        module.build_report(
            split_path=split,
            contract_path=contract,
            baseline_path=baseline,
            sft_path=sft,
            rl_path=rl,
            runs=3,
        )
