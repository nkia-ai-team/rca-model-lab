from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mcp_observation_probe.py"
SPEC = importlib.util.spec_from_file_location("mcp_observation_probe", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


def write_snapshot(root: Path, source: str) -> None:
    snap = root / "expanded" / "snapshots" / "00"
    snap.mkdir(parents=True)
    (snap / "trajectory.rationalized.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "content": "seed"}),
                json.dumps({"role": "tool", "message_position": 3, "name": "sample_logs", "content": source}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "expanded").mkdir(exist_ok=True)
    (root / "expanded" / "items.jsonl").write_text(
        json.dumps({"audit_id": "00-3", "case_id": "case-x"}) + "\n",
        encoding="utf-8",
    )


def test_select_probe_fact_uses_late_scalar_outside_legacy_prefix(tmp_path: Path) -> None:
    source = json.dumps(
        {
            "status": "normal",
            "summary": "has summary",
            "padding": "x" * 1300,
            "findings": [{"section": "summary_meta", "groups_observed": 7}],
        },
        separators=(",", ":"),
    )
    write_snapshot(tmp_path, source)

    fact = probe.select_probe_fact(tmp_path)

    assert fact.tier == "old_observation_gt_1200"
    assert fact.path == "$.findings[0].groups_observed"
    assert fact.expected == "7"
    assert fact.locator_offset < fact.value_offset
    assert fact.expected not in probe.truncated_view(source, probe.LEGACY_OLD_CHARS)[: probe.LEGACY_OLD_CHARS]


def test_answer_matches_is_exact() -> None:
    assert probe.answer_matches({"value": "7", "unknown": False}, "7")
    assert not probe.answer_matches({"value": "07", "unknown": False}, "7")
    assert not probe.answer_matches({"value": "7", "unknown": True}, "7")


def test_run_condition_reads_structured_observation_before_answering(tmp_path: Path) -> None:
    source = json.dumps({"status": "normal", "summary": "s", "padding": "x" * 1300, "late": "needle"}, separators=(",", ":"))
    rendered, store = probe.render_structured(source, "sample_logs", tmp_path)
    fact = probe.ProbeFact(
        audit_id="00-3",
        case_id="case-x",
        snapshot_path=str(tmp_path / "snap.jsonl"),
        message_position=3,
        tool_name="sample_logs",
        source_text=source,
        source_sha256=probe.sha256(source),
        source_chars=len(source),
        path="$.late",
        json_pointer="/late",
        expected="needle",
        locator_offset=source.index('"late"'),
        value_offset=source.index("needle"),
        tier="old_observation_gt_1200",
    )
    calls: list[dict[str, Any]] = []

    def fake_transport(_endpoint: str, body: dict[str, Any], _timeout: int) -> dict[str, Any]:
        calls.append(body)
        if len(calls) == 1:
            content = body["messages"][1]["content"]
            marker = '"observation_id":"'
            observation_id = content.split(marker, 1)[1].split('"', 1)[0]
            args = {"observation_id": observation_id, "offset": max(0, fact.value_offset - 20), "limit": 200}
            return {
                "choices": [{"message": {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_observation", "arguments": json.dumps(args)}}]}}],
                "usage": {"prompt_tokens": 10},
            }
        return {
            "choices": [{"message": {"content": "", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "answer_probe", "arguments": json.dumps({"value": "needle", "unknown": False})}}]}}],
            "usage": {"prompt_tokens": 20},
        }

    result = probe.run_condition(
        endpoint="http://unused/v1",
        model="m",
        condition="structured-v1",
        rendered_observation=rendered,
        fact=fact,
        store=store,
        seed=1,
        max_tool_calls=4,
        timeout=1,
        transport=fake_transport,
    )

    assert result["fact_match"] is True
    assert len(result["retrieval_calls"]) == 1
    assert result["tool_turns"] == 2


def test_load_fixed_fact_preserves_first_probe_source_and_pointer(tmp_path: Path) -> None:
    source = json.dumps({"status": "normal", "summary": "s", "findings": [{"axis": "entry"}, {"axis": "framework_segment"}]}, separators=(",", ":"))
    snap = tmp_path / "snap.jsonl"
    snap.write_text(json.dumps({"role": "tool", "message_position": 3, "name": "breakdown_endpoints", "content": source}) + "\n", encoding="utf-8")
    first = {
        "fact": {
            "audit_id": "00-36",
            "case_id": "case-x",
            "tier": "old_observation_gt_1200",
            "snapshot_path": str(snap),
            "message_position": 3,
            "tool_name": "breakdown_endpoints",
            "source_sha256": probe.sha256(source),
        }
    }
    first_path = tmp_path / "result.json"
    first_path.write_text(json.dumps(first), encoding="utf-8")

    fact = probe.load_fixed_fact(first_path, "/findings/1/axis")

    assert fact.source_sha256 == probe.sha256(source)
    assert fact.path == "$.findings[1].axis"
    assert fact.json_pointer == "/findings/1/axis"
    assert fact.expected == "framework_segment"


def test_length_finish_reason_is_output_budget_exhausted(tmp_path: Path) -> None:
    fact = probe.ProbeFact(
        audit_id="00-3",
        case_id="case-x",
        snapshot_path=str(tmp_path / "snap.jsonl"),
        message_position=3,
        tool_name="sample_logs",
        source_text="{}",
        source_sha256=probe.sha256("{}"),
        source_chars=2,
        path="$.late",
        json_pointer="/late",
        expected="needle",
        locator_offset=0,
        value_offset=0,
        tier="old_observation_gt_1200",
    )

    def fake_transport(_endpoint: str, _body: dict[str, Any], _timeout: int) -> dict[str, Any]:
        return {"choices": [{"finish_reason": "length", "message": {"content": "thinking", "tool_calls": []}}], "usage": {"completion_tokens": 1024}}

    result = probe.run_condition(
        endpoint="http://unused/v1",
        model="m",
        condition="legacy",
        rendered_observation="{}",
        fact=fact,
        store=None,
        seed=1,
        max_tool_calls=4,
        timeout=1,
        transport=fake_transport,
    )

    assert result["stop_reason"] == "output_budget_exhausted"
    assert result["incomplete"] is True
