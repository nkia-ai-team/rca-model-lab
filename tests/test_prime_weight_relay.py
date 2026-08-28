from __future__ import annotations

from pathlib import Path

import pytest

from rca_lab.prime_rl.weight_relay import (
    FINISHED,
    RECEIVER_READY,
    SENDER_READY,
    SOURCE_GENERATION,
    PrimeLoraWeightRelay,
    PublishedStep,
    SshEndpoint,
    WeightRelayConfig,
)


class FakeTrainer:
    def __init__(self) -> None:
        self.offer = PublishedStep(0, "generation-a")
        self.finished = False
        self.acknowledged: list[int] = []

    def published_steps(self) -> list[PublishedStep]:
        return [self.offer]

    def marker_exists(self, step: int, marker: str) -> bool:
        assert step == self.offer.step
        assert marker == FINISHED
        return self.finished

    def acknowledge(self, step: int) -> None:
        self.acknowledged.append(step)

    def download_step(self, step: int, destination: Path) -> None:
        assert step == self.offer.step
        destination.mkdir()
        (destination / "adapter_config.json").write_text("{}\n", encoding="utf-8")
        (destination / "adapter_model.safetensors").write_bytes(b"adapter")
        (destination / FINISHED).touch()


class FakeInference:
    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[int, list[str]]] = []
        self.fail = fail

    def publish_step(self, step: int, source: Path) -> None:
        if self.fail:
            raise RuntimeError("inference upload failed")
        self.published.append((step, sorted(path.name for path in source.iterdir())))


def test_relay_preserves_prime_handshake_and_commit_order(tmp_path: Path) -> None:
    trainer = FakeTrainer()
    inference = FakeInference()
    relay = PrimeLoraWeightRelay(tmp_path, trainer, inference)

    assert relay.sync_once() == 0
    local_step = tmp_path / "step_0"
    assert (local_step / SENDER_READY).exists()
    assert not trainer.acknowledged

    (local_step / RECEIVER_READY).touch()
    assert relay.sync_once() == 0
    assert trainer.acknowledged == [0]
    assert not (local_step / FINISHED).exists()

    trainer.finished = True
    assert relay.sync_once() == 1
    assert inference.published == [
        (0, [FINISHED, "adapter_config.json", "adapter_model.safetensors"])
    ]
    assert (local_step / "adapter_model.safetensors").read_bytes() == b"adapter"
    assert (local_step / FINISHED).exists()

    assert relay.sync_once() == 0
    assert len(inference.published) == 1


def test_relay_does_not_commit_locally_when_inference_upload_fails(tmp_path: Path) -> None:
    trainer = FakeTrainer()
    trainer.finished = True
    relay = PrimeLoraWeightRelay(tmp_path, trainer, FakeInference(fail=True))
    relay.sync_once()
    (tmp_path / "step_0" / RECEIVER_READY).touch()

    with pytest.raises(RuntimeError, match="upload failed"):
        relay.sync_once()

    assert not (tmp_path / "step_0" / FINISHED).exists()


def test_new_sender_generation_resets_stale_completed_step(tmp_path: Path) -> None:
    trainer = FakeTrainer()
    trainer.finished = True
    inference = FakeInference()
    relay = PrimeLoraWeightRelay(tmp_path, trainer, inference)
    relay.sync_once()
    step_dir = tmp_path / "step_0"
    (step_dir / RECEIVER_READY).touch()
    assert relay.sync_once() == 1

    trainer.offer = PublishedStep(0, "generation-b")
    trainer.finished = False
    assert relay.sync_once() == 0
    assert (step_dir / SOURCE_GENERATION).read_text(encoding="utf-8").strip() == "generation-b"
    assert (step_dir / SENDER_READY).exists()
    assert not (step_dir / RECEIVER_READY).exists()
    assert not (step_dir / FINISHED).exists()


def test_ssh_endpoint_rejects_shell_metacharacters_and_unsafe_roots(tmp_path: Path) -> None:
    common = {
        "port": 22,
        "user": "work",
        "identity_file": tmp_path / "key",
        "remote_broadcast_dir": "/home/work/run/broadcasts",
    }
    with pytest.raises(ValueError):
        SshEndpoint(host="host;shutdown", **common)
    with pytest.raises(ValueError):
        SshEndpoint(host="proxy.example.com", **(common | {"remote_broadcast_dir": "/tmp/../x"}))
    with pytest.raises(ValueError):
        SshEndpoint(host="proxy.example.com", **(common | {"remote_broadcast_dir": "/"}))
    with pytest.raises(ValueError):
        SshEndpoint(host="proxy.example.com", **(common | {"remote_broadcast_dir": "/tmp/./x"}))


def test_relay_config_requires_same_adapter_path_locally_and_on_vllm(tmp_path: Path) -> None:
    endpoint = {
        "host": "proxy.example.com",
        "port": 22,
        "user": "work",
        "identity_file": tmp_path / "key",
        "remote_broadcast_dir": "/tmp/rca-prime/broadcasts",
    }
    with pytest.raises(ValueError, match="must be identical"):
        WeightRelayConfig(
            trainer=endpoint,
            inference=endpoint,
            local_broadcast_dir=tmp_path / "broadcasts",
        )

    config = WeightRelayConfig(
        trainer=endpoint | {"remote_broadcast_dir": "/home/work/train/broadcasts"},
        inference=endpoint,
        local_broadcast_dir=Path("/tmp/rca-prime/broadcasts"),
    )
    assert config.local_broadcast_dir == Path("/tmp/rca-prime/broadcasts")
