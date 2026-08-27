"""Serialization and training views for recursive teacher trajectories."""

from __future__ import annotations

from pathlib import Path

from rca_lab.harness.models import RecursiveEpisode, ThoughtBranch


def load_recursive_episode(path: Path) -> RecursiveEpisode:
    return RecursiveEpisode.model_validate_json(path.read_text(encoding="utf-8"))


def save_recursive_episode(path: Path, episode: RecursiveEpisode) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(episode.model_dump_json(indent=2) + "\n", encoding="utf-8")


def recursive_training_view(episode: RecursiveEpisode) -> tuple[ThoughtBranch, ...]:
    """All useful attempts: failures, corrections, and successful retries."""

    return tuple(branch for branch in episode.branches if branch.include_in_recursive_training)


def sft_training_view(episode: RecursiveEpisode) -> ThoughtBranch:
    """Only the scorer-accepted final branch; never rejected reasoning."""

    return episode.selected_branch()
