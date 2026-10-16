import copy
import importlib.util
import json
from pathlib import Path

import pytest

from rca_lab.mcp.evidence_admission import MODEL, RUBRIC_VERSION, item_sha256, verdict_record


def module():
    path = Path(__file__).resolve().parents[1] / "scripts/mcp_prepare_admitted_recipe.py"
    spec = importlib.util.spec_from_file_location("mcp_prepare_admitted_recipe", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def sample_row(*, dataset_key: str | None = None, known_reject: bool = False) -> dict:
    scan = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "Need checkout metrics.",
        "tool_calls": [call("c1", "sample_metrics", {"target": "checkout"})],
    }
    if known_reject:
        scan["supervise"] = False
    row = {
        "case_id": "case-a",
        "run": "teacher/case-a/run1",
        "messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "alarm checkout"},
            scan,
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": "sample_metrics",
                "content": "checkout p95 rose after repo p95 rose",
            },
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "Repo p95 rose before checkout p95, so submit the supported cause.",
                "tool_calls": [call("c2", "submit_rca", {"cause": "repo p95"})],
            },
        ],
        "tools": [{"type": "function", "function": {"name": "sample_metrics"}}],
    }
    if dataset_key is not None:
        row["dataset_key"] = dataset_key
    return row


def source_row_with_supervise_metadata() -> dict:
    row = sample_row()
    row["messages"][2]["supervise"] = True
    row["messages"][4]["supervise"] = True
    return row


