"""Resume data generation in an isolated workspace; never launch training.

Default: read-only inventory. --execute snapshots inputs and repairs rationalization
before exporting each stage. --not-before defers execution without probing quota.
Quota/infrastructure failures stop the process; rerunning resumes its workspace.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def candidates(stage: dict, root: Path = ROOT) -> list[Path]:
    train = set(yaml.safe_load((root / stage["split"]).read_text())["train"])
    result = []
    for name in stage["roots"]:
        for path in sorted((root / name).glob("case-*/run*/score.json")):
            score = json.loads(path.read_text())
            review = path.parent / "review.json"
            if (score.get("case_id") in train and not score.get("unscored")
                    and score.get("stop_reason") == "submitted"
                    and float(score.get("root_f1", 0)) >= 1.0
                    and float(score.get("ref_grounding", 0)) >= 1.0
                    and int(score.get("tool_calls", 0)) <= 50
                    and review.exists() and json.loads(review.read_text()).get("accepted") is True):
                result.append(path.parent)
    return result


def snapshot(source: Path, destination: Path) -> None:
    """Freeze all run files once. Partial copies are safe to retry; never overwrite repairs."""
    marker = destination / "recovery_source.json"
    if marker.exists():
        if json.loads(marker.read_text())["source"] != str(source.resolve()):
            raise ValueError(f"snapshot source mismatch: {destination}")
        return
    destination.mkdir(parents=True, exist_ok=True)
    files = {}
    for path in sorted(source.iterdir()):
        if not path.is_file():
            continue
        files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        shutil.copy2(path, destination / path.name)
    marker.write_text(json.dumps({"source": str(source.resolve()), "sha256": files}, indent=2) + "\n")


def save_state(workspace: Path, phase: str, **extra: object) -> None:
    payload = {"phase": phase, "utc": datetime.now(UTC).isoformat(), "pid": os.getpid(), **extra}
    temporary = workspace / "state.tmp"
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(workspace / "state.json")
    print(json.dumps(payload), flush=True)


def execute_command(command: list[str], log_path: Path) -> int:
    with log_path.open("a") as log:
        return subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False).returncode


def run_recovery(config: dict, workspace: Path) -> int:
    writer_provider = config.get("writer_provider", "claude")
    judge_provider = config.get("judge_provider", "claude")
    if writer_provider not in {"claude", "codex"} or judge_provider not in {"claude", "codex"}:
        raise ValueError("unsupported recovery model provider")
    workers = config.get("rationalize_workers", 1)
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("rationalize_workers must be an integer from 1 to 8")
    for stage in config["stages"]:
        name = stage["name"]
        dataset = workspace / "datasets" / f"{name}.jsonl"
        completed = workspace / f"{name}.complete.json"
        if completed.exists():
            record = json.loads(completed.read_text())
            if hashlib.sha256(dataset.read_bytes()).hexdigest() != record["sha256"]:
                raise ValueError(f"completed dataset changed: {dataset}")
            continue
        roots = []
        jobs = candidates(stage)
        if not jobs:
            raise ValueError(f"no eligible training candidates: {name}")
        save_state(workspace, "snapshot", stage=name, runs=len(jobs))
        for source in jobs:
            target_root = workspace / "sources" / source.parent.parent.name
            snapshot(source, target_root / source.parent.name / source.name)
            if target_root not in roots:
                roots.append(target_root)
        runner = stage.get("runner", "teacher")
        if runner not in {"teacher", "student"}:
            raise ValueError(f"unsupported source runner: {runner}")
        root_args = [arg for path in roots for arg in (f"--{runner}-root", str(path))]
        common = ["--cases-root", stage["cases_root"], "--max-turns", "50"]
        commands = [
            ("rationalize", [sys.executable, "scripts/mcp_rationalize.py", *root_args, *common,
                             "--writer", writer_provider, "--writer-model", config["writer_model"],
                             *(["--student-all", "--student-require-review"] if runner == "student" else []),
                             "--judge", "--judge-model", config["judge_model"],
                             "--judge-provider", judge_provider,
                             "--judge-rounds", "2", "--redo-stale-view",
                             *(["--redo-provider"] if config.get("redo_provider", False) else [])]),
            ("export", [sys.executable, "scripts/mcp_export_sft.py", *root_args, *common,
                        "--split", stage["split"], "--out", str(dataset), "--per-case", "2",
                        "--max-tool-calls", "50", "--min-root-f1", "1.0", "--require-grounding",
                        "--require-review", "--require-rationalized", "--require-judged",
                        "--max-unsupervised-fraction", "0.3"]),
        ]
        for phase, command in commands:
            count = workers if phase == "rationalize" else 1
            save_state(workspace, phase, stage=name, workers=count,
                       writer_provider=writer_provider, judge_provider=judge_provider)

            if count == 1:
                codes = [execute_command(command, workspace / f"{name}.{phase}.log")]
            else:
                with concurrent.futures.ThreadPoolExecutor(count) as pool:
                    futures = [pool.submit(execute_command, command + ["--shard", f"{shard}/{count}"],
                                           workspace / f"{name}.{phase}.shard-{shard}.log") for shard in range(count)]
                    codes = [future.result() for future in futures]
            failures = [code for code in codes if code]
            if failures:
                code = next((code for code in failures if code != 3), 3)
                save_state(workspace, "quota_blocked" if code == 3 else "failed",
                           stage=name, operation=phase, exit_code=code, shard_exit_codes=codes)
                return code
        manifest = json.loads(dataset.with_suffix(".manifest.json").read_text())
        if not manifest["examples"]:
            raise ValueError(f"empty dataset after export: {name}")
        completed.write_text(json.dumps({"sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
                                         "writer_provider": writer_provider, "writer_model": config["writer_model"],
                                         "judge_provider": judge_provider, "judge_model": config["judge_model"],
                                         "examples": manifest["examples"], "cases": manifest["cases"]}, indent=2) + "\n")
    save_state(workspace, "data_ready_for_review", training_started=False)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/teacher/data-recovery-20261007.yaml")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--not-before", help="ISO-8601 timestamp with timezone; no calls before this time")
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text())
    if not args.execute:
        for stage in config["stages"]:
            jobs = candidates(stage)
            print(json.dumps({"stage": stage["name"], "eligible_runs": len(jobs),
                              "eligible_cases": len({p.parent.name for p in jobs})}))
        return 0
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    with (workspace / "recovery.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            saved_config = workspace / "config.json"
            if saved_config.exists() and json.loads(saved_config.read_text()) != config:
                raise ValueError("recovery configuration changed; use a new workspace")
            if not saved_config.exists():
                saved_config.write_text(json.dumps(config, indent=2) + "\n")
            if args.not_before:
                deadline = datetime.fromisoformat(args.not_before)
                if deadline.tzinfo is None:
                    raise ValueError("--not-before requires an explicit timezone")
                save_state(workspace, "waiting_for_quota_reset", not_before=deadline.isoformat())
                while (remaining := deadline.timestamp() - time.time()) > 0:
                    time.sleep(min(remaining, 60))
            return run_recovery(config, workspace)
        except Exception as error:
            save_state(workspace, "failed", error=str(error))
            raise


if __name__ == "__main__":
    raise SystemExit(main())
