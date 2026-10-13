"""Mechanism-judge student (or teacher) runs so evaluations can report strict accuracy (user 2026-10-06:
"두 지표 같이 보고하게"): hit = root_f1 == 1, strict = hit and mechanism judge == 1 (rubric v2, claude-opus-5-5).

  uv run python scripts/mcp_judge_runs.py outputs/mcp-eval/val36f-<model> outputs/mcp-eval/val36-base-final-20261006

Only runs with root_f1 == 1 are judged by default (strict is 0 otherwise); verdicts are cached per run in
mechanism_judge.json and reused while judge model, rubric and reference version are unchanged.
Failures are saved separately in mechanism_judge.failure.json. Quota exhaustion stops new calls;
any incomplete judging produces a nonzero exit status. Rerun after recovery to reuse valid caches.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import fcntl
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rca_lab.mcp.claude_cli import SessionLimitError
from rca_lab.mcp.rl_reward import (
    conclusion_text,
    judge_cache_path,
    judge_cache_valid,
    judge_mechanism,
    reference_cause,
    reference_version,
)


def case_dir(case_id: str) -> Path:
    if "-openrca2-" in case_id:
        return ROOT / "data/external/openrca2/captures" / case_id
    if "-rcaeval-" in case_id:
        return ROOT / "data/external/rcaeval/captures" / case_id
    return Path("/data/eval-cases") / case_id


def judge_run(run: Path, model: str, rubric: str, judge_all: bool, provider: str = "claude") -> str:
    # Streaming and final sweeps may reach the same episode concurrently.
    # Lock before checking the cache so only one process spends a judge call.
    cache = judge_cache_path(run, provider, model=model)
    with cache.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _judge_run_locked(run, model, rubric, judge_all, provider)


def _judge_run_locked(run: Path, model: str, rubric: str, judge_all: bool, provider: str) -> str:
    score = json.loads((run / "score.json").read_text(encoding="utf-8"))
    if not judge_all and score.get("root_f1") != 1:
        return "skip"
    cache = judge_cache_path(run, provider, model=model)
    if cache.exists() and judge_cache_valid(json.loads(cache.read_text(encoding="utf-8")), score["case_id"], model=model, rubric=rubric, provider=provider):
        return "cached"
    failure = judge_cache_path(run, provider, model=model, failure=True)
    try:
        if not (run / "result.json").exists():
            value, detail = None, "missing result.json"
        else:
            value, detail = judge_mechanism(
                reference_cause(case_dir(score["case_id"])),
                conclusion_text(json.loads((run / "result.json").read_text(encoding="utf-8"))),
                model=model,
                rubric=rubric,
                provider=provider,
            )
        status = "judge-failed" if value is None else "judged"
    except SessionLimitError as exc:
        value, detail, status = None, str(exc), "session-limit"
    if value is None:
        failure.write_text(json.dumps({
            "status": status, "detail": detail, "model": model, "rubric": rubric,
            "case_id": score["case_id"], "provider": provider,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        return status
    temporary = cache.with_suffix(".tmp")
    temporary.write_text(json.dumps({"score": value, "detail": detail, "provider": provider, "model": model, "rubric": rubric, "reference": reference_version(score["case_id"])}, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(cache)
    failure.unlink(missing_ok=True)
    return "judged"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--provider", choices=("claude", "codex"), default="claude")
    parser.add_argument("--rubric", default="v2")
    parser.add_argument("--all", action="store_true", help="also judge runs whose root_f1 < 1")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    runs = [p.parent for root in args.roots for p in sorted(root.glob("case-*/run*/score.json"))]
    outcomes = []
    remaining = iter(runs)
    quota_hit = False
    with cf.ThreadPoolExecutor(args.workers) as pool:
        # Keep at most one call per worker in flight; no queued account-wide retries.
        pending = {
            pool.submit(judge_run, run, args.model, args.rubric, args.all, args.provider)
            for run in (next(remaining, None) for _ in range(args.workers))
            if run is not None
        }
        while pending:
            done, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            for future in done:
                outcome = future.result()
                outcomes.append(outcome)
                quota_hit |= outcome == "session-limit"
            if not quota_hit:
                for _ in done:
                    run = next(remaining, None)
                    if run is not None:
                        pending.add(pool.submit(judge_run, run, args.model, args.rubric, args.all, args.provider))
    outcomes.extend("not-started" for _ in remaining)
    print(json.dumps({k: outcomes.count(k) for k in sorted(set(outcomes))}))
    if quota_hit and args.provider == "codex":
        return 3
    return int(any(outcome in {"session-limit", "judge-failed", "not-started"} for outcome in outcomes))


if __name__ == "__main__":
    sys.exit(main())
