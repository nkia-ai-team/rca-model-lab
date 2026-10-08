"""Hermetic tests for the rca-mcp student/teacher surface (no GPU, no CLI, no DB)."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rca_lab.mcp.alias import UUIDAliaser
from rca_lab.mcp.answer import SUBMIT_TOOL, validate_result
from rca_lab.mcp.client import openai_tools
from rca_lab.mcp.golden import (
    ExpectedCase,
    RootCandidate,
    RootExpectation,
    candidates_from_result,
    root_f1,
)
from rca_lab.mcp.muse_grammar import MUSE_GUIDED_TOOL_STOP, guided_request_fields, structural_tag
from rca_lab.mcp.score import score_result
from rca_lab.mcp.seed import build_observed_seed

FIXTURES = Path(__file__).parent / "fixtures"
UUID_A = "414db206-d121-43b1-886c-78b240d23a7a"
UUID_B = "cf97076f-e72d-4c9e-b731-649592cfc5bf"


@pytest.fixture
def catalog() -> list[dict]:
    return json.loads((FIXTURES / "rca_mcp_tools.json").read_text(encoding="utf-8"))


def test_alias_roundtrip_and_unknown_handles() -> None:
    aliaser = UUIDAliaser()
    shown = aliaser.alias({"targets": [UUID_A, UUID_B.upper()], "refs": [f"pg:targets:{UUID_A}"]})
    assert shown == {"targets": ["T1", "T2"], "refs": ["pg:targets:T1"]}
    resolved = aliaser.resolve({"target": "T2", "support_refs": ["pg:targets:T1"]})
    assert resolved == {"target": UUID_B, "support_refs": [f"pg:targets:{UUID_A}"]}
    assert aliaser.unknown_handles({"target": "T9", "note": "T1 ok"}) == {"T9"}
    assert aliaser.mapping == {UUID_A: "T1", UUID_B: "T2"}


def test_alias_sql_resolution_only_touches_exact_string_literals() -> None:
    aliaser = UUIDAliaser()
    assert aliaser.alias({"targets": [UUID_A]}) == {"targets": ["T1"]}
    query = (
        "select T1.target_id, 'T1', 'prefix T1', \"T1\", $$T1$$ "
        "from targets T1 -- T1\n"
        "where note = 'T2' and quoted = 'can''t T1' and block = /* T1 */ 'ok'"
    )
    resolved, unknown = aliaser.resolve_sql_literals(query)
    assert unknown == {"T2"}
    assert f"'{UUID_A}'" in resolved
    assert "select T1.target_id" in resolved
    assert "'prefix T1'" in resolved
    assert '"T1"' in resolved
    assert f"$${UUID_A}$$" in resolved
    assert "-- T1" in resolved
    assert "/* T1 */" in resolved
    assert "'can''t T1'" in resolved


def test_validate_result_rejects_with_recovery_info() -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_result({"status": "confirmed", "summary": "", "causes": [{"target": "nope", "mechanism": "m", "support_refs": []}]})
    message = str(excinfo.value)
    assert "target must be a target UUID" in message
    assert "support_refs must cite" in message
    with pytest.raises(ValueError, match="at least one cause"):
        validate_result({"status": "provisional", "summary": "s", "causes": []})


def test_validate_result_normalises() -> None:
    result = validate_result(
        {
            "status": "provisional",
            "summary": " s ",
            "causes": [{"target": UUID_B.upper(), "mechanism": "lock", "support_refs": ["tool:x"]}],
            "external_causes": [{"id": "external:pg", "kind": "external_dependency", "name": "pg", "boundary_target": UUID_A, "evidence_refs": ["tool:y"]}],
        }
    )
    assert result["causes"][0]["target"] == UUID_B
    assert result["external_causes"][0]["boundary_target"] == UUID_A
    assert result["summary"] == "s"


def test_root_f1_and_score() -> None:
    expected = ExpectedCase.model_validate({"expected_status": "confirmed", "roots": [{"target_ids": [UUID_B], "target_aliases": ["db"]}]})
    result = {"status": "confirmed", "summary": "", "causes": [{"target": UUID_B, "mechanism": "m", "support_refs": ["tool:x"]}], "external_causes": []}
    assert root_f1(expected.roots, candidates_from_result(result)) == 1.0
    score = score_result(expected, result, turns=5, tool_calls=4, tool_errors=0, observed_refs={"tool:x"})
    assert score["strict_correct"] and score["reward"] == 1.0
    wrong = {**result, "causes": [{"target": UUID_A, "mechanism": "m", "support_refs": ["tool:x"]}]}
    score = score_result(expected, wrong, turns=5, tool_calls=4, tool_errors=0, observed_refs={"tool:x"})
    assert score["root_f1"] == 0.0 and score["unsupported_confirmation"] == 1 and score["reward"] == 0.0
    assert score_result(expected, None, turns=1, tool_calls=0, tool_errors=0, observed_refs=set())["status"] == "missing"


def test_structural_tag_covers_every_tool(catalog: list[dict]) -> None:
    tools = openai_tools(catalog) + openai_tools([SUBMIT_TOOL])
    tag = json.loads(structural_tag(tools))
    assert tag["type"] == "structural_tag"
    reasoning, call = tag["format"]["elements"]
    assert reasoning["elements"][0]["value"] == " to=self"
    begins = [element["begin"] for element in call["elements"]]
    assert len(begins) == len(tools)
    assert all(begin.startswith("<|start|>assistant to=") for begin in begins)
    only = json.loads(structural_tag(tools, only="submit_rca"))
    assert only["format"]["elements"][1]["begin"].startswith("<|start|>assistant to=submit_rca")
    fields = guided_request_fields(tools)
    assert fields["stop"] == [MUSE_GUIDED_TOOL_STOP] and fields["ignore_eos"] is True
    with pytest.raises(ValueError):
        structural_tag(tools, only="missing_tool")


def test_seed_reads_only_time_bounds(tmp_path: Path) -> None:
    case = tmp_path / "case-x"
    (case / "data/clickhouse").mkdir(parents=True)
    (case / "meta.json").write_text(json.dumps({"t1": "2026-01-01T00:10:00Z", "t2": "2026-01-01T00:20:00Z", "scenario_metadata": {"cause": "SECRET"}}), encoding="utf-8")
    base = 1_767_225_600_000_000_000  # 2026-01-01T00:00:00Z in ns
    rows = {
        "event_id": ["e1", "e2", "e3", "e4"],
        "occurred_at": pa.array([base + 5 * 60 * 10**9, base + 11 * 60 * 10**9, base + 12 * 60 * 10**9, base + 13 * 60 * 10**9], pa.timestamp("ns", tz="UTC")),
        "detector": ["d"] * 4,
        "event_kind": ["metric_anomaly"] * 4,
        "severity": ["warning", "warning", "cleared", "warning"],
        "target_id": [UUID_A, UUID_A, UUID_B, UUID_B],
        "service_name": ["", "svc", "", ""],
        "reason": ["r"] * 4,
        "transition": ["open", "open", "close", "open"],
        "class": ["anomaly"] * 4,
        "evidence": [json.dumps({"direction": "up", "expected": 1.0, "exemplars": {"metric": "m"}})] * 4,
        "raw": [json.dumps({"value": 2.5})] * 4,
    }
    pq.write_table(pa.table(rows), case / "data/clickhouse/lucida_events_local.parquet")
    seed = build_observed_seed(case, max_alarms=1)
    assert seed["selection"] == {"source_total": 4, "in_window": 3, "eligible": 2, "collapsed_repeats": 0, "per_target_max": 8, "dropped_by_target_cap": 0, "selected": 1, "truncated": True}
    assert seed["alarms"][0]["event_id"] == "e2" and seed["alarms"][0]["metric"] == "m" and seed["alarms"][0]["observed"] == 2.5
    assert seed["alarms"][0]["change"] == "above_baseline" and "direction" not in seed["alarms"][0]
    assert "SECRET" not in json.dumps(seed)


def test_export_ledger_to_student_messages(tmp_path: Path) -> None:
    from rca_lab.mcp.export import export_teacher_run

    run = tmp_path / "run1"
    run.mkdir()
    seed = {"time_window": {"first_event": "2026-01-01T00:10:00Z", "last_event": "2026-01-01T00:20:00Z"}, "alarms": [], "affected_targets": [UUID_A]}
    (run / "seed.json").write_text(json.dumps(seed), encoding="utf-8")
    ledger = [
        {"kind": "call", "thought": f"확인 {UUID_A}", "name": "describe_target", "arguments": {"target": UUID_A}, "response": json.dumps({"refs": [f"pg:targets:{UUID_A}"]}), "is_error": False},
        {"kind": "submit", "thought": "결론", "name": "submit_rca", "arguments": {"status": "provisional", "summary": "s", "causes": [{"target": UUID_A, "mechanism": "m", "support_refs": [f"pg:targets:{UUID_A}"]}], "external_causes": []}},
    ]
    (run / "ledger.jsonl").write_text("".join(json.dumps(record) + "\n" for record in ledger), encoding="utf-8")
    messages, mapping = export_teacher_run(run, max_turns=30)
    assert mapping == {UUID_A: "T1"}
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "tool", "assistant", "tool"]
    assert messages[2]["reasoning_content"] == "확인 T1"
    assert json.loads(messages[2]["tool_calls"][0]["function"]["arguments"]) == {"target": "T1"}
    assert "pg:targets:T1" in messages[3]["content"]
    assert json.loads(messages[4]["tool_calls"][0]["function"]["arguments"])["causes"][0]["target"] == "T1"
    assert messages[5]["content"] == "accepted"
    assert UUID_A not in json.dumps(messages)


def test_replay_ledger_wins_and_marks_rationalized_reasoning_stale(tmp_path: Path) -> None:
    import importlib.util

    from rca_lab.mcp.export import export_teacher_run, view_source

    run = tmp_path / "run1"
    run.mkdir()
    seed = {"time_window": {"first_event": "2026-01-01T00:10:00Z", "last_event": "2026-01-01T00:20:00Z"}, "alarms": [], "affected_targets": [UUID_A]}
    (run / "seed.json").write_text(json.dumps(seed), encoding="utf-8")

    def ledger(response: str) -> str:
        records = [
            {"kind": "call", "thought": "t", "name": "describe_target", "arguments": {"target": UUID_A}, "response": response, "is_error": False},
            {"kind": "submit", "thought": "c", "name": "submit_rca", "arguments": {"status": "provisional", "summary": "s", "causes": [], "external_causes": []}},
        ]
        return "".join(json.dumps(record) + "\n" for record in records)

    (run / "ledger.jsonl").write_text(ledger('{"refs":["r"],"summary":"old"}'), encoding="utf-8")
    assert view_source(run) == "collected"
    (run / "rationalize.json").write_text(json.dumps({"inconsistent": []}), encoding="utf-8")
    (run / "trajectory.rationalized.jsonl").write_text("{}\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("mcp_export_sft", Path(__file__).resolve().parents[1] / "scripts/mcp_export_sft.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert not module._stale_view(run)  # rationalized against the collected view, nothing replayed

    (run / "ledger.replay.jsonl").write_text(ledger('{"summary":"new","refs":["r"]}'), encoding="utf-8")
    messages, _ = export_teacher_run(run, max_turns=30)
    assert messages[3]["content"] == '{"summary":"new","refs":["r"]}'
    assert view_source(run).startswith("replay:")
    assert module._stale_view(run)  # reasoning written against the old responses must not be exported with the new ones


def test_pseudo_root_matches_free_text_external_id() -> None:
    from rca_lab.mcp.golden import RootCandidate, RootExpectation, root_matches

    expected = RootExpectation(pseudo_kind="external_dependency", pseudo_ids=("external:external-pg", "external:testbed-external-pg-mock"), boundary_target_ids=(UUID_A,))
    assert root_matches(expected, RootCandidate(variant="pseudo", pseudo_id="external:pg-rate-limit", pseudo_name="PG 429", pseudo_kind="external_dependency", boundary_target=UUID_A))
    assert not root_matches(expected, RootCandidate(variant="pseudo", pseudo_id="external:kafka-broker", pseudo_name="broker", pseudo_kind="external_dependency", boundary_target=UUID_A))
    assert not root_matches(expected, RootCandidate(variant="pseudo", pseudo_id="external:external-pg", pseudo_kind="kafka", boundary_target=UUID_A))


def test_shrink_context_truncates_largest_tool_messages() -> None:
    from rca_lab.mcp.student import _shrink_context

    messages = [
        {"role": "system", "content": "s"},
        {"role": "tool", "content": "a" * 100},
        {"role": "tool", "content": "b" * 5000},
        {"role": "tool", "content": "c" * 3000},
        {"role": "tool", "content": "d" * 4000},
    ]
    _shrink_context(messages, keep_chars=800)
    assert messages[1]["content"] == "a" * 100
    assert all(len(messages[index]["content"]) < 900 for index in (2, 3, 4))
    assert messages[2]["content"].startswith("b" * 800)


def test_root_f1_ignores_same_root_named_at_two_layers() -> None:
    expected = (RootExpectation(target_ids=(UUID_A, UUID_B)),)  # UUID_B = the k8s deployment of the same service
    both = [RootCandidate(variant="target", target_id=UUID_A), RootCandidate(variant="target", target_id=UUID_B)]
    assert root_f1(expected, both) == 1.0
    wrong_extra = [RootCandidate(variant="target", target_id=UUID_A), RootCandidate(variant="target", target_id="00000000-0000-0000-0000-000000000000")]
    assert root_f1(expected, wrong_extra) == pytest.approx(2 / 3)


def test_split_registry_covers_every_capture_and_isolates_sealed_scenarios():
    """Every capture on disk must be assigned, and no scenario may appear in both train and
    sealed/validation — a re-capture of a sealed scenario is a different case id but the same
    incident, so an unassigned one would slip past the export's sealed refusal."""
    import json
    from pathlib import Path

    import yaml

    cases_root = Path("/data/eval-cases")
    if not cases_root.exists():
        pytest.skip("eval-case captures are not mounted here")
    split = yaml.safe_load(Path("configs/teacher/v45-family-split-val.yaml").read_text(encoding="utf-8"))
    assigned = {case: part for part in ("train", "sealed_eval", "validation", "validation_pending_golden", "excluded") for case in (split.get(part) or [])}
    on_disk = {path.name for path in cases_root.glob("case-*")}
    assert not on_disk - set(assigned), f"unassigned captures: {sorted(on_disk - set(assigned))}"

    scenarios: dict[str, set[str]] = {}
    for case, part in assigned.items():
        meta = cases_root / case / "meta.json"
        if not meta.exists():
            continue
        scenario = json.loads(meta.read_text(encoding="utf-8")).get("scenario_id")
        scenarios.setdefault(scenario, set()).add(part)
    for scenario, parts in scenarios.items():
        if "train" in parts:
            assert not parts & {"sealed_eval", "validation", "validation_pending_golden"}, f"scenario {scenario} is in train and {parts - {'train'}}"


