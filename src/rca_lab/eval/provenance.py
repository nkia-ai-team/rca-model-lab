"""Comparable evaluation provenance shared by monitor and final gates."""

from __future__ import annotations

from typing import Any

COMPARABLE_EVALUATION_FIELDS = (
    "agent_sha256",
    "restore_sha256",
    "split_sha256",
    "case_set_sha256",
    "scoring_contract_sha256",
    "structured_output_backend",
    "actor_temperature",
    "actor_seed",
    "reasoning_strength",
    "request_contract_enforced",
    "restore_timeout_seconds",
    "partition",
    "runs",
    "cases",
)


def provenance_failures(reference: dict[str, Any], candidate: dict[str, Any]) -> tuple[str, ...]:
    """Return every missing or mismatched like-for-like evaluation field."""
    failures: list[str] = []
    reference_provenance = reference.get("evaluation_provenance", {})
    candidate_provenance = candidate.get("evaluation_provenance", {})
    for key in COMPARABLE_EVALUATION_FIELDS:
        if key not in reference_provenance or key not in candidate_provenance:
            failures.append(f"missing comparable provenance: {key}")
        elif reference_provenance[key] != candidate_provenance[key]:
            failures.append(f"evaluation provenance mismatch: {key}")
    return tuple(failures)
