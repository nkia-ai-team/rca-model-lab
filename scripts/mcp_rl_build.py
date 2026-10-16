"""Build an on-policy GRPO dataset from student rollouts on the rca-mcp surface.

Rollouts come from `mcp_case_run.py --runner student --runs K` with the current policy adapter (one
group of K runs per train capture). Reward = score.json `reward` (root F1 + exact-hit bonus +
grounding + status, from rca_lab.mcp.score). Advantage per run = (r - group mean) / (group std + eps),
clipped to ±clip; groups whose rewards are all equal carry no signal and are dropped. Each row is the
student's own trajectory (already in the training message format) plus its advantage, consumed by
`mcp_sft.py` with `method: rl`.

  uv run python scripts/mcp_rl_build.py --root outputs/mcp-rl/iter1 --out data/processed/mcp-rl-iter1.jsonl \
      [--split configs/teacher/v45-family-split-val.yaml] [--min-group 2]

Only train-partition captures are accepted (validation/sealed rollouts are refused).

Guided runs (--hinted-root): hint-ladder runs never enter as raw trajectories (their reasoning echoes
the live-prompt hint). Only runs that passed every gate of rca_lab.mcp.hinted_data (accepted review,
rationalized + judged reasoning, no guidance wording) are added, at most --hinted-per-group, and only
to a group whose own K runs have no full-mark success (root_f1 1 and mechanism 1). They join the
group before the advantage is computed, so the guided success is pushed up and the group's failures
down; their rows carry the rationalized messages and source "student-hinted".

Teacher runs (--teacher-root, 2026-09-29 user: "못맞추는것들을 교사풀이를 넣으면서"): accepted Claude-teacher
episodes that already passed the SFT gates (review, rationalized + judged, grounding 1, <= 50 calls,
unsupervised fraction <= 0.3, cleaned like the SFT export) and get mechanism judge 1 join unsolved groups
the same way (hinted runs first, then teacher; --guided-per-group in total), source "teacher".

Keeping what the policy already solves ("정답을 잘맞추는건 그 추론과정을 잘유지"): a full-mark run (root_f1 1 +
mechanism 1) never gets a negative advantage (GRPO is relative, so a slightly slower correct run would be
pushed down; --no-protect-correct restores plain GRPO), and a group dropped as flat that holds full-mark
runs contributes up to --self-replay-per-group of them with advantage --self-replay-advantage (0 = off).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rca_lab.mcp.answer import SUBMIT_TOOL
from rca_lab.mcp.claude_cli import SessionLimitError
from rca_lab.mcp.client import openai_tools
from rca_lab.mcp.golden import candidates_from_result, load_contract, root_precision_recall
from rca_lab.mcp.hinted_data import (
    clean_messages,
    load_rationalized,
    unsupervise_wrong_status,
)
from rca_lab.mcp.rl_reward import (
    JUDGE_PROMPTS,
    conclusion_text,
    forced_answer,
    judge_cache_valid,
    judge_mechanism,
    reference_cause,
    reference_version,
    rl_reward,
)


def group_advantages(rewards: list[float], *, eps: float = 0.05, clip: float = 3.0, norm: str = "group", min_range: float = 0.0) -> list[float] | None:
    """Advantages for one group; None when the group has no usable reward spread (max - min < min_range).

    norm="group": (r - mean) / (group std + eps), clipped — the original GRPO form. With K=4 and small
    spreads it turns judge/efficiency noise into advantages as large as a real hit/miss split (Fable
    review 2026-09-29, e.g. a 0.13 spread became ±0.74). norm="batch" / "none": the centered r - mean;
    `scale_advantages` then divides every group by one iteration-wide std ("batch", keeps the overall
    scale the learning rate was tuned for) or leaves them in reward units ("none", Dr. GRPO)."""
    if len(rewards) < 2:
        return None
    spread = max(rewards) - min(rewards)
    if spread < 1e-9 or spread < min_range:
        return None
    mean = statistics.fmean(rewards)
    if norm == "group":
        std = statistics.pstdev(rewards)
        return [max(-clip, min(clip, (r - mean) / (std + eps))) for r in rewards]
    return [r - mean for r in rewards]


def scale_advantages(per_group: list[list[float]], *, norm: str, eps: float = 0.05, clip: float = 3.0) -> list[list[float]]:
    """Iteration-level scaling for the centered forms (see group_advantages); "group" is already final."""
    if norm == "group":
        return per_group
    scale = 1.0
    if norm == "batch":
        values = [value for group in per_group for value in group]
        std = statistics.pstdev(values) if len(values) > 1 else 0.0
        scale = 1.0 / (std + eps) if std > 1e-9 else 1.0
    return [[max(-clip, min(clip, value * scale)) for value in group] for group in per_group]


def unsupervise_prose_turns(messages: list[dict[str, Any]]) -> int:
    """Assistant turns without a tool call (stop-string collisions, nudged turns) stay as context but carry
    no loss: every legitimate turn of this harness ends in a call (submit_rca included)."""
    marked = 0
    for message in messages:
        if message.get("role") == "assistant" and not message.get("tool_calls") and message.get("supervise") is not False:
            message["supervise"] = False
            marked += 1
    return marked


def load_groups(root: Path) -> dict[str, list[tuple[Path, dict[str, Any]]]]:
    groups: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for score_path in sorted(root.glob("case-*/run*/score.json")):
        score = json.loads(score_path.read_text(encoding="utf-8"))
        if score.get("observation_policy", "legacy") != "legacy":
            raise SystemExit(f"structured observation run requires policy-aware catalog and admission before RL export: {score_path}")
        if score.get("unscored"):
            continue
        if score.get("hinted"):
            # Hint-ladder runs are off-policy and their reasoning echoes the live-prompt hint
            # ("Previous feedback says ..."); they may enter training only after rationalize.
            continue
        groups.setdefault(score["case_id"], []).append((score_path.parent, score))
    return groups


def load_hinted(roots: list[Path], train: set[str], *, max_unsupervised_fraction: float) -> tuple[dict[str, list[tuple[Path, dict[str, Any], list[dict[str, Any]]]]], dict[str, int]]:
    """case -> [(run_dir, score, rationalized messages)] of gated hinted runs, best first (lowest rung, fewest calls)."""
    found: dict[str, list[tuple[Path, dict[str, Any], list[dict[str, Any]]]]] = {}
    reasons: dict[str, int] = {}
    for root in roots:
        for score_path in sorted(root.glob("case-*/run*/score.json")):
            score = json.loads(score_path.read_text(encoding="utf-8"))
            if not score.get("hinted"):
                continue
            if score.get("observation_policy", "legacy") != "legacy":
                raise SystemExit(f"structured observation run requires policy-aware catalog and admission before RL export: {score_path}")
            if score["case_id"] not in train:
                raise SystemExit(f"non-train capture in a hinted root (validation/sealed must never train): {score['case_id']}")
            messages, reason = load_rationalized(score_path.parent, max_unsupervised_fraction=max_unsupervised_fraction)
            reasons[reason] = reasons.get(reason, 0) + 1
            if messages is not None:
                found.setdefault(score["case_id"], []).append((score_path.parent, score, messages))
    for runs in found.values():
        runs.sort(key=lambda item: (int(item[1].get("hint_rung") or 0), int(item[1].get("tool_calls") or 0)))
    return found, reasons


def load_teacher(roots: list[Path], train: set[str], *, tool_names: set[str], max_turns: int, max_tool_calls: int,
                 max_unsupervised_fraction: float) -> tuple[dict[str, list[tuple[Path, dict[str, Any], list[dict[str, Any]]]]], dict[str, int]]:
    """case -> [(run_dir, score, cleaned rationalized messages)] of teacher runs that pass the SFT gates (fewest calls first).
    The mechanism gate (judge = 1) is applied later, only for the captures that need guidance."""
    found: dict[str, list[tuple[Path, dict[str, Any], list[dict[str, Any]]]]] = {}
    reasons: dict[str, int] = {}

    def note(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for root in roots:
        for score_path in sorted(root.glob("case-*/run*/score.json")):
            score = json.loads(score_path.read_text(encoding="utf-8"))
            run_dir = score_path.parent
            if score.get("stop_reason") != "submitted" or score.get("unscored") or float(score.get("root_f1") or 0.0) != 1.0:
                note("not a full hit")
                continue
            if score["case_id"] not in train:
                raise SystemExit(f"non-train capture in a teacher root (validation/sealed must never train): {score['case_id']}")
            if float(score.get("ref_grounding") or 0.0) < 1.0 or int(score.get("tool_calls") or 0) > max_tool_calls:
                note("grounding < 1 or too many calls")
                continue
            messages, reason = load_rationalized(run_dir, max_unsupervised_fraction=max_unsupervised_fraction)
            if messages is None:
                note(reason)
                continue
            messages, _ = clean_messages(messages, tool_names, max_turns)
            unsupervise_wrong_status(messages, score)
            note("ok")
            found.setdefault(score["case_id"], []).append((run_dir, score, messages))
    for runs in found.values():
        runs.sort(key=lambda item: int(item[1].get("tool_calls") or 0))
    return found, reasons


def guided_additions(groups: dict[str, list[tuple[Path, dict[str, Any]]]], pool: dict[str, list[tuple[Path, dict[str, Any], list[dict[str, Any]], str]]],
                     full_mark: Any, per_group: int, eligible: Any = None) -> dict[str, list[tuple[Path, dict[str, Any], list[dict[str, Any]], str]]]:
    """Guided runs to add: only for groups (captures of this iteration) with no full-mark on-policy run;
    candidates in pool order, each must pass `eligible(run_dir, score, source)` (e.g. the mechanism gate)."""
    additions = {}
    for case, runs in groups.items():
        if case not in pool or any(full_mark(run_dir, score) for run_dir, score in runs):
            continue
        chosen = [item for item in pool[case] if eligible is None or eligible(item[0], item[1], item[3])][:per_group]
        if chosen:
            additions[case] = chosen
    return additions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, type=Path, action="append", help="rollout root(s) of one iteration")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--split", default="configs/teacher/v45-family-split-val.yaml")
    parser.add_argument("--catalog", default="tests/fixtures/rca_mcp_tools.json")
    parser.add_argument("--min-group", type=int, default=2)
    parser.add_argument("--eps", type=float, default=0.05)
    parser.add_argument("--clip", type=float, default=3.0)
    parser.add_argument("--reward", choices=("rl", "eval"), default="rl", help="rl = rca_lab.mcp.rl_reward (mechanism judge, unclipped wrong-confirmation penalty, efficiency); eval = score.json reward")
    parser.add_argument("--reward-root-term", choices=("precision2", "f1"), default="precision2", help="root credit in the rl reward: recall x precision^2 (default since 2026-10-01; extra candidates cost) or plain F1 (legacy)")
    parser.add_argument("--contract", default="configs/eval/train-family-v2.yaml", help="golden contract used to rescore precision/recall of runs scored before score.json carried them")
    parser.add_argument("--reward-status-terms", action="store_true", help="legacy rl reward with the confidence-level terms (+status, -wrong confirmation); off since 2026-10-01")
    parser.add_argument("--judge-model", default="claude-opus-5-5")
    parser.add_argument("--judge-rubric", choices=tuple(JUDGE_PROMPTS), default="v2", help="mechanism-judge prompt version (v2 = 2026-09-29 audit: observability-aware)")
    parser.add_argument("--cases-root", type=Path, default=Path("/data/eval-cases"))
    parser.add_argument("--judge-workers", type=int, default=4)
    parser.add_argument("--hinted-root", type=Path, action="append", default=[], help="hint-ladder root(s); only gated, rationalized runs are used (see module docstring)")
    parser.add_argument("--teacher-root", type=Path, action="append", default=[], help="Claude-teacher root(s); SFT-gated, rationalized runs with mechanism judge 1 (see module docstring)")
    parser.add_argument("--guided-per-group", "--hinted-per-group", dest="guided_per_group", type=int, default=1)
    parser.add_argument("--guided-max-unsupervised-fraction", "--hinted-max-unsupervised-fraction", dest="guided_max_unsupervised_fraction", type=float, default=0.3)
    parser.add_argument("--teacher-max-tool-calls", type=int, default=50)
    parser.add_argument("--max-turns", type=int, default=50, help="turn budget written into teacher prompts (same as the SFT export)")
    parser.add_argument("--advantage-norm", choices=("batch", "group", "none"), default="batch", help="batch: group-centered, one iteration-wide std (default); group: original per-group std; none: Dr. GRPO")
    parser.add_argument("--min-range", type=float, default=0.15, help="drop groups whose reward spread (max - min) is below this")
    parser.add_argument("--supervise-prose-turns", action="store_true", help="legacy: train assistant turns that made no tool call")
    parser.add_argument("--no-protect-correct", dest="protect_correct", action="store_false", help="plain GRPO: full-mark runs may get negative advantages")
    parser.add_argument("--self-replay-advantage", type=float, default=0.5, help="advantage for full-mark runs of groups dropped as flat (0 = off)")
    parser.add_argument("--self-replay-per-group", type=int, default=2)
    args = parser.parse_args()

    split = yaml.safe_load(Path(args.split).read_text(encoding="utf-8"))
    train = set(split["train"])
    tools = openai_tools(json.loads(Path(args.catalog).read_text(encoding="utf-8"))) + openai_tools([SUBMIT_TOOL])

    groups: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for root in args.root:
        for case, runs in load_groups(root).items():
            groups.setdefault(case, []).extend(runs)
    refused = sorted(c for c in groups if c not in train)
    if refused:
        raise SystemExit(f"non-train captures in RL rollouts (validation/sealed must never train): {refused}")

    judge_stats = {"judged": 0, "cached": 0, "failed": 0, "skipped_f1_zero": 0}
    if args.reward == "rl":
        _judge_all(groups, args, judge_stats)
    guided: dict[Path, tuple[list[dict[str, Any]], str]] = {}
    guided_stats: dict[str, Any] = {}

    def full_mark(run_dir: Path, score: dict[str, Any]) -> bool:
        cached = _cached_judgement(run_dir, args)
        return float(score.get("root_f1") or 0.0) == 1.0 and bool(cached) and cached.get("score") == 1.0

    if args.hinted_root or args.teacher_root:
        pool: dict[str, list[tuple[Path, dict[str, Any], list[dict[str, Any]], str]]] = {}
        hinted, hinted_gate = load_hinted(args.hinted_root, train, max_unsupervised_fraction=args.guided_max_unsupervised_fraction) if args.hinted_root else ({}, {})
        tool_names = {tool["function"]["name"] for tool in tools}
        teacher, teacher_gate = load_teacher(args.teacher_root, train, tool_names=tool_names, max_turns=args.max_turns, max_tool_calls=args.teacher_max_tool_calls,
                                             max_unsupervised_fraction=args.guided_max_unsupervised_fraction) if args.teacher_root else ({}, {})
        for source, found in (("student-hinted", hinted), ("teacher", teacher)):  # hinted student runs are closer to the policy: first
            for case, runs in found.items():
                pool.setdefault(case, []).extend((run_dir, score, messages, source) for run_dir, score, messages in runs)

        def eligible(run_dir: Path, score: dict[str, Any], source: str) -> bool:
            if args.reward == "rl" and _cached_judgement(run_dir, args) is None:
                _judge_all({score["case_id"]: [(run_dir, score)]}, args, judge_stats)
            return full_mark(run_dir, score)

        additions = guided_additions(groups, pool, full_mark, args.guided_per_group, eligible)
        for case, runs in additions.items():
            for run_dir, score, messages, source in runs:
                groups[case].append((run_dir, score))
                guided[run_dir] = (messages, source)
        by_source: dict[str, int] = {}
        for _, source in guided.values():
            by_source[source] = by_source.get(source, 0) + 1
        guided_stats = {"hinted_roots": [str(r) for r in args.hinted_root], "teacher_roots": [str(r) for r in args.teacher_root], "hinted_gate": hinted_gate,
                        "teacher_gate": teacher_gate, "captures_available": sorted(pool), "added": len(guided), "added_by_source": by_source, "added_cases": sorted(additions)}
    rows: list[dict[str, Any]] = []
    stats = {"groups": len(groups), "groups_used": 0, "groups_flat": 0, "groups_small": 0, "rollouts": 0, "reward_mean": 0.0, "eval_reward_mean": 0.0, "reward_kind": args.reward,
             "protected_correct": 0, "self_replay": 0, "prose_turns_unsupervised": 0, "advantage_norm": args.advantage_norm, "min_range": args.min_range, "reward_status_terms": args.reward_status_terms, "reward_root_term": args.reward_root_term}
    all_rewards: list[float] = []
    eval_rewards: list[float] = []
    scored: list[tuple[str, list[tuple[Path, dict[str, Any]]], list[float], list[float] | None]] = []
    for case in sorted(groups):
        runs = groups[case]
        on_policy = [(run_dir, score) for run_dir, score in runs if run_dir not in guided]
        eval_rewards.extend(float(score.get("reward", 0.0)) for _, score in on_policy)
        rewards = [_reward(run_dir, score, args, guided[run_dir][0] if run_dir in guided else None) for run_dir, score in runs]
        all_rewards.extend(reward for (run_dir, _), reward in zip(runs, rewards, strict=True) if run_dir not in guided)
        stats["rollouts"] += len(on_policy)
        if len(runs) < args.min_group:
            stats["groups_small"] += 1
            continue
        scored.append((case, runs, rewards, group_advantages(rewards, eps=args.eps, clip=args.clip, norm=args.advantage_norm, min_range=args.min_range)))
    scaled = iter(scale_advantages([adv for *_, adv in scored if adv is not None], norm=args.advantage_norm, eps=args.eps, clip=args.clip))
    for case, runs, rewards, raw in scored:
        advantages = next(scaled) if raw is not None else None
        if advantages is None:
            stats["groups_flat"] += 1
            replay = [i for i, (run_dir, score) in enumerate(runs) if full_mark(run_dir, score)][: args.self_replay_per_group] if args.self_replay_advantage > 0 else []
            if not replay:
                continue
            advantages = [args.self_replay_advantage if i in replay else 0.0 for i in range(len(runs))]
            stats["self_replay"] += len(replay)
        else:
            stats["groups_used"] += 1
        for index, ((run_dir, score), advantage) in enumerate(zip(runs, advantages, strict=True)):
            if args.protect_correct and advantage < 0 and full_mark(run_dir, score):
                stats["protected_correct"] += 1
                advantage = 0.0
            if advantage == 0.0:
                continue  # zero weight: no gradient either way
            messages, source = guided.get(run_dir, (None, "student-rl"))
            if messages is None:
                messages = [json.loads(line) for line in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            if not args.supervise_prose_turns:
                stats["prose_turns_unsupervised"] += unsupervise_prose_turns(messages)
            if not any(m.get("role") == "assistant" and m.get("tool_calls") for m in messages):
                continue
            rows.append({"case_id": case, "run": f"{run_dir.parent.parent.name}/{run_dir.parent.name}/{run_dir.name}", "source": source,
                         "reward": rewards[index], "eval_reward": float(score.get("reward", 0.0)), "root_f1": score.get("root_f1"), "advantage": round(advantage, 6),
                         "messages": messages, "tools": tools})
    stats["reward_mean"] = round(statistics.fmean(all_rewards), 4) if all_rewards else 0.0
    stats["eval_reward_mean"] = round(statistics.fmean(eval_rewards), 4) if eval_rewards else 0.0
    stats["judge"] = {**judge_stats, "model": args.judge_model, "rubric": args.judge_rubric}
    if guided_stats:
        stats["guided"] = guided_stats
    if not rows:
        raise SystemExit(f"no usable groups: {stats}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    args.out.write_text(text, encoding="utf-8")
    manifest = {**stats, "examples": len(rows), "cases": len({r["case_id"] for r in rows}), "positive": sum(1 for r in rows if r["advantage"] > 0),
                "negative": sum(1 for r in rows if r["advantage"] < 0), "dataset_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "roots": [str(r) for r in args.root], "eps": args.eps, "clip": args.clip}
    args.out.with_suffix(".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


def _judge_path(run_dir: Path) -> Path:
    return run_dir / "mechanism_judge.json"


def _cached_judgement(run_dir: Path, args: argparse.Namespace) -> dict[str, Any] | None:
    """The cached grade, only when it came from the same judge model and rubric (entries before the rubric field are v1)."""
    path = _judge_path(run_dir)
    if not path.exists():
        return None
    entry = json.loads(path.read_text(encoding="utf-8"))
    case_id = run_dir.parent.name
    if not judge_cache_valid(entry, case_id, model=args.judge_model, rubric=args.judge_rubric):
        return None
    return entry


def _reward(run_dir: Path, score: dict[str, Any], args: argparse.Namespace, messages: list[dict[str, Any]] | None = None) -> float:
    if args.reward == "eval":
        return float(score.get("reward", 0.0))
    if messages is None:
        messages = [json.loads(line) for line in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    cached = _cached_judgement(run_dir, args)  # a grade from another judge model or rubric is not reused
    mech = cached.get("score") if cached else None
    root_term = getattr(args, "reward_root_term", "precision2")
    if root_term != "f1" and "root_precision" not in score:
        score = {**score, **_rescore_root(run_dir, score, args)}
    return rl_reward(score, mechanism=mech, forced=forced_answer(messages), status_terms=getattr(args, "reward_status_terms", False), root_term=root_term)


def _rescore_root(run_dir: Path, score: dict[str, Any], args: argparse.Namespace) -> dict[str, float]:
    """Precision / recall for runs scored before score.json carried them (from result.json + the golden contract)."""
    contract = getattr(args, "_contract", None)
    if contract is None:
        contract = args._contract = load_contract(Path(getattr(args, "contract", "configs/eval/train-family-v2.yaml")))
    result_path = run_dir / "result.json"
    expected = contract.get(score["case_id"])
    if expected is None or not result_path.exists():
        return {"root_precision": 0.0, "root_recall": 0.0}
    result = json.loads(result_path.read_text(encoding="utf-8"))
    precision, recall = root_precision_recall(expected.roots, candidates_from_result(result.get("answer") or result), expected.allowed_target_ids)
    return {"root_precision": precision, "root_recall": recall}


def _judge_all(groups: dict[str, list[tuple[Path, dict[str, Any]]]], args: argparse.Namespace, stats: dict[str, int]) -> None:
    """Grade the mechanism of every run with f1 > 0 (cached per run). On the Claude session limit, wait and retry."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    todo = []
    for case, runs in groups.items():
        reference = reference_cause(args.cases_root / case)
        for run_dir, score in runs:
            if float(score.get("root_f1", 0.0)) <= 0 or score.get("stop_reason") != "submitted":
                stats["skipped_f1_zero"] += 1
                continue
            if _cached_judgement(run_dir, args) is not None:
                stats["cached"] += 1
                continue
            result_path = run_dir / "result.json"
            if not result_path.exists():
                continue
            todo.append((run_dir, reference, conclusion_text(json.loads(result_path.read_text(encoding="utf-8")))))

    def one(item: tuple[Path, str, str]) -> None:
        run_dir, reference, conclusion = item
        for attempt in range(8):
            try:
                value, diag = judge_mechanism(reference, conclusion, model=args.judge_model, rubric=args.judge_rubric)
            except SessionLimitError as exc:
                print(f"  judge session limit ({exc}); waiting 20 min", flush=True)
                time.sleep(1200)
                continue
            if value is not None:
                _judge_path(run_dir).write_text(json.dumps({"score": value, "detail": diag, "model": args.judge_model, "rubric": args.judge_rubric, "reference": reference_version(run_dir.parent.name)}, ensure_ascii=False, indent=1), encoding="utf-8")
                stats["judged"] += 1
                return
            time.sleep(10 * (attempt + 1))
        stats["failed"] += 1
        print(f"  judge failed: {run_dir}", flush=True)

    with ThreadPoolExecutor(max_workers=args.judge_workers) as pool:
        list(pool.map(one, todo))


if __name__ == "__main__":
    main()