def test_seed_storm_suppression() -> None:
    from rca_lab.mcp.seed import _cap_per_target, _collapse_repeats

    def view(i: int, target: str, metric: str) -> dict:
        return {"event_id": f"e{i}", "occurred_at": f"2026-01-01T00:00:{i:02d}.000000Z", "target_id": target, "kind": "metric_anomaly", "class": "anomaly", "metric": metric, "reason": "r", "change": "above_baseline"}

    # 60 identical storm alarms on A, one distinct alarm on B two minutes later
    storm = [view(i, "A", "io_write") for i in range(60)] + [{**view(59, "B", "restarts"), "occurred_at": "2026-01-01T00:02:00.000000Z", "event_id": "b1"}]
    collapsed, repeats = _collapse_repeats(storm)
    assert repeats == 59 and [v["target_id"] for v in collapsed] == ["A", "B"]
    assert collapsed[0]["repeat_count"] == 60 and collapsed[0]["last_at"] == "2026-01-01T00:00:59.000000Z"
    assert "repeat_count" not in collapsed[1]

    # 12 distinct alarms on A crowd out B unless the per-target cap holds
    many = [view(i, "A", f"m{i}") for i in range(12)] + [view(30, "B", "restarts")]
    capped, dropped = _cap_per_target(many, 8)
    assert dropped == 4 and [v["target_id"] for v in capped][-1] == "B" and len(capped) == 9
    assert _cap_per_target(many, 0) == (many, 0)


