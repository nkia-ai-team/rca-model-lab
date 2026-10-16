"""Continue the recovered MCP training chain after data recovery is complete.

Default mode is read-only preflight. Use --execute from a persistent service to wait for
datasets, validate them, sync only bounded training assets to KT, train the staged chain,
and run fresh non-sealed validation.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import shlex
import stat
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs/sft/recovered-chain-20261007.yaml"


class ChainError(RuntimeError):
    """Non-resumable chain failure."""


class Waiting(RuntimeError):
    """Resumable wait condition."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def state(workspace: Path, phase: str, **extra: Any) -> None:
    payload = {"phase": phase, "utc": utc_now(), "pid": os.getpid(), **extra}
    atomic_json(workspace / "state.json", payload)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def load_config(path: Path, recovery_workspace: Path | None = None) -> dict[str, Any]:
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if recovery_workspace is not None:
        for stage in config.get("stages", []):
            stage["dataset"] = str(recovery_workspace / "datasets" / f"{stage['key']}.jsonl")
    for stage in config.get("stages", []):
        stage.setdefault("recovery_expected", config.get("recovery_expected") or {})
    return config


def split_sets(path: Path) -> dict[str, set[str]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {key: set(raw.get(key) or []) for key in ("train", "validation", "sealed_eval", "public_compare", "excluded")}


def validate_split_disjoint(split_path: Path) -> None:
    parts = split_sets(split_path)
    seen: dict[str, str] = {}
    overlap: list[str] = []
    for part, cases in parts.items():
        for case in cases:
            if case in seen:
                overlap.append(f"{case}:{seen[case]}+{part}")
            seen[case] = part
    if overlap:
        raise ChainError(f"split partitions overlap in {split_path}: {overlap[:8]}")


def assistant_turns(row: dict[str, Any], turns_per_episode: int) -> int:
    count = sum(
        1
        for msg in row.get("messages", [])
        if msg.get("role") == "assistant" and msg.get("supervise") is not False
    )
    return min(count, turns_per_episode) if turns_per_episode > 0 else count


def local_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def verify_evidence_admission(dataset: Path, manifest: Path) -> None:
    """Keep the new observation-support contract distinct from legacy readiness."""
    script = ROOT / "scripts/mcp_prepare_admitted_recipe.py"
    spec = importlib.util.spec_from_file_location("admitted_recipe_contract", script)
    if spec is None or spec.loader is None:
        raise ChainError("evidence admission validator is unavailable")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    try:
        validator.verify_admitted_dataset(dataset, manifest)
    except (validator.AdmittedRecipeError, ValueError, OSError, KeyError) as exc:
        raise ChainError(f"evidence admission verification failed: {exc}") from exc


def validate_dataset(stage: dict[str, Any]) -> dict[str, Any]:
    dataset = local_path(stage["dataset"])
    manifest = dataset.with_suffix(".manifest.json")
    split = ROOT / stage["split"]
    if not dataset.exists() or not manifest.exists():
        raise Waiting(f"missing recovered dataset or manifest for {stage['key']}: {dataset}")
    complete = Path(stage.get("complete_marker") or dataset.parent.parent / f"{stage['key']}.complete.json")
    if not complete.exists():
        raise Waiting(f"missing recovery complete marker for {stage['key']}: {complete}")
    validate_split_disjoint(split)
    manifest_data = read_json(manifest)
    complete_data = read_json(complete)
    actual_sha = sha256_file(dataset)
    if manifest_data.get("dataset_sha256") != actual_sha:
        raise ChainError(f"dataset hash mismatch for {dataset}")
    if complete_data.get("sha256") != actual_sha:
        raise ChainError(f"complete marker hash mismatch for {dataset}")
    expected_recovery = stage.get("recovery_expected") or {}
    for key, expected in expected_recovery.items():
        if complete_data.get(key) != expected:
            raise ChainError(f"complete marker {key} mismatch for {stage['key']}: expected {expected!r}, got {complete_data.get(key)!r}")
    if int(manifest_data.get("examples") or 0) <= 0 or int(manifest_data.get("cases") or 0) <= 0:
        raise ChainError(f"empty recovered dataset: {dataset}")
    readiness = manifest_data.get("readiness")
    if not isinstance(readiness, dict):
        raise ChainError(f"missing readiness contract in manifest: {manifest}")
    if readiness.get("infrastructure_incomplete", 0):
        raise ChainError(f"incomplete infrastructure in manifest: {dataset}")
    if readiness.get("require_judged") is not True:
        raise ChainError(f"dataset was not exported with judged-readiness: {dataset}")
    if stage.get("require_evidence_admission") is True:
        verify_evidence_admission(dataset, manifest)
    train = split_sets(split)["train"]
    forbidden = set().union(*(split_sets(split)[key] for key in ("validation", "sealed_eval", "public_compare")))
    cases: set[str] = set()
    turns = 0
    examples = 0
    with dataset.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            examples += 1
            row = json.loads(line)
            case_id = row.get("case_id")
            if case_id not in train or case_id in forbidden:
                raise ChainError(f"{dataset}:{line_no} uses non-train case {case_id}")
            cases.add(case_id)
            turns += assistant_turns(row, int(stage.get("turns_per_episode") or 0))
    if examples != int(manifest_data["examples"]) or examples != int(complete_data.get("examples")):
        raise ChainError(f"example count mismatch for {dataset}")
    if len(cases) != int(manifest_data.get("cases")):
        raise ChainError(f"manifest case count mismatch for {dataset}")
    if turns <= 0:
        raise ChainError(f"dataset has no supervised assistant turns: {dataset}")
    return {"dataset": str(dataset), "manifest": str(manifest), "sha256": actual_sha, "cases": len(cases), "turns": turns, "examples": examples, "complete_marker": str(complete)}


def render_stage_config(template: Path, stage: dict[str, Any], previous: dict[str, Any] | None, workspace: Path) -> dict[str, Any]:
    dataset_info = validate_dataset(stage)
    version = stage["version"]
    model_name = f"muse-glimmer-30b-mcp-{version}"
    config = yaml.safe_load(template.read_text(encoding="utf-8")) or {}
    # Epoch mode lets the trainer use the actual post-tokenization sampler size.
    # The legacy estimate precedes length filtering and must not force repetitions
    # in an admitted subset.
    budget_mode = stage.get("training_budget", "legacy_steps")
    if budget_mode not in {"legacy_steps", "one_epoch"}:
        raise ChainError(f"unknown training_budget: {budget_mode}")
    max_steps = -1 if budget_mode == "one_epoch" else max(50, math.ceil(dataset_info["turns"] / 4))
    config.update(
        {
            "name": model_name,
            "dataset": rel(Path(dataset_info["dataset"])),
            "dataset_sha256": dataset_info["sha256"],
            "expected_examples": dataset_info["examples"],
            "expected_cases": dataset_info["cases"],
            "output_dir": f"/home/work/models/rca-adapters/{model_name}",
            "epochs": 1,
            "max_steps": max_steps,
            "save_steps": int(stage.get("save_steps", 50)),
        }
    )
    config.pop("init_adapter", None)
    config.pop("turns_per_episode", None)
    if int(stage.get("turns_per_episode") or 0) > 0:
        config["turns_per_episode"] = int(stage["turns_per_episode"])
    if previous is not None:
        config["init_adapter"] = f"/home/work/models/rca-adapters/muse-glimmer-30b-mcp-{previous['version']}/adapter"
    if isinstance(config.get("wandb"), dict):
        config["wandb"]["version"] = f"mcp-{version}"
    header = stage.get("header", f"# recovered-chain stage {stage['key']}")
    out = workspace / "generated-configs" / f"{model_name}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header.rstrip() + "\n" + yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return {**stage, **dataset_info, "config_path": str(out), "max_steps": max_steps, "model_name": model_name, "run_id": f"mcp-{version}"}


def rel(path: Path) -> str:
    return str(path.resolve().relative_to(ROOT))


def iter_sync_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = (ROOT / raw).resolve()
        if path.is_dir():
            for item in sorted(path.rglob("*")):
                if item.is_file() and "__pycache__" not in item.parts:
                    files.append(item)
        elif path.is_file():
            files.append(path)
        else:
            raise ChainError(f"sync path missing: {raw}")
    return sorted(set(files))


def snapshot_secret(source: Path, runtime: Path) -> Path:
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(runtime, 0o700)
    target = runtime / "ktkey"
    if not target.exists():
        if not source.exists():
            raise ChainError(f"SSH key missing and no runtime snapshot exists: {source}")
        target.write_bytes(source.read_bytes())
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    return target


class Remote:
    def __init__(self, config: dict[str, Any], workspace: Path):
        ssh = config["remote"]["ssh"]
        key = snapshot_secret(Path(ssh["key_path"]), workspace / "runtime")
        self.remote_root = config["remote"].get("root", "/home/work/rca-model-lab-mcp")
        self.timeout = int(config["remote"].get("timeout_seconds", 60))
        self.base = [
            "ssh",
            "-q",
            "-i",
            str(key),
            "-p",
            str(ssh["port"]),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=20",
            f"{ssh['user']}@{ssh['host']}",
        ]

    def run(self, command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                self.base + [command],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise ChainError(f"remote command timed out after {self.timeout}s: {command[:160]}") from error
        if check and result.returncode:
            raise ChainError(f"remote command failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
        return result

    def sync(self, files: list[Path]) -> None:
        rels = [rel(path) for path in files]
        self.run(f"mkdir -p {shlex.quote(self.remote_root)}")
        sender = subprocess.Popen(["tar", "czf", "-", *rels], cwd=ROOT, stdout=subprocess.PIPE)
        receiver = subprocess.run(
            self.base + [f"tar xzf - -C {shlex.quote(self.remote_root)}"],
            stdin=sender.stdout,
            text=False,
            capture_output=True,
            check=False,
        )
        if sender.stdout:
            sender.stdout.close()
        sender_code = sender.wait()
        if sender_code or receiver.returncode:
            raise ChainError(f"sync failed: tar={sender_code} ssh={receiver.returncode} {receiver.stderr.decode(errors='ignore')}")
        self.verify_hashes(files)

    def verify_hashes(self, files: list[Path]) -> None:
        for path in files:
            remote = f"{self.remote_root}/{rel(path)}"
            result = self.run(f"sha256sum {shlex.quote(remote)}")
            if result.stdout.split()[0] != sha256_file(path):
                raise ChainError(f"remote hash mismatch: {rel(path)}")


def preflight_remote(remote: Remote, config: dict[str, Any]) -> dict[str, Any]:
    launcher = config["remote"].get("launcher", "/home/work/run-mcp-train.sh")
    probe = remote.run(
        " && ".join(
            [
                f"test -x {shlex.quote(launcher)}",
                f"sha256sum {shlex.quote(launcher)}",
                f"sed -n '1,120p' {shlex.quote(launcher)} | sha256sum",
                "nvidia-smi --query-gpu=index,memory.used --format=csv,noheader,nounits",
            ]
        )
    )
    lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
    return {"launcher_sha256": lines[0].split()[0], "launcher_head_sha256": lines[1].split()[0], "gpu": lines[2:]}


def remote_stage_complete(remote: Remote, stage: dict[str, Any]) -> bool:
    exit_file = f"{remote.remote_root}/training-{stage['run_id']}.exitcode"
    adapter = f"/home/work/models/rca-adapters/{stage['model_name']}/adapter/adapter_model.safetensors"
    result = remote.run(
        f"test -f {shlex.quote(exit_file)} && test \"$(cat {shlex.quote(exit_file)})\" = 0 && test -f {shlex.quote(adapter)}",
        check=False,
    )
    return result.returncode == 0


def stage_identity(stage: dict[str, Any]) -> dict[str, str]:
    return {
        "dataset_sha256": stage["sha256"],
        "config_sha256": sha256_file(Path(stage["config_path"])),
    }


def record_matches_stage(record: dict[str, Any], stage: dict[str, Any]) -> bool:
    identity = stage_identity(stage)
    return (
        record.get("version") == stage["version"]
        and record.get("run_id") == stage["run_id"]
        and record.get("model_name") == stage["model_name"]
        and record.get("dataset_sha256") == identity["dataset_sha256"]
        and record.get("config_sha256") == identity["config_sha256"]
    )


def remote_stage_output_exists(remote: Remote, stage: dict[str, Any]) -> bool:
    output_dir = f"/home/work/models/rca-adapters/{stage['model_name']}"
    return remote.run(f"test -e {shlex.quote(output_dir)}", check=False).returncode == 0


def stage_record_path(workspace: Path, stage: dict[str, Any]) -> Path:
    return workspace / "stages" / f"{stage['key']}.json"


def write_stage_record(workspace: Path, stage: dict[str, Any], phase: str, **extra: Any) -> None:
    identity = stage_identity(stage)
    atomic_json(
        stage_record_path(workspace, stage),
        {
            "stage": stage["key"],
            "version": stage["version"],
            "run_id": stage["run_id"],
            "model_name": stage["model_name"],
            "phase": phase,
            "utc": utc_now(),
            **identity,
            **extra,
        },
    )


def wait_gpu(remote: Remote, gpu_index: int, workspace: Path, poll_seconds: int) -> None:
    while True:
        used = int(remote.run(f"nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -i {gpu_index}").stdout.strip())
        if used < 2000:
            return
        state(workspace, "waiting_for_gpu", gpu_index=gpu_index, memory_used_mb=used)
        time.sleep(poll_seconds)


def wait_training_slot(remote: Remote, workspace: Path, poll_seconds: int) -> None:
    while True:
        result = remote.run(
            f"cd {shlex.quote(remote.remote_root)} && flock -n training.lock -c true",
            check=False,
        )
        if result.returncode == 0:
            return
        state(workspace, "waiting_for_training_lock")
        time.sleep(poll_seconds)


def default_pid_file(remote: Remote, stage: dict[str, Any]) -> str:
    return f"{remote.remote_root}/training-{stage['run_id']}.pid"


def read_remote_pid(remote: Remote, pid_file: str) -> int | None:
    result = remote.run(f"test -f {shlex.quote(pid_file)} && cat {shlex.quote(pid_file)}", check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return int(result.stdout.strip())
    except ValueError as error:
        raise ChainError(f"invalid remote pid in {pid_file}: {result.stdout.strip()!r}") from error


def wait_remote_exit(remote: Remote, stage: dict[str, Any], workspace: Path, poll_seconds: int) -> None:
    run_id = stage["run_id"]
    exit_file = f"{remote.remote_root}/training-{run_id}.exitcode"
    while True:
        result = remote.run(f"test -f {shlex.quote(exit_file)} && cat {shlex.quote(exit_file)}", check=False)
        if result.returncode == 0:
            code = result.stdout.strip()
            if code != "0":
                raise ChainError(f"{run_id} training exited {code}")
            return
        record = stage_record_path(workspace, stage)
        if record.exists():
            payload = read_json(record)
            remote_pid = payload.get("remote_pid")
            if remote_pid:
                live = remote.run(f"kill -0 {int(remote_pid)}", check=False)
                if live.returncode != 0:
                    raise ChainError(f"{run_id} launcher process vanished without exitcode")
        state(workspace, "waiting_for_training", run_id=run_id)
        time.sleep(poll_seconds)


def ensure_stage_complete(remote: Remote, stage: dict[str, Any], workspace: Path, poll_seconds: int) -> None:
    record = stage_record_path(workspace, stage)
    payload = read_json(record) if record.exists() else None
    if payload is not None and not record_matches_stage(payload, stage):
        raise ChainError(f"stage record identity mismatch: {stage['key']}")
    if remote_stage_complete(remote, stage):
        if payload is None:
            raise ChainError(f"remote stage complete but no local identity record exists: {stage['key']}")
        write_stage_record(workspace, stage, "complete", remote_reconciled=True)
        return
    if payload is not None and payload.get("phase") in {"launching", "launched"}:
        if not payload.get("remote_pid"):
            pid_file = str(payload.get("pid_file") or default_pid_file(remote, stage))
            remote_pid = read_remote_pid(remote, pid_file)
            if remote_pid is None:
                raise ChainError(f"{stage['run_id']} launch intent exists but no remote pid or exitcode")
            write_stage_record(
                workspace,
                stage,
                "launched",
                remote_config=payload.get("remote_config"),
                remote_pid=remote_pid,
                pid_file=pid_file,
                launch_log=payload.get("launch_log"),
                remote_reconciled=True,
            )
        wait_remote_exit(remote, stage, workspace, poll_seconds)
        if not remote_stage_complete(remote, stage):
            raise ChainError(f"{stage['run_id']} exit0 but final adapter missing")
        write_stage_record(workspace, stage, "complete")
        return
    raise ChainError(f"stage not launched and not complete: {stage['key']}")


def wait_eval_idle(config: dict[str, Any], workspace: Path, poll_seconds: int) -> None:
    guard = config.get("eval_guard") or {}
    while True:
        blockers: list[str] = []
        for item in guard.get("state_files") or []:
            path = Path(item["path"])
            if not path.exists():
                continue
            phase = read_json(path).get("phase")
            if phase not in set(item.get("allowed_phases") or []):
                blockers.append(f"{path}:{phase}")
        for pattern in guard.get("process_absent") or []:
            result = subprocess.run(["pgrep", "-af", pattern], text=True, capture_output=True, check=False)
            if result.returncode == 0:
                blockers.append(f"process:{pattern}")
        if not blockers:
            return
        state(workspace, "waiting_for_existing_eval", blockers=blockers[:8])
        time.sleep(poll_seconds)


def http_post(base: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode() or "{}")


def http_models(base: str) -> set[str]:
    with urllib.request.urlopen(base.rstrip("/") + "/models", timeout=120) as response:
        return {item["id"] for item in json.loads(response.read().decode())["data"]}


def load_adapter(remote: Remote, stage: dict[str, Any], config: dict[str, Any], workspace: Path) -> str:
    wait_eval_idle(config, workspace, int(config.get("poll_seconds", 300)))
    name = f"{stage['run_id']}-final"
    adapter = f"/home/work/models/rca-adapters/{stage['model_name']}/adapter"
    lm = adapter.replace("/rca-adapters/", "/rca-adapters-lm/")
    remote.run(f"test -f {shlex.quote(lm)}/adapter_model.safetensors || /home/work/rca-model-lab/.venv/bin/python /home/work/kt_lora_strip_vision.py {shlex.quote(adapter)}")
    endpoint = config["validation"]["endpoint"]
    if name not in http_models(endpoint):
        http_post(endpoint, "/load_lora_adapter", {"lora_name": name, "lora_path": lm})
    if name not in http_models(endpoint):
        raise ChainError(f"adapter load did not register model: {name}")
    return name


def run_validation(model: str, run_name: str, config: dict[str, Any], workspace: Path) -> Path:
    validation = config["validation"]
    root = ROOT / validation.get("out_root", "outputs/mcp-eval") / run_name
    if validation_complete(root, validation):
        return root
    args = [
        sys.executable,
        "scripts/mcp_case_run.py",
        "--runner",
        "student",
        "--run-name",
        run_name,
        "--split",
        validation["split"],
        "--partition",
        "validation",
        "--project-prefix",
        run_name[:10].replace("-", ""),
        "--endpoint",
        validation["endpoint"],
        "--model",
        model,
        "--runs",
        str(validation["runs"]),
        "--max-turns",
        str(validation.get("max_turns", 50)),
    ]
    log = workspace / f"{run_name}.log"
    with log.open("a", encoding="utf-8") as handle:
        result = subprocess.run(args, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise ChainError(f"validation failed for {model}: {run_name}")
    validate_validation_root(root, validation)
    return root


def validation_cases(validation: dict[str, Any]) -> set[str]:
    split = split_sets(ROOT / validation["split"])
    return split["validation"]


def validate_validation_root(root: Path, validation: dict[str, Any]) -> None:
    scores = [read_json(path) for path in root.glob("case-*/run*/score.json")]
    expected_scores = int(validation["expected_scores"])
    expected_cases = validation_cases(validation)
    if len(scores) != expected_scores:
        raise ChainError(f"{root}: expected {expected_scores} scores, got {len(scores)}")
    if any(score.get("unscored") for score in scores):
        raise ChainError(f"{root}: contains unscored runs")
    actual_cases = {score.get("case_id") for score in scores}
    if actual_cases != expected_cases:
        raise ChainError(f"{root}: validation case mismatch: {sorted(actual_cases ^ expected_cases)}")


def validation_complete(root: Path, validation: dict[str, Any]) -> bool:
    try:
        validate_validation_root(root, validation)
        return True
    except ChainError:
        return False


def judge_and_report(roots: list[Path], config: dict[str, Any], workspace: Path) -> int:
    judge = config.get("mechanism_judge") or {}
    if not judge.get("enabled", True):
        return 0
    workspace.mkdir(parents=True, exist_ok=True)
    args = [
        sys.executable,
        "scripts/mcp_judge_runs.py",
        *[str(root) for root in roots],
        "--provider",
        judge["provider"],
        "--model",
        judge["model"],
        "--rubric",
        judge.get("rubric", "v2"),
        "--workers",
        str(judge.get("workers", 2)),
    ]
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=False)
    (workspace / "mechanism_judge.stdout").write_text(result.stdout, encoding="utf-8")
    (workspace / "mechanism_judge.stderr").write_text(result.stderr, encoding="utf-8")
    if result.returncode:
        try:
            counts = json.loads(result.stdout.splitlines()[-1])
        except (IndexError, ValueError):
            raise ChainError(f"mechanism judge failed without JSON summary: {result.stderr.strip()}")
        if counts.get("session-limit") or counts.get("not-started"):
            state(workspace, "mechanism_judge_resumable", counts=counts, provider=judge.get("provider"), model=judge["model"])
            return 3
        raise ChainError(f"mechanism judge failed: {counts}")
    report_args = [
        sys.executable,
        "scripts/mcp_eval_report.py",
        *[str(root) for root in roots],
        "--provider",
        judge["provider"],
        "--model",
        judge["model"],
        "--rubric",
        judge.get("rubric", "v2"),
    ]
    report = subprocess.check_output(report_args, cwd=ROOT, text=True)
    (workspace / "validation_report.json").write_text(report, encoding="utf-8")
    return 0


def run_chain(config: dict[str, Any], workspace: Path, execute: bool) -> int:
    workspace.mkdir(parents=True, exist_ok=True)
    frozen_config = workspace / "chain-config.json"
    if frozen_config.exists():
        if read_json(frozen_config) != config:
            raise ChainError("continuation config changed for this workspace; use a new workspace")
    else:
        atomic_json(frozen_config, config)
    poll_seconds = int(config.get("poll_seconds", 300))
    state(workspace, "preflight")
    remote = None
    if execute:
        remote = Remote(config, workspace)
        state(workspace, "remote_preflight", **preflight_remote(remote, config))
    generated: list[dict[str, Any]] = []
    previous_stage = None
    if not execute:
        try:
            for stage in config["stages"]:
                rendered = render_stage_config(ROOT / config["template"], stage, previous_stage, workspace)
                generated.append(rendered)
                previous_stage = rendered
        except Waiting as wait:
            state(workspace, "waiting_for_recovered_data", stage=stage.get("key"), reason=str(wait))
            return 3
        atomic_json(workspace / "generated-stages.json", {"stages": generated})
        state(workspace, "ready_to_execute", stages=[stage["version"] for stage in generated])
        return 0

    assert remote is not None
    previous = None
    for raw_stage in config["stages"]:
        if previous is not None:
            ensure_stage_complete(remote, previous, workspace, poll_seconds)
        while True:
            try:
                stage = render_stage_config(ROOT / config["template"], raw_stage, previous, workspace)
                break
            except Waiting as wait:
                state(workspace, "waiting_for_recovered_data", stage=raw_stage.get("key"), reason=str(wait))
                time.sleep(poll_seconds)
        generated.append(stage)
        atomic_json(workspace / "generated-stages.json", {"stages": generated})
        record = stage_record_path(workspace, stage)
        if remote_stage_complete(remote, stage):
            if not record.exists():
                raise ChainError(f"remote stage complete but no local identity record exists: {stage['key']}")
            payload = read_json(record)
            if not record_matches_stage(payload, stage):
                raise ChainError(f"remote complete stage identity mismatch: {stage['key']}")
            write_stage_record(workspace, stage, "complete", remote_reconciled=True)
            previous = stage
            continue
        if record.exists():
            payload = read_json(record)
            if not record_matches_stage(payload, stage):
                raise ChainError(f"stage record identity mismatch: {stage['key']}")
            if payload.get("phase") in {"launching", "launched"}:
                ensure_stage_complete(remote, stage, workspace, poll_seconds)
                previous = stage
                continue
            raise ChainError(f"local stage record exists but remote is not complete: {stage['key']}:{payload.get('phase')}")
        wait_eval_idle(config, workspace, poll_seconds)
        common_files = iter_sync_files(config["sync"]["paths"])
        files = common_files + [Path(stage["dataset"]), Path(stage["manifest"]), Path(stage["config_path"])]
        state(workspace, "sync", stage=stage["key"], files=len(files))
        remote.sync(files)
        remote.run(f"cd {shlex.quote(remote.remote_root)} && PYTHONPATH=src /home/work/rca-model-lab/.venv/bin/python -c 'from rca_lab.train import mcp_sft; print(\"train import ok\")'")
        if remote_stage_output_exists(remote, stage):
            raise ChainError(f"refusing to overwrite existing incomplete output for {stage['run_id']}")
        wait_training_slot(remote, workspace, poll_seconds)
        wait_gpu(remote, int(config["remote"].get("gpu_index", 1)), workspace, poll_seconds)
        state(workspace, "launch_training", stage=stage["key"], run_id=stage["run_id"])
        remote_config = f"{remote.remote_root}/{rel(Path(stage['config_path']))}"
        launcher = config["remote"].get("launcher", "/home/work/run-mcp-train.sh")
        exit_file = f"{remote.remote_root}/training-{stage['run_id']}.exitcode"
        launch_log = f"{remote.remote_root}/train-{stage['run_id']}.launch.log"
        pid_file = f"{remote.remote_root}/training-{stage['run_id']}.pid"
        inner = (
            "cd /home/work; "
            f"bash {shlex.quote(launcher)} {shlex.quote(remote_config)} {shlex.quote(stage['run_id'])}; "
            "code=$?; "
            f"test -f {shlex.quote(exit_file)} || printf '%s\\n' \"$code\" > {shlex.quote(exit_file)}; "
            "exit \"$code\""
        )
        wrapper = (
            "set +e; "
            f"rm -f {shlex.quote(exit_file)} {shlex.quote(pid_file)}; "
            f"nohup bash -c {shlex.quote(inner)} > {shlex.quote(launch_log)} 2>&1 < /dev/null & "
            f"echo $! > {shlex.quote(pid_file)}"
        )
        write_stage_record(workspace, stage, "launching", remote_config=remote_config, pid_file=pid_file, launch_log=launch_log)
        remote.run(wrapper)
        remote_pid = read_remote_pid(remote, pid_file)
        if remote_pid is None:
            raise ChainError(f"{stage['run_id']} launcher did not write pid file")
        write_stage_record(workspace, stage, "launched", remote_config=remote_config, remote_pid=remote_pid, pid_file=pid_file, launch_log=launch_log)
        ensure_stage_complete(remote, stage, workspace, poll_seconds)
        previous = stage
    if previous is not None:
        ensure_stage_complete(remote, previous, workspace, poll_seconds)

    roots: list[Path] = []
    comparison_roots = []
    for raw_root in config["validation"].get("comparison_roots", []):
        root = ROOT / raw_root
        if root.is_dir():
            comparison_roots.append(root)
        else:
            state(workspace, "comparison_root_missing", root=str(root))
    for item in config["validation"]["models"]:
        if item["kind"] == "base":
            model = item["model"]
        else:
            stage = next(stage for stage in generated if stage["key"] == item["stage"])
            model = load_adapter(remote, stage, config, workspace)
        run_name = item["run_name"]
        state(workspace, "validation", model=model, run_name=run_name)
        roots.append(run_validation(model, run_name, config, workspace))
    code = judge_and_report(comparison_roots + roots, config, workspace)
    if code == 0:
        state(workspace, "complete", validation_roots=[str(root) for root in roots])
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--workspace", type=Path, default=ROOT / "outputs/recovered-chain-20261007")
    parser.add_argument("--recovery-workspace", type=Path, help="override stage datasets to <workspace>/datasets/<key>.jsonl")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    config = load_config(args.config, args.recovery_workspace.resolve() if args.recovery_workspace else None)
    workspace.mkdir(parents=True, exist_ok=True)
    with (workspace / "continue.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            return run_chain(config, workspace, execute=args.execute)
        except Exception as error:
            state(workspace, "failed", error=str(error))
            raise


if __name__ == "__main__":
    raise SystemExit(main())
