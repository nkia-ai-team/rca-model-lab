from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "run_prime_trainer.sh"


def test_trainer_launcher_supplies_single_process_distributed_contract(tmp_path: Path) -> None:
    checkout = tmp_path / "prime"
    torchrun = checkout / ".venv" / "bin" / "torchrun"
    torchrun.parent.mkdir(parents=True)
    invocation = tmp_path / "invocation"
    torchrun.write_text(
        f"#!/usr/bin/env bash\npwd > {invocation!s}\nprintf '%s\\n' \"$@\" >> {invocation!s}\n",
        encoding="utf-8",
    )
    torchrun.chmod(0o755)
    config = tmp_path / "trainer.toml"
    config.write_text("max_steps = 1\n", encoding="utf-8")

    subprocess.run([SCRIPT, checkout, config], check=True, env=os.environ.copy())

    assert invocation.read_text(encoding="utf-8").splitlines() == [
        str(checkout),
        "--standalone",
        "--nnodes=1",
        "--nproc-per-node=1",
        "-m",
        "prime_rl.entrypoints.trainer",
        "@",
        str(config),
    ]
