"""Build completion-only SFT records from accepted blind-teacher trajectories."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from rca_lab.eval.scoring import EvalContract, ExpectedCase, root_f1
from rca_lab.harness.models import ActionRequest, StrictModel
from rca_lab.scenarios.split import TeacherSplit


class SFTMessage(StrictModel):
    role: str
    content: str


class SFTTurn(StrictModel):
    turn: int = Field(ge=1)
    messages: tuple[SFTMessage, SFTMessage, SFTMessage]


class SFTRecord(StrictModel):
    turns: tuple[SFTTurn, ...] = Field(min_length=1)
    scenario_id: str
    trajectory_id: str
    turn_count: int = Field(ge=1)


class SFTDatasetManifest(StrictModel):
    version: int = 1
    scenario_count: int = Field(ge=1)
    trajectory_count: int = Field(ge=1)
    turn_count: int = Field(ge=1)
    failed_observation_count: int = Field(ge=0)
    scenarios: tuple[str, ...]
    trajectories_per_scenario: dict[str, int]
    trajectory_sha256: dict[str, str]
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminal_contract_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    curation_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    selection_contract: str = "curated_top_level_runtime_valid_v1"

    @model_validator(mode="after")
    def counts_match(self) -> SFTDatasetManifest:
        if self.scenario_count != len(self.scenarios):
            raise ValueError("scenario_count does not match scenarios")
        if self.trajectory_count != sum(self.trajectories_per_scenario.values()):
            raise ValueError("trajectory_count does not match trajectories_per_scenario")
        if len(self.trajectory_sha256) != self.trajectory_count:
            raise ValueError("trajectory_sha256 count does not match trajectory_count")
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in self.trajectory_sha256.values()):
            raise ValueError("trajectory_sha256 contains an invalid digest")
        return self


def _trajectory_files(root: Path) -> tuple[Path, ...]:
    # Accepted artifacts live directly under the scenario directory. Recursive failed branches
    # live below branches/ and are deliberately excluded from SFT.
    return tuple(sorted(path for path in root.glob("case-*/*.episode*.jsonl") if path.is_file()))


_REFERENCE_RE = re.compile(r"\b(?:obs-[0-9]{3,}|ev[0-9]+)\b")


def sft_records_payload(records: tuple[SFTRecord, ...]) -> str:
    return "".join(record.model_dump_json() + "\n" for record in records)


def _external_kind(original: dict[str, Any]) -> str:
    """Map the pre-registry teacher wire format onto the current enum."""
    value = str(original.get("kind") or original.get("type") or "external_dependency")
    return {
        "http_upstream_dependency": "external_dependency",
        "external_http_dependency": "external_dependency",
        "external_service": "external_dependency",
    }.get(value, value)


def _external_id(original: dict[str, Any]) -> str:
    existing = str(original.get("id") or "")
    if existing.startswith("external:"):
        return existing
    name = re.sub(r"[^a-z0-9]+", "-", str(original.get("name") or "dependency").casefold())
    return "external:" + name.strip("-")


def _references(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(_REFERENCE_RE.findall(value))
    if isinstance(value, (list, tuple)):
        return tuple(
            ref
            for item in value
            for ref in _references(item)
        )
    return ()


def _expected_pseudo_root(
    *,
    original: dict[str, Any],
    boundary_target: str,
    expected_case: ExpectedCase | None,
) -> Any | None:
    if expected_case is None:
        return None
    kind = _external_kind(original)
    candidates = [
        root
        for root in expected_case.roots
        if root.pseudo_kind == kind
        and (not boundary_target or boundary_target in root.boundary_target_ids)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _canonical_action(
    raw: dict[str, Any],
    user_prompt: str,
    prior_actions: tuple[str, ...],
    expected_case: ExpectedCase | None = None,
) -> dict[str, Any]:
    """Lift accepted legacy teacher actions onto the current runtime schema."""
    query = {
        key: value
        for key, value in dict(raw.get("query") or {}).items()
        if key in {"target", "source", "kind", "from", "to", "limit"}
    }
    if query.get("limit", 0) > 12:
        query["limit"] = 12
    if query.get("limit", 1) < 1:
        query.pop("limit", None)

    source_answer = dict(raw.get("answer") or {})
    legacy_summary = str(source_answer.get("text") or source_answer.get("summary") or "")
    if not legacy_summary and source_answer.get("causes"):
        legacy_summary = str(source_answer["causes"][0].get("mechanism") or "")
    answer: dict[str, Any] = {
        "status": source_answer.get("status") or "insufficient",
        "causes": [],
        "culprits": list(source_answer.get("culprits") or []),
        "external_causes": [],
        "ready": bool(source_answer.get("ready", False)),
        "text": legacy_summary,
    }

    visible_refs = set(_REFERENCE_RE.findall(user_prompt))
    for original in source_answer.get("causes") or []:
        claim = str(original.get("claim") or original.get("mechanism") or "")
        proof_type = str(original.get("proof_type") or "")
        if not proof_type:
            proof_type = (
                "db_blocking"
                if {"probe_db_blocking", "probe_topsql"} <= set(prior_actions)
                else "unknown"
            )
        cited = [ref for ref in _REFERENCE_RE.findall(claim) if ref in visible_refs]
        support_refs = [ref for ref in original.get("support_refs") or [] if ref in visible_refs]
        if not support_refs:
            if not cited:
                observations = sorted(ref for ref in visible_refs if ref.startswith("obs-"))
                cited = observations[-1:] if observations else sorted(visible_refs)[:1]
            support_refs = cited
        if not support_refs:
            if answer["ready"]:
                raise ValueError("ready teacher cause has no visible support reference")
            continue
        answer["causes"].append(
            {
                "target": str(original.get("target") or original.get("target_id") or ""),
                "confidence": float(original.get("confidence", 0.5)),
                "claim": claim,
                "mechanism": str(original.get("mechanism") or claim),
                "proof_type": proof_type,
                "support_refs": support_refs,
                "counter_refs": [
                    ref for ref in original.get("counter_refs") or [] if ref in visible_refs
                ],
            }
        )
    for original in source_answer.get("external_causes") or []:
        refs = list(
            dict.fromkeys(
                ref
                for field in (
                    "evidence_refs",
                    "support_refs",
                    "evidence",
                    "claim",
                    "detail",
                    "behavior",
                    "status_code_raw",
                    "note",
                )
                for ref in _references(original.get(field))
                if ref in visible_refs
            )
        )
        if not refs:
            continue
        boundary_target = str(
            original.get("boundary_target")
            or original.get("boundary_target_id")
            or original.get("via")
            or ""
        )
        if not boundary_target and answer["causes"]:
            # The earliest teacher format attached the boundary only to the
            # adjacent internal cause.  Preserve that unambiguous relationship.
            boundary_target = str(answer["causes"][0]["target"])
        expected_root = _expected_pseudo_root(
            original=original,
            boundary_target=boundary_target,
            expected_case=expected_case,
        )
        external_id = _external_id(original)
        external_kind = _external_kind(original)
        if expected_root is not None:
            external_id = expected_root.pseudo_ids[0]
            external_kind = str(expected_root.pseudo_kind)
            if not boundary_target:
                boundary_target = expected_root.boundary_target_ids[0]
        answer["external_causes"].append(
            {
                "id": external_id,
                "kind": external_kind,
                "name": str(original.get("name") or ""),
                "boundary_target": boundary_target,
                "evidence_refs": refs,
            }
        )

    action: dict[str, Any] = {
        "thought": str(raw.get("thought") or legacy_summary),
        "action": str(raw.get("action") or ""),
        "arg1": str(raw.get("arg1") or ""),
        "arg2": str(raw.get("arg2") or ""),
        "query": query,
        "refresh": bool(raw.get("refresh", False)),
        "answer": answer,
    }
    if raw.get("metric"):
        action["metric"] = str(raw["metric"])
    # This is the same wire contract consumed by the Go actor boundary.  SFT
    # export therefore fails before training if either side drifts.
    wire = ActionRequest.model_validate(action).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    wire["query"] = {
        key: value
        for key, value in wire["query"].items()
        if value not in ("", 0, None)
    }
    return wire


def _terminal_contract_errors(
    *,
    scenario_id: str,
    trajectory_id: str,
    action: dict[str, Any],
    expected_case: ExpectedCase,
) -> tuple[str, ...]:
    answer = dict(action.get("answer") or {})
    errors: list[str] = []
    if action.get("action") != "answer" or not answer.get("ready"):
        errors.append("terminal action must submit a ready answer")
    if answer.get("status") != expected_case.expected_status:
        errors.append(
            f"status={answer.get('status')} expected={expected_case.expected_status}"
        )
    actual = [
        {
            "variant": "target",
            "target_id": str(cause.get("target", "")),
            "target_name": "",
        }
        for cause in answer.get("causes") or []
    ] + [
        {
            "variant": "pseudo",
            "pseudo_id": str(cause.get("id", "")),
            "pseudo_kind": str(cause.get("kind", "")),
            "boundary_target": str(cause.get("boundary_target", "")),
        }
        for cause in answer.get("external_causes") or []
    ]
    score = root_f1(expected_case.roots, actual)
    if score != 1.0:
        errors.append(f"terminal root_f1={score:.3f}")
    if errors:
        return tuple(
            f"{scenario_id}/{trajectory_id}: {message}" for message in errors
        )
    return ()


def build_sft_dataset(
    *,
    synth_root: Path,
    split: TeacherSplit,
    expected_scenarios: int | None = None,
    contract: EvalContract | None = None,
    terminal_contract_sha256: str | None = None,
    curation_manifest_sha256: str | None = None,
) -> tuple[tuple[SFTRecord, ...], SFTDatasetManifest]:
    train = set(split.train)
    sealed = set(split.sealed_eval)
    excluded = set(split.excluded)
    records: list[SFTRecord] = []
    seen_trajectories: set[str] = set()
    trajectory_sha256: dict[str, str] = {}
    counts: Counter[str] = Counter()
    failed_observations = 0
    total_turns = 0
    contract_errors: list[str] = []

    for path in _trajectory_files(synth_root):
        scenario_id = path.parent.name
        if scenario_id in sealed:
            raise ValueError(f"sealed eval trajectory found in SFT input: {scenario_id}")
        if scenario_id in excluded:
            raise ValueError(f"excluded trajectory found in SFT input: {scenario_id}")
        if scenario_id not in train:
            raise ValueError(f"unregistered trajectory found in SFT input: {scenario_id}")

        trajectory_id = path.stem
        trajectory_key = f"{scenario_id}/{trajectory_id}"
        seen_trajectories.add(trajectory_key)
        trajectory_sha256[trajectory_key] = hashlib.sha256(path.read_bytes()).hexdigest()
        steps: list[dict[str, Any]] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            raw: dict[str, Any] = json.loads(line)
            if not isinstance(raw.get("ok"), bool):
                raise TypeError(f"teacher step has no boolean outcome: {path}:{line_number}")
            if raw["ok"] is False:
                # A valid probe may return no data. Keep the attempted action: the following
                # rendered state contains this observation and teaches recovery/navigation.
                failed_observations += 1
            action = raw.get("action")
            if not isinstance(action, dict):
                raise TypeError(f"teacher action is not an object: {path}:{line_number}")
            turn = raw.get("turn")
            if not isinstance(turn, int):
                raise TypeError(f"teacher turn is not an integer: {path}:{line_number}")
            steps.append(raw)

        if not steps:
            raise ValueError(f"empty teacher trajectory: {path}")
        turns: list[SFTTurn] = []
        prior_actions: list[str] = []
        for index, step in enumerate(steps):
            turn = step["turn"]
            if turn != index + 1:
                raise ValueError(f"teacher turns must be contiguous from 1: {path}:{turn}")
            expected_case = contract.cases.get(scenario_id) if contract else None
            canonical = _canonical_action(
                step["action"],
                str(step["user"]),
                tuple(prior_actions),
                expected_case,
            )
            turns.append(
                SFTTurn(
                    turn=turn,
                    messages=(
                        SFTMessage(role="system", content=str(step["system"])),
                        SFTMessage(role="user", content=str(step["user"])),
                        SFTMessage(
                            role="assistant",
                            content=json.dumps(
                                canonical, ensure_ascii=False, separators=(",", ":")
                            ),
                        ),
                    ),
                )
            )
            prior_actions.append(str(canonical["action"]))

        if contract is not None:
            expected_case = contract.cases.get(scenario_id)
            if expected_case is None:
                contract_errors.append(
                    f"{scenario_id}/{trajectory_id}: scenario missing from evaluation contract"
                )
            else:
                contract_errors.extend(
                    _terminal_contract_errors(
                        scenario_id=scenario_id,
                        trajectory_id=trajectory_id,
                        action=json.loads(turns[-1].messages[2].content),
                        expected_case=expected_case,
                    )
                )

        records.append(
            SFTRecord(
                turns=tuple(turns),
                scenario_id=scenario_id,
                trajectory_id=trajectory_id,
                turn_count=len(steps),
            )
        )
        counts[scenario_id] += 1
        total_turns += len(steps)

    scenarios = tuple(sorted(counts))
    if contract_errors:
        raise ValueError(
            "teacher trajectories violate the typed terminal contract:\n"
            + "\n".join(contract_errors)
        )
    if expected_scenarios is not None and len(scenarios) != expected_scenarios:
        raise ValueError(
            f"expected {expected_scenarios} accepted scenarios, found {len(scenarios)}"
        )
    record_tuple = tuple(records)
    manifest = SFTDatasetManifest(
        scenario_count=len(scenarios),
        trajectory_count=len(seen_trajectories),
        turn_count=total_turns,
        failed_observation_count=failed_observations,
        scenarios=scenarios,
        trajectories_per_scenario=dict(sorted(counts.items())),
        trajectory_sha256=dict(sorted(trajectory_sha256.items())),
        dataset_sha256=hashlib.sha256(sft_records_payload(record_tuple).encode()).hexdigest(),
        terminal_contract_sha256=terminal_contract_sha256,
        curation_manifest_sha256=curation_manifest_sha256,
    )
    return record_tuple, manifest


def write_sft_dataset(
    *, records: tuple[SFTRecord, ...], manifest: SFTDatasetManifest, output: Path
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = sft_records_payload(records)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    if digest != manifest.dataset_sha256:
        raise ValueError("dataset payload does not match manifest SHA-256")
    output.write_text(payload, encoding="utf-8")
    output.with_suffix(".manifest.json").write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
