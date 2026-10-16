"""Collect new-only Codex teacher episodes, recover/export, then prepare training data."""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
from mcp_judge_runs import judge_run

from rca_lab.mcp.rl_reward import judge_cache_path


def read_yaml(path: str | Path) -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8")) or {}


def split_members(config: dict) -> tuple[set[str], set[str]]:
    split = read_yaml(config["split"])
    train = set(split.get("train", []))
    protected = set(split.get("validation", [])) | set(split.get("sealed_eval", []))
    overlap = train & protected
    if overlap:
        raise ValueError(f"split train overlaps protected captures: {sorted(overlap)}")
    return train, protected


def prepare_retry_hints(config: dict) -> dict[str, str]:
    hints_path = config.get("retry_hints")
    if not hints_path:
        return {}
    threshold = config.get("retry_hints_after_attempt")
    if not isinstance(threshold, int) or threshold <= 0:
        raise ValueError("retry_hints_after_attempt must be a positive integer when retry_hints is set")
    train, protected = split_members(config)
    cases = set(config["cases"])
    outside_train = sorted(cases - train)
    if outside_train:
        raise ValueError(f"retry hints are limited to train captures; cases outside train: {outside_train}")
    raw = read_yaml(hints_path)
    hints = {str(case_id): str(text) for case_id, text in raw.items() if str(text).strip()}
    hinted_cases = set(hints)
    protected_hints = sorted(hinted_cases & protected)
    outside_hints = sorted(hinted_cases - train)
    if protected_hints:
        raise ValueError(f"retry hints must not include validation/sealed captures: {protected_hints}")
    if outside_hints:
        raise ValueError(f"retry hint keys outside train split: {outside_hints}")
    return hints


def retry_hints_path(config: dict, retry_hints: dict[str, str], case: str, attempt: int) -> str | None:
    if not retry_hints or attempt <= config["retry_hints_after_attempt"]:
        return None
    if case not in retry_hints:
        raise RuntimeError(
            f"{case}: guided retry skipped after {attempt - 1} attempts; no retry hint configured"
        )
    return str((ROOT / config["retry_hints"]).resolve())


def configured_retry_hints(config: dict) -> dict[str, str]:
    if "retry_hints" not in config:
        return {}
    return config.get("_retry_hints_by_case", {})


def command_retry_hints_path(config: dict, case: str, attempt: int) -> str | None:
    if not config.get("retry_hints"):
        return None
    return retry_hints_path(config, configured_retry_hints(config), case, attempt)


def state(workspace: Path, phase: str, **extra) -> None:
    temporary = workspace / "state.tmp"
    temporary.write_text(json.dumps({"phase": phase, "utc": datetime.now(UTC).isoformat(),
                                     "pid": os.getpid(), **extra}, indent=2))
    temporary.replace(workspace / "state.json")


def collect_case(config: dict, case: str, index: int) -> dict:
    case_dir = ROOT / "outputs/mcp-eval" / config["run_name"] / case
    accepted = 0
    for attempt in range(1, config["max_attempts"] + 1):
        run = case_dir / f"run{attempt}"
        if not (run / "score.json").exists():
            command = [sys.executable, "scripts/mcp_case_run.py", "--runner", "student",
                       "--endpoint", "codex://local", "--model", config["model"],
                       "--no-guided", "--max-prompt-tokens", "0", "--request-timeout", "900",
                       "--run-name", config["run_name"], "--split", config["split"],
                       "--contract", config["contract"], "--cases-root", config["cases_root"],
                       "--case", case, "--runs", str(attempt), "--max-turns", str(config["max_turns"]),
                       "--mcp-binary", str((ROOT / config["mcp_binary"]).resolve()),
                       "--project-prefix", f"cn17{index:02d}", "--reuse-db", "--keep-db"]
            hints = command_retry_hints_path(config, case, attempt)
            if hints:
                command.extend(["--hints", hints])
            log_path = ROOT / config["workspace"] / f"{case}.log"
            with log_path.open("a") as log:
                subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
        if not (run / "score.json").exists():
            raise RuntimeError(f"{case} attempt {attempt}: no score; see restore/CLI log")
        score = json.loads((run / "score.json").read_text())
        valid = score.get("stop_reason") == "submitted" and score.get("root_f1") == 1 and score.get("ref_grounding") == 1 and score.get("tool_calls", 0) <= 50
        if valid:
            outcome = judge_run(run, "gpt-6.1-sol", "v2", False, "codex")
            if outcome not in {"judged", "cached"}:
                raise RuntimeError(f"mechanism judge incomplete: {case}/{attempt}: {outcome}")
            cache = judge_cache_path(run, "codex", model="gpt-6.1-sol")
            judgment = json.loads(cache.read_text())
            valid = judgment.get("score") == 1
            (run / "review.json").write_text(json.dumps({"accepted": valid, "provider": "codex",
                "model": "gpt-6.1-sol", "mechanism_judge": str(cache), "note": "Root, grounded refs, and mechanism rubric v2"}, indent=2))
        accepted += int(valid)
        if accepted >= config["accepted_per_case"]:
            # Only our unique project is removed; source capture and episode artifacts stay.
            from mcp_case_run import teardown
            teardown(f"cn17{index:02d}-" + case.split("-v3-")[0].replace("case-", ""))
            return {"case_id": case, "accepted": accepted, "attempts": attempt}
    raise RuntimeError(f"{case}: {accepted} accepted episodes after {config['max_attempts']} attempts")


def run(config_path: Path) -> None:
    config = yaml.safe_load(config_path.read_text())
    config["_retry_hints_by_case"] = prepare_retry_hints(config)
    workspace = ROOT / config["workspace"]
    workspace.mkdir(parents=True, exist_ok=True)
    with (workspace / "generation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state(workspace, "collecting", cases=len(config["cases"]), workers=config["workers"])
        completed = []
        failures = []
        with concurrent.futures.ThreadPoolExecutor(config["workers"]) as pool:
            jobs = {pool.submit(collect_case, config, case, i): case for i, case in enumerate(config["cases"])}
            for future in concurrent.futures.as_completed(jobs):
                try:
                    completed.append(future.result())
                except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                    failures.append({"case_id": jobs[future], "error": str(exc)})
                state(workspace, "collecting", completed=completed, failures=failures,
                      total_cases=len(config["cases"]))
        if failures:
            state(workspace, "needs_repair", completed=completed, failures=failures)
            return
        state(workspace, "recovering", completed=completed)
        with (workspace / "recovery.log").open("a") as log:
            subprocess.run([sys.executable, "scripts/mcp_recover_data.py", "--execute",
                            "--workspace", str(workspace), "--config", config["recovery_config"]],
                           cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
        subprocess.run([sys.executable, "scripts/mcp_prepare_polestar_expanded.py", "--config", config["merge_config"]], cwd=ROOT, check=True)
        launcher = ROOT / config["training_launcher"]
        state(workspace, "dataset_ready", training_started=False, launcher=str(launcher))
        if launcher.exists():
            while True:
                result = subprocess.run(["bash", str(launcher)], cwd=ROOT, check=False)
                if result.returncode == 0:
                    break
                if result.returncode != 75:
                    raise RuntimeError(f"training launcher failed: {result.returncode}")
                state(workspace, "waiting_training_runtime", launcher=str(launcher))
                time.sleep(30)
            state(workspace, "training_launched", launcher=str(launcher))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    run(parser.parse_args().config)
