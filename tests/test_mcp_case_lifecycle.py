from __future__ import annotations

import argparse
import importlib.util
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def isolated_replay_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("RCA_REPLAY_LEASE_DIR", str(tmp_path / "leases"))


def load_runner():
    spec = importlib.util.spec_from_file_location("mcp_case_run_lifecycle_under_test", Path("scripts/mcp_case_run.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    values = {
        "runner": "student",
        "cases_root": str(tmp_path),
        "project_prefix": "t",
        "overwrite": False,
        "reuse_db": False,
        "keep_db": False,
        "max_alarms": 50,
        "runs": 1,
        "hint_ladder": False,
        "mcp_nav_hints": False,
        "endpoint": "e",
        "model": "m",
        "no_alias": False,
        "no_guided": False,
        "no_reasoning_guard": False,
        "max_turns": 5,
        "max_prompt_tokens": 1000,
        "max_output_tokens": 100,
        "tool_result_max_chars": 100,
        "temperature": 1.0,
        "top_p": 1.0,
        "top_k": 1,
        "seed": 0,
        "hints_by_case": {},
        "hinted_only": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def case_dir(tmp_path: Path, case_id: str = "case-x-v3-1") -> Path:
    path = tmp_path / case_id
    path.mkdir()
    return path


def test_restore_failure_does_not_parent_teardown_before_ownership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = load_runner()
    case_dir(tmp_path)
    torn_down: list[str] = []
    monkeypatch.setattr(runner, "restore_case", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("restore boom")))
    monkeypatch.setattr(runner, "teardown", lambda project: torn_down.append(project))

    with pytest.raises(RuntimeError, match="restore boom"):
        runner.run_case(args(tmp_path, runner="prepare", keep_db=True), "case-x-v3-1", {}, tmp_path / "out", Path("/bin/true"))

    assert torn_down == []


def test_external_capture_env_failure_tears_down_owned_restore_even_with_keep_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = load_runner()
    case_dir(tmp_path)
    torn_down: list[str] = []

    def fake_restore(_case_dir, _project, connections_path, _log_path):
        connections_path.write_text('{"RCA_PG_DSN":"pg"}', encoding="utf-8")
        return {"RCA_PG_DSN": "pg"}

    monkeypatch.setattr(runner, "restore_case", fake_restore)
    monkeypatch.setattr(runner, "external_capture_env", lambda *_: (_ for _ in ()).throw(ValueError("bad manifest")))
    monkeypatch.setattr(runner, "teardown", lambda project: torn_down.append(project))

    with pytest.raises(ValueError, match="bad manifest"):
        runner.run_case(args(tmp_path, keep_db=True), "case-x-v3-1", {}, tmp_path / "out", Path("/bin/true"))

    assert torn_down == ["t-x"]


def test_prepare_success_keeps_db_without_mutating_keep_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = load_runner()
    case_dir(tmp_path)
    torn_down: list[str] = []

    def fake_restore(_case_dir, _project, connections_path, _log_path):
        connections_path.write_text('{"RCA_PG_DSN":"pg"}', encoding="utf-8")
        return {"RCA_PG_DSN": "pg"}

    monkeypatch.setattr(runner, "restore_case", fake_restore)
    monkeypatch.setattr(runner, "external_capture_env", lambda *_: {})
    monkeypatch.setattr(runner, "build_observed_seed", lambda *a, **k: {"time_window": {"first_event": "f", "last_event": "l"}})
    monkeypatch.setattr(runner, "blind_seed", lambda _binary, seed: seed)
    monkeypatch.setattr(runner, "teardown", lambda project: torn_down.append(project))
    run_args = args(tmp_path, runner="prepare")

    assert runner.run_case(run_args, "case-x-v3-1", {}, tmp_path / "out", Path("/bin/true")) == []

    assert torn_down == []
    assert run_args.keep_db is False


def test_teardown_retries_then_succeeds_and_logs_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    runner = load_runner()
    calls = []

    def fake_run(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stderr="first failure\n", stdout="")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner.time, "sleep", lambda *_: None)

    runner.teardown("project-x")

    assert len(calls) == 2
    assert "project-x: teardown attempt 1/2 failed" in capsys.readouterr().out


def test_teardown_raises_after_bounded_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = load_runner()
    monkeypatch.setattr(runner.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1, stderr="still bad\n", stdout=""))
    monkeypatch.setattr(runner.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError, match="teardown failed for project-x"):
        runner.teardown("project-x")


