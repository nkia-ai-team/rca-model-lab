"""Typed, deterministic scoring for RCA runtime episode artifacts."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from rca_lab.harness.models import StrictModel
from rca_lab.harness.scorer import (
    RootCandidate,
    RootExpectation,
)
from rca_lab.harness.scorer import (
    root_f1 as typed_root_f1,
)

ExpectedRoot = RootExpectation


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


def load_episode_text(text: str, *, source: str = "episode payload") -> dict[str, Any]:
    completed = [
        value
        for line in text.splitlines()
        if (value := json.loads(line)).get("event") == "episode_completed"
    ]
    if len(completed) != 1:
        raise ValueError(f"expected one episode_completed event: {source}")
    return completed[0]


def load_episode(path: Path) -> dict[str, Any]:
    return load_episode_text(path.read_text(encoding="utf-8"), source=str(path))


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


def root_tokens(result: dict[str, Any], names: dict[str, str]) -> list[RootCandidate]:
    roots = [
        RootCandidate(
            variant="target",
            target_id=str(cause.get("target", "")),
            target_name=names.get(
                str(cause.get("target", "")), str(cause.get("target", ""))
            ),
        )
        for cause in result.get("causes", [])
    ]
    roots.extend(
        RootCandidate(
            variant="pseudo",
            pseudo_id=str(cause.get("id", "")),
            pseudo_kind=str(cause.get("kind", "")),
            boundary_target=str(cause.get("boundary_target", "")),
        )
        for cause in result.get("external_causes", [])
    )
    return roots


def root_f1(
    expected: list[dict[str, Any]] | tuple[RootExpectation, ...],
    actual: list[dict[str, Any]] | list[RootCandidate],
) -> float:
    expected_roots = [
        item
        if isinstance(item, RootExpectation)
        else RootExpectation.model_validate(item)
        for item in expected
    ]
    actual_roots = [
        item if isinstance(item, RootCandidate) else RootCandidate.model_validate(item)
        for item in actual
    ]
    return typed_root_f1(expected_roots, actual_roots)


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


def diagnosis_optimization_reward(
    *,
    root_f1: float,
    proof_rate: float,
    status_correct: bool,
    strict_correct: bool,
    unsupported_confirmation: int,
    format_errors: int,
) -> float:
    """Return the diagnosis-only reward used for policy optimization.

    Efficiency and tool-call success remain evaluation diagnostics. They cannot
    rank incorrect episodes during RL because that would reward premature or
    mechanically valid investigation paths without a grounded diagnosis.
    """

    root_exact = float(root_f1 == 1.0)
    return max(
        0.0,
        min(
            1.0,
            0.55 * root_f1
            + 0.15 * root_exact
            + 0.15 * proof_rate * root_f1
            + 0.10 * float(status_correct) * root_f1
            + 0.05 * float(strict_correct)
            - 0.60 * float(unsupported_confirmation)
            - 0.10 * float(min(1, format_errors)),
        ),
    )


def exploration_bootstrap_reward(
    *,
    diagnosis_reward: float,
    root_target_coverage: float,
    root_observed_evidence_refs: int,
    root_grounded_answer_refs: int,
    rejected_actions: int,
    action_count: int,
    unsupported_confirmation: int,
    format_errors: int,
) -> float:
    """Reward grounded investigation before switching to diagnosis-only RL.

    The bootstrap stage never rewards a tool name, turn count, generic evidence
    volume, or a successful RPC by itself. Process credit requires a successful
    observation whose typed target is one of the hidden expected root or
    external-boundary targets. This gives an all-zero diagnosis group relative
    signal without reinforcing broad, irrelevant scans.
    """

    rejected_rate = rejected_actions / max(1, action_count)
    return max(
        0.0,
        min(
            1.0,
            0.60 * diagnosis_reward
            + 0.20 * root_target_coverage
            + 0.10 * min(1.0, root_observed_evidence_refs / 4)
            + 0.10 * min(1.0, root_grounded_answer_refs / 2)
            - 0.15 * rejected_rate
            - 0.25 * float(unsupported_confirmation)
            - 0.10 * float(min(1, format_errors)),
        ),
    )


def _expected_probe_targets(expected: dict[str, Any]) -> tuple[frozenset[str], ...]:
    """Return one canonical probe-target set per expected root identity."""

    groups = []
    for root in expected["roots"]:
        target_ids = root.get("target_ids") or root.get("boundary_target_ids") or []
        groups.append(frozenset(map(str, target_ids)))
    return tuple(groups)


def _observation_target(item: dict[str, Any]) -> str:
    target = str(item.get("target", ""))
    if target:
        return target
    query = item.get("query")
    return str(query.get("target", "")) if isinstance(query, dict) else ""


def score_episode(case_id: str, expected: dict[str, Any], episode: dict[str, Any]) -> dict[str, Any]:
    result = episode.get("result", {})
    names = target_names(episode)
    roots_score = root_f1(expected["roots"], root_tokens(result, names))
    transient_format_errors = count_format_errors(episode.get("ledger", []))
    causes = result.get("causes", [])
    external_causes = result.get("external_causes", [])
    diagnoses = [*causes, *external_causes]
    proofs_valid = bool(causes) and all(cause.get("proof_valid") for cause in causes)
    proof_rate = (
        sum(bool(cause.get("proof_valid")) for cause in causes) / len(causes)
        if causes
        else 0
    )
    ledger = episode.get("ledger", [])
    known_refs = {
        str(ref)
        for item in ledger
        for ref in [item.get("id"), *item.get("evidence_refs", [])]
        if ref
    }
    observed_evidence_refs = {
        str(ref)
        for item in ledger
        if item.get("ok")
        for ref in item.get("evidence_refs", [])
        if ref
    }
    cause_targets = {str(cause.get("target", "")) for cause in causes}
    internal_evidence_complete = all(
        bool(str(cause.get("mechanism", "")).strip())
        and bool(cause.get("support_refs"))
        and set(map(str, cause.get("support_refs", []))).issubset(known_refs)
        and set(map(str, cause.get("counter_refs", []))).issubset(known_refs)
        for cause in causes
    )
    external_evidence_complete = all(
        bool(str(cause.get("id", "")).strip())
        and bool(str(cause.get("name", "")).strip())
        and str(cause.get("boundary_target", "")) in cause_targets
        and bool(cause.get("evidence_refs"))
        and set(map(str, cause.get("evidence_refs", []))).issubset(known_refs)
        for cause in external_causes
    )
    evidence_complete = bool(diagnoses) and bool(causes) and (
        internal_evidence_complete and external_evidence_complete
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
    turns = int(result.get("turns", len(ledger)))
    actions = [str(item["action"]) for item in ledger if item.get("action")]
    rejected_actions = sum(not bool(item.get("ok")) for item in ledger)
    grounded_answer_refs = {
        str(ref)
        for cause in causes
        for ref in [*cause.get("support_refs", []), *cause.get("counter_refs", [])]
        if str(ref) in known_refs
    } | {
        str(ref)
        for cause in external_causes
        for ref in cause.get("evidence_refs", [])
        if str(ref) in known_refs
    }
    expected_probe_targets = _expected_probe_targets(expected)
    root_observations = [
        item
        for item in ledger
        if item.get("ok")
        and item.get("evidence_refs")
        and any(
            _observation_target(item) in target_group
            for target_group in expected_probe_targets
        )
    ]
    root_observed_evidence_refs = {
        str(ref)
        for item in root_observations
        for ref in item.get("evidence_refs", [])
        if ref
    }
    observed_root_targets = {_observation_target(item) for item in root_observations}
    root_target_coverage = (
        sum(bool(target_group & observed_root_targets) for target_group in expected_probe_targets)
        / len(expected_probe_targets)
        if expected_probe_targets
        else 0.0
    )
    root_grounded_answer_refs = grounded_answer_refs & root_observed_evidence_refs
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
    optimization_reward = diagnosis_optimization_reward(
        root_f1=roots_score,
        proof_rate=proof_rate,
        status_correct=status_correct,
        strict_correct=strict,
        unsupported_confirmation=unsupported,
        format_errors=transient_format_errors,
    )
    bootstrap_reward = exploration_bootstrap_reward(
        diagnosis_reward=optimization_reward,
        root_target_coverage=root_target_coverage,
        root_observed_evidence_refs=len(root_observed_evidence_refs),
        root_grounded_answer_refs=len(root_grounded_answer_refs),
        rejected_actions=rejected_actions,
        action_count=len(actions),
        unsupported_confirmation=unsupported,
        format_errors=transient_format_errors,
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
        "optimization_reward": optimization_reward,
        "exploration_bootstrap_reward": bootstrap_reward,
        "observed_evidence_refs": len(observed_evidence_refs),
        "grounded_answer_refs": len(grounded_answer_refs),
        "root_target_coverage": root_target_coverage,
        "root_observed_evidence_refs": len(root_observed_evidence_refs),
        "root_grounded_answer_refs": len(root_grounded_answer_refs),
        "turns": turns,
        "actions": actions,
        "distinct_actions": len(set(actions)),
        "used_metric_action": any(action in _METRIC_ACTIONS for action in actions),
        "used_specialized_probe": any(action.startswith("probe_") for action in actions),
        "rejected_actions": rejected_actions,
    }


def score_directory(output: Path, contract: dict[str, Any], runs_per_case: int) -> dict[str, Any]:
    if runs_per_case < 1:
        raise ValueError("runs_per_case must be positive")
    rows: list[dict[str, Any]] = []
    invalid_runs: list[str] = []
    for case_id, expected in contract["cases"].items():
        trajectories = sorted((output / case_id).glob("traj-run*/agent-*.jsonl"))
        counts = Counter(path.parent.name for path in trajectories)
        expected_names = {f"traj-run{index}" for index in range(1, runs_per_case + 1)}
        for run_name in sorted(expected_names | set(counts)):
            expected_count = 1 if run_name in expected_names else 0
            actual_count = counts[run_name]
            if actual_count != expected_count:
                invalid_runs.append(
                    f"{case_id}/{run_name}={actual_count}/{expected_count}"
                )
        for path in trajectories:
            rows.append(
                {
                    **score_episode(case_id, expected, load_episode(path)),
                    "run": path.parent.name,
                }
            )
    if invalid_runs:
        raise ValueError(
            "evaluation run set is incomplete or duplicated: " + ", ".join(invalid_runs)
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
