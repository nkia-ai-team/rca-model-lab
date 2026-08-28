from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

from rca_lab.prime_rl.weight_relay import WeightRelayConfig

ROOT = Path(__file__).parents[1]
CONFIG_DIR = ROOT / "configs" / "prime_rl" / "rca-online-smoke"


def _toml(name: str) -> dict:
    with (CONFIG_DIR / name).open("rb") as stream:
        return tomllib.load(stream)


def test_trainer_starts_rl_from_exact_sft_adapter_without_merging() -> None:
    config = _toml("trainer.toml")
    lora = config["model"]["lora"]
    assert config["model"]["impl"] == "hf"
    assert config["model"]["attn"] == "sdpa"
    assert config["model"]["seq_len"] == 32768
    assert lora["rank"] == 8
    assert lora["init_adapter"].endswith(
        "/outputs/muse-glimmer-30b-teacher-v3-contract-clean/adapter"
    )
    assert lora["target_modules"] == [
        r"^model\.language_model\..*\.(q_proj|k_proj|v_proj|o_proj|up_proj|down_proj|gate_proj)$"
    ]


def test_orchestrator_uses_online_group_rollouts_and_production_harness() -> None:
    config = _toml("orchestrator.toml")
    source = config["train"]["source"][0]
    assert config["batch_size"] == config["group_size"] == 8
    assert config["seq_len"] == 32768
    assert config["train"]["sampling"] == {
        "temperature": 1.0,
        "max_completion_tokens": 2048,
    }
    assert source["env"]["taskset"]["id"] == "rca-student"
    assert source["env"]["agent"]["harness"]["id"] == "rca-student"
    assert config["concurrency"]["max_inflight"] == 16


def test_inference_budget_targets_throughput_without_reducing_kv_capacity() -> None:
    config = _toml("inference.toml")["vllm"]
    assert config["served_model_name"] == ["rca-actor"]
    assert config["gpu_memory_utilization"] == 0.95
    assert config["max_num_seqs"] == 8
    assert config["max_num_batched_tokens"] == 16384
    assert config["enable_chunked_prefill"] is True
    assert config["enable_prefix_caching"] is True
    assert config["enable_lora"] is True


def test_relay_path_is_the_path_prime_sends_to_remote_vllm() -> None:
    relay = WeightRelayConfig.model_validate(
        yaml.safe_load((CONFIG_DIR / "relay.example.yaml").read_text(encoding="utf-8"))
    )
    orchestrator_broadcasts = Path(_toml("orchestrator.toml")["output_dir"]) / "broadcasts"
    assert relay.local_broadcast_dir == orchestrator_broadcasts
    assert str(orchestrator_broadcasts) == relay.inference.remote_broadcast_dir
