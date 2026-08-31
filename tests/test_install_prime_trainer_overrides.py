from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "install_prime_trainer_overrides.sh"


def test_installer_resolves_overrides_outside_the_prime_workspace(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    invocation = tmp_path / "invocation"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"pwd > {invocation!s}\n"
        f"printf '%s\\n' \"$@\" >> {invocation!s}\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    # Stop after uv invocation: this test locks workspace isolation and argv;
    # distribution-version verification is exercised on the real trainer.
    fake_python = tmp_path / "python"
    fake_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)

    env = os.environ | {"UV_BIN": str(fake_uv)}
    subprocess.run([SCRIPT, fake_python], check=True, env=env)

    lines = invocation.read_text(encoding="utf-8").splitlines()
    assert Path(lines[0]).parent == Path("/tmp")
    assert lines[1:4] == ["pip", "install", "--python"]
    assert lines[4] == str(fake_python)
    assert lines[5] == "-r"
    assert lines[6].endswith("/integrations/prime_rl/trainer-overrides.txt")
