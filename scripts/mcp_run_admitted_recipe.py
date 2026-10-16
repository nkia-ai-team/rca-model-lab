"""Durable controller for admitted-only recipe training.

This script is orchestration only. Admission candidate generation, evidence judging,
approved-only export, and continuation training remain owned by their dedicated
tools. The controller records phase state, exits 3 for resumable pending/quota,
and exits 2 before training when the admission export gate fails.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


class ControllerError(RuntimeError):
    """Controller configuration or subprocess execution failed."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ControllerError(f"expected JSON object: {path}")
    return data


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def state(workspace: Path, phase: str, **fields: Any) -> None:
    payload = {"phase": phase, "updated_at": utc_now(), **fields}
    atomic_json(workspace / "state.json", payload)


def as_path(config: dict[str, Any], key: str) -> Path:
    value = config.get(key)
    if not isinstance(value, str) or not value:
        raise ControllerError(f"config requires string {key}")
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def command(config: dict[str, Any], key: str) -> list[str]:
    commands = config.get("commands")
    if not isinstance(commands, dict):
        raise ControllerError("config requires commands object")
    value = commands.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ControllerError(f"config requires commands.{key} string list")
    return list(value)


def count_from_admission(report: dict[str, Any]) -> dict[str, int]:
    counts = report.get("counts")
    if not isinstance(counts, dict):
        return {}
    result: dict[str, int] = {}
    for key in ("approved", "held", "rejected", "pending", "total"):
        value = counts.get(key)
        if isinstance(value, int):
            result[key] = value
    return result


def run_step(command_line: list[str], workspace: Path, name: str) -> subprocess.CompletedProcess[str]:
    state(workspace, name, command=command_line)
    workspace.mkdir(parents=True, exist_ok=True)
    with (
        (workspace / f"{name}.stdout").open("a", encoding="utf-8") as stdout,
        (workspace / f"{name}.stderr").open("a", encoding="utf-8") as stderr,
    ):
        return subprocess.run(
            command_line,
            cwd=ROOT,
            text=True,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )


def sha_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def valid_candidate_items_path(candidates_dir: Path) -> Path | None:
    manifest = candidates_dir / "manifest.json"
    if not manifest.exists():
        return None
    data = read_json(manifest)
    expected = data.get("items_sha256")
    if not isinstance(expected, str) or not expected:
        return None
    for key in ("candidate_items", "items_jsonl", "items"):
        value = data.get(key)
        if isinstance(value, str) and value:
            path = Path(value)
            resolved = path if path.is_absolute() else ROOT / path
            if resolved.exists() and sha_file(resolved) == expected:
                return resolved
            return None
    return None


def admission_report_path(judged_dir: Path) -> Path:
    return judged_dir / "admission.json"


def report_complete(path: Path) -> tuple[bool, dict[str, int]]:
    if not path.exists():
        return False, {}
    report = read_json(path)
    counts = count_from_admission(report)
    progress = report.get("progress")
    processed = progress.get("processed") if isinstance(progress, dict) else None
    expected_total = progress.get("expected_total") if isinstance(progress, dict) else None
    total = counts.get("total", 0)
    resolved = counts.get("approved", 0) + counts.get("held", 0) + counts.get("rejected", 0)
    complete = (
        report.get("status") == "complete"
        and counts.get("pending", 0) == 0
        and total > 0
        and processed == expected_total == total
        and resolved == total
    )
    return complete, counts


def append_cache_args(args: list[str], cache_roots: list[Path]) -> None:
    for root in cache_roots:
        args.extend(["--cache-from", str(root)])


