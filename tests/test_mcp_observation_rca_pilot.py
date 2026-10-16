from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mcp_observation_rca_pilot.py"
SPEC = importlib.util.spec_from_file_location("mcp_observation_rca_pilot", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)


def test_identical_resume_preserves_original_manifest(tmp_path: Path) -> None:
    config = {"created_at_utc": "first", "seed": 7}
    provenance = {"files_sha256": {"source": "abc"}}
    pilot.persist_run_contract(tmp_path, config, provenance)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    pilot.persist_run_contract(tmp_path, {**config, "created_at_utc": "later"}, provenance)
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


@pytest.mark.parametrize("change", ["seed", "source"])
def test_drift_is_rejected_without_overwriting(tmp_path: Path, change: str) -> None:
    config = {"seed": 7}
    provenance = {"files_sha256": {"source": "abc"}}
    pilot.persist_run_contract(tmp_path, config, provenance)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(RuntimeError, match="changed"):
        pilot.persist_run_contract(
            tmp_path,
            {"seed": 8} if change == "seed" else config,
            {"files_sha256": {"source": "different"}} if change == "source" else provenance,
        )
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_missing_provenance_does_not_adopt_existing_scores(tmp_path: Path) -> None:
    score = tmp_path / "arm" / "run1" / "score.json"
    score.parent.mkdir(parents=True)
    score.write_text('{}')
    with pytest.raises(RuntimeError, match="Incomplete"):
        pilot.persist_run_contract(tmp_path, {"seed": 7}, {})
    assert not (tmp_path / "runtime-config.json").exists()
