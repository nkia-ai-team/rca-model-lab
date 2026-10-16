import importlib.util
import json
from pathlib import Path

import pytest


def module():
    path = Path(__file__).resolve().parents[1] / "scripts/mcp_prepare_joint_recovered.py"
    spec = importlib.util.spec_from_file_location("prepare_joint", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def write_split(path: Path, train: list[str], validation: list[str] | None = None) -> None:
    path.write_text(
        "train:\n"
        + "".join(f"- {case}\n" for case in train)
        + "validation:\n"
        + "".join(f"- {case}\n" for case in (validation or []))
        + "sealed_eval: []\npublic_compare: []\nexcluded: []\n",
        encoding="utf-8",
    )


def write_dataset(prep, root: Path, name: str, rows: list[dict]) -> Path:
    dataset = root / "datasets" / f"{name}.jsonl"
    dataset.parent.mkdir(parents=True, exist_ok=True)
    dataset.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    digest = prep.recovered.sha256_file(dataset)
    cases = len({row["case_id"] for row in rows})
    dataset.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "dataset_sha256": digest,
                "examples": len(rows),
                "cases": cases,
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
                "examples": len(rows),
                "cases": cases,
                "writer_provider": "codex",
                "writer_model": "gpt-6-astra",
                "judge_provider": "codex",
                "judge_model": "gpt-6.1-sol",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return dataset


def row(case_id: str, turns: int = 8) -> dict:
    return {
        "case_id": case_id,
        "messages": [
            {"role": "assistant", "content": f"turn {index}", "supervise": True}
            for index in range(turns)
        ],
    }


def configure(prep, tmp_path: Path, stages: list[dict]) -> Path:
    prep.ROOT = tmp_path
    prep.recovered.ROOT = tmp_path
    config = {
        "recovery_expected": {
            "writer_provider": "codex",
            "writer_model": "gpt-6-astra",
            "judge_provider": "codex",
            "judge_model": "gpt-6.1-sol",
        },
        "stages": stages,
    }
    path = tmp_path / "config.yaml"
    import yaml

    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def test_prepare_joint_writes_stable_dataset_manifest_complete_and_split(tmp_path):
    prep = module()
    write_split(tmp_path / "a-split.yaml", ["case-a1", "case-a2"], ["case-a-val"])
    write_split(tmp_path / "b-split.yaml", ["case-b1"], ["case-b-val"])
    write_dataset(prep, tmp_path, "a", [row("case-a1"), row("case-a2", turns=4)])
    write_dataset(prep, tmp_path, "b", [row("case-b1")])
    config = configure(
        prep,
        tmp_path,
        [
            {"key": "a", "dataset": "datasets/a.jsonl", "split": "a-split.yaml", "version": "a"},
            {"key": "b", "dataset": "datasets/b.jsonl", "split": "b-split.yaml", "version": "b"},
        ],
    )
    result = prep.prepare_joint(config, tmp_path / "joint")

    assert result["examples"] == 3
    assert result["cases"] == 3
    assert result["supervised_turns"] == 16
    assert Path(result["dataset"]).name == "joint.jsonl"
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    complete = json.loads(Path(result["complete"]).read_text(encoding="utf-8"))
    split_text = Path(result["split"]).read_text(encoding="utf-8")
    assert manifest["readiness"]["source_datasets_validated"] == 2
    assert manifest["provenance"]["new_judging"] is False
    assert manifest["provenance"]["judge_model"] == "gpt-6.1-sol"
    assert complete["sha256"] == manifest["dataset_sha256"]
    assert complete["new_judging"] is False
    assert "case-a-val" in split_text and "case-b-val" in split_text

    assert prep.prepare_joint(config, tmp_path / "joint")["dataset_sha256"] == result["dataset_sha256"]


def test_prepare_joint_rejects_cross_source_case_collision(tmp_path):
    prep = module()
    write_split(tmp_path / "a-split.yaml", ["case-same"])
    write_split(tmp_path / "b-split.yaml", ["case-same"])
    write_dataset(prep, tmp_path, "a", [row("case-same")])
    write_dataset(prep, tmp_path, "b", [row("case-same")])
    config = configure(
        prep,
        tmp_path,
        [
            {"key": "a", "dataset": "datasets/a.jsonl", "split": "a-split.yaml", "version": "a"},
            {"key": "b", "dataset": "datasets/b.jsonl", "split": "b-split.yaml", "version": "b"},
        ],
    )
    with pytest.raises(prep.JointError, match="case id collisions"):
        prep.prepare_joint(config, tmp_path / "joint")


def test_prepare_joint_refuses_divergent_existing_output(tmp_path):
    prep = module()
    write_split(tmp_path / "a-split.yaml", ["case-a"])
    write_dataset(prep, tmp_path, "a", [row("case-a")])
    config = configure(
        prep,
        tmp_path,
        [{"key": "a", "dataset": "datasets/a.jsonl", "split": "a-split.yaml", "version": "a"}],
    )
    out_root = tmp_path / "joint"
    (out_root / "datasets").mkdir(parents=True)
    (out_root / "datasets/joint.jsonl").write_text("old\n", encoding="utf-8")
    with pytest.raises(prep.JointError, match="divergent output"):
        prep.prepare_joint(config, out_root)
