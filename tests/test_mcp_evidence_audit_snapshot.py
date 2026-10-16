"""Admission and immutable snapshot contracts for the offline P0 audit."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("audit_script", Path(__file__).parents[1] / "scripts/mcp_evidence_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def fixture_run(tmp_path, *, case="case-train", stamp="collected", score_case=None):
    sources = tmp_path / "sources"
    run = sources / "teacher" / case / "run1"
    run.mkdir(parents=True)
    messages = [{"role": "user", "content": "alarm"},
                {"role": "assistant", "reasoning_content": "Investigate alarm", "tool_calls":
                 [{"id": "c1", "function": {"name": "scan", "arguments": "{}"}}]}]
    stats = {"judge_provider": "codex", "judge_model": "gpt-6.1-sol", "view_source": stamp,
             "judge": {"1": {"id": 1, "consistent": True, "grounded": True}}, "failed": []}
    (run / "trajectory.rationalized.jsonl").write_text("\n".join(json.dumps(x) for x in messages))
    (run / "rationalize.json").write_text(json.dumps(stats))
    (run / "score.json").write_text(json.dumps({"case_id": score_case or case}))
    split = tmp_path / "split.yaml"
    split.write_text("train: [case-train]\nvalidation: [case-heldout]\n")
    return sources, split, run


def test_split_overlap_rejected():
    with pytest.raises(ValueError, match="overlaps"):
        audit.train_members({"train": ["x"], "sealed_eval": ["x"]})


@pytest.mark.parametrize("case,stamp,score_case", [
    ("case-heldout", "collected", None),
    ("case-train", "replay:stale", None),
    ("case-train", "collected", "case-wrong"),
])
def test_nonadmissible_sources_rejected(tmp_path, case, stamp, score_case):
    sources, split, _ = fixture_run(tmp_path, case=case, stamp=stamp, score_case=score_case)
    with pytest.raises(ValueError, match="no eligible"):
        audit.snapshot(sources, split, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_snapshot_blinds_judge_and_preserves_source(tmp_path):
    sources, split, run = fixture_run(tmp_path)
    original = (run / "trajectory.rationalized.jsonl").read_bytes()
    out = tmp_path / "out"
    result = audit.snapshot(sources, split, out)
    assert result["cases"] == 1
    assert result["items"] == 1
    blind = json.loads((out / "blind-items.json").read_text())
    assert set(blind[0]) == {"audit_id", "item"}
    assert "legacy_verdict" not in json.dumps(blind)
    assert (out / "snapshots/00/trajectory.rationalized.jsonl").read_bytes() == original
    assert (run / "trajectory.rationalized.jsonl").read_bytes() == original
    with pytest.raises(FileExistsError):
        audit.snapshot(sources, split, out)


def test_blind_serialization_matches_budget(tmp_path):
    sources, split, _ = fixture_run(tmp_path)
    out = tmp_path / "out"
    result = audit.snapshot(sources, split, out, max_chars=1600)
    text = (out / "blind-items.json").read_text()
    blind = json.loads(text)
    assert text == audit.canonical_json(blind)
    item_size = len(audit.canonical_json(blind[0]["item"]))
    assert item_size <= 1600
    assert result["item_chars_max"] == item_size


def test_final_selection_requires_real_submit_and_excludes_prior_case(tmp_path):
    sources, split, run = fixture_run(tmp_path)
    with pytest.raises(ValueError, match="no eligible"):
        audit.snapshot(sources, split, tmp_path / "no-final", selection="final_and_rejected")
    trajectory = run / "trajectory.rationalized.jsonl"
    messages = [json.loads(x) for x in trajectory.read_text().splitlines()]
    messages[-1]["tool_calls"][0]["function"]["name"] = "submit_rca"
    trajectory.write_text("\n".join(json.dumps(x) for x in messages))
    with pytest.raises(ValueError, match="no eligible"):
        audit.snapshot(sources, split, tmp_path / "excluded", selection="final_and_rejected",
                       exclude_cases={"case-train"})
    audit.snapshot(sources, split, tmp_path / "final", selection="final_and_rejected")
    row = json.loads((tmp_path / "final/items.jsonl").read_text())
    assert row["phase"] == "final_submission"
