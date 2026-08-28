from __future__ import annotations

import threading
from pathlib import Path

import pytest

from rca_lab.prime_rl import ScenarioLease, parse_incident_id

INCIDENT_A = "11111111-1111-1111-1111-111111111111"
INCIDENT_B = "22222222-2222-2222-2222-222222222222"


def test_parse_incident_id_uses_last_restored_incident() -> None:
    assert parse_incident_id(
        f"old incident id: {INCIDENT_A}\nrestored incident id: {INCIDENT_B}\n"
    ) == INCIDENT_B
    with pytest.raises(ValueError, match="did not report"):
        parse_incident_id("restore completed without identity")


def test_same_scenario_rollouts_share_restored_state_concurrently(tmp_path: Path) -> None:
    first = ScenarioLease(tmp_path)
    assert first.acquire("case-a") is None
    assert first.needs_restore
    first.publish("case-a", INCIDENT_A)

    acquired = threading.Event()
    second_state = []

    def acquire_second() -> None:
        second = ScenarioLease(tmp_path)
        second_state.append(second.acquire("case-a"))
        acquired.set()
        second.release()

    thread = threading.Thread(target=acquire_second)
    thread.start()
    assert acquired.wait(1), "same-case reader was serialized behind an active rollout"
    assert second_state[0] is not None
    assert second_state[0].incident_id == INCIDENT_A
    first.release()
    thread.join(timeout=1)


def test_different_scenario_restore_waits_for_active_readers(tmp_path: Path) -> None:
    first = ScenarioLease(tmp_path)
    first.acquire("case-a")
    first.publish("case-a", INCIDENT_A)

    acquired = threading.Event()
    second_lease: list[ScenarioLease] = []

    def acquire_second() -> None:
        second = ScenarioLease(tmp_path)
        second.acquire("case-b")
        second_lease.append(second)
        acquired.set()

    thread = threading.Thread(target=acquire_second)
    thread.start()
    assert not acquired.wait(0.1), "a different case restored while readers were active"
    first.release()
    assert acquired.wait(1)
    assert second_lease[0].needs_restore
    second_lease[0].publish("case-b", INCIDENT_B)
    second_lease[0].release()
    thread.join(timeout=1)
