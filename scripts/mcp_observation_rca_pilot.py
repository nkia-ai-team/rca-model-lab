#!/usr/bin/env python3
"""Run a bounded 2x2 full-episode observation-policy RCA pilot on one train case.

This is a development pilot for harness comparison only. It does not train a model
and does not estimate general RCA accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rca_lab.mcp.golden import load_contract
from rca_lab.mcp.seed import build_observed_seed

CASE_ID = "case-rcaeval-tt4-v3-158f2019"
DEFAULT_OUT = ROOT / "outputs/observation-rca-pilot-20261007"
DEFAULT_CASES_ROOT = ROOT / "data/external/rcaeval/captures"
DEFAULT_SPLIT = ROOT / "configs/teacher/rcaeval-tt4-split.yaml"
DEFAULT_CONTRACT = ROOT / "configs/eval/rcaeval-tt4.yaml"
DEFAULT_MCP_BINARY = ROOT / "tools/rca-mcp/bin/rca-mcp"
DEFAULT_ENDPOINT = "http://127.0.0.1:18002/v1"
MODELS = ("muse-glimmer-base", "mcp-v2bs-checkpoint-94")
POLICIES = ("legacy", "structured-v1")


def _load_case_run_module() -> Any:
    path = ROOT / "scripts/mcp_case_run.py"
    spec = importlib.util.spec_from_file_location("mcp_case_run_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def available_models(endpoint: str) -> set[str]:
    with urllib.request.urlopen(endpoint.rstrip("/") + "/models", timeout=10) as response:
        payload = json.load(response)
    return {str(item.get("id")) for item in payload.get("data", [])}


def git_head() -> str:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def verify_inputs(args: argparse.Namespace) -> dict[str, Any]:
    split = yaml.safe_load(args.split.read_text(encoding="utf-8"))
    train = set(split.get("train") or [])
    if CASE_ID not in train:
        raise RuntimeError(f"{CASE_ID} is not in train partition of {args.split}")
    case_dir = args.cases_root / CASE_ID
    if not case_dir.exists():
        raise RuntimeError(f"capture missing: {case_dir}")
    if not args.mcp_binary.exists():
        raise RuntimeError(f"rca-mcp binary missing: {args.mcp_binary}")
    served = available_models(args.endpoint)
    missing = [model for model in MODELS if model not in served]
    if missing:
        raise RuntimeError(f"model(s) not served at {args.endpoint}: {missing}")
    contract = load_contract(args.contract)
    if CASE_ID not in contract:
        raise RuntimeError(f"{CASE_ID} missing from contract {args.contract}")
    return {
        "case_dir": str(case_dir),
        "split": str(args.split),
        "train_verified": True,
        "contract": str(args.contract),
        "mcp_binary": str(args.mcp_binary),
        "mcp_binary_sha256": sha256_file(args.mcp_binary),
        "endpoint": args.endpoint,
        "models": list(MODELS),
        "served_models": sorted(model for model in served if model in MODELS),
        "git_head": git_head(),
        "script_sha256": sha256_file(Path(__file__)),
    }




def completed_score(run_dir: Path) -> dict[str, Any] | None:
    score_path = run_dir / "score.json"
    trajectory_path = run_dir / "trajectory.jsonl"
    if not score_path.exists() or not trajectory_path.exists():
        return None
    try:
        score = json.loads(score_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return score if score else None


def source_hashes(args: argparse.Namespace) -> dict[str, Any]:
    case_dir = args.cases_root / CASE_ID
    files: dict[str, str] = {}
    for path in sorted(case_dir.rglob("*")):
        if path.is_file():
            files[str(path.relative_to(ROOT))] = sha256_file(path)
    for path in (args.split, args.contract, args.mcp_binary, ROOT / "src/rca_lab/mcp/observations.py", ROOT / "src/rca_lab/mcp/student.py", ROOT / "src/rca_lab/mcp/context.py", ROOT / "scripts/mcp_case_run.py", Path(__file__)):
        if path.exists():
            files[str(path.relative_to(ROOT))] = sha256_file(path)
    return {
        "case_id": CASE_ID,
        "case_dir": str(case_dir),
        "files_sha256": files,
    }

def persist_run_contract(output: Path, config: dict[str, Any], provenance: dict[str, Any]) -> None:
    """Resume only identical runs and never overwrite the original provenance."""
    config_path = output / "runtime-config.json"
    provenance_path = output / "provenance.json"
    if config_path.exists() or provenance_path.exists() or any(output.glob("*/run1/score.json")):
        if not config_path.exists() or not provenance_path.exists():
            raise RuntimeError("Incomplete run provenance; use a new output directory")
        previous = json.loads(config_path.read_text(encoding="utf-8"))
        old_contract = {key: value for key, value in previous.items() if key != "created_at_utc"}
        new_contract = {key: value for key, value in config.items() if key != "created_at_utc"}
        if old_contract != new_contract or json.loads(provenance_path.read_text(encoding="utf-8")) != provenance:
            raise RuntimeError("Run configuration or source hashes changed; use a new output directory")
        return
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=1), encoding="utf-8")


def arm_args(args: argparse.Namespace, *, model: str, policy: str) -> SimpleNamespace:
    return SimpleNamespace(
        runner="student",
        endpoint=args.endpoint,
        model=model,
        no_alias=False,
        no_guided=False,
        no_reasoning_guard=False,
        max_turns=args.max_turns,
        max_prompt_tokens=40_000,
        tool_result_max_chars=6_000,
        observation_policy=policy,
        temperature=1.0,
        top_p=0.95,
        top_k=64,
        seed=args.seed,
    )


def summarize(out_dir: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [row for row in rows if not row.get("unscored")]
    summary = {
        "scope": "single train-case full-episode development pilot; not general RCA accuracy and not a model-gain claim",
        "case_id": CASE_ID,
        "episodes": len(rows),
        "strict_correct": sum(bool(row.get("strict_correct")) for row in scored),
        "scored": len(scored),
        "mean_root_f1": (sum(float(row.get("root_f1", 0.0)) for row in scored) / len(scored)) if scored else 0.0,
        "rows": rows,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [
        "| model | policy | stop | status | root_f1 | strict | calls | errors | seconds |",
        "|---|---|---|---|---:|---|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('model')} | {row.get('observation_policy')} | {row.get('stop_reason')} | {row.get('status')} | "
            f"{row.get('root_f1', '-')} | {row.get('strict_correct', '-')} | {row.get('tool_calls', '-')} | "
            f"{row.get('tool_errors', '-')} | {row.get('wall_seconds', '-')} |"
        )
    lines.append(f"\nstrict {summary['strict_correct']}/{summary['scored']}  mean_root_f1 {summary['mean_root_f1']:.3f}")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--cases-root", type=Path, default=DEFAULT_CASES_ROOT)
    parser.add_argument("--split", type=Path, default=DEFAULT_SPLIT)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--mcp-binary", type=Path, default=DEFAULT_MCP_BINARY)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--seed", type=int, default=7107)
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--max-alarms", type=int, default=50)
    parser.add_argument("--project", default=f"obsrcapilot-{CASE_ID.rsplit('-', 1)[-1]}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    case_run = _load_case_run_module()
    config = verify_inputs(args)
    config.update(
        {
            "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "case_id": CASE_ID,
            "seed": args.seed,
            "max_turns": args.max_turns,
            "max_alarms": args.max_alarms,
            "policies": list(POLICIES),
            "docker_project": args.project,
        }
    )
    persist_run_contract(args.output, config, source_hashes(args))

    case_dir = args.cases_root / CASE_ID
    connections_path = args.output / "connections.json"
    project = args.project
    restored = False
    rows: list[dict[str, Any]] = []
    try:
        connections = case_run.restore_case(case_dir, project, connections_path, args.output / "restore.log")
        restored = True
        connections = {**connections, **case_run.external_capture_env(case_dir)}
        raw_seed = build_observed_seed(case_dir, max_alarms=args.max_alarms)
        seed = case_run.blind_seed(args.mcp_binary, raw_seed)
        seed_text = json.dumps(seed, ensure_ascii=False, indent=1)
        (args.output / "seed.json").write_text(seed_text, encoding="utf-8")
        (args.output / "seed.sha256").write_text(sha256_text(seed_text) + "\n", encoding="utf-8")
        expected = load_contract(args.contract)[CASE_ID]
        for model in MODELS:
            for policy in POLICIES:
                arm = f"{model}.{policy}"
                run_dir = args.output / arm / "run1"
                run_dir.mkdir(parents=True, exist_ok=True)
                existing = completed_score(run_dir)
                if existing is not None:
                    existing["arm"] = arm
                    existing["observation_policy"] = policy
                    rows.append(existing)
                    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] skip completed {arm}", flush=True)
                    (args.output / "partial-summary.json").write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
                    summarize(args.output, rows)
                    continue
                print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] start {arm}", flush=True)
                started = time.time()
                score, _ = case_run.run_one(
                    arm_args(args, model=model, policy=policy),
                    CASE_ID,
                    run_dir,
                    1,
                    seed,
                    connections,
                    args.mcp_binary,
                    expected,
                    None,
                )
                score["arm"] = arm
                score["observation_policy"] = policy
                score["pilot_wall_seconds"] = round(time.time() - started, 3)
                rows.append(score)
                (args.output / "partial-summary.json").write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
                summarize(args.output, rows)
        summary = summarize(args.output, rows)
        print(json.dumps({"output": str(args.output / "summary.json"), "summary": summary}, ensure_ascii=False, indent=1), flush=True)
        return 0
    finally:
        if restored:
            case_run.teardown(project)


if __name__ == "__main__":
    raise SystemExit(main())
