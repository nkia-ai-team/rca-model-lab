"""Run the teacher or the student on captured cases over the rca-mcp tool surface.

Per case: restore an isolated DB set (tools/rca-mcp/replay/restore.sh), build the observed-alarm
seed, pass it through the blind filter, run N agent episodes, score each against the golden
contract, tear the DB set down. Sealed cases are refused unless --allow-sealed is given.

Example:
  uv run python scripts/mcp_case_run.py --runner student --run-name base-val \
      --split configs/teacher/v45-family-split-val.yaml --partition validation \
      --endpoint http://127.0.0.1:8100/v1 --model muse-glimmer-base --runs 3
  uv run python scripts/mcp_case_run.py --runner teacher --run-name teacher-v1 \
      --split configs/teacher/v45-family-split-val.yaml --partition train --claude-model claude-opus-5-5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rca_lab.mcp.claude_cli import SessionLimitError
from rca_lab.mcp.client import MCPClient
from rca_lab.mcp.golden import candidates_from_result, load_contract
from rca_lab.mcp.hint_ladder import (
    NO_PICK_TEXT,
    attempt_digest,
    claude_hint,
    forbidden_root_names,
    load_ladder_hints,
    rung_hint,
    scenario_reference,
    target_names,
)
from rca_lab.mcp.rl_reward import (
    conclusion_text,
    judge_cache_valid,
    judge_mechanism,
    reference_cause,
    reference_version,
)
from rca_lab.mcp.score import score_result
from rca_lab.mcp.seed import build_observed_seed
from rca_lab.mcp.student import StudentConfig, run_student
from rca_lab.mcp.teacher import TeacherConfig, run_teacher

RESTORE = ROOT / "tools/rca-mcp/replay/restore.sh"
COMPOSE = ROOT / "tools/rca-mcp/replay/eval-dbs.compose.yml"


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {message}", flush=True)


def select_cases(args: argparse.Namespace) -> tuple[list[str], set[str]]:
    split = yaml.safe_load(Path(args.split).read_text(encoding="utf-8")) if args.split else {}
    sealed = set(split.get("sealed_eval", []))
    if args.case:
        cases = list(args.case)
    else:
        cases = list(split.get(args.partition, []))
    if not cases:
        raise SystemExit("no cases selected")
    blocked = [case for case in cases if case in sealed]
    if blocked and not args.allow_sealed:
        raise SystemExit(f"sealed cases refused (use --allow-sealed only for the final sealed evaluation): {blocked}")
    return cases, sealed


def restore_case(case_dir: Path, project: str, connections_path: Path, log_path: Path) -> dict[str, str]:
    if connections_path.exists():
        connections_path.unlink()
    env = {**os.environ, "LUCIDA_NEXT": os.environ.get("LUCIDA_NEXT", str(ROOT.parent / "lucida-next")), "RCA_CONNECTIONS_FILE": str(connections_path)}
    with log_path.open("w", encoding="utf-8") as sink:
        proc = subprocess.run(["bash", str(RESTORE), str(case_dir), project], env=env, stdout=sink, stderr=subprocess.STDOUT, text=True, check=False)
    if proc.returncode != 0 or not connections_path.exists():
        raise RuntimeError(f"restore failed for {case_dir.name} (exit {proc.returncode}); see {log_path}")
    return json.loads(connections_path.read_text(encoding="utf-8"))


def external_capture_env(case_dir: Path) -> dict[str, str]:
    """Converted external captures (data/sources.json present) tell rca-mcp which datasets were not collected
    and where their data starts (tools/availability.go); Polestar captures get nothing, so they are unchanged."""
    manifest = case_dir / "data" / "sources.json"
    if not manifest.exists():
        return {}
    env = {"RCA_CAPTURE_SOURCES": str(manifest.resolve())}
    meta = json.loads((case_dir / "meta.json").read_text(encoding="utf-8"))
    if meta.get("capture_start"):
        env["RCA_CAPTURE_START"] = str(meta["capture_start"])
    return env


def teardown(project: str) -> None:
    subprocess.run(["docker", "compose", "-f", str(COMPOSE), "-p", project, "down", "-v"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def blind_seed(mcp_binary: Path, seed: dict[str, Any]) -> dict[str, Any]:
    proc = subprocess.run([str(mcp_binary), "-sanitize-stdin"], input=json.dumps(seed, ensure_ascii=False), capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def load_hints(path: str | None) -> dict[str, str]:
    """YAML {case_id: hint text}. Hints are ladder guidance for the live prompt only; they are
    hashed into score.json (hint_sha256) so exports can tell hinted episodes apart, but never
    written into trajectories."""
    if not path:
        return {}
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {str(case_id): str(text) for case_id, text in raw.items() if str(text).strip()}


def selected_tool_surface(args: argparse.Namespace) -> str:
    if args.runner == "student":
        return getattr(args, "student_tools", "harness")
    if args.runner == "teacher":
        return getattr(args, "teacher_tools", "harness")
    return "harness"


def raw_server_env(connections: dict[str, str], seed: dict[str, Any], mcp_binary: Path) -> dict[str, str]:
    return {
        **os.environ,
        **{key: connections[key] for key in ("RCA_PG_DSN", "RCA_CH_URL", "RCA_VM_URL") if key in connections},
        "RCA_MCP_BINARY": str(mcp_binary),
        "RCA_FIRST_EVENT": seed["time_window"]["first_event"],
        "RCA_LAST_EVENT": seed["time_window"]["last_event"],
        "PYTHONPATH": str(ROOT / "src"),
        "PATH": os.environ.get("PATH", ""),
    }


def student_mcp_command(args: argparse.Namespace, seed: dict[str, Any], mcp_binary: Path) -> list[str]:
    if getattr(args, "student_tools", "harness") == "raw":
        return [sys.executable, "-m", "rca_lab.mcp.raw_server"]
    return [str(mcp_binary), "-blind", "-first-event", seed["time_window"]["first_event"], "-last-event", seed["time_window"]["last_event"]]


def validate_observation_cache(out_root: Path, policy: str, tool_surface: str = "harness") -> None:
    """Do not relabel old scores as a new harness arm, even with --overwrite."""
    config_path = out_root / "config.json"
    if config_path.exists():
        old = json.loads(config_path.read_text(encoding="utf-8"))
        if old.get("observation_policy", "legacy") != policy:
            raise ValueError("observation policy differs from cached run; use a new --run-name")
        old_surface = old.get("student_tools" if old.get("runner") == "student" else "teacher_tools", "harness")
        if old_surface != tool_surface:
            raise ValueError("tool surface differs from cached run; use a new --run-name")
    for path in out_root.glob("case-*/run*/score.json"):
        old = json.loads(path.read_text(encoding="utf-8"))
        if old.get("observation_policy", "legacy") != policy:
            raise ValueError(f"observation policy differs from cached score {path}; use a new --run-name")
        if old.get("tool_surface", "harness") != tool_surface:
            raise ValueError(f"tool surface differs from cached score {path}; use a new --run-name")
        if policy != "legacy":
            runtime_path = path.parent / "observation-runtime.json"
            if not runtime_path.exists():
                raise ValueError("structured score has no runtime provenance; use a new --run-name")
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            expected_sources = {"src/rca_lab/mcp/student.py", "src/rca_lab/mcp/context.py", "src/rca_lab/mcp/observations.py"}
            hashes = runtime.get("source_sha256", {})
            if set(hashes) != expected_sources or any(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest for name, digest in hashes.items()):
                raise ValueError("structured runtime differs from cached score; use a new --run-name")


def run_case(args: argparse.Namespace, case_id: str, contract: dict[str, Any], out_root: Path, mcp_binary: Path) -> list[dict[str, Any]]:
    case_dir = Path(args.cases_root) / case_id
    hint = args.hints_by_case.get(case_id)
    if args.hints_by_case and hint is None and args.hinted_only:
        log(f"{case_id}: no hint in {args.hints} — skipped (--hinted-only)")
        return []
    case_out = out_root / case_id
    case_out.mkdir(parents=True, exist_ok=True)
    project = f"{args.project_prefix}-" + case_id.split("-v3-")[0].replace("case-", "")
    connections_path = case_out / "connections.json"
    if args.runner != "prepare" and not args.hint_ladder and not args.overwrite and all((case_out / f"run{index}" / "score.json").exists() for index in range(1, args.runs + 1)):
        log(f"{case_id}: all {args.runs} runs cached — no restore")
        return [json.loads((case_out / f"run{index}" / "score.json").read_text(encoding="utf-8")) for index in range(1, args.runs + 1)]
    if args.reuse_db and connections_path.exists():
        log(f"{case_id}: reusing restored DB set (project {project})")
        connections = json.loads(connections_path.read_text(encoding="utf-8"))
    else:
        log(f"{case_id}: restore (project {project})")
        connections = restore_case(case_dir, project, connections_path, case_out / "restore.log")
    connections = {**connections, **external_capture_env(case_dir)}
    if getattr(args, "mcp_nav_hints", False):
        connections["RCA_NAV_HINTS"] = "1"
    rows: list[dict[str, Any]] = []
    try:
        raw_seed = build_observed_seed(case_dir, max_alarms=args.max_alarms)
        seed = blind_seed(mcp_binary, raw_seed)
        (case_out / "seed.json").write_text(json.dumps(seed, ensure_ascii=False, indent=1), encoding="utf-8")
        expected = contract.get(case_id)
        if args.runner == "prepare":
            # Restore + seed only: an in-session teacher agent drives the tools through
            # scripts/mcp_tool_cli.py --episode <case_out>/run<N> afterwards.
            for run_index in range(1, args.runs + 1):
                run_dir = case_out / f"run{run_index}"
                run_dir.mkdir(exist_ok=True)
                for name in ("connections.json", "seed.json"):
                    (run_dir / name).write_text((case_out / name).read_text(encoding="utf-8"), encoding="utf-8")
            log(f"{case_id}: prepared {args.runs} episode dir(s) under {case_out} (DB kept)")
            args.keep_db = True
            return rows
        if args.hint_ladder:
            rows.extend(run_ladder(args, case_id, case_out, project, seed, connections, mcp_binary, expected))
            return rows
        for run_index in range(1, args.runs + 1):
            run_dir = case_out / f"run{run_index}"
            run_dir.mkdir(exist_ok=True)
            if (run_dir / "score.json").exists() and not args.overwrite:
                rows.append(json.loads((run_dir / "score.json").read_text(encoding="utf-8")))
                log(f"{case_id} run{run_index}: cached")
                continue
            score, _ = run_one(args, case_id, run_dir, run_index, seed, connections, mcp_binary, expected, hint)
            rows.append(score)
    finally:
        if not args.keep_db:
            teardown(project)
    return rows


def run_one(args: argparse.Namespace, case_id: str, run_dir: Path, run_index: int, seed: dict[str, Any], connections: dict[str, str], mcp_binary: Path,
            expected: Any, hint: str | None, *, hint_rung: int | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """One episode in an already restored capture; writes the run dir and returns (score, result)."""
    log(f"{case_id} run{run_index}: {args.runner} start")
    if args.runner == "student":
        config = StudentConfig(
            endpoint=args.endpoint,
            model=args.model,
            alias_targets=not args.no_alias,
            guided=not args.no_guided,
            reasoning_guard=not args.no_reasoning_guard,
            max_turns=args.max_turns,
            max_prompt_tokens=args.max_prompt_tokens,
            tool_result_max_chars=args.tool_result_max_chars,
            observation_policy=getattr(args, "observation_policy", "legacy"),
            observation_dir=run_dir / "observations",
            temperature=args.temperature,
            top_p=args.top_p,
            top_k=args.top_k,
            seed=args.seed + 1000 * run_index,
            hint=hint,
            tool_surface=getattr(args, "student_tools", "harness"),
        )
        command = student_mcp_command(args, seed, mcp_binary)
        if config.observation_policy != "legacy":
            source_paths = ["src/rca_lab/mcp/student.py", "src/rca_lab/mcp/context.py", "src/rca_lab/mcp/observations.py"]
            runtime = {
                "observation_policy": config.observation_policy,
                "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in source_paths},
                "mcp_binary_sha256": hashlib.sha256(mcp_binary.read_bytes()).hexdigest(),
                "model": config.model, "endpoint": config.endpoint,
                "seed": config.seed, "max_turns": config.max_turns,
                "max_prompt_tokens": config.max_prompt_tokens, "max_tokens": config.max_tokens,
                "tool_result_max_chars": config.tool_result_max_chars,
            }
            (run_dir / "observation-runtime.json").write_text(json.dumps(runtime, ensure_ascii=False, indent=2), encoding="utf-8")
        env = raw_server_env(connections, seed, mcp_binary) if config.tool_surface == "raw" else {**os.environ, **connections}
        with MCPClient(command, env, log_path=run_dir / "mcp.stderr") as mcp:
            episode = run_student(config, seed, mcp)
        (run_dir / "trajectory.jsonl").write_text("".join(json.dumps(message, ensure_ascii=False) + "\n" for message in episode.messages), encoding="utf-8")
        if episode.alias_map:
            (run_dir / "alias.json").write_text(json.dumps(episode.alias_map, ensure_ascii=False, indent=1), encoding="utf-8")
        # structured-v1 counts only refs delivered in a view/page; legacy keeps its old scoring.
        (run_dir / "observed_refs.json").write_text(json.dumps(sorted(episode.observed_refs), ensure_ascii=False), encoding="utf-8")
        extra = {"prompt_tokens_last": episode.prompt_tokens_last, "aliased": bool(episode.alias_map), "reasoning_guard": not args.no_reasoning_guard, "observation_policy": config.observation_policy,
                 **({"tool_surface": "raw"} if config.tool_surface == "raw" else {})}
    else:
        config = TeacherConfig(claude_model=args.claude_model, mcp_binary=mcp_binary, max_turns=args.max_turns, python_executable=sys.executable, hint=hint,
                               tool_surface=getattr(args, "teacher_tools", "harness"))
        episode = run_teacher(config, seed, connections, run_dir)
        (run_dir / "cli.stderr").write_text(episode.stderr_tail, encoding="utf-8")
        extra = {}
    if episode.result is not None:
        (run_dir / "result.json").write_text(json.dumps(episode.result, ensure_ascii=False, indent=1), encoding="utf-8")
    score = {
        "case_id": case_id,
        "run": run_index,
        "runner": args.runner,
        "model": args.model if args.runner == "student" else args.claude_model,
        "stop_reason": episode.stop_reason,
        "wall_seconds": round(episode.wall_seconds, 1),
        "hinted": hint is not None,
        **({"tool_surface": "raw"} if selected_tool_surface(args) == "raw" else {}),
        "hint_sha256": hashlib.sha256(hint.encode("utf-8")).hexdigest() if hint else None,
        **({"hint_rung": hint_rung} if hint_rung is not None else {}),
        **extra,
    }
    if expected is not None:
        score.update(score_result(expected, episode.result, turns=episode.turns, tool_calls=episode.tool_calls, tool_errors=episode.tool_errors, observed_refs=episode.observed_refs))
    else:
        score.update({"status": (episode.result or {}).get("status", "missing"), "turns": episode.turns, "tool_calls": episode.tool_calls, "tool_errors": episode.tool_errors, "unscored": True})
    (run_dir / "score.json").write_text(json.dumps(score, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"{case_id} run{run_index}: stop={episode.stop_reason} status={score.get('status')} root_f1={score.get('root_f1', 'n/a')} strict={score.get('strict_correct', 'n/a')} calls={episode.tool_calls} err={episode.tool_errors} {episode.wall_seconds:.0f}s")
    return score, episode.result


def run_ladder(args: argparse.Namespace, case_id: str, case_out: Path, project: str, seed: dict[str, Any], connections: dict[str, str], mcp_binary: Path, expected: Any) -> list[dict[str, Any]]:
    """Real-time hint ladder (student, train captures only): rung 0 blind, rung 1 automatic
    wrong-pick verdict, rungs 2-3 Claude-written direction / navigation hints (the written hint
    file is the rung-2 fallback), all in the same restore window, until a run names every root.
    See rca_lab.mcp.hint_ladder."""
    if expected is None:
        raise RuntimeError(f"{case_id}: --hint-ladder needs a golden entry in {args.contract}")
    rows: list[dict[str, Any]] = []
    results: list[dict[str, Any] | None] = []
    run_dirs: list[Path] = []
    ladder: list[dict[str, Any]] = []
    given: list[str] = []
    written = args.ladder_hints_by_case.get(case_id)
    tools: list[str] = []
    if args.ladder_writer == "claude":
        command = [str(mcp_binary), "-blind", "-first-event", seed["time_window"]["first_event"], "-last-event", seed["time_window"]["last_event"]]
        with MCPClient(command, {**os.environ, **connections}, log_path=case_out / "catalog.stderr") as mcp:
            tools = [f"{tool['name']}: {str(tool.get('description', '')).split('.')[0][:140]}" for tool in mcp.list_tools()]
    run_index = 0
    solved_at: int | None = None
    start_rung = 0
    if args.ladder_prior_root:
        # The on-policy rollouts of this capture (e.g. the RL iteration's K runs) are rung 0.
        prior = sorted((Path(args.ladder_prior_root) / case_id).glob("run*/score.json"), key=lambda path: int(path.parent.name[3:]))
        if not prior:
            raise RuntimeError(f"{case_id}: no prior runs under {args.ladder_prior_root}")
        step0: dict[str, Any] = {"rung": 0, "writer": "prior", "source": str(args.ladder_prior_root), "runs": []}
        for score_path in prior:
            prior_score = json.loads(score_path.read_text(encoding="utf-8"))
            prior_dir = score_path.parent
            result_path = prior_dir / "result.json"
            results.append(json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else None)
            run_dirs.append(prior_dir)
            entry = {"run": f"prior/{prior_dir.name}", "root_f1": prior_score.get("root_f1"), "status": prior_score.get("status"), "stop_reason": prior_score.get("stop_reason")}
            judged = prior_dir / "mechanism_judge.json"
            if judged.exists() and judge_cache_valid(json.loads(judged.read_text(encoding="utf-8")), case_id, model=args.ladder_writer_model, rubric="v2"):
                entry["mechanism"] = json.loads(judged.read_text(encoding="utf-8")).get("score")
            elif args.ladder_stop == "mechanism" and float(prior_score.get("root_f1") or 0.0) == 1.0 and results[-1]:
                entry["mechanism"] = _judge_run(args, case_id, prior_dir, results[-1])
            step0["runs"].append(entry)
        ladder.append(step0)
        if any(_ladder_solved(args, run) for run in step0["runs"]):
            _write_ladder(case_out, case_id, ladder, solved_at=0, done=True)
            log(f"{case_id}: prior runs already solve it — no ladder")
            return rows
        start_rung = 1
    for rung in range(start_rung, args.ladder_max_rung + 1):
        seen_ids = [c.target_id for r in results for c in candidates_from_result(r or {}) if c.variant == "target"]
        seen_ids += [root_id for root in expected.roots for root_id in root.target_ids]
        for run_dir in run_dirs:
            alias_path = run_dir / "alias.json"
            if alias_path.exists():
                seen_ids += list(json.loads(alias_path.read_text(encoding="utf-8")))
        names = target_names(project, seen_ids)
        step: dict[str, Any] = {"rung": rung, "writer": "none" if rung == 0 else "auto", "runs": []}
        if rung <= 1 or args.ladder_writer == "file":
            hint = rung_hint(rung, results=results, expected=expected, names=names, written=written)
            if rung >= 2:
                step["writer"] = "file"
        else:
            exclusion = rung_hint(1, results=results, expected=expected, names=names, written=None)
            first_latest = len(run_dirs) - args.runs  # tool-call sequences only for the latest rung's runs
            attempts = [attempt_digest(d, r, names, with_calls=i >= first_latest) for i, (d, r) in enumerate(zip(run_dirs, results))]
            text, audit = claude_hint(rung=rung, reference=scenario_reference(Path(args.cases_root) / case_id), forbidden=forbidden_root_names(expected, names),
                                      attempts=attempts, given=list(given), tools=tools, model=args.ladder_writer_model)
            step["writer_log"] = audit
            if text:
                hint, step["writer"] = f"{exclusion}\n{text}", "claude"
            elif rung == 2 and written:
                hint, step["writer"] = f"{exclusion}\n{written}", "file-fallback"
            else:
                hint = None
        if rung > 0 and hint is None:
            log(f"{case_id}: ladder ends before rung {rung} (no hint available)")
            break
        if rung == 1 and hint == NO_PICK_TEXT and args.ladder_max_rung >= 2 and (args.ladder_writer == "claude" or written):
            log(f"{case_id}: rung 1 skipped (no picks to reject)")
            continue
        step["hint"] = hint
        if hint:
            given.append(hint)
        _write_ladder(case_out, case_id, [*ladder, step], solved_at=None, done=False)  # hint visible before its runs finish
        for _ in range(args.runs):
            run_index += 1
            run_dir = case_out / f"run{run_index}"
            run_dir.mkdir(exist_ok=True)
            score, result = run_one(args, case_id, run_dir, run_index, seed, connections, mcp_binary, expected, hint, hint_rung=rung)
            rows.append(score)
            results.append(result)
            run_dirs.append(run_dir)
            entry = {"run": run_index, "root_f1": score.get("root_f1"), "status": score.get("status"), "stop_reason": score.get("stop_reason")}
            if args.ladder_stop == "mechanism" and float(score.get("root_f1") or 0.0) == 1.0 and result:
                entry["mechanism"] = _judge_run(args, case_id, run_dir, result)
            step["runs"].append(entry)
        ladder.append(step)
        _write_ladder(case_out, case_id, ladder, solved_at=None, done=False)
        if any(_ladder_solved(args, run) for run in step["runs"]):
            solved_at = rung
            break
    _write_ladder(case_out, case_id, ladder, solved_at=solved_at, done=True)
    log(f"{case_id}: ladder solved at rung {solved_at}" if solved_at is not None else f"{case_id}: ladder exhausted without a full hit")
    return rows


def _ladder_solved(args: argparse.Namespace, run: dict[str, Any]) -> bool:
    """Target stop: every root named. Mechanism stop (default): also the mechanism judge's full mark,
    so a hint that only steers the student onto the right component does not end the ladder."""
    if float(run.get("root_f1") or 0.0) != 1.0:
        return False
    return args.ladder_stop == "target" or run.get("mechanism") == 1.0


def _judge_run(args: argparse.Namespace, case_id: str, run_dir: Path, result: dict[str, Any]) -> float | None:
    """Mechanism judge (same prompt/model contract as the RL builder's cache, so it can be reused there)."""
    try:
        value, detail = judge_mechanism(reference_cause(Path(args.cases_root) / case_id), conclusion_text(result), model=args.ladder_writer_model, rubric="v2")
    except SessionLimitError as exc:
        log(f"{case_id} {run_dir.name}: mechanism judge hit the session limit ({exc}) — not counted as solved")
        return None
    if value is not None:
        payload = {"score": value, "detail": detail, "model": args.ladder_writer_model, "rubric": "v2", "reference": reference_version(case_id)}
        (run_dir / "mechanism_judge.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"{case_id} {run_dir.name}: mechanism judge = {value}")
    return value


def _write_ladder(case_out: Path, case_id: str, steps: list[dict[str, Any]], *, solved_at: int | None, done: bool) -> None:
    payload = {"case_id": case_id, "solved_at_rung": solved_at, "done": done, "steps": steps}
    (case_out / "ladder.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def write_summary(out_root: Path, rows: list[dict[str, Any]]) -> None:
    scored = [row for row in rows if not row.get("unscored")]
    summary = {
        "runs": len(rows),
        "scored_runs": len(scored),
        "strict_correct_runs": sum(bool(row.get("strict_correct")) for row in scored),
        "mean_root_f1": (sum(row.get("root_f1", 0.0) for row in scored) / len(scored)) if scored else 0.0,
        "mean_reward": (sum(row.get("reward", 0.0) for row in scored) / len(scored)) if scored else 0.0,
        "mean_tool_calls": (sum(row.get("tool_calls", 0) for row in rows) / len(rows)) if rows else 0.0,
        "stop_reasons": {reason: sum(row.get("stop_reason") == reason for row in rows) for reason in sorted({row.get("stop_reason") for row in rows})},
        "rows": rows,
    }
    (out_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = ["| case | run | stop | status | root_f1 | strict | calls | err | s |", "|---|---|---|---|---|---|---|---|---|"]
    lines += [f"| {row['case_id']} | {row['run']} | {row['stop_reason']} | {row.get('status')} | {row.get('root_f1', '-')} | {row.get('strict_correct', '-')} | {row.get('tool_calls')} | {row.get('tool_errors')} | {row.get('wall_seconds')} |" for row in rows]
    lines.append(f"\nstrict {summary['strict_correct_runs']}/{summary['scored_runs']}  mean root_f1 {summary['mean_root_f1']:.3f}  mean reward {summary['mean_reward']:.3f}  mean calls {summary['mean_tool_calls']:.1f}")
    (out_root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runner", choices=("student", "teacher", "prepare"), required=True)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--split", default="configs/teacher/v45-family-split-val.yaml")
    parser.add_argument("--partition", choices=("train", "validation", "sealed_eval"), default="validation")
    parser.add_argument("--case", action="append", help="explicit case id(s); overrides --partition")
    parser.add_argument("--allow-sealed", action="store_true")
    parser.add_argument("--contract", default="configs/eval/train-family-v2.yaml")
    parser.add_argument("--cases-root", default="/data/eval-cases")
    parser.add_argument("--out-root", default="outputs/mcp-eval")
    parser.add_argument("--mcp-binary", default=os.environ.get("RCA_MCP_BINARY", ""))
    parser.add_argument("--teacher-tools", choices=("harness", "raw"), default="harness", help="raw: harness ablation — the teacher gets generic SQL/PromQL access (rca_lab.mcp.raw_server) instead of the rca-mcp tools")
    parser.add_argument("--student-tools", choices=("harness", "raw"), default="harness", help="raw: harness ablation — the student gets generic SQL/PromQL access (rca_lab.mcp.raw_server) instead of the rca-mcp tools")
    parser.add_argument("--mcp-nav-hints", action="store_true", help="rca-mcp adds query-path navigation to no-data envelopes (RCA_NAV_HINTS=1); off keeps responses byte-identical")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--max-alarms", type=int, default=50)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--project-prefix", default="mcp", help="docker compose project prefix; use a distinct prefix per concurrent runner")
    parser.add_argument("--keep-db", action="store_true", help="leave the isolated DB set running after the case")
    parser.add_argument("--reuse-db", action="store_true", help="skip restore when the run dir already has connections.json (iteration only)")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8100/v1")
    parser.add_argument("--model", default="muse-glimmer-base")
    parser.add_argument("--no-alias", action="store_true", help="student sees raw UUIDs instead of T<n> handles")
    parser.add_argument("--no-reasoning-guard", action="store_true", help="pre-2026-09-29 grammar: reasoning may contain ATEM text (stop-string collision); only for parity with older runs")
    parser.add_argument("--no-guided", action="store_true", help="do not enforce the Muse reasoning→ATEM grammar (structural tag)")
    parser.add_argument("--max-prompt-tokens", type=int, default=40_000)
    parser.add_argument("--tool-result-max-chars", type=int, default=6_000)
    parser.add_argument("--observation-policy", choices=("legacy", "structured-v1"), default="legacy", help="structured-v1: persist full blind tool responses and expose bounded views with read_observation")
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--top-k", type=int, default=64)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--claude-model", default="claude-opus-5-5", help="teacher model (claude-opus-5-5 since 2026-09-29; teacher-v1..v3hx data was collected with claude-opus-5)")
    parser.add_argument("--hints", default=None, help="YAML {case_id: guidance}; live prompt only, never stored")
    parser.add_argument("--hinted-only", action="store_true", help="with --hints: skip cases that have no hint")
    parser.add_argument("--hint-ladder", action="store_true", help="student or teacher, train captures: blind runs (or --ladder-prior-root), then hinted rungs in the same restore until a full hit")
    parser.add_argument("--ladder-hints", action="append", help="YAML {case_id: written ladder hint} for rung 2; earlier files win")
    parser.add_argument("--ladder-max-rung", type=int, default=3)
    parser.add_argument("--ladder-writer", choices=("claude", "file"), default="claude", help="who writes rungs 2-3: Claude from the student's failed attempts (file = written hints, rung 2 only)")
    parser.add_argument("--ladder-writer-model", default="claude-opus-5-5")
    parser.add_argument("--ladder-prior-root", default=None, help="use <root>/<case>/run*/ (e.g. the RL iteration's rollouts) as rung 0 and start at rung 1")
    parser.add_argument("--ladder-stop", choices=("mechanism", "target"), default="mechanism", help="mechanism: a rung counts as solved only with root_f1 1 and mechanism judge 1 (v2)")
    args = parser.parse_args()
    if args.observation_policy != "legacy" and args.runner != "student":
        parser.error("--observation-policy structured-v1 currently supports only the student runner")
    args.hints_by_case = load_hints(args.hints)
    args.ladder_hints_by_case = load_ladder_hints(args.ladder_hints)
    if args.hint_ladder and (args.runner == "prepare" or args.hints):
        raise SystemExit("--hint-ladder is for the student or teacher runner and replaces --hints")

    mcp_binary = Path(args.mcp_binary).resolve() if args.mcp_binary else ROOT / "tools/rca-mcp/bin/rca-mcp"
    if not mcp_binary.exists():
        raise SystemExit(f"rca-mcp binary not found: {mcp_binary} (build: cd tools/rca-mcp && go build -buildvcs=false -o bin/rca-mcp ./cmd/rca-mcp)")
    cases, _ = select_cases(args)
    if args.hint_ladder:
        train = set((yaml.safe_load(Path(args.split).read_text(encoding="utf-8")) or {}).get("train", []))
        outside = [case for case in cases if case not in train]
        if outside:
            raise SystemExit(f"--hint-ladder is limited to train captures (hints must never touch validation or sealed): {outside}")
    contract = load_contract(Path(args.contract))
    out_root = Path(args.out_root) / args.run_name
    validate_observation_cache(out_root, args.observation_policy, selected_tool_surface(args))
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "config.json").write_text(json.dumps({**vars(args), "mcp_binary": str(mcp_binary), "cases": cases}, ensure_ascii=False, indent=1), encoding="utf-8")
    rows: list[dict[str, Any]] = []
    for case_id in cases:
        try:
            rows.extend(run_case(args, case_id, contract, out_root, mcp_binary))
        except Exception as exc:  # noqa: BLE001 — one broken case must not stop the batch
            log(f"{case_id}: FAILED {exc}")
            rows.append({"case_id": case_id, "run": 0, "runner": args.runner, "stop_reason": f"case_error: {exc}"[:200], "unscored": True})
        write_summary(out_root, rows)
    log(f"done: {out_root}/summary.md")


if __name__ == "__main__":
    main()
