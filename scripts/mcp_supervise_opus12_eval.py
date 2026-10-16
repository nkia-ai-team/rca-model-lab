"""Durable supervisor for the 12-case/24-run harness comparison eval."""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(ROOT / ".venv/bin/python")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def state(work: Path, phase: str, **extra: Any) -> None:
    payload = {"phase": phase, "utc": now(), **extra}
    write_json(work / "state.json", payload)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def score_paths(case_dir: Path, runs_per_case: int) -> list[Path]:
    return [case_dir / f"run{index}" / "score.json" for index in range(1, runs_per_case + 1)]


def case_complete(case_dir: Path, runs_per_case: int) -> bool:
    return all(path.exists() for path in score_paths(case_dir, runs_per_case))


def all_expected_scores(aggregate: Path, cases: list[str], runs_per_case: int) -> bool:
    expected = {(case, run) for case in cases for run in range(1, runs_per_case + 1)}
    actual: set[tuple[str, int]] = set()
    for path in aggregate.glob("case-*/run*/score.json"):
        row = read_json(path)
        if not row.get("unscored"):
            actual.add((row["case_id"], int(row["run"])))
    return actual == expected


def collect_metrics(aggregate: Path) -> dict[str, Any]:
    rows = []
    for path in sorted(aggregate.glob("case-*/run*/score.json")):
        row = read_json(path)
        if not row.get("unscored"):
            rows.append(row)
    return {
        "completed": len(rows),
        "root_hits": sum(row.get("root_f1") == 1 for row in rows),
        "mean_root_f1": (sum(float(row.get("root_f1", 0.0)) for row in rows) / len(rows)) if rows else None,
        "submitted": sum(row.get("stop_reason") == "submitted" for row in rows),
    }


def ensure_link(aggregate: Path, case: str, source: Path) -> None:
    aggregate.mkdir(parents=True, exist_ok=True)
    link = aggregate / case
    if link.is_symlink() and link.resolve() == source.resolve():
        return
    if link.exists() or link.is_symlink():
        raise RuntimeError(f"aggregate link already exists: {link} -> {link.resolve()}")
    link.symlink_to(source, target_is_directory=True)


def assert_runtime(plan: dict[str, Any]) -> None:
    for rel, digest in plan["runtime_sha256"].items():
        actual = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        if actual != digest:
            raise RuntimeError(f"runtime hash mismatch for {rel}: {actual} != {digest}")
    binary_hash = hashlib.sha256((ROOT / "tools/rca-mcp/bin/rca-mcp").read_bytes()).hexdigest()
    if binary_hash != plan["mcp_binary_sha256"]:
        raise RuntimeError("rca-mcp binary hash mismatch")


def wait_endpoint(plan: dict[str, Any]) -> None:
    marker = ROOT / plan["serving_marker"]
    if not marker.exists():
        raise RuntimeError(f"serving marker missing: {marker}")
    payload = read_json(marker)
    if payload.get("status") != "success":
        raise RuntimeError(f"serving marker is not successful: {marker}")
    if payload.get("endpoint") != plan["endpoint"] or payload.get("model") != plan["model"]:
        raise RuntimeError(f"serving marker does not match endpoint/model: {payload}")


