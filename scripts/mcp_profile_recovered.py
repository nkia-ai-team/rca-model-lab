"""Profile recovered MCP datasets for a dataset-informed joint SFT recipe.

The report is structural only: it performs no judging, launches no training, and
does not mutate source datasets. It measures the current six-turn training
sampler against all eligible supervised turns so recipe changes can be grounded
in the recovered data rather than in dataset names alone.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET_DIR = ROOT / "outputs/data-recovery-codex-sol-20261007/datasets"
DEFAULT_OUT_ROOT = ROOT / "outputs/recipe-audit-20261008"
DEFAULT_TOKENIZER = ROOT / "models/muse-glimmer-30b-tokenizer/tokenizer.json"
DEFAULT_TURNS_PER_EPISODE = 6


class ProfileError(RuntimeError):
    """Recovered dataset profiling failed without touching source data."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ProfileError(f"invalid jsonl row in {path}:{line_no}: {error}") from error
    return rows


def quantiles(values: list[int | float]) -> dict[str, int | float]:
    if not values:
        return {"min": 0, "p50": 0, "p90": 0, "p95": 0, "max": 0, "mean": 0}
    ordered = sorted(values)

    def pct(q: float) -> int | float:
        index = round((len(ordered) - 1) * q)
        return ordered[index]

    return {
        "min": ordered[0],
        "p50": pct(0.50),
        "p90": pct(0.90),
        "p95": pct(0.95),
        "max": ordered[-1],
        "mean": round(statistics.fmean(values), 3),
    }


def tool_name(message: dict[str, Any]) -> str:
    calls = message.get("tool_calls") or []
    if not calls:
        return ""
    function = (calls[0] or {}).get("function") or {}
    return str(function.get("name") or "")


def tool_arguments(message: dict[str, Any]) -> str:
    calls = message.get("tool_calls") or []
    if not calls:
        return ""
    function = (calls[0] or {}).get("function") or {}
    return str(function.get("arguments") or "")


def tool_family(name: str) -> str:
    if name == "submit_rca":
        return "submit"
    if name.startswith("db_"):
        return "database"
    if name.startswith("get_trace") or name == "breakdown_endpoints":
        return "traces"
    if "metric" in name or name == "read_timeseries" or name == "compare_peers":
        return "metrics"
    if "log" in name:
        return "logs"
    if "event" in name or "change" in name or "k8s" in name:
        return "events_k8s_changes"
    if "target" in name or "topology" in name or name == "describe_data_sources":
        return "inventory_topology"
    if "process" in name or "host" in name:
        return "host_process"
    return name.split("_", 1)[0] if name else "unknown"


