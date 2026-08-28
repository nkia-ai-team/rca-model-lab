from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    spec = importlib.util.spec_from_file_location(
        "score_eval_runs", Path("scripts/score_eval_runs.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest() -> dict[str, object]:
    return {
        "model_artifact_sha256": "a" * 64,
        "agent_sha256": "b" * 64,
        "restore_sha256": "c" * 64,
        "split_sha256": "d" * 64,
        "case_set_sha256": "e" * 64,
        "structured_output_backend": "guidance",
        "actor_temperature": 0.0,
        "actor_seed": 0,
        "partition": "sealed_eval",
        "runs": 3,
        "cases": ["case-a", "case-b"],
    }


def test_evaluation_provenance_binds_manifest_to_contract_population(tmp_path: Path) -> None:
    module = _module()
    output = tmp_path / "eval"
    output.mkdir()
    (output / "run-manifest.json").write_text(json.dumps(_manifest()) + "\n")
    contract = tmp_path / "contract.yaml"
    contract.write_text("cases: {}\n")

    provenance = module.evaluation_provenance(
        output,
        contract,
        expected_cases=["case-a", "case-b"],
        expected_runs=3,
    )

    assert provenance["cases"] == ["case-a", "case-b"]
    assert provenance["runs"] == 3


@pytest.mark.parametrize(
    ("cases", "runs", "message"),
    [
        (["case-b", "case-a"], 3, "case order/population"),
        (["case-a", "case-b"], 2, "run count"),
    ],
)
def test_evaluation_provenance_rejects_manifest_contract_drift(
    tmp_path: Path, cases: list[str], runs: int, message: str
) -> None:
    module = _module()
    output = tmp_path / "eval"
    output.mkdir()
    manifest = _manifest()
    manifest["cases"] = cases
    manifest["runs"] = runs
    (output / "run-manifest.json").write_text(json.dumps(manifest) + "\n")
    contract = tmp_path / "contract.yaml"
    contract.write_text("cases: {}\n")

    with pytest.raises(ValueError, match=message):
        module.evaluation_provenance(
            output,
            contract,
            expected_cases=["case-a", "case-b"],
            expected_runs=3,
        )
