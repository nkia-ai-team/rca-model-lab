#!/usr/bin/env python3
"""Run the student harness against a typed scenario-split partition."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from urllib.request import urlopen

import yaml

from rca_lab.openai_proxy import enforced_openai_endpoint
from rca_lab.provenance import case_set_identity, file_sha256, resolve_model_identity


def served_model_ids(payload: object) -> set[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return set()
    return {
        str(item["id"])
        for item in payload["data"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def wait_for_model(url: str, expected_model: str, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{url}/models", timeout=5) as response:
                payload = json.load(response)
                if response.status == 200 and expected_model in served_model_ids(payload):
                    return
        except OSError:
            time.sleep(2)
    raise TimeoutError(f"model endpoint did not serve {expected_model}: {url}")


def eval_manifest_contract(args: argparse.Namespace, cases: list[str]) -> dict[str, object]:
    return {
        "model": args.model,
        "model_artifact": args.model_artifact,
        "model_artifact_sha256": resolve_model_identity(
            args.model_artifact, args.model_artifact_sha256
        ),
        "base_url": args.base_url,
        "structured_output_backend": args.structured_backend,
        "runs": args.runs,
        "cases": cases,
        "partition": args.partition,
        "actor_temperature": 0.0,
        "actor_seed": 0,
        "reasoning_strength": args.reasoning_strength,
        "request_contract_enforced": True,
        "restore_timeout_seconds": args.restore_timeout,
        "agent_sha256": file_sha256(args.agent),
        "restore_sha256": file_sha256(args.restore),
        "split_sha256": file_sha256(args.split),
        "case_set_sha256": case_set_identity(args.case_root, cases),
    }


def completed_episode(path: Path) -> bool:
    """A run is reusable only when it contains exactly one terminal episode event."""

    try:
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, json.JSONDecodeError):
        return False
    return sum(event.get("event") == "episode_completed" for event in events) == 1


def case_is_complete(case_dir: Path, runs: int) -> bool:
    for run in range(1, runs + 1):
        trajectories = list((case_dir / f"traj-run{run}").glob("agent-*.jsonl"))
        if len(trajectories) != 1 or not completed_episode(trajectories[0]):
            return False
        if not (case_dir / f"agent-run{run}.log").is_file():
            return False
    return True


def validate_resume_manifest(path: Path, expected: dict[str, object]) -> dict[str, object]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot resume without a valid evaluation manifest: {error}") from error
    mismatches = {
        key: (manifest.get(key), value)
        for key, value in expected.items()
        if manifest.get(key) != value
    }
    if mismatches:
        raise SystemExit(f"resume manifest mismatch: {mismatches}")
    return manifest


def archive_incomplete_case(case_dir: Path, archive_root: Path) -> None:
    if not case_dir.exists() or not any(case_dir.iterdir()):
        return
    archive_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    case_dir.rename(archive_root / f"{case_dir.name}-{stamp}")


def acquire_output_lock(output: Path) -> TextIO:
    """Prevent two evaluators from mutating shared stores for one run."""

    lock = (output / ".run.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock.close()
        raise SystemExit(f"evaluation output is already active: {output}") from error
    return lock


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", type=Path, default=Path("configs/teacher/codex-blind-v1.yaml"))
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--restore", type=Path, required=True)
    parser.add_argument("--case-root", type=Path, default=Path("/data/eval-cases"))
    parser.add_argument("--output", type=Path, default=Path("outputs/eval/sft-teacher-v1"))
    parser.add_argument("--base-url", default="http://localhost:8002/v1")
    parser.add_argument("--model", default="rca-actor")
    parser.add_argument(
        "--model-artifact",
        default="",
        help="immutable model or adapter path recorded for provenance",
    )
    parser.add_argument(
        "--model-artifact-sha256",
        default="",
        help="required when the immutable model artifact lives on a remote inference host",
    )
    parser.add_argument(
        "--structured-backend",
        choices=("guidance", "xgrammar", "outlines", "lm-format-enforcer", "unspecified"),
        default="unspecified",
        help="server-side structured-output backend recorded for provenance",
    )
    parser.add_argument(
        "--reasoning-strength",
        choices=("low", "medium", "high"),
        default="low",
        help="chat-template branch enforced by the evaluator; must match SFT",
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--restore-timeout",
        type=int,
        default=900,
        help="maximum seconds allowed for one idempotent scenario restore",
    )
    parser.add_argument(
        "--partition",
        choices=("train", "sealed_eval"),
        default="sealed_eval",
        help="split partition to execute; sealed_eval remains the default",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="selected_cases",
        help="run only this sealed case; repeat to select multiple cases",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reuse only complete cases after the immutable manifest matches exactly",
    )
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.restore_timeout < 1:
        parser.error("--restore-timeout must be at least 1")
    try:
        resolve_model_identity(args.model_artifact, args.model_artifact_sha256)
    except ValueError as error:
        parser.error(str(error))
    if not args.restore.is_file():
        parser.error("--restore must identify a readable reset executable")

    cases = yaml.safe_load(args.split.read_text(encoding="utf-8"))[args.partition]
    if args.selected_cases:
        unknown = sorted(set(args.selected_cases) - set(cases))
        if unknown:
            parser.error(f"--case is not in the {args.partition} split: {', '.join(unknown)}")
        selected = set(args.selected_cases)
        cases = [case for case in cases if case in selected]
    if not case_set_identity(args.case_root, cases):
        parser.error("--case-root is missing one or more selected case artifacts")
    non_empty_output = args.output.exists() and any(args.output.iterdir())
    if non_empty_output and not args.resume:
        raise SystemExit(f"refusing to mix evaluation artifacts in non-empty output: {args.output}")
    args.output.mkdir(parents=True, exist_ok=True)
    _output_lock = acquire_output_lock(args.output)
    manifest_contract = eval_manifest_contract(args, cases)
    manifest_path = args.output / "run-manifest.json"
    if args.resume:
        validate_resume_manifest(manifest_path, manifest_contract)
    else:
        manifest_path.write_text(
            json.dumps(
                {
                    "created_at": datetime.now(UTC).isoformat(),
                    **manifest_contract,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    wait_for_model(args.base_url, args.model)
    with enforced_openai_endpoint(
        args.base_url.removesuffix("/v1"),
        temperature=0.0,
        seed=0,
        reasoning_strength=args.reasoning_strength,
    ) as enforced_endpoint:
        run_evaluation(args, cases, f"{enforced_endpoint}/v1")


def run_evaluation(args: argparse.Namespace, cases: list[str], actor_base_url: str) -> None:
    shared_env = {
        **os.environ,
        "LUCIDA_AI_RCA_PROBE_URL": actor_base_url,
        "LUCIDA_AI_MODEL_RCA_PROBE": args.model,
        "LUCIDA_AI_URL": actor_base_url,
        "LUCIDA_AI_MODEL": args.model,
        "LUCIDA_QDRANT_URL": "http://localhost:1",
        "LUCIDA_LLM_PROVIDER": "vllm",
        "LUCIDA_SECRET_KEY": "eval-dummy",
        "POSTGRES_DSN": "postgres://lucida:lucida123@localhost:55432/lucida",
        "CLICKHOUSE_ADDR": "localhost:57001",
        "CLICKHOUSE_DB": "lucida",
        "CLICKHOUSE_USER": "lucida",
        "CLICKHOUSE_PASSWORD": "lucida123",
        "VM_QUERY_URL": "http://localhost:58428",
    }

    summary = args.output / "summary.txt"
    if not args.resume:
        summary.write_text("", encoding="utf-8")
    elif not summary.exists():
        summary.touch()
    total_cases = len(cases)
    print(
        f"[eval] model={args.model} cases={total_cases} runs={args.runs} output={args.output}",
        flush=True,
    )
    for case_index, case in enumerate(cases, start=1):
        case_started = time.monotonic()
        case_dir = args.output / case
        if args.resume and case_is_complete(case_dir, args.runs):
            print(f"[eval] [{case_index}/{total_cases}] reusing complete {case}", flush=True)
            continue
        if args.resume:
            archive_incomplete_case(case_dir, args.output / ".interrupted")
        print(f"[eval] [{case_index}/{total_cases}] restoring {case}", flush=True)
        case_dir.mkdir(parents=True, exist_ok=True)
        try:
            restored = subprocess.run(
                [str(args.restore), case],
                capture_output=True,
                text=True,
                check=False,
                timeout=args.restore_timeout,
            )
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode() if isinstance(error.stdout, bytes) else error.stdout or ""
            stderr = error.stderr.decode() if isinstance(error.stderr, bytes) else error.stderr or ""
            (case_dir / "restore.log").write_text(
                stdout
                + stderr
                + f"\nrestore timed out after {args.restore_timeout}s\n",
                encoding="utf-8",
            )
            with summary.open("a", encoding="utf-8") as stream:
                stream.write(f"{case}\tRESTORE_TIMEOUT\n")
            print(
                f"[eval] [{case_index}/{total_cases}] restore timed out {case} "
                f"after {args.restore_timeout}s",
                flush=True,
            )
            continue
        (case_dir / "restore.log").write_text(restored.stdout + restored.stderr, encoding="utf-8")
        matches = re.findall(r"incident id: ([0-9a-f-]+)", restored.stdout + restored.stderr)
        if restored.returncode or not matches:
            with summary.open("a", encoding="utf-8") as stream:
                stream.write(f"{case}\tRESTORE_FAIL\n")
            print(
                f"[eval] [{case_index}/{total_cases}] restore failed {case} rc={restored.returncode}",
                flush=True,
            )
            continue
        incident = matches[-1]
        print(
            f"[eval] [{case_index}/{total_cases}] running {args.runs} replicas for {case}",
            flush=True,
        )

        def execute(
            run: int,
            *,
            current_case_dir: Path = case_dir,
            current_incident: str = incident,
        ) -> tuple[int, int, str]:
            trajectory_dir = current_case_dir / f"traj-run{run}"
            trajectory_dir.mkdir(exist_ok=True)
            env = {**shared_env, "RCA_TRAJECTORY_DIR": str(trajectory_dir)}
            result = subprocess.run(
                [str(args.agent), "--actor-temperature", "0", current_incident],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            log = result.stdout + result.stderr
            (current_case_dir / f"agent-run{run}.log").write_text(log, encoding="utf-8")
            output = next((line for line in log.splitlines() if "OUTPUT |" in line), "NO_OUTPUT")
            return run, result.returncode, output

        # A restore mutates the shared evidence stores, so cases stay sequential. Replicates of
        # the already-restored case are read-only and can safely share vLLM's continuous batching.
        with ThreadPoolExecutor(max_workers=args.runs) as pool:
            results = list(pool.map(execute, range(1, args.runs + 1)))
        for run, return_code, output in results:
            with summary.open("a", encoding="utf-8") as stream:
                stream.write(f"{case}\trun={run}\trc={return_code}\t{output}\n")
        elapsed = time.monotonic() - case_started
        successful = sum(return_code == 0 for _, return_code, _ in results)
        print(
            f"[eval] [{case_index}/{total_cases}] completed {case} "
            f"success={successful}/{args.runs} elapsed={elapsed:.1f}s",
            flush=True,
        )


if __name__ == "__main__":
    main()
