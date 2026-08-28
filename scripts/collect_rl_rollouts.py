#!/usr/bin/env python3
"""Collect grouped on-policy student episodes from training scenarios."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from rca_lab.provenance import case_set_identity, file_sha256, model_artifact_identity


def run_agent(
    *,
    agent: Path,
    incident: str,
    trajectory_dir: Path,
    base_env: dict[str, str],
    temperature: float,
    seed: int,
) -> tuple[int, str]:
    trajectory_dir.mkdir(parents=True, exist_ok=True)
    env = {**base_env, "RCA_TRAJECTORY_DIR": str(trajectory_dir)}
    result = subprocess.run(
        [
            str(agent),
            "--actor-temperature",
            str(temperature),
            "--actor-seed",
            str(seed),
            incident,
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def load_eligible_scenarios(dataset: Path) -> set[str]:
    """Use the accepted SFT population; incomplete teacher cases stay out of RL."""
    return {
        str(json.loads(line)["scenario_id"])
        for line in dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def rollout_seed(base_seed: int, case: str, rollout_index: int) -> int:
    """Derive a stable, distinct non-negative seed from rollout identity."""
    payload = f"{base_seed}:{case}:{rollout_index}".encode()
    value = struct.unpack(">Q", hashlib.sha256(payload).digest()[:8])[0]
    return value & ((1 << 63) - 1)


def trajectory_completed(path: Path) -> bool:
    files = sorted(path.glob("agent-*.jsonl"))
    if len(files) != 1:
        return False
    lines = [line for line in files[0].read_text(encoding="utf-8").splitlines() if line]
    if not lines:
        return False
    try:
        return json.loads(lines[-1]).get("event") == "episode_completed"
    except json.JSONDecodeError:
        return False


def completed_trajectory_dirs(case_dir: Path, group_size: int) -> list[Path]:
    marker = case_dir / "case-complete.json"
    if marker.exists():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            paths = [case_dir / value for value in payload["trajectory_dirs"]]
        except (json.JSONDecodeError, KeyError, TypeError):
            return []
        return paths if len(paths) == group_size and all(map(trajectory_completed, paths)) else []
    legacy = [case_dir / f"rollout-{index:02d}" for index in range(1, group_size + 1)]
    return legacy if all(map(trajectory_completed, legacy)) else []


def manifest_contract(args: argparse.Namespace, cases: list[str]) -> dict[str, Any]:
    return {
        "model": args.model,
        "model_artifact": args.model_artifact,
        "model_artifact_sha256": model_artifact_identity(args.model_artifact),
        "base_url": args.base_url,
        "structured_output_backend": args.structured_backend,
        "group_size": args.group_size,
        "temperature": args.temperature,
        "base_seed": args.seed,
        "seed_strategy": "sha256(base_seed:case_id:rollout_index)/int63",
        "cases": cases,
        "agent_sha256": hashlib.sha256(args.agent.read_bytes()).hexdigest(),
        "restore_sha256": file_sha256(args.restore),
        "split_sha256": hashlib.sha256(args.split.read_bytes()).hexdigest(),
        "eligible_dataset_sha256": hashlib.sha256(args.eligible_dataset.read_bytes()).hexdigest(),
        "case_set_sha256": case_set_identity(args.case_root, cases),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", type=Path, default=Path("configs/teacher/codex-blind-v1.yaml"))
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--restore", type=Path, required=True)
    parser.add_argument("--case-root", type=Path, default=Path("/data/eval-cases"))
    parser.add_argument("--output", type=Path, default=Path("outputs/rl/rollouts-v1"))
    parser.add_argument(
        "--eligible-dataset",
        type=Path,
        default=Path("data/processed/sft-teacher-v1.jsonl"),
    )
    parser.add_argument("--base-url", default="http://localhost:8003/v1")
    parser.add_argument("--model", default="rca-actor")
    parser.add_argument(
        "--model-artifact",
        default="",
        help="immutable behavior-policy path recorded for provenance",
    )
    parser.add_argument(
        "--structured-backend",
        choices=("guidance", "xgrammar", "outlines", "lm-format-enforcer", "unspecified"),
        default="unspecified",
        help="server-side structured-output backend recorded for provenance",
    )
    parser.add_argument("--group-size", type=int, default=4)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume only when the immutable rollout manifest matches exactly",
    )
    args = parser.parse_args()
    if args.group_size < 1:
        parser.error("--group-size must be at least 1")
    if not 0 < args.temperature <= 2:
        parser.error("--temperature must be in (0, 2]")
    if args.seed < 0:
        parser.error("--seed must be non-negative")
    if not args.model_artifact or not model_artifact_identity(args.model_artifact):
        parser.error("--model-artifact must identify a readable immutable model artifact")
    if not args.restore.is_file():
        parser.error("--restore must identify a readable reset executable")

    eligible = load_eligible_scenarios(args.eligible_dataset)
    cases = [
        case
        for case in yaml.safe_load(args.split.read_text(encoding="utf-8"))["train"]
        if case in eligible
    ]
    if args.max_cases:
        cases = cases[: args.max_cases]
    if not case_set_identity(args.case_root, cases):
        parser.error("--case-root is missing one or more selected case artifacts")
    contract = manifest_contract(args, cases)
    manifest_path = args.output / "run-manifest.json"
    if args.output.exists() and any(args.output.iterdir()):
        if not args.resume:
            raise SystemExit(
                f"refusing to mix rollout artifacts in non-empty output: {args.output}"
            )
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise SystemExit(f"cannot resume without a valid manifest: {error}") from error
        mismatches = {
            key: (existing_manifest.get(key), value)
            for key, value in contract.items()
            if existing_manifest.get(key) != value
        }
        if mismatches:
            raise SystemExit(f"resume manifest mismatch: {mismatches}")
    args.output.mkdir(parents=True, exist_ok=True)
    if not manifest_path.exists():
        manifest_path.write_text(
            json.dumps(
                {
                    "created_at": datetime.now(UTC).isoformat(),
                    **contract,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    shared_env = {
        **os.environ,
        "LUCIDA_AI_RCA_PROBE_URL": args.base_url,
        "LUCIDA_AI_MODEL_RCA_PROBE": args.model,
        "LUCIDA_AI_URL": args.base_url,
        "LUCIDA_AI_MODEL": args.model,
        "LUCIDA_QDRANT_URL": "http://localhost:1",
        "LUCIDA_LLM_PROVIDER": "vllm",
        "LUCIDA_SECRET_KEY": "rl-dummy",
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
    total_cases = len(cases)
    print(
        f"[rollout] model={args.model} cases={total_cases} group_size={args.group_size} "
        f"output={args.output}",
        flush=True,
    )
    for case_index, case in enumerate(cases, start=1):
        case_started = time.monotonic()
        case_dir = args.output / case
        case_dir.mkdir(parents=True, exist_ok=True)
        if completed_trajectory_dirs(case_dir, args.group_size):
            print(f"[rollout] [{case_index}/{total_cases}] already complete {case}", flush=True)
            continue
        attempt_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        attempt_dir = case_dir / "attempts" / attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=False)
        print(f"[rollout] [{case_index}/{total_cases}] restoring {case}", flush=True)
        restored = subprocess.run(
            [str(args.restore), case], capture_output=True, text=True, check=False
        )
        (attempt_dir / "restore.log").write_text(
            restored.stdout + restored.stderr, encoding="utf-8"
        )
        matches = re.findall(r"incident id: ([0-9a-f-]+)", restored.stdout + restored.stderr)
        if restored.returncode or not matches:
            with summary.open("a", encoding="utf-8") as stream:
                stream.write(f"{case}\tRESTORE_FAIL\n")
            print(
                f"[rollout] [{case_index}/{total_cases}] restore failed {case} "
                f"rc={restored.returncode}",
                flush=True,
            )
            continue
        incident = matches[-1]
        print(
            f"[rollout] [{case_index}/{total_cases}] running {args.group_size} samples for {case}",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=args.group_size) as pool:
            futures = [
                pool.submit(
                    run_agent,
                    agent=args.agent,
                    incident=incident,
                    trajectory_dir=attempt_dir / f"rollout-{index:02d}",
                    base_env=shared_env,
                    temperature=args.temperature,
                    seed=rollout_seed(args.seed, case, index),
                )
                for index in range(1, args.group_size + 1)
            ]
            for index, future in enumerate(futures, 1):
                return_code, log = future.result()
                (attempt_dir / f"rollout-{index:02d}.log").write_text(log, encoding="utf-8")
                output = next(
                    (line for line in log.splitlines() if "OUTPUT |" in line), "NO_OUTPUT"
                )
                with summary.open("a", encoding="utf-8") as stream:
                    stream.write(f"{case}\trollout={index}\trc={return_code}\t{output}\n")
        trajectories = [
            attempt_dir / f"rollout-{index:02d}" for index in range(1, args.group_size + 1)
        ]
        if not all(map(trajectory_completed, trajectories)):
            print(
                f"[rollout] [{case_index}/{total_cases}] incomplete trajectories {case}",
                flush=True,
            )
            continue
        marker_payload = {
            "completed_at": datetime.now(UTC).isoformat(),
            "attempt": attempt_id,
            "trajectory_dirs": [str(path.relative_to(case_dir)) for path in trajectories],
        }
        marker_tmp = case_dir / "case-complete.json.tmp"
        marker_tmp.write_text(json.dumps(marker_payload, indent=2) + "\n", encoding="utf-8")
        marker_tmp.replace(case_dir / "case-complete.json")
        elapsed = time.monotonic() - case_started
        print(
            f"[rollout] [{case_index}/{total_cases}] completed {case} elapsed={elapsed:.1f}s",
            flush=True,
        )


if __name__ == "__main__":
    main()
