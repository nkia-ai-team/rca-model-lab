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
INVALID_CAUSES_STRING = {"status": "confirmed", "summary": "Bad schema", "causes": "not-an-array"}
ALIASED_CAUSE_ANSWER = {
    "status": "confirmed",
    "summary": "Recovered schema",
    "causes": [{"target": "T1", "mechanism": "Observed bad config", "support_refs": ["pg:targets:T1"]}],
}


def reply(name, args):
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    ]}}], "usage": {"prompt_tokens": 100}}


def rejected_submit_messages():
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "case"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "bad-submit", "type": "function", "function": {"name": "submit_rca", "arguments": json.dumps(INVALID_CAUSES_STRING)}}
        ]},
        {"role": "tool", "tool_call_id": "bad-submit", "name": "submit_rca", "content": json.dumps({"error": "submit_rca rejected: causes must be a list"})},
    ]


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


@pytest.mark.parametrize("policy", ["legacy", "structured-v1", "structured-v2"])
def test_zero_prompt_budget_keeps_investigation_available_but_enforces_turn_cap(tmp_path, monkeypatch, policy):
    bodies = []

    def post(cfg, body):
        bodies.append(body)
        if len(bodies) <= 2:
            assert body["tool_choice"] == "auto"
            response = reply("probe", {})
            response["usage"]["prompt_tokens"] = 500_000
            return response
        assert body["tool_choice"]["function"]["name"] == "submit_rca"
        return reply("submit_rca", ANSWER)

    monkeypatch.setattr(student, "_post_chat", post)
    cfg = config(tmp_path, policy)
    cfg.max_prompt_tokens = 0
    cfg.max_turns = 2
    run = student.run_student(cfg, SEED, MCP("{}"))
    assert run.stop_reason == "submitted"
    assert run.turns == 3 and run.tool_calls == 2


def test_positive_prompt_budget_still_forces_submission(tmp_path, monkeypatch):
    bodies = []

    def post(cfg, body):
        bodies.append(body)
        if len(bodies) == 1:
            response = reply("probe", {})
            response["usage"]["prompt_tokens"] = 40_000
            return response
        assert body["tool_choice"]["function"]["name"] == "submit_rca"
        return reply("submit_rca", ANSWER)

    monkeypatch.setattr(student, "_post_chat", post)
    run = student.run_student(config(tmp_path), SEED, MCP("{}"))
    assert run.turns == 2


def test_forced_submit_validation_retry_default_keeps_legacy_rejection(tmp_path, monkeypatch):
    responses = iter([reply("probe", {}), reply("submit_rca", INVALID_CAUSES_STRING)])
    monkeypatch.setattr(student, "_post_chat", lambda *_: next(responses))
    cfg = config(tmp_path)
    cfg.max_turns = 1
    run = student.run_student(cfg, SEED, MCP("{}"))
    assert cfg.forced_submit_validation_retries == 0
    assert run.stop_reason == "forced_answer_rejected"
    assert run.result is None
    assert run.forced_submit_validation_retries == 0


def test_forced_submit_validation_retry_can_repair_schema_only(tmp_path, monkeypatch):
    bodies = []
    responses = iter([reply("probe", {}), reply("submit_rca", INVALID_CAUSES_STRING), reply("submit_rca", ANSWER)])

    def post(cfg, body):
        bodies.append(body)
        return next(responses)

    monkeypatch.setattr(student, "_post_chat", post)
    cfg = config(tmp_path)
    cfg.max_turns = 1
    cfg.forced_submit_validation_retries = 1
    mcp = MCP("{}")
    run = student.run_student(cfg, SEED, mcp)
    assert run.stop_reason == "submitted"
    assert run.result == {**ANSWER, "external_causes": []}
    assert run.forced_submit_validation_retries == 1
    assert run.tool_errors == 1 and mcp.calls == [("probe", {})]
    assert bodies[1]["tool_choice"]["function"]["name"] == "submit_rca"
    assert bodies[2]["tool_choice"]["function"]["name"] == "submit_rca"
    assert "submit_rca 검증 오류" in bodies[2]["messages"][-1]["content"]