def run_controller(config_path: Path) -> int:
    config = read_json(config_path)
    workspace = as_path(config, "workspace")
    out_root = as_path(config, "out_root")
    workspace.mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)
    frozen = workspace / "controller-config.json"
    if frozen.exists():
        if read_json(frozen) != config:
            raise ControllerError("controller config changed for this workspace; use a new workspace")
    else:
        atomic_json(frozen, config)

    candidate_joint = as_path(config, "candidate_input")
    source_joint_manifest = as_path(config, "source_joint_manifest")
    continuation_config = as_path(config, "continuation_config")
    continuation_workspace = Path(config.get("continuation_workspace") or (workspace / "continuation"))
    if not continuation_workspace.is_absolute():
        continuation_workspace = ROOT / continuation_workspace
    cache_roots = [Path(raw) if Path(raw).is_absolute() else ROOT / raw for raw in config.get("cache_roots", [])]
    judge_workers = int(config.get("judge_workers", 4))
    turns_per_episode = int(config.get("turns_per_episode", 0))
    candidates_dir = out_root / "candidates"
    judged_dir = out_root / "judged"
    admitted_dir = out_root / "admitted"

    state(workspace, "start", config=str(config_path), out_root=str(out_root))

    items_path = valid_candidate_items_path(candidates_dir)
    if items_path is None:
        args = [
            *command(config, "prepare_candidates"),
            "--joint",
            str(candidate_joint),
            "--manifest",
            str(source_joint_manifest),
            "--out-dir",
            str(candidates_dir),
            "--turns-per-episode",
            str(turns_per_episode),
        ]
        result = run_step(args, workspace, "prepare_candidates")
        if result.returncode == 3:
            state(workspace, "candidate_pending_or_quota", exit_code=3)
            return 3
        if result.returncode:
            state(workspace, "failed_prepare_candidates", exit_code=result.returncode)
            return 2
        items_path = valid_candidate_items_path(candidates_dir)
        if items_path is None:
            state(workspace, "failed_prepare_candidates", error=f"missing candidate items: {items_path}")
            return 2

    admission_path = admission_report_path(judged_dir)
    complete, counts = report_complete(admission_path)
    if not complete:
        args = [
            *command(config, "judge"),
            "--input",
            str(items_path),
            "--output",
            str(judged_dir),
            "--limit",
            "0",
            "--workers",
            str(judge_workers),
        ]
        append_cache_args(args, cache_roots)
        result = run_step(args, workspace, "judge_admission")
        if result.returncode in {2, 3}:
            counts = {}
            if admission_path.exists():
                counts = count_from_admission(read_json(admission_path))
            state(workspace, "admission_pending_or_quota", exit_code=3, counts=counts)
            return 3
        if result.returncode:
            state(workspace, "failed_admission_judge", exit_code=result.returncode)
            return 2
        complete, counts = report_complete(admission_path)
        if not complete:
            state(workspace, "admission_pending_or_quota", exit_code=3, counts=counts)
            return 3

    exported_manifest = admitted_dir / "joint.manifest.json"
    if not exported_manifest.exists():
        args = [
            *command(config, "export"),
            "--joint",
            str(candidate_joint),
            "--manifest",
            str(source_joint_manifest),
            "--candidates",
            str(items_path),
            "--cache-dir",
            str(judged_dir),
            "--out-dir",
            str(admitted_dir),
            "--turns-per-episode",
            str(turns_per_episode),
            "--minimum-final-per-source",
            str(int(config.get("minimum_final_per_source", 1))),
        ]
        result = run_step(args, workspace, "export_approved")
        if result.returncode:
            state(
                workspace,
                "failed_admission_gate",
                exit_code=2,
                export_exit_code=result.returncode,
                counts=counts,
            )
            return 2
        if not exported_manifest.exists():
            state(workspace, "failed_admission_gate", exit_code=2, error="missing export manifest")
            return 2

    preflight = [
        *command(config, "continue"),
        "--config",
        str(continuation_config),
        "--workspace",
        str(continuation_workspace),
    ]
    result = run_step(preflight, workspace, "continuation_preflight")
    if result.returncode == 3:
        state(workspace, "continuation_waiting", exit_code=3)
        return 3
    if result.returncode:
        state(workspace, "failed_continuation_preflight", exit_code=result.returncode)
        return 2

    execute = [*preflight, "--execute"]
    result = run_step(execute, workspace, "continuation_execute")
    if result.returncode == 3:
        state(workspace, "continuation_waiting", exit_code=3)
        return 3
    if result.returncode:
        state(workspace, "failed_continuation_execute", exit_code=result.returncode)
        return 2
    state(workspace, "complete", counts=counts, approved_manifest=str(exported_manifest))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return run_controller(args.config)
    except ControllerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
