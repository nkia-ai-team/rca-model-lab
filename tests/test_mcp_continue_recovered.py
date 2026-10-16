import importlib.util
import json
from pathlib import Path

import pytest


def module():
    path = Path(__file__).resolve().parents[1] / "scripts/mcp_continue_recovered.py"
    spec = importlib.util.spec_from_file_location("continue_recovered", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def write_dataset(root: Path, name: str, case_id: str, turns: int = 8) -> Path:
    dataset = root / "datasets" / f"{name}.jsonl"
    dataset.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "case_id": case_id,
        "messages": [
            {"role": "assistant", "content": f"turn {index}", "supervise": True}
            for index in range(turns)
        ],
    }
    dataset.write_text(json.dumps(row) + "\n", encoding="utf-8")
    digest = module().sha256_file(dataset)
    dataset.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "dataset_sha256": digest,
                "examples": 1,
                "cases": 1,
                "readiness": {"require_judged": True, "infrastructure_incomplete": 0},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / f"{name}.complete.json").write_text(
        json.dumps(
            {
                "sha256": digest,
                "examples": 1,
                "cases": 1,
                "writer_provider": "codex",
                "writer_model": "gpt-6-astra",
                "judge_provider": "codex",
                "judge_model": "gpt-6-astra",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return dataset


def write_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """name: muse-glimmer-30b-mcp-template
dataset: data/processed/old.jsonl
dataset_sha256: "old"
expected_examples: 0
expected_cases: 0
output_dir: /home/work/models/rca-adapters/old
epochs: 2
max_steps: 999
save_steps: 20
sequence_mode: turns
init_adapter: /old/adapter
turns_per_episode: 99
wandb:
  version: mcp-template
""",
        encoding="utf-8",
    )


def test_missing_recovered_dataset_is_wait_state_not_permanent_failure(tmp_path, monkeypatch):
    cont = module()
    monkeypatch.setattr(cont, "ROOT", tmp_path)
    write_template(tmp_path / "template.yaml")
    (tmp_path / "split.yaml").write_text("train: [case-a]\nvalidation: []\nsealed_eval: []\n", encoding="utf-8")
    config = {
        "template": "template.yaml",
        "stages": [
            {
                "key": "tt4",
                "version": "recovered-tt4",
                "dataset": "datasets/tt4.jsonl",
                "split": "split.yaml",
                "recovery_expected": {
                    "writer_provider": "codex",
                    "writer_model": "gpt-6-astra",
                    "judge_provider": "codex",
                    "judge_model": "gpt-6-astra",
                },
            }
        ],
    }
    assert cont.run_chain(config, tmp_path / "work", execute=False) == 3
    state = json.loads((tmp_path / "work/state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "waiting_for_recovered_data"


def test_split_overlap_is_rejected(tmp_path, monkeypatch):
    cont = module()
    monkeypatch.setattr(cont, "ROOT", tmp_path)
    (tmp_path / "split.yaml").write_text("train: [case-a]\nvalidation: [case-a]\n", encoding="utf-8")
    with pytest.raises(cont.ChainError, match="overlap"):
        cont.validate_split_disjoint(tmp_path / "split.yaml")


def test_observation_admission_is_separate_opt_in_gate(tmp_path, monkeypatch):
    cont = module()
    monkeypatch.setattr(cont, "ROOT", tmp_path)
    dataset = write_dataset(tmp_path, "joint", "case-a")
    (tmp_path / "split.yaml").write_text("train: [case-a]\nvalidation: []\nsealed_eval: []\n")
    stage = {"key": "joint", "dataset": str(dataset), "split": "split.yaml"}
    calls = []

    def reject(dataset, manifest):
        calls.append((dataset, manifest))
        raise cont.ChainError("evidence admission verification failed")

    monkeypatch.setattr(cont, "verify_evidence_admission", reject)
    assert cont.validate_dataset(stage)["examples"] == 1
    assert not calls
    with pytest.raises(cont.ChainError, match="evidence admission"):
        cont.validate_dataset({**stage, "require_evidence_admission": True})
    assert calls == [(dataset, dataset.with_suffix(".manifest.json"))]


def test_config_generation_starts_from_base_then_chains_previous_stage(tmp_path, monkeypatch):
    cont = module()
    monkeypatch.setattr(cont, "ROOT", tmp_path)
    write_template(tmp_path / "template.yaml")
    (tmp_path / "split.yaml").write_text("train: [case-a]\nvalidation: [case-b]\nsealed_eval: []\n", encoding="utf-8")
    write_dataset(tmp_path, "tt4", "case-a", turns=9)
    write_dataset(tmp_path, "ob4", "case-a", turns=9)
    first = {
        "key": "tt4",
        "version": "recovered-tt4",
        "dataset": "datasets/tt4.jsonl",
        "split": "split.yaml",
        "turns_per_episode": 6,
        "recovery_expected": {
            "writer_provider": "codex",
            "writer_model": "gpt-6-astra",
            "judge_provider": "codex",
            "judge_model": "gpt-6-astra",
        },
    }
    second = {
        "key": "ob4",
        "version": "recovered-ob4",
        "dataset": "datasets/ob4.jsonl",
        "split": "split.yaml",
        "turns_per_episode": 6,
        "recovery_expected": {
            "writer_provider": "codex",
            "writer_model": "gpt-6-astra",
            "judge_provider": "codex",
            "judge_model": "gpt-6-astra",
        },
    }
    rendered_first = cont.render_stage_config(tmp_path / "template.yaml", first, None, tmp_path / "work")
    rendered_second = cont.render_stage_config(tmp_path / "template.yaml", second, rendered_first, tmp_path / "work")
    text_first = Path(rendered_first["config_path"]).read_text(encoding="utf-8")
    text_second = Path(rendered_second["config_path"]).read_text(encoding="utf-8")
    assert "init_adapter:" not in text_first
    assert "muse-glimmer-30b-mcp-recovered-tt4" in text_first
    assert "turns_per_episode: 6" in text_first
    assert "version: mcp-recovered-tt4" in text_first
    assert "init_adapter: /home/work/models/rca-adapters/muse-glimmer-30b-mcp-recovered-tt4/adapter" in text_second
    assert rendered_first["max_steps"] == 50
    epoch = cont.render_stage_config(
        tmp_path / "template.yaml", {**first, "training_budget": "one_epoch"}, None, tmp_path / "epoch"
    )
    assert epoch["max_steps"] == -1
    assert "epochs: 1" in Path(epoch["config_path"]).read_text()
    with pytest.raises(cont.ChainError, match="training_budget"):
        cont.render_stage_config(
            tmp_path / "template.yaml", {**first, "training_budget": "unknown"}, None, tmp_path / "bad"
        )


def test_judge_provider_flag_is_used_only_when_supported(tmp_path, monkeypatch):
    cont = module()
    monkeypatch.setattr(cont, "ROOT", tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "mcp_judge_runs.py").write_text("\n", encoding="utf-8")
    (scripts / "mcp_eval_report.py").write_text("\n", encoding="utf-8")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return type("Result", (), {"returncode": 0, "stdout": '{"cached": 1}\n', "stderr": ""})()

    monkeypatch.setattr(cont.subprocess, "run", fake_run)
    monkeypatch.setattr(cont.subprocess, "check_output", lambda args, **kwargs: "[]")
    root = tmp_path / "eval"
    root.mkdir()
    code = cont.judge_and_report(
        [root],
        {"mechanism_judge": {"provider": "codex", "model": "gpt-6-astra", "rubric": "v2", "workers": 1}},
        tmp_path / "work",
    )
    assert code == 0
    assert "--provider" in calls[0] and "codex" in calls[0]


def test_execute_restart_skips_remote_completed_stage_without_sync_or_launch(tmp_path, monkeypatch):
    cont = module()
    monkeypatch.setattr(cont, "ROOT", tmp_path)
    write_template(tmp_path / "template.yaml")
    (tmp_path / "split.yaml").write_text("train: [case-a]\nvalidation: []\nsealed_eval: []\n", encoding="utf-8")
    write_dataset(tmp_path, "tt4", "case-a", turns=8)
    calls = {"sync": 0, "launch": 0}

    class FakeRemote:
        remote_root = "/remote/root"

        def __init__(self, config, workspace):
            pass

        def run(self, command, *, check=True):
            if "run-mcp-train.sh" in command and "nohup" in command:
                calls["launch"] += 1
            return type("Result", (), {"returncode": 0, "stdout": "abc  /home/work/run-mcp-train.sh\nhead  -\n1,0\n", "stderr": ""})()

        def sync(self, files):
            calls["sync"] += 1

    monkeypatch.setattr(cont, "Remote", FakeRemote)
    config = {
        "template": "template.yaml",
        "poll_seconds": 0,
        "remote": {"launcher": "/home/work/run-mcp-train.sh", "gpu_index": 1},
        "sync": {"paths": []},
        "stages": [
            {
                "key": "tt4",
                "version": "recovered-tt4",
                "dataset": "datasets/tt4.jsonl",
                "split": "split.yaml",
                "turns_per_episode": 6,
                "recovery_expected": {
                    "writer_provider": "codex",
                    "writer_model": "gpt-6-astra",
                    "judge_provider": "codex",
                    "judge_model": "gpt-6-astra",
                },
            }
        ],
        "validation": {"models": []},
        "mechanism_judge": {"enabled": False},
    }
    workspace = tmp_path / "work"
    rendered = cont.render_stage_config(tmp_path / "template.yaml", config["stages"][0], None, workspace)
    cont.write_stage_record(workspace, rendered, "complete")
    assert cont.run_chain(config, workspace, execute=True) == 0
    assert calls == {"sync": 0, "launch": 0}
    record = json.loads((workspace / "stages/tt4.json").read_text(encoding="utf-8"))
    assert record["phase"] == "complete" and record["remote_reconciled"] is True


def test_launch_wrapper_turns_preflight_failure_into_visible_exitcode(tmp_path, monkeypatch):
    cont = module()
    monkeypatch.setattr(cont, "ROOT", tmp_path)
    write_template(tmp_path / "template.yaml")
    (tmp_path / "split.yaml").write_text("train: [case-a]\nvalidation: []\nsealed_eval: []\n", encoding="utf-8")
    write_dataset(tmp_path, "tt4", "case-a", turns=8)
    launched = {"command": ""}

    class FakeRemote:
        remote_root = "/remote/root"

        def __init__(self, config, workspace):
            pass

        def run(self, command, *, check=True):
            if "sha256sum" in command and "nvidia-smi" in command:
                return type("Result", (), {"returncode": 0, "stdout": "abc  /home/work/run-mcp-train.sh\nhead  -\n1,0\n", "stderr": ""})()
            if "adapter/adapter_model.safetensors" in command:
                return type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            if "test -e /home/work/models/rca-adapters" in command:
                return type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            if "nohup bash -c" in command and "run-mcp-train.sh" in command:
                launched["command"] = command
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            if "training-mcp-recovered-tt4.pid" in command and "cat" in command:
                return type("Result", (), {"returncode": 0, "stdout": "123\n", "stderr": ""})()
            if "training-mcp-recovered-tt4.exitcode" in command and "cat" in command:
                if launched["command"]:
                    return type("Result", (), {"returncode": 0, "stdout": "75\n", "stderr": ""})()
                return type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            return type("Result", (), {"returncode": 0, "stdout": "0\n", "stderr": ""})()

        def sync(self, files):
            pass

    monkeypatch.setattr(cont, "Remote", FakeRemote)
    config = {
        "template": "template.yaml",
        "poll_seconds": 0,
        "remote": {"launcher": "/home/work/run-mcp-train.sh", "gpu_index": 1},
        "sync": {"paths": []},
        "stages": [
            {
                "key": "tt4",
                "version": "recovered-tt4",
                "dataset": "datasets/tt4.jsonl",
                "split": "split.yaml",
                "turns_per_episode": 6,
                "recovery_expected": {
                    "writer_provider": "codex",
                    "writer_model": "gpt-6-astra",
                    "judge_provider": "codex",
                    "judge_model": "gpt-6-astra",
                },
            }
        ],
        "validation": {"models": []},
        "mechanism_judge": {"enabled": False},
    }
    with pytest.raises(cont.ChainError, match="exited 75"):
        cont.run_chain(config, tmp_path / "work", execute=True)
    assert "nohup bash -c" in launched["command"]
    assert "test -f /remote/root/training-mcp-recovered-tt4.exitcode || printf" in launched["command"]


def test_remote_complete_with_stale_stage_hash_is_rejected(tmp_path, monkeypatch):
    cont = module()
    monkeypatch.setattr(cont, "ROOT", tmp_path)
    write_template(tmp_path / "template.yaml")
    (tmp_path / "split.yaml").write_text("train: [case-a]\nvalidation: []\nsealed_eval: []\n", encoding="utf-8")
    write_dataset(tmp_path, "tt4", "case-a", turns=8)

    class FakeRemote:
        remote_root = "/remote/root"

        def __init__(self, config, workspace):
            pass

        def run(self, command, *, check=True):
            return type("Result", (), {"returncode": 0, "stdout": "abc  /home/work/run-mcp-train.sh\nhead  -\n1,0\n", "stderr": ""})()

        def sync(self, files):
            raise AssertionError("must not sync stale completed stage")

    monkeypatch.setattr(cont, "Remote", FakeRemote)
    workspace = tmp_path / "work"
    stage = {
        "key": "tt4",
        "version": "recovered-tt4",
        "dataset": "datasets/tt4.jsonl",
        "split": "split.yaml",
        "turns_per_episode": 6,
        "recovery_expected": {
            "writer_provider": "codex",
            "writer_model": "gpt-6-astra",
            "judge_provider": "codex",
            "judge_model": "gpt-6-astra",
        },
    }
    rendered = cont.render_stage_config(tmp_path / "template.yaml", stage, None, workspace)
    cont.write_stage_record(workspace, rendered, "complete")
    record = json.loads((workspace / "stages/tt4.json").read_text(encoding="utf-8"))
    record["dataset_sha256"] = "stale"
    (workspace / "stages/tt4.json").write_text(json.dumps(record), encoding="utf-8")
    config = {
        "template": "template.yaml",
        "poll_seconds": 0,
        "remote": {"launcher": "/home/work/run-mcp-train.sh", "gpu_index": 1},
        "sync": {"paths": []},
        "stages": [stage],
        "validation": {"models": []},
        "mechanism_judge": {"enabled": False},
    }
    with pytest.raises(cont.ChainError, match="identity mismatch"):
        cont.run_chain(config, workspace, execute=True)


def test_complete_marker_requires_writer_and_judge_provenance(tmp_path, monkeypatch):
    cont = module()
    monkeypatch.setattr(cont, "ROOT", tmp_path)
    (tmp_path / "split.yaml").write_text("train: [case-a]\nvalidation: []\nsealed_eval: []\n", encoding="utf-8")
    write_dataset(tmp_path, "tt4", "case-a", turns=8)
    stage = {
        "key": "tt4",
        "version": "recovered-tt4",
        "dataset": "datasets/tt4.jsonl",
        "split": "split.yaml",
        "turns_per_episode": 6,
        "recovery_expected": {
            "writer_provider": "codex",
            "writer_model": "gpt-6-astra",
            "judge_provider": "codex",
            "judge_model": "gpt-6.1-sol",
        },
    }
    with pytest.raises(cont.ChainError, match="judge_model mismatch"):
        cont.validate_dataset(stage)
    stage["recovery_expected"]["judge_model"] = "gpt-6-astra"
    assert cont.validate_dataset(stage)["examples"] == 1


def test_launching_restart_reconciles_remote_pid_without_relaunch(tmp_path, monkeypatch):
    cont = module()
    monkeypatch.setattr(cont, "ROOT", tmp_path)
    write_template(tmp_path / "template.yaml")
    (tmp_path / "split.yaml").write_text("train: [case-a]\nvalidation: []\nsealed_eval: []\n", encoding="utf-8")
    write_dataset(tmp_path, "tt4", "case-a", turns=8)
    calls = {"launch": 0, "sync": 0, "completed": False}

    class FakeRemote:
        remote_root = "/remote/root"

        def __init__(self, config, workspace):
            pass

        def run(self, command, *, check=True):
            if "sha256sum" in command and "nvidia-smi" in command:
                return type("Result", (), {"returncode": 0, "stdout": "abc  /home/work/run-mcp-train.sh\nhead  -\n1,0\n", "stderr": ""})()
            if "nohup bash -c" in command:
                calls["launch"] += 1
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            if "adapter/adapter_model.safetensors" in command:
                return type("Result", (), {"returncode": 0 if calls["completed"] else 1, "stdout": "", "stderr": ""})()
            if "test -e /home/work/models/rca-adapters" in command:
                return type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            if "training-mcp-recovered-tt4.pid" in command and "cat" in command:
                return type("Result", (), {"returncode": 0, "stdout": "321\n", "stderr": ""})()
            if "training-mcp-recovered-tt4.exitcode" in command and "cat" in command:
                calls["completed"] = True
                return type("Result", (), {"returncode": 0, "stdout": "0\n", "stderr": ""})()
            if "kill -0" in command:
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            return type("Result", (), {"returncode": 0, "stdout": "0\n", "stderr": ""})()

        def sync(self, files):
            calls["sync"] += 1

    monkeypatch.setattr(cont, "Remote", FakeRemote)
    config = {
        "template": "template.yaml",
        "poll_seconds": 0,
        "remote": {"launcher": "/home/work/run-mcp-train.sh", "gpu_index": 1},
        "sync": {"paths": []},
        "stages": [
            {
                "key": "tt4",
                "version": "recovered-tt4",
                "dataset": "datasets/tt4.jsonl",
                "split": "split.yaml",
                "turns_per_episode": 6,
                "recovery_expected": {
                    "writer_provider": "codex",
                    "writer_model": "gpt-6-astra",
                    "judge_provider": "codex",
                    "judge_model": "gpt-6-astra",
                },
            }
        ],
        "validation": {"models": []},
        "mechanism_judge": {"enabled": False},
    }
    workspace = tmp_path / "work"
    rendered = cont.render_stage_config(tmp_path / "template.yaml", config["stages"][0], None, workspace)
    cont.write_stage_record(
        workspace,
        rendered,
        "launching",
        remote_config="/remote/root/generated.yaml",
        pid_file="/remote/root/training-mcp-recovered-tt4.pid",
        launch_log="/remote/root/train-mcp-recovered-tt4.launch.log",
    )
    assert cont.run_chain(config, workspace, execute=True) == 0
    assert calls["launch"] == 0
    assert calls["sync"] == 0
    record = json.loads((workspace / "stages/tt4.json").read_text(encoding="utf-8"))
    assert record["phase"] == "complete"


def test_execute_trains_ready_first_stage_before_waiting_for_later_dataset(tmp_path, monkeypatch):
    cont = module()
    monkeypatch.setattr(cont, "ROOT", tmp_path)
    write_template(tmp_path / "template.yaml")
    (tmp_path / "split.yaml").write_text("train: [case-a]\nvalidation: []\nsealed_eval: []\n", encoding="utf-8")
    write_dataset(tmp_path, "tt4", "case-a", turns=8)
    calls = {"launch": 0, "sync": 0, "completed": False}

    class FakeRemote:
        remote_root = "/remote/root"

        def __init__(self, config, workspace):
            pass

        def run(self, command, *, check=True):
            if "sha256sum" in command and "nvidia-smi" in command:
                return type("Result", (), {"returncode": 0, "stdout": "abc  /home/work/run-mcp-train.sh\nhead  -\n1,0\n", "stderr": ""})()
            if "nohup bash -c" in command:
                calls["launch"] += 1
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            if "adapter/adapter_model.safetensors" in command:
                return type("Result", (), {"returncode": 0 if calls["completed"] else 1, "stdout": "", "stderr": ""})()
            if "test -e /home/work/models/rca-adapters" in command:
                return type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            if "training-mcp-recovered-tt4.pid" in command and "cat" in command:
                return type("Result", (), {"returncode": 0, "stdout": "123\n", "stderr": ""})()
            if "training-mcp-recovered-tt4.exitcode" in command and "cat" in command:
                calls["completed"] = True
                return type("Result", (), {"returncode": 0, "stdout": "0\n", "stderr": ""})()
            return type("Result", (), {"returncode": 0, "stdout": "0\n", "stderr": ""})()

        def sync(self, files):
            calls["sync"] += 1

    def stop_after_first_wait(seconds):
        raise RuntimeError("stop after first later-stage wait")

    monkeypatch.setattr(cont, "Remote", FakeRemote)
    monkeypatch.setattr(cont.time, "sleep", stop_after_first_wait)
    config = {
        "template": "template.yaml",
        "poll_seconds": 0,
        "remote": {"launcher": "/home/work/run-mcp-train.sh", "gpu_index": 1},
        "sync": {"paths": []},
        "stages": [
            {
                "key": "tt4",
                "version": "recovered-tt4",
                "dataset": "datasets/tt4.jsonl",
                "split": "split.yaml",
                "turns_per_episode": 6,
                "recovery_expected": {
                    "writer_provider": "codex",
                    "writer_model": "gpt-6-astra",
                    "judge_provider": "codex",
                    "judge_model": "gpt-6-astra",
                },
            },
            {
                "key": "ob4",
                "version": "recovered-ob4",
                "dataset": "datasets/ob4.jsonl",
                "split": "split.yaml",
                "turns_per_episode": 6,
                "recovery_expected": {
                    "writer_provider": "codex",
                    "writer_model": "gpt-6-astra",
                    "judge_provider": "codex",
                    "judge_model": "gpt-6-astra",
                },
            },
        ],
        "validation": {"models": []},
        "mechanism_judge": {"enabled": False},
    }
    workspace = tmp_path / "work"
    with pytest.raises(RuntimeError, match="later-stage wait"):
        cont.run_chain(config, workspace, execute=True)
    assert calls["sync"] == 1
    assert calls["launch"] == 1
    assert json.loads((workspace / "state.json").read_text(encoding="utf-8"))["stage"] == "ob4"
    stages = json.loads((workspace / "generated-stages.json").read_text(encoding="utf-8"))["stages"]
    assert [stage["key"] for stage in stages] == ["tt4"]
    assert json.loads((workspace / "stages/tt4.json").read_text(encoding="utf-8"))["phase"] == "complete"


def test_sync_inventory_is_refreshed_after_recovered_data_wait(tmp_path, monkeypatch):
    cont = module()
    monkeypatch.setattr(cont, "ROOT", tmp_path)
    source_dir = tmp_path / "src/rca_lab/mcp"
    source_dir.mkdir(parents=True)
    (source_dir / "context.py").write_text("# existing source\n", encoding="utf-8")
    dataset = tmp_path / "datasets/tt4.jsonl"
    manifest = tmp_path / "datasets/tt4.manifest.json"
    config_path = tmp_path / "generated/tt4.yaml"
    dataset.parent.mkdir(parents=True)
    config_path.parent.mkdir(parents=True)
    dataset.write_text("{}\n", encoding="utf-8")
    manifest.write_text("{}\n", encoding="utf-8")
    config_path.write_text("name: tt4\n", encoding="utf-8")
    synced: list[list[str]] = []
    calls = {"render": 0, "complete": False}

    def fake_render(template, raw_stage, previous, workspace):
        calls["render"] += 1
        if calls["render"] == 1:
            (source_dir / "observations.py").write_text("# late source\n", encoding="utf-8")
            raise cont.Waiting("waiting for late recovered data")
        return {
            "key": "tt4",
            "version": "recovered-tt4",
            "run_id": "mcp-recovered-tt4",
            "model_name": "muse-glimmer-30b-mcp-recovered-tt4",
            "dataset": str(dataset),
            "manifest": str(manifest),
            "config_path": str(config_path),
            "sha256": "dataset-sha",
        }

    class FakeRemote:
        remote_root = "/remote/root"

        def __init__(self, config, workspace):
            pass

        def run(self, command, *, check=True):
            if "sha256sum" in command and "nvidia-smi" in command:
                return type("Result", (), {"returncode": 0, "stdout": "abc  /home/work/run-mcp-train.sh\nhead  -\n1,0\n", "stderr": ""})()
            if "adapter/adapter_model.safetensors" in command:
                return type("Result", (), {"returncode": 0 if calls["complete"] else 1, "stdout": "", "stderr": ""})()
            if "test -e /home/work/models/rca-adapters" in command:
                return type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            if "training-mcp-recovered-tt4.pid" in command and "cat" in command:
                return type("Result", (), {"returncode": 0, "stdout": "123\n", "stderr": ""})()
            if "training-mcp-recovered-tt4.exitcode" in command and "cat" in command:
                calls["complete"] = True
                return type("Result", (), {"returncode": 0, "stdout": "0\n", "stderr": ""})()
            return type("Result", (), {"returncode": 0, "stdout": "0\n", "stderr": ""})()

        def sync(self, files):
            synced.append([cont.rel(path) for path in files])

    monkeypatch.setattr(cont, "Remote", FakeRemote)
    monkeypatch.setattr(cont, "render_stage_config", fake_render)
    monkeypatch.setattr(cont.time, "sleep", lambda seconds: None)
    config = {
        "template": "template.yaml",
        "poll_seconds": 0,
        "remote": {"launcher": "/home/work/run-mcp-train.sh", "gpu_index": 1},
        "sync": {"paths": ["src/rca_lab/mcp"]},
        "stages": [{"key": "tt4"}],
        "validation": {"models": []},
        "mechanism_judge": {"enabled": False},
    }
    assert cont.run_chain(config, tmp_path / "work", execute=True) == 0
    assert "src/rca_lab/mcp/observations.py" in synced[0]
