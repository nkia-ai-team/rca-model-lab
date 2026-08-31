from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "install_prime_orchestrator.sh"


def test_orchestrator_installer_isolates_post_sync_overrides(tmp_path: Path) -> None:
    checkout = tmp_path / "prime"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\nname='prime'\n", encoding="utf-8")
    python = checkout / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)

    invocation = tmp_path / "invocation"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf 'cwd=%s\\n' \"$PWD\" >> {invocation!s}\n"
        f"printf 'arg=%s\\n' \"$@\" >> {invocation!s}\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    subprocess.run(
        [SCRIPT, checkout],
        check=True,
        env=os.environ | {"UV_BIN": str(fake_uv)},
    )

    lines = invocation.read_text(encoding="utf-8").splitlines()
    working_directories = [line.removeprefix("cwd=") for line in lines if line.startswith("cwd=")]
    assert working_directories[0] == str(checkout)
    assert all(Path(path).parent == Path("/tmp") for path in working_directories[1:])
    assert lines.count("arg=pip") == 3
    assert "arg=https://download.pytorch.org/whl/cpu" in lines
    assert f"arg={Path(__file__).parents[1] / 'environments' / 'rca_student'}" in lines
