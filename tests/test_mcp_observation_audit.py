import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("observation_audit", Path(__file__).parents[1] / "scripts/mcp_observation_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def _call(call_id: str, name: str, args: dict) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def _snapshot(tmp_path: Path, *, content: str | None = None, case_id: str = "case-train") -> Path:
    root = tmp_path / "snap"
    snap = root / "snapshots" / "00"
    snap.mkdir(parents=True)
    tool_content = content or json.dumps(
        {
            "status": "anomalous",
            "summary": "summary at head",
            "scope": {"target": "T1", "metric": "cpu"},
            "rows": [{"metric": f"m{i}", "value": i} for i in range(120)],
            "truncated": True,
            "total": 900,
            "returned": 450,
        },
        ensure_ascii=False,
    )
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "seed"},
        {"role": "assistant", "content": "", "reasoning_content": "r", "tool_calls": [_call("c1", "scan_metrics", {"target": "T1", "metric": "cpu"})]},
        {"role": "tool", "tool_call_id": "c1", "name": "scan_metrics", "content": tool_content},
        {"role": "assistant", "content": "", "reasoning_content": "claim", "tool_calls": [_call("final", "submit_rca", {})]},
    ]
    raw = "".join(json.dumps(message, ensure_ascii=False) + "\n" for message in messages)
    (snap / "trajectory.rationalized.jsonl").write_text(raw, encoding="utf-8")
    item = {
        "evidence": {
            "refs": [
                {
                    "ref_id": "m3",
                    "message_position": 3,
                    "role": "tool",
                    "tool_call_id": "c1",
                    "tool_name": "scan_metrics",
                    "paired_call_position": 2,
                    "source_sha256": audit.sha_text(tool_content),
                    "source_chars": len(tool_content),
                    "visible_chars": len(tool_content),
                }
            ]
        }
    }
    row = {
        "case_id": case_id,
        "turn_index": 4,
        "phase": "final",
        "audit_id": "00-4",
        "item": item,
        "trajectory_sha256": audit.sha_bytes(raw.encode("utf-8")),
    }
    (root / "items.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({"status": "inputs_prepared_not_judged", "cases": 1, "items": 1}), encoding="utf-8")
    (root / "split.yaml").write_text("train: [case-train]\nvalidation: [case-heldout]\n", encoding="utf-8")
    return root


def test_audit_reconstructs_stored_source_and_reports_legacy_old_loss(tmp_path: Path) -> None:
    root = _snapshot(tmp_path)
    report = audit.audit_snapshot(root)

    assert report["status"] == "complete"
    assert report["metrics"]["tool_refs"] == 1
    assert report["metrics"]["source_sha_mismatches"] == 0
    assert report["metrics"]["legacy_initial_source_reachable"] == 1
    assert report["metrics"]["legacy_old_source_reachable"] == 0
    assert report["metrics"]["structured_source_reachable"] == 1
    assert report["field_survival"]["status"] == {"available": 1, "legacy_initial": 1, "legacy_old": 1, "structured_present": 1, "structured_old": 1}
    assert report["field_survival"]["scope"]["structured_present"] == 1
    assert report["per_ref"][0]["structured_read_exact"] is True


def test_audit_flags_train_membership_and_already_capped_sources(tmp_path: Path) -> None:
    capped = "x" * 6001 + "\n...[응답 절단: 전체 7000자 중 6000자. 더 좁은 인자로 다시 조회하라]"
    root = _snapshot(tmp_path, content=capped, case_id="case-heldout")
    report = audit.audit_snapshot(root)

    assert report["metrics"]["train_membership_failures"] == 1
    assert report["metrics"]["stored_source_already_capped"] == 1
    assert report["metrics"]["valid_source_json"] == 0
    assert report["per_ref"][0]["stored_source_already_capped"] is True


def test_run_writes_summary_and_per_input_report(tmp_path: Path) -> None:
    root = _snapshot(tmp_path)
    out = tmp_path / "out"
    summary = audit.run([root], out)

    assert summary["status"] == "complete"
    assert summary["totals"]["tool_refs"] == 1
    assert json.loads((out / "summary.json").read_text(encoding="utf-8")) == summary
    assert (out / "snap.json").exists()


def test_aggregate_extrema_and_unique_hashes_are_not_summed(tmp_path):
    root = _snapshot(tmp_path)
    single = audit.audit_snapshot(root)
    combined = audit.run([root, root], tmp_path / "combined")
    assert combined["totals"]["source_chars_max"] == single["metrics"]["source_chars_max"]
    assert combined["totals"]["source_chars_min"] == single["metrics"]["source_chars_min"]
    assert combined["totals"]["unique_source_sha256"] == single["metrics"]["unique_source_sha256"]
    assert combined["totals"]["tool_refs"] == 2 * single["metrics"]["tool_refs"]
