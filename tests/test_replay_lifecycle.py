from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_lifecycle():
    spec = importlib.util.spec_from_file_location("replay_lifecycle_under_test", Path("scripts/replay_lifecycle.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RCA_REPLAY_LEASE_DIR", str(tmp_path / "leases"))
    return load_lifecycle()


def lease_record(module, project: str, *, status: str = "finished", lease_id: str = "lease-1", compose_path: str | None = None):
    return {
        "version": module.VERSION,
        "project": project,
        "compose_path": compose_path or str(module.COMPOSE),
        "lease_id": lease_id,
        "owner_pid": 999999,
        "owner_starttime": "1",
        "owner_boot_id": "boot",
        "status": status,
        "created_at": time.time() - 500,
        "updated_at": time.time() - 500,
    }


def write_lease(module, tmp_path: Path, project: str, record: dict) -> Path:
    path = Path(os.environ["RCA_REPLAY_LEASE_DIR"]) / f"{project}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    (path.parent / f"{project}.lock").touch()
    return path


def test_replay_lease_sets_generation_env_and_marks_finished(lifecycle, tmp_path: Path) -> None:
    with lifecycle.replay_lease("proj-a") as lease_id:
        assert os.environ["RCA_REPLAY_LEASE_ID"] == lease_id
        record = json.loads((tmp_path / "leases/proj-a.json").read_text(encoding="utf-8"))
        assert record["status"] == "active"
        assert record["lease_id"] == lease_id

    assert "RCA_REPLAY_LEASE_ID" not in os.environ
    record = json.loads((tmp_path / "leases/proj-a.json").read_text(encoding="utf-8"))
    assert record["status"] == "finished"


def test_replay_lease_retires_empty_finished_record_before_reuse(lifecycle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_lease(lifecycle, tmp_path, "proj-a", lease_record(lifecycle, "proj-a"))
    monkeypatch.setattr(lifecycle, "_finished_record_has_resources", lambda _project: False)

    with lifecycle.replay_lease("proj-a") as lease_id:
        assert lease_id != "lease-1"


def test_retained_lease_is_not_reaped(lifecycle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_lease(lifecycle, tmp_path, "proj-a", lease_record(lifecycle, "proj-a", status="retained"))
    monkeypatch.setattr(lifecycle, "_lease_token_in_processes", lambda _lease_id, **kwargs: False)

    summary = lifecycle.sweep_finished_replays(apply=True)

    assert summary["removed"] == []
    assert summary["skipped"][0]["reason"] == "retained"
    assert (tmp_path / "leases/proj-a.json").exists()


def test_foreign_compose_path_is_not_reaped(lifecycle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_lease(lifecycle, tmp_path, "proj-a", lease_record(lifecycle, "proj-a", compose_path="/tmp/other.yml"))
    monkeypatch.setattr(lifecycle, "_lease_token_in_processes", lambda _lease_id, **kwargs: False)

    summary = lifecycle.sweep_finished_replays(apply=True)

    assert summary["removed"] == []
    assert "foreign" in summary["skipped"][0]["reason"]
    assert (tmp_path / "leases/proj-a.json").exists()


def test_apply_reaps_only_matching_generation_and_deletes_consumed_lease(lifecycle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_lease(lifecycle, tmp_path, "proj-a", lease_record(lifecycle, "proj-a"))
    monkeypatch.setattr(lifecycle, "_lease_token_in_processes", lambda _lease_id, **kwargs: False)
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["docker", "ps", "-aq"]:
            return SimpleNamespace(returncode=0, stdout="c1\nc2\n", stderr="")
        if args[:2] == ["docker", "inspect"]:
            labels = {
                "com.docker.compose.project": "proj-a",
                "com.docker.compose.project.config_files": str(lifecycle.COMPOSE),
                lifecycle.LEASE_LABEL: "lease-1",
            }
            payload = [
                {"Id": "c1", "Config": {"Labels": labels}, "State": {"Running": True}},
                {"Id": "c2", "Config": {"Labels": labels}, "State": {"Running": False}},
            ]
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        if args[:3] == ["docker", "network", "ls"]:
            return SimpleNamespace(returncode=0, stdout="n1\n", stderr="")
        if args[:3] == ["docker", "network", "inspect"]:
            payload = [{"Id": "n1", "Labels": {"com.docker.compose.project": "proj-a", lifecycle.LEASE_LABEL: "lease-1"}, "Containers": {}}]
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)

    summary = lifecycle.sweep_finished_replays(apply=True)

    assert summary["removed"] == [{"project": "proj-a", "containers": 2, "networks": 1}]
    assert ["docker", "stop", "-t", "10", "c1"] in calls
    assert ["docker", "rm", "-v", "c1", "c2"] in calls
    assert ["docker", "network", "rm", "n1"] in calls
    assert not (tmp_path / "leases/proj-a.json").exists()


def test_cleanup_error_keeps_lease_for_retry(lifecycle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_lease(lifecycle, tmp_path, "proj-a", lease_record(lifecycle, "proj-a"))
    monkeypatch.setattr(lifecycle, "_lease_token_in_processes", lambda _lease_id, **kwargs: False)

    def fake_run(args, **_kwargs):
        if args[:3] == ["docker", "ps", "-aq"]:
            return SimpleNamespace(returncode=0, stdout="c1\n", stderr="")
        if args[:2] == ["docker", "inspect"]:
            labels = {
                "com.docker.compose.project": "proj-a",
                "com.docker.compose.project.config_files": str(lifecycle.COMPOSE),
                lifecycle.LEASE_LABEL: "lease-1",
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Id": "c1", "Config": {"Labels": labels}, "State": {"Running": False}}]), stderr="")
        if args[:3] == ["docker", "network", "ls"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:3] == ["docker", "rm", "-v"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="rm failed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)

    summary = lifecycle.sweep_finished_replays(apply=True)

    assert summary["errors"] == [{"project": "proj-a", "error": "rm failed"}]
    assert (tmp_path / "leases/proj-a.json").exists()


def test_live_lease_lock_prevents_sweep(lifecycle):
    with lifecycle.replay_lease("live"):
        result = lifecycle.sweep_finished_replays(apply=True)
        assert result["skipped"] == [{"project": "live", "reason": "locked"}]
        assert not result["removed"]


def test_failed_retained_run_is_reclaimable(lifecycle, tmp_path):
    with pytest.raises(RuntimeError), lifecycle.replay_lease("failed", retained=True):
        raise RuntimeError("restore failed")
    assert json.loads((tmp_path / "leases/failed.json").read_text())["status"] == "finished"


def test_owner_identity_rejects_pid_reuse(lifecycle, monkeypatch):
    record = lease_record(lifecycle, "p", status="active")
    monkeypatch.setattr(lifecycle, "_boot_id", lambda: "boot")
    monkeypatch.setattr(lifecycle, "_proc_starttime", lambda pid: "1")
    assert lifecycle._owner_alive(record)
    assert lifecycle._eligible(record, grace_seconds=0) == (False, "owner alive")
    monkeypatch.setattr(lifecycle, "_proc_starttime", lambda pid: "2")
    assert not lifecycle._owner_alive(record)


def test_child_process_protects_finished_lease(lifecycle, monkeypatch):
    monkeypatch.setattr(lifecycle, "_lease_token_in_processes", lambda token, **kwargs: True)
    assert not lifecycle._eligible(lease_record(lifecycle, "p"), grace_seconds=0)[0]


def test_generation_and_legacy_ids_are_exact(lifecycle):
    record = lease_record(lifecycle, "p")
    item = {"Id": "full-container-id", "Config": {"Labels": {
        "com.docker.compose.project": "p",
        "com.docker.compose.project.config_files": str(lifecycle.COMPOSE),
        lifecycle.LEASE_LABEL: "wrong-generation",
    }}}
    assert not lifecycle._container_generation_ok(item, record)
    record["container_ids"] = ["full-container-id"]
    assert lifecycle._container_generation_ok(item, record)
    item["Config"]["Labels"]["com.docker.compose.project"] = "other"
    assert not lifecycle._container_generation_ok(item, record)


def test_dry_run_never_mutates_and_keeps_lease(lifecycle, tmp_path, monkeypatch):
    path = write_lease(lifecycle, tmp_path, "dry", lease_record(lifecycle, "dry"))
    monkeypatch.setattr(lifecycle, "_eligible", lambda *a, **k: (True, "eligible"))
    monkeypatch.setattr(lifecycle, "_docker_lines", lambda args: ["c1"] if args[1] == "ps" else [])
    monkeypatch.setattr(lifecycle, "_docker_json", lambda args: [{"Id": "c1", "State": {"Running": True}}])
    monkeypatch.setattr(lifecycle, "_container_generation_ok", lambda *args: True)
    monkeypatch.setattr(lifecycle, "_run", lambda *a, **k: pytest.fail("dry-run mutated Docker"))
    result = lifecycle.sweep_finished_replays()
    assert not result["errors"]
    assert result["removed"][0]["containers"] == 1
    assert path.exists()


@pytest.mark.parametrize("started,denied", [("99", False), ("101", True)])
def test_unreadable_process_predating_owner_cannot_inherit_lease(lifecycle, monkeypatch, started, denied):
    monkeypatch.setattr(Path, "iterdir", lambda self: iter([Path("/proc/123")]))
    def unreadable(self):
        raise PermissionError("not dumpable")
    monkeypatch.setattr(Path, "read_bytes", unreadable)
    monkeypatch.setattr(lifecycle, "_same_uid", lambda path: True)
    monkeypatch.setattr(lifecycle, "_proc_starttime", lambda pid: started)
    if denied:
        with pytest.raises(RuntimeError, match="env scan denied"):
            lifecycle._lease_token_in_processes("lease", owner_starttime="100")
    else:
        assert not lifecycle._lease_token_in_processes("lease", owner_starttime="100")
