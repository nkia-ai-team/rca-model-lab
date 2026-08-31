from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

from rca_lab.prime_rl.weight_relay import WeightRelayConfig

ROOT = Path(__file__).parents[1]
CONFIG_DIR = ROOT / "configs" / "prime_rl" / "rca-online-smoke"
INTEGRATION_DIR = ROOT / "integrations" / "prime_rl"


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
    assert config["model"]["name"].endswith("/muse-glimmer-30b-fp8-block-bf16-train-cache")
    assert lora["init_adapter"].endswith("/rca-adapters/muse-glimmer-30b-teacher-v4-high/adapter")
    assert config["tokenizer"]["name"].endswith("/models/muse-glimmer-30b-tokenizer")
    assert lora["target_modules"] == [
        r"^model\.language_model\..*\.(q_proj|k_proj|v_proj|o_proj|up_proj|down_proj|gate_proj)$"
    ]
    assert config["loss"] == {
        "type": "ipo",
        "eps": 0.1,
        "adv_tau": 1.0,
        "kl_tau": 0.001,
    }


def test_trainer_runtime_uses_muse_capable_transformers_and_metrics_dependency() -> None:
    requirements = {
        line
        for raw in (INTEGRATION_DIR / "trainer-overrides.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    }
    assert requirements == {
        "transformers==5.15.1",
        "prometheus-client==0.22.1",
    }


def test_orchestrator_uses_online_group_rollouts_and_production_harness() -> None:
    config = _toml("orchestrator.toml")
    source = config["train"]["source"][0]
    assert config["batch_size"] == config["group_size"] == 8
    assert config["seq_len"] == 32768
    assert config["train"]["sampling"] == {
        "temperature": 0.7,
        "max_completion_tokens": 2048,
        "extra_body": {
            "chat_template_kwargs": {"reasoning_strength": "high"},
        },
    }
    assert source["env"]["taskset"]["id"] == "rca-student"
    assert source["env"]["agent"]["harness"]["id"] == "rca-student"
    assert source["env"]["max_concurrent_agents"] == config["group_size"]
    assert config["concurrency"]["max_inflight"] == 16


def test_standalone_env_server_runs_the_same_harness_with_eight_slots() -> None:
    config = _toml("env-server.toml")

    assert config["env"]["taskset"]["id"] == "rca-student"
    assert config["env"]["taskset"]["task"]["reward_stage"] == ("exploration_bootstrap")
    assert config["env"]["agent"]["harness"]["id"] == "rca-student"
    assert config["env"]["agent"]["runtime"]["type"] == "subprocess"
    assert config["env"]["max_concurrent_agents"] == 8
    assert config["serve"]["max_concurrent"] == 8
    assert config["serve"]["pool"] == {"type": "static", "num_workers": 1}


def test_progressive_second_stage_removes_exploration_shaping() -> None:
    config = _toml("env-server-diagnosis.toml")

    assert config["env"]["taskset"]["task"]["reward_stage"] == "diagnosis"
    assert config["env"]["agent"]["harness"]["id"] == "rca-student"


def test_progressive_second_stage_resumes_both_sides_from_same_checkpoint() -> None:
    bootstrap_orchestrator = _toml("orchestrator.toml")
    bootstrap_trainer = _toml("trainer.toml")
    diagnosis_orchestrator = _toml("orchestrator-diagnosis.toml")
    diagnosis_trainer = _toml("trainer-diagnosis.toml")

    assert bootstrap_orchestrator["max_steps"] == 3
    assert bootstrap_trainer["max_steps"] == 3
    assert diagnosis_orchestrator["resume"] == {"step": 3}
    assert diagnosis_trainer["resume"] == {"step": 3}
    assert diagnosis_orchestrator["max_steps"] == 6
    assert diagnosis_trainer["max_steps"] == 6
    assert diagnosis_orchestrator["output_dir"] == bootstrap_orchestrator["output_dir"]
    assert diagnosis_trainer["output_dir"] == bootstrap_trainer["output_dir"]
    assert diagnosis_orchestrator["train"] == bootstrap_orchestrator["train"]
    assert diagnosis_trainer["model"] == bootstrap_trainer["model"]
    assert diagnosis_trainer["loss"] == bootstrap_trainer["loss"]
    assert diagnosis_trainer["optim"] == bootstrap_trainer["optim"]


def test_inference_budget_targets_throughput_without_reducing_kv_capacity() -> None:
    config = _toml("inference.toml")["vllm"]
    assert config["served_model_name"] == ["rca-actor"]
    assert config["model"].endswith("/muse-glimmer-30b-fp8-block")
    assert config["tokenizer"].endswith("/models/muse-glimmer-30b-tokenizer")
    assert config["dtype"] == "auto"
    assert config["gpu_memory_utilization"] == 0.95
    assert config["max_num_seqs"] == 8
    assert config["max_num_batched_tokens"] == 16384
    assert config["enable_chunked_prefill"] is True
    assert config["enable_prefix_caching"] is True
    assert config["language_model_only"] is True
    assert config["enable_lora"] is True
    assert config["attention_backend"] == "FLASH_ATTN"
    assert config["kernel_config"]["enable_flashinfer_autotune"] is False


def test_relay_path_is_the_path_prime_sends_to_remote_vllm() -> None:
    relay = WeightRelayConfig.model_validate(
        yaml.safe_load((CONFIG_DIR / "relay.example.yaml").read_text(encoding="utf-8"))
    )
    assert relay.trainer.port == 10515
    assert relay.inference.port == 10658
    orchestrator_broadcasts = Path(_toml("orchestrator.toml")["output_dir"]) / "broadcasts"
    assert relay.local_broadcast_dir == orchestrator_broadcasts
    assert str(orchestrator_broadcasts) == relay.inference.remote_broadcast_dir