def test_forced_submit_validation_retry_exhaustion_stays_rejected(tmp_path, monkeypatch):
    responses = iter([reply("probe", {}), reply("submit_rca", INVALID_CAUSES_STRING), reply("submit_rca", INVALID_CAUSES_STRING)])
    monkeypatch.setattr(student, "_post_chat", lambda *_: next(responses))
    cfg = config(tmp_path)
    cfg.max_turns = 1
    cfg.forced_submit_validation_retries = 1
    run = student.run_student(cfg, SEED, MCP("{}"))
    assert run.stop_reason == "forced_answer_rejected"
    assert run.result is None
    assert run.forced_submit_validation_retries == 1
    assert run.tool_errors == 2


def test_forced_phase_blocks_non_submit_tools_without_execution(tmp_path, monkeypatch):
    responses = iter([reply("probe", {}), reply("probe", {})])
    monkeypatch.setattr(student, "_post_chat", lambda *_: next(responses))
    cfg = config(tmp_path)
    cfg.max_turns = 1
    cfg.forced_submit_validation_retries = 2
    mcp = MCP("{}")
    run = student.run_student(cfg, SEED, mcp)
    assert run.stop_reason == "forced_answer_rejected"
    assert mcp.calls == [("probe", {})]
    assert run.tool_errors == 1
    assert "submit_rca 만 호출" in run.messages[-1]["content"]


def test_forced_submit_repair_accepts_valid_correction_without_mcp(tmp_path, monkeypatch):
    bodies = []

    def post(cfg, body):
        bodies.append(body)
        return reply("submit_rca", ALIASED_CAUSE_ANSWER)

    monkeypatch.setattr(student, "_post_chat", post)
    cfg = config(tmp_path)
    cfg.seed = 10
    messages = rejected_submit_messages()
    run = student.run_forced_submit_repair(cfg, messages, {UUID: "T1"}, retry_budget=1, observed_refs={"pg:targets:" + UUID})
    assert run.stop_reason == "submitted"
    assert run.turns == 1 and run.tool_calls == 0 and run.tool_errors == 0
    assert run.forced_submit_validation_retries == 1
    assert run.observed_refs == {"pg:targets:" + UUID}
    assert run.result["causes"][0]["target"] == UUID
    assert run.alias_map == {UUID: "T1"}
    assert bodies[0]["seed"] == 12
    assert bodies[0]["tool_choice"]["function"]["name"] == "submit_rca"
    assert [tool["function"]["name"] for tool in bodies[0]["tools"]] == ["submit_rca"]


def test_forced_submit_repair_exhaustion_returns_additional_errors(tmp_path, monkeypatch):
    responses = iter([reply("submit_rca", INVALID_CAUSES_STRING), reply("submit_rca", INVALID_CAUSES_STRING)])
    monkeypatch.setattr(student, "_post_chat", lambda *_: next(responses))
    run = student.run_forced_submit_repair(config(tmp_path), rejected_submit_messages(), {}, retry_budget=2)
    assert run.stop_reason == "forced_answer_rejected"
    assert run.result is None
    assert run.turns == 2
    assert run.tool_calls == 0 and run.tool_errors == 2
    assert run.forced_submit_validation_retries == 2


def test_forced_submit_repair_blocks_investigation_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(student, "_post_chat", lambda *_: reply("probe", {}))
    run = student.run_forced_submit_repair(config(tmp_path), rejected_submit_messages(), {}, retry_budget=1)
    assert run.stop_reason == "forced_answer_rejected"
    assert run.turns == 1
    assert run.tool_calls == 0 and run.tool_errors == 1
    assert "submit_rca 만 호출" in run.messages[-1]["content"]


def test_forced_submit_repair_preserves_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(student, "_post_chat", lambda *_: reply("submit_rca", ANSWER))
    messages = rejected_submit_messages()
    alias_map = {UUID: "T1"}
    before_messages = json.dumps(messages, sort_keys=True)
    before_alias = dict(alias_map)
    run = student.run_forced_submit_repair(config(tmp_path), messages, alias_map, retry_budget=1)
    assert run.stop_reason == "submitted"
    assert json.dumps(messages, sort_keys=True) == before_messages
    assert alias_map == before_alias
    assert run.messages is not messages
    assert len(run.messages) > len(messages)


