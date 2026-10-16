#!/usr/bin/env python3
"""Lease and reclaim RCA replay DB compose projects.

The reaper is deliberately narrow: it only touches projects registered by this
module, with the exact replay compose file and the exact lease generation label.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "tools/rca-mcp/replay/eval-dbs.compose.yml").resolve()
LEASE_LABEL = "io.rca.replay.lease-id"
VERSION = 1


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()


def _proc_starttime(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    rest = stat.rsplit(") ", 1)[1].split()
    return None if rest[0] == "Z" else rest[19]


def _owner_alive(record: dict[str, Any]) -> bool:
    pid = int(record.get("owner_pid") or -1)
    return pid > 0 and record.get("owner_boot_id") == _boot_id() and record.get("owner_starttime") == _proc_starttime(pid)


def _registry_dir(root: Path = ROOT) -> Path:
    override = os.environ.get("RCA_REPLAY_LEASE_DIR")
    return Path(override).resolve() if override else root / "outputs/replay-leases"


def _paths(project: str, root: Path = ROOT) -> tuple[Path, Path]:
    if "/" in project or project in {"", ".", ".."}:
        raise ValueError(f"invalid replay project: {project!r}")
    base = _registry_dir(root)
    return base / f"{project}.json", base / f"{project}.lock"


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _finished_record_has_resources(project: str) -> bool:
    containers = _docker_lines(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"])
    networks = _docker_lines(["docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"])
    return bool(containers or networks)


@contextmanager
def replay_lease(project: str, compose: Path = COMPOSE, *, retained: bool = False, root: Path = ROOT) -> Iterator[str]:
    lease_path, lock_path = _paths(project, root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = _read_json(lease_path)
        if old and old.get("status") == "finished" and old.get("project") == project and old.get("compose_path") == str(COMPOSE):
            if _finished_record_has_resources(project):
                raise RuntimeError(f"unreclaimed replay resources exist for {project}: {lease_path}")
            lease_path.unlink(missing_ok=True)
            old = None
        if old and old.get("status") in {"active", "finished", "retained"}:
            raise RuntimeError(f"unreclaimed replay lease exists for {project}: {lease_path}")
        lease_id = uuid.uuid4().hex
        previous = os.environ.get("RCA_REPLAY_LEASE_ID")
        now = time.time()
        record = {
            "version": VERSION,
            "project": project,
            "compose_path": str(Path(compose).resolve()),
            "lease_id": lease_id,
            "owner_pid": os.getpid(),
            "owner_starttime": _proc_starttime(os.getpid()),
            "owner_boot_id": _boot_id(),
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        _write_atomic(lease_path, record)
        os.environ["RCA_REPLAY_LEASE_ID"] = lease_id
        success = False
        try:
            yield lease_id
            success = True
        finally:
            record["status"] = "retained" if retained and success else "finished"
            record["updated_at"] = time.time()
            _write_atomic(lease_path, record)
            if previous is None:
                os.environ.pop("RCA_REPLAY_LEASE_ID", None)
            else:
                os.environ["RCA_REPLAY_LEASE_ID"] = previous


def _docker_json(args: list[str], *, timeout: float = 30.0) -> Any:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"{args[0]} failed").strip())
    return json.loads(proc.stdout or "[]")


def _docker_lines(args: list[str], *, timeout: float = 30.0) -> list[str]:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"{args[0]} failed").strip())
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _run(args: list[str], *, timeout: float = 60.0) -> None:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"{args[0]} failed").strip())


def _compose_label_ok(labels: dict[str, str], record: dict[str, Any]) -> bool:
    configs = [p.strip() for p in labels.get("com.docker.compose.project.config_files", "").split(",")]
    return (
        labels.get("com.docker.compose.project") == record["project"]
        and configs == [str(COMPOSE)]
    )


def _container_generation_ok(container: dict[str, Any], record: dict[str, Any]) -> bool:
    labels = container.get("Config", {}).get("Labels") or {}
    if not _compose_label_ok(labels, record):
        return False
    if labels.get(LEASE_LABEL) == record["lease_id"]:
        return True
    return container.get("Id") in set(record.get("container_ids") or [])


def _same_uid(proc_dir: Path) -> bool:
    return proc_dir.stat().st_uid == os.getuid()


def _lease_token_in_processes(lease_id: str, ports: set[int] | None = None, *, owner_starttime: str = "0") -> bool:
    token = f"RCA_REPLAY_LEASE_ID={lease_id}".encode()
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            env = (proc_dir / "environ").read_bytes().split(b"\0")
            if token in env:
                return True
            if ports:
                for item in env:
                    key, _, value = item.partition(b"=")
                    if key in {b"RCA_PG_DSN", b"RCA_CH_URL", b"RCA_VM_URL"} and urlsplit(value.decode()).port in ports:
                        return True
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            # A process predating the owner cannot inherit this lease or its DB env.
            started = _proc_starttime(int(proc_dir.name))
            if _same_uid(proc_dir) and started is not None and int(started) >= int(owner_starttime):
                raise RuntimeError(f"process env scan denied at {proc_dir}")
            continue
        except OSError as exc:
            raise RuntimeError(f"process scan failed at {proc_dir}: {exc}") from exc
    return False


def _restore_process_for_project(project: str) -> bool:
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            args = [part.decode(errors="replace") for part in (proc_dir / "cmdline").read_bytes().split(b"\0") if part]
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            if _same_uid(proc_dir):
                raise RuntimeError(f"process cmdline scan denied at {proc_dir}")
            continue
        except OSError as exc:
            raise RuntimeError(f"process scan failed at {proc_dir}: {exc}") from exc
        if project in args and any(arg.endswith("/restore.sh") or arg == "restore.sh" for arg in args):
            return True
        if any(Path(arg).name == "mcp_case_run.py" for arg in args) and "--case" in args:
            prefix = args[args.index("--project-prefix") + 1] if "--project-prefix" in args else "mcp"
            case = args[args.index("--case") + 1]
            if prefix + "-" + case.split("-v3-")[0].replace("case-", "") == project:
                return True
    return False


def _eligible(record: dict[str, Any], *, grace_seconds: float) -> tuple[bool, str]:
    if record.get("version") != VERSION or record.get("compose_path") != str(COMPOSE):
        return False, "foreign lease schema or compose path"
    if record.get("status") not in {"active", "finished", "retained"} or not record.get("lease_id"):
        return False, "invalid lease schema"
    if record.get("status") == "retained":
        return False, "retained"
    if record.get("status") == "active" and _owner_alive(record):
        return False, "owner alive"
    if _lease_token_in_processes(str(record.get("lease_id", "")), owner_starttime=str(record.get("owner_starttime") or "0")):
        return False, "lease token still present in a process"
    if _restore_process_for_project(str(record.get("project", ""))):
        return False, "restore process still references project"
    if record.get("status") == "active":
        age = time.time() - float(record.get("updated_at") or record.get("created_at") or time.time())
        if age < grace_seconds:
            return False, f"active orphan grace {age:.0f}s"
    return True, "eligible"


def sweep_finished_replays(*, apply: bool = False, grace_seconds: float = 120.0, root: Path = ROOT) -> dict[str, Any]:
    lease_dir = _registry_dir(root)
    summary: dict[str, Any] = {"apply": apply, "removed": [], "skipped": [], "errors": []}
    for lease_path in sorted(lease_dir.glob("*.json")):
        project = lease_path.stem
        _, lock_path = _paths(project, root)
        try:
            with lock_path.open("a+", encoding="utf-8") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    summary["skipped"].append({"project": project, "reason": "locked"})
                    continue
                record = _read_json(lease_path)
                if not record:
                    continue
                if record.get("project") != project:
                    summary["skipped"].append({"project": project, "reason": "lease filename/project mismatch"})
                    continue
                ok, reason = _eligible(record, grace_seconds=grace_seconds)
                if not ok:
                    summary["skipped"].append({"project": project, "reason": reason})
                    continue
                ids = _docker_lines(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"])
                if ids:
                    inspected = _docker_json(["docker", "inspect", *ids])
                    bad = [item.get("Id", "")[:12] for item in inspected if not _container_generation_ok(item, record)]
                    if bad:
                        summary["skipped"].append({"project": project, "reason": f"container label mismatch: {bad}"})
                        continue
                    ports = {int(binding["HostPort"]) for item in inspected
                             for bindings in item.get("NetworkSettings", {}).get("Ports", {}).values()
                             for binding in (bindings or [])}
                    if ports and _lease_token_in_processes(record["lease_id"], ports, owner_starttime=str(record.get("owner_starttime") or "0")):
                        summary["skipped"].append({"project": project, "reason": "DB client still active"})
                        continue
                    running = [item["Id"] for item in inspected if item.get("State", {}).get("Running")]
                    if apply:
                        if running:
                            _run(["docker", "stop", "-t", "10", *running], timeout=90)
                        _run(["docker", "rm", "-v", *ids], timeout=90)
                networks = _docker_lines(["docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"])
                if networks:
                    inspected_nets = _docker_json(["docker", "network", "inspect", *networks])
                    removable = [
                        item["Id"]
                        for item in inspected_nets
                        if (item.get("Labels") or {}).get("com.docker.compose.project") == project
                        and ((item.get("Labels") or {}).get(LEASE_LABEL) == record["lease_id"]
                             or item["Id"] in record.get("network_ids", []))
                        and not item.get("Containers")
                    ]
                    if apply and removable:
                        _run(["docker", "network", "rm", *removable], timeout=60)
                    if len(removable) != len(networks):
                        raise RuntimeError("network still occupied or generation mismatch; retaining lease for retry")
                if apply:
                    lease_path.unlink(missing_ok=True)
                summary["removed"].append({"project": project, "containers": len(ids), "networks": len(networks)})
        except Exception as exc:  # noqa: BLE001 - keep retry state for the next sweep.
            summary["errors"].append({"project": project, "error": str(exc)})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Reclaim finished RCA replay DB containers")
    parser.add_argument("--apply", action="store_true", help="actually remove containers and consumed leases")
    parser.add_argument("--grace-seconds", type=float, default=120.0)
    args = parser.parse_args()
    summary = sweep_finished_replays(apply=args.apply, grace_seconds=args.grace_seconds)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary.get("errors"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