def quarantined_row_from_source(row: dict) -> dict:
    out = copy.deepcopy(row)
    out["messages"][2]["supervise"] = False
    out["messages"][4]["supervise"] = True
    return out


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def write_inputs(tmp_path: Path, prep, *, joint_row: dict | None = None, source_row: dict | None = None) -> tuple[Path, Path]:
    source = tmp_path / "sources/a.jsonl"
    joint = tmp_path / "joint.jsonl"
    source_rows = [source_row or sample_row()]
    joint_rows = [joint_row or sample_row()]
    write_jsonl(source, source_rows)
    write_jsonl(joint, joint_rows)
    manifest = tmp_path / "joint.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "dataset": str(joint),
                "dataset_sha256": prep.sha_file(joint),
                "sources": [
                    {
                        "key": "tt4",
                        "dataset": str(source),
                        "dataset_sha256": prep.sha_file(source),
                        "examples": 1,
                        "cases": 1,
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return joint, manifest


def supported_verdict(item: dict) -> dict:
    ref = item["evidence"]["refs"][0]["ref_id"]
    claim = {
        "status": "supported",
        "evidence_refs": [ref],
        "absence_based": False,
        "explanation": "visible metric supports the claim",
    }
    return {
        "rationale": claim,
        "action": dict(claim),
        "consistency": {"status": "consistent", "explanation": "certainty matches visible evidence"},
    }


def hold_verdict(item: dict) -> dict:
    return {
        "rationale": {
            "status": "insufficient_evidence",
            "evidence_refs": [],
            "absence_based": False,
            "explanation": "not enough visible evidence",
        },
        "action": {
            "status": "insufficient_evidence",
            "evidence_refs": [],
            "absence_based": False,
            "explanation": "not enough visible evidence",
        },
        "consistency": {"status": "insufficient_evidence", "explanation": "cannot compare certainty"},
    }


def cache_candidate(prep, cache_dir: Path, candidate: dict, verdict: dict) -> None:
    path = prep.evidence_judge.cache_path(
        cache_dir,
        item_hash=candidate["item_sha256"],
        rubric=RUBRIC_VERSION,
        model=MODEL,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(verdict_record(candidate["item"], verdict), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )


def load_candidates(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_build_candidates_attach_dataset_key_and_do_not_regain_known_reject(tmp_path):
    prep = module()
    joint, manifest = write_inputs(tmp_path, prep, joint_row=sample_row(known_reject=True))

    report = prep.build_candidates(joint, manifest, tmp_path / "candidates", turns_per_episode=6)

    candidates = load_candidates(tmp_path / "candidates/items.jsonl")
    assert report["candidates"] == 1
    assert candidates[0]["dataset_key"] == "tt4"
    assert candidates[0]["message_index"] == 4
    assert candidates[0]["final_submit"] is True
    assert "supervise" not in json.dumps(candidates[0]["item"], ensure_ascii=False)
    assert (tmp_path / "candidates/manifest.json").exists()


def test_supervise_metadata_does_not_change_candidate_item_hash(tmp_path):
    prep = module()
    with_flag = sample_row(known_reject=True)
    without_flag = copy.deepcopy(with_flag)
    without_flag["messages"][2].pop("supervise")
    joint_a, manifest_a = write_inputs(tmp_path / "a", prep, joint_row=with_flag, source_row=sample_row())
    joint_b, manifest_b = write_inputs(tmp_path / "b", prep, joint_row=without_flag, source_row=sample_row())

    prep.build_candidates(joint_a, manifest_a, tmp_path / "a/candidates", turns_per_episode=1)
    prep.build_candidates(joint_b, manifest_b, tmp_path / "b/candidates", turns_per_episode=1)

    a = load_candidates(tmp_path / "a/candidates/items.jsonl")[0]
    b = load_candidates(tmp_path / "b/candidates/items.jsonl")[0]
    assert a["item_sha256"] == b["item_sha256"] == item_sha256(a["item"])


def test_export_approved_only_preserves_context_and_writes_readiness_marker(tmp_path):
    prep = module()
    joint, manifest = write_inputs(tmp_path, prep)
    prep.build_candidates(joint, manifest, tmp_path / "candidates", turns_per_episode=2)
    candidates = load_candidates(tmp_path / "candidates/items.jsonl")
    cache_dir = tmp_path / "judged"
    for candidate in candidates:
        verdict = supported_verdict(candidate["item"]) if candidate["final_submit"] else hold_verdict(candidate["item"])
        cache_candidate(prep, cache_dir, candidate, verdict)

    report = prep.export_admitted(
        joint,
        manifest,
        tmp_path / "candidates/items.jsonl",
        cache_dir,
        tmp_path / "admitted",
        minimum_final_per_source=1,
    )

    row = json.loads((tmp_path / "admitted/joint.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["dataset_key"] == "tt4"
    assert row["messages"][1]["content"] == "alarm checkout"
    assert row["messages"][2]["supervise"] is False
    assert row["messages"][4]["supervise"] is True
    assert report["readiness"]["require_judged"] is True
    assert report["admission"]["decisions"] == {"approve": 1, "hold": 1}
    assert (tmp_path / "admitted/joint.complete.json").exists()
    assert prep.verify_admitted_dataset(tmp_path / "admitted/joint.jsonl", tmp_path / "admitted/joint.manifest.json")


def test_export_binding_uses_raw_source_item_when_quarantine_changes_supervise_metadata(tmp_path):
    prep = module()
    source = source_row_with_supervise_metadata()
    joint, manifest = write_inputs(tmp_path, prep, joint_row=quarantined_row_from_source(source), source_row=source)
    prep.build_candidates(joint, manifest, tmp_path / "candidates", turns_per_episode=2)
    candidates = load_candidates(tmp_path / "candidates/items.jsonl")
    cache_dir = tmp_path / "judged"
    for candidate in candidates:
        verdict = supported_verdict(candidate["item"]) if candidate["final_submit"] else hold_verdict(candidate["item"])
        cache_candidate(prep, cache_dir, candidate, verdict)

    report = prep.export_admitted(joint, manifest, tmp_path / "candidates/items.jsonl", cache_dir, tmp_path / "admitted")

    row = json.loads((tmp_path / "admitted/joint.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["messages"][2]["supervise"] is False
    assert row["messages"][4]["supervise"] is True
    assert report["admission"]["decisions"] == {"approve": 1}
    assert prep.verify_admitted_dataset(tmp_path / "admitted/joint.jsonl", tmp_path / "admitted/joint.manifest.json")


def test_verify_rejects_tampered_decisions_and_stale_cache(tmp_path):
    prep = module()
    joint, manifest = write_inputs(tmp_path, prep)
    prep.build_candidates(joint, manifest, tmp_path / "candidates", turns_per_episode=1)
    candidate = load_candidates(tmp_path / "candidates/items.jsonl")[0]
    cache_candidate(prep, tmp_path / "judged", candidate, supported_verdict(candidate["item"]))
    assert prep.resolve_decision(candidate, tmp_path / "judged" / "cache") == prep.resolve_decision(
        candidate, tmp_path / "judged"
    )
    prep.export_admitted(joint, manifest, tmp_path / "candidates/items.jsonl", tmp_path / "judged", tmp_path / "admitted")

    decisions = tmp_path / "admitted/admission_decisions.jsonl"
    decisions.write_text(decisions.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(prep.AdmittedRecipeError, match="decisions hash mismatch"):
        prep.verify_admitted_dataset(tmp_path / "admitted/joint.jsonl", tmp_path / "admitted/joint.manifest.json")

    prep = module()
    tmp2 = tmp_path / "stale"
    joint, manifest = write_inputs(tmp2, prep)
    prep.build_candidates(joint, manifest, tmp2 / "candidates", turns_per_episode=1)
    candidate = load_candidates(tmp2 / "candidates/items.jsonl")[0]
    cache_candidate(prep, tmp2 / "judged", candidate, supported_verdict(candidate["item"]))
    prep.export_admitted(joint, manifest, tmp2 / "candidates/items.jsonl", tmp2 / "judged", tmp2 / "admitted")
    cache_path = prep.evidence_judge.cache_path(
        tmp2 / "judged",
        item_hash=candidate["item_sha256"],
        rubric=RUBRIC_VERSION,
        model=MODEL,
    )
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cache["model"] = "claude-cli"
    cache_path.write_text(json.dumps(cache), encoding="utf-8")
    with pytest.raises(prep.AdmittedRecipeError, match="cache no longer matches"):
        prep.verify_admitted_dataset(tmp2 / "admitted/joint.jsonl", tmp2 / "admitted/joint.manifest.json")


def test_export_fails_if_any_candidate_cache_is_pending(tmp_path):
    prep = module()
    joint, manifest = write_inputs(tmp_path, prep)
    prep.build_candidates(joint, manifest, tmp_path / "candidates", turns_per_episode=1)

    with pytest.raises(prep.AdmittedRecipeError, match="pending admission cache"):
        prep.export_admitted(joint, manifest, tmp_path / "candidates/items.jsonl", tmp_path / "judged", tmp_path / "admitted")
    assert not (tmp_path / "admitted/joint.complete.json").exists()


def test_export_fails_closed_on_target_tamper(tmp_path):
    prep = module()
    joint, manifest = write_inputs(tmp_path, prep)
    prep.build_candidates(joint, manifest, tmp_path / "candidates", turns_per_episode=1)
    candidate = load_candidates(tmp_path / "candidates/items.jsonl")[0]
    cache_candidate(prep, tmp_path / "judged", candidate, supported_verdict(candidate["item"]))
    tampered = sample_row()
    tampered["messages"][4]["reasoning_content"] = "changed"
    write_jsonl(joint, [tampered])

    with pytest.raises(prep.AdmittedRecipeError, match="source identity mismatch"):
        prep.export_admitted(joint, manifest, tmp_path / "candidates/items.jsonl", tmp_path / "judged", tmp_path / "admitted")


def test_empty_sources_and_cross_source_dataset_key_mismatch_fail(tmp_path):
    prep = module()
    joint, _manifest = write_inputs(tmp_path, prep)
    bad_manifest = tmp_path / "bad.manifest.json"
    bad_manifest.write_text(json.dumps({"sources": []}), encoding="utf-8")
    with pytest.raises(prep.AdmittedRecipeError, match="nonempty sources"):
        prep.build_candidates(joint, bad_manifest, tmp_path / "out")

    mismatch_joint, mismatch_manifest = write_inputs(
        tmp_path / "mismatch",
        prep,
        joint_row=sample_row(dataset_key="wrong"),
        source_row=sample_row(),
    )
    with pytest.raises(prep.AdmittedRecipeError, match="dataset_key mismatch"):
        prep.build_candidates(mismatch_joint, mismatch_manifest, tmp_path / "mismatch/out")


def test_unlimited_recipe_keeps_more_than_six_targets_and_exports_all(tmp_path):
    prep = module()
    row = sample_row()
    final = row["messages"].pop()
    for index in range(7):
        row["messages"].extend([
            {"role": "assistant", "reasoning_content": "Inspect observed checkout latency.",
             "tool_calls": [call(f"extra-{index}", "sample_metrics", {"target": "checkout"})]},
            {"role": "tool", "tool_call_id": f"extra-{index}", "content": "latency elevated"},
        ])
    row["messages"].append(final)
    joint, manifest = write_inputs(tmp_path, prep, joint_row=row, source_row=row)
    report = prep.build_candidates(joint, manifest, tmp_path / "candidates")
    assert report["candidates"] == 9
    candidates = load_candidates(tmp_path / "candidates/items.jsonl")
    for candidate in candidates:
        cache_candidate(prep, tmp_path / "judged", candidate, supported_verdict(candidate["item"]))
    exported = prep.export_admitted(joint, manifest, tmp_path / "candidates/items.jsonl",
                                    tmp_path / "judged", tmp_path / "admitted")
    assert exported["supervised_turns"] == 9
    assert exported["turns_per_episode"] == 0