def test_reasoning_guard_keeps_atem_markup_out_of_the_reasoning_channel():
    from rca_lab.mcp.muse_grammar import REASONING_EXCLUDES, structural_tag_exact

    tools = [{"type": "function", "function": {"name": "probe", "parameters": {"type": "object", "properties": {}}}}]
    guarded = json.loads(structural_tag(tools))["format"]["elements"][0]["elements"][1]["content"]
    assert guarded == {"type": "any_text", "excludes": list(REASONING_EXCLUDES)}
    assert "<atem:" in REASONING_EXCLUDES and "<|start|>" in REASONING_EXCLUDES
    legacy = json.loads(structural_tag(tools, reasoning_guard=False))["format"]["elements"][0]["elements"][1]["content"]
    assert legacy["type"] == "any_tokens"
    exact = json.loads(structural_tag_exact("probe", {}))["format"]["elements"][0]["elements"][1]["content"]
    assert exact["type"] == "any_text"


def test_post_chat_retries_transport_errors_only(monkeypatch):
    import io
    import urllib.error

    from rca_lab.mcp import student

    calls = []

    def flaky(request, timeout):
        calls.append(1)
        if len(calls) <= 2:
            raise ConnectionResetError("reset by peer") if len(calls) == 1 else urllib.error.URLError(ConnectionRefusedError("refused"))
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(student.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(student.time, "sleep", lambda s: None)
    config = student.StudentConfig(endpoint="http://x/v1", model="m", transport_retry_delays=(1, 1, 1))
    assert student._post_chat(config, {}) == {"ok": True} and len(calls) == 3

    def http_error(request, timeout):
        raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b"nope"))

    monkeypatch.setattr(student.urllib.request, "urlopen", http_error)
    with pytest.raises(RuntimeError, match="HTTP 400"):
        student._post_chat(config, {})

    def always_down(request, timeout):
        raise ConnectionResetError("down")

    monkeypatch.setattr(student.urllib.request, "urlopen", always_down)
    with pytest.raises(ConnectionResetError):
        student._post_chat(config, {})


