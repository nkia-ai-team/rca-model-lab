"""Regenerate student reasoning for accepted episodes under the clean prompt (teacher-forced actions).

  uv run python scripts/mcp_rationalize.py --student-root outputs/mcp-eval/rollouts-h1 --endpoint http://127.0.0.1:18002/v1
  uv run python scripts/mcp_rationalize.py --teacher-root outputs/mcp-eval/teacher-r1 --endpoint ...

Student roots: accepted runs (score.json) get run<N>/trajectory.rationalized.jsonl.
Teacher roots: accepted + reviewed runs are first rebuilt into student format (aliased), then
rationalized; the result is run<N>/trajectory.rationalized.jsonl as well. Export prefers that file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rca_lab.mcp.answer import SUBMIT_TOOL
from rca_lab.mcp.claude_cli import SessionLimitError
from rca_lab.mcp.client import openai_tools
from rca_lab.mcp.export import export_teacher_run, view_source
from rca_lab.mcp.judge import Judge, JudgeConfig, claude_judge
from rca_lab.mcp.rationalize import (
    RationalizeConfig,
    RationalizeInterrupted,
    judge_pass,
    quota_contaminated_reasoning,
    rationalize_infra_failures,
    rationalize_messages,
    repair_turns,
    tool_turn_indices,
)


def _accepted(score: dict, min_root_f1: float, require_status: bool) -> bool:
    return score.get("stop_reason") == "submitted" and float(score.get("root_f1", 0.0)) >= min_root_f1 and (score.get("status_correct") or not require_status)


def _reviewed(run_dir: Path) -> bool:
    review = run_dir / "review.json"
    return review.exists() and bool(json.loads(review.read_text(encoding="utf-8")).get("accepted"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--student-root", action="append", default=[], type=Path)
    parser.add_argument("--teacher-root", action="append", default=[], type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:18002/v1")
    parser.add_argument("--model", default="muse-glimmer-base")
    parser.add_argument("--catalog", default="tests/fixtures/rca_mcp_tools.json")
    parser.add_argument("--min-root-f1", type=float, default=1.0)
    parser.add_argument("--require-status", action="store_true")
    parser.add_argument("--cases-root", type=Path, default=Path("/data/eval-cases"), help="rebuild teacher seeds from captures with the current seed builder")
    parser.add_argument("--mcp-binary", type=Path, default=ROOT / "tools/rca-mcp/bin/rca-mcp")
    parser.add_argument("--max-turns", type=int, default=50, help="turn budget wording for rebuilt teacher prompts (export rewrites it again)")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--redo-stale-view", action="store_true", help="also redo cached teacher episodes rationalized against a different view than the current one (e.g. before ledger.replay.jsonl existed); resumable, unlike --overwrite")
    parser.add_argument("--redo-failed-fraction", type=float, default=None, help="also redo cached episodes whose rationalize.json failed/turns >= this fraction")
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--judge", action="store_true", help="LLM-judge every reasoning against its call (judge.py); cached episodes are judged in place and flagged turns resampled")
    parser.add_argument("--judge-provider", choices=["claude", "codex"], default="claude")
    parser.add_argument("--judge-model", default="claude-sonnet-5")
    parser.add_argument("--judge-rounds", type=int, default=2, help="resample+re-judge rounds for flagged turns (0 = audit only)")
    parser.add_argument("--judge-batch", type=int, default=12)
    parser.add_argument("--rejudge", action="store_true", help="judge cached episodes even when rationalize.json already carries verdicts")
    parser.add_argument("--writer", choices=["gemma", "student", "claude", "codex"], default="student", help="who writes the reasoning: gemma (on-policy native Gemma thoughts, accepted when the sampled calls match; use with --max-tokens 8192 --temperature 1.0), the student (Muse, vLLM forced call), Claude, or Codex (rationale_writer.py)")
    parser.add_argument("--gemma-samples", type=int, default=4, help="gemma writer: free samples per turn")
    parser.add_argument("--writer-model", default="claude-sonnet-5")
    parser.add_argument("--redo-provider", action="store_true", help="redo cached episodes whose writer/judge provider or model stamp differs from the requested provider/model")
    parser.add_argument("--shard", default=None, help="k/n: process only episodes with index %% n == k (parallel workers)")
    parser.add_argument("--student-all", dest="student_hinted_only", action="store_false", help="also rationalize unhinted student runs (default: hinted only)")
    parser.add_argument("--student-require-review", action="store_true", help="student roots: only runs with an accepted review.json (hint-ladder runs: scripts/mcp_ladder_accept.py)")
    args = parser.parse_args()
    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    tools = openai_tools(catalog) + openai_tools([SUBMIT_TOOL])
    config = RationalizeConfig(endpoint=args.endpoint, model=args.model, temperature=args.temperature, max_tokens=args.max_tokens, writer=args.writer, writer_model=args.writer_model, gemma_samples=args.gemma_samples)
    judge = claude_judge(JudgeConfig(provider=args.judge_provider, model=args.judge_model, batch_size=args.judge_batch)) if args.judge else None

    jobs: list[tuple[Path, list[dict], str]] = []
    for root in args.student_root:
        for score_path in sorted(root.glob("case-*/run*/score.json")):
            score = json.loads(score_path.read_text(encoding="utf-8"))
            if args.student_hinted_only and not score.get("hinted"):
                continue  # an unhinted success is the student's own reasoning for its own calls; used as-is
            if _accepted(score, args.min_root_f1, args.require_status) and (not args.student_require_review or _reviewed(score_path.parent)):
                run_dir = score_path.parent
                jobs.append((run_dir, [json.loads(line) for line in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()], "collected"))
    for root in args.teacher_root:
        for score_path in sorted(root.glob("case-*/run*/score.json")):
            score = json.loads(score_path.read_text(encoding="utf-8"))
            run_dir = score_path.parent
            if _accepted(score, args.min_root_f1, args.require_status) and _reviewed(run_dir):
                source = view_source(run_dir)
                messages, _ = export_teacher_run(run_dir, max_turns=args.max_turns, alias=True, cases_root=args.cases_root, mcp_binary=args.mcp_binary)
                jobs.append((run_dir, messages, source))
    if args.shard:
        k, n = (int(x) for x in args.shard.split("/"))
        jobs = [job for i, job in enumerate(jobs) if i % n == k]
    print(f"{len(jobs)} episodes to rationalize", flush=True)
    totals = {"turns": 0, "inconsistent": 0, "resampled": 0}
    try:
        _process(jobs, args, config, tools, judge, totals)
    except SessionLimitError as exc:
        print(f"stopping: model-provider usage limit reached ({exc}). Re-run to resume; finished episodes are cached.", flush=True)
        raise SystemExit(3) from exc
    if judge is not None:
        print(f"judge summary: turns={totals['turns']} still_inconsistent={totals['inconsistent']} resampled={totals['resampled']}", flush=True)
    if totals.get("infra_failed"):
        print(f"incomplete rationalization: infra_failed_turns={totals['infra_failed']} (re-run to resume)", flush=True)
    return int(bool(totals.get("infra_failed")))


def _process(jobs: list[tuple[Path, list[dict], str]], args: argparse.Namespace, config: RationalizeConfig, tools: list[dict], judge: Judge | None, totals: dict[str, int]) -> None:
    for run_dir, messages, source in jobs:
        target = run_dir / "trajectory.rationalized.jsonl"
        stats_path = run_dir / "rationalize.json"
        if target.exists() and not args.overwrite:
            prior = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.exists() else {}
            redo = bool(args.redo_failed_fraction is not None and prior.get("turns") and len(prior.get("failed", [])) / prior["turns"] >= args.redo_failed_fraction)
            redo = redo or (args.redo_stale_view and prior.get("view_source", "collected") != source)
            redo = redo or (getattr(args, "redo_provider", False) and _writer_stale(prior, config))
            if not redo:
                cached = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
                prior.setdefault("turns", len(tool_turn_indices(cached)))
                prior.setdefault("regenerated", 0)
                judge_stale = bool(getattr(args, "redo_provider", False) and _judge_stale(prior, args, judge is not None))
                if judge_stale:
                    prior.pop("judge", None)
                    prior.pop("judge_rounds", None)
                    prior["inconsistent"] = []
                    prior.pop("infra_failed", None)
                repair = set(rationalize_infra_failures(cached, prior, judge_expected=False))
                repair.update(index for index in tool_turn_indices(cached) if quota_contaminated_reasoning(cached[index]))
                try:
                    repaired = repair_turns(config, cached, repair, tools, prior) if repair else []
                    need_judge = judge is not None and (judge_stale or args.rejudge or "judge" not in prior or bool(repaired) or bool(rationalize_infra_failures(cached, prior, judge_expected=True)))
                    if judge is not None and need_judge:
                        # Audit the cached reasoning in place: preserve verified turns and resample only
                        # turns whose prior completion was missing, infra-failed, or explicitly rejudged.
                        only = None if judge_stale or args.rejudge or "judge" not in prior else set(repaired) | set(rationalize_infra_failures(cached, prior, judge_expected=True))
                        judge_pass(config, cached, tools, judge, prior, rounds=args.judge_rounds, only=only)
                except RationalizeInterrupted as exc:
                    prior = exc.stats
                    cached = exc.messages
                    _stamp_provenance(prior, args, config, judge is not None)
                    prior["infra_failed"] = rationalize_infra_failures(cached, prior, judge_expected=judge is not None)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("".join(json.dumps(message, ensure_ascii=False) + "\n" for message in cached), encoding="utf-8")
                    stats_path.write_text(json.dumps(prior, ensure_ascii=False, indent=1), encoding="utf-8")
                    raise SessionLimitError(str(exc)) from exc
                infra_failed = rationalize_infra_failures(cached, prior, judge_expected=judge is not None)
                if repair or need_judge or infra_failed:
                    target.write_text("".join(json.dumps(message, ensure_ascii=False) + "\n" for message in cached), encoding="utf-8")
                    prior["infra_failed"] = infra_failed
                    if judge is not None and need_judge:
                        _stamp_judge_provenance(prior, args)
                    stats_path.write_text(json.dumps(prior, ensure_ascii=False, indent=1), encoding="utf-8")
                totals["turns"] += prior.get("turns") or len(tool_turn_indices(cached))
                totals["inconsistent"] += len(prior.get("inconsistent", []))
                totals["resampled"] += prior.get("judge_resampled", 0)
                totals["infra_failed"] = totals.get("infra_failed", 0) + len(infra_failed)
                if repair or need_judge:
                    print(f"  repaired {run_dir}: repaired={repaired} inconsistent={prior.get('inconsistent', [])} infra_failed={infra_failed} resampled={prior.get('judge_resampled', 0)}", flush=True)
                else:
                    print(f"  cached {run_dir}", flush=True)
                continue
        try:
            new_messages, stats = rationalize_messages(config, messages, tools, judge=judge, judge_rounds=args.judge_rounds)
        except RationalizeInterrupted as exc:
            new_messages, stats = exc.messages, exc.stats
            stats["view_source"] = source
            _stamp_provenance(stats, args, config, judge is not None)
            stats["infra_failed"] = rationalize_infra_failures(new_messages, stats, judge_expected=judge is not None)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("".join(json.dumps(message, ensure_ascii=False) + "\n" for message in new_messages), encoding="utf-8")
            stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
            raise SessionLimitError(str(exc)) from exc
        stats["view_source"] = source
        _stamp_provenance(stats, args, config, judge is not None)
        target.write_text("".join(json.dumps(message, ensure_ascii=False) + "\n" for message in new_messages), encoding="utf-8")
        stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
        totals["turns"] += stats["turns"]
        totals["inconsistent"] += len(stats.get("inconsistent", []))
        totals["resampled"] += stats.get("judge_resampled", 0)
        infra_failed = rationalize_infra_failures(new_messages, stats, judge_expected=judge is not None)
        stats["infra_failed"] = infra_failed
        totals["infra_failed"] = totals.get("infra_failed", 0) + len(infra_failed)
        stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  {run_dir}: turns={stats['turns']} regenerated={stats['regenerated']} failed={stats['failed']} inconsistent={stats.get('inconsistent')} {stats['seconds']}s", flush=True)


def _stamp_provenance(stats: dict, args: argparse.Namespace, config: RationalizeConfig, judge_enabled: bool) -> None:
    stats["writer_provider"] = config.writer
    stats["writer_model"] = config.writer_model
    if judge_enabled:
        _stamp_judge_provenance(stats, args)


def _stamp_judge_provenance(stats: dict, args: argparse.Namespace) -> None:
    stats["judge_provider"] = getattr(args, "judge_provider", "claude")
    stats["judge_model"] = getattr(args, "judge_model", "claude-sonnet-5")


def _writer_stale(prior: dict, config: RationalizeConfig) -> bool:
    if prior.get("writer_provider", "claude") != config.writer:
        return True
    return prior.get("writer_model", prior.get("claude_model", "claude-sonnet-5")) != config.writer_model


def _judge_stale(prior: dict, args: argparse.Namespace, judge_enabled: bool) -> bool:
    if not judge_enabled:
        return False
    if prior.get("judge_provider", "claude") != getattr(args, "judge_provider", "claude"):
        return True
    return prior.get("judge_model", prior.get("claude_judge_model", "claude-sonnet-5")) != getattr(args, "judge_model", "claude-sonnet-5")


if __name__ == "__main__":
    raise SystemExit(main())