def test_forced_submit_repair_requires_submit_rejection_source(tmp_path):
    messages = rejected_submit_messages()
    messages[-1] = {"role": "tool", "tool_call_id": "bad-submit", "name": "submit_rca", "content": "accepted"}
    with pytest.raises(ValueError, match="rejection"):
        student.run_forced_submit_repair(config(tmp_path), messages, {}, retry_budget=1)


def test_v2_physical_context_limit_is_explicit_failure(tmp_path, monkeypatch):
    def post(*_):
        raise RuntimeError("maximum context length exceeded")

    monkeypatch.setattr(student, "_post_chat", post)
    cfg = config(tmp_path, "structured-v2")
    cfg.max_prompt_tokens = 0
    with pytest.raises(RuntimeError, match="maximum context length"):
        student.run_student(cfg, SEED, MCP("{}"))


def test_v2_read_evidence_survives_rolling_and_explicit_compaction(tmp_path):
    store = ObservationStore(tmp_path, policy="structured-v2")
    initial = json.loads(store.present(json.dumps({"findings": "X" * 7000 + " -Xmx96m"}), "probe", False, 6000))
    page, error = store.read({"observation_id": initial["observation_id"], "json_pointer": "/findings", "offset": 4000}, 6000)
    assert not error and "-Xmx96m" in json.loads(page)["page"]["text"]
    messages = [{"role": "tool", "name": "read_observation", "content": page}] + [
        {"role": "tool", "content": "accepted"} for _ in range(10)
    ]
    assert rolling_view(messages)[0]["content"] == page
    student._shrink_context(messages, keep_chars=800)
    assert messages[0]["content"] == page


def test_v2_full_field_uses_available_envelope_budget(tmp_path):
    findings = [{"actual": "x" * 3600, "changed_setting": "-Xmx96m"}]
    text = json.dumps({"status": "normal", "summary": "Not a health judgment", "findings": findings, "huge": "z" * 9000})
    store = ObservationStore(tmp_path, policy="structured-v2")
    rendered = store.present(text, "probe", False, 6000)
    view = json.loads(rendered)
    assert len(rendered) <= 6000
    assert view["fields"]["findings"] == findings
    assert "huge" not in view["fields"]
    assert view["presentation"]["omitted_field_count"] == 1
    assert view["version"] == "mcp-observation-v2"
    historical = json.loads(ObservationStore().present(text, "probe", False, 6000))
    assert "findings" not in historical["fields"]


def test_v2_initial_evidence_survives_rolling_and_emergency_compaction(tmp_path):
    store = ObservationStore(tmp_path, policy="structured-v2")
    findings = {"details": "x" * 2400, "changed_setting": "-Xmx96m"}
    initial = store.present(json.dumps({"findings": findings}), "probe", False, 6000)
    assert len(initial) > 1200
    assert json.loads(initial)["fields"]["findings"] == findings
    messages = [{"role": "tool", "name": "probe", "content": initial}] + [
        {"role": "tool", "content": "accepted"} for _ in range(10)
    ]
    assert rolling_view(messages)[0]["content"] == initial
    student._shrink_context(messages, keep_chars=800)
    assert messages[0]["content"] == initial


@pytest.mark.parametrize("with_call", [False, True])
def test_output_limit_is_capacity_failure_and_never_executes_partial_call(tmp_path, monkeypatch, with_call):
    cfg = config(tmp_path)
    cfg.max_tokens = 32768
    cfg.stop_on_output_limit = True
    cfg.response_log = tmp_path / "responses.jsonl"
    response = reply("probe", {}) if with_call else {"choices": [{"message": {"content": "", "reasoning_content": "unfinished"}}]}
    response["choices"][0]["finish_reason"] = "length"
    response["usage"] = {"prompt_tokens": 1234, "completion_tokens": 32768}
    bodies = []

    def post(config, body):
        bodies.append(body)
        return response

    monkeypatch.setattr(student, "_post_chat", post)
    mcp = MCP("{}")
    run = student.run_student(cfg, SEED, mcp)
    assert run.stop_reason == "output_budget_exhausted"
    assert run.output_limit_hits == 1 and run.turns == 1
    assert run.result is None and mcp.calls == []
    assert bodies[0]["max_tokens"] == 32768
    record = json.loads(cfg.response_log.read_text())
    assert record == {"turn": 1, "finish_reason": "length", "output_budget_exhausted": True,
                      "requested_max_tokens": 32768, "usage": response["usage"]}


