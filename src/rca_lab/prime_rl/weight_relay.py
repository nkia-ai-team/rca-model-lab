"""Relay Prime-RL filesystem LoRA broadcasts across isolated SSH hosts.

Prime's filesystem transport assumes that trainer, orchestrator, and inference
see the same path.  KT training and inference containers do not share a
filesystem, so this module mirrors the existing four-marker handshake without
changing Prime's trainer or receiver semantics.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SENDER_READY = ".sender_ready"
RECEIVER_READY = ".receiver_ready"
STARTED = ".started"
FINISHED = ".finished"
SOURCE_GENERATION = ".relay-source-generation"

_SAFE_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
_SAFE_PRINCIPAL = re.compile(r"^[A-Za-z0-9._-]+$")
_STEP = re.compile(r"^step_([0-9]+)$")


class SshEndpoint(BaseModel):
    """One SSH-accessible broadcast root."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    user: str = Field(min_length=1)
    identity_file: Path
    remote_broadcast_dir: str
    strict_host_key_checking: bool = False
    known_hosts_file: Path = Path("/dev/null")
    connect_timeout_seconds: int = Field(default=10, ge=1, le=120)
    command_timeout_seconds: int = Field(default=30, ge=1, le=600)
    transfer_timeout_seconds: int = Field(default=1800, ge=1, le=7200)

    @field_validator("host", "user")
    @classmethod
    def validate_principal(cls, value: str) -> str:
        if not _SAFE_PRINCIPAL.fullmatch(value):
            raise ValueError("SSH host and user must contain only safe principal characters")
        return value

    @field_validator("remote_broadcast_dir")
    @classmethod
    def validate_remote_root(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parts = PurePosixPath(normalized).parts
        if (
            not _SAFE_REMOTE_PATH.fullmatch(value)
            or normalized in {"", "/"}
            or "//" in value
            or str(PurePosixPath(value)) != normalized
            or ".." in parts
            or "." in parts
        ):
            raise ValueError("remote_broadcast_dir must be a normalized absolute path")
        return normalized


class WeightRelayConfig(BaseModel):
    """Typed deployment contract for the cross-session adapter relay."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trainer: SshEndpoint
    inference: SshEndpoint
    local_broadcast_dir: Path
    poll_interval_seconds: float = Field(default=0.25, gt=0, le=60)

    @model_validator(mode="after")
    def validate_shared_visible_path(self) -> WeightRelayConfig:
        local = str(self.local_broadcast_dir)
        if not self.local_broadcast_dir.is_absolute():
            raise ValueError("local_broadcast_dir must be absolute")
        if local != self.inference.remote_broadcast_dir:
            raise ValueError(
                "local_broadcast_dir and inference.remote_broadcast_dir must be identical; "
                "Prime sends that absolute adapter path to vLLM"
            )
        return self


@dataclass(frozen=True, order=True)
class PublishedStep:
    step: int
    generation: str


class TrainerBroadcastStore(Protocol):
    def published_steps(self) -> list[PublishedStep]: ...

    def marker_exists(self, step: int, marker: str) -> bool: ...

    def acknowledge(self, step: int) -> None: ...

    def download_step(self, step: int, destination: Path) -> None: ...


class InferenceBroadcastStore(Protocol):
    def publish_step(self, step: int, source: Path) -> None: ...


def _step_name(step: int) -> str:
    if step < 0:
        raise ValueError("Prime-RL broadcast step must be non-negative")
    return f"step_{step}"


class OpenSshBroadcastStore:
    """OpenSSH-backed trainer and inference store.

    Only paths below the configured broadcast root are addressable.  Directory
    publication uses a unique staging name followed by a remote rename.
    """

    def __init__(self, endpoint: SshEndpoint):
        if not endpoint.identity_file.is_file():
            raise FileNotFoundError(endpoint.identity_file)
        self.endpoint = endpoint

    @property
    def root(self) -> PurePosixPath:
        return PurePosixPath(self.endpoint.remote_broadcast_dir)

    def _path(self, step: int, marker: str | None = None) -> PurePosixPath:
        path = self.root / _step_name(step)
        if marker is not None:
            if marker not in {SENDER_READY, RECEIVER_READY, STARTED, FINISHED}:
                raise ValueError(f"unsupported Prime-RL marker: {marker}")
            path /= marker
        return path

    def _ssh_args(self) -> list[str]:
        endpoint = self.endpoint
        return [
            "ssh",
            "-i",
            str(endpoint.identity_file),
            "-p",
            str(endpoint.port),
            "-o",
            "BatchMode=yes",
            "-o",
            f"StrictHostKeyChecking={'yes' if endpoint.strict_host_key_checking else 'no'}",
            "-o",
            f"UserKnownHostsFile={endpoint.known_hosts_file}",
            "-o",
            f"ConnectTimeout={endpoint.connect_timeout_seconds}",
            f"{endpoint.user}@{endpoint.host}",
        ]

    def _scp_args(self) -> list[str]:
        endpoint = self.endpoint
        return [
            "scp",
            "-O",
            "-r",
            "-i",
            str(endpoint.identity_file),
            "-P",
            str(endpoint.port),
            "-o",
            "BatchMode=yes",
            "-o",
            f"StrictHostKeyChecking={'yes' if endpoint.strict_host_key_checking else 'no'}",
            "-o",
            f"UserKnownHostsFile={endpoint.known_hosts_file}",
            "-o",
            f"ConnectTimeout={endpoint.connect_timeout_seconds}",
        ]

    def _ssh(self, command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self._ssh_args(), command],
            check=check,
            capture_output=True,
            text=True,
            timeout=self.endpoint.command_timeout_seconds,
        )

    def _scp(self, source: str | Path, destination: str | Path) -> None:
        subprocess.run(
            [*self._scp_args(), str(source), str(destination)],
            check=True,
            capture_output=True,
            text=True,
            timeout=self.endpoint.transfer_timeout_seconds,
        )

    def _remote_spec(self, path: PurePosixPath) -> str:
        return f"{self.endpoint.user}@{self.endpoint.host}:{path}"

    def published_steps(self) -> list[PublishedStep]:
        root = shlex.quote(str(self.root))
        command = (
            f"if test -d {root}; then "
            f"find {root} -mindepth 2 -maxdepth 2 -type f -name {SENDER_READY} "
            "-printf '%h\\t%i:%T@:%s\\n'; fi"
        )
        result = self._ssh(command)
        offers: list[PublishedStep] = []
        for line in result.stdout.splitlines():
            parent, separator, generation = line.partition("\t")
            match = _STEP.fullmatch(PurePosixPath(parent).name)
            if not separator or match is None or not generation:
                raise RuntimeError(f"invalid Prime-RL broadcast listing: {line!r}")
            offers.append(PublishedStep(step=int(match.group(1)), generation=generation))
        return sorted(offers)

    def marker_exists(self, step: int, marker: str) -> bool:
        result = self._ssh(f"test -e {shlex.quote(str(self._path(step, marker)))}", check=False)
        if result.returncode not in {0, 1}:
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                output=result.stdout,
                stderr=result.stderr,
            )
        return result.returncode == 0

    def acknowledge(self, step: int) -> None:
        step_dir = shlex.quote(str(self._path(step)))
        marker = shlex.quote(str(self._path(step, RECEIVER_READY)))
        self._ssh(f"test -d {step_dir} && : > {marker}")

    def download_step(self, step: int, destination: Path) -> None:
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._scp(self._remote_spec(self._path(step)), destination)

    def publish_step(self, step: int, source: Path) -> None:
        if not source.is_dir():
            raise FileNotFoundError(source)
        root = shlex.quote(str(self.root))
        final_path = self._path(step)
        staging_path = self.root / f".relay-{_step_name(step)}-{uuid.uuid4().hex}"
        self._ssh(f"mkdir -p {root}")
        self._scp(source, self._remote_spec(staging_path))
        final = shlex.quote(str(final_path))
        staging = shlex.quote(str(staging_path))
        # Both targets are validated children of remote_broadcast_dir.  The
        # final path is invisible to vLLM until the local receiver observes
        # .finished after this atomic publication completes.
        self._ssh(f"test -d {staging} && rm -rf -- {final} && mv -- {staging} {final}")


class PrimeLoraWeightRelay:
    """State machine that preserves Prime's filesystem broadcast handshake."""

    def __init__(
        self,
        local_broadcast_dir: Path,
        trainer: TrainerBroadcastStore,
        inference: InferenceBroadcastStore,
    ) -> None:
        self.local_broadcast_dir = local_broadcast_dir
        self.trainer = trainer
        self.inference = inference

    def sync_once(self) -> int:
        """Advance every offered step once; return newly committed versions."""

        committed = 0
        self.local_broadcast_dir.mkdir(parents=True, exist_ok=True)
        for offer in self.trainer.published_steps():
            step_dir = self.local_broadcast_dir / _step_name(offer.step)
            self._mirror_offer(step_dir, offer)
            if not (step_dir / RECEIVER_READY).exists():
                continue
            self.trainer.acknowledge(offer.step)
            if not self.trainer.marker_exists(offer.step, FINISHED):
                continue
            if (step_dir / FINISHED).exists():
                continue
            staging = self.local_broadcast_dir / (
                f".relay-download-{_step_name(offer.step)}-{uuid.uuid4().hex}"
            )
            try:
                self.trainer.download_step(offer.step, staging)
                self._validate_adapter(staging)
                self.inference.publish_step(offer.step, staging)
                self._install_local(step_dir, staging)
                committed += 1
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        return committed

    def run(self, poll_interval_seconds: float) -> None:
        while True:
            self.sync_once()
            time.sleep(poll_interval_seconds)

    @staticmethod
    def _mirror_offer(step_dir: Path, offer: PublishedStep) -> None:
        generation_file = step_dir / SOURCE_GENERATION
        current = None
        try:
            current = generation_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if current != offer.generation:
            shutil.rmtree(step_dir, ignore_errors=True)
            step_dir.mkdir(parents=True)
            temporary = step_dir / f"{SOURCE_GENERATION}.{os.getpid()}.tmp"
            temporary.write_text(f"{offer.generation}\n", encoding="utf-8")
            os.replace(temporary, generation_file)
        (step_dir / SENDER_READY).touch(exist_ok=True)

    @staticmethod
    def _validate_adapter(step_dir: Path) -> None:
        if not (step_dir / FINISHED).is_file():
            raise RuntimeError("trainer download is missing Prime-RL .finished marker")
        if not (step_dir / "adapter_config.json").is_file():
            raise RuntimeError("trainer broadcast is not a PEFT LoRA adapter")
        if not any(step_dir.glob("*.safetensors")):
            raise RuntimeError("trainer LoRA broadcast has no safetensors weights")

    @staticmethod
    def _install_local(step_dir: Path, staging: Path) -> None:
        for source in staging.iterdir():
            if source.name in {SENDER_READY, RECEIVER_READY, FINISHED, SOURCE_GENERATION}:
                continue
            target = step_dir / source.name
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target)
        (step_dir / STARTED).touch(exist_ok=True)
        # This is the commit point observed by Prime's local receiver.  The
        # matching adapter already exists at the same absolute path on the
        # inference host before this marker becomes visible.
        (step_dir / FINISHED).touch()
