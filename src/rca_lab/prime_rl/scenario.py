"""Coordinate read-only rollouts over a scenario-restored shared data plane."""

from __future__ import annotations

import fcntl
import os
import re
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

_INCIDENT_ID = re.compile(r"incident id: ([0-9a-f-]{36})", re.IGNORECASE)


class ScenarioState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    incident_id: str = Field(pattern=r"^[0-9a-f-]{36}$")


def parse_incident_id(output: str) -> str:
    matches = _INCIDENT_ID.findall(output)
    if not matches:
        raise ValueError("scenario restore did not report an incident id")
    return matches[-1]


class ScenarioLease:
    """A cross-process lease for one mutable scenario restore.

    Setup is serialized by a gate lock. Active rollouts retain a shared data lock,
    so another case cannot restore the databases until every reader has finished.
    Rollouts for the already-loaded case can acquire shared leases concurrently.
    """

    def __init__(self, directory: Path):
        self.directory = directory
        self._gate = None
        self._data = None
        self._exclusive = False
        self._state: ScenarioState | None = None

    @property
    def state(self) -> ScenarioState | None:
        return self._state

    @property
    def needs_restore(self) -> bool:
        return self._exclusive

    def acquire(self, case_id: str) -> ScenarioState | None:
        if self._gate is not None or self._data is not None:
            raise RuntimeError("scenario lease is already acquired")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._gate = (self.directory / "setup.lock").open("a+")
        self._data = (self.directory / "data.lock").open("a+")
        try:
            fcntl.flock(self._gate.fileno(), fcntl.LOCK_EX)
            fcntl.flock(self._data.fileno(), fcntl.LOCK_SH)
            current = self._read_state()
            if current is not None and current.case_id == case_id:
                self._state = current
                fcntl.flock(self._gate.fileno(), fcntl.LOCK_UN)
                self._gate.close()
                self._gate = None
                return current

            fcntl.flock(self._data.fileno(), fcntl.LOCK_UN)
            fcntl.flock(self._data.fileno(), fcntl.LOCK_EX)
            # A previous waiter may have restored this case while we were queued.
            current = self._read_state()
            if current is not None and current.case_id == case_id:
                self._state = current
                fcntl.flock(self._data.fileno(), fcntl.LOCK_SH)
                fcntl.flock(self._gate.fileno(), fcntl.LOCK_UN)
                self._gate.close()
                self._gate = None
                return current
            self._exclusive = True
            return None
        except BaseException:
            self.release()
            raise

    def publish(self, case_id: str, incident_id: str) -> ScenarioState:
        if not self._exclusive or self._data is None or self._gate is None:
            raise RuntimeError("publish requires an exclusive restore lease")
        state = ScenarioState(case_id=case_id, incident_id=incident_id)
        target = self.directory / "state.json"
        temporary = self.directory / f"state.{os.getpid()}.tmp"
        payload = state.model_dump_json(indent=2) + "\n"
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, target)
        self._state = state
        self._exclusive = False
        # Downgrade before opening the setup gate. The data plane remains pinned
        # to this scenario for the complete rollout lifetime.
        fcntl.flock(self._data.fileno(), fcntl.LOCK_SH)
        fcntl.flock(self._gate.fileno(), fcntl.LOCK_UN)
        self._gate.close()
        self._gate = None
        return state

    def release(self) -> None:
        if self._data is not None:
            try:
                fcntl.flock(self._data.fileno(), fcntl.LOCK_UN)
            finally:
                self._data.close()
                self._data = None
        if self._gate is not None:
            try:
                fcntl.flock(self._gate.fileno(), fcntl.LOCK_UN)
            finally:
                self._gate.close()
                self._gate = None
        self._exclusive = False

    def _read_state(self) -> ScenarioState | None:
        try:
            return ScenarioState.model_validate_json(
                (self.directory / "state.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
