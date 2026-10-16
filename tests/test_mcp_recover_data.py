import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def module():
    path = Path(__file__).resolve().parents[1] / "scripts/mcp_recover_data.py"
    spec = importlib.util.spec_from_file_location("recovery_driver", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_snapshot_preserves_source_and_resumed_repairs(tmp_path):
    recovery = module()
    source, destination = tmp_path / "original", tmp_path / "copy"
    source.mkdir()
    (source / "rationalize.json").write_text('{"error": "quota"}')
    recovery.snapshot(source, destination)
    (destination / "rationalize.json").write_text('{"repaired": true}')
    recovery.snapshot(source, destination)
    assert json.loads((source / "rationalize.json").read_text()) == {"error": "quota"}
    assert json.loads((destination / "rationalize.json").read_text()) == {"repaired": True}


def test_candidate_inventory_is_train_only_and_reviewed(tmp_path):
    recovery = module()
    (tmp_path / "split.yaml").write_text("train: [case-train]\nvalidation: [case-val]\n")
    for case, accepted in [("case-train", True), ("case-val", True), ("case-rejected", False)]:
        run = tmp_path / "teacher" / case / "run1"
        run.mkdir(parents=True)
        (run / "score.json").write_text(json.dumps({"case_id": case, "stop_reason": "submitted",
                                                   "root_f1": 1, "ref_grounding": 1, "tool_calls": 3}))
        (run / "review.json").write_text(json.dumps({"accepted": accepted}))
    stage = {"split": "split.yaml", "roots": ["teacher"]}
    assert recovery.candidates(stage, tmp_path) == [tmp_path / "teacher/case-train/run1"]


def test_quota_failure_never_exports_or_starts_next_stage(tmp_path, monkeypatch):
    recovery = module()
    source = tmp_path / "teacher/case-train/run1"
    source.mkdir(parents=True)
    (source / "score.json").write_text("{}")
    workspace = tmp_path / "recovery"
    workspace.mkdir()
    monkeypatch.setattr(recovery, "candidates", lambda stage: [source])
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(recovery.subprocess, "run", fail)
    stage = {"name": "tt4", "split": "split.yaml", "cases_root": "captures"}
    config = {"writer_model": "original-writer", "judge_model": "original-judge", "stages": [stage, stage]}
    assert recovery.run_recovery(config, workspace) == 3
    assert len(calls) == 1 and "scripts/mcp_rationalize.py" in calls[0]
    assert json.loads((workspace / "state.json").read_text())["phase"] == "quota_blocked"
    assert not (workspace / "datasets").exists()


def test_codex_shards_propagate_provider_and_quota_without_export(tmp_path, monkeypatch):
    recovery = module()
    source = tmp_path / "teacher/case-train/run1"
    source.mkdir(parents=True)
    workspace = tmp_path / "recovery"
    workspace.mkdir()
    monkeypatch.setattr(recovery, "candidates", lambda stage: [source])
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(recovery.subprocess, "run", fail)
    config = {"writer_provider": "codex", "judge_provider": "codex", "writer_model": "configured-model",
              "judge_model": "configured-model", "rationalize_workers": 4, "redo_provider": True,
              "stages": [{"name": "tt4", "split": "split.yaml", "cases_root": "captures"}]}
    assert recovery.run_recovery(config, workspace) == 3
    assert len(calls) == 4
    assert {command[command.index("--shard") + 1] for command in calls} == {"0/4", "1/4", "2/4", "3/4"}
    for command in calls:
        assert command[command.index("--writer") + 1] == "codex"
        assert command[command.index("--judge-provider") + 1] == "codex"
        assert "--redo-provider" in command
        assert "scripts/mcp_export_sft.py" not in command
    state = json.loads((workspace / "state.json").read_text())
    assert state["phase"] == "quota_blocked"
    assert state["shard_exit_codes"] == [3, 3, 3, 3]


def test_invalid_provider_rejected_before_any_work(tmp_path, monkeypatch):
    import pytest

    recovery = module()
    monkeypatch.setattr(recovery, "candidates", lambda _: pytest.fail("must validate provider first"))
    with pytest.raises(ValueError, match="unsupported"):
        recovery.run_recovery({"writer_provider": "guessed", "stages": [{}]}, tmp_path)


def test_codex_actions_use_recorded_student_trajectory(tmp_path, monkeypatch):
    recovery = module()
    source = tmp_path / "source/case-train/run1"
    source.mkdir(parents=True)
    workspace = tmp_path / "recovery"
    workspace.mkdir()
    monkeypatch.setattr(recovery, "candidates", lambda stage: [source])
    commands = []

    def stop_after_inspection(command, log):
        commands.append(command)
        return 3

    monkeypatch.setattr(recovery, "execute_command", stop_after_inspection)
    config = {"writer_provider": "codex", "judge_provider": "codex", "writer_model": "writer",
              "judge_model": "judge", "stages": [{"name": "new", "runner": "student",
              "split": "split.yaml", "cases_root": "captures"}]}
    assert recovery.run_recovery(config, workspace) == 3
    assert "--student-root" in commands[0]
    assert "--teacher-root" not in commands[0]

    assert "--student-all" in commands[0]
    assert "--student-require-review" in commands[0]
