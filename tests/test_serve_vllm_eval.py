from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "serve_vllm_eval.sh"


def _fake_vllm(tmp_path: Path) -> tuple[Path, Path]:
    output = tmp_path / "args.txt"
    executable = tmp_path / "vllm"
    executable.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$CAPTURE_ARGS\"\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, output


def test_serves_adapter_without_merging(tmp_path: Path) -> None:
    base = tmp_path / "base"
    adapter = tmp_path / "adapter"
    base.mkdir()
    adapter.mkdir()
    (adapter / "adapter_config.json").write_text("{}\n", encoding="utf-8")
    executable, output = _fake_vllm(tmp_path)
    env = os.environ | {"VLLM_BIN": str(executable), "CAPTURE_ARGS": str(output)}

    subprocess.run(
        [str(SCRIPT), str(base), "8002", "rca-actor", str(adapter)],
        check=True,
        env=env,
    )

    args = output.read_text(encoding="utf-8").splitlines()
    assert args[args.index("--served-model-name") + 1] == "rca-actor-base"
    assert "--enable-lora" in args
    assert args[args.index("--lora-modules") + 1] == f"rca-actor={adapter}"


def test_base_model_contract_remains_backward_compatible(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    executable, output = _fake_vllm(tmp_path)
    env = os.environ | {"VLLM_BIN": str(executable), "CAPTURE_ARGS": str(output)}

    subprocess.run(
        [str(SCRIPT), str(base), "8002", "rca-actor"], check=True, env=env
    )

    args = output.read_text(encoding="utf-8").splitlines()
    assert args[args.index("--served-model-name") + 1] == "rca-actor"
    assert "--enable-lora" not in args
