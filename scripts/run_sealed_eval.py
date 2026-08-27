#!/usr/bin/env python3
"""Run the student harness against every sealed scenario without label leakage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

import yaml


def wait_for_model(url: str, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{url}/models", timeout=5) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(2)
    raise TimeoutError(f"model endpoint did not become ready: {url}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", type=Path, default=Path("configs/teacher/codex-blind-v1.yaml"))
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--restore", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("outputs/eval/sft-teacher-v1"))
    parser.add_argument("--base-url", default="http://localhost:8002/v1")
    parser.add_argument("--model", default="rca-actor")
    parser.add_argument(
        "--model-artifact",
        default="",
        help="immutable model or adapter path recorded for provenance",
    )
    parser.add_argument(
        "--structured-backend",
        choices=("guidance", "xgrammar", "outlines", "lm-format-enforcer", "unspecified"),
        default="unspecified",
        help="server-side structured-output backend recorded for provenance",
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--case",
        action="append",
        dest="selected_cases",
        help="run only this sealed case; repeat to select multiple cases",
    )
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    cases = yaml.safe_load(args.split.read_text(encoding="utf-8"))["sealed_eval"]
    if args.selected_cases:
        unknown = sorted(set(args.selected_cases) - set(cases))
        if unknown:
            parser.error(f"--case is not in the sealed split: {', '.join(unknown)}")
        selected = set(args.selected_cases)
        cases = [case for case in cases if case in selected]
    if args.output.exists() and any(args.output.iterdir()):
        raise SystemExit(f"refusing to mix evaluation artifacts in non-empty output: {args.output}")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "run-manifest.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now(UTC).isoformat(),
                "model": args.model,
                "model_artifact": args.model_artifact,
                "base_url": args.base_url,
                "structured_output_backend": args.structured_backend,
                "runs": args.runs,
                "cases": cases,
                "agent_sha256": hashlib.sha256(args.agent.read_bytes()).hexdigest(),
                "split_sha256": hashlib.sha256(args.split.read_bytes()).hexdigest(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    wait_for_model(args.base_url)
    shared_env = {
        **os.environ,
        "LUCIDA_AI_RCA_PROBE_URL": args.base_url,
        "LUCIDA_AI_MODEL_RCA_PROBE": args.model,
        "LUCIDA_AI_URL": args.base_url,
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
    summary.write_text("", encoding="utf-8")
    total_cases = len(cases)
    print(
        f"[eval] model={args.model} cases={total_cases} runs={args.runs} output={args.output}",
        flush=True,
    )
    for case_index, case in enumerate(cases, start=1):
        case_started = time.monotonic()
        print(f"[eval] [{case_index}/{total_cases}] restoring {case}", flush=True)
        case_dir = args.output / case
        case_dir.mkdir(parents=True, exist_ok=True)
        restored = subprocess.run(
            [str(args.restore), case], capture_output=True, text=True, check=False
        )
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
                [str(args.agent), current_incident],
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
