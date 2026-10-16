import importlib.util
import json
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/mcp_prepare_polestar_expanded.py"
    spec = importlib.util.spec_from_file_location("polestar_expanded", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module.recovered, "ROOT", tmp_path)
    original = {"train": ["old"], "validation": ["val"], "sealed_eval": ["sealed"], "excluded": ["excluded"]}
    expanded = {**original, "train": ["old", "new"]}
    (tmp_path / "original.yaml").write_text(yaml.safe_dump(original))
    (tmp_path / "expanded.yaml").write_text(yaml.safe_dump(expanded))
    provenance = {"writer_model": "gpt-6-astra", "judge_model": "gpt-6.1-sol"}
    sources = []
    for case in ["old", "new"]:
        dataset = tmp_path / case / "datasets" / f"{case}.jsonl"
        dataset.parent.mkdir(parents=True)
        row = {"case_id": case, "run": "run1", "messages": [
            {"role": "assistant", "content": str(i), "tool_calls": [{"function": {"name": "inspect"}}]}
            for i in range(9)
        ]}
        dataset.write_text(json.dumps(row) + "\n")
        digest = module.recovered.sha256_file(dataset)
        dataset.with_suffix(".manifest.json").write_text(json.dumps({
            "dataset_sha256": digest, "cases": 1, "examples": 1,
            "readiness": {"require_judged": True, "infrastructure_incomplete": 0},
        }))
        (dataset.parent.parent / f"{case}.complete.json").write_text(json.dumps({
            **provenance, "sha256": digest, "cases": 1, "examples": 1,
        }))
        sources.append({"key": case, "dataset": str(dataset), "split": "expanded.yaml"})
    config = {"split": "expanded.yaml", "original_split": "original.yaml",
              "out_root": "out", "sources": sources, "recovery_expected": provenance}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return module, path, tmp_path


def test_preserves_all_turns_and_source_bytes(setup):
    module, config, root = setup
    result = module.prepare(config)
    assert result["supervised_turns"] == 18
    assert result["turns_per_episode"] == 0
    assert (root / "out/datasets/polestar.jsonl").read_bytes() == b"".join(
        (root / case / "datasets" / f"{case}.jsonl").read_bytes() for case in ["old", "new"]
    )
    assert module.prepare(config) == result


def test_refuses_changed_protected_partition(setup):
    module, config, root = setup
    path = root / "expanded.yaml"
    split = yaml.safe_load(path.read_text())
    split["sealed_eval"] = []
    path.write_text(yaml.safe_dump(split))
    with pytest.raises(ValueError, match="protected partition"):
        module.prepare(config)
    assert not (root / "out/dataset.complete.json").exists()


def test_refuses_incomplete_new_data(setup):
    module, config, root = setup
    (root / "new/new.complete.json").unlink()
    with pytest.raises(module.recovered.Waiting):
        module.prepare(config)
    assert not (root / "out/dataset.complete.json").exists()
