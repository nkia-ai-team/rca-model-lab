import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from rca_lab.mcp import student
from rca_lab.mcp.context import rolling_view
from rca_lab.mcp.observations import ObservationStore

UUID = "11111111-2222-3333-4444-555555555555"
SEED = {"affected_targets": [UUID], "alarms": []}
ANSWER = {"status": "insufficient", "summary": "Insufficient observations", "causes": []}


def reply(name, args):
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    ]}}], "usage": {"prompt_tokens": 100}}


class MCP:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def list_tools(self):
        return [{"name": "probe", "inputSchema": {"type": "object", "properties": {}}}]

    def call(self, name, args):
        self.calls.append((name, args))
        return self.text, False


class RawMCP(MCP):
    def list_tools(self):
        return [{"name": "pg_sql", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]


def config(tmp_path, policy="structured-v1"):
    return student.StudentConfig(endpoint="http://unused", model="test", guided=False,
                                 observation_policy=policy, observation_dir=tmp_path,
                                 tool_result_max_chars=1200, max_turns=5)


def test_student_reader_is_local_and_full_response_is_aliased(tmp_path, monkeypatch):
    raw = json.dumps({"padding": "x" * 7000, "summary": "late fact", "target": UUID}, ensure_ascii=False)
    mcp = MCP(raw)
    bodies = []

    def post(cfg, body):
        bodies.append(body)
        if len(bodies) == 1:
            assert "read_observation" in [t["function"]["name"] for t in body["tools"]]
            return reply("probe", {})
        if len(bodies) == 2:
            view = json.loads(body["messages"][-1]["content"])
            return reply("read_observation", {"observation_id": view["observation_id"], "offset": 6900, "limit": 500})
        page = json.loads(body["messages"][-1]["content"])
        assert "late fact" in page["page"]["text"]
        assert UUID not in page["page"]["text"]
        return reply("submit_rca", ANSWER)

    monkeypatch.setattr(student, "_post_chat", post)
    run = student.run_student(config(tmp_path), SEED, mcp)
    assert run.stop_reason == "submitted" and run.tool_calls == 2 and run.tool_errors == 0
    assert len(mcp.calls) == 1
    artifacts = list(tmp_path.rglob("*"))
    assert any(p.is_file() and "late fact" in p.read_text() for p in artifacts)
    assert all(UUID not in p.read_text() for p in artifacts if p.is_file())
    assert run.alias_map == {UUID: "T1"}
    requests = [json.loads(line)["request"] for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert requests == bodies


def test_legacy_catalog_and_view_remain_unchanged(tmp_path, monkeypatch):
    mcp = MCP("x" * 7000)
    calls = []

    def post(cfg, body):
        calls.append(body)
        assert "read_observation" not in [t["function"]["name"] for t in body["tools"]]
        if len(calls) == 1:
            return reply("probe", {})
        assert body["messages"][-1]["content"] == student._truncate(mcp.text, 1200)
        return reply("submit_rca", ANSWER)

    monkeypatch.setattr(student, "_post_chat", post)
    student.run_student(config(tmp_path / "unused", "legacy"), SEED, mcp)
    assert not (tmp_path / "unused").exists()


def test_raw_student_uses_raw_system_prompt(tmp_path, monkeypatch):
    mcp = MCP("{}")
    bodies = []

    def post(cfg, body):
        bodies.append(body)
        return reply("submit_rca", ANSWER)

    monkeypatch.setattr(student, "_post_chat", post)
    cfg = config(tmp_path / "unused", "legacy")
    cfg.tool_surface = "raw"
    run = student.run_student(cfg, SEED, mcp)
    assert run.stop_reason == "submitted"
    system = bodies[0]["messages"][0]["content"]
    assert "원본 데이터를 가공 없이 돌려준다" in system
    assert "search_targets" not in system


def test_raw_sql_aliasing_resolves_only_string_literal_handles(tmp_path, monkeypatch):
    mcp = RawMCP(json.dumps({"rows": [], "refs": []}))
    responses = iter(
        [
            reply("pg_sql", {"query": "select T1.target_id from targets T1 where target_id = 'T1' and note = 'seen T1' -- T1"}),
            reply("submit_rca", ANSWER),
        ]
    )
    monkeypatch.setattr(student, "_post_chat", lambda *_: next(responses))
    cfg = config(tmp_path / "unused", "legacy")
    cfg.tool_surface = "raw"
    run = student.run_student(cfg, SEED, mcp)
    assert run.stop_reason == "submitted" and run.tool_errors == 0
    assert mcp.calls == [
        (
            "pg_sql",
            {"query": f"select T1.target_id from targets T1 where target_id = '{UUID}' and note = 'seen T1' -- T1"},
        )
    ]


def test_raw_sql_unknown_handle_check_is_limited_to_literals(tmp_path, monkeypatch):
    mcp = RawMCP("{}")
    responses = iter(
        [
            reply("pg_sql", {"query": "select T9.target_id from targets T9 where target_id = 'T9'"}),
            reply("submit_rca", ANSWER),
        ]
    )
    monkeypatch.setattr(student, "_post_chat", lambda *_: next(responses))
    cfg = config(tmp_path / "unused", "legacy")
    cfg.tool_surface = "raw"
    run = student.run_student(cfg, SEED, mcp)
    assert run.stop_reason == "submitted" and run.tool_errors == 1
    assert mcp.calls == []
    assert "T9" in run.messages[-3]["content"]


def test_rolling_and_emergency_shrink_preserve_json_and_retrieval(tmp_path):
    store = ObservationStore(tmp_path)
    text = store.present(json.dumps({"summary": "fact", "findings": list(range(1000))}), tool_name="probe", is_error=False, budget=6000)
    messages = [{"role": "tool", "content": text}] + [{"role": "tool", "content": "accepted"}] * 8
    before = json.dumps(messages)
    old = json.loads(rolling_view(messages)[0]["content"])
    assert old["observation_id"] == json.loads(text)["observation_id"]
    assert len(rolling_view(messages)[0]["content"]) <= 1200
    assert json.dumps(messages) == before
    student._shrink_context(messages, keep_chars=800)
    compact = json.loads(messages[0]["content"])
    assert compact["observation_id"] == old["observation_id"]
    page, error = store.read({"observation_id": compact["observation_id"], "offset": 0, "limit": 200}, budget=1200)
    assert not error and json.loads(page)["page"]["text"]


def test_invalid_policy_and_small_budget_fail_before_model(tmp_path):
    with pytest.raises(ValueError, match="policy"):
        student.run_student(config(tmp_path, "unknown"), SEED, MCP("{}"))
    cfg = config(tmp_path)
    cfg.tool_result_max_chars = 100
    with pytest.raises(ValueError, match="1200"):
        student.run_student(cfg, SEED, MCP("{}"))


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, Path("scripts") / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cross_policy_score_cache_rejected(tmp_path):
    script = load_script("mcp_case_run")
    run = tmp_path / "case-x" / "run1"
    run.mkdir(parents=True)
    (run / "score.json").write_text("{}")
    script.validate_observation_cache(tmp_path, "legacy")
    with pytest.raises(ValueError, match="new --run-name"):
        script.validate_observation_cache(tmp_path, "structured-v1")


def test_cross_tool_surface_score_cache_rejected(tmp_path):
    script = load_script("mcp_case_run")
    run = tmp_path / "case-x" / "run1"
    run.mkdir(parents=True)
    (run / "score.json").write_text("{}")
    script.validate_observation_cache(tmp_path, "legacy", "harness")
    with pytest.raises(ValueError, match="new --run-name"):
        script.validate_observation_cache(tmp_path, "legacy", "raw")


def test_raw_student_mcp_command_and_env(tmp_path):
    script = load_script("mcp_case_run")
    args = SimpleNamespace(runner="student", student_tools="raw")
    seed = {"time_window": {"first_event": "2026-01-01T00:00:00Z", "last_event": "2026-01-01T00:05:00Z"}}
    binary = tmp_path / "rca-mcp"
    command = script.student_mcp_command(args, seed, binary)
    env = script.raw_server_env({"RCA_PG_DSN": "pg", "RCA_CH_URL": "ch", "RCA_VM_URL": "vm"}, seed, binary)
    assert command == [sys.executable, "-m", "rca_lab.mcp.raw_server"]
    assert env["RCA_MCP_BINARY"] == str(binary)
    assert env["RCA_FIRST_EVENT"] == seed["time_window"]["first_event"]
    assert env["RCA_LAST_EVENT"] == seed["time_window"]["last_event"]
    assert env["RCA_PG_DSN"] == "pg" and env["RCA_CH_URL"] == "ch" and env["RCA_VM_URL"] == "vm"


def test_structured_experiment_cannot_silently_drop_reader_during_export(tmp_path):
    script = load_script("mcp_export_sft")
    run = tmp_path / "case-x" / "run1"
    run.mkdir(parents=True)
    (run / "score.json").write_text(json.dumps({"case_id": "case-x", "stop_reason": "submitted", "root_f1": 1,
                                               "observation_policy": "structured-v1"}))
    args = SimpleNamespace(min_root_f1=1, max_tool_calls=0, require_status=False, require_grounding=False, readiness_errors=[])
    assert script._student_rows(tmp_path, args, [], {"case-x"}) == []
    assert "policy-aware catalog" in args.readiness_errors[0]


@pytest.mark.parametrize("read_page", [False, True])
def test_hidden_refs_are_not_counted_until_page_is_seen(tmp_path, monkeypatch, read_page):
    ref = "evidence:" + "q" * 800
    raw = json.dumps({"padding": "x" * 7000, "refs": [ref]})
    mcp = MCP(raw)
    bodies = []

    def post(cfg, body):
        bodies.append(body)
        if len(bodies) == 1:
            return reply("probe", {})
        if len(bodies) == 2 and read_page:
            view = json.loads(body["messages"][-1]["content"])
            return reply("read_observation", {"observation_id": view["observation_id"], "offset": raw.index('"refs"'), "limit": 1200})
        return reply("submit_rca", ANSWER)

    monkeypatch.setattr(student, "_post_chat", post)
    cfg = config(tmp_path)
    cfg.tool_result_max_chars = 3000
    run = student.run_student(cfg, SEED, mcp)
    assert (ref in run.observed_refs) is read_page


def test_structured_ref_field_is_counted(tmp_path, monkeypatch):
    raw = json.dumps({"refs": ["pg:targets:" + UUID], "summary": "observation"})
    responses = iter([reply("probe", {}), reply("submit_rca", ANSWER)])
    monkeypatch.setattr(student, "_post_chat", lambda *_: next(responses))
    cfg = config(tmp_path)
    cfg.tool_result_max_chars = 6000
    run = student.run_student(cfg, SEED, MCP(raw))
    assert run.observed_refs == {"pg:targets:" + UUID}


def test_structured_context_recovery_keeps_reader_available(tmp_path, monkeypatch):
    mcp = MCP(json.dumps({"summary": "fact", "payload": list(range(400))}))
    calls = []

    def post(cfg, body):
        calls.append(body)
        if len(calls) == 1:
            return reply("probe", {})
        if len(calls) == 2:
            raise RuntimeError("maximum context length exceeded")
        assert body["tool_choice"] == "auto"
        view = next(m["content"] for m in body["messages"] if m["role"] == "tool")
        assert json.loads(view)["observation_id"]
        return reply("submit_rca", ANSWER)

    monkeypatch.setattr(student, "_post_chat", post)
    assert student.run_student(config(tmp_path), SEED, mcp).stop_reason == "submitted"


def test_no_data_refs_are_not_positive_evidence(tmp_path, monkeypatch):
    responses = iter([reply("probe", {}), reply("submit_rca", ANSWER)])
    monkeypatch.setattr(student, "_post_chat", lambda *_: next(responses))
    raw = json.dumps({"status": "no_data", "no_data_reason": "collector_gap", "refs": ["pg:missing"]})
    run = student.run_student(config(tmp_path), SEED, MCP(raw))
    assert not run.observed_refs


def test_refs_from_same_batch_are_not_seen_before_submit(tmp_path, monkeypatch):
    batch = reply("probe", {})
    batch["choices"][0]["message"]["tool_calls"].extend(reply("submit_rca", ANSWER)["choices"][0]["message"]["tool_calls"])
    monkeypatch.setattr(student, "_post_chat", lambda *_: batch)
    run = student.run_student(config(tmp_path), SEED, MCP(json.dumps({"refs": ["never-delivered"]})))
    assert run.stop_reason == "submitted" and not run.observed_refs


def test_structured_cache_requires_matching_runtime_provenance(tmp_path):
    script = load_script("mcp_case_run")
    run = tmp_path / "case-x" / "run1"
    run.mkdir(parents=True)
    (run / "score.json").write_text(json.dumps({"observation_policy": "structured-v1"}))
    with pytest.raises(ValueError, match="provenance"):
        script.validate_observation_cache(tmp_path, "structured-v1")
    (run / "observation-runtime.json").write_text(json.dumps({"source_sha256": {}}))
    with pytest.raises(ValueError, match="runtime differs"):
        script.validate_observation_cache(tmp_path, "structured-v1")


@pytest.mark.parametrize("hinted", [False, True])
def test_rl_export_refuses_unadmitted_structured_runs(tmp_path, hinted):
    script = load_script("mcp_rl_build")
    run = tmp_path / "case-x" / "run1"
    run.mkdir(parents=True)
    (run / "score.json").write_text(json.dumps({"case_id": "case-x", "observation_policy": "structured-v1", "hinted": hinted}))
    with pytest.raises(SystemExit, match="policy-aware catalog"):
        if hinted:
            script.load_hinted([tmp_path], {"case-x"}, max_unsupervised_fraction=1.0)
        else:
            script.load_groups(tmp_path)