def test_restore_existing_project_refusal_does_not_parent_teardown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = load_runner()
    case_dir(tmp_path)
    torn_down: list[str] = []
    monkeypatch.setattr(runner, "restore_case", lambda *a, **k: (_ for _ in ()).throw(runner.ExistingReplayProjectError("exists")))
    monkeypatch.setattr(runner, "teardown", lambda project: torn_down.append(project))

    with pytest.raises(runner.ExistingReplayProjectError):
        runner.run_case(args(tmp_path), "case-x-v3-1", {}, tmp_path / "out", Path("/bin/true"))

    assert torn_down == []


def test_restore_case_maps_existing_project_refusal_to_typed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = load_runner()
    case = case_dir(tmp_path)
    log_path = tmp_path / "restore.log"
    connections_path = tmp_path / "connections.json"
    connections_path.write_text('{"RCA_PG_DSN":"old"}', encoding="utf-8")

    class FakePopen:
        pid = 12345

        def __init__(self, *_args, **_kwargs):
            pass

        def wait(self, timeout=None):
            return runner.RESTORE_EXISTING_PROJECT_EXIT

    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)

    with pytest.raises(runner.ExistingReplayProjectError):
        runner.restore_case(case, "project-x", connections_path, log_path)

    assert connections_path.read_text(encoding="utf-8") == '{"RCA_PG_DSN":"old"}'


def test_teardown_passes_timeout_to_docker_down(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = load_runner()
    seen: list[float] = []

    def fake_run(*_args, **kwargs):
        seen.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    runner.teardown("project-x", timeout_seconds=7.0)

    assert seen == [7.0]


@pytest.mark.parametrize("content", [None, "invalid json"])
def test_successful_restore_cleans_failed_handoff(tmp_path, monkeypatch, content):
    runner = load_runner()
    connection = tmp_path / "connections.json"
    if content is not None:
        connection.write_text(content)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: SimpleNamespace(wait=lambda: 0))
    removed = []
    monkeypatch.setattr(runner, "teardown", removed.append)
    with pytest.raises((FileNotFoundError, ValueError)):
        runner.restore_case(tmp_path, "owned", connection, tmp_path / "restore.log")
    assert removed == ["owned"]


def test_interrupted_restore_allows_shell_cleanup_to_finish(tmp_path, monkeypatch):
    runner = load_runner()
    waits, signals = [], []

    def wait(timeout=None):
        waits.append(timeout)
        if timeout is None:
            raise KeyboardInterrupt()
        return 143

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=123, wait=wait))
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    with pytest.raises(KeyboardInterrupt):
        runner.restore_case(tmp_path, "owned", tmp_path / "connections.json", tmp_path / "restore.log")
    assert waits == [None, 150]
    assert signals == [(123, signal.SIGTERM)]


@pytest.mark.parametrize("keep", [False, True])
def test_explicit_reuse_keeps_original_teardown_contract(tmp_path, monkeypatch, keep):
    runner = load_runner()
    output = tmp_path / "out" / "case-x-v3-1"
    output.mkdir(parents=True)
    (output / "connections.json").write_text('{}')
    removed = []
    monkeypatch.setattr(runner, "teardown", removed.append)
    monkeypatch.setattr(runner, "external_capture_env", lambda *a: {})
    monkeypatch.setattr(runner, "build_observed_seed", lambda *a, **k: {})
    monkeypatch.setattr(runner, "blind_seed", lambda *a: {})
    monkeypatch.setattr(runner, "run_one", lambda *a: ({}, {}))
    runner.run_case(args(tmp_path, reuse_db=True, keep_db=keep), "case-x-v3-1", {}, tmp_path / "out", Path('/bin/true'))
    assert removed == ([] if keep else ["t-x"])


def test_repeated_restore_publishes_new_exclusive_handoff_and_preserves_prior(tmp_path,monkeypatch):
    import json
    runner=load_runner();canonical=tmp_path/'connections.json'
    canonical.write_text(json.dumps({'RCA_PG_DSN':'old'}))
    seen=[]
    def launch(*args,**kwargs):
        path=Path(kwargs['env']['RCA_CONNECTIONS_FILE'])
        assert path!=canonical and not path.exists()
        assert json.loads(canonical.read_text())['RCA_PG_DSN']=='old'
        with path.open('x') as f:json.dump({'RCA_PG_DSN':'new'},f)
        seen.append(path)
        return SimpleNamespace(wait=lambda:0)
    monkeypatch.setattr(runner.subprocess,'Popen',launch)
    assert runner.restore_case(tmp_path,'owned',canonical,tmp_path/'restore.log')=={'RCA_PG_DSN':'new'}
    assert json.loads(canonical.read_text())['RCA_PG_DSN']=='new'
    assert [json.loads(p.read_text()) for p in tmp_path.glob('connections.previous-*.json')]==[{'RCA_PG_DSN':'old'}]
    assert not seen[0].exists()
