import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def module():
    path = Path(__file__).resolve().parents[1] / "scripts/mcp_run_admitted_recipe.py"
    spec = importlib.util.spec_from_file_location("mcp_run_admitted_recipe", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def config(tmp_path: Path) -> Path:
    write_json(tmp_path / "joint.jsonl", {})
    write_json(tmp_path / "joint.manifest.json", {})
    write_json(tmp_path / "continue.json", {})
    payload = {
        "candidate_input": str(tmp_path / "joint.jsonl"),
        "source_joint_manifest": str(tmp_path / "joint.manifest.json"),
        "out_root": str(tmp_path / "out"),
        "workspace": str(tmp_path / "workspace"),
        "cache_roots": [str(tmp_path / "cache-a"), str(tmp_path / "cache-b")],
        "candidate_workers": 2,
        "judge_workers": 4,
        "minimum_final_per_source": 1,
        "turns_per_episode": 6,
        "continuation_config": str(tmp_path / "continue.json"),
        "commands": {
            "prepare_candidates": ["prepare-admitted", "build"],
            "judge": ["judge-evidence"],
            "export": ["prepare-admitted", "export"],
            "continue": ["continue-chain"],
        },
    }
    path = tmp_path / "controller.json"
    write_json(path, payload)
    return path


def completed(code: int = 0):
    return SimpleNamespace(returncode=code, stdout="{}", stderr="")


def admission_report(approved: int, held: int, rejected: int, pending: int) -> dict:
    total = approved + held + rejected + pending
    return {
        "status": "complete" if pending == 0 else "pending",
        "counts": {
            "approved": approved,
            "held": held,
            "rejected": rejected,
            "pending": pending,
            "total": total,
        },
        "progress": {"processed": total, "expected_total": total},
    }


def write_candidates(tmp_path: Path) -> None:
    items = tmp_path / "out/candidates/items.jsonl"
    content = "{}\n"
    items.parent.mkdir(parents=True, exist_ok=True)
    items.write_text(content, encoding="utf-8")
    write_json(
        tmp_path / "out/candidates/manifest.json",
        {
            "items_jsonl": str(items),
            "items_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        },
    )


def test_pending_judge_exits_3_and_never_exports_or_trains(tmp_path, monkeypatch):
    ctl = module()
    config_path = config(tmp_path)
    calls = []

    def fake_run(command, **_):
        calls.append(command)
        if command[:2] == ["prepare-admitted", "build"]:
            write_candidates(tmp_path)
            return completed()
        if command[0] == "judge-evidence":
            write_json(tmp_path / "out/judged/admission.json", admission_report(0, 0, 0, 1))
            return completed(2)
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(ctl.subprocess, "run", fake_run)
    assert ctl.run_controller(config_path) == 3
    assert [call[:2] for call in calls] == [["prepare-admitted", "build"], ["judge-evidence", "--input"]]
    judge = calls[1]
    assert judge[judge.index("--limit") + 1] == "0"
    assert judge[judge.index("--workers") + 1] == "4"
    assert judge.count("--cache-from") == 2
    assert json.loads((tmp_path / "workspace/state.json").read_text())["phase"] == "admission_pending_or_quota"


def test_export_failure_stops_before_training(tmp_path, monkeypatch):
    ctl = module()
    config_path = config(tmp_path)
    calls = []
    write_json(tmp_path / "out/judged/admission.json", admission_report(1, 0, 0, 0))

    def fake_run(command, **_):
        calls.append(command)
        if command[:2] == ["prepare-admitted", "build"]:
            write_candidates(tmp_path)
            return completed()
        assert command[:2] == ["prepare-admitted", "export"]
        return completed(2)

    monkeypatch.setattr(ctl.subprocess, "run", fake_run)
    assert ctl.run_controller(config_path) == 2
    assert [call[:2] for call in calls] == [["prepare-admitted", "build"], ["prepare-admitted", "export"]]
    assert json.loads((tmp_path / "workspace/state.json").read_text())["phase"] == "failed_admission_gate"


def test_complete_cached_admission_skips_rejudge_and_exports_before_continue(tmp_path, monkeypatch):
    ctl = module()
    config_path = config(tmp_path)
    calls = []
    write_json(tmp_path / "out/judged/admission.json", admission_report(1, 0, 0, 0))

    def fake_run(command, **_):
        calls.append(command)
        if command[:2] == ["prepare-admitted", "build"]:
            write_candidates(tmp_path)
            return completed()
        if command[:2] == ["prepare-admitted", "export"]:
            assert command[command.index("--minimum-final-per-source") + 1] == "1"
            assert command[command.index("--cache-dir") + 1] == str(tmp_path / "out/judged")
            assert command[command.index("--turns-per-episode") + 1] == "6"
            write_json(tmp_path / "out/admitted/joint.manifest.json", {"ok": True})
            return completed()
        if command[0] == "continue-chain":
            return completed()
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(ctl.subprocess, "run", fake_run)
    assert ctl.run_controller(config_path) == 0
    assert [call[:2] for call in calls] == [
        ["prepare-admitted", "build"],
        ["prepare-admitted", "export"],
        ["continue-chain", "--config"],
        ["continue-chain", "--config"],
    ]
    assert "--execute" not in calls[2]
    assert "--execute" in calls[3]
    assert "--recovery-workspace" not in calls[2] and "--recovery-workspace" not in calls[3]
    assert json.loads((tmp_path / "workspace/state.json").read_text())["phase"] == "complete"


def test_existing_export_manifest_skips_export_and_does_not_retry_valid_errors(tmp_path, monkeypatch):
    ctl = module()
    config_path = config(tmp_path)
    calls = []
    write_json(tmp_path / "out/judged/admission.json", admission_report(1, 2, 3, 0))
    write_json(tmp_path / "out/admitted/joint.manifest.json", {"ok": True})

    def fake_run(command, **_):
        calls.append(command)
        if command[:2] == ["prepare-admitted", "build"]:
            write_candidates(tmp_path)
            return completed()
        assert command[0] == "continue-chain"
        return completed()

    monkeypatch.setattr(ctl.subprocess, "run", fake_run)
    assert ctl.run_controller(config_path) == 0
    assert [call[:2] for call in calls] == [
        ["prepare-admitted", "build"],
        ["continue-chain", "--config"],
        ["continue-chain", "--config"],
    ]


def test_running_report_is_not_complete_even_with_pending_zero(tmp_path, monkeypatch):
    ctl = module()
    config_path = config(tmp_path)
    calls = []
    write_json(
        tmp_path / "out/judged/admission.json",
        {
            "status": "running",
            "counts": {"approved": 1, "held": 0, "rejected": 0, "pending": 0, "total": 2},
            "progress": {"processed": 1, "expected_total": 2},
        },
    )

    def fake_run(command, **_):
        calls.append(command)
        if command[:2] == ["prepare-admitted", "build"]:
            write_candidates(tmp_path)
            return completed()
        if command[0] == "judge-evidence":
            write_json(tmp_path / "out/judged/admission.json", admission_report(2, 0, 0, 0))
            return completed()
        if command[:2] == ["prepare-admitted", "export"]:
            write_json(tmp_path / "out/admitted/joint.manifest.json", {"ok": True})
            return completed()
        if command[0] == "continue-chain":
            return completed()
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(ctl.subprocess, "run", fake_run)
    assert ctl.run_controller(config_path) == 0
    assert [call[0] for call in calls].count("judge-evidence") == 1


def test_partial_candidate_items_without_manifest_reruns_builder(tmp_path, monkeypatch):
    ctl = module()
    config_path = config(tmp_path)
    calls = []
    partial = tmp_path / "out/candidates/items.jsonl"
    partial.parent.mkdir(parents=True)
    partial.write_text("partial\n", encoding="utf-8")
    write_json(tmp_path / "out/judged/admission.json", admission_report(1, 0, 0, 0))
    write_json(tmp_path / "out/admitted/joint.manifest.json", {"ok": True})

    def fake_run(command, **_):
        calls.append(command)
        if command[:2] == ["prepare-admitted", "build"]:
            write_candidates(tmp_path)
            return completed()
        if command[0] == "continue-chain":
            return completed()
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(ctl.subprocess, "run", fake_run)
    assert ctl.run_controller(config_path) == 0
    assert calls[0][:2] == ["prepare-admitted", "build"]
