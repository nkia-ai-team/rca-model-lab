"""Reproducibly curate accepted teacher episodes without mutating their source."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import Field, model_validator

from rca_lab.harness.models import StrictModel


class CauseCorrection(StrictModel):
    claim: str | None = None
    mechanism: str | None = None
    proof_type: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class TerminalCorrection(StrictModel):
    drop_cause_targets: tuple[str, ...] = ()
    replace_cause_targets: dict[str, str] = Field(default_factory=dict)
    cause_updates: dict[str, CauseCorrection] = Field(default_factory=dict)
    text: str | None = None


class CuratedTrajectory(StrictModel):
    episode: str
    actions: str | None = None
    terminal_correction: TerminalCorrection | None = None

    @model_validator(mode="after")
    def paths_are_relative_and_local(self) -> CuratedTrajectory:
        for value in (self.episode, self.actions):
            if value is None:
                continue
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("curation paths must be relative and may not traverse parents")
        return self


class TeacherCurationSpec(StrictModel):
    version: int = 1
    trajectories: tuple[CuratedTrajectory, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def episodes_are_unique(self) -> TeacherCurationSpec:
        episodes = [item.episode for item in self.trajectories]
        if len(episodes) != len(set(episodes)):
            raise ValueError("curation episode paths must be unique")
        return self


class CuratedArtifact(StrictModel):
    episode: str
    source_sha256: str
    output_sha256: str
    actions_sha256: str | None = None
    corrected: bool = False


class TeacherCurationManifest(StrictModel):
    version: int = 1
    spec_sha256: str
    trajectory_count: int = Field(ge=1)
    scenario_count: int = Field(ge=1)
    artifacts: tuple[CuratedArtifact, ...]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_episode(path: Path) -> list[dict[str, Any]]:
    turns = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not turns:
        raise ValueError(f"empty teacher episode: {path}")
    for expected_turn, turn in enumerate(turns, 1):
        if turn.get("turn") != expected_turn:
            raise ValueError(f"episode turns must be contiguous: {path}:{turn.get('turn')}")
    return turns


def _reattach_actions(turns: list[dict[str, Any]], path: Path) -> None:
    actions: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    if len(turns) != len(actions):
        raise ValueError(
            f"turn/action count mismatch: episode={len(turns)} actions={len(actions)}"
        )
    for index, (turn, action) in enumerate(zip(turns, actions, strict=True), 1):
        if not isinstance(action, dict) or not action.get("action"):
            raise ValueError(f"lossless action is invalid: {path}:{index}")
        turn["action"] = action


def _correct_terminal(turns: list[dict[str, Any]], correction: TerminalCorrection) -> None:
    action = turns[-1].get("action")
    if not isinstance(action, dict) or action.get("action") != "answer":
        raise ValueError("terminal correction requires a final answer action")
    answer = action.get("answer")
    if not isinstance(answer, dict) or not answer.get("ready"):
        raise ValueError("terminal correction requires a ready answer")

    drop = set(correction.drop_cause_targets)
    replacements = correction.replace_cause_targets
    causes: list[dict[str, Any]] = []
    for raw in answer.get("causes") or []:
        cause = dict(raw)
        target_key = "target" if "target" in cause else "target_id"
        target = str(cause.get(target_key) or "")
        if target in drop:
            continue
        if target in replacements:
            cause[target_key] = replacements[target]
            target = replacements[target]
        update = correction.cause_updates.get(target)
        if update is not None:
            for field, value in update.model_dump(exclude_none=True).items():
                cause[field] = value
        causes.append(cause)
    targets = [str(cause.get("target") or cause.get("target_id") or "") for cause in causes]
    if not targets or len(targets) != len(set(targets)):
        raise ValueError("terminal correction produced empty or duplicate internal causes")
    answer["causes"] = causes
    if correction.text is not None:
        answer["text"] = correction.text


def curate_teacher_episodes(
    *,
    source_root: Path,
    output_root: Path,
    spec: TeacherCurationSpec,
    spec_bytes: bytes,
) -> TeacherCurationManifest:
    expected_outputs = {output_root / item.episode for item in spec.trajectories}
    stale = set(output_root.glob("case-*/*.episode*.jsonl")) - expected_outputs
    if stale:
        raise ValueError(f"curation output contains unregistered episodes: {sorted(stale)}")

    artifacts: list[CuratedArtifact] = []
    scenarios: set[str] = set()
    for item in spec.trajectories:
        source = source_root / item.episode
        if not source.is_file():
            raise FileNotFoundError(source)
        turns = _load_episode(source)
        actions_path = source_root / item.actions if item.actions else None
        if actions_path is not None:
            if not actions_path.is_file():
                raise FileNotFoundError(actions_path)
            _reattach_actions(turns, actions_path)
        if item.terminal_correction is not None:
            _correct_terminal(turns, item.terminal_correction)

        payload = "".join(
            json.dumps(turn, ensure_ascii=False, separators=(",", ":")) + "\n" for turn in turns
        ).encode()
        output = output_root / item.episode
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
        scenarios.add(PurePosixPath(item.episode).parts[0])
        artifacts.append(
            CuratedArtifact(
                episode=item.episode,
                source_sha256=_sha256(source.read_bytes()),
                output_sha256=_sha256(payload),
                actions_sha256=_sha256(actions_path.read_bytes()) if actions_path else None,
                corrected=item.terminal_correction is not None,
            )
        )

    manifest = TeacherCurationManifest(
        spec_sha256=_sha256(spec_bytes),
        trajectory_count=len(artifacts),
        scenario_count=len(scenarios),
        artifacts=tuple(artifacts),
    )
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "curation-manifest.json").write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return manifest
