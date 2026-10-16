import importlib.util
import json
from pathlib import Path


def module():
    path = Path(__file__).resolve().parents[1] / "scripts/mcp_profile_recovered.py"
    spec = importlib.util.spec_from_file_location("profile_recovered", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def call(name: str, arguments: dict | None = None) -> dict:
    return {
        "id": f"call-{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {})},
    }


def row(case_id: str, run: str, tools: list[str], *, unsupervised: set[int] | None = None) -> dict:
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    unsupervised = unsupervised or set()
    for index, name in enumerate(tools):
        assistant = {
            "role": "assistant",
            "content": "",
            "reasoning_content": f"reason {name}",
            "tool_calls": [call(name, {"status": "confirmed"} if name == "submit_rca" else {"x": index})],
        }
        if index in unsupervised:
            assistant["supervise"] = False
        messages.append(assistant)
        messages.append({"role": "tool", "name": name, "tool_call_id": f"call-{name}", "content": json.dumps({"status": "normal"})})
    return {
        "case_id": case_id,
        "run": run,
        "source": "teacher",
        "root_f1": 1.0,
        "tool_calls": len(tools),
        "unsupervised_turns": len(unsupervised),
        "messages": messages,
        "tools": [],
    }


def write_dataset(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")


def test_profile_measures_final_random_selection_and_support_coverage(tmp_path):
    prof = module()
    dataset_dir = tmp_path / "datasets"
    rows = [
        row("case-a", "run-a", ["search_targets", "scan_metrics", "sample_logs", "read_timeseries", "list_events", "db_slow_queries", "submit_rca"]),
        row("case-b", "run-b", ["search_targets", "scan_metrics", "submit_rca"], unsupervised={1}),
    ]
    write_dataset(dataset_dir / "toy.jsonl", rows)

    report = prof.profile(dataset_dir, tmp_path / "out", turns_per_episode=3, tokenizer_json=None)
    source = report["sources"][0]

    assert source["examples"] == 2
    assert source["cases"] == 2
    assert source["turns"]["eligible_supervised"] == 9
    assert source["turns"]["unsupervised_tool_turns"] == 1
    assert source["turns"]["selected_by_final_plus_random"] == 5
    assert source["coverage"]["submit_turn_fraction_selected"] == 1.0
    assert source["coverage"]["pre_submit_support_fraction_selected"] < 1.0
    assert "structural turn-retention" in source["coverage"]["metric_scope"]
    assert {"6", "8", "12"} <= set(source["policy_comparison"]["final_greedy_unique_tool_then_temporal"])
    assert source["policy_comparison"]["final_greedy_unique_tool_then_temporal"]["6"]["first_tool_occurrence_fraction_selected"] >= source["policy_comparison"]["final_seeded_random"]["6"]["first_tool_occurrence_fraction_selected"]
    assert source["policy_comparison"]["final_pre_submit_unique_tool_then_temporal"]["6"]["pre_submit_support_fraction_selected"] == 1.0
    assert source["tools"]["final_submit_status_selected"] == {"confirmed": 2}
    assert (tmp_path / "out" / "profile.json").exists()
    assert (tmp_path / "out" / "profile-summary.md").exists()


def test_profile_reports_raw_content_token_scope_without_tokenizer(tmp_path):
    prof = module()
    dataset_dir = tmp_path / "datasets"
    write_dataset(dataset_dir / "toy.jsonl", [row("case-a", "run-a", ["submit_rca"])])

    report = prof.profile(dataset_dir, tmp_path / "out", turns_per_episode=6, tokenizer_json=None)

    assert report["tokenizer"]["status"] == "disabled"
    assert report["sources"][0]["lengths"]["tokens"] == {}
    assert "raw content only" in report["sources"][0]["lengths"]["token_scope"]
