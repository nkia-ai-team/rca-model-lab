"""Golden root-cause contract (configs/eval/*.yaml) and typed root matching.

Ported from the thinkfl harness scorer so results on the new tool surface stay comparable with
the old sealed-eval numbers: an internal root is matched by canonical target id, a pseudo root by
kind + canonical external id + boundary target.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

PseudoKind = Literal["external_dependency", "kafka", "redis", "network", "capacity_limit"]


class RootExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_ids: tuple[str, ...] = ()
    target_aliases: tuple[str, ...] = ()
    proof_types: tuple[str, ...] = ()
    pseudo_kind: PseudoKind | None = None
    pseudo_ids: tuple[str, ...] = ()
    boundary_target_ids: tuple[str, ...] = ()
    boundary_target_aliases: tuple[str, ...] = ()
    # Internal target ids that *are* the external system inside the capture (testbed mocks):
    # naming that target as a cause satisfies this pseudo root on the MCP surface.
    proxy_target_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def complete_identity(self) -> RootExpectation:
        internal = bool(self.target_ids or self.target_aliases)
        pseudo = bool(self.pseudo_kind or self.pseudo_ids or self.boundary_target_ids or self.boundary_target_aliases)
        if internal == pseudo:
            raise ValueError("root must define exactly one internal or pseudo identity")
        if internal and not self.target_ids:
            raise ValueError("internal root requires canonical target_ids")
        if pseudo and not (self.pseudo_kind and self.pseudo_ids and self.boundary_target_ids):
            raise ValueError("pseudo root requires kind, canonical id, and canonical boundary target id")
        return self


class RootCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    variant: Literal["target", "pseudo"]
    target_id: str = ""
    pseudo_id: str = ""
    pseudo_name: str = ""
    pseudo_kind: str = ""
    boundary_target: str = ""


class ExpectedCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_status: Literal["confirmed", "provisional", "insufficient"]
    roots: tuple[RootExpectation, ...] = Field(min_length=1)
    # Targets of a parallel, load-induced bottleneck that is real in the capture but is not the injected
    # fault (2026-10-01 audit: f07-h cart, f19/f10-h dispatch). Naming them is neither a hit nor a false
    # positive; the injected roots are still required for root_f1 1.
    allowed_target_ids: tuple[str, ...] = ()
    allowed_note: str = ""


def load_contract(path: Path) -> dict[str, ExpectedCase]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {case_id: ExpectedCase.model_validate(spec) for case_id, spec in raw["cases"].items()}


_GENERIC_TOKENS = frozenset({"external", "mock", "testbed", "dependency", "service", "api", "svc"})


def _identity_tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", value.lower().removeprefix("external:")) if len(token) >= 2 and token not in _GENERIC_TOKENS}


def pseudo_identity_matches(candidate_id: str, candidate_name: str, expected_ids: tuple[str, ...]) -> bool:
    """External ids are free text on the MCP surface (no candidate enum), so match on identity
    tokens: `external:pg-rate-limit` ↔ `external:external-pg` share `pg`."""
    if candidate_id in expected_ids:
        return True
    candidate_tokens = _identity_tokens(candidate_id) | _identity_tokens(candidate_name)
    return any(candidate_tokens & _identity_tokens(expected) for expected in expected_ids)


def root_matches(expected: RootExpectation, actual: RootCandidate) -> bool:
    if expected.target_ids:
        return actual.variant == "target" and actual.target_id in expected.target_ids
    if actual.variant == "target":
        return actual.target_id in expected.proxy_target_ids
    return bool(
        actual.variant == "pseudo"
        and actual.pseudo_kind == expected.pseudo_kind
        and actual.boundary_target in expected.boundary_target_ids
        and pseudo_identity_matches(actual.pseudo_id, actual.pseudo_name, expected.pseudo_ids)
    )


def root_precision_recall(expected: tuple[RootExpectation, ...], actual: list[RootCandidate], allowed: tuple[str, ...] = ()) -> tuple[float, float]:
    """(precision, recall) of the submitted candidates against the golden roots. Candidates on an allowed
    parallel-bottleneck target (ExpectedCase.allowed_target_ids) that match no root are left out."""
    if allowed:
        actual = [c for c in actual if not (c.variant == "target" and c.target_id in allowed and not any(root_matches(r, c) for r in expected))]
    remaining = list(actual)
    hits = 0
    for root in expected:
        match = next((index for index, candidate in enumerate(remaining) if root_matches(root, candidate)), None)
        if match is not None:
            hits += 1
            remaining.pop(match)
    if not expected:
        empty = float(not actual)
        return empty, empty
    # Candidates left over that match a root already hit name the same cause at another layer
    # (application vs its k8s deployment/pod); they are duplicates, not false positives.
    duplicates = sum(any(root_matches(root, candidate) for root in expected) for candidate in remaining)
    return hits / max(1, len(actual) - duplicates), hits / len(expected)


def root_f1(expected: tuple[RootExpectation, ...], actual: list[RootCandidate], allowed: tuple[str, ...] = ()) -> float:
    precision, recall = root_precision_recall(expected, actual, allowed)
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def candidates_from_result(result: dict[str, Any]) -> list[RootCandidate]:
    roots = [RootCandidate(variant="target", target_id=str(cause.get("target", ""))) for cause in result.get("causes", [])]
    roots.extend(
        RootCandidate(
            variant="pseudo",
            pseudo_id=str(cause.get("id", "")),
            pseudo_name=str(cause.get("name", "")),
            pseudo_kind=str(cause.get("kind", "")),
            boundary_target=str(cause.get("boundary_target", "")),
        )
        for cause in result.get("external_causes", [])
    )
    return roots