def run_case(plan: dict[str, Any], work: Path, aggregate: Path, case: str, index: int) -> dict[str, Any]:
    shard = f"harness-{index:02d}"
    case_dir = work / "shards" / shard / case
    case_dir.mkdir(parents=True, exist_ok=True)
    ensure_link(aggregate, case, case_dir)
    if case_complete(case_dir, int(plan["runs_per_case"])):
        return {"case": case, "status": "cached", "shard": shard}
    cmd = [
        PYTHON,
        "scripts/mcp_case_run.py",
        "--runner",
        "student",
        "--student-tools",
        "harness",
        "--run-name",
        shard,
        "--out-root",
        str(work / "shards"),
        "--case",
        case,
        "--project-prefix",
        f"{plan['project_prefix']}{index:02d}",
        "--runs",
        str(plan["runs_per_case"]),
        "--max-turns",
        str(plan["max_turns"]),
        "--endpoint",
        plan["endpoint"],
        "--model",
        plan["model"],
        "--max-prompt-tokens",
        str(plan["max_prompt_tokens"]),
        "--tool-result-max-chars",
        str(plan["tool_result_max_chars"]),
        "--temperature",
        str(plan["temperature"]),
        "--top-p",
        str(plan["top_p"]),
        "--top-k",
        str(plan["top_k"]),
        "--seed",
        str(plan["seed"]),
        "--mcp-binary",
        str(ROOT / "tools/rca-mcp/bin/rca-mcp"),
        "--no-guided",
    ]
    write_json(work / f"{shard}.command.json", cmd)
    with (work / f"{shard}.log").open("a", encoding="utf-8") as log:
        rc = subprocess.run(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False).returncode
    return {"case": case, "status": "complete" if rc == 0 and case_complete(case_dir, int(plan["runs_per_case"])) else "failed", "returncode": rc, "shard": shard}


def judge_and_report(plan: dict[str, Any], work: Path, aggregate: Path) -> int:
    judge = plan["judge"]
    judge_cmd = [
        PYTHON,
        "scripts/mcp_judge_runs.py",
        str(aggregate),
        "--provider",
        judge["provider"],
        "--model",
        judge["model"],
        "--rubric",
        judge["rubric"],
        "--workers",
        str(judge["workers"]),
    ]
    with (work / "judge.log").open("a", encoding="utf-8") as log:
        judge_rc = subprocess.run(judge_cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False).returncode
    report = subprocess.run(
        [
            PYTHON,
            "scripts/mcp_eval_report.py",
            str(aggregate),
            "--provider",
            judge["provider"],
            "--model",
            judge["model"],
            "--rubric",
            judge["rubric"],
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    (work / "report.json").write_text(report.stdout, encoding="utf-8")
    reports = json.loads(report.stdout)
    done = judge_rc == 0 and len(reports) == 1 and reports[0]["total"] == plan["expected_total"] and reports[0]["unresolved_hits"] == 0
    state(work, "complete" if done else "incomplete_judging", metrics=collect_metrics(aggregate), reports=reports, judge_exit=judge_rc)
    return 0 if done else 1


def supervise(plan_path: Path) -> int:
    plan = read_json(plan_path)
    work = plan_path.parent
    aggregate = ROOT / plan["aggregate_root"]
    assert_runtime(plan)
    wait_endpoint(plan)
    state(work, "running", aggregate_root=str(aggregate), endpoint=plan["endpoint"], model=plan["model"])
    if all_expected_scores(aggregate, plan["cases"], int(plan["runs_per_case"])):
        state(work, "mechanism_judging", metrics=collect_metrics(aggregate))
        return judge_and_report(plan, work, aggregate)
    failures: list[dict[str, Any]] = []
    with cf.ThreadPoolExecutor(max_workers=int(plan["concurrency"])) as pool:
        futures = {
            pool.submit(run_case, plan, work, aggregate, case, index): case
            for index, case in enumerate(plan["cases"])
        }
        for future in cf.as_completed(futures):
            result = future.result()
            if result["status"] == "failed":
                failures.append(result)
            state(work, "rollouts_active", latest=result, metrics=collect_metrics(aggregate), failures=failures)
            if failures:
                break
    if failures:
        state(work, "failed", failures=failures, metrics=collect_metrics(aggregate))
        return 2
    if not all_expected_scores(aggregate, plan["cases"], int(plan["runs_per_case"])):
        state(work, "incomplete_rollouts", metrics=collect_metrics(aggregate))
        return 3
    state(work, "mechanism_judging", metrics=collect_metrics(aggregate))
    return judge_and_report(plan, work, aggregate)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    args = parser.parse_args()
    return supervise(args.plan.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
