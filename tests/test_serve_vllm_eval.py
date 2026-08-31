from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "serve_vllm_eval.sh"


def _fake_vllm(tmp_path: Path) -> tuple[Path, Path, Path]:
    output = tmp_path / "args.txt"
    python_no_user_site = tmp_path / "python-no-user-site.txt"
    executable = tmp_path / "vllm"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$CAPTURE_ARGS\"\n"
        "printf '%s\\n' \"${PYTHONNOUSERSITE:-}\" > \"$CAPTURE_PYTHONNOUSERSITE\"\n"
        "if [[ -v PYTHONPATH ]]; then printf 'set\\n'; else printf 'unset\\n'; fi "
        ">> \"$CAPTURE_PYTHONNOUSERSITE\"\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, output, python_no_user_site


def test_serves_adapter_without_merging(tmp_path: Path) -> None:
    base = tmp_path / "base"
    adapter = tmp_path / "adapter"
    base.mkdir()
    adapter.mkdir()
    (adapter / "adapter_config.json").write_text("{}\n", encoding="utf-8")
    executable, output, python_no_user_site = _fake_vllm(tmp_path)
    env = os.environ | {
        "VLLM_BIN": str(executable),
        "CAPTURE_ARGS": str(output),
        "CAPTURE_PYTHONNOUSERSITE": str(python_no_user_site),
    }

    subprocess.run(
        [str(SCRIPT), str(base), "8002", "rca-actor", str(adapter)],
        check=True,
        env=env,
    )

    args = output.read_text(encoding="utf-8").splitlines()
    assert python_no_user_site.read_text(encoding="utf-8").splitlines() == [
        "1",
        "unset",
    ]
    assert args[args.index("--served-model-name") + 1] == "rca-actor-base"
    assert args[args.index("--gpu-memory-utilization") + 1] == "0.95"
    assert args[args.index("--max-num-seqs") + 1] == "8"
    assert args[args.index("--max-num-batched-tokens") + 1] == "16384"
    assert "--enable-chunked-prefill" in args
    assert "--language-model-only" in args
    assert "--enable-lora" in args
    assert args[args.index("--max-lora-rank") + 1] == "8"
    target_index = args.index("--lora-target-modules")
    assert args[target_index + 1 : target_index + 8] == [
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj",
        "up_proj",
        "down_proj",
        "gate_proj",
    ]
    assert args[args.index("--lora-modules") + 1] == f"rca-actor={adapter}"


def test_base_model_contract_remains_backward_compatible(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    executable, output, python_no_user_site = _fake_vllm(tmp_path)
    env = os.environ | {
        "VLLM_BIN": str(executable),
        "CAPTURE_ARGS": str(output),
        "CAPTURE_PYTHONNOUSERSITE": str(python_no_user_site),
    }

    subprocess.run(
        [str(SCRIPT), str(base), "8002", "rca-actor"], check=True, env=env
    )

    args = output.read_text(encoding="utf-8").splitlines()
    assert args[args.index("--served-model-name") + 1] == "rca-actor"
    assert "--enable-lora" not in args


def test_inference_capacity_can_be_tuned_without_editing_script(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    executable, output, python_no_user_site = _fake_vllm(tmp_path)
    env = os.environ | {
        "VLLM_BIN": str(executable),
        "CAPTURE_ARGS": str(output),
        "CAPTURE_PYTHONNOUSERSITE": str(python_no_user_site),
        "VLLM_GPU_MEMORY_UTILIZATION": "0.92",
        "VLLM_MAX_NUM_SEQS": "12",
        "VLLM_MAX_NUM_BATCHED_TOKENS": "24576",
    }

    subprocess.run(
        [str(SCRIPT), str(base), "8002", "rca-actor"], check=True, env=env
    )

    args = output.read_text(encoding="utf-8").splitlines()
    assert args[args.index("--gpu-memory-utilization") + 1] == "0.92"
    assert args[args.index("--max-num-seqs") + 1] == "12"
    assert args[args.index("--max-num-batched-tokens") + 1] == "24576"


def test_inference_can_pin_the_training_tokenizer_without_dequantizing_fp8(
    tmp_path: Path,
) -> None:
    base = tmp_path / "fp8-base"
    tokenizer = tmp_path / "tokenizer"
    base.mkdir()
    tokenizer.mkdir()
    executable, output, python_no_user_site = _fake_vllm(tmp_path)
    env = os.environ | {
        "VLLM_BIN": str(executable),
        "VLLM_TOKENIZER_PATH": str(tokenizer),
        "CAPTURE_ARGS": str(output),
        "CAPTURE_PYTHONNOUSERSITE": str(python_no_user_site),
    }

    subprocess.run([str(SCRIPT), str(base)], check=True, env=env)

    args = output.read_text(encoding="utf-8").splitlines()
    assert args[args.index("--tokenizer") + 1] == str(tokenizer)
    assert "--dtype" not in args