def test_thinking_token_budget_is_omitted_by_default(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    bodies = []

    def post(config, body):
        bodies.append(body)
        return reply("submit_rca", ANSWER)

    monkeypatch.setattr(student, "_post_chat", post)
    run = student.run_student(cfg, SEED, MCP("{}"))
    assert run.stop_reason == "submitted"
    assert "thinking_token_budget" not in bodies[0]


def test_thinking_token_budget_is_top_level_request_field(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    cfg.max_tokens = 32768
    cfg.thinking_token_budget = 8192
    cfg.response_log = tmp_path / "responses.jsonl"
    bodies = []

    def post(config, body):
        bodies.append(body)
        return reply("submit_rca", ANSWER)

    monkeypatch.setattr(student, "_post_chat", post)
    run = student.run_student(cfg, SEED, MCP("{}"))
    assert run.stop_reason == "submitted"
    assert bodies[0]["max_tokens"] == 32768
    assert bodies[0]["thinking_token_budget"] == 8192
    record = json.loads(cfg.response_log.read_text())
    assert record["requested_thinking_token_budget"] == 8192


@pytest.mark.parametrize("budget", [0, -1, 4096])
def test_invalid_thinking_token_budget_is_rejected(tmp_path, budget):
    cfg = config(tmp_path)
    cfg.max_tokens = 4096
    cfg.thinking_token_budget = budget
    with pytest.raises(ValueError, match="thinking_token_budget"):
        student.run_student(cfg, SEED, MCP("{}"))


def test_default_output_limit_keeps_existing_nudge_behavior(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    response = {"choices": [{"finish_reason": "length", "message": {"content": "", "reasoning_content": "unfinished"}}]}
    monkeypatch.setattr(student, "_post_chat", lambda *_: response)
    run = student.run_student(cfg, SEED, MCP("{}"))
    assert cfg.max_tokens == 4096 and not cfg.stop_on_output_limit
    assert run.stop_reason == "no_tool_call"
    assert run.output_limit_hits == cfg.max_nudges + 1


def test_case_runner_persists_output_limit_metadata(tmp_path, monkeypatch):
    module = load_script("mcp_case_run")
    cfgs = []
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def run(cfg, *args):
        cfgs.append(cfg)
        return student.StudentRun(messages=[], result=None, stop_reason="output_budget_exhausted",
                                  turns=1, tool_calls=0, tool_errors=0, observed_refs=set(),
                                  prompt_tokens_last=200, wall_seconds=1, output_limit_hits=1)
    monkeypatch.setattr(module, "MCPClient", Client)
    monkeypatch.setattr(module, "run_student", run)
    args = SimpleNamespace(runner="student", endpoint="http://unused", model="qwen", no_alias=False,
                           no_guided=True, no_reasoning_guard=False, max_turns=60, max_prompt_tokens=0,
                           max_output_tokens=32768, stop_on_output_limit=True, request_timeout=3600,
                           tool_result_max_chars=6000, observation_policy="legacy", temperature=1,
                           top_p=.95, top_k=64, seed=0, student_tools="harness")
    seed = {"time_window": {"first_event": "start", "last_event": "end"}}
    mcp_binary = tmp_path / "mcp"
    mcp_binary.write_bytes(b"fake")
    module.run_one(args, "case", tmp_path, 1, seed, {}, mcp_binary, None, None)
    score = json.loads((tmp_path / "score.json").read_text())
    assert score["stop_reason"] == "output_budget_exhausted"
    assert score["output_limit_hits"] == 1 and score["max_output_tokens"] == 32768
    assert score["stop_on_output_limit"] is True
    assert cfgs[0].request_timeout == 3600
    assert cfgs[0].response_log == tmp_path / "responses.jsonl"


def test_case_runner_records_thinking_token_budget(tmp_path, monkeypatch):
    module = load_script("mcp_case_run")
    cfgs = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def run(cfg, *args):
        cfgs.append(cfg)
        return student.StudentRun(messages=[], result=None, stop_reason="no_tool_call",
                                  turns=1, tool_calls=0, tool_errors=0, observed_refs=set(),
                                  prompt_tokens_last=200, wall_seconds=1)

    monkeypatch.setattr(module, "MCPClient", Client)
    monkeypatch.setattr(module, "run_student", run)
    args = SimpleNamespace(runner="student", endpoint="http://unused", model="qwen", no_alias=False,
                           no_guided=True, no_reasoning_guard=False, max_turns=60, max_prompt_tokens=0,
                           max_output_tokens=32768, thinking_token_budget=8192,
                           stop_on_output_limit=True, request_timeout=3600,
                           tool_result_max_chars=6000, observation_policy="structured-v2", temperature=1,
                           top_p=.95, top_k=64, seed=0, student_tools="harness")
    seed = {"time_window": {"first_event": "start", "last_event": "end"}}
    mcp_binary = tmp_path / "mcp"
    mcp_binary.write_bytes(b"fake")
    module.run_one(args, "case", tmp_path, 1, seed, {}, mcp_binary, None, None)
    score = json.loads((tmp_path / "score.json").read_text())
    runtime = json.loads((tmp_path / "observation-runtime.json").read_text())
    assert score["thinking_token_budget"] == 8192
    assert runtime["thinking_token_budget"] == 8192
    assert cfgs[0].thinking_token_budget == 8192


def capacity_error(status=400, param="input_tokens"):
    from rca_lab.mcp.completion_errors import ChatCompletionError
    return ChatCompletionError(status, json.dumps({"error": {
        "type": "BadRequestError", "param": param,
        "message": "This model's maximum context length is 131072 tokens.", "code": status}}))


def test_legacy_exhausted_context_fallback_is_scored_terminal(tmp_path, monkeypatch):
    requests = []
    def post(cfg, body):
        requests.append(body)
        if len(requests) == 1:
            return reply("probe", {})
        raise capacity_error()
    monkeypatch.setattr(student, "_post_chat", post)
    cfg = config(tmp_path, "legacy")
    cfg.response_log = tmp_path / "responses.jsonl"
    cfg.max_tokens = 32768
    result = student.run_student(cfg, SEED, MCP("observed fact"))
    assert result.stop_reason == "context_budget_exhausted"
    assert result.result is None and result.turns == 3 and result.tool_calls == 1
    assert requests[1]["tool_choice"] == "auto"
    assert requests[2]["tool_choice"]["function"]["name"] == "submit_rca"
    assert all(r["max_tokens"] == 32768 for r in requests)
    errors = [json.loads(line) for line in cfg.response_log.read_text().splitlines()
              if "completion_error" in json.loads(line)]
    assert [r["terminal"] for r in errors] == [False, True]
    assert any(m["role"] == "tool" for m in result.messages)


@pytest.mark.parametrize("status,param", [(500,"input_tokens"),(400,"tools")])
def test_other_http_errors_are_not_terminal_model_failures(tmp_path, monkeypatch,status,param):
    def post(*args): raise capacity_error(status,param)
    monkeypatch.setattr(student, "_post_chat", post)
    # Even a matching diagnostic string cannot classify the wrong status/parameter.
    with pytest.raises(student.ChatCompletionError):
        student.run_student(config(tmp_path,"legacy"),SEED,MCP("fact"))


def test_legacy_dedicated_context_code_does_not_require_diagnostic_text(tmp_path, monkeypatch):
    calls=[]
    def post(cfg,body):
        calls.append(body)
        raise student.ChatCompletionError(400,json.dumps({"error":{
            "code":"context_length_exceeded","message":"Capacity exhausted"}}))
    monkeypatch.setattr(student,"_post_chat",post)
    result=student.run_student(config(tmp_path,"legacy"),SEED,MCP("fact"))
    assert result.stop_reason=="context_budget_exhausted" and len(calls)==2
    assert calls[0]["tool_choice"]=="auto"
    assert calls[1]["tool_choice"]["function"]["name"]=="submit_rca"