def supervised_assistant_turns(row: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    return [
        (index, message)
        for index, message in enumerate(row.get("messages") or [])
        if message.get("role") == "assistant"
        and message.get("tool_calls")
        and message.get("supervise") is not False
    ]


def all_assistant_turns(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [message for message in row.get("messages") or [] if message.get("role") == "assistant"]


def sample_indices(count: int, cap: int, seed: str) -> list[int]:
    if cap <= 0 or count <= cap:
        return list(range(count))
    picked = sorted(random.Random(seed).sample(list(range(count - 1)), cap - 1)) + [count - 1]
    return picked


def temporal_even_indices(count: int, cap: int) -> list[int]:
    if cap <= 0 or count <= cap:
        return list(range(count))
    if cap == 1:
        return [count - 1]
    picked = {count - 1}
    span = max(count - 2, 0)
    for step in range(cap - 1):
        picked.add(round(step * span / max(cap - 2, 1)))
    index = 0
    while len(picked) < cap and index < count:
        picked.add(index)
        index += 1
    return sorted(picked)


def greedy_unique_tool_indices(names: list[str], cap: int) -> list[int]:
    count = len(names)
    if cap <= 0 or count <= cap:
        return list(range(count))
    picked = {count - 1}
    seen = {names[-1]}
    for index, name in enumerate(names[:-1]):
        if name not in seen:
            picked.add(index)
            seen.add(name)
            if len(picked) >= cap:
                return sorted(picked)
    for index in temporal_even_indices(count, cap):
        picked.add(index)
        if len(picked) >= cap:
            return sorted(picked)
    return sorted(picked)


def pre_submit_unique_tool_indices(names: list[str], cap: int) -> list[int]:
    count = len(names)
    if cap <= 0 or count <= cap:
        return list(range(count))
    picked = {count - 1}
    for index, name in enumerate(names):
        if name == "submit_rca" and index > 0:
            picked.add(index - 1)
            if len(picked) >= cap:
                return sorted(picked)
    seen = {names[index] for index in picked}
    for index, name in enumerate(names):
        if name not in seen:
            picked.add(index)
            seen.add(name)
            if len(picked) >= cap:
                return sorted(picked)
    for index in temporal_even_indices(count, cap):
        picked.add(index)
        if len(picked) >= cap:
            return sorted(picked)
    return sorted(picked)


def policy_indices(policy: str, names: list[str], cap: int, seed: str) -> list[int]:
    if policy == "final_seeded_random":
        return sample_indices(len(names), cap, seed=seed)
    if policy == "final_temporal_even":
        return temporal_even_indices(len(names), cap)
    if policy == "final_greedy_unique_tool_then_temporal":
        return greedy_unique_tool_indices(names, cap)
    if policy == "final_pre_submit_unique_tool_then_temporal":
        return pre_submit_unique_tool_indices(names, cap)
    raise ProfileError(f"unknown turn-selection policy: {policy}")


def parse_json_dict(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def result_status(message: dict[str, Any]) -> str:
    parsed = parse_json_dict(str(message.get("content") or ""))
    status = parsed.get("status")
    return str(status) if status is not None else "unparsed"


def load_raw_tokenizer(tokenizer_json: Path | None) -> tuple[str, Any | None]:
    if tokenizer_json is None:
        return "disabled", None
    if not tokenizer_json.exists():
        return f"missing:{tokenizer_json}", None
    try:
        from tokenizers import Tokenizer
    except ImportError as error:  # pragma: no cover - depends on local optional package
        return f"unavailable:{type(error).__name__}", None
    try:
        return "raw_tokenizer_json_content_only", Tokenizer.from_file(str(tokenizer_json))
    except Exception as error:  # noqa: BLE001 - tokenizers Rust loader raises base Exception for invalid model variants.
        return f"unavailable:{type(error).__name__}:{error}", None


def token_len(tokenizer: Any | None, text: str) -> int | None:
    if tokenizer is None:
        return None
    return len(tokenizer.encode(text).ids)


def add_length(lengths: dict[str, list[int]], token_lengths: dict[str, list[int]], key: str, text: str, tokenizer: Any | None) -> None:
    lengths[key].append(len(text))
    tokens = token_len(tokenizer, text)
    if tokens is not None:
        token_lengths[key].append(tokens)


def summarize_counter(counter: Counter[str], limit: int = 30) -> dict[str, int]:
    return dict(counter.most_common(limit))


def profile_source(path: Path, rows: list[dict[str, Any]], *, turns_per_episode: int, tokenizer: Any | None) -> dict[str, Any]:
    tool_counts: Counter[str] = Counter()
    selected_tool_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    selected_family_counts: Counter[str] = Counter()
    final_tool_counts: Counter[str] = Counter()
    final_status_counts: Counter[str] = Counter()
    tool_result_status_counts: Counter[str] = Counter()
    root_f1_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    episode_counts_by_case: Counter[str] = Counter()
    selected_first_family = 0
    total_first_family = 0
    selected_first_tool = 0
    total_first_tool = 0
    selected_pre_submit = 0
    total_pre_submit = 0
    selected_submit = 0
    total_submit = 0
    selected_turns = 0
    eligible_turns = 0
    unsupervised_turns = 0
    all_assistant_count = 0
    length_chars: dict[str, list[int]] = defaultdict(list)
    length_tokens: dict[str, list[int]] = defaultdict(list)
    per_episode: dict[str, list[int]] = defaultdict(list)
    selected_examples: list[dict[str, Any]] = []
    policy_stats: dict[str, dict[int, dict[str, Any]]] = {
        policy: {
            cap: {
                "selected_turns": 0,
                "eligible_turns": 0,
                "tools": Counter(),
                "all_tools": Counter(),
                "families": Counter(),
                "all_families": Counter(),
                "selected_first_tool": 0,
                "total_first_tool": 0,
                "selected_first_family": 0,
                "total_first_family": 0,
                "selected_pre_submit": 0,
                "total_pre_submit": 0,
                "selected_submit": 0,
                "total_submit": 0,
            }
            for cap in (6, 8, 12)
        }
        for policy in (
            "final_seeded_random",
            "final_greedy_unique_tool_then_temporal",
            "final_pre_submit_unique_tool_then_temporal",
            "final_temporal_even",
        )
    }

    for row in rows:
        case_id = str(row.get("case_id") or "")
        run = str(row.get("run") or "")
        source_counts[str(row.get("source") or "")] += 1
        root_f1_counts[str(row.get("root_f1"))] += 1
        episode_counts_by_case[case_id] += 1
        assistants = all_assistant_turns(row)
        supervised = supervised_assistant_turns(row)
        selected_positions = set(sample_indices(len(supervised), turns_per_episode, seed=f"{case_id}/{run}"))
        selected_turns += len(selected_positions)
        eligible_turns += len(supervised)
        all_assistant_count += len(assistants)
        unsupervised_turns += sum(
            1 for message in assistants if message.get("tool_calls") and message.get("supervise") is False
        )
        per_episode["assistant_turns"].append(len(assistants))
        per_episode["eligible_turns"].append(len(supervised))
        per_episode["selected_turns"].append(len(selected_positions))
        per_episode["tool_calls_field"].append(int(row.get("tool_calls") or 0))

        first_family_seen: set[str] = set()
        first_tool_seen: set[str] = set()
        submit_positions: list[int] = []
        supervised_names = [tool_name(message) for _, message in supervised]
        for position, (message_index, message) in enumerate(supervised):
            name = tool_name(message)
            family = tool_family(name)
            selected = position in selected_positions
            tool_counts[name] += 1
            family_counts[family] += 1
            if selected:
                selected_tool_counts[name] += 1
                selected_family_counts[family] += 1
            if name == "submit_rca":
                submit_positions.append(position)
                total_submit += 1
                if selected:
                    selected_submit += 1
                    args = parse_json_dict(tool_arguments(message))
                    if "status" in args:
                        final_status_counts[str(args["status"])] += 1
            if family not in first_family_seen:
                first_family_seen.add(family)
                total_first_family += 1
                if selected:
                    selected_first_family += 1
            if name not in first_tool_seen:
                first_tool_seen.add(name)
                total_first_tool += 1
                if selected:
                    selected_first_tool += 1
            add_length(length_chars, length_tokens, "reasoning", str(message.get("reasoning_content") or ""), tokenizer)
            add_length(length_chars, length_tokens, "tool_arguments", tool_arguments(message), tokenizer)
            if selected:
                add_length(length_chars, length_tokens, "selected_reasoning", str(message.get("reasoning_content") or ""), tokenizer)
                add_length(length_chars, length_tokens, "selected_tool_arguments", tool_arguments(message), tokenizer)
            if len(selected_examples) < 20 and selected:
                selected_examples.append({"case_id": case_id, "run": run, "position": position, "message_index": message_index, "tool": name})
        if supervised:
            final_tool_counts[tool_name(supervised[-1][1])] += 1
        for submit_position in submit_positions:
            if submit_position > 0:
                total_pre_submit += 1
                if submit_position - 1 in selected_positions:
                    selected_pre_submit += 1
        for policy, caps in policy_stats.items():
            for cap, stats in caps.items():
                chosen = set(policy_indices(policy, supervised_names, cap, seed=f"{case_id}/{run}"))
                all_names = set(supervised_names)
                all_families = {tool_family(name) for name in supervised_names}
                stats["selected_turns"] += len(chosen)
                stats["eligible_turns"] += len(supervised_names)
                stats["all_tools"].update(all_names)
                stats["all_families"].update(all_families)
                stats["tools"].update(supervised_names[index] for index in chosen)
                stats["families"].update(tool_family(supervised_names[index]) for index in chosen)
                seen_tools: set[str] = set()
                seen_families: set[str] = set()
                for index, name in enumerate(supervised_names):
                    family = tool_family(name)
                    if name not in seen_tools:
                        seen_tools.add(name)
                        stats["total_first_tool"] += 1
                        if index in chosen:
                            stats["selected_first_tool"] += 1
                    if family not in seen_families:
                        seen_families.add(family)
                        stats["total_first_family"] += 1
                        if index in chosen:
                            stats["selected_first_family"] += 1
                    if name == "submit_rca":
                        stats["total_submit"] += 1
                        if index in chosen:
                            stats["selected_submit"] += 1
                        if index > 0:
                            stats["total_pre_submit"] += 1
                            if index - 1 in chosen:
                                stats["selected_pre_submit"] += 1
        for message in row.get("messages") or []:
            if message.get("role") == "tool":
                add_length(length_chars, length_tokens, "tool_results", str(message.get("content") or ""), tokenizer)
                tool_result_status_counts[result_status(message)] += 1

    unique_tools = set(tool_counts)
    selected_unique_tools = set(selected_tool_counts)
    unique_families = set(family_counts)
    selected_unique_families = set(selected_family_counts)
    cases = len(episode_counts_by_case)
    policy_comparison: dict[str, dict[str, Any]] = {}
    for policy, caps in policy_stats.items():
        policy_comparison[policy] = {}
        for cap, stats in caps.items():
            all_tools = set(stats["all_tools"])
            selected_tools = set(stats["tools"])
            all_families = set(stats["all_families"])
            selected_families = set(stats["families"])
            policy_comparison[policy][str(cap)] = {
                "eligible_turn_fraction_selected": round(stats["selected_turns"] / stats["eligible_turns"], 6) if stats["eligible_turns"] else 0,
                "unique_tool_fraction_selected": round(len(selected_tools) / len(all_tools), 6) if all_tools else 0,
                "unique_family_fraction_selected": round(len(selected_families) / len(all_families), 6) if all_families else 0,
                "first_tool_occurrence_fraction_selected": round(stats["selected_first_tool"] / stats["total_first_tool"], 6) if stats["total_first_tool"] else 0,
                "first_family_occurrence_fraction_selected": round(stats["selected_first_family"] / stats["total_first_family"], 6) if stats["total_first_family"] else 0,
                "submit_turn_fraction_selected": round(stats["selected_submit"] / stats["total_submit"], 6) if stats["total_submit"] else 0,
                "pre_submit_support_fraction_selected": round(stats["selected_pre_submit"] / stats["total_pre_submit"], 6) if stats["total_pre_submit"] else 0,
                "selected_turns": stats["selected_turns"],
                "eligible_turns": stats["eligible_turns"],
            }

    return {
        "source": path.stem,
        "dataset": str(path),
        "dataset_sha256": sha256_file(path),
        "examples": len(rows),
        "cases": cases,
        "episodes_per_case": quantiles(list(episode_counts_by_case.values())),
        "row_sources": summarize_counter(source_counts),
        "root_f1": summarize_counter(root_f1_counts),
        "turns": {
            "assistant_total": all_assistant_count,
            "eligible_supervised": eligible_turns,
            "unsupervised_tool_turns": unsupervised_turns,
            "selected_by_final_plus_random": selected_turns,
            "cap": turns_per_episode,
            "per_episode": {key: quantiles(value) for key, value in per_episode.items()},
        },
        "tools": {
            "all": summarize_counter(tool_counts),
            "selected": summarize_counter(selected_tool_counts),
            "families_all": summarize_counter(family_counts),
            "families_selected": summarize_counter(selected_family_counts),
            "unique_tools_all": len(unique_tools),
            "unique_tools_selected": len(selected_unique_tools),
            "unique_tools_missed_by_selection": sorted(unique_tools - selected_unique_tools),
            "unique_families_all": len(unique_families),
            "unique_families_selected": len(selected_unique_families),
            "unique_families_missed_by_selection": sorted(unique_families - selected_unique_families),
            "final_tool": summarize_counter(final_tool_counts),
            "final_submit_status_selected": summarize_counter(final_status_counts),
            "tool_result_status": summarize_counter(tool_result_status_counts),
        },
        "coverage": {
            "metric_scope": "structural turn-retention proxies only; first/pre-submit coverage does not judge semantic evidence quality",
            "eligible_turn_fraction_selected": round(selected_turns / eligible_turns, 6) if eligible_turns else 0,
            "unique_tool_fraction_selected": round(len(selected_unique_tools) / len(unique_tools), 6) if unique_tools else 0,
            "unique_family_fraction_selected": round(len(selected_unique_families) / len(unique_families), 6) if unique_families else 0,
            "first_tool_occurrence_fraction_selected": round(selected_first_tool / total_first_tool, 6) if total_first_tool else 0,
            "first_family_occurrence_fraction_selected": round(selected_first_family / total_first_family, 6) if total_first_family else 0,
            "submit_turn_fraction_selected": round(selected_submit / total_submit, 6) if total_submit else 0,
            "pre_submit_support_fraction_selected": round(selected_pre_submit / total_pre_submit, 6) if total_pre_submit else 0,
            "counts": {
                "first_tool_total": total_first_tool,
                "first_tool_selected": selected_first_tool,
                "first_family_total": total_first_family,
                "first_family_selected": selected_first_family,
                "submit_total": total_submit,
                "submit_selected": selected_submit,
                "pre_submit_support_total": total_pre_submit,
                "pre_submit_support_selected": selected_pre_submit,
            },
        },
        "policy_comparison": policy_comparison,
        "lengths": {
            "chars": {key: quantiles(value) for key, value in length_chars.items()},
            "tokens": {key: quantiles(value) for key, value in length_tokens.items()},
            "token_scope": "raw content only; not chat-template rendered full prefix/completion",
        },
        "selection_examples": selected_examples,
    }


def compact_findings(report: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    sources = report["sources"]
    total_cases = sum(source["cases"] for source in sources)
    total_examples = sum(source["examples"] for source in sources)
    findings.append(f"{len(sources)} sources, {total_cases} cases, {total_examples} episodes profiled.")
    for source in sources:
        coverage = source["coverage"]
        turns = source["turns"]
        findings.append(
            f"{source['source']}: {source['cases']} cases/{source['examples']} episodes; "
            f"selected {turns['selected_by_final_plus_random']}/{turns['eligible_supervised']} eligible turns "
            f"({coverage['eligible_turn_fraction_selected']:.1%}); "
            f"first-tool coverage {coverage['first_tool_occurrence_fraction_selected']:.1%}; "
            f"immediate-pre-submit turn retention {coverage['pre_submit_support_fraction_selected']:.1%}."
        )
    low = [
        source["source"]
        for source in sources
        if source["coverage"]["pre_submit_support_fraction_selected"] < 0.5
        or source["coverage"]["first_tool_occurrence_fraction_selected"] < 0.5
    ]
    if low:
        findings.append(
            "Six-turn sampling has low structural supervised-turn retention for first tool occurrences "
            "or the turn immediately before submit_rca in: " + ", ".join(low) + "."
        )
    for source in sources:
        baseline = source["policy_comparison"]["final_seeded_random"]["6"]
        greedy6 = source["policy_comparison"]["final_greedy_unique_tool_then_temporal"]["6"]
        support8 = source["policy_comparison"]["final_pre_submit_unique_tool_then_temporal"]["8"]
        findings.append(
            f"{source['source']} policy check: greedy unique-tool+temporal at cap6 raises first-tool "
            f"{baseline['first_tool_occurrence_fraction_selected']:.1%}->{greedy6['first_tool_occurrence_fraction_selected']:.1%}; "
            f"pre-submit+unique+temporal at cap8 reaches {support8['first_tool_occurrence_fraction_selected']:.1%} first-tool and "
            f"{support8['pre_submit_support_fraction_selected']:.1%} immediate-pre-submit turn retention."
        )
    return findings


def write_outputs(out_root: Path, report: dict[str, Any]) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    report_path = out_root / "profile.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# Recovered dataset profile", ""]
    for finding in report["findings"]:
        lines.append(f"- {finding}")
    lines.extend(["", "Machine report: `profile.json`", ""])
    (out_root / "profile-summary.md").write_text("\n".join(lines), encoding="utf-8")


def profile(dataset_dir: Path, out_root: Path, *, turns_per_episode: int, tokenizer_json: Path | None) -> dict[str, Any]:
    paths = sorted(dataset_dir.glob("*.jsonl"))
    if not paths:
        raise ProfileError(f"no recovered jsonl datasets under {dataset_dir}")
    token_status, tokenizer = load_raw_tokenizer(tokenizer_json)
    sources = [
        profile_source(path, read_jsonl(path), turns_per_episode=turns_per_episode, tokenizer=tokenizer)
        for path in paths
    ]
    report = {
        "schema_version": 1,
        "dataset_dir": str(dataset_dir),
        "turn_selection": {
            "name": "final_plus_seeded_random",
            "turns_per_episode": turns_per_episode,
            "seed": "case_id/run",
            "matches_training_function": "src/rca_lab/train/mcp_sft.py:sample_turns",
            "length_filter_not_simulated": True,
        },
        "tokenizer": {
            "status": token_status,
            "path": str(tokenizer_json) if tokenizer_json else "",
            "raw_tokenizer_sha256": sha256_file(tokenizer_json) if tokenizer_json and tokenizer_json.exists() else "",
        },
        "sources": sources,
        "source_sha256": {source["source"]: source["dataset_sha256"] for source in sources},
    }
    report["findings"] = compact_findings(report)
    write_outputs(out_root, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR)
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    parser.add_argument("--turns-per-episode", type=int, default=DEFAULT_TURNS_PER_EPISODE)
    parser.add_argument("--tokenizer-json", type=Path, default=DEFAULT_TOKENIZER)
    parser.add_argument("--no-tokenizer", action="store_true", help="skip raw content token counts")
    args = parser.parse_args(argv)
    try:
        report = profile(
            args.dataset_dir,
            args.out_root,
            turns_per_episode=args.turns_per_episode,
            tokenizer_json=None if args.no_tokenizer else args.tokenizer_json,
        )
    except ProfileError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"sources": [source["source"] for source in report["sources"]], "findings": report["findings"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
