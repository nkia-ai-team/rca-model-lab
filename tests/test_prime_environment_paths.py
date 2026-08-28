from __future__ import annotations

from pathlib import Path

from rca_lab.prime_rl.paths import project_path, project_root


def test_prime_environment_assets_follow_current_checkout() -> None:
    root = Path(__file__).parents[1].resolve()
    assert project_root() == root
    assert project_path("bin/rca-agent-v6") == root / "bin/rca-agent-v6"
    assert project_path("scripts/restore_eval_case.sh") == root / "scripts/restore_eval_case.sh"


def test_prime_environment_root_can_be_explicitly_overridden(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pyproject.toml").touch()
    monkeypatch.setenv("RCA_LAB_ROOT", str(tmp_path))
    assert project_root() == tmp_path
    assert project_path("bin/rca-agent-v6") == tmp_path / "bin/rca-agent-v6"
