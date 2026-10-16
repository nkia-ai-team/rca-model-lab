import importlib.util
import json
from pathlib import Path

import pytest


def module():
    path = Path(__file__).resolve().parents[1] / "scripts/mcp_generate_polestar_new.py"
    spec = importlib.util.spec_from_file_location("mcp_generate_polestar_new", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def write_yaml(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def base_config(tmp_path: Path) -> dict:
    return {
        "run_name": "teacher-new",
        "model": "gpt-6-astra",
        "split": "split.yaml",
        "contract": "contract.yaml",
        "cases_root": str(tmp_path / "cases"),
        "max_turns": 60,
        "mcp_binary": "bin/rca-mcp",
        "workspace": "work",
        "max_attempts": 2,
        "accepted_per_case": 1,
        "workers": 1,
        "cases": ["case-a-v3-1"],
    }


def test_retry_hints_are_added_only_after_threshold_for_missing_scores(tmp_path, monkeypatch):
    gen = module()
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    write_yaml(tmp_path / "split.yaml", "train: [case-a-v3-1]\nvalidation: []\nsealed_eval: []\n")
    write_yaml(tmp_path / "hints.yaml", "case-a-v3-1: inspect checkout-service first\n")
    (tmp_path / "work").mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/rca-mcp").write_text("binary", encoding="utf-8")
    config = {
        **base_config(tmp_path),
        "retry_hints": "hints.yaml",
        "retry_hints_after_attempt": 1,
    }
    config["_retry_hints_by_case"] = gen.prepare_retry_hints(config)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        case = command[command.index("--case") + 1]
        run = command[command.index("--runs") + 1]
        run_dir = tmp_path / "outputs/mcp-eval" / config["run_name"] / case / f"run{run}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "score.json").write_text(
            json.dumps(
                {
                    "case_id": case,
                    "stop_reason": "submitted",
                    "root_f1": 0,
                    "ref_grounding": 1,
                    "tool_calls": 3,
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(gen.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="accepted episodes"):
        gen.collect_case(config, "case-a-v3-1", 0)

    assert "--hints" not in calls[0]
    assert calls[1][-2:] == ["--hints", str((tmp_path / "hints.yaml").resolve())]


def test_cached_scores_are_not_retried_with_hints(tmp_path, monkeypatch):
    gen = module()
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    write_yaml(tmp_path / "split.yaml", "train: [case-a-v3-1]\nvalidation: []\nsealed_eval: []\n")
    write_yaml(tmp_path / "hints.yaml", "case-a-v3-1: inspect checkout-service first\n")
    run1 = tmp_path / "outputs/mcp-eval/teacher-new/case-a-v3-1/run1"
    run1.mkdir(parents=True)
    (run1 / "score.json").write_text(
        json.dumps({"stop_reason": "submitted", "root_f1": 0, "ref_grounding": 1, "tool_calls": 3}),
        encoding="utf-8",
    )
    (tmp_path / "work").mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/rca-mcp").write_text("binary", encoding="utf-8")
    config = {
        **base_config(tmp_path),
        "retry_hints": "hints.yaml",
        "retry_hints_after_attempt": 0,
    }

    with pytest.raises(ValueError, match="positive integer"):
        gen.prepare_retry_hints(config)

    config["retry_hints_after_attempt"] = 1
    config["_retry_hints_by_case"] = gen.prepare_retry_hints(config)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        run_dir = tmp_path / "outputs/mcp-eval/teacher-new/case-a-v3-1/run2"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "score.json").write_text(
            json.dumps({"stop_reason": "submitted", "root_f1": 0, "ref_grounding": 1, "tool_calls": 3}),
            encoding="utf-8",
        )

    monkeypatch.setattr(gen.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="accepted episodes"):
        gen.collect_case(config, "case-a-v3-1", 0)

    assert len(calls) == 1
    assert calls[0][calls[0].index("--runs") + 1] == "2"
    assert "--hints" in calls[0]


def test_retry_hints_reject_non_train_or_protected_cases_before_subprocess(tmp_path, monkeypatch):
    gen = module()
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    write_yaml(
        tmp_path / "split.yaml",
        "train: [case-a-v3-1]\nvalidation: [case-b-v3-1]\nsealed_eval: [case-c-v3-1]\n",
    )
    write_yaml(tmp_path / "hints.yaml", "case-b-v3-1: forbidden\n")
    config = {
        **base_config(tmp_path),
        "retry_hints": "hints.yaml",
        "retry_hints_after_attempt": 1,
    }

    with pytest.raises(ValueError, match="validation/sealed"):
        gen.prepare_retry_hints(config)

    write_yaml(tmp_path / "hints.yaml", "case-a-v3-1: allowed\n")
    config["cases"] = ["case-b-v3-1"]
    with pytest.raises(ValueError, match="outside train"):
        gen.prepare_retry_hints(config)


def test_retry_hints_stop_unguided_cases_after_threshold_without_subprocess(tmp_path, monkeypatch):
    gen = module()
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    write_yaml(tmp_path / "split.yaml", "train: [case-a-v3-1, case-b-v3-1]\nvalidation: []\nsealed_eval: []\n")
    write_yaml(tmp_path / "hints.yaml", "case-a-v3-1: inspect checkout-service first\n")
    case_dir = tmp_path / "outputs/mcp-eval/teacher-new/case-b-v3-1"
    for attempt in range(1, 6):
        run_dir = case_dir / f"run{attempt}"
        run_dir.mkdir(parents=True)
        (run_dir / "score.json").write_text(
            json.dumps({"stop_reason": "submitted", "root_f1": 0, "ref_grounding": 1, "tool_calls": 3}),
            encoding="utf-8",
        )
    (tmp_path / "work").mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/rca-mcp").write_text("binary", encoding="utf-8")
    config = {
        **base_config(tmp_path),
        "cases": ["case-a-v3-1", "case-b-v3-1"],
        "max_attempts": 7,
        "retry_hints": "hints.yaml",
        "retry_hints_after_attempt": 5,
    }
    config["_retry_hints_by_case"] = gen.prepare_retry_hints(config)

    def fail_run(command, **kwargs):
        raise AssertionError(f"unexpected subprocess: {command}")

    monkeypatch.setattr(gen.subprocess, "run", fail_run)

    with pytest.raises(RuntimeError, match="guided retry skipped after 5 attempts"):
        gen.collect_case(config, "case-b-v3-1", 1)

    assert not (case_dir / "run6" / "score.json").exists()
