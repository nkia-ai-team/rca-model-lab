"""Typed, deterministic scoring for RCA runtime episode artifacts."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from rca_lab.harness.models import StrictModel


class ExpectedRoot(StrictModel):
    target_aliases: tuple[str, ...] = ()
    pseudo_kind: Literal[
        "external_dependency", "kafka", "redis", "network", "capacity_limit"
    ] | None = None

    @model_validator(mode="after")
    def one_identity(self) -> ExpectedRoot:
        if bool(self.target_aliases) == bool(self.pseudo_kind):
            raise ValueError("root must define exactly one of target_aliases or pseudo_kind")
        return self


class ExpectedCase(StrictModel):
    expected_status: Literal["confirmed", "provisional", "insufficient"]
    roots: tuple[ExpectedRoot, ...] = Field(min_length=1)


class EvalContract(StrictModel):
    version: int = 1
    cases: dict[str, ExpectedCase] = Field(min_length=1)


_METRIC_ACTIONS = frozenset(
    {
        "discover_metrics",
        "probe_metric",
        "metric_describe",
        "metric_overview",
        "metric_fetch_raw",
        "metric_fetch_window",
        "metric_compare",
    }
)
_NAMED_TARGET = re.compile(r"([0-9a-f]{8}-[0-9a-f-]{27}) \(([^)]*)\)", re.IGNORECASE)
_MAIN_TARGET = re.compile(r"main:\s*([^\n(]+)\(([0-9a-f-]{36})\)", re.IGNORECASE)


def load_episode(path: Path) -> dict[str, Any]:
    completed = [
        value
        for line in path.read_text(encoding="utf-8").splitlines()
        if (value := json.loads(line)).get("event") == "episode_completed"
    ]
    if len(completed) != 1:
        raise ValueError(f"expected one episode_completed event: {path}")
    return completed[0]


def target_names(episode: dict[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for prompt in episode.get("prompts", []):
        for message in prompt.get("Messages", []):
            content = str(message.get("content", ""))
            for target_id, name in _NAMED_TARGET.findall(content):
                if name.strip():
                    names[target_id] = name.strip()
            for name, target_id in _MAIN_TARGET.findall(content):
                names[target_id] = name.strip()
    return names


def root_tokens(result: dict[str, Any], names: dict[str, str]) -> list[tuple[str, str]]:
    roots = [
        ("target", names.get(str(cause.get("target", "")), str(cause.get("target", ""))))
        for cause in result.get("causes", [])
    ]
    roots.extend(
        ("pseudo", str(cause.get("kind", ""))) for cause in result.get("external_causes", [])
    )
    return roots


def root_matches(expected: dict[str, Any], actual: tuple[str, str]) -> bool:
    kind, value = actual
    if "pseudo_kind" in expected:
        return kind == "pseudo" and value == expected["pseudo_kind"]
    aliases = [str(item).casefold() for item in expected.get("target_aliases", [])]
    folded = value.casefold()
    return kind == "target" and any(alias in folded or folded in alias for alias in aliases)


def root_f1(expected: list[dict[str, Any]], actual: list[tuple[str, str]]) -> float:
    remaining = list(actual)
    hits = 0
    for root in expected:
        match = next(
            (index for index, value in enumerate(remaining) if root_matches(root, value)), None
        )
        if match is not None:
            hits += 1
            remaining.pop(match)
    if not expected:
        return float(not actual)
    precision = hits / max(1, len(actual))
    recall = hits / len(expected)
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def count_format_errors(ledger: list[dict[str, Any]]) -> int:
    """Count malformed structured actions, not typed semantic-policy rejections."""
    total = 0
    for item in ledger:
        kind = str(item.get("error_kind", ""))
        if kind:
            total += kind == "schema_format"
            continue
        # Compatibility for pre-error_kind episodes. Proof validation happens
        # after JSON/schema parsing and therefore is not a format failure.
        summary = str(item.get("summary", ""))
        total += (
            "structured response validation failed" in summary
            and " proof not satisfied:" not in summary
        )
    return total


def score_episode(case_id: str, expected: dict[str, Any], episode: dict[str, Any]) -> dict[str, Any]:
    result = episode.get("result", {})
    names = target_names(episode)
    roots_score = root_f1(expected["roots"], root_tokens(result, names))
    transient_format_errors = count_format_errors(episode.get("ledger", []))
    causes = result.get("causes", [])
    external_causes = result.get("external_causes", [])
    diagnoses = [*causes, *external_causes]
    proofs_valid = bool(diagnoses) and all(cause.get("proof_valid") for cause in diagnoses)
    proof_rate = (
        sum(bool(cause.get("proof_valid")) for cause in diagnoses) / len(diagnoses)
        if diagnoses
        else 0
    )
    evidence_complete = bool(diagnoses) and all(
        bool(str(cause.get("mechanism", "")).strip()) and bool(cause.get("support_refs"))
        for cause in diagnoses
    )
    unsupported = int(result.get("status") == "confirmed" and (roots_score < 1 or not proofs_valid))
    status_correct = result.get("status") == expected["expected_status"]
    strict = bool(
        roots_score == 1
        and status_correct
        and evidence_complete
        and transient_format_errors == 0
        and unsupported == 0
    )
    ledger = episode.get("ledger", [])
    turns = int(result.get("turns", len(ledger)))
    actions = [str(item["action"]) for item in ledger if item.get("action")]
    rejected_actions = sum(not bool(item.get("ok")) for item in ledger)
    efficiency = max(0.0, 1 - max(0, turns - 1) / 12)
    tool_success = sum(bool(item.get("ok")) for item in ledger) / len(ledger) if ledger else 0
    reward = max(
        0.0,
        min(
            1.0,
            0.45 * roots_score
            + 0.25 * proof_rate
            + 0.15 * float(status_correct)
            + 0.10 * efficiency
            + 0.05 * tool_success
            - 0.50 * unsupported
            - 0.05 * min(1, transient_format_errors),
        ),
    )
    return {
        "case_id": case_id,
        "status": result.get("status", "missing"),
        "root_f1": roots_score,
        "strict_correct": strict,
        "format_errors": transient_format_errors,
        "unsupported_confirmation": unsupported,
        "target_names": names,
        "proof_rate": proof_rate,
        "evidence_complete": evidence_complete,
        "reward": reward,
        "turns": turns,
        "actions": actions,
        "distinct_actions": len(set(actions)),
        "used_metric_action": any(action in _METRIC_ACTIONS for action in actions),
        "used_specialized_probe": any(action.startswith("probe_") for action in actions),
        "rejected_actions": rejected_actions,
    }


def score_directory(output: Path, contract: dict[str, Any], runs_per_case: int) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for case_id, expected in contract["cases"].items():
        trajectories = sorted((output / case_id).glob("traj-run*/agent-*.jsonl"))
        for path in trajectories:
            rows.append(
                {
                    **score_episode(case_id, expected, load_episode(path)),
                    "run": path.parent.name,
                }
            )
    majority = sum(
        sum(row["strict_correct"] for row in rows if row["case_id"] == case_id)
        >= runs_per_case // 2 + 1
        for case_id in contract["cases"]
    )
    action_counts = Counter(action for row in rows for action in row["actions"])
    return {
        "completed_runs": len(rows),
        "format_errors": sum(row["format_errors"] for row in rows),
        "unsupported_confirmations": sum(row["unsupported_confirmation"] for row in rows),
        "strict_correct_runs": sum(row["strict_correct"] for row in rows),
        "majority_strict_correct": majority,
        "evidence_complete_runs": sum(row["evidence_complete"] for row in rows),
        "mean_proof_rate": (
            sum(row["proof_rate"] for row in rows) / len(rows) if rows else 0.0
        ),
        "mean_reward": sum(row["reward"] for row in rows) / len(rows) if rows else 0.0,
        "mean_root_f1": sum(row["root_f1"] for row in rows) / len(rows) if rows else 0.0,
        "behavior": {
            "action_counts": dict(sorted(action_counts.items())),
            "runs_using_metric_actions": sum(row["used_metric_action"] for row in rows),
            "runs_using_specialized_probes": sum(
                row["used_specialized_probe"] for row in rows
            ),
            "mean_distinct_actions": (
                sum(row["distinct_actions"] for row in rows) / len(rows) if rows else 0.0
            ),
            "mean_rejected_actions": (
                sum(row["rejected_actions"] for row in rows) / len(rows) if rows else 0.0
            ),
            "mean_turns": sum(row["turns"] for row in rows) / len(rows) if rows else 0.0,
        },
        "runs": rows,
    }
