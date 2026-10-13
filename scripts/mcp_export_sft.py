"""Assemble the SFT dataset from accepted rca-mcp episodes.

Sources:
  --student-root outputs/mcp-eval/rollouts-v1   student episodes (trajectory.jsonl as seen by the model)
  --teacher-root outputs/mcp-eval/teacher-v1    teacher episodes (rebuilt into student format, UUIDs aliased)
Acceptance (per run, from score.json): stop=submitted, root_f1 >= --min-root-f1, and optionally
status_correct / ref_grounding == 1. Per case the shortest --per-case accepted episodes are kept
(fewest tool calls first). Only cases in the split's train partition are allowed; validation and
sealed cases are refused. Output: JSONL rows {case_id, run, source, tool_calls, messages, tools}
plus a manifest with counts and the dataset SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rca_lab.mcp import hinted_data
from rca_lab.mcp.answer import SUBMIT_TOOL
from rca_lab.mcp.client import openai_tools
from rca_lab.mcp.export import apply_call_supervision, export_teacher_run, view_source
from rca_lab.mcp.rationalize import infrastructure_pending, quota_contaminated_reasoning


def _accepted(score: dict[str, Any], args: argparse.Namespace) -> bool:
    if score.get("stop_reason") != "submitted" or score.get("unscored"):
        return False
    if float(score.get("root_f1", 0.0)) < args.min_root_f1:
        return False
    if args.max_tool_calls and int(score.get("tool_calls", 0)) > args.max_tool_calls:
        return False
    if args.require_status and not score.get("status_correct"):
        return False
    return not (args.require_grounding and float(score.get("ref_grounding", 0.0)) < 1.0)


LEAK_RE = hinted_data.LEAK_RE
_leaks_hint = hinted_data.leaks_hint


BUDGET_RE = hinted_data.BUDGET_RE
clean_messages = hinted_data.clean_messages


mark_unsupervised = hinted_data.mark_unsupervised


unsupervise_wrong_status = hinted_data.unsupervise_wrong_status


def _train_case(score: dict[str, Any], train_cases: set[str]) -> bool:
    return str(score.get("case_id", "")) in train_cases


def _record_incomplete(args: argparse.Namespace, run: str, reason: str) -> None:
    args.readiness_errors.append(f"{run}: {reason}")


def _judged_turns_missing(run_dir: Path) -> str | None:
    stats_path = run_dir / "rationalize.json"
    if not stats_path.exists():
        return "missing rationalize.json"
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    if "inconsistent" not in stats:
        return "rationalize.json has no judge verdicts (missing inconsistent turns)"
    return None


def _judge_readiness_issue(messages: list[dict[str, Any]], run_dir: Path) -> str | None:
    basic_issue = _judged_turns_missing(run_dir)
    if basic_issue is not None:
        return basic_issue
    stats = json.loads((run_dir / "rationalize.json").read_text(encoding="utf-8"))
    pending = infrastructure_pending(messages, stats)
    if not pending:
        return None
    index = pending[0]
    message = messages[index] if 0 <= index < len(messages) else {}
    raw_judge = stats.get("judge")
    verdict = raw_judge.get(str(index)) if isinstance(raw_judge, dict) else None
    failed = []
    for item in stats.get("failed", []):
        try:
            failed.append(int(item))
        except (TypeError, ValueError):
            continue
    if index in failed:
        return f"writer failed for turn {index}"
    if not (message.get("reasoning_content") or "").strip():
        return f"missing rationalized reasoning for turn {index}"
    if quota_contaminated_reasoning(message):
        return f"quota message cached as reasoning for turn {index}"
    if isinstance(verdict, dict) and verdict.get("error"):
        return f"judge error for turn {index}: {str(verdict.get('error'))[:160]}"
    if not isinstance(raw_judge, dict):
        return "rationalize.json has no per-turn judge verdicts"
    if verdict is None:
        return f"missing judge verdict for turn {index}"
    if not isinstance(verdict, dict) or type(verdict.get("consistent")) is not bool or type(verdict.get("grounded")) is not bool:
        return f"incomplete judge verdict for turn {index}"
    if len(pending) > 1:
        return f"incomplete rationalization/judge state for turn {index} (+{len(pending) - 1} more)"
    return f"incomplete rationalization/judge state for turn {index}"


def _accepted_forbidden_cases(roots: list[Path], args: argparse.Namespace, forbidden: set[str]) -> list[str]:
    leaked = set()
    for root in roots:
        for score_path in sorted(root.glob("case-*/run*/score.json")):
            score = json.loads(score_path.read_text(encoding="utf-8"))
            case_id = str(score.get("case_id", ""))
            if case_id in forbidden and _accepted(score, args):
                leaked.add(case_id)
    return sorted(leaked)


def _unsupervised_tool_turns(messages: list[dict[str, Any]]) -> int:
    return sum(1 for message in messages if message.get("role") == "assistant" and message.get("tool_calls") and message.get("supervise") is False)


def _student_rows(root: Path, args: argparse.Namespace, tools: list[dict[str, Any]], train_cases: set[str]) -> list[dict[str, Any]]:
    rows = []
    for score_path in sorted(root.glob("case-*/run*/score.json")):
        score = json.loads(score_path.read_text(encoding="utf-8"))
        if not _train_case(score, train_cases) or not _accepted(score, args):
            continue
        run_dir = score_path.parent
        run = f"{root.name}/{run_dir.parent.name}/{run_dir.name}"
        if score.get("observation_policy", "legacy") != "legacy":
            _record_incomplete(args, run, "structured observation runs require a policy-aware catalog and new admission before SFT export")
            continue
        source_file = run_dir / "trajectory.rationalized.jsonl" if (run_dir / "trajectory.rationalized.jsonl").exists() else run_dir / "trajectory.jsonl"
        messages = [json.loads(line) for line in source_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        if args.require_rationalized and source_file.name != "trajectory.rationalized.jsonl" and score.get("hinted"):
            _record_incomplete(args, run, "hinted run missing trajectory.rationalized.jsonl")
            continue
        if score.get("hinted") and _leaks_hint(messages):
            print(f"  leak-filtered: {run}")
            continue
        if args.require_judged and source_file.name == "trajectory.rationalized.jsonl":
            issue = _judge_readiness_issue(messages, run_dir)
            if issue is not None:
                _record_incomplete(args, run, issue)
                continue
        unsupervised = mark_unsupervised(messages, run_dir) if source_file.name == "trajectory.rationalized.jsonl" else 0
        call_errors = apply_call_supervision(messages)
        if not _judge_ok(unsupervised, messages, args, run):
            continue
        status_mismatch_turns = 0
        if args.unsupervise_wrong_status:
            status_mismatch_turns = unsupervise_wrong_status(messages, score)
        rows.append({"case_id": score["case_id"], "run": run, "source": "student", "tool_calls": int(score.get("tool_calls", 0)), "root_f1": score.get("root_f1"), "unsupervised_turns": _unsupervised_tool_turns(messages), "call_supervision_errors": int(call_errors or 0), "status_mismatch_turns": status_mismatch_turns, "messages": messages, "tools": tools})
    return rows


def _judge_ok(unsupervised: int, messages: list[dict[str, Any]], args: argparse.Namespace, run: str) -> bool:
    if unsupervised < 0:
        if args.require_judged:
            print(f"  skipped (not judged): {run}")
            return False
        return True
    turns = sum(1 for message in messages if message.get("role") == "assistant" and message.get("tool_calls"))
    if turns and unsupervised / turns > args.max_unsupervised_fraction:
        print(f"  skipped ({unsupervised}/{turns} turns failed the judge): {run}")
        return False
    return True


def _reviewed(run_dir: Path) -> bool:
    """Grader review gate: outputs/<run>/<case>/runN/review.json {"accepted": true, "note": ...}.
    Automatic scoring only checks the cause target; the mechanism text is judged by the grader."""
    review = run_dir / "review.json"
    return review.exists() and bool(json.loads(review.read_text(encoding="utf-8")).get("accepted"))


def _stale_view(run_dir: Path) -> bool:
    stats_path = run_dir / "rationalize.json"
    prior = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.exists() else {}
    return prior.get("view_source", "collected") != view_source(run_dir)


def _teacher_rows(root: Path, args: argparse.Namespace, tools: list[dict[str, Any]], train_cases: set[str]) -> list[dict[str, Any]]:
    rows = []
    for score_path in sorted(root.glob("case-*/run*/score.json")):
        score = json.loads(score_path.read_text(encoding="utf-8"))
        if not _train_case(score, train_cases) or not _accepted(score, args):
            continue
        run_dir = score_path.parent
        if args.require_review and not _reviewed(run_dir):
            continue
        run = f"{root.name}/{run_dir.parent.name}/{run_dir.name}"
        rationalized = (run_dir / "trajectory.rationalized.jsonl").exists()
        if rationalized and _stale_view(run_dir):
            # reasoning written against another view (e.g. the collection-time responses before a replay): its
            # observations no longer match what the student sees, so it is never mixed with the current responses
            if args.require_rationalized:
                _record_incomplete(args, run, "stale rationalized view (re-rationalize with --redo-stale-view)")
                continue
            print(f"  stale view (re-rationalize with --redo-stale-view): {run}")
            rationalized = False
        if rationalized:
            messages = [json.loads(line) for line in (run_dir / "trajectory.rationalized.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        elif args.require_rationalized:
            _record_incomplete(args, run, "teacher run missing trajectory.rationalized.jsonl")
            continue
        else:
            messages, _ = export_teacher_run(run_dir, max_turns=args.max_turns, alias=True, tool_result_max_chars=args.tool_result_max_chars, cases_root=args.cases_root, mcp_binary=args.mcp_binary)
        if args.require_judged and rationalized:
            issue = _judge_readiness_issue(messages, run_dir)
            if issue is not None:
                _record_incomplete(args, run, issue)
                continue
        unsupervised = mark_unsupervised(messages, run_dir) if rationalized else 0
        call_errors = apply_call_supervision(messages)
        if not _judge_ok(unsupervised, messages, args, run):
            continue
        status_mismatch_turns = 0
        if args.unsupervise_wrong_status:
            status_mismatch_turns = unsupervise_wrong_status(messages, score)
        rows.append({"case_id": score["case_id"], "run": run, "source": "teacher", "tool_calls": int(score.get("tool_calls", 0)), "root_f1": score.get("root_f1"), "unsupervised_turns": _unsupervised_tool_turns(messages), "call_supervision_errors": int(call_errors or 0), "status_mismatch_turns": status_mismatch_turns, "messages": messages, "tools": tools})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--student-root", action="append", default=[], type=Path)
    parser.add_argument("--teacher-root", action="append", default=[], type=Path)
    parser.add_argument("--split", default="configs/teacher/v45-family-split-val.yaml")
    parser.add_argument("--catalog", default="tests/fixtures/rca_mcp_tools.json", help="MCP tool catalog JSON (tools/list result)")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--per-case", type=int, default=3)
    parser.add_argument("--max-tool-calls", type=int, default=0, help="drop episodes longer than this many tool calls (0 = no limit); the student's own budget is 30")
    parser.add_argument("--min-root-f1", type=float, default=1.0)
    parser.add_argument("--require-status", action="store_true")
    parser.add_argument("--require-grounding", action="store_true")
    parser.add_argument("--require-review", action="store_true", help="teacher rows need a grader review.json with accepted=true (mechanism check)")
    parser.add_argument("--require-rationalized", action="store_true", help="hinted student rows and teacher rows must have trajectory.rationalized.jsonl (student reasoning regenerated under the clean prompt)")
    parser.add_argument("--cases-root", type=Path, default=Path("/data/eval-cases"), help="rebuild teacher seeds from captures with the current seed builder")
    parser.add_argument("--mcp-binary", type=Path, default=ROOT / "tools/rca-mcp/bin/rca-mcp")
    parser.add_argument("--max-turns", type=int, default=40, help="turn budget written into every exported user prompt; must match the student's rollout/eval --max-turns")
    parser.add_argument("--tool-result-max-chars", type=int, default=6_000)
    parser.add_argument("--unsupervise-wrong-status", action="store_true", help="keep episodes whose submitted status disagrees with the golden, but exclude their final submit_rca turn from the loss (17/42 of dataset v2 were confirmed-vs-provisional mismatches)")
    parser.add_argument("--require-judged", action="store_true", help="rationalized rows must carry judge verdicts (mcp_rationalize.py --judge); infrastructure-incomplete train rows fail the export before writing output")
    parser.add_argument("--max-unsupervised-fraction", type=float, default=0.3, help="drop an episode when more than this fraction of its turns failed the judge")
    args = parser.parse_args()

    split = yaml.safe_load(Path(args.split).read_text(encoding="utf-8"))
    train_cases = set(split["train"])
    forbidden = set(split.get("validation", [])) | set(split.get("sealed_eval", []))
    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    tools = openai_tools(catalog) + openai_tools([SUBMIT_TOOL])
    args.readiness_errors = []

    leaked = _accepted_forbidden_cases(args.student_root + args.teacher_root, args, forbidden)
    if leaked:
        raise SystemExit(f"refusing validation/sealed cases in training data: {leaked}")
    candidates: list[dict[str, Any]] = []
    for root in args.student_root:
        candidates += _student_rows(root, args, tools, train_cases)
    for root in args.teacher_root:
        candidates += _teacher_rows(root, args, tools, train_cases)
    if args.readiness_errors:
        detail = "\n".join(f"  - {item}" for item in args.readiness_errors[:20])
        more = f"\n  ... {len(args.readiness_errors) - 20} more" if len(args.readiness_errors) > 20 else ""
        raise SystemExit(f"refusing to export: {len(args.readiness_errors)} train run(s) have incomplete rationalization/judging\n{detail}{more}")
    candidates = [row for row in candidates if row["case_id"] in train_cases]
    tool_names = {tool["function"]["name"] for tool in tools}
    for row in candidates:
        row["messages"], dropped = clean_messages(row["messages"], tool_names, args.max_turns)
        if dropped:
            row["tool_calls"] -= dropped
            print(f"  dropped {dropped} off-catalog tool turn(s): {row['run']}")
        if args.max_tool_calls and row["tool_calls"] > args.max_tool_calls:
            row["case_id"] = ""  # over budget after cleaning; excluded below
    candidates = [row for row in candidates if row["case_id"]]

    selected: list[dict[str, Any]] = []
    by_case: dict[str, list[dict[str, Any]]] = {}
    for row in candidates:
        by_case.setdefault(row["case_id"], []).append(row)
    for case_id in sorted(by_case):
        ranked = sorted(by_case[case_id], key=lambda row: (row["tool_calls"], row["run"]))
        selected += ranked[: args.per_case]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in selected)
    args.out.write_text(payload, encoding="utf-8")
    manifest = {
        "examples": len(selected),
        "cases": len(by_case),
        "per_case": {case_id: len([row for row in selected if row["case_id"] == case_id]) for case_id in sorted(by_case)},
        "sources": {source: sum(row["source"] == source for row in selected) for source in ("student", "teacher")},
        "mean_tool_calls": (sum(row["tool_calls"] for row in selected) / len(selected)) if selected else 0.0,
        "unsupervised_turns": sum(row.get("unsupervised_turns", 0) for row in selected),
        "call_supervision_errors": sum(row.get("call_supervision_errors", 0) for row in selected),
        "readiness": {"require_judged": args.require_judged, "infrastructure_incomplete": 0},
        "status_mismatch_episodes": sum(1 for row in selected if row.get("status_mismatch_turns", 0) > 0),
        "supervised_turns": sum(sum(1 for m in row["messages"] if m.get("role") == "assistant" and m.get("tool_calls") and m.get("supervise") is not False) for row in selected),
        "acceptance": {"min_root_f1": args.min_root_f1, "require_status": args.require_status, "require_grounding": args.require_grounding},
        "dataset_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "split": args.split,
        "missing_train_cases": sorted(train_cases - set(by_case)),
    }
    args.out.with_suffix(".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({key: value for key, value in manifest.items() if key != "per_case"}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
