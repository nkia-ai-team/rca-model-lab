import copy
import importlib.util
import json
from pathlib import Path

import pytest


def module():
    path = Path(__file__).resolve().parents[1] / "scripts/mcp_quarantine_supervision.py"
    spec = importlib.util.spec_from_file_location("mcp_quarantine_supervision", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def row(supervise: bool | None = None) -> dict:
    target = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "target reasoning",
        "tool_calls": [call("c2", "submit_rca", {"status": "insufficient"})],
    }
    if supervise is not None:
        target["supervise"] = supervise
    return {
        "case_id": "case-a",
        "run": "source/case-a/run1",
        "messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "prior private",
                "tool_calls": [call("c1", "describe_target", {"target": "T1"})],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "describe_target", "content": "{}"},
            target,
        ],
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows), encoding="utf-8")


def quarantine_item(qs, sample: dict, decision: str = "reject") -> dict:
    index = 4
    message = sample["messages"][index]
    return {
        "audit_group": "test",
        "audit_id": "00-4",
        "decision": decision,
        "run": sample["run"],
        "case_id": sample["case_id"],
        "message_index": index,
        "already_unsupervised": message.get("supervise") is False,
        "reasoning_sha256": qs.reasoning_sha(message),
        "tool_calls_sha256": qs.tool_calls_sha(message),
        "actor_prefix_sha256": qs.actor_prefix_sha(sample["messages"], index),
        "actor_prefix_policy": "template-normalized arguments + no supervise metadata + rolling_view(recent8,old1200,no prior reasoning)",
        "exact_source_and_actor_prefix_verified": True,
    }


def write_quarantine(qs, path: Path, joint: Path, items: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": qs.SUPPORTED_QUARANTINE_SCHEMA,
                "joint_sha256": qs.sha_file(joint),
                "hash_encoding": "test",
                "items": items,
                "unknown_policy": "unchanged; not approved by absence from quarantine",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_quarantine_masks_reject_and_hold_only_after_exact_hash_match(tmp_path):
    qs = module()
    sample = row()
    joint = tmp_path / "joint.jsonl"
    quarantine = tmp_path / "evidence-quarantine.json"
    write_jsonl(joint, [sample])
    write_quarantine(qs, quarantine, joint, [quarantine_item(qs, sample, decision="hold")])

    summary = qs.mask_joint(joint, quarantine, tmp_path / "out")

    candidate = [json.loads(line) for line in (tmp_path / "out/joint.quarantined.jsonl").read_text(encoding="utf-8").splitlines()]
    assert candidate[0]["messages"][4]["supervise"] is False
    assert candidate[0]["messages"][4]["reasoning_content"] == sample["messages"][4]["reasoning_content"]
    assert candidate[0]["messages"][4]["tool_calls"] == sample["messages"][4]["tool_calls"]
    assert summary["status"] == "candidate_only_not_readiness_or_admission_approval"
    assert summary["readiness"]["approved_for_training"] is False
    assert summary["decisions"] == {"reject": 0, "hold": 1}
    assert summary["newly_unsupervised"] == 1


def test_actor_prefix_change_fails_closed(tmp_path):
    qs = module()
    sample = row()
    original = copy.deepcopy(sample)
    joint = tmp_path / "joint.jsonl"
    quarantine = tmp_path / "evidence-quarantine.json"
    write_jsonl(joint, [sample])
    item = quarantine_item(qs, original)
    sample["messages"][1]["content"] = "changed prompt"
    write_jsonl(joint, [sample])
    quarantine_payload = {
        "schema": qs.SUPPORTED_QUARANTINE_SCHEMA,
        "joint_sha256": qs.sha_file(joint),
        "items": [item],
    }
    quarantine.write_text(json.dumps(quarantine_payload), encoding="utf-8")

    with pytest.raises(qs.QuarantineError, match="actor_prefix_sha256 mismatch"):
        qs.mask_joint(joint, quarantine, tmp_path / "out")


def test_already_false_marker_is_idempotent_and_model_input_stays_same(tmp_path):
    qs = module()
    sample = row(supervise=False)
    before_template = qs._template_messages(sample["messages"])
    before_prefix = qs.actor_prefix_sha(sample["messages"], 4)
    joint = tmp_path / "joint.jsonl"
    quarantine = tmp_path / "evidence-quarantine.json"
    write_jsonl(joint, [sample])
    write_quarantine(qs, quarantine, joint, [quarantine_item(qs, sample)])

    summary = qs.mask_joint(joint, quarantine, tmp_path / "out")

    candidate = json.loads((tmp_path / "out/joint.quarantined.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert qs._template_messages(candidate["messages"]) == before_template
    assert qs.actor_prefix_sha(candidate["messages"], 4) == before_prefix
    assert summary["already_unsupervised"] == 1
    assert summary["newly_unsupervised"] == 0
    assert summary["original_supervised_turns_capped"] == summary["candidate_supervised_turns_capped"] == 1
    assert (
        summary["original_supervised_assistant_turns_all_messages"]
        == summary["candidate_supervised_assistant_turns_all_messages"]
        == 1
    )


def test_unknown_identity_fails_closed(tmp_path):
    qs = module()
    sample = row()
    joint = tmp_path / "joint.jsonl"
    quarantine = tmp_path / "evidence-quarantine.json"
    write_jsonl(joint, [sample])
    item = quarantine_item(qs, sample)
    item["case_id"] = "missing"
    write_quarantine(qs, quarantine, joint, [item])

    with pytest.raises(qs.QuarantineError, match="not found"):
        qs.mask_joint(joint, quarantine, tmp_path / "out")