def test_submit_rca_caps_candidates_and_scores_carry_precision_recall() -> None:
    from rca_lab.mcp.answer import MAX_CANDIDATES
    from rca_lab.mcp.golden import root_precision_recall

    uuid = "11111111-2222-3333-4444-{:012d}"
    cause = lambda i: {"target": uuid.format(i), "mechanism": "m", "support_refs": ["r"]}
    assert len(validate_result({"status": "provisional", "summary": "s", "causes": [cause(i) for i in range(MAX_CANDIDATES)]})["causes"]) == 3
    with pytest.raises(ValueError, match="at most 3 candidates"):
        validate_result({"status": "provisional", "summary": "s", "causes": [cause(i) for i in range(MAX_CANDIDATES + 1)]})
    expected = (RootExpectation(target_ids=(uuid.format(0),)),)
    right_wrong = [RootCandidate(variant="target", target_id=uuid.format(0)), RootCandidate(variant="target", target_id=uuid.format(9))]
    assert root_precision_recall(expected, right_wrong) == (0.5, 1.0)


def test_allowed_parallel_bottleneck_is_neither_hit_nor_false_positive() -> None:
    uuid = "11111111-2222-3333-4444-{:012d}"
    expected = (RootExpectation(target_ids=(uuid.format(0),)),)
    order, cart, other = (RootCandidate(variant="target", target_id=uuid.format(i)) for i in (0, 7, 9))
    allowed = (uuid.format(7),)
    assert root_f1(expected, [order, cart], allowed) == 1.0  # injected root + allowed parallel bottleneck
    assert root_f1(expected, [cart], allowed) == 0.0  # the injected root is still required
    assert root_f1(expected, [order, cart, other], allowed) == pytest.approx(2 / 3)  # a real false positive still costs
    assert root_f1(expected, [order, cart]) == pytest.approx(2 / 3)  # without the allowance cart is a false positive


def test_alias_sql_preserves_escaping_and_unfinished_queries():
    from rca_lab.mcp.alias import UUIDAliaser
    aliaser = UUIDAliaser()
    aliaser.handle_for("a0a6668c-f22e-429f-8733-aaaaaaa11111")
    for query in ["SELECT 'T1", r"SELECT E'foo\'T1'", 'SELECT `T1`, "T1"',
                  "SELECT /* a /* b */ 'T1' */ 1", "SELECT 'it''s T1'"]:
        assert aliaser.resolve_sql_literals(query) == (query, set())
