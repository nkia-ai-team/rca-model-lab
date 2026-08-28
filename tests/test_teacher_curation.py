import json
from pathlib import Path

import pytest

from rca_lab.data.curation import (
    CuratedTrajectory,
    TeacherCurationSpec,
    TerminalCorrection,
    curate_teacher_episodes,
)


def _write_episode(path: Path) -> None:
    path.parent.mkdir(parents=True)
    turns = [
        {
            "turn": 1,
            "system": "system",
            "user": "obs-001",
            "action": {"action": "probe_logs"},
            "ok": True,
            "obs": "logs",
        },
        {
            "turn": 2,
            "system": "system",
            "user": "obs-001 obs-002",
            "action": {
                "action": "answer",
                "answer": {
                    "status": "provisional",
                    "ready": True,
                    "causes": [
                        {"target": "wrong", "claim": "old"},
                        {"target": "symptom", "claim": "propagation"},
                    ],
                    "text": "old conclusion",
                },
            },
            "ok": True,
            "obs": "done",
        },
    ]
    path.write_text("".join(json.dumps(turn) + "\n" for turn in turns), encoding="utf-8")


def test_curates_lossless_actions_and_audited_terminal_correction(tmp_path: Path) -> None:
    episode = Path("case-a/teacher.episode.jsonl")
    actions = Path("case-a/teacher.actions.json")
    _write_episode(tmp_path / "source" / episode)
    (tmp_path / "source" / actions).write_text(
        json.dumps(
            [
                {"action": "probe_metrics", "metric": "cpu_usage"},
                json.loads((tmp_path / "source" / episode).read_text().splitlines()[-1])["action"],
            ]
        ),
        encoding="utf-8",
    )
    spec = TeacherCurationSpec(
        trajectories=(
            CuratedTrajectory(
                episode=str(episode),
                actions=str(actions),
                terminal_correction=TerminalCorrection(
                    drop_cause_targets=("symptom",),
                    replace_cause_targets={"wrong": "root"},
                    cause_updates={"root": {"claim": "corrected", "confidence": 0.8}},
                    text="correct conclusion",
                ),
            ),
        )
    )

    manifest = curate_teacher_episodes(
        source_root=tmp_path / "source",
        output_root=tmp_path / "curated",
        spec=spec,
        spec_bytes=b"spec",
    )

    output = [json.loads(line) for line in (tmp_path / "curated" / episode).read_text().splitlines()]
    assert output[0]["action"]["action"] == "probe_metrics"
    assert output[-1]["action"]["answer"]["causes"] == [
        {"target": "root", "claim": "corrected", "confidence": 0.8}
    ]
    assert output[-1]["action"]["answer"]["text"] == "correct conclusion"
    assert manifest.trajectory_count == 1
    assert manifest.scenario_count == 1
    assert manifest.artifacts[0].corrected is True
    assert manifest.artifacts[0].actions_sha256 is not None


def test_curator_rejects_unregistered_stale_episode(tmp_path: Path) -> None:
    _write_episode(tmp_path / "source/case-a/teacher.episode.jsonl")
    _write_episode(tmp_path / "curated/case-stale/stale.episode.jsonl")
    spec = TeacherCurationSpec(
        trajectories=(CuratedTrajectory(episode="case-a/teacher.episode.jsonl"),)
    )

    with pytest.raises(ValueError, match="unregistered episodes"):
        curate_teacher_episodes(
            source_root=tmp_path / "source",
            output_root=tmp_path / "curated",
            spec=spec,
            spec_bytes=b"spec",
        )
